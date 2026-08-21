import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config/sources.yaml").read_text(encoding="utf-8"))

OUT = ROOT / "public/euro-coins.xml"
STATE_FILE = ROOT / "data/state.json"

OUT.parent.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

S = requests.Session()
S.headers["User-Agent"] = "EuroCoinsOfficialFeed/2.0"

INCLUDE = [x.lower() for x in CFG.get("include", [])]
EXCLUDE = [x.lower() for x in CFG.get("exclude", [])]

EURO_TERMS = (
    "euro", "€", "2 euro", "2-euro",
    "1 euro", "50 cent", "20 cent", "10 cent",
    "5 cent", "2 cent", "1 cent"
)

COMM_2_EURO = (
    "commemorative 2 euro",
    "commemorative 2-euro",
    "2 euro commemorative",
    "2-euro commemorative",
    "2 € commemorative",
    "2-euro-gedenkmünze",
    "2-euromunt",
    "2 euron juhlaraha",
    "moneda conmemorativa de 2 euros",
    "pièce commémorative de 2 euros",
    "moneta commemorativa da 2 euro",
    "moneta commemorativa da 2€",
)

CIRCULATION = (
    "intended for circulation",
    "for circulation",
    "in circulation",
    "circulation",
    "circulating",
    "circulazione",
    "circolazione",
    "circulatie",
    "umlauf",
    "umlaufmünze",
    "moneda de circulación",
    "monedas destinadas a la circulación",
    "pièces destinées à la circulation",
)

def clean(text):
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    ).strip()


def get(url):
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return r


def parse_date(value):
    if not value:
        return None

    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        pass

    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        return dt.replace(
            tzinfo=dt.tzinfo or timezone.utc
        ).astimezone(timezone.utc)
    except Exception:
        return None


def page_date(soup):
    # Metadatos
    for meta in soup.find_all("meta"):
        key = (
            meta.get("property")
            or meta.get("name")
            or ""
        ).lower()

        value = meta.get("content")

        if value and any(x in key for x in (
            "article:published_time",
            "article:modified_time",
            "datepublished",
            "datecreated",
            "date",
            "publish"
        )):
            dt = parse_date(value)
            if dt:
                return dt

    # <time datetime="">
    for tag in soup.find_all("time"):
        if tag.get("datetime"):
            dt = parse_date(tag["datetime"])
            if dt:
                return dt

    # JSON-LD
    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):
        try:
            data = json.loads(
                script.string or script.get_text()
            )

            if isinstance(data, dict):
                data = [data]

            for obj in data:
                if not isinstance(obj, dict):
                    continue

                for key in (
                    "datePublished",
                    "dateCreated",
                    "dateModified"
                ):
                    dt = parse_date(obj.get(key))
                    if dt:
                        return dt

        except Exception:
            pass

    return None


def discover_feeds(url):
    try:
        r = get(url)
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    feeds = []

    for tag in soup.find_all("link", href=True):
        typ = (tag.get("type") or "").lower()
        rel = " ".join(tag.get("rel") or []).lower()

        if (
            "rss" in typ
            or "atom" in typ
            or (
                "alternate" in rel
                and ("rss" in typ or "atom" in typ)
            )
        ):
            feeds.append(urljoin(r.url, tag["href"]))

    parsed = urlparse(r.url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in (
        "/rss.xml",
        "/feed",
        "/feed.xml",
        "/rss",
        "/atom.xml"
    ):
        feeds.append(urljoin(base, path))

    return list(dict.fromkeys(feeds))


def is_relevant(title, summary):
    text = clean(f"{title} {summary}").lower()

    # Siempre aceptamos las 2 € conmemorativas.
    if any(term in text for term in COMM_2_EURO):
        return True, "commemorative_2_euro"

    # Para las demás monedas necesitamos una referencia clara
    # a circulación.
    has_euro = any(term in text for term in EURO_TERMS)
    has_circulation = any(term in text for term in CIRCULATION)

    if has_euro and has_circulation:
        return True, "circulation"

    # Usamos los términos del YAML como ayuda secundaria.
    if has_euro and any(term in text for term in INCLUDE):
        if not any(term in text for term in EXCLUDE):
            return True, "possible"

    return False, None


def extract_image(soup, base_url):
    for meta in soup.find_all("meta"):
        key = (
            meta.get("property")
            or meta.get("name")
            or ""
        ).lower()

        value = meta.get("content")

        if value and key in (
            "og:image",
            "twitter:image"
        ):
            return urljoin(base_url, value)

    return ""


def extract_year(text):
    years = re.findall(r"\b20[0-9]{2}\b", text)
    return int(years[0]) if years else None


def extract_denomination(text):
    text = text.lower().replace("€", " euro")

    if re.search(r"\b2(?:[.,]00)?\s*euro\b", text):
        return 2

    if re.search(r"\b1(?:[.,]00)?\s*euro\b", text):
        return 1

    for value, term in (
        (50, "50 cent"),
        (20, "20 cent"),
        (10, "10 cent"),
        (5, "5 cent"),
        (2, "2 cent"),
        (1, "1 cent"),
    ):
        if term in text:
            return value

    return None


def parse_feed(feed_url, source):
    try:
        feed = feedparser.parse(feed_url)
    except Exception:
        return []

    result = []

    for entry in feed.entries[:100]:
        title = clean(entry.get("title", ""))
        link = entry.get("link", "")

        summary = clean(
            entry.get("summary", "")
            or entry.get("description", "")
        )

        if not title or not link:
            continue

        relevant, kind = is_relevant(title, summary)

        if not relevant:
            continue

        dt = None

        parsed = (
            entry.get("published_parsed")
            or entry.get("updated_parsed")
        )

        if parsed:
            dt = datetime(
                *parsed[:6],
                tzinfo=timezone.utc
            )

        if not dt:
            dt = parse_date(
                entry.get("published")
                or entry.get("updated")
            )

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
            "image": ""
        })

    return result


def scrape(url, source):
    try:
        response = get(url)
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    result = []
    seen = set()

    page_default_date = (
        page_date(soup)
        or datetime.now(timezone.utc)
    )

    for link_tag in soup.find_all("a", href=True):

        title = clean(
            link_tag.get_text(" ", strip=True)
        )

        link = urljoin(
            response.url,
            link_tag["href"]
        )

        if len(title) < 10:
            continue

        if link in seen:
            continue

        parent = (
            link_tag.parent.get_text(
                " ",
                strip=True
            )
            if link_tag.parent
            else ""
        )

        context = f"{title} {parent}"

        relevant, kind = is_relevant(
            title,
            context
        )

        if not relevant:
            continue

        seen.add(link)

        date = page_default_date
        image = ""

        # Intentamos obtener fecha e imagen de la noticia.
        try:
            page = get(link)
            detail = BeautifulSoup(
                page.text,
                "html.parser"
            )

            date = (
                page_date(detail)
                or date
            )

            image = extract_image(
                detail,
                page.url
            )

        except Exception:
            pass

        result.append({
            "title": f"[{source['country']}] {title}",
            "link": link,
            "summary": context[:1800],
            "date": date,
            "source": source["name"],
            "country": source["country"],
            "kind": kind,
            "image": image
        })

        if len(result) >= 25:
            break

    return result


def collect(source):
    section_url = source.get("section_url")

    if not section_url:
        return []

    # Primero intenta encontrar RSS.
    for feed_url in discover_feeds(section_url):

        items = parse_feed(
            feed_url,
            source
        )

        if items:
            return items

    # Si no hay RSS, hace scraping.
    return scrape(
        section_url,
        source
    )


def load_state():
    if not STATE_FILE.exists():
        return {"items": {}}

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {"items": {}}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def make_id(item):
    text = clean(
        f"{item['title']} {item['summary']}"
    ).lower()

    year = extract_year(text)
    denomination = extract_denomination(text)

    # Para las 2 € intentamos agrupar noticias
    # de distintas fuentes sobre la misma emisión.
    if denomination == 2 and year:
        words = re.sub(
            r"[^a-z0-9áéíóúüñ]+",
            " ",
            text
        ).split()

        ignored = {
            "euro",
            "euros",
            "coin",
            "coins",
            "commemorative",
            "moneda",
            "monedas",
            "moneta",
            "monete",
            "pièce",
            "pieces",
            str(year),
            "2"
        }

        subject = " ".join(
            word
            for word in words
            if len(word) > 3
            and word not in ignored
        )[:120]

        key = (
            f"{item['country']}|"
            f"2|"
            f"{year}|"
            f"{subject}"
        )

        return hashlib.sha256(
            key.encode()
        ).hexdigest()

    return hashlib.sha256(
        item["link"].encode()
    ).hexdigest()


def update_state(state, items):
    now = datetime.now(
        timezone.utc
    ).isoformat()

    for item in items:

        item_id = make_id(item)

        old = state["items"].get(
            item_id,
            {}
        )

        sources = old.get(
            "sources",
            []
        )

        if item["source"] not in sources:
            sources.append(
                item["source"]
            )

        text = clean(
            f"{item['title']} {item['summary']}"
        )

        state["items"][item_id] = {
            "id": item_id,
            "title": item["title"],
            "link": item["link"],
            "summary": item["summary"],
            "date": min(
                old.get(
                    "date",
                    item["date"].isoformat()
                ),
                item["date"].isoformat()
            ),
            "source": item["source"],
            "sources": sources,
            "country": item["country"],
            "kind": item["kind"],
            "year": extract_year(text),
            "denomination": extract_denomination(text),
            "image": (
                item.get("image")
                or old.get("image", "")
            ),
            "last_seen": now
        }


def description(item):
    if item["kind"] == "commemorative_2_euro":
        kind = "Moneda conmemorativa de 2 €"
    elif item["kind"] == "circulation":
        kind = "Moneda de circulación"
    else:
        kind = "Posible emisión de circulación"

    text = (
        f"País: {item['country']}\n"
        f"Denominación: "
        f"{item.get('denomination') or '—'}\n"
        f"Año: "
        f"{item.get('year') or '—'}\n"
        f"Tipo: {kind}\n\n"
        f"{item['summary']}\n\n"
        f"Fuentes: "
        f"{', '.join(item['sources'])}"
    )

    if item.get("image"):
        text += (
            f"\n\nImagen oficial: "
            f"{item['image']}"
        )

    return text


def main():
    state = load_state()

    found = []

    for source in CFG.get(
        "sources",
        []
    ):
        try:
            print(
                "Consultando:",
                source["name"]
            )

            found.extend(
                collect(source)
            )

        except Exception as error:
            print(
                "ERROR:",
                source["name"],
                error
            )

        time.sleep(0.2)

    update_state(
        state,
        found
    )

    save_state(state)

    items = list(
        state["items"].values()
    )

    items.sort(
        key=lambda x: x.get(
            "date",
            ""
        ),
        reverse=True
    )

    items = items[:100]

    now = format_datetime(
        datetime.now(timezone.utc),
        usegmt=True
    )

    xml_items = []

    for item in items:

        date = parse_date(
            item.get("date")
        ) or datetime.now(
            timezone.utc
        )

        xml_items.append(
            "<item>"
            f"<title>"
            f"{html.escape(item['title'])}"
            f"</title>"
            f"<link>"
            f"{html.escape(item['link'])}"
            f"</link>"
            f'<guid isPermaLink="false">'
            f"{item['id']}"
            f"</guid>"
            f"<pubDate>"
            f"{format_datetime(date, usegmt=True)}"
            f"</pubDate>"
            f"<description>"
            f"{html.escape(description(item))}"
            f"</description>"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0">'
        "<channel>"
        "<title>"
        "Euro Coins — emisiones oficiales"
        "</title>"
        "<link>"
        "https://github.com/"
        "</link>"
        "<description>"
        "Novedades oficiales sobre monedas de euro de circulación."
        "</description>"
        "<language>es</language>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        + "".join(xml_items)
        + "</channel>"
        "</rss>"
    )

    OUT.write_text(
        xml,
        encoding="utf-8"
    )

    print(
        f"Feed generado: {len(items)} elementos"
    )


if __name__ == "__main__":
    main()
