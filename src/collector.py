"""Recogida de elementos de cada fuente + diagnóstico.

build_feed.py le pasa sus utilidades con init(...). Aquí viven:
  collect()           lee una fuente (feeds indicados a mano > feeds descubiertos > scraping)
  description()       texto de cada elemento del feed
  save_diagnostics()  escribe data/diagnostics.json y un resumen en el log de Actions

Claves opcionales por fuente en config/sources.yaml:
  feeds:          lista de URLs de RSS/Atom oficiales (se leen todas). Si responden,
                  además se rastrea section_url, salvo que se ponga "scrape: false".
  coins_section:  true si section_url es una página dedicada a monedas; los enlaces
                  que parecen monedas se abren para mirar su título/texto
  scrape:         false para no rastrear section_url cuando hay feeds que responden
"""

import copy
import json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

import relevance
from relevance import is_relevant

MAX_SECOND_CHANCE = 15   # enlaces de una "coins_section" que se abren para mirar su detalle

# Los rellena build_feed.py con init(...)
S = ROOT = DIAG_FILE = None
clean = get = parse_date = page_date = extract_image = None

DIAG = {}
CURRENT = {"id": None}

KIND_LABELS = {
    "commemorative_2_euro": "Moneda conmemorativa de 2 €",
    "commemorative": "Moneda conmemorativa",
    "national_side": "Nueva cara nacional (circulación)",
    "collector": "Moneda de colección / set",
    "plan": "Calendario o plan de emisión",
    "circulation": "Moneda de circulación",
    "euro_coin": "Moneda de euro",
}


def init(**helpers):
    """build_feed.py pasa aquí su sesión HTTP y sus utilidades."""
    globals().update(helpers)
    globals()["DIAG_FILE"] = helpers["ROOT"] / "data/diagnostics.json"


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
# Descubrir y leer feeds
# ---------------------------------------------------------------------------
GUESSED_PATHS = ("/rss.xml", "/feed", "/feed.xml", "/rss", "/atom.xml")


def discover_feeds(url):
    advertised = []
    try:
        r = get(url)
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
    try:
        response = S.get(feed_url, timeout=30)
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

    result, sample = [], []

    for entry in feed.entries[:100]:
        title = clean(entry.get("title", ""))
        raw_link = (entry.get("link", "") or "").strip()
        # Enlaces relativos ("/news/x") o sin protocolo ("//host/x"): se completan con la URL del feed
        link = urljoin(response.url, raw_link) if raw_link else ""
        summary = clean(entry.get("summary", "") or entry.get("description", ""))

        if not title or not link:
            continue

        relevant, kind = is_relevant(title, summary)
        if len(sample) < 12:
            sample.append({"title": title[:110], "ok": relevant})
        if not relevant:
            continue

        dt = None
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            dt = datetime(*parsed[:6], tzinfo=timezone.utc)
        if not dt:
            dt = parse_date(entry.get("published") or entry.get("updated"))
        if not dt:
            dt = datetime.now(timezone.utc)

        result.append({
            "title": f"[{source['country']}] {title}",
            "link": link,
            "summary": summary[:1800],
            "date": dt,
            "source": source["name"],
            "country": source["country"],
            "kind": kind,
            "image": "",
        })

    if feed.entries:
        info["relevant"] = len(result)
        info["sample"] = sample
    note(feeds_tried=[info])
    return result, len(feed.entries)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def fetch_detail(link):
    try:
        page = get(link)
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


def scrape(url, source):
    stats = {"anchors": 0, "accepted": 0, "second_chance": 0,
             "detail_errors": 0, "rejected_sample": []}
    try:
        response = get(url)
    except Exception as error:
        stats["error"] = _err(error)
        note(scrape=stats)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    stats["http"] = response.status_code
    stats["page_title"] = soup.title.get_text(" ", strip=True)[:100] if soup.title else ""
    stats["html_chars"] = len(response.text)

    coins_section = bool(source.get("coins_section"))
    page_default_date = page_date(soup) or datetime.now(timezone.utc)
    result, seen = [], set()

    for link_tag in soup.find_all("a", href=True):
        title = clean(link_tag.get_text(" ", strip=True))
        link = urljoin(response.url, link_tag["href"])

        if len(title) < 10 or link in seen:
            continue
        stats["anchors"] += 1

        detail, extra = None, ""
        relevant, kind = is_relevant(title, "")

        # Segunda oportunidad: solo en secciones dedicadas a monedas, para enlaces
        # como "Coin dedicated to Kaunas" que no mencionan el euro en el texto.
        if (not relevant and coins_section and relevance.has_coin_word(title)
                and stats["second_chance"] < MAX_SECOND_CHANCE):
            stats["second_chance"] += 1
            detail = fetch_detail(link)
            if detail is None:
                stats["detail_errors"] += 1
            else:
                extra = detail_text(detail["soup"])
                relevant, kind = is_relevant(title, extra, strict=True)
                stats.setdefault("second_chance_log", []).append(
                    {"title": title[:80], "ok": relevant, "text": extra[:140]}
                )

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

        if detail is None:
            detail = fetch_detail(link)
            if detail is None:
                stats["detail_errors"] += 1

        date, image = page_default_date, ""
        if detail is not None:
            date = page_date(detail["soup"]) or date
            image = extract_image(detail["soup"], detail["url"])

        result.append({
            "title": f"[{source['country']}] {title}",
            "link": link,
            "summary": f"{title}. {extra[:400]}" if extra else title[:1800],
            "date": date,
            "source": source["name"],
            "country": source["country"],
            "kind": kind,
            "image": image,
        })
        stats["accepted"] += 1

        if len(result) >= 25:
            break

    note(scrape=stats)
    return result


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
    for feed_url in discover_feeds(section_url):
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
