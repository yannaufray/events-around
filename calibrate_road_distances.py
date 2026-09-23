"""Calibre distances_route.json : distance routière réelle (OSRM) entre Montignac et
chaque commune des départements alentour, pour les communes à moins de ~47 km à vol
d'oiseau (marge au-delà du rayon_km=45 d'alors, gardée pour ne pas recouper trop juste).

Pourquoi ce fichier existe : apply_distance() (dans agenda_local.py) filtrait et affichait
jusqu'ici une distance à vol d'oiseau (haversine), alors que l'usage réel de l'app compare
des distances routières (ex. Montignac -> Les Eyzies fait ~15 km à vol d'oiseau mais ~22,5 km
par la route ; Montignac -> Brive ~29 km à vol d'oiseau mais ~36,4 km par la route). Les
paliers de rayon (config.json["paliers_rayon"]) sont censés représenter Sarlat/Brive/
Périgueux par la route, pas à vol d'oiseau — d'où l'écart observé en usage réel.

OSRM (service de démo gratuit) n'est pas toujours fiable dans cette zone vallonnée : il a
par exemple choisi, pour Brive et Périgueux, un détour via une portion d'autoroute gratuite
plutôt que le trajet direct, gonflant la distance de ~10 km par rapport aux valeurs réelles.
Les distances vérifiées à la main par l'utilisateur (champ "source" du JSON commençant par
"manuel") corrigent ces cas et sont préservées d'un recalcul à l'autre par main() ci-dessous.

Script ponctuel, PAS appelé par le pipeline quotidien (agenda_local.py reste stdlib-only,
sans dépendance réseau au-delà de ses sources d'événements habituelles) : à relancer
seulement si le centre (config.json["centre"]) change, ou pour élargir la zone couverte.
Utilise deux API publiques sans clé :
  - geo.api.gouv.fr pour la liste des communes (nom, code INSEE, centre) par département
  - router.project-osrm.org (service de démonstration OSRM) pour la distance routière,
    via l'API "table" (une requête par lot de ~90 communes plutôt qu'une par commune,
    pour rester raisonnable vis-à-vis d'un service public gratuit).

Usage : python3 calibrate_road_distances.py
"""

import json
import time
from pathlib import Path
from urllib import parse, request

from agenda_local import haversine

CONFIG_PATH = "config.json"
OUTPUT_PATH = "distances_route.json"
# Dordogne, Corrèze, Lot, Lot-et-Garonne, Haute-Vienne : départements qui bordent le
# rayon de recherche autour de Montignac (Dordogne).
DEPARTEMENTS = ["24", "19", "46", "47", "87"]
MARGE_KM = 47  # rayon_km=45 + marge, en vol d'oiseau (borne large, filtrée après coup)
CHUNK = 90


def communes_alentour(centre_lat, centre_lon):
    candidats = []
    for dep in DEPARTEMENTS:
        url = f"https://geo.api.gouv.fr/departements/{dep}/communes?" + parse.urlencode(
            {"fields": "nom,code,centre"}
        )
        with request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
        candidats += data
        time.sleep(0.2)
    within = []
    for c in candidats:
        centre = c.get("centre")
        if not centre:
            continue
        lon, lat = centre["coordinates"]
        d = haversine(centre_lat, centre_lon, lat, lon)
        if d <= MARGE_KM:
            within.append({"nom": c["nom"], "code": c["code"], "lat": lat, "lon": lon, "vol_oiseau_km": round(d, 2)})
    return within


def distances_routieres(centre_lat, centre_lon, communes):
    results = {}
    for i in range(0, len(communes), CHUNK):
        batch = communes[i : i + CHUNK]
        pts = [(centre_lon, centre_lat)] + [(c["lon"], c["lat"]) for c in batch]
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in pts)
        dest = ";".join(str(j) for j in range(1, len(pts)))
        url = (
            f"https://router.project-osrm.org/table/v1/driving/{coords}"
            f"?sources=0&destinations={dest}&annotations=distance"
        )
        req = request.Request(url, headers={"User-Agent": "agenda-local-calibration/1.0"})
        with request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        for c, dm in zip(batch, data["distances"][0]):
            results[c["code"]] = {
                "nom": c["nom"],
                "lat": c["lat"],
                "lon": c["lon"],
                "vol_oiseau_km": c["vol_oiseau_km"],
                "route_km": round(dm / 1000, 2) if dm is not None else None,
            }
        print(f"  {i + len(batch)}/{len(communes)} communes traitées")
        time.sleep(1)
    return results


def main():
    # OSRM (service de démo gratuit) choisit parfois un itinéraire plus long qu'en
    # pratique (ex. détour par une portion d'autoroute gratuite plutôt que la route
    # directe) : Brive et Périgueux calculés par OSRM étaient sous-estimés de ~10 km
    # par rapport aux valeurs réelles données par l'utilisateur. Les entrées avec
    # source "manuel (...)" sont donc des corrections de confiance à préserver : un
    # nouveau calcul OSRM ne doit jamais les écraser.
    existants = {}
    if Path(OUTPUT_PATH).exists():
        existants = json.loads(Path(OUTPUT_PATH).read_text(encoding="utf-8"))
    manuels = {code: v for code, v in existants.items() if str(v.get("source", "")).startswith("manuel")}

    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    lat0, lon0 = cfg["centre"]["lat"], cfg["centre"]["lon"]
    print("Recherche des communes alentour…")
    communes = communes_alentour(lat0, lon0)
    print(f"{len(communes)} communes candidates, calcul des distances routières (OSRM)…")
    results = distances_routieres(lat0, lon0, communes)
    results.update(manuels)
    manquants = [v["nom"] for v in results.values() if v["route_km"] is None]
    if manquants:
        print(f"Attention, pas de route trouvée pour : {', '.join(manquants)}")
    if manuels:
        print(f"{len(manuels)} distances manuelles préservées : {', '.join(v['nom'] for v in manuels.values())}")
    json.dump(results, open(OUTPUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"-> {OUTPUT_PATH} ({len(results)} communes)")


if __name__ == "__main__":
    main()
