#!/usr/bin/env python3
"""
agenda_local : agrège les événements autour de chez toi et produit
  - sortie/agenda.ics  (calendrier auquel s'abonner)
  - sortie/index.html  (page "ce week-end", lisible sur téléphone)

Sources (v1) :
  1. OpenAgenda (jeu de données public Opendatasoft, sans clé, filtre par rayon)
  2. Flux iCal (.ics) listés dans config.json (mairies, offices de tourisme, salles...)

Usage :
  python3 agenda_local.py             # génère les fichiers
  python3 agenda_local.py --probe     # diagnostic : vérifie l'accès à OpenAgenda
Python 3.9+, aucune dépendance externe.
"""
import argparse
import difflib
import functools
import hashlib
import html
import http.cookiejar as http_cookiejar
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
UA = "agenda-local/0.1 (usage personnel)"
ODS_DATASET = "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets/evenements-publics-openagenda"
DATATOURISME_API = "https://api.datatourisme.fr/v1"


def datatourisme_key():
    """DATATOURISME_KEY : variable d'environnement, sinon fichier .env (jamais loggée)."""
    key = os.environ.get("DATATOURISME_KEY")
    if key:
        return key.strip()
    env_path = Path(__file__).with_name(".env")
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "DATATOURISME_KEY":
                return v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return None


# ---------------------------------------------------------------- modèle
@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    place: str = ""
    url: str = ""
    lat: float | None = None
    lon: float | None = None
    sources: list = field(default_factory=list)
    distance: float | None = None
    long_running: bool = False
    all_day: bool = False
    # attribution obligatoire (Licence Ouverte Etalab) : liste de (source, producteur, date de mise à jour "jj/mm/aaaa")
    attributions: list = field(default_factory=list)
    category: str = ""  # catégorie grossière pour les filtres (voir CATEGORIES)
    description: str = ""  # court descriptif, affiché en détail repliable sur la carte
    image: str = ""


# catégories grossières utilisées pour les filtres de la page HTML
CATEGORIES = [
    "Culture & spectacles",
    "Patrimoine & visites",
    "Marchés & fêtes",
    "Sport",
    "Nature & randonnées",
    "Enfants & familles",
    "Autre",
]

# mots-clés sport non ambigus (dont les activités "sur appareils"/en salle et les
# courses type trail, qui ne portent pas forcément le mot "sport")
_SPORT_KEYWORDS = (
    "sport", "rugby", "petanque", "tournoi", "trail", "pilates", "yoga", "fitness",
    "zumba", "course a pied", "randonnee sportive", "vtt", "cyclo", "match",
)
# sorties nature (balades commentées, brame du cerf...) : à distinguer du sport et
# du "Autre" fourre-tout — mais on évite le mot "nature" seul (trop ambigu, ex.
# "nature morte" en arts plastiques).
_NATURE_KEYWORDS = (
    "brame", "randonnee", "rando accompagnee", "rando decouverte", "balade nature",
    "balade decouverte", "sortie nature", "faune", "flore", "ornitholog", "champignon",
    "cueillette", "sentier", "eco pature", "observation des etoiles", "astronomie",
)


def openagenda_category(title, keywords, origin_title):
    text = norm(f"{title} {' '.join(keywords or [])} {origin_title or ''}")
    if any(k in text for k in ("emploi", "travail", "formation", "recrutement", "job dating")):
        return "Emploi & formation"  # exclu en amont dans fetch_openagenda, jamais affiché
    if any(
        k in text
        for k in (
            "patrimoine", "chateau", "grotte", "musee", "abbaye", "eglise", "jardin", "visite",
            "prehistorique", "prehistoire", "archeolog", "fouilles", "troglodyte", "abri sous roche",
            "gisement",
        )
    ):
        return "Patrimoine & visites"
    if any(
        k in text
        for k in (
            "concert", "festival", "spectacle", "theatre", "danse", "choregraphi", "musique", "cinema",
            "expo", "conference", "vernissage", "dedicace", "rencontre", "lecture", "lecture publique",
            "artiste",
        )
    ):
        return "Culture & spectacles"
    if any(k in text for k in ("marche", "fete", "vide grenier", "brocante", "foire")):
        return "Marchés & fêtes"
    if any(k in text for k in _SPORT_KEYWORDS):
        return "Sport"
    if any(k in text for k in _NATURE_KEYWORDS):
        return "Nature & randonnées"
    if any(k in text for k in ("enfant", "jeune public", "famille")):
        return "Enfants & familles"
    return "Autre"


# Ordre de priorité : les types précis et parlants d'abord (Concert, Exhibition...),
# les types très génériques (SportsEvent, CulturalEvent, SocialEvent) en dernier —
# beaucoup d'objets DATAtourisme portent plusieurs types à la fois (ex : une
# randonnée commentée porte à la fois "SportsEvent" et "CulturalEvent").
_DT_CATEGORY_ORDER = [
    ("Concert", "Culture & spectacles"),
    ("MusicEvent", "Culture & spectacles"),
    ("TheaterEvent", "Culture & spectacles"),
    ("ShowEvent", "Culture & spectacles"),
    ("Opera", "Culture & spectacles"),
    ("VisualArtsEvent", "Culture & spectacles"),
    ("ScreeningEvent", "Culture & spectacles"),
    ("Exhibition", "Patrimoine & visites"),
    ("ExhibitionEvent", "Patrimoine & visites"),
    ("GarageSale", "Marchés & fêtes"),
    ("SaleEvent", "Marchés & fêtes"),
    ("BricABrac", "Marchés & fêtes"),
    ("Market", "Marchés & fêtes"),
    ("FairOrShow", "Marchés & fêtes"),
    ("TraditionalCelebration", "Marchés & fêtes"),
    ("ChildrensEvent", "Enfants & familles"),
]


def datatourisme_category(types, title=""):
    """DATAtourisme étiquette large et cumule plusieurs types sur un même objet
    (ex : un vide-grenier avec concert porte à la fois GarageSale et Concert ; les
    Journées du patrimoine portent le type générique « SportsEvent »). On tranche
    d'abord sur un indice de titre sans ambiguïté, puis sur le type le plus
    spécifique, et seulement en dernier recours sur les types génériques (Sport)."""
    text = norm(title)
    if re.search(r"\bvides?\s+greniers?\b", text) or any(k in text for k in ("brocante", "foire aux")):
        return "Marchés & fêtes"
    types = set(types or [])
    for key, cat in _DT_CATEGORY_ORDER:
        if key in types:
            return cat
    if any(
        k in text
        for k in (
            "patrimoine", "chateau", "abbaye", "eglise", "musee", "grotte", "visite",
            "prehistorique", "prehistoire", "archeolog", "fouilles", "troglodyte", "abri sous roche",
            "gisement",
        )
    ):
        return "Patrimoine & visites"
    if any(k in text for k in ("conference", "vernissage", "dedicace", "rencontre", "lecture", "artiste")):
        return "Culture & spectacles"
    if "SportsCompetition" in types:
        return "Sport"
    if any(k in text for k in _SPORT_KEYWORDS) or any(
        k in text for k in ("course", "competition")
    ):
        return "Sport"
    # SportsEvent/Rambling sont des types génériques que DATAtourisme colle aussi à
    # tout un tas de choses sans rapport (un stage de dessin, une conférence...) —
    # on ne les range en "Nature & randonnées" que si le titre le confirme
    # explicitement, sinon ils tombent dans "Autre" comme n'importe quel autre
    # évènement sans indice de catégorie clair.
    if any(k in text for k in _NATURE_KEYWORDS):
        return "Nature & randonnées"
    return "Autre"


MAIRIE_CATEGORY_MAP = {
    "concert": "Culture & spectacles",
    "concert-chant": "Culture & spectacles",
    "jazz": "Culture & spectacles",
    "spectacle": "Culture & spectacles",
    "livre": "Culture & spectacles",
    "festival-soirs-des-toiles": "Culture & spectacles",
    "fete": "Marchés & fêtes",
    "vide-grenier": "Marchés & fêtes",
    "sport": "Sport",
    "rugby": "Sport",
    "enfants": "Enfants & familles",
}


# ---------------------------------------------------------------- réseau
# un CookieJar partagé : certains sites (ex. médiathèque de Brive) redirigent en
# boucle tant qu'aucune requête n'accepte leur cookie de session.
_HTTP_OPENER = request.build_opener(request.HTTPCookieProcessor(http_cookiejar.CookieJar()))


def http_get(url, params=None, timeout=30):
    if params:
        url = url + "?" + parse.urlencode(params)
    req = request.Request(url, headers={"User-Agent": UA})
    with _HTTP_OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_get_json(url, params=None):
    return json.loads(http_get(url, params))


def _url_ascii(url):
    """Encode en pourcents les caractères non-ASCII du chemin/de la requête (certains
    sites, ex. contesduleberou.com, ont des URL avec des accents non encodés dans
    leurs liens <a href>, ce que urllib refuse d'envoyer tel quel)."""
    parts = parse.urlsplit(url)
    path = parse.quote(parts.path, safe="/%")
    query = parse.quote(parts.query, safe="=&%")
    return parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


# ---------------------------------------------------------------- utilitaires
def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def to_aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def parse_iso(s):
    if not s:
        return None
    try:
        return to_aware(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


# ---------------------------------------------------------------- source 1 : OpenAgenda (Opendatasoft)
def ods_geo_field():
    """Trouve le nom du champ géographique du jeu de données."""
    try:
        meta = http_get_json(ODS_DATASET)
        for f in meta.get("fields", []):
            if f.get("type") in ("geo_point_2d", "geo_point"):
                return f["name"]
    except Exception:
        pass
    return "location_coordinates"


def _coords(rec, geo):
    g = rec.get(geo)
    if isinstance(g, dict):
        return g.get("lat"), g.get("lon")
    if isinstance(g, (list, tuple)) and len(g) == 2:
        return g[0], g[1]
    return None, None


def _occurrences(rec, w_start, w_end):
    """Liste des (début, fin) de l'événement qui tombent dans la fenêtre."""
    timings = rec.get("timings")
    if isinstance(timings, str):
        try:
            timings = json.loads(timings)
        except ValueError:
            timings = None
    occ = []
    if isinstance(timings, list):
        for t in timings:
            b, e = parse_iso(t.get("begin")), parse_iso(t.get("end"))
            if b and e and e >= w_start and b <= w_end:
                occ.append((b, e))
    if not occ:  # repli : plage globale
        b, e = parse_iso(rec.get("firstdate_begin")), parse_iso(rec.get("lastdate_end"))
        if b and e and e >= w_start and b <= w_end:
            occ.append((b, e))
    return sorted(occ)


def fetch_openagenda(cfg, w_start, w_end):
    lat, lon = cfg["centre"]["lat"], cfg["centre"]["lon"]
    radius = cfg["rayon_km"]
    geo = ods_geo_field()
    geo_where = f"within_distance({geo}, geom'POINT({lon} {lat})', {radius}km)"
    date_where = (
        f"lastdate_end >= date'{w_start:%Y-%m-%d}' AND firstdate_begin <= date'{w_end:%Y-%m-%d}'"
    )
    records = []
    for where in (f"{geo_where} AND {date_where}", geo_where):
        try:
            offset = 0
            while offset < 2000:
                page = http_get_json(
                    ODS_DATASET + "/records",
                    {"where": where, "limit": 100, "offset": offset, "order_by": "firstdate_begin"},
                )
                batch = page.get("results", [])
                records += batch
                if len(batch) < 100:
                    break
                offset += 100
            break
        except error.HTTPError as e:
            print(f"  OpenAgenda : requête refusée ({e.code}), essai simplifié", file=sys.stderr)
            records = []
    events = []
    for rec in records:
        title = rec.get("title_fr") or rec.get("title") or ""
        if not title:
            continue
        elat, elon = _coords(rec, geo)
        place = ", ".join(x for x in (rec.get("location_name"), rec.get("location_city")) if x)
        url = rec.get("canonicalurl") or ""
        category = openagenda_category(title, rec.get("keywords_fr"), rec.get("originagenda_title"))
        if category == "Emploi & formation":  # hors sujet pour un agenda de sorties
            continue
        occ = _occurrences(rec, w_start, w_end)
        if len(occ) > 7:  # exposition / événement quotidien : une seule ligne
            occ = [(occ[0][0], occ[-1][1])]
            long_running = True
        else:
            long_running = False
        for b, e in occ:
            events.append(Event(title, b, e, place, url, elat, elon, ["OpenAgenda"], long_running=long_running, category=category))
    return events


# ---------------------------------------------------------------- source 1bis : DATAtourisme (API REST)
# La doc officielle (https://api.datatourisme.fr/v1/docs) décrit une API REST, pas
# GraphQL (le support GraphQL de DATAtourisme concerne un serveur à auto-héberger à
# partir du flux Turtle complet, hors périmètre ici). On utilise donc le REST documenté :
# authentification X-API-Key, filtre géographique geo_distance=lat,lon,rayon(km),
# dates dans la propriété takesPlaceAt (ontologie DATAtourisme), pagination via meta.next.
def _fmt_date_fr(iso):
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return ""


def _lang_str(v):
    """Une valeur DATAtourisme @fr/@en est en général une chaîne, mais devient une
    liste dès qu'un lieu a plusieurs libellés (ex. « Le Vigan-en-Quercy », « Le
    Vigan ») : on garde le premier dans ce cas."""
    return v[0] if isinstance(v, list) else (v or "")


def _datatourisme_object_events(obj, w_start, w_end):
    label = obj.get("label") or {}
    title = _lang_str(label.get("@fr")) or _lang_str(label.get("@en")) or next(iter(label.values()), "")
    if not title:
        return []
    loc = (obj.get("isLocatedAt") or [{}])[0]
    geo = loc.get("geo") or {}
    lat_e, lon_e = geo.get("latitude"), geo.get("longitude")
    addr = (loc.get("address") or [{}])[0]
    city = _lang_str(((addr.get("hasAddressCity") or {}).get("label") or {}).get("@fr", ""))
    street = (addr.get("streetAddress") or [""])[0]
    place = ", ".join(x for x in (street, city) if x)
    creator = obj.get("hasBeenCreatedBy") or {}
    producer = creator.get("legalName", "")
    updated = _fmt_date_fr(obj.get("lastUpdateDatatourisme") or obj.get("lastUpdate") or "")
    attribution = [("DATAtourisme", producer, updated)]
    category = datatourisme_category(obj.get("type"), title)

    # le homepage de l'office de tourisme producteur (creator.homepage) n'est PAS une
    # page de l'événement (juste son accueil générique), et la fiche technique
    # DATAtourisme (obj["uri"]) n'est jamais utile pour un particulier (vocabulaire
    # RDF brut) : aucun des deux n'est retenu. Seul un contact.homepage pointant vers
    # une page dédiée à l'évènement l'est ; sinon on laisse le champ vide, _card()
    # proposera une recherche Google à la place.
    contact = (obj.get("hasContact") or [{}])[0]
    url = (contact.get("homepage") or [""])[0]

    desc = (obj.get("hasDescription") or [{}])[0]
    description = (
        _lang_str((desc.get("shortDescription") or {}).get("@fr"))
        or _lang_str((desc.get("description") or {}).get("@fr"))
        or ""
    )
    image = ""
    for repr_ in obj.get("hasRepresentation") or []:
        locator = ((repr_.get("hasRelatedResource") or [{}])[0].get("locator") or [""])[0]
        if locator:
            image = locator
            break

    occ = []
    for period in obj.get("takesPlaceAt") or []:
        sd = period.get("startDate")
        if not sd:
            continue
        ed = period.get("endDate") or sd
        try:
            start = datetime.strptime(sd, "%Y-%m-%d")
            end_d = datetime.strptime(ed, "%Y-%m-%d")
        except ValueError:
            continue
        st, et = period.get("startTime"), period.get("endTime")
        all_day = st is None
        if st:
            h, m = (int(x) for x in st.split(":")[:2])
            start = start.replace(hour=h, minute=m)
        if et:
            h, m = (int(x) for x in et.split(":")[:2])
            end = end_d.replace(hour=h, minute=m)
        else:
            end = end_d.replace(hour=23, minute=59) if all_day else start + timedelta(hours=2)
        start, end = start.replace(tzinfo=TZ), end.replace(tzinfo=TZ)
        if end >= w_start and start <= w_end:
            occ.append((start, end))
    if not occ:
        return []
    occ.sort()
    if len(occ) > 7:  # exposition / animation quotidienne : une seule ligne
        occ = [(occ[0][0], occ[-1][1])]
        long_running = True
    else:
        long_running = False
    return [
        Event(
            title, b, e, place, url, lat_e, lon_e, ["DATAtourisme"],
            long_running=long_running, attributions=list(attribution), category=category,
            description=description, image=image,
        )
        for b, e in occ
    ]


def _slugify(text):
    text = re.sub(r"[\"'’«»]", "", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


# sites d'offices de tourisme identifiés (à la main, cf. conversation) dont le
# sitemap expose une page dédiée par évènement, sous une URL commençant par le
# slug du titre (ex : /agenda/{slug}/, ou /agenda/{slug}-{ville}-fr-{id}/) : on
# construit un index slug -> URL une fois par site et par exécution, bien plus
# fiable qu'une URL devinée à l'aveugle ou que la fiche technique DATAtourisme.
_DT_SITE_SITEMAPS = {
    "OT Lascaux Dordogne Vallée Vézère": ("https://www.lascaux-dordogne.com/agenda-sitemap.xml", "/agenda/"),
    "Office de Tourisme Sarlat Périgord Noir": (
        "https://www.sarlat-tourisme.com/sitemap.xml/",
        "/je-selectionne-mes-activites/agenda/",
    ),
    "Vézère Périgord Noir": (
        "https://www.vezere-perigord.fr/sitemap.xml",
        "/l-agenda-des-fetes-et-manifestations/",
    ),
}


def _fetch_sitemap_urls(url, depth=0):
    try:
        req = request.Request(url, headers={"User-Agent": UA})
        with request.urlopen(req, timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    locs = re.findall(r"<loc>([^<]+)</loc>", text)
    if depth == 0 and "<sitemapindex" in text:
        urls = []
        for sub in locs:
            urls += _fetch_sitemap_urls(sub, depth=1)
        return urls
    return locs


@functools.lru_cache(maxsize=None)
def _agenda_slug_index(producer):
    """slug (dernier segment de l'URL, avant un éventuel -ville-fr-id) -> URL
    complète, pour les évènements de ce producteur DATAtourisme."""
    cfg = _DT_SITE_SITEMAPS.get(producer)
    if not cfg:
        return {}
    root, marker = cfg
    index = {}
    for u in _fetch_sitemap_urls(root):
        if marker not in u:
            continue
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        if slug and slug not in index:
            index[slug] = u
    return index


def _resolve_event_page(producer, title):
    index = _agenda_slug_index(producer)
    if not index:
        return ""
    slug = _slugify(title)
    if slug in index:
        return index[slug]
    return next((u for k, u in index.items() if k.startswith(slug + "-")), "")


def fetch_datatourisme(cfg, w_start, w_end):
    key = datatourisme_key()
    if not key:
        raise RuntimeError("DATATOURISME_KEY absente (variable d'environnement ou .env)")
    lat0, lon0, radius = cfg["centre"]["lat"], cfg["centre"]["lon"], cfg["rayon_km"]
    fields = (
        "uuid,uri,label,type,takesPlaceAt,isLocatedAt,hasBeenCreatedBy,lastUpdateDatatourisme,"
        "hasContact,hasDescription,hasRepresentation"
    )
    url = DATATOURISME_API + "/entertainmentAndEvent?" + parse.urlencode(
        {"geo_distance": f"{lat0},{lon0},{radius}km", "page_size": 250, "fields": fields}
    )
    events = []
    for page in range(20):  # limite de sécurité : peu de requêtes
        req = request.Request(url, headers={"X-API-Key": key, "User-Agent": UA})
        with request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        for obj in data.get("objects", []):
            events += _datatourisme_object_events(obj, w_start, w_end)
        url = (data.get("meta") or {}).get("next")
        if not url:
            break
        time.sleep(0.3)
    for e in events:
        if not e.url and e.attributions:
            producer = e.attributions[0][1]
            e.url = _resolve_event_page(producer, e.title)
    return events


# ---------------------------------------------------------------- source 2 : flux iCal
def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _ics_unescape(s):
    return s.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_ics_dt(key, value):
    params = key.split(";")[1:]
    tzid = next((p.split("=", 1)[1] for p in params if p.upper().startswith("TZID=")), None)
    v = value.strip()
    if "VALUE=DATE" in key.upper() or len(v) == 8:
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(tzinfo=TZ), True
    fmt = "%Y%m%dT%H%M%S"
    if v.endswith("Z"):
        return datetime.strptime(v[:-1], fmt).replace(tzinfo=timezone.utc), False
    try:
        zone = ZoneInfo(tzid) if tzid else TZ
    except Exception:
        zone = TZ
    return datetime.strptime(v, fmt).replace(tzinfo=zone), False


def parse_ics(text, source_name, w_start, w_end):
    events, cur = [], None
    for line in _unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("END:VEVENT") and cur is not None:
            try:
                if "DTSTART" in cur:
                    start, allday = _parse_ics_dt(*cur["DTSTART"])
                    if "DTEND" in cur:
                        end, _ = _parse_ics_dt(*cur["DTEND"])
                    else:
                        end = start + (timedelta(days=1) if allday else timedelta(hours=2))
                    if end >= w_start and start <= w_end and "RRULE" not in cur:
                        lat = lon = None
                        if "GEO" in cur:
                            try:
                                lat, lon = (float(x) for x in cur["GEO"][1].split(";"))
                            except ValueError:
                                pass
                        events.append(
                            Event(
                                _ics_unescape(cur.get("SUMMARY", ("", ""))[1]),
                                start,
                                end,
                                _ics_unescape(cur.get("LOCATION", ("", ""))[1]),
                                cur.get("URL", ("", ""))[1].strip(),
                                lat,
                                lon,
                                [source_name],
                                all_day=allday,
                            )
                        )
            except ValueError:
                pass
            cur = None
        elif cur is not None and ":" in line:
            key, value = line.split(":", 1)
            cur[key.split(";")[0].upper()] = (key, value)
    return [e for e in events if e.title]


def fetch_feeds(cfg, w_start, w_end):
    events = []
    for feed in cfg.get("flux_ics", []):
        try:
            events += parse_ics(http_get(feed["url"]), feed["nom"], w_start, w_end)
        except Exception as e:  # une source cassée ne bloque pas les autres
            print(f"  Flux « {feed.get('nom')} » ignoré : {e}", file=sys.stderr)
    return events



# ---------------------------------------------------------------- source 3 : agenda web de la mairie de Montignac
MONTHS = {m: i + 1 for i, m in enumerate(
    ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"])}
_BLOCK = {"p", "div", "li", "ul", "ol", "br", "article", "section", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "header", "footer"}
_AGENDA_HREF = re.compile(r"https?://ville-montignac\.com/agenda/(?!categorie)([a-z0-9_%\-]+)", re.I)
_DATE_LINE = re.compile(r"^(?:le|du)\s+(?:[a-zéû]+\s+)?(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})(.*)$", re.I)
_END_DATE = re.compile(r"\bau\s+(?:[a-zéû]+\s+)?(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", re.I)
_TIME_LINE = re.compile(r"^(?:de\s+(\d{1,2})h(\d{2})?\s+à\s+(\d{1,2})h(\d{2})?|à\s+(\d{1,2})h(\d{2})?)\s*$", re.I)


class _TextLines(HTMLParser):
    """HTML -> lignes de texte (un saut de ligne à chaque bloc) + liens rencontrés."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.hrefs, self._skip = [""], [], 0

    def _nl(self):
        if self.lines[-1].strip():
            self.lines.append("")

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in _BLOCK:
            self._nl()
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append((len(self.lines) - 1, v))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag in _BLOCK:
            self._nl()

    def handle_data(self, data):
        if not self._skip:
            self.lines[-1] += data


def _mk_date(d, mois, y, h=0, m=0):
    mo = MONTHS.get(mois.lower())
    if not mo:
        raise ValueError(mois)
    return datetime(int(y), mo, int(d), h, m, tzinfo=TZ)


def parse_montignac_html(page, source_name):
    tp = _TextLines()
    tp.feed(page)
    lines = [re.sub(r"\s+", " ", l).strip() for l in tp.lines]
    idx = [i for i, l in enumerate(lines) if l]  # indices non vides
    events = []
    for pos, i in enumerate(idx):
        m = _DATE_LINE.match(lines[i])
        if not m or pos == 0:
            continue
        t = idx[pos - 1]  # le titre est la ligne juste avant la date
        title = lines[t]
        try:
            start = _mk_date(m.group(1), m.group(2), m.group(3))
        except ValueError:
            continue
        end, all_day = None, True
        em = _END_DATE.search(m.group(4))
        if em:
            try:
                end = _mk_date(em.group(1), em.group(2), em.group(3), 23, 59)
            except ValueError:
                pass
        nxt = idx[pos + 1 : pos + 4]
        k = 0
        if k < len(nxt):
            tm = _TIME_LINE.match(lines[nxt[k]])
            if tm:
                g = tm.groups()
                if g[0] is not None:
                    sh, sm, eh, em_ = int(g[0]), int(g[1] or 0), int(g[2]), int(g[3] or 0)
                    start = start.replace(hour=sh, minute=sm)
                    end = start.replace(hour=eh, minute=em_)
                else:
                    start = start.replace(hour=int(g[4]), minute=int(g[5] or 0))
                all_day = False
                k += 1
        place = ""
        if k < len(nxt) and not re.match(r"^\d{5}\b", lines[nxt[k]]):
            place = lines[nxt[k]]
            k += 1
        city = ""
        if k < len(nxt):
            pm = re.match(r"^\d{5}\s+(.+)$", lines[nxt[k]])
            city = pm.group(1) if pm else ""
        if end is None:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=2))
        url = ""
        for hi, href in tp.hrefs:
            if hi > t:
                break
            hm = _AGENDA_HREF.search(href)
            if hm:
                url = hm.group(0)
        events.append(Event(title, start, end, ", ".join(x for x in (place, city) if x), url, None, None, [source_name], all_day=all_day))
    return events


def fetch_web_pages(cfg, w_start, w_end):
    events = []
    for src in cfg.get("pages_web", []):
        for base in src["urls"]:
            for n in range(1, src.get("pages", 2) + 1):
                url = base if n == 1 else f"{base.rstrip('/')}/page/{n}"
                try:
                    got = parse_montignac_html(http_get(url), src["nom"])
                except Exception as e:
                    print(f"  Page « {url} » ignorée : {e}", file=sys.stderr)
                    break
                slug = base.rstrip("/").rsplit("/", 1)[-1]
                category = MAIRIE_CATEGORY_MAP.get(slug, "Autre")
                for e in got:
                    if e.lat is None and "lat" in src:
                        e.lat, e.lon = src["lat"], src["lon"]
                    e.category = category
                events += [e for e in got if e.end >= w_start and e.start <= w_end]
                if not got or min(e.start for e in got) < w_start:
                    break  # les pages sont triées du plus récent au plus ancien
    return events


# ---------------------------------------------------------------- source 3bis : festival Le Lébérou (contes)
# Site Jimdo, un contexte par conteur sous /festival-{année}/<slug>/ (le slug de l'année
# courante change chaque année et l'ancienne édition part sous /archives/festival-{année}/,
# donc on calcule l'URL de l'édition en cours à partir de l'année courante plutôt que de
# la figer dans config.json).
_LEBEROU_SUBPAGE_HREF = re.compile(r"^/festival-\d{4}/([^/]+)/?$")
_LEBEROU_DATE = re.compile(
    r"\b(?:Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche|Lun|Mar|Mer|Jeu|Ven|Sam|Dim)\.?"
    r"\s+(\d{1,2})(?:er)?\s+([a-zéûA-ZÉÛ]+)\b",
    re.I,
)
_LEBEROU_TIME = re.compile(r"^(\d{1,2})h(\d{2})?$")


def parse_leberou_subpage(page, year, source_name):
    """Cherche, sur la page d'un conteur, la ligne juste après le titre qui contient
    une date (« Samedi 31 octobre - Lieu - 21h00 », ou parfois la date en fin de
    ligne) ; renvoie None si la page ne présente pas ce format (ex. redirection,
    page « hors festival » atypique)."""
    tp = _TextLines()
    tp.feed(page)
    lines = [re.sub(r"\s+", " ", l).strip() for l in tp.lines]
    idx = [i for i, l in enumerate(lines) if l]
    for pos, i in enumerate(idx):
        line = lines[i]
        dm = _LEBEROU_DATE.search(line)
        if not dm or pos == 0:
            continue
        title = lines[idx[pos - 1]]
        if not title:
            continue
        try:
            start = _mk_date(dm.group(1), dm.group(2), year)
        except ValueError:
            continue
        segments = [s.strip() for s in line.split(" - ") if s.strip()]
        segments = [s for s in segments if not _LEBEROU_DATE.search(s)]
        all_day = True
        if segments and _LEBEROU_TIME.match(segments[-1]):
            tm = _LEBEROU_TIME.match(segments[-1])
            start = start.replace(hour=int(tm.group(1)), minute=int(tm.group(2) or 0))
            all_day = False
            segments = segments[:-1]
        place = " - ".join(segments)
        end = start + (timedelta(days=1) if all_day else timedelta(hours=2))
        return Event(title, start, end, place, "", None, None, [source_name],
                      all_day=all_day, category="Culture & spectacles")
    return None


def fetch_leberou(cfg, w_start, w_end):
    src = cfg.get("festival_leberou")
    if not src:
        return []
    year = datetime.now(TZ).year
    base = src["site"].rstrip("/")
    nom = src.get("nom", "Festival Le Lébérou")
    overview_url = f"{base}/festival-{year}/"
    try:
        overview = http_get(overview_url)
    except Exception as e:
        print(f"  Festival Le Lébérou ignoré : {e}", file=sys.stderr)
        return []
    tp = _TextLines()
    tp.feed(overview)
    sub_urls, seen = [], set()
    for _, href in tp.hrefs:
        m = _LEBEROU_SUBPAGE_HREF.match(parse.urlsplit(href).path)
        if not m or not m.group(1):
            continue
        url = parse.urljoin(base + "/", href)
        if url not in seen:
            seen.add(url)
            sub_urls.append(url)
    events = []
    for url in sub_urls:
        try:
            e = parse_leberou_subpage(http_get(_url_ascii(url)), year, nom)
        except Exception as ex:
            print(f"  Page « {url} » ignorée : {ex}", file=sys.stderr)
            continue
        if e and e.end >= w_start and e.start <= w_end:
            e.url = url
            events.append(e)
    return events


# ---------------------------------------------------------------- source 3ter : Pôle international de la Préhistoire
# Page « actualités / événements » : tous les événements sont listés sur une seule
# page (blocs <div class="magazine-item ...">), pas besoin de pagination ni de
# sous-pages. La date n'a parfois pas d'année (événements récurrents comme les
# Journées du patrimoine) -> on complète avec l'année en cours, ou la suivante si
# la date obtenue tombe dans le passé.
_POLE_BLOCK = re.compile(
    r'<div class="magazine-item(?:\s[^"]*)?">(.*?)(?=<div class="magazine-item(?:\s[^"]*)?">|\Z)', re.S
)
_POLE_LINK = re.compile(r'href="([^"]+/evenements/\d+-[^"]+)"[^>]*itemprop="url">\s*([^<]+)', re.S)
_POLE_DATE_P = re.compile(r'<div class="magazine-item-ct">\s*<p>(.*?)</p>', re.S)
_POLE_DATE = re.compile(
    r"(\d{1,2})(?:er)?\s+([a-zéûA-ZÉÛ]+)(?:\s+(\d{4}))?(?:.*?\bà\s+(\d{1,2})h(\d{2})?)?",
    re.S,
)


def parse_pole_prehistoire_html(page, base_url, source_name):
    events = []
    now = datetime.now(TZ)
    for block in _POLE_BLOCK.findall(page):
        lm = _POLE_LINK.search(block)
        dm_p = _POLE_DATE_P.search(block)
        if not lm or not dm_p:
            continue
        href, title = lm.group(1), html.unescape(re.sub(r"\s+", " ", lm.group(2))).strip()
        date_text = html.unescape(re.sub(r"<[^>]+>", " ", dm_p.group(1)))
        dm = _POLE_DATE.search(date_text)
        if not dm:
            continue
        day, mois, year, h, mi = dm.groups()
        try:
            start = _mk_date(day, mois, year or now.year)
        except ValueError:
            continue
        if not year and start < now - timedelta(days=1):
            start = start.replace(year=start.year + 1)
        all_day = h is None
        if not all_day:
            start = start.replace(hour=int(h), minute=int(mi or 0))
        end = start + (timedelta(days=1) if all_day else timedelta(hours=2))
        url = parse.urljoin(base_url, href)
        category = openagenda_category(title, None, None)
        events.append(Event(title, start, end, "", url, None, None, [source_name],
                             all_day=all_day, category=category))
    return events


def fetch_pole_prehistoire(cfg, w_start, w_end):
    src = cfg.get("pole_prehistoire")
    if not src:
        return []
    events = []
    try:
        got = parse_pole_prehistoire_html(http_get(src["url"]), src["url"], src.get("nom", "Pôle international de la Préhistoire"))
    except Exception as e:
        print(f"  Pôle international de la Préhistoire ignoré : {e}", file=sys.stderr)
        return []
    for e in got:
        if e.lat is None and "lat" in src:
            e.lat, e.lon = src["lat"], src["lon"]
        if e.end >= w_start and e.start <= w_end:
            events.append(e)
    return events


# ---------------------------------------------------------------- source 3quater : Brive Tourisme (agenda)
# Widget de listing paginé (?id1[currentPage]=N, rendu côté serveur) : un
# <div class="list-item"> par OCCURRENCE (les événements récurrents/multi-dates
# sont déjà éclatés en une ligne par date par le site lui-même, pas besoin de
# suivre chaque fiche détail) — titre, commune, date unique ("Le ...") ou plage
# ("Du ... au ..."). Couvre toute la Corrèze autour de Brive (Tulle, Turenne,
# Saint-Geniez-ô-Merle...), pas seulement Brive-ville, mais la page liste la commune
# en texte seul (pas de lat/lon par item) : on approxime donc TOUTES les occurrences
# avec les coordonnées de Brive-la-Gaillarde (cfg["brive_tourisme"]["lat"/"lon"]),
# comme fetch_pole_prehistoire le fait pour sa propre source. Imprécis pour les
# communes éloignées de Brive (ex. Tulle), mais nécessaire pour que le filtre de
# rayon (apply_distance, et le menu de palier en JS) ne les traite pas comme
# "toujours à Montignac" — sans coordonnées du tout, ils remonteraient à tort dans
# le filtre le plus étroit ("Montignac 15 km"). Trié par date croissante -> on
# arrête la pagination dès qu'une page ne ramène plus aucune date dans la fenêtre
# [w_start, w_end], avec un plafond de pages en garde-fou si jamais le tri venait à
# changer.
_BT_ITEM = re.compile(r'<div class="list-item">.*?</div></div></div>', re.S)
_BT_TITLE = re.compile(r'<h3><a href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a></h3>')
_BT_PLACE = re.compile(r'class="place[^"]*">.*?list-icon"></i>\s*([^<]+?)\s*</span>', re.S)
_BT_DATE_RANGE = re.compile(
    r"Du\s+(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})\s+au\s+(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", re.I
)
_BT_DATE_SINGLE = re.compile(r"Le\s+(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", re.I)
_BT_MAX_PAGES = 60


def parse_brivetourisme_html(page, base_url):
    events = []
    for block in _BT_ITEM.findall(page):
        tm = _BT_TITLE.search(block)
        if not tm:
            continue
        href, title = tm.group(1), html.unescape(tm.group(2))
        pm = _BT_PLACE.search(block)
        place = html.unescape(pm.group(1)).strip().title() if pm else ""
        rm = _BT_DATE_RANGE.search(block)
        try:
            if rm:
                d1, mo1, y1, d2, mo2, y2 = rm.groups()
                start = _mk_date(d1, mo1, y1)
                end = _mk_date(d2, mo2, y2).replace(hour=23, minute=59)
            else:
                sm = _BT_DATE_SINGLE.search(block)
                if not sm:
                    continue
                start = _mk_date(*sm.groups())
                end = start + timedelta(days=1)
        except ValueError:
            continue
        url = parse.urljoin(base_url, href)
        category = openagenda_category(title, None, None)
        events.append(Event(title, start, end, place, url, None, None, ["Brive Tourisme"],
                             all_day=True, category=category))
    return events


def fetch_brivetourisme(cfg, w_start, w_end):
    src = cfg.get("brive_tourisme")
    if not src:
        return []
    base_url = src["url"]
    events = []
    for page_num in range(1, _BT_MAX_PAGES + 1):
        url = base_url if page_num == 1 else base_url + "?" + parse.urlencode({"id1[currentPage]": page_num})
        try:
            got = parse_brivetourisme_html(http_get(url), base_url)
        except Exception as e:
            print(f"  Brive Tourisme (page {page_num}) ignorée : {e}", file=sys.stderr)
            break
        if not got:
            break
        in_window = [e for e in got if e.end >= w_start and e.start <= w_end]
        for e in in_window:
            if "lat" in src:
                e.lat, e.lon = src["lat"], src["lon"]
        events += in_window
        if not in_window and all(e.start > w_end for e in got):
            break
    return events


# ---------------------------------------------------------------- source 3quinquies : Ville de Périgueux (agenda)
# Flux RSS dédié (perigueux.fr/agenda/flux-agenda/rss.xml) : un <item> par
# événement, avec <ev:startdate>/<ev:enddate> (format RFC822, "Tue, 11 May 2027
# 20:00:00 +0200"). Le XML est invalide (préfixe "ev:" jamais déclaré dans le
# <rss> racine) -> xml.etree refuse de le parser, d'où le parseur regex ci-dessous,
# sur le modèle de parse_brivetourisme_html. Pas de lat/lon par item (ni dans le
# flux ni sur la page) : comme pour Brive Tourisme, tous les items sont approximés
# avec les coordonnées du centre-ville de Périgueux (cfg["perigueux"]["lat"/"lon"]).
# Pour une poignée d'animations récurrentes (~6 items sur 114 au moment de
# l'écriture, ex. « Les dimanches de la Clautre »), <ev:startdate>/<ev:enddate>
# contiennent plusieurs dates à la suite (bug d'export du site, pas un format
# documenté) : on ne garde que la première occurrence de chaque tag plutôt que
# d'essayer de deviner les suivantes -> date réelle et non trompeuse, juste
# incomplète pour la suite de la série (comme pole_prehistoire pour ses dates
# sans année).
_PGX_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_PGX_TITLE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.S)
_PGX_LINK = re.compile(r"<link>\s*(.*?)\s*</link>", re.S)
_PGX_IMAGE = re.compile(r"<image>\s*(.*?)\s*</image>", re.S)
_PGX_DESCRIPTION = re.compile(r"<description><!\[CDATA\[(.*?)\]\]></description>", re.S)
_PGX_CATEGORY = re.compile(r"<category[^>]*>([^<]+)</category>")
_PGX_STARTDATE = re.compile(r"<ev:startdate>(.*?)</ev:startdate>", re.S)
_PGX_ENDDATE = re.compile(r"<ev:enddate>(.*?)</ev:enddate>", re.S)
_PGX_DATE = re.compile(r"[A-Za-z]{3}, \d{1,2} [A-Za-z]{3} \d{4} [\d:]{8} [+-]\d{4}")


def _pgx_first_date(block):
    dates = _PGX_DATE.findall(block)
    if not dates:
        return None
    return parsedate_to_datetime(dates[0]).astimezone(TZ)


def parse_perigueux_rss(page, source_name):
    events = []
    for block in _PGX_ITEM.findall(page):
        tm = _PGX_TITLE.search(block)
        sm = _PGX_STARTDATE.search(block)
        if not tm or not sm:
            continue
        start = _pgx_first_date(sm.group(1))
        if start is None:
            continue
        em = _PGX_ENDDATE.search(block)
        end = _pgx_first_date(em.group(1)) if em else None
        if end is None or end <= start:
            end = start + timedelta(hours=2)
        title = html.unescape(re.sub(r"\s+", " ", tm.group(1))).strip()
        lm = _PGX_LINK.search(block)
        url = html.unescape(lm.group(1)).strip() if lm else ""
        im = _PGX_IMAGE.search(block)
        image = html.unescape(im.group(1)).strip() if im else ""
        dm = _PGX_DESCRIPTION.search(block)
        description = ""
        if dm:
            description = html.unescape(re.sub(r"<[^>]+>", " ", dm.group(1)))
            description = re.sub(r"\s+", " ", description).strip()
        category = openagenda_category(title, _PGX_CATEGORY.findall(block), None)
        events.append(Event(title, start, end, "Périgueux", url, None, None, [source_name],
                             category=category, description=description, image=image))
    return events


def fetch_perigueux(cfg, w_start, w_end):
    src = cfg.get("perigueux")
    if not src:
        return []
    try:
        got = parse_perigueux_rss(http_get(src["url"]), src.get("nom", "Ville de Périgueux"))
    except Exception as e:
        print(f"  Ville de Périgueux ignorée : {e}", file=sys.stderr)
        return []
    events = []
    for e in got:
        if "lat" in src:
            e.lat, e.lon = src["lat"], src["lon"]
        if e.end >= w_start and e.start <= w_end:
            events.append(e)
    return events


# ---------------------------------------------------------------- source 4 : horaires des bibliothèques
_LIB_DAY_LINE = re.compile(r"^([A-ZÀ-Ü][\w& à-ü-]*?)\s*:\s*(.+)$")
# variante sans « : » (ex. site de Sarlat : « Mardi 12h30 – 18h30 »)
_LIB_DAY_LINE_BARE = re.compile(
    r"^(Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche)\s+(\d{1,2}\s*h.*)$", re.I
)


def parse_library_hours(page):
    """Cherche un bloc « Horaires » suivi de lignes « Jour(s) : plage(s) » (ou, à
    défaut, « Jour plage » sans « : », variante rencontrée sur d'autres sites)."""
    tp = _TextLines()
    tp.feed(page)
    lines = [re.sub(r"\s+", " ", l).strip() for l in tp.lines]
    idx = [i for i, l in enumerate(lines) if l]
    for pos, i in enumerate(idx):
        if "horaires" not in norm(lines[i]):
            continue
        horaires = []
        for j in idx[pos + 1:]:
            m = _LIB_DAY_LINE.match(lines[j]) or _LIB_DAY_LINE_BARE.match(lines[j])
            if not m:
                break
            horaires.append((m.group(1).strip(), m.group(2).strip()))
        if horaires:
            return horaires
    return []


def _place_distance(cfg, entry):
    """None si le lieu n'a pas de lat/lon déclarée (source locale, toujours gardée).
    Sinon la distance au centre — le lieu est écarté s'il dépasse le rayon maximal
    de récupération (cfg['rayon_km']) ; en-dessous, l'affichage effectif dépend du
    palier de rayon choisi dans l'interface (voir write_html / paliers_rayon)."""
    if "lat" not in entry or "lon" not in entry:
        return "keep", None
    lat0, lon0, radius = cfg["centre"]["lat"], cfg["centre"]["lon"], cfg["rayon_km"]
    d = haversine(lat0, lon0, entry["lat"], entry["lon"])
    return ("keep" if d <= radius else "drop"), d


def fetch_library_hours(cfg):
    """Bibliothèques : ce sont des lieux (horaires fixes), pas des événements datés."""
    out = []
    for lib in cfg.get("bibliotheques", []):
        keep, dist = _place_distance(cfg, lib)
        if keep == "drop":
            continue
        try:
            horaires = parse_library_hours(http_get(lib["url"]))
        except Exception as e:
            print(f"  Bibliothèque « {lib.get('nom')} » ignorée : {e}", file=sys.stderr)
            continue
        if horaires:
            lines = [f"{jour} : {heures}" for jour, heures in horaires]
            out.append({"nom": lib["nom"], "url": lib["url"], "lines": lines, "links": [], "distance": dist})
    return out


# ---------------------------------------------------------------- source 5 : cinéma (infos, pas de séances)
# Le programme complet (dates/séances) n'existe qu'en PDF mensuel, sans structure
# garantie -> pas de parsing de séances (fragile + dépendance externe). On se
# contente d'un lien toujours à jour vers ce PDF, comme pour un lieu classique.
_CINEMA_PROGRAMME = re.compile(r"^(Programme du .+)$", re.I)
_CINEMA_PHONE = re.compile(r"^T[ée]l\.?\s*:?\s*([\d .]{8,})$")
_CINEMA_POSTAL = re.compile(r"^(\d{5})\s+(.+)$")


def parse_cinema_info(page):
    tp = _TextLines()
    tp.feed(page)
    lines = [re.sub(r"\s+", " ", l).strip() for l in tp.lines]
    info = {}
    for i, l in enumerate(lines):
        m = _CINEMA_PROGRAMME.match(l)
        if m and "programme_text" not in info:
            info["programme_text"] = m.group(1)
            for hi, href in tp.hrefs:
                if hi == i and href.lower().endswith(".pdf"):
                    info["programme_url"] = href
                    break
        m2 = _CINEMA_PHONE.match(l)
        if m2 and "telephone" not in info:
            info["telephone"] = m2.group(1).strip()
        m3 = _CINEMA_POSTAL.match(l)
        if m3 and "adresse" not in info and i > 0 and lines[i - 1].strip():
            info["adresse"] = f"{lines[i - 1].strip()}, {m3.group(1)} {m3.group(2)}"
    return info


def fetch_cinema_info(cfg):
    out = []
    for cine in cfg.get("cinemas", []):
        keep, dist = _place_distance(cfg, cine)
        if keep == "drop":
            continue
        if "lines" in cine:
            # certains sites (multiplexes, JS côté client) ne se prêtent pas au
            # parsing : infos saisies à la main plutôt qu'une carte vide.
            out.append({"nom": cine["nom"], "url": cine["url"], "lines": cine["lines"],
                        "links": cine.get("links", []), "distance": dist})
            continue
        try:
            info = parse_cinema_info(http_get(cine["url"]))
        except Exception as e:
            print(f"  Cinéma « {cine.get('nom')} » ignoré : {e}", file=sys.stderr)
            continue
        lines = [info[k] for k in ("adresse",) if k in info]
        if info.get("telephone"):
            lines.append(f"Tél. : {info['telephone']}")
        links = []
        if info.get("programme_url"):
            links.append((info.get("programme_text", "Programme"), info["programme_url"]))
        elif info.get("programme_text"):
            lines.append(info["programme_text"])
        if lines or links:
            out.append({"nom": cine["nom"], "url": cine["url"], "lines": lines, "links": links, "distance": dist})
    return out


# ---------------------------------------------------------------- fusion
def mark_long_running(events):
    """Une occurrence unique mais très longue (expo, saison, ou événement sur plusieurs
    jours comme un vide-grenier samedi ET dimanche) s'affiche comme les événements
    collapsés à la source : une seule ligne « jusqu'au ... ». Seuil à 20h plutôt qu'à
    2 jours pleins : un vide-grenier samedi 9h → dimanche 18h ne dure que 33h mais
    chevauche bien deux dates, et sans ce marquage l'affichage ne montrerait que
    « samedi » même le dimanche, laissant croire à tort que l'événement est passé. Un
    concert finissant peu après minuit (quelques heures à peine) reste, lui, sous ce
    seuil et continue de s'afficher avec sa seule date de début."""
    for e in events:
        if not e.long_running and (e.end - e.start) > timedelta(hours=20):
            e.long_running = True
    return events


def dedupe(events):
    events = sorted(events, key=lambda e: (e.start, -len(e.url)))
    kept = []
    for ev in events:
        n = norm(ev.title)
        dup = None
        for k in kept:
            if k.start.astimezone(TZ).date() != ev.start.astimezone(TZ).date():
                continue
            m = norm(k.title)
            close = difflib.SequenceMatcher(None, n, m).ratio() >= 0.85
            contained = min(len(n), len(m)) >= 8 and (n in m or m in n)
            if close or contained:
                dup = k
                break
        if dup:
            for s in ev.sources:
                if s not in dup.sources:
                    dup.sources.append(s)
            for att in ev.attributions:
                if att not in dup.attributions:
                    dup.attributions.append(att)
            dup.url = dup.url or ev.url
            dup.place = dup.place or ev.place
            dup.description = dup.description or ev.description
            dup.image = dup.image or ev.image
            if not dup.category or dup.category == "Autre":
                dup.category = ev.category or dup.category
            if dup.lat is None:
                dup.lat, dup.lon = ev.lat, ev.lon
        else:
            kept.append(ev)
    return kept


def apply_distance(events, cfg):
    lat0, lon0, radius = cfg["centre"]["lat"], cfg["centre"]["lon"], cfg["rayon_km"]
    out = []
    for e in events:
        if e.lat is not None and e.lon is not None:
            e.distance = haversine(lat0, lon0, e.lat, e.lon)
            if e.distance > radius:
                continue
        out.append(e)  # sans coordonnées : on garde (flux locaux choisis par toi)
    return sorted(out, key=lambda e: e.start)


# ---------------------------------------------------------------- fenêtres de temps
def weekend_window(now):
    wd = now.weekday()
    if wd >= 5:
        start = now
        sunday = now + timedelta(days=6 - wd)
    else:
        start = (now + timedelta(days=4 - wd)).replace(hour=17, minute=0, second=0, microsecond=0)
        sunday = start + timedelta(days=2)
    return start, sunday.replace(hour=23, minute=59, second=59, microsecond=0)


def next_weekend_window(now):
    """Le week-end qui suit celui renvoyé par weekend_window(now)."""
    _, this_end = weekend_window(now)
    return weekend_window(this_end + timedelta(days=1))


def week_window(now):
    """La semaine entre 'ce week-end' (weekend_window) et 'le week-end suivant'
    (next_weekend_window) : toujours une semaine pleine et non vide, quel que soit
    le jour courant — contrairement à une fenêtre "reste de la semaine avant le
    prochain week-end", qui elle peut être vide (si on est déjà dans le week-end) ou
    partielle (si on est en milieu de semaine)."""
    _, wk_end = weekend_window(now)
    next_start, _ = next_weekend_window(now)
    return wk_end + timedelta(seconds=1), next_start


# ---------------------------------------------------------------- sorties
def event_uid(title, start):
    """Identifiant stable d'un événement (titre + date), utilisé à la fois pour
    l'UID iCal et pour repérer un événement enregistré en favori d'une génération
    de la page à l'autre (le titre exact et la date ne changent pas)."""
    return hashlib.sha1(f"{norm(title)}{start.date()}".encode()).hexdigest()[:20]


def _ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        limit = 75 if not parts else 74
        if len(cur) + len(cb) > limit:
            parts.append(cur)
            cur = b""
        cur += cb
    parts.append(cur)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def write_ics(events, path, now):
    def utc(d):
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//agenda-local//FR", "X-WR-CALNAME:Sorties locales", "CALSCALE:GREGORIAN"]
    for e in events:
        uid = event_uid(e.title, e.start) + "@agenda-local"
        desc = "Source : " + ", ".join(e.sources)
        if e.distance is not None:
            desc += f"\nDistance : {e.distance:.0f} km"
        for src, producer, updated in e.attributions:
            if producer:
                desc += f"\n{src} : {producer}" + (f", mis à jour le {updated}" if updated else "")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{utc(now)}",
            *(
                [f"DTSTART;VALUE=DATE:{e.start:%Y%m%d}", f"DTEND;VALUE=DATE:{e.end:%Y%m%d}"]
                if e.all_day
                else [f"DTSTART:{utc(e.start)}", f"DTEND:{utc(e.end)}"]
            ),
            f"SUMMARY:{_ics_escape(e.title)}",
        ]
        if e.place:
            lines.append(f"LOCATION:{_ics_escape(e.place)}")
        if e.url:
            lines.append(f"URL:{e.url}")
        lines += [f"DESCRIPTION:{_ics_escape(desc)}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    Path(path).write_text("\r\n".join(_fold(l) for l in lines) + "\r\n", encoding="utf-8")


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _fmt_when(e):
    s, en = e.start.astimezone(TZ), e.end.astimezone(TZ)
    if e.long_running:
        # « jusqu'au ... » seul est ambigu pour un événement court sur 2-4 jours (ex.
        # vide-grenier samedi + dimanche) : avant qu'il ne commence, on dirait qu'il
        # est déjà en cours depuis longtemps. Réservé aux événements vraiment longs
        # (expos, saisons) ; en dessous, on affiche les deux bornes.
        span_days = (en.date() - s.date()).days
        if 1 <= span_days <= 3:
            return f"du {JOURS[s.weekday()]} {s:%d/%m} au {JOURS[en.weekday()]} {en:%d/%m}"
        return f"jusqu'au {JOURS[en.weekday()]} {en:%d/%m}"
    if e.all_day:
        return f"{JOURS[s.weekday()]} {s:%d/%m} · horaire non précisé"
    return f"{JOURS[s.weekday()]} {s:%d/%m} {s:%H:%M}"


def _fav_button(e, uid):
    """Étoile d'enregistrement en favori : toutes les données nécessaires pour
    reconstruire la carte dans la section Favoris sont portées en attributs, car
    cette section est peuplée en JS depuis localStorage (l'événement peut avoir
    disparu du flux lors d'une régénération ultérieure de la page)."""
    return (
        f'<button class="fav" type="button" data-id="{uid}" data-title="{html.escape(e.title)}" '
        f'data-when="{html.escape(_fmt_when(e))}" data-place="{html.escape(e.place)}" '
        f'data-url="{html.escape(e.url)}" data-cat="{html.escape(e.category or "Autre")}" '
        f'data-start="{e.start.astimezone(TZ).isoformat()}" data-end="{e.end.astimezone(TZ).isoformat()}" '
        f'data-desc="{html.escape(e.description)}" data-img="{html.escape(e.image)}" '
        f'aria-label="Enregistrer dans mes favoris">☆</button>'
    )


def _card(e):
    title = html.escape(e.title)
    uid = event_uid(e.title, e.start)
    meta = [x for x in (html.escape(e.place), f"{e.distance:.0f} km" if e.distance is not None else "") if x]
    attr_lines = "".join(
        f'<div class="a">Source : {html.escape(producer)}, mis à jour le {html.escape(updated)}</div>'
        for _, producer, updated in e.attributions
        if producer
    )
    cat = html.escape(e.category or "Autre")
    dist_attr = f' data-dist="{e.distance:.0f}"' if e.distance is not None else ""
    place_attr = f' data-place="{html.escape(norm(e.place))}"' if e.place else ""
    bare_link = e.url and not e.description and not e.image
    title_html = f'<a href="{html.escape(e.url)}">{title}</a>' if bare_link else title
    header = (
        f'<div class="cardhead">{_fav_button(e, uid)}<div class="when">{_fmt_when(e)}</div>'
        f'<div class="t">{title_html}</div></div>'
        f'<div class="m">{" · ".join(meta)}</div><div class="s">{html.escape(", ".join(e.sources))}</div>{attr_lines}'
    )
    if bare_link:
        return f'<li data-cat="{cat}" data-id="{uid}"{dist_attr}{place_attr}>{header}</li>'
    detail = ""
    if e.image:
        detail += f'<img src="{html.escape(e.image)}" alt="" loading="lazy">'
    if e.description:
        detail += f'<div class="d">{html.escape(e.description)}</div>'
    if e.url:
        detail += f'<div><a class="more" href="{html.escape(e.url)}">Plus d\'infos</a></div>'
    else:
        # pas de page dédiée à l'évènement : une recherche Google sur le titre + le
        # lieu est toujours utile pour un particulier, contrairement à la fiche
        # technique DATAtourisme (vocabulaire RDF brut, jamais utile en pratique)
        q = parse.quote(f"{e.title} {e.place}".strip())
        detail += f'<div><a class="more" href="https://www.google.com/search?q={q}">Rechercher en ligne</a></div>'
    return (
        f'<li data-cat="{cat}" data-id="{uid}"{dist_attr}{place_attr}><details><summary>{header}</summary>{detail}</details></li>'
    )


def _slug(label):
    return norm(label).replace(" ", "-")


def _section(label, events):
    items = "".join(_card(e) for e in events) or "<li>Rien trouvé.</li>"
    return f'<section><h2 id="{_slug(label)}">{html.escape(label)}</h2><ul>{items}</ul></section>'


def _bucketize(events, windows):
    """Range chaque événement dans la première fenêtre chronologique qu'il chevauche
    (les fenêtres sont déjà triées par ordre chronologique et ne se recouvrent pas)."""
    buckets = {label: [] for label, _, _ in windows}
    buckets["À venir"] = []
    assigned = set()
    for label, a, b in windows:
        for e in events:
            if id(e) in assigned:
                continue
            if e.end >= a and e.start <= b:
                buckets[label].append(e)
                assigned.add(id(e))
    for e in events:
        if id(e) not in assigned:
            buckets["À venir"].append(e)
    return buckets


def _place_card(place):
    """Carte pour un lieu culturel (bibliothèque, cinéma...) : infos fixes, pas de date."""
    name = html.escape(place["nom"])
    if place.get("url"):
        name = f'<a href="{html.escape(place["url"])}">{name}</a>'
    lines = "".join(f"<div>{html.escape(l)}</div>" for l in place.get("lines", []))
    if place.get("distance") is not None:
        lines += f'<div>{place["distance"]:.0f} km</div>'
    links = "".join(
        f'<div><a href="{html.escape(u)}">{html.escape(lbl)}</a></div>' for lbl, u in place.get("links", [])
    )
    dist_attr = f' data-dist="{place["distance"]:.0f}"' if place.get("distance") is not None else ""
    return f'<li{dist_attr}><div class="t">{name}</div><div class="m">{lines}{links}</div></li>'


def write_html(events, path, cfg, now, lieux=(), autres_liens=()):
    wk_start, wk_end = weekend_window(now)
    week_start, week_end = week_window(now)
    next_wk_start, next_wk_end = next_weekend_window(now)
    windows = [
        ("Ce week-end", wk_start, wk_end),
        ("La semaine prochaine", week_start, week_end),
        ("Week-end suivant", next_wk_start, next_wk_end),
    ]
    # "Cette semaine" (le reste de la semaine en cours, avant le prochain week-end) n'a
    # de sens que si on n'est pas déjà dans ce week-end — sinon la fenêtre serait vide.
    if now < wk_start:
        windows.append(("Cette semaine", now, wk_start))
    windows.sort(key=lambda w: w[1])
    buckets = _bucketize(events, windows)

    label = html.escape(cfg["centre"].get("nom", ""))

    # la section Favoris a son propre onglet (voir tabs_html plus bas), elle ne
    # fait donc plus partie de ce menu qui ne couvre que l'onglet Découvrir.
    section_defs = list(windows)
    if buckets["À venir"]:
        section_defs.append(("À venir", None, None))
    if lieux:
        section_defs.append(("Lieux culturels", None, None))

    # Trois menus déroulants alignés (aller à / catégorie / rayon) plutôt que des
    # rangées de pastilles empilées : un seul style, une seule hauteur de ligne,
    # aucun scroll horizontal à gérer. Le menu "aller à" scrolle vers la section
    # puis se réinitialise (voir JS plus bas) : c'est une action, pas un filtre.
    jump_options = "".join(
        f'<option value="{_slug(lbl)}">{html.escape(lbl)}</option>' for lbl, _, _ in section_defs
    )
    jump_select = (
        f'<select class="selfilter" id="jumpsel"><option value="" selected>Aller à…</option>{jump_options}</select>'
        if len(section_defs) > 1
        else ""
    )

    categories = sorted({e.category or "Autre" for e in events} & set(CATEGORIES), key=CATEGORIES.index)
    cat_options = "".join(f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in categories)
    cat_select = (
        f'<select class="selfilter" id="catsel"><option value="__all__" selected>Toutes les catégories</option>{cat_options}</select>'
        if len(categories) > 1
        else ""
    )

    # paliers de rayon : options cumulatives (chacune inclut les précédentes), triées du
    # plus petit au plus grand ; la première est active par défaut pour ne rien changer
    # à l'affichage habituel tant qu'on ne touche pas au menu.
    paliers = sorted(cfg.get("paliers_rayon", []), key=lambda p: p["km"])
    dist_select = ""
    if len(paliers) > 1:
        dist_options = "".join(
            f'<option value="{p["km"]}" data-include="{html.escape(",".join(norm(v) for v in p.get("inclure", [])))}"'
            + (" selected" if i == 0 else "")
            + f'>{html.escape(p["nom"])}'
            + ("" if p.get("inclure") else f' (≤ {p["km"]} km)')
            + "</option>"
            for i, p in enumerate(paliers)
        )
        dist_select = f'<select class="selfilter" id="distsel">{dist_options}</select>'
    default_palier = paliers[0] if paliers else None
    default_km = default_palier["km"] if default_palier and not default_palier.get("inclure") else cfg["rayon_km"]
    h1_suffix = "" if default_palier and default_palier.get("inclure") else f"(≤ {default_km} km)"

    filters_html = f'<div class="filterbar">{jump_select}{cat_select}{dist_select}</div>'

    # l'URL du worker n'est pas sensible (juste "où" envoyer), mais le code d'accès
    # partagé, lui, ne doit JAMAIS être écrit dans cette page publique — voir la
    # fonction de synchro en JS plus bas, qui le demande à l'utilisateur et le garde
    # uniquement dans son propre localStorage.
    sync_cfg = cfg.get("sync_favoris") or {}
    sync_worker_url = sync_cfg.get("worker_url", "").rstrip("/")
    sync_html = (
        f'<div class="sync-box" id="sync-box">'
        f'<button type="button" id="sync-activate">Activer la synchro partagée</button>'
        f'<p class="s" id="sync-status" style="display:none"></p>'
        f"</div>"
        if sync_worker_url
        else ""
    )

    favoris_html = (
        f'<section id="{_slug("Favoris")}"><h2>Favoris</h2>'
        f'<ul id="favoris-upcoming"><li>Aucun favori enregistré — cliquez sur ☆ sur un événement pour le garder ici.</li></ul>'
        f'<div id="favoris-archived-wrap" style="display:none"><h3>Passés</h3><ul id="favoris-archived"></ul></div>'
        f"{sync_html}"
        f"</section>"
    )

    body = "".join(_section(label_, buckets[label_]) for label_, _, _ in windows)
    if buckets["À venir"]:
        body += _section("À venir", buckets["À venir"])

    if lieux:
        body += (
            f'<section><h2 id="{_slug("Lieux culturels")}">Lieux culturels</h2><ul>'
            + "".join(_place_card(p) for p in lieux)
            + "</ul></section>"
        )

    # sources dont les événements ne se prêtent pas à une agrégation fiable (trop
    # de dates/lieux différents par événement, mise en page trop irrégulière...) :
    # on pointe vers le site plutôt que de scraper. Repliée par défaut (<details>,
    # même widget que les cartes d'événements) : une seule ligne juste sous le
    # titre, repérable à chaque ouverture sans monopoliser la place en
    # permanence — un gros bloc ouvert à l'année aurait vite lassé. Réutilisable
    # pour toute future source du même genre en ajoutant une entrée à
    # cfg["liens_utiles"].
    autres_liens_html = (
        '<details class="alsobox"><summary>À voir aussi'
        f' <span class="s">({len(autres_liens)})</span></summary><ul>'
        + "".join(_place_card(p) for p in autres_liens)
        + "</ul></details>"
        if autres_liens
        else ""
    )

    # deux onglets pour ne pas tout empiler en haut de page : Découvrir (nav
    # d'ancres, filtres, sections datées) et Mes favoris (liste + synchro) ; le
    # dernier onglet consulté est mémorisé (localStorage) pour rouvrir directement
    # dessus au prochain chargement.
    tabs_html = (
        '<div class="tabs">'
        '<button type="button" class="tabbtn" data-tab="decouvrir">Découvrir</button>'
        '<button type="button" class="tabbtn" data-tab="favoris">Mes favoris</button>'
        "</div>"
        '<div id="tab-decouvrir" class="tabpanel">'
        f"{filters_html}{body}"
        "</div>"
        '<div id="tab-favoris" class="tabpanel" hidden>'
        f"{favoris_html}"
        "</div>"
    )

    has_datatourisme = any(src == "DATAtourisme" for e in events for src, _, _ in e.attributions)
    footer = f'<p class="s">Mis à jour le {now:%d/%m/%Y à %H:%M}</p>'
    if has_datatourisme:
        footer += '<p class="s">Données : DATAtourisme, Licence Ouverte Etalab.</p>'
    footer += '<p class="s">Pages de la mairie de Montignac : usage personnel, contenu non republié.</p>'

    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sorties autour de {label}</title>
<style>
body{{font:16px/1.4 system-ui,sans-serif;margin:0 auto;max-width:640px;padding:12px;color:#1a1a1a;background:#fafafa}}
h1{{font-size:20px;margin-bottom:4px}} h2{{font-size:17px;margin:24px 0 8px;border-bottom:1px solid #ddd;scroll-margin-top:64px}}
ul{{list-style:none;padding:0;margin:0}} li{{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:10px 12px;margin-bottom:8px}}
.when{{font-weight:600;color:#0a5}} .t{{margin:2px 0;font-weight:600;color:#1a1a1a}} .m,.s{{font-size:13px;color:#666}} .a{{font-size:12px;color:#888;margin-top:2px}} a{{color:#0a5;text-decoration:none}}
.t a{{color:inherit;text-decoration:none}} .t a::after{{content:" ↗";font-size:.75em;opacity:.55}}
summary{{cursor:pointer;list-style:none;position:relative;padding-right:20px}} summary::-webkit-details-marker{{display:none}}
summary::after{{content:"›";position:absolute;top:0;right:0;font-size:20px;line-height:1;color:#999;transition:transform .15s}}
details[open] summary::after{{transform:rotate(90deg)}}
details img{{width:100%;max-height:200px;object-fit:cover;border-radius:6px;margin-top:8px}}
details .d{{font-size:14px;margin-top:6px;color:#333}}
.more{{display:inline-block;margin-top:8px;font-size:13px;font-weight:600;color:#0a5;border:1px solid #0a5;border-radius:12px;padding:3px 10px}}
.filterbar{{position:sticky;top:0;background:#fafafa;display:flex;flex-wrap:wrap;gap:8px;padding:10px 0;margin-bottom:4px;z-index:1}}
.selfilter{{flex:1 1 150px;min-width:0;font:inherit;font-size:13px;color:inherit;background:#fff url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path fill="%23666" d="M5 7l5 6 5-6z"/></svg>') no-repeat right 10px center;background-size:14px;border:1px solid #ccc;border-radius:10px;padding:7px 28px 7px 10px;-webkit-appearance:none;appearance:none}}
li.hidden{{display:none}}
.cardhead{{overflow:hidden}}
.fav{{float:right;background:none;border:none;font-size:20px;line-height:1.2;cursor:pointer;color:#bbb;padding:0 0 4px 8px}}
.fav.active{{color:#e0a500}}
h3{{font-size:14px;margin:14px 0 6px;color:#666}}
.fav-toggle{{display:block;width:100%;border:1px dashed #ccc;background:none;border-radius:8px;padding:8px;font:inherit;font-size:13px;color:#0a5;cursor:pointer}}
.tabs{{display:flex;gap:6px;margin:10px 0}}
.tabbtn{{flex:1;border:1px solid #ccc;background:#fff;border-radius:10px;padding:8px;font:inherit;font-weight:600;color:inherit;cursor:pointer}}
.tabbtn.active{{background:#0a5;border-color:#0a5;color:#fff}}
.tabpanel[hidden]{{display:none}}
.alsobox{{margin:8px 0}} .alsobox summary{{font-size:13px;font-weight:600;color:#666;padding:4px 20px 4px 0}}
.alsobox ul{{margin-top:6px}} .alsobox li{{border-left:3px solid #0a5}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}li{{background:#1c1c1c;border-color:#333}}.m,.s,.a{{color:#aaa}}.t{{color:#eee}}a{{color:#5fd08a}}.filterbar{{background:#111}}.selfilter{{background-color:#1c1c1c;border-color:#444;color:#eee;background-image:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path fill="%23aaa" d="M5 7l5 6 5-6z"/></svg>')}}details .d{{color:#ccc}}.more{{color:#5fd08a;border-color:#5fd08a}}summary::after{{color:#777}}.fav{{color:#555}}.fav.active{{color:#e0a500}}h3{{color:#999}}.fav-toggle{{border-color:#444;color:#5fd08a}}.tabbtn{{background:#1c1c1c;border-color:#444;color:#eee}}.tabbtn.active{{background:#0a5;border-color:#0a5;color:#fff}}.alsobox summary{{color:#999}}.alsobox li{{border-left-color:#5fd08a}}}}
</style></head><body><h1>Sorties autour de {label} <span id="km-suffix">{h1_suffix}</span></h1>
{autres_liens_html}
{tabs_html}{footer}
<script>
// --- onglets Découvrir / Mes favoris ---
var tabButtons = document.querySelectorAll('.tabbtn');
function showTab(name){{
  document.getElementById('tab-decouvrir').hidden = name !== 'decouvrir';
  document.getElementById('tab-favoris').hidden = name !== 'favoris';
  tabButtons.forEach(function(b){{ b.classList.toggle('active', b.dataset.tab === name); }});
  try {{ localStorage.setItem('agendaTab', name); }} catch (err) {{}}
}}
tabButtons.forEach(function(b){{
  b.addEventListener('click', function(){{ showTab(b.dataset.tab); }});
}});
var savedTab = '';
try {{ savedTab = localStorage.getItem('agendaTab') || ''; }} catch (err) {{}}
showTab(savedTab === 'favoris' ? 'favoris' : 'decouvrir');

var catSel = document.getElementById('catsel');
var distSel = document.getElementById('distsel');
var currentCat = '__all__';
var currentDist = {default_km};
var currentInclude = (distSel && distSel.selectedOptions[0] && distSel.selectedOptions[0].dataset.include) ?
  distSel.selectedOptions[0].dataset.include.split(',').filter(Boolean) : [];
function applyFilters(){{
  document.querySelectorAll('li').forEach(function(li){{
    var catOk = currentCat === '__all__' || !li.dataset.cat || li.dataset.cat === currentCat;
    var place = li.dataset.place || '';
    var villeOk = !currentInclude.length || currentInclude.some(function(v){{ return place.indexOf(v) !== -1; }});
    var distOk = currentInclude.length || !li.dataset.dist || Number(li.dataset.dist) <= currentDist;
    li.classList.toggle('hidden', !(catOk && distOk && villeOk));
  }});
}}
applyFilters();
if (catSel) {{
  catSel.addEventListener('change', function(){{
    currentCat = catSel.value;
    applyFilters();
  }});
}}
var kmSuffix = document.getElementById('km-suffix');
if (distSel) {{
  distSel.addEventListener('change', function(){{
    var opt = distSel.selectedOptions[0];
    currentDist = Number(opt.value);
    currentInclude = opt.dataset.include ? opt.dataset.include.split(',').filter(Boolean) : [];
    if (kmSuffix) {{ kmSuffix.textContent = currentInclude.length ? '' : '(≤ ' + currentDist + ' km)'; }}
    applyFilters();
  }});
}}
var jumpSel = document.getElementById('jumpsel');
if (jumpSel) {{
  jumpSel.addEventListener('change', function(){{
    var target = document.getElementById(jumpSel.value);
    if (target) {{ target.scrollIntoView({{behavior: 'smooth', block: 'start'}}); }}
    jumpSel.selectedIndex = 0;
  }});
}}

// --- favoris (localStorage, persiste d'une génération de page à l'autre) ---
function favEsc(s){{
  var d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}}
function loadFavs(){{
  try {{ return JSON.parse(localStorage.getItem('agendaFavoris') || '{{}}'); }} catch (err) {{ return {{}}; }}
}}
function saveFavs(favs){{
  try {{ localStorage.setItem('agendaFavoris', JSON.stringify(favs)); }} catch (err) {{}}
}}
var favs = loadFavs();

// --- synchro partagée (opt-in) vers un service externe (worker Cloudflare, voir
// cloudflare-worker/), pour que deux personnes voient les mêmes favoris et
// s'abonnent au même calendrier Google Agenda. Désactivée par défaut : tant que
// personne n'a saisi le code d'accès, tout reste purement local (comportement
// d'origine). Le code n'est jamais écrit dans cette page ni dans le repo : il est
// demandé une fois via prompt() et gardé uniquement dans le localStorage de la
// personne qui l'a saisi.
var SYNC_URL = {json.dumps(sync_worker_url)};
function syncKey(){{
  try {{ return localStorage.getItem('agendaSyncKey') || ''; }} catch (err) {{ return ''; }}
}}
function setSyncKey(k){{
  try {{ if (k) localStorage.setItem('agendaSyncKey', k); else localStorage.removeItem('agendaSyncKey'); }} catch (err) {{}}
}}
function pushFavori(id, data){{
  var key = syncKey();
  if (!SYNC_URL || !key) return;
  fetch(SYNC_URL + '/favoris', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{secret: key, id: id, data: data || null}}),
  }}).catch(function(err){{}});
}}
function renderSyncStatus(){{
  var status = document.getElementById('sync-status');
  var btn = document.getElementById('sync-activate');
  if (!status || !btn) return;
  var key = syncKey();
  if (key) {{
    var icsUrl = SYNC_URL + '/favoris.ics?key=' + encodeURIComponent(key);
    status.innerHTML = 'Synchro activée. Lien à coller dans Google Calendar : <code>' + favEsc(icsUrl) + '</code> '
      + '<button type="button" id="sync-deactivate">Désactiver ici</button>';
    status.style.display = '';
    btn.style.display = 'none';
  }} else {{
    status.style.display = 'none';
    btn.style.display = '';
  }}
}}
// Le serveur est la seule source de vérité dès que la synchro est active : à
// chaque chargement on remplace l'état local par celui du serveur (pas de
// fusion) ; à chaque clic on pousse tout de suite le changement au serveur.
// C'est ce qui évite qu'un appareil resté en retard ne "ressuscite" un favori
// supprimé ailleurs. Contrepartie assumée : un clic fait hors-ligne peut être
// perdu au rechargement suivant si le push n'est jamais parti.
function fetchFavsFromServer(done){{
  var key = syncKey();
  if (!SYNC_URL || !key) {{ done(); return; }}
  fetch(SYNC_URL + '/favoris?key=' + encodeURIComponent(key)).then(function(r){{
    return r.ok ? r.json() : null;
  }}).then(function(serverFavs){{
    if (serverFavs) {{
      favs = serverFavs;
      saveFavs(favs);
    }}
    done();
  }}).catch(function(err){{ done(); }});
}}
// Activation de la synchro sur un appareil qui avait déjà des favoris locaux
// (avant que la synchro n'existe, ou saisis hors-ligne) : ceux absents du
// serveur sont poussés une bonne fois pour ne pas les perdre, puis l'état
// local devient l'union — après quoi les chargements suivants redeviennent
// une simple lecture du serveur, sans fusion.
function activateSync(code){{
  var localFavs = favs;
  setSyncKey(code);
  fetch(SYNC_URL + '/favoris?key=' + encodeURIComponent(code)).then(function(r){{
    return r.ok ? r.json() : {{}};
  }}).then(function(serverFavs){{
    serverFavs = serverFavs || {{}};
    Object.keys(localFavs).forEach(function(id){{
      if (!serverFavs[id]) pushFavori(id, localFavs[id]);
    }});
    favs = Object.assign({{}}, serverFavs, localFavs);
    saveFavs(favs);
    renderFavoris();
    renderSyncStatus();
  }}).catch(function(err){{ renderFavoris(); renderSyncStatus(); }});
}}

function markFavButtons(){{
  document.querySelectorAll('.fav').forEach(function(b){{
    var on = !!favs[b.dataset.id];
    b.textContent = on ? '★' : '☆';
    b.classList.toggle('active', on);
    b.setAttribute('aria-label', on ? 'Retirer des favoris' : 'Enregistrer dans mes favoris');
  }});
}}

function favCardHtml(id, f){{
  var bareLink = f.url && !f.desc && !f.img;
  var titleHtml = bareLink ? '<a href="' + favEsc(f.url) + '">' + favEsc(f.title) + '</a>' : favEsc(f.title);
  var header = '<div class="cardhead"><button class="fav" type="button" data-id="' + favEsc(id) + '"></button>'
    + '<div class="when">' + favEsc(f.when) + '</div><div class="t">' + titleHtml + '</div></div>'
    + (f.place ? '<div class="m">' + favEsc(f.place) + '</div>' : '');
  if (bareLink) {{
    return '<li data-cat="' + favEsc(f.cat) + '" data-id="' + favEsc(id) + '">' + header + '</li>';
  }}
  var detail = '';
  if (f.img) detail += '<img src="' + favEsc(f.img) + '" alt="" loading="lazy">';
  if (f.desc) detail += '<div class="d">' + favEsc(f.desc) + '</div>';
  if (f.url) detail += '<div><a class="more" href="' + favEsc(f.url) + '">Plus d\\'infos</a></div>';
  return '<li data-cat="' + favEsc(f.cat) + '" data-id="' + favEsc(id) + '">'
    + '<details><summary>' + header + '</summary>' + detail + '</details></li>';
}}

var ARCHIVE_LIMIT = 5;
var archivedExpanded = false;

function renderFavoris(){{
  var upcoming = [], archived = [];
  var now = new Date();
  Object.keys(favs).forEach(function(id){{
    // un évènement long (expo, saison...) reste "à venir" tant qu'il n'est pas
    // terminé, même s'il a déjà commencé — c'est la fin (end), pas le début
    // (start), qui détermine s'il doit passer en archives.
    (new Date(favs[id].end || favs[id].start) >= now ? upcoming : archived).push(id);
  }});
  upcoming.sort(function(a, b){{ return new Date(favs[a].start) - new Date(favs[b].start); }});
  archived.sort(function(a, b){{ return new Date(favs[b].end || favs[b].start) - new Date(favs[a].end || favs[a].start); }});
  var upcomingList = document.getElementById('favoris-upcoming');
  var archivedWrap = document.getElementById('favoris-archived-wrap');
  var archivedList = document.getElementById('favoris-archived');
  upcomingList.innerHTML = upcoming.length
    ? upcoming.map(function(id){{ return favCardHtml(id, favs[id]); }}).join('')
    : '<li>Aucun favori enregistré — cliquez sur ☆ sur un événement pour le garder ici.</li>';
  var shown = archivedExpanded ? archived : archived.slice(0, ARCHIVE_LIMIT);
  var archiveHtml = shown.map(function(id){{ return favCardHtml(id, favs[id]); }}).join('');
  if (archived.length > ARCHIVE_LIMIT) {{
    var label = archivedExpanded ? 'Réduire' : 'Afficher les ' + (archived.length - ARCHIVE_LIMIT) + ' précédents';
    archiveHtml += '<li><button type="button" class="fav-toggle" id="favoris-toggle-archive">' + label + '</button></li>';
  }}
  archivedList.innerHTML = archiveHtml;
  archivedWrap.style.display = archived.length ? '' : 'none';
  markFavButtons();
}}

document.body.addEventListener('click', function(ev){{
  if (ev.target.closest('#favoris-toggle-archive')) {{
    archivedExpanded = !archivedExpanded;
    renderFavoris();
    return;
  }}
  if (ev.target.closest('#sync-activate')) {{
    var code = window.prompt('Code de synchro partagée (donné une seule fois entre les deux personnes concernées) :');
    if (code) {{ activateSync(code.trim()); }}
    return;
  }}
  if (ev.target.closest('#sync-deactivate')) {{
    setSyncKey('');
    renderSyncStatus();
    return;
  }}
  var b = ev.target.closest('.fav');
  if (!b) return;
  ev.preventDefault();
  ev.stopPropagation();
  var id = b.dataset.id;
  if (favs[id]) {{
    delete favs[id];
    pushFavori(id, null);
  }} else {{
    favs[id] = {{
      title: b.dataset.title, when: b.dataset.when, place: b.dataset.place,
      url: b.dataset.url, cat: b.dataset.cat, start: b.dataset.start, end: b.dataset.end,
      desc: b.dataset.desc, img: b.dataset.img,
    }};
    pushFavori(id, favs[id]);
  }}
  saveFavs(favs);
  renderFavoris();
}});

renderSyncStatus();
fetchFavsFromServer(renderFavoris);
</script>
</body></html>"""
    Path(path).write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- main
def probe(cfg):
    print("Test OpenAgenda (Opendatasoft)…")
    geo = ods_geo_field()
    print("  champ géographique détecté :", geo)
    now = datetime.now(TZ)
    evs = fetch_openagenda(cfg, now, now + timedelta(days=14))
    print(f"  {len(evs)} occurrences brutes sur 14 jours")
    if evs:
        ds = [haversine(cfg['centre']['lat'], cfg['centre']['lon'], e.lat, e.lon) for e in evs if e.lat is not None]
        if ds:
            print(f"  distance min/max au centre : {min(ds):.0f} / {max(ds):.0f} km (doit être <= {cfg['rayon_km']})")
        print("  exemple :", evs[0].title, "|", evs[0].start, "|", evs[0].place)
    else:
        print("  aucun résultat : élargis le rayon, ou envoie-moi ce message pour ajuster la requête")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.probe:
        return probe(cfg)

    now = datetime.now(TZ)
    w_start, w_end = now, now + timedelta(days=cfg.get("jours", 30))

    print("Récupération…")
    events = []
    if cfg.get("sources", {}).get("openagenda", True):
        try:
            got = fetch_openagenda(cfg, w_start, w_end)
            print(f"  OpenAgenda : {len(got)}")
            events += got
        except Exception as e:  # une source cassée ne bloque pas les autres
            print(f"  OpenAgenda ignorée : {e}", file=sys.stderr)
    if cfg.get("sources", {}).get("datatourisme", True):
        try:
            got = fetch_datatourisme(cfg, w_start, w_end)
            print(f"  DATAtourisme : {len(got)}")
            events += got
        except Exception as e:
            print(f"  DATAtourisme ignorée : {e}", file=sys.stderr)
    got = fetch_feeds(cfg, w_start, w_end)
    print(f"  Flux iCal : {len(got)}")
    events += got
    got = fetch_web_pages(cfg, w_start, w_end)
    print(f"  Pages web : {len(got)}")
    events += got
    try:
        got = fetch_leberou(cfg, w_start, w_end)
        print(f"  Festival Le Lébérou : {len(got)}")
        events += got
    except Exception as e:
        print(f"  Festival Le Lébérou ignoré : {e}", file=sys.stderr)
    got = fetch_pole_prehistoire(cfg, w_start, w_end)
    print(f"  Pôle international de la Préhistoire : {len(got)}")
    events += got
    got = fetch_brivetourisme(cfg, w_start, w_end)
    print(f"  Brive Tourisme : {len(got)}")
    events += got
    got = fetch_perigueux(cfg, w_start, w_end)
    print(f"  Ville de Périgueux : {len(got)}")
    events += got

    lieux = fetch_library_hours(cfg) + fetch_cinema_info(cfg)
    print(f"  Lieux culturels : {len(lieux)}")
    autres_liens = [
        {"nom": s["nom"], "url": s.get("url"), "lines": s.get("lignes", []),
         "links": [tuple(l) for l in s.get("liens", [])]}
        for s in cfg.get("liens_utiles", [])
    ]

    events = mark_long_running(apply_distance(dedupe(events), cfg))
    out = Path(cfg.get("dossier_sortie", "sortie"))
    out.mkdir(exist_ok=True)
    write_ics(events, out / "agenda.ics", now)
    write_html(events, out / "index.html", cfg, now, lieux, autres_liens)
    print(f"{len(events)} événements -> {out}/agenda.ics et {out}/index.html")


if __name__ == "__main__":
    main()
