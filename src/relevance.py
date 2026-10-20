"""Filtro de relevancia: ¿habla este texto de monedas de euro?

Cuatro niveles (clave "coin_filter" de cada fuente en config/sources.yaml):

  strict (por defecto)  hace falta el euro (€, euro, cent...) + una moneda (coin, moneda, Münze...)
                        + algo concreto: conmemorativa, colección/set, cara nacional, plan de
                        emisión, un año reciente o un valor facial (2 €, two-euro, 10 euro...).
  news                  para fuentes que solo hablan de monedas (ceca, sección de monedas, BOE):
                        NO hace falta decir "euro", pero la moneda debe estar en el titular y
                        hace falta algo concreto (emitir, dedicada a, diseño, colección,
                        conmemorativa, plan...). El año suelto NO cuenta (todo el BOE lleva año).
  all                   solo para feeds de una tienda/ceca donde todo es una moneda: basta con
                        que el titular tenga una palabra de moneda.
  any                   para feeds que SOLO publican monedas (p. ej. el RSS del catálogo de monedas
                        conmemorativas de la Comisión Europea, cuyos titulares son "Bearded vulture"
                        o "35 years of the Erasmus programme - Finland"): entra todo.

Todo se compara con PALABRAS COMPLETAS: "euro" ya no encaja dentro de "Europe" ni
"circulation" dentro de "recirculation".

Autocomprobación:  python src/relevance.py
"""

import re
import unicodedata
from datetime import datetime, timezone

SUMMARY_CHARS = 900   # cuánto del resumen/texto se mira (el principio basta)
YEARS_BACK = 0        # 0 = año actual en adelante; 1 = también el año pasado
YEARS_AHEAD = 10      # para calendarios de emisión de años futuros
CURRENT_YEAR = None   # solo para pruebas; None = año del sistema

MODES = ("strict", "news", "all", "any")


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
_COIN_ALTS = [
    "coin", "coins", "coinage",
    "moneda", "monedas", "monedes", "moeda", "moedas", "moneta", "monete", "monetas",
    "monetos", "monetu", "monetoms", "monetomis", "monetai", "monetose", "monetoje",
    "monnaie", "monnaies",
    r"\w*munze\w*", r"\w*muenze\w*", r"\w*munt(?:en)?",
    r"mundi(?:d|t|le|st|na|ga|ks|des|de)?",          # estonio: mündi, mündid... (no "mundial")
    r"kolik\w*", r"juhlarah\w*",
    r"kovan\w*", r"minc\w*", "munita", "muniti",
    r"νομισμ\w*", r"κερμ\w*",
    "монета", "монети", "монетата", "монетите",
]
COIN_NOPIECE = _words(*_COIN_ALTS)                       # sin "piece(s)", ambiguo en inglés
COIN = _words(*_COIN_ALTS, "piece", "pieces")            # igual que antes: piece(s) = pièces
PIECE = _words("piece", "pieces")

# Nombres de instituciones que llevan la palabra "moneda" sin hablar de monedas
_INSTITUTIONS = re.compile(
    r"(?<!\w)(?:"
    r"fabrica nacional de moneda y timbre|real casa de la moneda|casa de la moneda|casa de moneda|"
    r"moneda y timbre|casa da moeda|"
    r"monnaie de paris|monnaie royale de belgique|monnaie royale|"
    r"munze osterreich|muenze oesterreich|muenze deutschland|munze deutschland|"
    r"lietuvos monetu kalykla|monetu kalykla|mincovna kremnica|"
    r"koninklijke nederlandse munt|royal dutch mint|royal belgian mint|royal mint"
    r")(?!\w)"
)

# --- 3) algo concreto -------------------------------------------------------
COMMEM = _words(
    r"commemorat\w*", r"conmemorat\w*", r"comemorat\w*", r"kommemorat\w*",
    r"gedenk\w*", r"herdenking\w*", r"juhla\w*",
    r"prigodn\w*", r"spominsk\w*", r"pamatn\w*", r"piemin\w*", r"progin\w*",
    r"meenemunt\w*", r"malestus\w*", r"juubel\w*", r"jubil\w*",
    r"αναμνηστικ\w*", r"възпоменателн\w*", r"юбилейн\w*",
)

COLLECTOR = _words(
    r"collector\w*", r"collection\w*", "proof", r"coincards?", r"numismat\w*",
    r"sammler\w*", r"kursmunzensatz\w*", r"coleccion\w*", r"colecion\w*",
    r"coleca\w*", r"coleco\w*",
    r"collezion\w*", r"verzamel\w*", r"keraily\w*",
    r"kolekcin\w*", r"kolekcij\w*", r"kolekcionar\w*",          # LT, LV
    r"zberatel\w*", r"zbirateljsk\w*", r"zbirk\w*",               # SK, SI/HR
    r"kollezzjon\w*", r"kollektsio\w*", r"kogumis\w*",            # MT, EE
    r"συλλεκτικ\w*", r"колекционн\w*",                            # GR, BG
    "set", "sets", "fdc", r"brilliant\s+uncirculated",
)

NATIONAL_SIDE = re.compile(
    r"(?<!\w)(?:national\s+sides?|nationale\s+seiten?|cara\s+nacional|caras\s+nacionales|"
    r"faces?\s+nationales?|facci?a\s+nazional\w*|facce\s+nazional\w*|"
    r"faces?\s+nacion\w*|"                                        # PT
    r"nationale\s+zijde\w*|kansallinen\s+puoli|kansalliset\s+puolet|"
    r"nacionalin\w*\s+pus\w*|nacionala\s+pus\w*|"                 # LT, LV
    r"narodn\w*\s+strana|nacionalna\s+stran\w*|"                  # SK, HR/SI
    r"rahvuslik\w*\s+kulg\w*|εθνικη\s+οψη)(?!\w)"                 # EE, GR
)

PLAN = _words(
    r"issuance\s+plan", r"issue\s+plan", r"issuing\s+plan",
    r"issuance\s+programme", r"issue\s+programme", r"coin\s+programme",
    "calendar", "calendario", "calendrier", "kalender",
    "programme", "programmes", "programa", "programma", "programm",
    "plan", "plans", "plano", "planes", "themes", "temas", "temi", "themen",
)

# Verbos y sustantivos de "se emite / se acuña" (solo cuentan en los niveles news y all)
ISSUE = _words(
    r"issu\w*", r"emisi\w*", r"emission\w*", r"emiss\w*", r"emit\w*", r"emet\w*",
    r"acunac\w*", r"acunar\w*", r"cunhag\w*", r"cunhad\w*", r"frappe\w*",
    r"coniaz\w*", r"coniat\w*",
    r"ausgab\w*", r"ausgeg\w*", r"ausgeb\w*", r"pragung", r"praegung",
    r"uitgif\w*", r"uitgegev\w*", r"uitgev\w*",
    r"liikkeeseen", r"skelbiam\w*", r"isleid\w*", r"leidzia\w*",
    r"izlaid\w*", r"izdod\w*", r"izdaj\w*", r"izdan\w*",
    r"vydav\w*", r"vydan\w*", r"emiteer\w*", r"valja\s+anda\w*", r"valjaand\w*",
    r"εκδο\w*", r"εκδιδ\w*", r"емитир\w*", r"емисия",
)

# "moneda dedicada a...", "coin marking the 100th anniversary of..."
DEDICATED = _words(
    r"dedicat\w*", r"dedicad\w*", r"dedie\w*", r"gewidmet", r"skirt\w*",
    r"puhendatud", r"veltit\w*", r"veltita\w*", r"venovan\w*", r"posvecen\w*",
    r"посветен\w*", r"αφιερωμεν\w*",
    r"anniversar\w*", r"aniversari\w*", r"anniversaire\w*", r"jahrestag\w*",
    r"jubilaeum", r"jubilaum", r"centenar\w*", r"bicentenar\w*",
)

# Noticias de diseño: "Eesti Pank is calling for designs for a coin..."
DESIGN_NEWS = re.compile(
    r"(?<!\w)(?:(?:calling|call)\s+for\s+designs?|design\s+(?:competition|contest)|"
    r"(?:competitions?|contest)\s+to\s+design|"
    r"(?:approved|selected|chosen|unveil\w*|reveal\w*|present\w*)\s+(?:the\s+)?designs?|"
    r"designs?\s+(?:approved|revealed|unveiled|announced|selected|chosen|presented))(?!\w)"
)

# "Numismatic issues commemorating Saint Francis": sin la palabra "coin", pero habla de emisiones
NUMISM = _words(r"numismat\w*")

# Concursos y votaciones de "moneda del año": hablan de monedas ya emitidas, no son un aviso nuevo
NOT_NEWS = _words(
    r"coins?\s+of\s+the\s+year", r"best\s+(?:collector\s+|commemorative\s+)?coins?",
    r"finalists?", r"finals",
)

# Solo para etiquetar (no decide si pasa o no)
CIRC = _words(
    "circulation", "circulating", "circulacion", "circulacao", "circulazione",
    "circolazione", "circulatie", r"umlauf\w*", r"optjecaj\w*", r"obeh\w*",
)

_UNITS = (r"(?:€|euros?|eur|eura|euroa|euron|euru|eiro|ewro|evro|evra|eurot|cents?|centimes|centimos|"
          r"centesimi|sentti\w*|euro(?:munt|munze|muenze|kolikko|moneda|moeda|moneta)\w*)")
_VALUES = r"(?:1|2|5|10|20|50|100|200|500)"
_WORDS_ANY = r"(?:one|two|five|ten|twenty|fifty|hundred|zwei|dos|deux|due|twee|dois|kaksi|kahe)"
_WORDS_TWO = r"(?:two|zwei|dos|deux|due|twee|dois|kaksi|kahe)"
FACE = re.compile(r"(?<![\d.,])" + _VALUES + r"\s*[-–]?\s*" + _UNITS + r"(?!\w)"
                  r"|€\s*" + _VALUES + r"(?!\d)"
                  r"|(?<!\w)" + _WORDS_ANY + r"\s*[-–]?\s*" + _UNITS + r"(?!\w)")
FACE2 = re.compile(r"(?<![\d.,])2\s*[-–]?\s*" + _UNITS + r"(?!\w)" r"|€\s*2(?!\d)"
                   r"|(?<!\w)" + _WORDS_TWO + r"\s*[-–]?\s*" + _UNITS + r"(?!\w)")
YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


def _strip_institutions(text):
    return _INSTITUTIONS.sub(" ", text)


def _has_euro(text):
    return "€" in text or bool(EURO.search(text))


def _has_recent_year(text):
    now = CURRENT_YEAR or datetime.now(timezone.utc).year
    return any(now - YEARS_BACK <= int(y) <= now + YEARS_AHEAD for y in YEAR.findall(text))


def _coin_in(text):
    """Palabra de moneda ya normalizada y sin nombres de institución.
    "piece(s)/pièces" es ambiguo (pieces of news, pièces jointes): solo cuenta con euro o conmemorativa."""
    return bool(COIN_NOPIECE.search(text)) or bool(
        PIECE.search(text) and (_has_euro(text) or COMMEM.search(text)))


def _numismatic_news(text):
    """"New Numismatic Issues Available": numismática solo cuenta si habla de emisiones o conmemoraciones
    (así "Numismatic Museum opens a new exhibition" no pasa)."""
    return bool(NUMISM.search(text) and (ISSUE.search(text) or COMMEM.search(text) or DEDICATED.search(text)))


def has_coin_word(text):
    """¿Aparece alguna palabra de moneda (aunque no se mencione el euro)?"""
    return bool(COIN.search(_strip_institutions(norm(text))))


def looks_coin_related(text):
    """Para el diagnóstico: ¿se parece a algo de monedas o del euro?"""
    t = _strip_institutions(norm(text))
    return bool(COIN.search(t)) or _has_euro(t)


def _kind(text, strict, year_ok=True):
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
    if (year_ok and _has_recent_year(text)) or (not strict and FACE.search(text)):
        return "circulation" if CIRC.search(text) else "euro_coin"
    return None


def _any_kind(text):
    """Etiqueta para el nivel "any": solo señales claras (no calendarios ni años: 'programme' no es un plan aquí)."""
    if COMMEM.search(text):
        return "commemorative_2_euro" if FACE2.search(text) else "commemorative"
    if NATIONAL_SIDE.search(text):
        return "national_side"
    if COLLECTOR.search(text):
        return "collector"
    return None


def _news_kind(text):
    """Señales para fuentes que solo hablan de monedas: sin año suelto, con verbos de emisión."""
    kind = _kind(text, False, year_ok=False)
    if kind:
        return kind
    if ISSUE.search(text) or DEDICATED.search(text) or DESIGN_NEWS.search(text):
        return "euro_coin"
    return None


def classify(title, summary="", strict=False, mode="strict"):
    """Devuelve el tipo de aviso o None si no interesa.

    mode="strict": hace falta que en título+resumen aparezcan el euro y una moneda, y además
    una señal concreta. El titular manda para elegir el tipo; el resumen solo se mira si el
    titular no basta. strict=True (texto sacado de páginas de detalle) no admite
    colección/plan/valor facial: exige conmemorativa, cara nacional o un año reciente.

    mode="news": la moneda debe estar en el titular; no hace falta decir "euro".
    mode="all": basta una palabra de moneda en el titular.
    mode="any": entra todo (feeds que solo publican monedas).
    """
    head = _strip_institutions(norm(title or ""))
    full = _strip_institutions(norm(f"{title or ''} {(summary or '')[:SUMMARY_CHARS]}"))

    if mode == "any":
        return _any_kind(head) or _any_kind(full) or "euro_coin"

    if mode in ("news", "all"):
        if not (_coin_in(head) or _numismatic_news(head)) or NOT_NEWS.search(head):
            return None
        if mode == "all":
            return _news_kind(head) or _news_kind(full) or "euro_coin"
        # "Sale of Cyprus euro coins with mint year 2026": con euro y moneda en el titular basta un año reciente
        return _news_kind(head) or _news_kind(full) or (_kind(head, False) if _has_euro(head) else None)

    if not (_has_euro(full) and COIN.search(full)):
        return None
    return _kind(head, strict) or _kind(full, strict)


def is_relevant(title, summary="", strict=False, mode="strict"):
    kind = classify(title, summary, strict, mode)
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
    # --- titulares reales del run del 17/10/2026 que el filtro antiguo perdía o aceptaba ---
    ("Skelbiamas 10 eurų kolekcinės monetos, skirtos Lietuvos didžiajam kunigaikščiui", "collector"),
    ("Skelbiamas 5 eurų kolekcinės monetos „Aitvaras“ (iš serijos „Lietuvių mitinės būtybės“) averso ir reverso", "collector"),
    ("Skelbiamas 2 eurų proginės monetos, skirtos Kaunui, nacionalinės pusės (averso) grafinio projekto sukūrimo", "commemorative_2_euro"),
    ("Eesti Pank is issuing a two-euro coin for Sipsik", "euro_coin"),
    ("The two-euro coin for Hiiumaa will show the Kõpu lighthouse", "euro_coin"),
    ("Eesti Pank approved the design for the two-euro commemorative coin dedicated to veterans of the Estonian Defence Forces", "commemorative_2_euro"),
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
    ("Nová pamätná minca 2 € Slovensko", "commemorative_2_euro"),
    ("Zberateľská minca 10 eur Mincovňa Kremnica", "collector"),
    ("Emissió de monedes commemoratives de 2 euros d'Andorra", "commemorative_2_euro"),       # catalán
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
    ("One euro per month for the whole family", None),
]


# (titular, resumen, tipo esperado, strict)
_CASES_SUMMARY = [
    # nota de la Casa de la Moneda belga por news.belgium.be: el titular es una metáfora, la moneda está en el texto
    ("100 ans de rail, immortalisé dans le métal",
     "La Monnaie royale de Belgique émet une pièce commémorative de 2 euros à l'occasion du centenaire de la SNCB.",
     "commemorative_2_euro", False),
    ("Honderd jaar spoor, vereeuwigd in metaal",
     "De Koninklijke Munt van België geeft een herdenkingsmunt van 2 euro uit ter gelegenheid van 100 jaar NMBS.",
     "commemorative_2_euro", False),
    ("Unieke verzamelobjecten te koop bij Fin Shop",
     "Ontwerpers en kunstenaars bieden unieke verzamelobjecten aan, onder meer een kristallen schaakspel.", None, False),
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


# (titular, resumen, tipo esperado, modo)
_CASES_MODE = [
    # --- news: titulares reales que no dicen "euro" ---
    ("Lietuvos monetų kalykla pristato naują kolekcinę monetą su M. K. Čiurlionio kūriniu „Pasaka“", "", "collector", "news"),
    ("Moeda dedicada a Amadeo de Souza-Cardoso apresentada no CAM – Centro de Arte Moderna Gulbenkian", "", "euro_coin", "news"),
    ("2026 Coin Set – Pontificate of Leo XIV", "", "collector", "news"),
    ("The Vatican Presents the 2025 Proof Versions of the €2 Commemorative Coins and the €5 Bimetallic Coin", "", "commemorative_2_euro", "news"),
    ("Coin Programme for 2026", "", "plan", "news"),
    ("Latvijas Banka is issuing a symbolic collector coin 'Chestnut'", "", "collector", "news"),
    ("Latvijas Banka is issuing a coin 'Bridal Crown' adorned with crystals", "", "euro_coin", "news"),
    ("Latvijas Banka issues a gold coin set 'Golden Seeds'", "", "collector", "news"),
    ("Characters from the animated film Flow will be reimagined on a coin issued by Latvijas Banka", "", "euro_coin", "news"),
    ("Eesti Pank is calling for designs for a coin for Hiiumaa", "", "euro_coin", "news"),
    ("Eesti Pank is issuing a two-euro coin for Sipsik", "", "euro_coin", "news"),
    ("Orden ECM/778/2026, de 27 de julio, por la que se acuerda la emisión, acuñación y puesta en circulación de monedas "
     "de colección «Campeones de la Copa Mundial de la FIFA 2026™».", "", "collector", "news"),
    ("Orden ECM/1351/2025, de 20 de noviembre, por la que se acuerda la emisión, acuñación y puesta en circulación de "
     "monedas de 2 euro conmemorativas del Monasterio de Poblet", "", "commemorative_2_euro", "news"),
    ("Frappe inaugurale de la nouvelle série de pièces luxembourgeoises en euros à l'effigie du Grand-Duc", "", "euro_coin", "news"),
    ("Frappe des pièces commémoratives 2026 : le Grand-Duc inaugure la série", "", "commemorative", "news"),
    # --- news: lo que NO debe pasar (sin moneda en el titular, logística de ventas, nombres de institución) ---
    ("Resolución de 5 de octubre de 2026, de la Fábrica Nacional de Moneda y Timbre-Real Casa de la Moneda, por la que se "
     "publica el Convenio para la emisión de certificados electrónicos", "", None, "news"),
    ("Resolución de 9 de octubre de 2026, de la Dirección General, por la que se convoca el Campeonato Mundial de ajedrez", "", None, "news"),
    ("On resuming the sale of the coin 'Bridal Crown'", "", None, "news"),
    ("The coin 'Flow' becomes Latvia's Coin of the Year 2025", "", None, "news"),
    ("Eesti Pank coins will be on sale temporarily from a different online store", "", None, "news"),
    ("Eesti Pank received 750 visitors yesterday", "", None, "news"),
    ("Additional Sipsik coin cards will go on sale on 3 August", "", None, "news"),
    ("Aukso karštinė leido Lietuvos monetų kalyklai vėl tapti pelningai, svarstoma gaminti daugiau juvelyrikos", "", None, "news"),
    ("Banche e moneta: serie nazionali - agosto 2026", "", None, "news"),
    ("Money, notes and coins", "", None, "news"),
    ("Banknotes & Coins", "", None, "news"),
    ("Coins and stamps", "", None, "news"),
    ("Candidaturas à 5.ª edição das Bolsas de Residência Literária Eça de Queiroz", "", None, "news"),
    ("A piece of news about the strategic plan", "", None, "news"),
    ("Exposition : une collection de pièces de théâtre à la bibliothèque", "", None, "news"),
    # --- titulares reales del 18/10/2026 (bank.lv/e-monetas.lv, vaticanstate.va, centralbank.cy, hnb.hr, fnmt.es) ---
    ("Four coins issued in 2025 have made it to the finals of the \"Coin of the Year\" competition", "", None, "news"),
    ("Voting for the best collector coin of 2025 is open", "", None, "news"),
    ("Opening hours of Latvijas Banka's Cashier's Offices during public holidays", "", None, "news"),
    ("Additional security measures for visitors to the Cashier's Office", "", None, "news"),
    ("New Numismatic Issues Available", "", "collector", "news"),
    ("Presentation of New Numismatic Issues", "", "collector", "news"),
    ("Numismatic issues commemorating Saint Francis", "", "commemorative", "news"),
    ("Numismatic Museum opens a new exhibition on Byzantine trade", "", None, "news"),
    ("Three States, One Tribute to Saint Francis: the Numismatic Triptych Dedicated to the Canticle of the Creatures is born",
     "", "collector", "news"),
    ("Three Numismatic Issues", "", "collector", "news"),
    ("Issue of set of Cyprus euro coins, mint year 2026", "", "collector", "news"),
    ("Sale of Cyprus euro coins with mint year 2025 and 2026", "", "euro_coin", "news"),
    ("Governor's opening speech at the \"Messages in Metal\" exhibition", "", None, "news"),
    ("Statistical Bulletin October 2026", "", None, "news"),
    ("CNB issues new commemorative 2-euro circulation coin", "", "commemorative_2_euro", "news"),
    ("Commemorative 2-euro coin “100 Years of the Croatian Radio and Television” in banks from 20 July", "",
     "commemorative_2_euro", "news"),
    ("Presentación en Washington D.C. de la colección de monedas conmemorativas del 250 aniversario de EEUU", "",
     "commemorative", "news"),
    ("Convocatoria de los Premios Tomás Francisco Prieto 2026", "", None, "news"),
    # --- any: feed del catálogo de monedas conmemorativas de la Comisión Europea ---
    ("35 years of the Erasmus programme - Finland", "", "euro_coin", "any"),
    ("Bearded vulture", "", "euro_coin", "any"),
    ("Marquisat des Baux", "", "euro_coin", "any"),
    ("100th anniversary of birth of Miki Muster", "", "euro_coin", "any"),
    # --- all: feed de una ceca, basta la palabra de moneda ---
    ("Златна монета „Св. Иван Рилски“", "", "euro_coin", "all"),
    ("Монета „150 години от Априлското въстание“", "", "euro_coin", "all"),
    ("Медал „Рожден ден“", "", None, "all"),
    ("Кюлчета от злато 999/1000 за раждане, кръщение и юбилей", "", None, "all"),
    # --- el nivel strict sigue igual ---
    ("Lietuvos monetų kalykla pristato naują kolekcinę monetą su M. K. Čiurlionio kūriniu „Pasaka“", "", None, "strict"),
    ("Moeda dedicada a Amadeo de Souza-Cardoso apresentada no CAM – Centro de Arte Moderna Gulbenkian", "", None, "strict"),
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
    for title, summary, expected, mode in _CASES_MODE:
        got = classify(title, summary, False, mode)
        if got != expected:
            failures += 1
            print(f"FALLA [{mode}]: esperado {expected!r}, obtenido {got!r}  <-  {title[:80]}")
    CURRENT_YEAR = None
    total = len(_CASES) + len(_CASES_SUMMARY) + len(_CASES_MODE)
    print(f"{total - failures}/{total} casos correctos")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
