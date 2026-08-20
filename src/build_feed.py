import hashlib
import html
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
OUT.parent.mkdir(parents=True, exist_ok=True)

S = requests.Session()
S.headers["User-Agent"] = "EuroCoinsOfficialFeed/1.0"
INC = [x.lower() for x in CFG["include"]]
EXC = [x.lower() for x in CFG["exclude"]]

def get(url):
    r = S.get(url, timeout=25)
    r.raise_for_status()
    return r

def discover_feeds(url):
    try:
        r = get(url)
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    found = []
    for tag in soup.find_all("link", href=True):
        typ = (tag.get("type") or "").lower()
        rel = " ".join(tag.get("rel") or []).lower()
        if "rss" in typ or "atom" in typ or ("alternate" in rel and ("rss" in typ or "atom" in typ)):
            found.append(urljoin(r.url, tag["href"]))
    p = urlparse(r.url)
    base = f"{p.scheme}://{p.netloc}"
    found += [base + x for x in ("/rss.xml", "/feed", "/feed.xml", "/rss", "/atom.xml")]
    return list(dict.fromkeys(found))

def relevant(title, summary):
    text = f"{title} {summary}".lower()
    strong = any(k in text for k in (
        "new national side of euro coins intended for circulation",
        "new national side of euro coins",
        "commemorative 2-euro",
        "commemorative 2 euro",
        "2-euro commemorative",
        "moneda conmemorativa de 2 euros",
        "pièce commémorative de 2 euros",
        "moneta commemorativa da 2 euro",
        "2-euro-gedenkmünze",
        "2 euron juhlaraha",
        "2-euromunt",
    ))
    if any(k in text for k in EXC) and not strong:
        return False
    return strong or any(k in text for k in INC)

def parse_feed(feed_url, source):
    try:
        d = feedparser.parse(feed_url)
    except Exception:
        return []
    out = []
    for e in d.entries[:80]:
        title = html.unescape(e.get("title", "")).strip()
        link = e.get("link", "")
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        if not title or not link or not relevant(title, summary):
            continue
        p = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime(*p[:6], tzinfo=timezone.utc) if p else datetime.now(timezone.utc)
        out.append({"title": f"[{source['country']}] {title}",
                    "link": link, "summary": summary[:1200], "date": dt,
                    "source": source["name"]})
    return out

def scrape(url, source):
    try:
        r = get(url)
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        link = urljoin(r.url, a["href"])
        context = f"{title} {(a.parent.get_text(' ', strip=True) if a.parent else '')}"
        if len(title) < 12 or link in seen or not relevant(title, context):
            continue
        seen.add(link)
        out.append({"title": f"[{source['country']}] {title}",
                    "link": link, "summary": context[:1200],
                    "date": datetime.now(timezone.utc),
                    "source": source["name"]})
        if len(out) >= 25:
            break
    return out

def collect(source):
    for f in discover_feeds(source["section_url"]):
        items = parse_feed(f, source)
        if items:
            return items
    return scrape(source["section_url"], source)

def main():
    items = []
    for source in CFG["sources"]:
        try:
            items += collect(source)
        except Exception as e:
            print("ERROR", source["id"], e)
        time.sleep(.15)

    unique = {}
    for x in items:
        unique[hashlib.sha256(x["link"].encode()).hexdigest()] = x
    items = sorted(unique.values(), key=lambda x: x["date"], reverse=True)[:100]

    now = format_datetime(datetime.now(timezone.utc), usegmt=True)
    xml_items = []
    for x in items:
        guid = hashlib.sha256(x["link"].encode()).hexdigest()
        desc = html.escape(f"{x['summary']} — Fuente: {x['source']}", quote=False)
        xml_items.append(
            "<item>"
            f"<title>{html.escape(x['title'])}</title>"
            f"<link>{html.escape(x['link'])}</link>"
            f'<guid isPermaLink="false">{guid}</guid>'
            f"<pubDate>{format_datetime(x['date'], usegmt=True)}</pubDate>"
            f"<description>{desc}</description>"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        '<title>Euro Coins — emisiones oficiales</title>'
        '<link>https://github.com/</link>'
        '<description>Novedades oficiales sobre monedas de euro de circulación.</description>'
        '<language>es</language>'
        f"<lastBuildDate>{now}</lastBuildDate>"
        + "".join(xml_items)
        + "</channel></rss>"
    )
    OUT.write_text(xml, encoding="utf-8")
    print(f"Generated {OUT} with {len(items)} items")

if __name__ == "__main__":
    main()
