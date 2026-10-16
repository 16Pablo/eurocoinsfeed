"""Filtro de relevancia: ¿habla este texto de monedas de euro?

Un texto interesa cuando
  1) menciona el euro (€, euro, cent...)             -> EURO
  2) menciona una moneda (coin, moneda, Münze...)    -> COIN
  3) y además hay algo concreto: conmemorativa, colección/set, cara nacional,
     plan de emisión, un año reciente o un valor facial (2 €, 10 euro...).

Todo se compara con PALABRAS COMPLETAS: "euro" ya no encaja dentro de "Europe"
ni "circulation" dentro de "recirculation".

Autocomprobación:  python src/relevance.py
"""

import re
import unicodedata
from datetime import datetime, timezone

SUMMARY_CHARS = 900   # cuánto del resumen/texto se mira (el principio basta)
YEARS_BACK = 0        # 0 = año actual en adelante; 1 = también el año pasado
YEARS_AHEAD = 10      # para calendarios de emisión de años futuros
CURRENT_YEAR = None   # solo para pruebas; None = año del sistema


def norm(text):
    """Minúsculas, sin acentos y con espacios normalizados."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _words(*alternatives):
    """Patrón de palabras COMPLETAS (alternativas escritas ya sin acentos)."""
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")


# --- 1) el euro -------------------------------------------------------------
EURO = _words(
    "euro", "euros", "eur", "eura", "euroa", "euron", "eurot", "euru", "eiro",
    "ewro", "evro", "evra", "evrov",
    "cent", "cents", "centimes", "centimos", "centesimi", "sentti", "senttia",
    "ευρω", "евро",
    r"eurov\w*", r"evrsk\w*",
    r"euro(?:munt|munze|muenze|kolikko|moneda|moeda|moneta|monete)\w*",
)

# --- 2) una moneda ----------------------------------------------------------
COIN = _words(
    "coin", "coins", "coinage",
    "moneda", "monedas", "moeda", "moedas", "moneta", "monete", "monetas",
    "monetos", "monetu",
    "piece", "pieces", "monnaie", "monnaies",
    r"\w*munze\w*", r"\w*muenze\w*", r"\w*munt(?:en)?", r"mundi\w*",
    r"kolik\w*", r"juhlarah\w*",
    r"kovan\w*", r"minc\w*", "munita", "muniti",
    r"νομισμ\w*", r"κερμ\w*",
    "монета", "монети", "монетата", "монетите",
)

# --- 3) algo concreto -------------------------------------------------------
COMMEM = _words(
    r"commemorat\w*", r"conmemorat\w*", r"comemorat\w*", r"kommemorat\w*",
    r"gedenk\w*", r"herdenking\w*", r"juhla\w*",
    r"prigodn\w*", r"spominsk\w*", r"pamatn\w*", r"piemin\w*", r"progin\w*",
    r"meenemunt\w*", r"malestus\w*",
    r"αναμνηστικ\w*", r"възпоменателн\w*", r"юбилейн\w*",
)

COLLECTOR = _words(
    r"collector\w*", r"collection\w*", "proof", r"coincards?", r"numismat\w*",
    r"sammler\w*", r"kursmunzensatz\w*", r"coleccion\w*", r"colecion\w*",
    r"collezion\w*", r"verzamel\w*", r"keraily\w*",
    "set", "sets", "fdc", r"brilliant\s+uncirculated",
)

NATIONAL_SIDE = re.compile(
    r"(?<!\w)(?:national\s+sides?|nationale\s+seiten?|cara\s+nacional|caras\s+nacionales|"
    r"faces?\s+nationales?|facci?a\s+nazional\w*|facce\s+nazional\w*|"
    r"nationale\s+zijde\w*|kansallinen\s+puoli|kansalliset\s+puolet)(?!\w)"
)

PLAN = _words(
    r"issuance\s+plan", r"issue\s+plan", r"issuing\s+plan",
    r"issuance\s+programme", r"issue\s+programme", r"coin\s+programme",
    "calendar", "calendario", "calendrier", "kalender",
    "programme", "programmes", "programa", "programma", "programm",
    "plan", "plans", "plano", "planes", "themes", "temas", "temi", "themen",
)

# Solo para etiquetar (no decide si pasa o no)
CIRC = _words(
    "circulation", "circulating", "circulacion", "circulacao", "circulazione",
    "circolazione", "circulatie", r"umlauf\w*", r"optjecaj\w*", r"obeh\w*",
)

_UNITS = (r"(?:€|euros?|eur|eura|euroa|euron|cents?|centimes|centimos|centesimi|"
          r"sentti\w*|euro(?:munt|munze|muenze|kolikko|moneda|moeda|moneta)\w*)")
_VALUES = r"(?:1|2|5|10|20|50|100|200|500)"
FACE = re.compile(r"(?<![\d.,])" + _VALUES + r"\s*[-–]?\s*" + _UNITS + r"(?!\w)"
                  r"|€\s*" + _VALUES + r"(?!\d)")
FACE2 = re.compile(r"(?<![\d.,])2\s*[-–]?\s*" + _UNITS + r"(?!\w)" r"|€\s*2(?!\d)")
YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


def _has_euro(text):
    return "€" in text or bool(EURO.search(text))


def _has_recent_year(text):
    now = CURRENT_YEAR or datetime.now(timezone.utc).year
    return any(now - YEARS_BACK <= int(y) <= now + YEARS_AHEAD for y in YEAR.findall(text))


def has_coin_word(text):
    """¿Aparece alguna palabra de moneda (aunque no se mencione el euro)?"""
    return bool(COIN.search(norm(text)))


def looks_coin_related(text):
    """Para el diagnóstico: ¿se parece a algo de monedas o del euro?"""
    t = norm(text)
    return bool(COIN.search(t)) or _has_euro(t)


def _kind(text, strict):
    """Tipo de aviso según las señales concretas que haya en `text` (ya normalizado)."""
    if COMMEM.search(text):
        return "commemorative_2_euro" if FACE2.search(text) else "commemorative"
    if NATIONAL_SIDE.search(text):
        return "national_side"
    if not strict:
        if COLLECTOR.search(text):
            return "collector"
        if PLAN.search(text):
            return "plan"
    if _has_recent_year(text) or (not strict and FACE.search(text)):
        return "circulation" if CIRC.search(text) else "euro_coin"
    return None


def classify(title, summary="", strict=False):
    """Devuelve el tipo de aviso o None si no interesa.

    Hace falta que en título+resumen aparezcan el euro y una moneda, y además una
    señal concreta. El titular manda para elegir el tipo; el resumen solo se mira
    si el titular no basta.

    strict=True (texto sacado de páginas de detalle) no admite colección/plan/valor
    facial: exige conmemorativa, cara nacional o un año reciente.
    """
    head = norm(title or "")
    full = norm(f"{title or ''} {(summary or '')[:SUMMARY_CHARS]}")
    if not (_has_euro(full) and COIN.search(full)):
        return None
    return _kind(head, strict) or _kind(full, strict)


def is_relevant(title, summary="", strict=False):
    kind = classify(title, summary, strict)
    return kind is not None, kind


# ---------------------------------------------------------------------------
# Autocomprobación: python src/relevance.py
# ---------------------------------------------------------------------------
_CASES = [
    # (texto, tipo esperado o None)
    # --- titulares reales ---
    ("Central Bank launches €2 commemorative coin to mark Irish Presidency of the Council of the European Union", "commemorative_2_euro"),
    ("2-Euro-Gedenkmünze „Konrad Adenauer“", "commemorative_2_euro"),
    ("CNB issues new commemorative 2-euro circulation coin", "commemorative_2_euro"),
    ("June 15, 2026 Purchase limits for the 2-euro commemorative coin in proof quality", "commemorative_2_euro"),
    ("Vatican Euro Coin Set Unveils New Color and Design", "collector"),
    ("Set of 2026 euro coins", "collector"),
    ("Euro coin issuance plan", "plan"),
    ("New national side of euro coins intended for circulation", "national_side"),
    ("Germany 2027 €2 Commemorative Coin 'Hans-Dietrich Genscher': Design Revealed", "commemorative_2_euro"),
    ("Lithuania: Second 2-Euro Commemorative Coin of 2026 \"Lithuanian Cities – Kaunas\" Issued", "commemorative_2_euro"),
    ("Collector euro coins", "collector"),
    # --- variantes de redacción en otros idiomas ---
    ("€2 commemorative coin", "commemorative_2_euro"),
    ("2€ commemorative coin", "commemorative_2_euro"),
    ("Commemorative €2 coin", "commemorative_2_euro"),
    ("Moneda de 2 euros conmemorativa", "commemorative_2_euro"),
    ("Monedas conmemorativas de 2 euros", "commemorative_2_euro"),
    ("Moeda comemorativa de 2 euros", "commemorative_2_euro"),
    ("2 Euro Gedenkmünze", "commemorative_2_euro"),
    ("Moneta da 2 euro commemorativa", "commemorative_2_euro"),
    ("Pièce de 2 euros commémorative", "commemorative_2_euro"),
    ("Herdenkingsmunt van 2 euro", "commemorative_2_euro"),
    ("2 euron juhlaraha", "commemorative_2_euro"),
    ("Nuevas monedas de euro para 2027", "euro_coin"),
    ("Nuevas monedas de euro puestas en circulación en 2027", "circulation"),
    ("10 euro silver coin Finland", "euro_coin"),
    # --- NO deben pasar ---
    ("« Les jeunes face à la désinformation » : Lancement du plan national monégasque dans le cadre de la campagne « Les journalistes comptent », du Conseil de l’Europe "
     "les jeunes figurent parmi les publics les plus directement exposés à la circulation massive de contenus trompeurs", None),
    ("Payments & financial stability digital euro payments authentication devices recirculation of banknotes", None),
    ("Euro, les pièces en circulation", None),
    ("Euro coin denominations", None),
    ("Types of euro coins", None),
    ("Common sides of euro coins", None),
    ("Legislation on the euro coins", None),
    ("Euro coins FAQ", None),
    ("Euro coins", None),
    ("The Eurosystem cash strategy the role of cash issuance and circulation", None),
    ("Monaco Yacht Show 2026 : montage des installations circulations piétonnière et automobile", None),
    ("Digital euro: ECB publishes progress report", None),
    ("Euro banknotes: new series 2026", None),
    ("Euro area inflation 2026", None),
    ("Cash management", None),
]


# (titular, resumen, tipo esperado, strict)
_CASES_SUMMARY = [
    # el titular decide el tipo aunque el resumen hable de "cara nacional"
    ("joint commemorative coin",
     "Commemorative coins are identical to the general circulation €2 coin. They have a common "
     "European side, and a national side with a motif marking an event.", "commemorative", False),
    # feed oficial: el euro y la moneda están en el resumen, no en el titular
    ("Nuevo diseño para Irlanda", "La moneda de 2 euros conmemorativa de 2026 ya está en producción.",
     "commemorative_2_euro", False),
    # no basta con que el resumen hable de euro y monedas sin nada concreto
    ("Informe anual", "Las monedas de euro y los billetes en general.", None, False),
    # el euro en el titular pero sin moneda en ningún sitio
    ("Digital euro 2026", "Progress report on the digital euro and banknotes.", None, False),
    # página de detalle (strict): solo conmemorativa, cara nacional o año reciente
    ("Coin dedicated to Kaunas", "Coin dedicated to Kaunas Release date: 01/10/2026 Denomination €2", "euro_coin", True),
    ("Euro coin denominations", "Euro coins have eight denominations: 1 cent, 2 cent, 1 euro and 2 euro.", None, True),
    ("Euro, les pièces en circulation", "Les pièces euro en circulation: 2 euros, 1 euro.", None, True),
    ("Collector euro coins", "Collector euro coins are legal tender only in the issuing country.", None, True),
]


def _selftest():
    global CURRENT_YEAR
    CURRENT_YEAR = 2026
    failures = 0
    for text, expected in _CASES:
        got = classify(text)
        if got != expected:
            failures += 1
            print(f"FALLA: esperado {expected!r}, obtenido {got!r}  <-  {text[:90]}")
    for title, summary, expected, strict in _CASES_SUMMARY:
        got = classify(title, summary, strict)
        if got != expected:
            failures += 1
            print(f"FALLA: esperado {expected!r}, obtenido {got!r}  <-  {title[:60]} | {summary[:40]}")
    CURRENT_YEAR = None
    total = len(_CASES) + len(_CASES_SUMMARY)
    print(f"{total - failures}/{total} casos correctos")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
