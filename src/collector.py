"""Recogida de elementos de cada fuente + diagnóstico.

build_feed.py le pasa sus utilidades con init(...). Aquí viven:
  collect()           lee una fuente (según su tipo y sus claves en config/sources.yaml)
  description()       texto de cada elemento del feed
  save_diagnostics()  escribe data/diagnostics.json, guarda la memoria de "watch" en
                      data/state.json y deja un resumen en el log de Actions

Claves opcionales por fuente en config/sources.yaml:
  feeds:          lista de URLs de RSS/Atom oficiales (se leen todas). Si responden,
                  además se rastrea section_url, salvo que se ponga "scrape: false".
  coins_section:  true si section_url es una página dedicada a monedas; los enlaces
                  que parecen monedas se abren para mirar su título/texto
  scrape:         false para no rastrear section_url cuando hay feeds que responden
  menu_links:     true para NO descartar los enlaces que están dentro de <nav>, <header> o <footer>
                  (por defecto se descartan: son menús y categorías como "2-Euro-Münzen", no noticias)
  coin_filter:    strict (por defecto) | news | all | any   (ver relevance.py)
  kind:           etiqueta de los elementos que el filtro no sabe clasificar (p. ej. commemorative_2_euro)
  title_prefix:   texto que se antepone al titular de cada elemento (p. ej. "2 € conmemorativa: ")
  browser_ua:     true para pedir las páginas con cabeceras de navegador (webs que dan 403 a bots)
  max_age_days:   ignora elementos cuya fecha CONOCIDA sea más antigua (por defecto, sin límite)
  max_items:      máximo de enlaces aceptados al rastrear una página (por defecto 25)
  watch:          lista de URLs (o {url, title}) que se vigilan: si cambia su texto, sale un aviso
  type:           eurlex_sparql | boe | watch   (fuentes que no son una web normal)
"""

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup

import relevance
from relevance import is_relevant

MAX_SECOND_CHANCE = 15   # enlaces de una "coins_section" que se abren para mirar su detalle
DEFAULT_MAX_ITEMS = 25   # enlaces aceptados por página rastreada
OLD_STREAK_STOP = 5      # con max_age_days: tras 5 enlaces seguidos antiguos se deja de mirar el listado

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,es;q=0.8",
}

# Los rellena build_feed.py con init(...)
S = ROOT = DIAG_FILE = STATE_FILE = None
clean = get = parse_date = page_date = extract_image = None

DIAG = {}
CURRENT = {"id": None}
WATCH = None             # memoria de las páginas vigiladas (se carga de data/state.json al primer uso)

KIND_LABELS = {
    "commemorative_2_euro": "Moneda conmemorativa de 2 €",
    "commemorative": "Moneda conmemorativa",
    "national_side": "Nueva cara nacional (circulación)",
    "collector": "Moneda de colección / set",
    "plan": "Calendario o plan de emisión",
    "circulation": "Moneda de circulación",
    "euro_coin": "Moneda de euro",
    "page_change": "Cambio en una página oficial",
    "official_journal": "Diario Oficial / Boletín oficial",
}


def init(**helpers):
    """build_feed.py pasa aquí su sesión HTTP y sus utilidades."""
    globals().update(helpers)
    globals()["DIAG_FILE"] = helpers["ROOT"] / "data/diagnostics.json"
    globals()["STATE_FILE"] = helpers["ROOT"] / "data/state.json"


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------
def _err(error):
    return f"{type(error).__name__}: {error}"[:200]


def note(**fields):
    """Apunta datos de la fuente que se está consultando."""
    sid = CURRENT["id"]
    if sid is None:
        return
    record = DIAG.setdefault(sid, {})
    for key, value in fields.items():
        if isinstance(value, list) and isinstance(record.get(key), list):
            record[key].extend(value)
        else:
            record[key] = value


def save_diagnostics():
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feedparser": feedparser.__version__,
        "sources": DIAG,
    }
    DIAG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    _persist_watch()

    print("\nRESUMEN POR FUENTE")
    for sid, rec in DIAG.items():
        feeds = rec.get("feeds_tried", [])
        with_entries = sum(1 for f in feeds if f.get("entries"))
        scrape_info = rec.get("scrape", {})
        web = rec.get("section_http") or scrape_info.get("http") \
            or rec.get("section_error") or scrape_info.get("error") or "?"
        print(
            f"- {sid}: método={rec.get('method', '?')} web={str(web)[:60]} "
            f"feeds_con_entradas={with_entries}/{len(feeds)} "
            f"enlaces={scrape_info.get('anchors', '-')} relevantes={rec.get('relevant', 0)}"
        )


# ---------------------------------------------------------------------------
# HTTP y utilidades comunes
# ---------------------------------------------------------------------------
def _headers(source):
    """Cabeceras de navegador solo si la fuente lo pide con 'browser_ua: true'."""
    return BROWSER_HEADERS if source and source.get("browser_ua") else None


def fetch(url, source=None, timeout=30):
    """GET con la sesión de build_feed.py (misma User-Agent) y error si el estado no es 2xx."""
    response = S.get(url, timeout=timeout, headers=_headers(source))
    response.raise_for_status()
    return response


def _mode(source, scraping=False):
    """Nivel del filtro de relevancia de esta fuente (clave coin_filter)."""
    mode = str(source.get("coin_filter") or "strict").strip().lower()
    if mode not in relevance.MODES:
        note(config_warning=f"coin_filter desconocido: {mode!r} (se usa strict)")
        mode = "strict"
    if scraping and mode in ("all", "any"):
        mode = "news"      # en una página web, "all" aceptaría los menús ("Coins", "Collector coins")
    return mode


def _item_title(source, title):
    return f"[{source['country']}] {source.get('title_prefix') or ''}{title}"


def _final_kind(source, kind):
    """La clave 'kind' de la fuente solo rellena huecos: si el filtro ya sabe qué es, manda el filtro."""
    fallback = source.get("kind")
    if fallback in KIND_LABELS and kind in (None, "euro_coin"):
        return fallback
    return kind


def _too_old(date, known, source):
    """True si la fuente pide max_age_days y la fecha (conocida) es más antigua."""
    days = source.get("max_age_days")
    if not days or not known:
        return False
    return (datetime.now(timezone.utc) - date).days > int(days)


_UNSTABLE_PARAMS = re.compile(
    r"^(?:qid|utm_.*|fbclid|gclid|mc_cid|mc_eid|_ga|phpsessid|jsessionid|sessionid)$", re.I)


def _canonical(link):
    """Quita del enlace los parámetros que cambian en cada visita (qid de EUR-Lex, utm_*, sesiones).

    Sin esto, una misma noticia tendría un enlace distinto en cada ejecución y entraría duplicada.
    Si el enlace no lleva ninguno de esos parámetros se devuelve tal cual (los identificadores ya
    guardados no cambian)."""
    try:
        parts = urlsplit(link)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return link
    kept = [(k, v) for k, v in pairs if not _UNSTABLE_PARAMS.match(k)]
    if len(kept) == len(pairs):
        return link
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _iso_date(value):
    """'2026-09-30', '2026-09-30+02:00' o '2026-09-30T10:00:00Z' -> datetime UTC (o None)."""
    found = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", value or "")
    if not found:
        return None
    try:
        return datetime(int(found[1]), int(found[2]), int(found[3]), tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Descubrir y leer feeds
# ---------------------------------------------------------------------------
GUESSED_PATHS = ("/rss.xml", "/feed", "/feed.xml", "/rss", "/atom.xml")


def discover_feeds(url, source=None):
    advertised = []
    try:
        r = fetch(url, source)
    except Exception as error:
        # La página de sección falla (403, 404, tiempo agotado...): se anota y se
        # siguen probando las rutas típicas de la misma web.
        note(section_error=_err(error))
        parsed = urlparse(url)
    else:
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all("link", href=True):
            typ = (tag.get("type") or "").lower()
            if "rss" in typ or "atom" in typ:
                advertised.append(urljoin(r.url, tag["href"]))
        parsed = urlparse(r.url)
        note(
            section_http=r.status_code,
            section_title=soup.title.get_text(" ", strip=True)[:100] if soup.title else "",
            link_feeds=advertised,
        )

    base = f"{parsed.scheme}://{parsed.netloc}"
    guessed = [urljoin(base, p) for p in GUESSED_PATHS]
    return list(dict.fromkeys(advertised + guessed))


def parse_feed(feed_url, source):
    """Devuelve (elementos relevantes, nº total de entradas del feed)."""
    info = {"url": feed_url}
    mode = _mode(source)
    try:
        response = S.get(feed_url, timeout=30, headers=_headers(source))
        info["http"] = response.status_code
        info["type"] = response.headers.get("Content-Type", "")[:60]
        feed = feedparser.parse(response.content)
    except Exception as error:
        info["error"] = _err(error)
        note(feeds_tried=[info])
        return [], 0

    info["entries"] = len(feed.entries)
    if not feed.entries and feed.get("bozo") and response.status_code == 200:
        info["bozo"] = str(feed.get("bozo_exception"))[:120]   # 200 pero no es un feed

    dates = [e.get("published_parsed") or e.get("updated_parsed") for e in feed.entries]
    dates = [d for d in dates if d]
    if dates:
        info["newest"] = datetime(*max(dates)[:6]).strftime("%Y-%m-%d")

    result, sample, skipped_old = [], [], 0

    for entry in feed.entries[:100]:
        title = clean(entry.get("title", ""))
        raw_link = (entry.get("link", "") or "").strip()
        # Enlaces relativos ("/news/x") o sin protocolo ("//host/x"): se completan con la URL del feed
        link = _canonical(urljoin(response.url, raw_link)) if raw_link else ""
        summary = clean(entry.get("summary", "") or entry.get("description", ""))

        if not title or not link:
            continue

        relevant, kind = is_relevant(title, summary, mode=mode)
        if len(sample) < 15:
            sample.append({"title": title[:110], "ok": relevant})
        if not relevant:
            continue

        known = True
        dt = None
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            dt = datetime(*parsed[:6], tzinfo=timezone.utc)
        if not dt:
            dt = parse_date(entry.get("published") or entry.get("updated"))
        if not dt:
            dt, known = datetime.now(timezone.utc), False

        if _too_old(dt, known, source):
            skipped_old += 1
            continue

        result.append({
            "title": _item_title(source, title),
            "link": link,
            "summary": summary[:1800],
            "date": dt,
            "source": source["name"],
            "country": source["country"],
            "kind": _final_kind(source, kind),
            "image": "",
        })

    if feed.entries:
        info["relevant"] = len(result)
        info["sample"] = sample
        if skipped_old:
            info["skipped_old"] = skipped_old
    note(feeds_tried=[info])
    return result, len(feed.entries)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def fetch_detail(link, source=None):
    try:
        page = fetch(link, source)
        return {"soup": BeautifulSoup(page.text, "html.parser"), "url": page.url}
    except Exception:
        return None


def detail_text(soup):
    """Título, h1, metadescripción y arranque del texto de una página de detalle."""
    work = copy.copy(soup)          # copia: no tocamos el original (fechas, imagen)
    parts = []
    if work.title:
        parts.append(work.title.get_text(" ", strip=True))
    for tag in work.find_all("h1", limit=2):
        parts.append(tag.get_text(" ", strip=True))
    for meta in work.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in ("description", "og:title", "og:description", "twitter:description"):
            parts.append(meta.get("content") or "")
    for tag in work(["nav", "header", "footer", "aside", "script", "style", "noscript", "form"]):
        tag.decompose()
    body = work.find("main") or work.find("article") or work.body
    if body:
        parts.append(body.get_text(" ", strip=True)[:500])
    return clean(" ".join(parts))


_DOCUMENT = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|jpe?g|png|gif|mp[34])(?:$|[?#])", re.I)


def _is_document(link):
    """PDF, Word, imágenes...: se aceptan como enlace, pero no se descargan para leer su detalle."""
    return bool(_DOCUMENT.search(link))


def _child_of(link, page_url):
    """¿El enlace cuelga de la ruta de la página (p. ej. /announcements/xxx dentro de /announcements)?"""
    a, b = urlparse(link), urlparse(page_url)
    base = b.path.rstrip("/")
    return bool(base) and a.netloc == b.netloc and a.path.startswith(base + "/") and a.path.rstrip("/") != base


def _slug_title(link):
    """Título aproximado sacado de la URL ('cyprus-euro-coins-mint-year-2026' -> 'cyprus euro coins ...')."""
    parts = [s for s in urlparse(link).path.split("/") if s]
    if not parts:
        return ""
    text = re.sub(r"\.(?:html?|aspx?|php)$", "", parts[-1], flags=re.I)
    text = re.sub(r"[-_+]+", " ", text).strip()
    return text if len(text.split()) >= 3 and re.search(r"[^\W\d_]{3}", text) else ""


def _page_title(soup):
    """Título real de una página de detalle: h1, og:title o <title> sin el nombre del sitio."""
    for tag in soup.find_all("h1", limit=2):
        text = clean(tag.get_text(" ", strip=True))
        if len(text) >= 10:
            return text
    for meta in soup.find_all("meta"):
        if (meta.get("property") or "").lower() == "og:title" and len(clean(meta.get("content") or "")) >= 10:
            return clean(meta["content"])
    if soup.title:
        text = clean(re.split(r"\s[|–—]\s", soup.title.get_text(" ", strip=True))[0])
        if len(text) >= 10:
            return text
    return ""


def scrape(url, source):
    stats = {"anchors": 0, "accepted": 0, "second_chance": 0,
             "detail_errors": 0, "rejected_sample": [], "coin_links": []}
    mode = _mode(source, scraping=True)
    max_items = int(source.get("max_items") or DEFAULT_MAX_ITEMS)
    try:
        response = fetch(url, source)
    except Exception as error:
        stats["error"] = _err(error)
        note(scrape=stats)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    stats["http"] = response.status_code
    stats["page_title"] = soup.title.get_text(" ", strip=True)[:100] if soup.title else ""
    stats["html_chars"] = len(response.text)
    if mode != "strict":
        stats["coin_filter"] = mode

    coins_section = bool(source.get("coins_section"))
    keep_menu = source.get("menu_links") is True
    page_default_date = page_date(soup) or datetime.now(timezone.utc)
    result, seen, coin_seen, old_streak = [], set(), set(), 0

    for link_tag in soup.find_all("a", href=True):
        href = link_tag["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        title = clean(link_tag.get_text(" ", strip=True))
        link = _canonical(urljoin(response.url, href))

        # Enlaces que cuelgan de la ruta de la sección: para el diagnóstico (¿tienen texto?)
        via = ""
        if _child_of(link, response.url):
            child = stats.setdefault("child_links", {"count": 0, "no_text": 0, "sample": []})
            child["count"] += 1
            if len(title) < 10:
                child["no_text"] += 1
            if len(child["sample"]) < 6:
                child["sample"].append({"t": title[:50], "u": link[-70:]})

        # Enlaces sin texto (iconos "leer más"): se usa su atributo title/aria-label o, si cuelgan de la
        # sección, el título que lleva la propia URL. Si luego se acepta, se cambia por el h1 de la página.
        if len(title) < 10:
            alt = clean(link_tag.get("title") or link_tag.get("aria-label") or "")
            if len(alt) >= 10:
                title, via = alt, "attr"
            elif _child_of(link, response.url):
                slug = _slug_title(link)
                if slug:
                    title, via = slug, "slug"

        if len(title) < 10 or link in seen:
            continue
        stats["anchors"] += 1

        # Menús, cabeceras y pies: categorías fijas ("2-Euro-Münzen", "Collector coins"), no noticias.
        # Se anotan en el diagnóstico (coin_links) por si alguna es una sección útil.
        if not keep_menu and link_tag.find_parent(["nav", "header", "footer"]):
            stats["skipped_menu"] = stats.get("skipped_menu", 0) + 1
            if relevance.looks_coin_related(title):
                if link not in coin_seen and len(stats["coin_links"]) < 30:
                    coin_seen.add(link)
                    stats["coin_links"].append({"t": title[:90], "u": link, "ok": False, "menu": True})
                sample = stats.setdefault("skipped_menu_sample", [])
                if len(sample) < 8:
                    sample.append(title[:90])
            continue

        detail, extra = None, ""
        relevant, kind = is_relevant(title, "", mode=mode)

        # Segunda oportunidad: solo en secciones dedicadas a monedas, para enlaces
        # como "Coin dedicated to Kaunas" que no mencionan el euro en el texto.
        if (not relevant and coins_section and relevance.has_coin_word(title)
                and stats["second_chance"] < MAX_SECOND_CHANCE):
            stats["second_chance"] += 1
            detail = fetch_detail(link, source)
            if detail is None:
                stats["detail_errors"] += 1
            else:
                extra = detail_text(detail["soup"])
                relevant, kind = is_relevant(title, extra, strict=True)
                stats.setdefault("second_chance_log", []).append(
                    {"title": title[:80], "ok": relevant, "text": extra[:140]}
                )

        # Enlaces con aspecto de moneda, con su URL: sirve para descubrir las secciones buenas de una web
        if (link not in coin_seen and len(stats["coin_links"]) < 30
                and relevance.looks_coin_related(title)):
            coin_seen.add(link)
            stats["coin_links"].append({"t": title[:90], "u": link, "ok": relevant, **({"via": via} if via else {})})

        if not relevant:
            if len(stats["rejected_sample"]) < 15 and relevance.looks_coin_related(title):
                stats["rejected_sample"].append(title[:110])
            continue

        seen.add(link)

        # Solo para el diagnóstico: ¿el enlace aceptado está dentro de un menú o pie?
        if link_tag.find_parent(["nav", "header", "footer", "aside"]):
            stats["accepted_in_menu"] = stats.get("accepted_in_menu", 0) + 1
            stats.setdefault("accepted_in_menu_sample", [])
            if len(stats["accepted_in_menu_sample"]) < 8:
                stats["accepted_in_menu_sample"].append(title[:90])

        if detail is None and not _is_document(link):
            detail = fetch_detail(link, source)
            if detail is None:
                stats["detail_errors"] += 1

        if via == "slug" and detail is not None:
            title = _page_title(detail["soup"]) or title
        date, known, image = page_default_date, False, ""
        if detail is not None:
            found_date = page_date(detail["soup"])
            if found_date:
                date, known = found_date, True
            image = extract_image(detail["soup"], detail["url"])

        if _too_old(date, known, source):
            stats["skipped_old"] = stats.get("skipped_old", 0) + 1
            old_streak += 1
            if old_streak >= OLD_STREAK_STOP:      # los listados van de más nuevo a más antiguo
                stats["stopped_old_streak"] = True
                break
            continue
        old_streak = 0

        result.append({
            "title": _item_title(source, title),
            "link": link,
            "summary": f"{title}. {extra[:400]}" if extra else title[:1800],
            "date": date,
            "source": source["name"],
            "country": source["country"],
            "kind": _final_kind(source, kind),
            "image": image,
        })
        stats["accepted"] += 1

        if len(result) >= max_items:
            break

    if stats["anchors"] == 0:
        stats["warning"] = ("la página respondió pero sin enlaces utilizables: ¿mantenimiento, bloqueo o página "
                            "que se pinta con JavaScript? (mira page_title)")
    note(scrape=stats)
    return result


# ---------------------------------------------------------------------------
# Vigilar páginas estáticas (calendarios de emisión, listas oficiales...)
# ---------------------------------------------------------------------------
_BLOCKS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "dt", "dd",
           "caption", "figcaption", "blockquote", "pre", "summary"]
_VOLATILE = re.compile(
    r"(?i)\b(?:last\s+(?:updated|modified)|updated\s+on|page\s+generated|cookies?|copyright|"
    r"all\s+rights\s+reserved|javascript|printed\s+on)\b|©"
)
WATCH_MIN_LINE = 12      # líneas más cortas se ignoran
WATCH_SHOWN = 10         # líneas nuevas que se muestran en el aviso


def _watch_memory():
    global WATCH
    if WATCH is None:
        try:
            WATCH = dict(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("watch", {}))
        except Exception:
            WATCH = {}
    return WATCH


def _persist_watch():
    """Guarda la memoria de las páginas vigiladas dentro de data/state.json (clave "watch").

    build_feed.save_state() reescribe el estado entero justo antes de llamar aquí, así que
    se lee el archivo recién escrito, se añade "watch" y se vuelve a guardar."""
    if WATCH is None:
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state["watch"] = WATCH
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as error:
        print("AVISO: no se pudo guardar la memoria de watch:", _err(error))


def _page_lines(soup):
    """Texto de una página como lista de líneas (bloques 'hoja': párrafos, filas, listas...)."""
    for tag in soup(["script", "style", "noscript", "svg", "form", "iframe",
                     "nav", "header", "footer", "aside"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines, seen = [], set()
    for element in root.find_all(_BLOCKS):
        if element.find(_BLOCKS):
            continue
        text = clean(element.get_text(" ", strip=True))
        if len(text) < WATCH_MIN_LINE or not re.search(r"[^\W\d_]{3}", text) or _VOLATILE.search(text):
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            lines.append(text)
    return lines


def _line_hash(line):
    return hashlib.sha1(line.casefold().encode("utf-8")).hexdigest()[:8]


def watch_page(entry, source):
    """Compara una página con la última vez; si hay líneas nuevas devuelve un aviso."""
    url = entry["url"]
    info = {"url": url}
    try:
        response = fetch(url, source)
    except Exception as error:
        info["error"] = _err(error)
        note(watch=[info])
        return []

    content_type = response.headers.get("Content-Type", "").lower()
    page_title = ""
    if "html" in content_type or response.text.lstrip()[:1] == "<":
        soup = BeautifulSoup(response.text, "html.parser")
        page_title = clean(soup.title.get_text(" ", strip=True))[:120] if soup.title else ""
        lines = _page_lines(soup)
    else:   # PDF u otro archivo: solo se vigila si cambia el contenido completo
        lines = [f"archivo {content_type[:30]} {hashlib.sha1(response.content).hexdigest()}"]
    hashes = [_line_hash(line) for line in lines]
    info.update(http=response.status_code, lines=len(lines), title=page_title)

    memory, now = _watch_memory(), datetime.now(timezone.utc)
    previous = memory.get(url)

    if not lines:                     # página vacía o que se pinta con JavaScript: no se toca la memoria
        info["warning"] = "sin texto que vigilar"
        note(watch=[info])
        return []

    if previous is None:              # primera vez: se guarda como punto de partida, sin aviso
        memory[url] = {"hashes": hashes, "first": now.isoformat(), "checked": now.isoformat(),
                       "lines": len(lines)}
        info["baseline"] = True
        note(watch=[info])
        return []

    old = set(previous.get("hashes", []))
    if len(old) >= 20 and len(lines) < 0.5 * len(old):
        info["warning"] = f"página mucho más corta ({len(lines)} líneas frente a {len(old)}): se ignora"
        note(watch=[info])
        return []

    new_pairs = [(line, h) for line, h in zip(lines, hashes) if h not in old]
    removed = len(old - set(hashes))
    previous.update(hashes=hashes, checked=now.isoformat(), lines=len(lines))
    info.update(added=len(new_pairs), removed=removed)
    if not new_pairs:
        note(watch=[info])
        return []

    previous["changed"] = now.isoformat()
    ref = hashlib.sha1("|".join(h for _, h in new_pairs).encode()).hexdigest()[:8]
    shown = [line[:220] for line, _ in new_pairs[:WATCH_SHOWN]]
    more = len(new_pairs) - len(shown)
    name = (entry.get("title") or page_title or url)[:45]
    summary = "Líneas nuevas respecto a la última comprobación: " + " | ".join(shown)
    if more > 0:
        summary += f" | (y {more} más)"
    if len(old) >= 20 and len(new_pairs) > 0.6 * len(lines):
        summary = "Cambio muy grande (¿página rediseñada?). " + summary
    info["ref"] = ref
    note(watch=[info])
    return [{
        "title": f"[{source['country']}] Cambio en página oficial: {name} · {ref}",
        "link": f"{url}#cambio-{ref}",     # el fragmento hace que cada cambio sea un elemento distinto
        "summary": summary[:1800],
        "date": now,
        "source": source["name"],
        "country": source["country"],
        "kind": "page_change",
        "image": "",
    }]


def watch_pages(source):
    found = []
    entries = source.get("watch") or []
    if isinstance(entries, (str, dict)):
        entries = [entries]
    for raw in entries:
        entry = {"url": raw} if isinstance(raw, str) else dict(raw)
        if entry.get("url"):
            found.extend(watch_page(entry, source))
    return found


# ---------------------------------------------------------------------------
# EUR-Lex: Diario Oficial de la UE por la vía de datos abiertos (SPARQL de la Oficina de Publicaciones)
# ---------------------------------------------------------------------------
SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
_PREFIXES = (
    "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
)
_ENG = "<http://publications.europa.eu/resource/authority/language/ENG>"
# Variante A: por título en inglés (avisos "New national side of euro coins...", monedas conmemorativas...)
_QUERY_TITLE = _PREFIXES + """
SELECT DISTINCT ?work ?celex ?date ?title WHERE {
  ?work cdm:work_date_document ?date .
  FILTER (?date >= "@SINCE@"^^xsd:date)
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language @ENG@ ;
        cdm:expression_title ?title .
  FILTER (REGEX(STR(?title), "euro coin|euro commemorative|commemorative euro|commemorative coin|national side", "i"))
  OPTIONAL { ?work cdm:resource_legal_id_celex ?celex }
}
ORDER BY DESC(?date)
LIMIT 100
"""
# Variante B: por número CELEX de los avisos de la serie C (5AAAAXC...) de los dos últimos años.
# Solo "coin": con "euro" entraban los ~200 avisos de tipos de cambio ("Euro exchange rates") y llenaban el LIMIT.
_QUERY_CELEX = _PREFIXES + """
SELECT DISTINCT ?work ?celex ?date ?title WHERE {
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER (STRSTARTS(STR(?celex), "5@YEAR@XC") || STRSTARTS(STR(?celex), "5@YEAR1@XC"))
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language @ENG@ ;
        cdm:expression_title ?title .
  FILTER (REGEX(STR(?title), "coin", "i"))
  OPTIONAL { ?work cdm:work_date_document ?date }
}
ORDER BY DESC(?celex)
LIMIT 200
"""


def sparql_queries(since):
    year = datetime.now(timezone.utc).year
    title = _QUERY_TITLE.replace("@SINCE@", since.strftime("%Y-%m-%d")).replace("@ENG@", _ENG)
    celex = (_QUERY_CELEX.replace("@YEAR@", str(year)).replace("@YEAR1@", str(year - 1))
             .replace("@ENG@", _ENG))
    return [("por_titulo", title), ("por_celex", celex)]


def _run_sparql(endpoint, query, timeout=90):
    """Devuelve (filas o None, datos del intento). Prueba POST y, si falla, GET.
    Los intentos fallidos anteriores al último quedan en info["earlier_attempts"]. El servidor tardó entre
    20 y 52 s en las ejecuciones reales y una vez agotó los 60 s: de ahí los 90 s."""
    headers = {"Accept": "application/sparql-results+json"}
    info, failed = {}, []
    for method in ("POST", "GET"):
        started = time.time()
        try:
            if method == "POST":
                r = S.post(endpoint, data={"query": query}, headers=headers, timeout=timeout)
            else:
                r = S.get(endpoint, params={"query": query, "format": "application/sparql-results+json"},
                          headers=headers, timeout=timeout)
        except Exception as error:
            info = {"method": method, "error": _err(error), "seconds": round(time.time() - started, 1)}
            failed.append(info)
            continue
        info = {"method": method, "http": r.status_code, "seconds": round(time.time() - started, 1)}
        if r.status_code == 200:
            try:
                rows = r.json()["results"]["bindings"]
                if failed:
                    info["earlier_attempts"] = failed
                return rows, info
            except Exception as error:
                info["error"] = "la respuesta no es JSON SPARQL: " + _err(error)
        info["body_head"] = r.text[:200]
        failed.append(dict(info))
    if len(failed) > 1:
        info["earlier_attempts"] = failed[:-1]
    return None, info


_CELLAR_ID = re.compile(r"/cellar/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.I)


def _eurlex_link(celex, work):
    if celex:
        return f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
    found = _CELLAR_ID.search(work or "")
    if found:
        return f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=cellar:{found.group(1).lower()}"
    return work


_C_NOTICE = re.compile(r"^5\d{4}XC", re.I)           # avisos de la serie C del Diario Oficial: 52026XC00415
_C_CELEX_NUM = re.compile(r"^5(\d{4})XC0*(\d+)$", re.I)
_NOT_A_NOTICE_LINK = re.compile(r"publications\.europa\.eu/resource/cellar/|eur-lex\.europa\.eu/.*uri=cellar:", re.I)


def _oj_ref(celex):
    """52026XC00415 -> C/2026/415, la referencia con la que cita el Diario Oficial."""
    found = _C_CELEX_NUM.match(celex or "")
    return f"C/{found.group(1)}/{found.group(2)}" if found else (celex or "")


def purge_state(state):
    """Retira del estado los elementos de EUR-Lex sin CELEX (resúmenes legislativos como "Issuance of euro
    coins", que no son un aviso de moneda). Las reglas actuales ya no los dejan entrar."""
    items = state.get("items", {})
    gone = [key for key, item in items.items() if _NOT_A_NOTICE_LINK.search(item.get("link", ""))]
    for key in gone:
        del items[key]
    return len(gone)


def collect_eurlex(source):
    since_days = int(source.get("since_days") or 180)
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    endpoint = source.get("endpoint") or SPARQL_ENDPOINT
    mode = _mode(source)
    if mode == "strict":
        mode = "news"          # los títulos del Diario Oficial rara vez repiten "euro" y moneda a la vez
    found, seen, seen_works, runs, answered = [], set(), set(), [], False
    # La consulta por título es la lenta (30 s con 180 días y una vez agotó los 60 s): nunca mira más de
    # 180 días atrás. La de CELEX (≈21 s, respondió bien las dos veces) filtra por fecha aquí, en Python,
    # y aprovecha since_days completo.
    since_title = max(since, datetime.now(timezone.utc) - timedelta(days=180))

    for name, query in sparql_queries(since_title):
        rows, info = _run_sparql(endpoint, query)
        info["query"] = name
        if rows is None:
            runs.append(info)
            continue
        answered = True
        info["rows"] = len(rows)
        kept = skipped = 0
        for row in rows:
            title = clean((row.get("title") or {}).get("value", ""))
            celex = (row.get("celex") or {}).get("value", "")
            work = (row.get("work") or {}).get("value", "")
            raw_date = (row.get("date") or {}).get("value", "")
            if not title:
                continue
            if not _C_NOTICE.match(celex):
                skipped += 1      # resúmenes legislativos, reglamentos...: no son avisos del Diario Oficial
                continue
            date = _iso_date(raw_date)
            if date is not None and date < since:
                continue
            link = _eurlex_link(celex, work)
            if not link or link in seen or (work and work in seen_works):
                continue
            relevant, kind = is_relevant(title, "", mode=mode)
            if not relevant:
                continue
            seen.add(link)
            seen_works.add(work)
            kept += 1
            found.append({
                "title": f"[{source['country']}] {title} ({_oj_ref(celex)})",
                "link": link,
                "summary": f"Diario Oficial de la Unión Europea, serie C ({_oj_ref(celex)}). {title}"[:1800],
                "date": date or datetime.now(timezone.utc),
                "source": source["name"],
                "country": source["country"],
                "kind": kind,
                "image": "",
            })
        info["relevant"] = kept
        info["skipped_not_notice"] = skipped
        runs.append(info)

    note(method="sparql" if answered else "sparql_sin_respuesta", sparql=runs, relevant=len(found))
    return found


# ---------------------------------------------------------------------------
# BOE (España): API de datos abiertos del sumario diario; las órdenes de acuñación salen ahí
# ---------------------------------------------------------------------------
BOE_API = "https://boe.es/datosabiertos/api/boe/sumario/{day}"


def _walk_boe(node):
    if isinstance(node, dict):
        if "titulo" in node and "identificador" in node:
            yield node
        for value in node.values():
            yield from _walk_boe(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_boe(value)


def collect_boe(source):
    days_back = int(source.get("days_back") or 4)
    mode = _mode(source)
    if mode == "strict":
        mode = "news"          # los títulos del BOE dicen "monedas", casi nunca "euro"
    template = source.get("api") or BOE_API
    today = datetime.now(timezone.utc).date()
    found, seen, runs = [], set(), []

    for back in range(days_back):
        day = today - timedelta(days=back)
        url = template.format(day=day.strftime("%Y%m%d"))
        info = {"day": day.isoformat()}
        try:
            r = S.get(url, headers={"Accept": "application/json"}, timeout=30)
        except Exception as error:
            info["error"] = _err(error)
            runs.append(info)
            continue
        info["http"] = r.status_code
        if r.status_code != 200:          # 404 = ese día no hay BOE (domingos, festivos)
            runs.append(info)
            continue
        try:
            items = list(_walk_boe(r.json()))
        except Exception as error:
            info["error"] = "la respuesta no es JSON: " + _err(error)
            info["body_head"] = r.text[:150]
            runs.append(info)
            continue
        info["items"] = len(items)
        kept = 0
        for item in items:
            title = clean(str(item.get("titulo", "")))
            ident = str(item.get("identificador", ""))
            html_url = item.get("url_html")
            link = html_url if isinstance(html_url, str) and html_url else \
                f"https://www.boe.es/diario_boe/txt.php?id={ident}"
            if not title or link in seen:
                continue
            relevant, kind = is_relevant(title, "", mode=mode)
            if not relevant:
                continue
            seen.add(link)
            kept += 1
            found.append({
                "title": f"[{source['country']}] {title}",
                "link": link,
                "summary": f"BOE {day.isoformat()} ({ident}). {title}"[:1800],
                "date": datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                "source": source["name"],
                "country": source["country"],
                "kind": kind,
                "image": "",
            })
        info["relevant"] = kept
        runs.append(info)

    answered = any(run.get("http") == 200 for run in runs)
    note(method="boe" if answered else "boe_sin_respuesta", boe=runs, relevant=len(found))
    return found


# ---------------------------------------------------------------------------
# Una fuente completa
# ---------------------------------------------------------------------------
def collect(source):
    CURRENT["id"] = source.get("id") or source.get("name")
    DIAG[CURRENT["id"]] = {}
    note(name=source.get("name"), country=source.get("country"),
         section_url=source.get("section_url"))
    try:
        return _collect(source)
    except Exception as error:
        # build_feed.py ya imprime el error; aquí además queda en el diagnóstico.
        note(method="error", crash=_err(error), relevant=0)
        raise


def _collect(source):
    kind = str(source.get("type") or "").strip().lower()
    if kind == "eurlex_sparql":
        found = collect_eurlex(source)
    elif kind == "boe":
        found = collect_boe(source)
    elif kind == "watch":
        found = []
        note(method="watch")
    else:
        found = _collect_web(source)

    if source.get("watch"):
        found = list(found) + watch_pages(source)
        note(relevant=len(found))
    return found


def _collect_web(source):
    section_url = source.get("section_url")

    # 1) Feeds oficiales indicados a mano en sources.yaml (clave "feeds").
    #    Si responden, se leen y ADEMÁS se rastrea la página de la sección (donde suelen
    #    aparecer monedas que no tienen nota de prensa). Con "scrape: false" no se rastrea.
    manual = source.get("feeds") or []
    if isinstance(manual, str):          # por si se escribe "feeds: https://..." sin lista
        manual = [manual]
    found, answered = [], False
    for feed_url in manual:
        items, total = parse_feed(feed_url, source)
        found.extend(items)
        answered = answered or total > 0
    if answered:
        method = "feeds_a_mano"
        if section_url and source.get("scrape", True) is not False:
            seen = {item["link"] for item in found}
            for item in scrape(section_url, source):
                if item["link"] not in seen:
                    found.append(item)
                    seen.add(item["link"])
            method = "feeds_a_mano+scraping"
        note(method=method, relevant=len(found))
        return found

    if not section_url:
        note(method="sin_url", relevant=0)
        return []

    # 2) Feeds que anuncia la propia web o rutas típicas (/feed, /rss.xml...)
    for feed_url in discover_feeds(section_url, source):
        items, total = parse_feed(feed_url, source)
        if items:
            note(method="feed_descubierto", feed_url=feed_url, relevant=len(items))
            return items

    # 3) Scraping de la página de la sección
    items = scrape(section_url, source)
    note(method="scraping", relevant=len(items))
    return items


def description(item):
    kind = KIND_LABELS.get(item.get("kind"), "Posible emisión de circulación")
    text = (
        f"País: {item['country']}\n"
        f"Denominación: {item.get('denomination') or '—'}\n"
        f"Año: {item.get('year') or '—'}\n"
        f"Tipo: {kind}\n\n"
        f"{item['summary']}\n\n"
        f"Fuentes: {', '.join(item['sources'])}"
    )
    if item.get("image"):
        text += f"\n\nImagen oficial: {item['image']}"
    return text
