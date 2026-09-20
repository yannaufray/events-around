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
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
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


# catégories grossières utilisées pour les filtres de la page HTML
CATEGORIES = [
    "Culture & spectacles",
    "Patrimoine & visites",
    "Marchés & fêtes",
    "Sport",
    "Enfants & familles",
    "Autre",
]


def openagenda_category(title, keywords, origin_title):
    text = norm(f"{title} {' '.join(keywords or [])} {origin_title or ''}")
    if any(k in text for k in ("emploi", "travail", "formation", "recrutement", "job dating")):
        return "Emploi & formation"  # exclu en amont dans fetch_openagenda, jamais affiché
    if any(k in text for k in ("patrimoine", "chateau", "grotte", "musee", "abbaye", "eglise", "jardin", "visite")):
        return "Patrimoine & visites"
    if any(
        k in text
        for k in (
            "concert", "festival", "spectacle", "theatre", "danse", "musique", "cinema", "expo",
            "conference", "vernissage", "dedicace", "rencontre", "lecture", "lecture publique", "artiste",
        )
    ):
        return "Culture & spectacles"
    if any(k in text for k in ("marche", "fete", "vide grenier", "brocante", "foire")):
        return "Marchés & fêtes"
    if any(k in text for k in ("sport", "rugby", "petanque", "tournoi", "randonnee")):
        return "Sport"
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
    """DATAtourisme étiquette large (ex : les Journées du patrimoine portent le type
    générique « SportsEvent »), donc on ne se fie aux types génériques Sport qu'en
    dernier recours, après un indice textuel dans le titre."""
    types = set(types or [])
    for key, cat in _DT_CATEGORY_ORDER:
        if key in types:
            return cat
    text = norm(title)
    if any(k in text for k in ("patrimoine", "chateau", "abbaye", "eglise", "musee", "grotte", "visite")):
        return "Patrimoine & visites"
    if any(k in text for k in ("conference", "vernissage", "dedicace", "rencontre", "lecture", "artiste")):
        return "Culture & spectacles"
    if types & {"SportsEvent", "SportsCompetition", "Rambling"}:
        return "Sport"
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
def http_get(url, params=None, timeout=30):
    if params:
        url = url + "?" + parse.urlencode(params)
    req = request.Request(url, headers={"User-Agent": UA})
    with request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_get_json(url, params=None):
    return json.loads(http_get(url, params))


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


def _datatourisme_object_events(obj, w_start, w_end):
    label = obj.get("label") or {}
    title = label.get("@fr") or label.get("@en") or next(iter(label.values()), "")
    if not title:
        return []
    loc = (obj.get("isLocatedAt") or [{}])[0]
    geo = loc.get("geo") or {}
    lat_e, lon_e = geo.get("latitude"), geo.get("longitude")
    addr = (loc.get("address") or [{}])[0]
    city = ((addr.get("hasAddressCity") or {}).get("label") or {}).get("@fr", "")
    street = (addr.get("streetAddress") or [""])[0]
    place = ", ".join(x for x in (street, city) if x)
    producer = (obj.get("hasBeenCreatedBy") or {}).get("legalName", "")
    updated = _fmt_date_fr(obj.get("lastUpdateDatatourisme") or obj.get("lastUpdate") or "")
    attribution = [("DATAtourisme", producer, updated)]
    category = datatourisme_category(obj.get("type"), title)

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
    url = obj.get("uri", "")
    if len(occ) > 7:  # exposition / animation quotidienne : une seule ligne
        occ = [(occ[0][0], occ[-1][1])]
        long_running = True
    else:
        long_running = False
    return [
        Event(title, b, e, place, url, lat_e, lon_e, ["DATAtourisme"], long_running=long_running, attributions=list(attribution), category=category)
        for b, e in occ
    ]


def fetch_datatourisme(cfg, w_start, w_end):
    key = datatourisme_key()
    if not key:
        raise RuntimeError("DATATOURISME_KEY absente (variable d'environnement ou .env)")
    lat0, lon0, radius = cfg["centre"]["lat"], cfg["centre"]["lon"], cfg["rayon_km"]
    fields = "uuid,label,type,takesPlaceAt,isLocatedAt,hasBeenCreatedBy,lastUpdateDatatourisme"
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


# ---------------------------------------------------------------- source 4 : horaires des bibliothèques
_LIB_DAY_LINE = re.compile(r"^([A-ZÀ-Ü][\w& à-ü-]*?)\s*:\s*(.+)$")


def parse_library_hours(page):
    """Cherche un bloc « Horaires » suivi de lignes « Jour(s) : plage(s) »."""
    tp = _TextLines()
    tp.feed(page)
    lines = [re.sub(r"\s+", " ", l).strip() for l in tp.lines]
    idx = [i for i, l in enumerate(lines) if l]
    horaires = []
    for pos, i in enumerate(idx):
        if norm(lines[i]) != "horaires":
            continue
        for j in idx[pos + 1:]:
            m = _LIB_DAY_LINE.match(lines[j])
            if not m:
                break
            horaires.append((m.group(1).strip(), m.group(2).strip()))
        break
    return horaires


def fetch_library_hours(cfg):
    out = []
    for lib in cfg.get("bibliotheques", []):
        try:
            horaires = parse_library_hours(http_get(lib["url"]))
        except Exception as e:
            print(f"  Bibliothèque « {lib.get('nom')} » ignorée : {e}", file=sys.stderr)
            continue
        if horaires:
            out.append({"nom": lib["nom"], "url": lib["url"], "horaires": horaires})
    return out


# ---------------------------------------------------------------- fusion
def mark_long_running(events):
    """Une occurrence unique mais très longue (expo, saison) s'affiche comme les
    événements collapsés à la source : une seule ligne « jusqu'au ... »."""
    for e in events:
        if not e.long_running and (e.end - e.start) > timedelta(days=2):
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


def wednesday_window(now):
    """Le prochain mercredi (aujourd'hui inclus si on est déjà mercredi), journée entière."""
    days_ahead = (2 - now.weekday()) % 7
    target = now + timedelta(days=days_ahead)
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = target.replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


# ---------------------------------------------------------------- sorties
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
        uid = hashlib.sha1(f"{norm(e.title)}{e.start.date()}".encode()).hexdigest()[:20] + "@agenda-local"
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
        return f"jusqu'au {JOURS[en.weekday()]} {en:%d/%m}"
    if e.all_day:
        return f"{JOURS[s.weekday()]} {s:%d/%m} · horaire non précisé"
    return f"{JOURS[s.weekday()]} {s:%d/%m} {s:%H:%M}"


def _card(e):
    title = html.escape(e.title)
    if e.url:
        title = f'<a href="{html.escape(e.url)}">{title}</a>'
    meta = [x for x in (html.escape(e.place), f"{e.distance:.0f} km" if e.distance is not None else "") if x]
    attr_lines = "".join(
        f'<div class="a">Source : {html.escape(producer)}, mis à jour le {html.escape(updated)}</div>'
        for _, producer, updated in e.attributions
        if producer
    )
    cat = html.escape(e.category or "Autre")
    return (
        f'<li data-cat="{cat}"><div class="when">{_fmt_when(e)}</div><div class="t">{title}</div>'
        f'<div class="m">{" · ".join(meta)}</div><div class="s">{html.escape(", ".join(e.sources))}</div>{attr_lines}</li>'
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


def _library_card(lib):
    lines = "".join(f"<div>{html.escape(d)} : {html.escape(h)}</div>" for d, h in lib["horaires"])
    name = html.escape(lib["nom"])
    if lib.get("url"):
        name = f'<a href="{html.escape(lib["url"])}">{name}</a>'
    return f'<li><div class="t">{name}</div><div class="m">{lines}</div></li>'


def write_html(events, path, cfg, now, libraries=()):
    wk_start, wk_end = weekend_window(now)
    wed_start, wed_end = wednesday_window(now)
    next_wk_start, next_wk_end = next_weekend_window(now)
    windows = sorted(
        [("Ce week-end", wk_start, wk_end), ("Mercredi", wed_start, wed_end), ("Week-end suivant", next_wk_start, next_wk_end)],
        key=lambda w: w[1],
    )
    buckets = _bucketize(events, windows)

    label = html.escape(cfg["centre"].get("nom", ""))
    categories = sorted({e.category or "Autre" for e in events} & set(CATEGORIES), key=CATEGORIES.index)
    filter_bar = "".join(
        f'<button class="catf" data-cat="{html.escape(c)}">{html.escape(c)}</button>' for c in categories
    )
    filters_html = (
        f'<div class="filters"><button class="catf active" data-cat="__all__">Toutes</button>{filter_bar}</div>'
        if len(categories) > 1
        else ""
    )

    section_defs = list(windows)
    if buckets["À venir"]:
        section_defs.append(("À venir", None, None))
    if libraries:
        section_defs.append(("Bibliothèques", None, None))

    nav = "".join(f'<a href="#{_slug(lbl)}">{html.escape(lbl)}</a>' for lbl, _, _ in section_defs)
    nav_html = f'<nav class="jump">{nav}</nav>' if len(section_defs) > 1 else ""

    body = "".join(_section(label_, buckets[label_]) for label_, _, _ in windows)
    if buckets["À venir"]:
        body += _section("À venir", buckets["À venir"])

    if libraries:
        body += (
            '<section><h2 id="bibliotheques">Bibliothèques</h2><ul>'
            + "".join(_library_card(lib) for lib in libraries)
            + "</ul></section>"
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
.when{{font-weight:600;color:#0a5}} .t{{margin:2px 0}} .m,.s{{font-size:13px;color:#666}} .a{{font-size:12px;color:#888;margin-top:2px}} a{{color:#0b57d0;text-decoration:none}}
nav.jump{{position:sticky;top:0;background:#fafafa;display:flex;flex-wrap:wrap;gap:6px;padding:8px 0;margin-bottom:4px;z-index:1}}
nav.jump a{{font-size:13px;background:#eee;border-radius:12px;padding:4px 10px;color:#1a1a1a}}
.filters{{display:flex;flex-wrap:wrap;gap:6px;font-size:13px;margin:4px 0 8px}}
.filters button{{border:1px solid #ccc;background:#fff;border-radius:12px;padding:4px 10px;font:inherit;color:inherit;cursor:pointer}}
.filters button.active{{background:#0a5;border-color:#0a5;color:#fff}}
li.hidden{{display:none}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}li{{background:#1c1c1c;border-color:#333}}.m,.s,.a{{color:#aaa}}a{{color:#8ab4f8}}nav.jump{{background:#111}}nav.jump a{{background:#262626;color:#eee}}.filters button{{background:#1c1c1c;border-color:#444;color:#eee}}}}
</style></head><body><h1>Sorties autour de {label} ({cfg['rayon_km']} km)</h1>
{nav_html}
{filters_html}
{body}{footer}
<script>
var buttons = document.querySelectorAll('.catf');
var current = '__all__';
buttons.forEach(function(b){{
  b.addEventListener('click', function(){{
    current = (current === b.dataset.cat) ? '__all__' : b.dataset.cat;
    buttons.forEach(function(x){{ x.classList.toggle('active', x.dataset.cat === current); }});
    document.querySelectorAll('li[data-cat]').forEach(function(li){{
      li.classList.toggle('hidden', current !== '__all__' && li.dataset.cat !== current);
    }});
  }});
}});
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

    libraries = fetch_library_hours(cfg)
    print(f"  Bibliothèques : {len(libraries)}")

    events = mark_long_running(apply_distance(dedupe(events), cfg))
    out = Path(cfg.get("dossier_sortie", "sortie"))
    out.mkdir(exist_ok=True)
    write_ics(events, out / "agenda.ics", now)
    write_html(events, out / "index.html", cfg, now, libraries)
    print(f"{len(events)} événements -> {out}/agenda.ics et {out}/index.html")


if __name__ == "__main__":
    main()
