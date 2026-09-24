"""Tests hors-réseau pour agenda_local.py : fusion, rayon, .ics, parseurs.

Les parseurs HTML/JSON sont testés sur des données enregistrées dans
tests/fixtures/ (capturées une fois sur le vrai réseau), jamais en live.
"""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agenda_local as al  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures")
TZ = al.TZ


def dt(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


class TestDistance(unittest.TestCase):
    def test_haversine_montignac_sarlat(self):
        # distance réelle Montignac <-> Sarlat ~ 20-22 km
        d = al.haversine(45.0667, 1.1667, 44.8903, 1.2167)
        self.assertGreater(d, 15)
        self.assertLess(d, 30)

    def test_apply_distance_filters_outside_radius(self):
        cfg = {"centre": {"lat": 45.0667, "lon": 1.1667}, "rayon_km": 20}
        near = al.Event("Proche", dt(2026, 1, 1), dt(2026, 1, 1, 2), lat=45.07, lon=1.17)
        far = al.Event("Loin", dt(2026, 1, 1), dt(2026, 1, 1, 2), lat=48.85, lon=2.35)  # Paris
        no_coords = al.Event("Sans coordonnées", dt(2026, 1, 1), dt(2026, 1, 1, 2))
        out = al.apply_distance([near, far, no_coords], cfg)
        titles = {e.title for e in out}
        self.assertIn("Proche", titles)
        self.assertIn("Sans coordonnées", titles)  # gardé faute de coordonnées
        self.assertNotIn("Loin", titles)


class TestDedupe(unittest.TestCase):
    def test_merges_similar_titles_same_day(self):
        e1 = al.Event("Concert de jazz au Vox", dt(2026, 6, 1, 20), dt(2026, 6, 1, 22),
                       url="https://a.example/x", sources=["OpenAgenda"])
        e2 = al.Event("Concert de Jazz au Vox !", dt(2026, 6, 1, 20), dt(2026, 6, 1, 22),
                       sources=["Mairie de Montignac"], attributions=[("DATAtourisme", "OT Test", "01/01/2026")])
        out = al.dedupe([e1, e2])
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0].sources), {"OpenAgenda", "Mairie de Montignac"})
        self.assertEqual(out[0].url, "https://a.example/x")
        self.assertEqual(out[0].attributions, [("DATAtourisme", "OT Test", "01/01/2026")])

    def test_keeps_events_on_different_days(self):
        e1 = al.Event("Vide-grenier", dt(2026, 6, 1), dt(2026, 6, 1, 18))
        e2 = al.Event("Vide-grenier", dt(2026, 6, 8), dt(2026, 6, 8, 18))
        out = al.dedupe([e1, e2])
        self.assertEqual(len(out), 2)

    def test_keeps_unrelated_titles_same_day(self):
        e1 = al.Event("Marché nocturne", dt(2026, 6, 1, 18), dt(2026, 6, 1, 22))
        e2 = al.Event("Tournoi de pétanque", dt(2026, 6, 1, 18), dt(2026, 6, 1, 22))
        out = al.dedupe([e1, e2])
        self.assertEqual(len(out), 2)


class TestLongRunning(unittest.TestCase):
    def test_marks_multi_day_events(self):
        short = al.Event("Concert", dt(2026, 6, 1, 20), dt(2026, 6, 1, 22))
        expo = al.Event("Exposition", dt(2026, 1, 1), dt(2026, 12, 31))
        out = al.mark_long_running([short, expo])
        self.assertFalse(out[0].long_running)
        self.assertTrue(out[1].long_running)

    def test_marks_weekend_event_spanning_two_days(self):
        # vide-grenier samedi 9h -> dimanche 18h : 33h, chevauche deux dates mais
        # reste bien en-dessous de l'ancien seuil de 2 jours pleins.
        vide_grenier = al.Event("Vide-grenier", dt(2026, 6, 6, 9), dt(2026, 6, 7, 18))
        out = al.mark_long_running([vide_grenier])
        self.assertTrue(out[0].long_running)

    def test_keeps_short_overnight_event_as_single_date(self):
        # concert finissant après minuit : chevauche deux dates mais dure quelques
        # heures à peine, ne doit pas passer en « jusqu'au ... ».
        concert = al.Event("Concert nocturne", dt(2026, 6, 6, 22), dt(2026, 6, 7, 1))
        out = al.mark_long_running([concert])
        self.assertFalse(out[0].long_running)


class TestFmtWhen(unittest.TestCase):
    def test_short_multi_day_event_shows_both_dates(self):
        # vide-grenier samedi + dimanche : ne doit pas donner l'impression d'un
        # événement déjà en cours depuis longtemps.
        e = al.Event("Vide-grenier", dt(2026, 6, 6, 9), dt(2026, 6, 7, 18))
        al.mark_long_running([e])
        self.assertEqual(al._fmt_when(e), "du samedi 06/06 au dimanche 07/06")

    def test_long_running_event_shows_end_date_only(self):
        e = al.Event("Exposition", dt(2026, 1, 1), dt(2026, 12, 31))
        al.mark_long_running([e])
        self.assertEqual(al._fmt_when(e), "jusqu'au jeudi 31/12")


class TestIcs(unittest.TestCase):
    def test_write_and_reparse_roundtrip(self, tmp_path=None):
        import tempfile
        events = [
            al.Event("Café littéraire", dt(2026, 6, 1, 10, 30), dt(2026, 6, 1, 12),
                     place="Bibliothèque, Montignac", url="https://example.com/e",
                     sources=["Mairie de Montignac"], distance=1.2,
                     attributions=[("DATAtourisme", "OT Lascaux Dordogne Vallée Vézère", "09/07/2026")]),
            al.Event("Foire d'antan", dt(2026, 6, 6), dt(2026, 6, 8), all_day=True,
                     sources=["OpenAgenda"]),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "agenda.ics"
            al.write_ics(events, path, dt(2026, 5, 1))
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()

        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.rstrip("\r\n").endswith("END:VCALENDAR"))
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART:20260601T083000Z", text)  # 10:30 Europe/Paris (CEST, UTC+2) -> 08:30 UTC
        self.assertIn("DTSTART;VALUE=DATE:20260606", text)
        self.assertIn("mis à jour le 09/07/2026", text)
        # aucune ligne de contenu ne doit dépasser 75 octets (pliage RFC 5545)
        for line in text.split("\r\n"):
            if line.startswith(" "):
                continue
            self.assertLessEqual(len(line.encode("utf-8")), 75)

        reparsed = al.parse_ics(text, "roundtrip", dt(2026, 1, 1), dt(2027, 1, 1))
        titles = {e.title for e in reparsed}
        self.assertEqual(titles, {"Café littéraire", "Foire d'antan"})


class TestParseIcsFeed(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "sample_feed.ics").read_text(encoding="utf-8")

    def test_parses_timed_and_allday_excludes_recurring(self):
        events = al.parse_ics(self.text, "Flux test", dt(2099, 1, 1), dt(2099, 12, 31))
        titles = {e.title for e in events}
        self.assertIn("Concert de test", titles)
        self.assertIn("Marché de producteurs", titles)
        self.assertNotIn("Cours de yoga", titles)  # RRULE : non géré, exclu explicitement

        concert = next(e for e in events if e.title == "Concert de test")
        self.assertEqual(concert.start, dt(2099, 6, 15, 19, 0))
        self.assertEqual(concert.end, dt(2099, 6, 15, 22, 0))
        self.assertFalse(concert.all_day)
        self.assertEqual(concert.place, "Salle des fêtes, Montignac")
        self.assertEqual(concert.url, "https://example.com/concert")
        self.assertAlmostEqual(concert.lat, 45.0667, places=3)

        marche = next(e for e in events if e.title == "Marché de producteurs")
        self.assertTrue(marche.all_day)
        self.assertEqual(marche.start.date(), dt(2099, 6, 20).date())

    def test_window_filters_out_of_range(self):
        events = al.parse_ics(self.text, "Flux test", dt(2050, 1, 1), dt(2050, 12, 31))
        self.assertEqual(events, [])


class TestParseMontignacHtml(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "montignac_page.html").read_text(encoding="utf-8")

    def test_extracts_plausible_events(self):
        events = al.parse_montignac_html(self.page, "Mairie de Montignac")
        self.assertGreater(len(events), 0)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            if e.url:
                self.assertTrue(e.url.startswith("https://ville-montignac.com/agenda/"))


class TestParsePolePrehistoireHtml(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "pole_prehistoire_evenements.html").read_text(encoding="utf-8")
        self.base_url = "https://www.pole-prehistoire.com/index.php/fr/actualites-fr/evenements"

    def test_extracts_plausible_events(self):
        events = al.parse_pole_prehistoire_html(self.page, self.base_url, "Pôle international de la Préhistoire")
        self.assertGreater(len(events), 0)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            self.assertTrue(e.url.startswith("https://www.pole-prehistoire.com/fr/actualites-fr/evenements/"))

    def test_time_and_all_day_parsed(self):
        events = al.parse_pole_prehistoire_html(self.page, self.base_url, "Pôle international de la Préhistoire")
        concert = next(e for e in events if "Antropoceno" in e.title)
        self.assertFalse(concert.all_day)
        self.assertEqual((concert.start.month, concert.start.day), (9, 25))
        self.assertEqual((concert.start.hour, concert.start.minute), (20, 30))

    def test_missing_year_is_inferred(self):
        events = al.parse_pole_prehistoire_html(self.page, self.base_url, "Pôle international de la Préhistoire")
        jep = next(e for e in events if "patrimoine" in e.title.lower())
        self.assertTrue(jep.all_day)
        self.assertEqual((jep.start.month, jep.start.day), (9, 20))


class TestParseBriveTourismeHtml(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "brive_tourisme_agenda.html").read_text(encoding="utf-8")
        self.base_url = "https://www.brive-tourisme.com/fr/agenda/complet/"

    def test_extracts_plausible_events(self):
        events = al.parse_brivetourisme_html(self.page, self.base_url)
        self.assertEqual(len(events), 10)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            self.assertTrue(e.url.startswith("https://www.brive-tourisme.com/fr/fiche/"))
            self.assertIsNone(e.lat)  # pas de coordonnées -> jamais filtré par distance

    def test_single_date_parsed(self):
        events = al.parse_brivetourisme_html(self.page, self.base_url)
        e = next(e for e in events if "Tours de Merle" in e.title)
        self.assertEqual((e.start.year, e.start.month, e.start.day), (2026, 9, 21))
        self.assertTrue(e.all_day)
        self.assertEqual(e.place, "Saint-Geniez-O-Merle")

    def test_date_range_parsed(self):
        events = al.parse_brivetourisme_html(self.page, self.base_url)
        e = next(e for e in events if "Retour de Cannes" in e.title)
        self.assertEqual((e.start.year, e.start.month, e.start.day), (2026, 9, 11))
        self.assertEqual((e.end.year, e.end.month, e.end.day), (2026, 9, 27))
        self.assertEqual(e.place, "Brive-La-Gaillarde")

    def test_recurring_event_kept_as_separate_occurrences(self):
        # le widget éclate déjà les événements récurrents en une ligne par date
        events = al.parse_brivetourisme_html(self.page, self.base_url)
        tresors = [e for e in events if "Trésors d'archives" in e.title]
        self.assertEqual({e.start.day for e in tresors}, {21, 22})


class TestParsePerigueuxRss(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "perigueux_rss.xml").read_text(encoding="utf-8")

    def test_extracts_plausible_events(self):
        events = al.parse_perigueux_rss(self.page, "Ville de Périgueux")
        self.assertGreater(len(events), 100)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            self.assertIsNone(e.lat)  # pas de coordonnées par item -> approximées en aval

    def test_single_date_parsed(self):
        events = al.parse_perigueux_rss(self.page, "Ville de Périgueux")
        e = next(ev for ev in events if "RABBIT HOLE" in ev.title)
        self.assertEqual((e.start.year, e.start.month, e.start.day), (2027, 5, 11))
        self.assertEqual((e.start.hour, e.start.minute), (20, 0))
        self.assertEqual(e.url, "https://www.odyssee-perigueux.fr/spectacles-de-la-saison/rabbit-hole/")

    def test_start_and_end_parsed(self):
        events = al.parse_perigueux_rss(self.page, "Ville de Périgueux")
        e = next(ev for ev in events if "VIDE GRENIER" in ev.title)
        self.assertEqual((e.start.hour, e.start.minute), (9, 0))
        self.assertEqual((e.end.hour, e.end.minute), (17, 0))

    def test_recurring_series_keeps_only_first_occurrence(self):
        # le flux tasse parfois plusieurs dates dans un même <ev:startdate>/<ev:enddate>
        # (bug d'export du site) -> on ne garde que la première, pas de date inventée
        events = al.parse_perigueux_rss(self.page, "Ville de Périgueux")
        e = next(ev for ev in events if "gymnastique" in ev.title)
        self.assertEqual((e.start.year, e.start.month, e.start.day), (2026, 10, 4))


class TestParseLeberouSubpage(unittest.TestCase):
    def test_extracts_date_place_time(self):
        page = (FIXTURES / "leberou_conteur_page.html").read_text(encoding="utf-8")
        e = al.parse_leberou_subpage(page, 2026, "Festival Le Lébérou (contes)")
        self.assertIsNotNone(e)
        self.assertEqual(e.title, "Nadia Roz")
        self.assertEqual((e.start.month, e.start.day), (10, 31))
        self.assertFalse(e.all_day)
        self.assertEqual((e.start.hour, e.start.minute), (21, 0))
        self.assertIn("Montignac", e.place)
        self.assertEqual(e.category, "Culture & spectacles")
        self.assertEqual(e.sources, ["Festival Le Lébérou (contes)"])

    def test_no_explicit_time_is_all_day(self):
        page = (FIXTURES / "leberou_bertoo_page.html").read_text(encoding="utf-8")
        e = al.parse_leberou_subpage(page, 2026, "Festival Le Lébérou (contes)")
        self.assertIsNotNone(e)
        self.assertEqual(e.title, "Bertoo")
        self.assertEqual((e.start.month, e.start.day), (11, 7))
        self.assertTrue(e.all_day)
        self.assertIn("La Chapelle Aubareil", e.place)

    def test_date_at_end_of_line_still_parsed(self):
        # cas particulier : sur cette page la date est en fin de ligne, pas en début
        page = (FIXTURES / "leberou_randonnee_page.html").read_text(encoding="utf-8")
        e = al.parse_leberou_subpage(page, 2026, "Festival Le Lébérou (contes)")
        self.assertIsNotNone(e)
        self.assertEqual((e.start.month, e.start.day), (10, 4))
        self.assertFalse(e.all_day)
        self.assertEqual((e.start.hour, e.start.minute), (8, 30))


class TestDatatourisme(unittest.TestCase):
    def setUp(self):
        self.objects = json.loads((FIXTURES / "datatourisme_objects.json").read_text(encoding="utf-8"))

    def test_extracts_occurrences_with_attribution(self):
        wide_start, wide_end = dt(2000, 1, 1), dt(2100, 1, 1)
        total = []
        for obj in self.objects:
            total += al._datatourisme_object_events(obj, wide_start, wide_end)
        self.assertGreater(len(total), 0)
        for e in total:
            self.assertTrue(e.title)
            self.assertLessEqual(e.start, e.end)
            self.assertEqual(e.sources, ["DATAtourisme"])
            self.assertEqual(len(e.attributions), 1)
            source, producer, updated = e.attributions[0]
            self.assertEqual(source, "DATAtourisme")
            self.assertTrue(producer)  # legalName toujours présent sur ces fixtures
            # e.url n'est renseignée que si contact.homepage pointe vers une page
            # dédiée à l'évènement (jamais la fiche technique DATAtourisme brute,
            # jamais l'accueil générique de l'office de tourisme) ; sinon vide, et
            # l'UI proposera une recherche Google à la place
            if e.url:
                self.assertTrue(e.url.startswith("http"))
                self.assertNotIn("data.datatourisme.fr", e.url)

    def test_no_contact_homepage_leaves_url_empty(self):
        # aucune des 3 fixtures n'a de contact.homepage : url doit rester vide plutôt
        # que de retomber sur la fiche technique ou l'accueil de l'office de tourisme
        for obj in self.objects:
            for e in al._datatourisme_object_events(obj, dt(2000, 1, 1), dt(2100, 1, 1)):
                self.assertEqual(e.url, "")

    def test_contact_homepage_used_when_present(self):
        obj = dict(self.objects[0])
        obj["hasContact"] = [{"homepage": ["https://www.sarlat-centreculturel.fr/evenement/the-wackids"]}]
        out = al._datatourisme_object_events(obj, dt(2000, 1, 1), dt(2100, 1, 1))
        self.assertTrue(out)
        for e in out:
            self.assertEqual(e.url, "https://www.sarlat-centreculturel.fr/evenement/the-wackids")

    def test_empty_window_yields_no_events(self):
        out = al._datatourisme_object_events(self.objects[0], dt(1900, 1, 1), dt(1901, 1, 1))
        self.assertEqual(out, [])


class TestDatatourismeSitePages(unittest.TestCase):
    """Résolution d'une page dédiée à l'évènement via le sitemap du site
    producteur (cf. conversation : lascaux-dordogne.com, sarlat-tourisme.com,
    vezere-perigord.fr exposent chacun une page par évènement, retrouvable par
    slug du titre dans leur sitemap — jamais par une fiche DATAtourisme brute ou
    l'accueil générique de l'office de tourisme)."""

    def test_slugify_matches_real_site_pattern(self):
        # cas réel : « Nature Sauvage » : Exposition d'estampes d'art ->
        # nature-sauvage-exposition-destampes-dart (vérifié sur lascaux-dordogne.com
        # ET sarlat-tourisme.com, qui publient tous deux ce même évènement)
        self.assertEqual(
            al._slugify("« Nature Sauvage » : Exposition d'estampes d'art"),
            "nature-sauvage-exposition-destampes-dart",
        )

    def setUp(self):
        self._orig_fetch = al._fetch_sitemap_urls
        self.addCleanup(setattr, al, "_fetch_sitemap_urls", self._orig_fetch)
        self.addCleanup(al._DT_SITE_SITEMAPS.pop, "Test OT", None)
        self.addCleanup(al._agenda_slug_index.cache_clear)
        al._DT_SITE_SITEMAPS["Test OT"] = ("https://example.org/agenda-sitemap.xml", "/agenda/")

    def _mock_sitemap(self, urls):
        al._agenda_slug_index.cache_clear()
        al._fetch_sitemap_urls = lambda url, depth=0: list(urls)

    def test_resolve_event_page_exact_slug(self):
        self._mock_sitemap(["https://example.org/agenda/nature-sauvage-exposition-destampes-dart/"])
        got = al._resolve_event_page("Test OT", "« Nature Sauvage » : Exposition d'estampes d'art")
        self.assertEqual(got, "https://example.org/agenda/nature-sauvage-exposition-destampes-dart/")

    def test_resolve_event_page_prefix_with_id_suffix(self):
        # cas réel sarlat-tourisme.com : le slug est suivi de -ville-fr-<id>
        self._mock_sitemap(
            ["https://example.org/agenda/nature-sauvage-exposition-destampes-dart-sarlat-la-caneda-fr-5698215/"]
        )
        got = al._resolve_event_page("Test OT", "« Nature Sauvage » : Exposition d'estampes d'art")
        self.assertEqual(
            got,
            "https://example.org/agenda/nature-sauvage-exposition-destampes-dart-sarlat-la-caneda-fr-5698215/",
        )

    def test_resolve_event_page_unknown_producer_returns_empty(self):
        self.assertEqual(al._resolve_event_page("Un OT quelconque", "Peu importe"), "")


class TestCategories(unittest.TestCase):
    def test_datatourisme_generic_sport_type_overridden_by_title(self):
        # cas réel observé : DATAtourisme étiquette les Journées du patrimoine
        # avec le type générique "SportsEvent" faute de type plus précis.
        types = ["EntertainmentAndEvent", "CulturalEvent", "PointOfInterest", "Event", "SportsEvent"]
        cat = al.datatourisme_category(types, "Journées européennes du Patrimoine au Château de Losse")
        self.assertEqual(cat, "Patrimoine & visites")

    def test_datatourisme_real_sport_stays_sport(self):
        types = ["EntertainmentAndEvent", "SportsEvent", "SportsCompetition"]
        cat = al.datatourisme_category(types, "Grande course cycliste UFOLEP")
        self.assertEqual(cat, "Sport")

    def test_datatourisme_specific_type_wins_when_no_title_hint(self):
        cat = al.datatourisme_category(["Concert", "CulturalEvent"], "Soirée acoustique au jardin")
        self.assertEqual(cat, "Culture & spectacles")

    def test_datatourisme_market_title_wins_over_concert_type(self):
        # cas réel : DATAtourisme porte à la fois GarageSale/SaleEvent (marché) et
        # Concert/MusicEvent (il y a de la musique en plus) sur "Vides grenier au
        # Domaine du Sablou" — le titre doit trancher, pas l'ordre des types.
        types = ["SocialEvent", "GarageSale", "MusicEvent", "TraditionalCelebration", "SaleEvent", "Concert", "CulturalEvent"]
        cat = al.datatourisme_category(types, "Vides grenier au Domaine du Sablou")
        self.assertEqual(cat, "Marchés & fêtes")

    def test_datatourisme_conference_falls_back_to_culture(self):
        # cas réel : DATAtourisme ne porte aucun type précis pour une conférence
        types = ["EntertainmentAndEvent", "CulturalEvent", "PointOfInterest", "Event"]
        cat = al.datatourisme_category(types, "Conférence - Quoi de nouveau depuis Guernica ?")
        self.assertEqual(cat, "Culture & spectacles")

    def test_datatourisme_nature_outing_not_sport(self):
        # cas réel observé : DATAtourisme colle le type générique "SportsEvent" à
        # des sorties nature sans rapport avec le sport (observation du brame du
        # cerf, week-end découverte...) : elles vont dans "Nature & randonnées",
        # pas dans "Sport" ni dans le fourre-tout "Autre".
        types = ["EntertainmentAndEvent", "SportsEvent", "CulturalEvent"]
        cat = al.datatourisme_category(types, "Soirée brame du cerf aux Eyzies")
        self.assertEqual(cat, "Nature & randonnées")

    def test_datatourisme_nature_weekend_not_sport(self):
        # le mot "nature" seul n'est volontairement pas un déclencheur (trop
        # ambigu : cf. "nature morte" en arts plastiques) — sans mot-clé plus
        # spécifique (brame, randonnée, balade nature...), l'évènement tombe dans
        # "Autre" plutôt que d'être classé à tort en "Sport" ou en "Nature".
        types = ["EntertainmentAndEvent", "Rambling", "PointOfInterest"]
        cat = al.datatourisme_category(types, "Week-end nature, saveurs et détente")
        self.assertEqual(cat, "Autre")

    def test_datatourisme_generic_type_without_keyword_not_nature(self):
        # cas réel observé : un stage de dessin porte le type générique
        # "SportsEvent" chez DATAtourisme sans aucun rapport avec le sport ni la
        # nature — le simple type ne doit jamais suffire à classer en "Nature &
        # randonnées", il faut un mot-clé explicite dans le titre.
        types = ["EntertainmentAndEvent", "SportsEvent", "CulturalEvent"]
        cat = al.datatourisme_category(types, "Dessiner est un super pouvoir - stage Le dessin de visage")
        self.assertEqual(cat, "Autre")

    def test_datatourisme_prehistoric_site_is_heritage(self):
        types = ["EntertainmentAndEvent", "SportsEvent", "PointOfInterest"]
        cat = al.datatourisme_category(types, "Le mois de septembre sur les Sites préhistoriques de la vallée de la Vézère")
        self.assertEqual(cat, "Patrimoine & visites")

    def test_openagenda_employment_events(self):
        cat = al.openagenda_category("OBJECTIF EMPLOI", [], "Mes événements France Travail")
        self.assertEqual(cat, "Emploi & formation")

    def test_openagenda_falls_back_to_autre(self):
        cat = al.openagenda_category("Réunion du conseil", [], "")
        self.assertEqual(cat, "Autre")

    def test_openagenda_pilates_is_sport(self):
        cat = al.openagenda_category("Cours Pilates sur appareils", [], "")
        self.assertEqual(cat, "Sport")

    def test_openagenda_choreographic_walk_is_culture(self):
        cat = al.openagenda_category("Déambulation chorégraphique", [], "")
        self.assertEqual(cat, "Culture & spectacles")

    def test_openagenda_trail_is_sport(self):
        cat = al.openagenda_category("Trail des châtaigniers", [], "")
        self.assertEqual(cat, "Sport")

    def test_openagenda_nature_outing_not_autre(self):
        cat = al.openagenda_category("Sortie nature : observation des rapaces", [], "")
        self.assertEqual(cat, "Nature & randonnées")

    def test_datatourisme_trail_with_sport_type_is_sport(self):
        cat = al.datatourisme_category(["EntertainmentAndEvent", "SportsEvent"], "Trail nocturne de la Vézère")
        self.assertEqual(cat, "Sport")


class TestWeekendWindow(unittest.TestCase):
    def test_midweek_targets_next_friday_evening_to_sunday(self):
        wednesday = dt(2026, 9, 23, 14, 0)  # un mercredi
        start, end = al.weekend_window(wednesday)
        self.assertEqual(start.weekday(), 4)  # vendredi
        self.assertEqual(start.hour, 17)
        self.assertEqual(end.weekday(), 6)  # dimanche
        self.assertEqual((end.hour, end.minute), (23, 59))

    def test_saturday_starts_now(self):
        saturday = dt(2026, 9, 26, 9, 0)
        start, end = al.weekend_window(saturday)
        self.assertEqual(start, saturday)
        self.assertEqual(end.weekday(), 6)

    def test_next_weekend_is_one_week_after(self):
        wednesday = dt(2026, 9, 23, 14, 0)
        this_start, this_end = al.weekend_window(wednesday)
        next_start, next_end = al.next_weekend_window(wednesday)
        self.assertEqual(next_start, this_start + timedelta(days=7))
        self.assertEqual(next_end, this_end + timedelta(days=7))

    def test_next_weekend_from_within_current_weekend(self):
        saturday = dt(2026, 9, 26, 9, 0)
        next_start, next_end = al.next_weekend_window(saturday)
        self.assertEqual(next_start.weekday(), 4)  # vendredi
        self.assertEqual(next_start.date(), dt(2026, 10, 2).date())


class TestWeekWindow(unittest.TestCase):
    def test_spans_between_the_two_weekends(self):
        monday = dt(2026, 9, 21, 8, 0)
        start, end = al.week_window(monday)
        _, wk_end = al.weekend_window(monday)
        next_start, _ = al.next_weekend_window(monday)
        self.assertEqual(start, wk_end + timedelta(seconds=1))
        self.assertEqual(end, next_start)
        self.assertEqual(start.weekday(), 0)  # lundi

    def test_never_empty_even_from_within_the_weekend(self):
        saturday = dt(2026, 9, 26, 9, 0)
        start, end = al.week_window(saturday)
        self.assertLess(start, end)
        self.assertEqual(start.weekday(), 0)  # lundi


class TestBucketize(unittest.TestCase):
    def test_events_go_to_first_matching_window_in_order(self):
        e_weekend = al.Event("A", dt(2026, 9, 26, 10), dt(2026, 9, 26, 12))
        e_wed = al.Event("B", dt(2026, 9, 30, 10), dt(2026, 9, 30, 12))
        e_far = al.Event("C", dt(2026, 11, 1, 10), dt(2026, 11, 1, 12))
        windows = [
            ("Ce week-end", dt(2026, 9, 25, 17), dt(2026, 9, 27, 23, 59)),
            ("Cette semaine", dt(2026, 9, 30, 0), dt(2026, 9, 30, 23, 59)),
        ]
        buckets = al._bucketize([e_weekend, e_wed, e_far], windows)
        self.assertEqual(buckets["Ce week-end"], [e_weekend])
        self.assertEqual(buckets["Cette semaine"], [e_wed])
        self.assertEqual(buckets["À venir"], [e_far])


class TestTodayOrder(unittest.TestCase):
    def test_dated_events_before_ongoing_expos(self):
        expo_old = al.Event("Expo A", dt(2026, 8, 1, 10), dt(2026, 10, 31, 18), long_running=True)
        expo_last_day = al.Event("Expo B", dt(2026, 9, 1, 10), dt(2026, 9, 24, 18), long_running=True)
        concert = al.Event("Concert", dt(2026, 9, 24, 21), dt(2026, 9, 24, 23))
        marche = al.Event("Marché", dt(2026, 9, 24, 8), dt(2026, 9, 24, 12))
        self.assertEqual(
            al._today_order([expo_old, concert, expo_last_day, marche]),
            [marche, concert, expo_last_day, expo_old],
        )


class TestParseSarlatMairieHtml(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "sarlat_mairie_agenda.html").read_text(encoding="utf-8")

    def test_extracts_plausible_events(self):
        events = al.parse_sarlat_mairie_html(self.page, "Mairie de Sarlat")
        self.assertGreater(len(events), 50)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            self.assertTrue(e.url.startswith("https://sarlat.fr/agenda/"))

    def test_time_and_place_parsed(self):
        events = al.parse_sarlat_mairie_html(self.page, "Mairie de Sarlat")
        lecture = next(e for e in events if "Délire de lire" in e.title)
        self.assertFalse(lecture.all_day)
        self.assertEqual((lecture.start.month, lecture.start.day), (9, 22))
        self.assertEqual((lecture.start.hour, lecture.start.minute), (17, 0))
        self.assertIn("Médiathèque de Sarlat", lecture.place)

    def test_multiday_range_with_daily_hours(self):
        events = al.parse_sarlat_mairie_html(self.page, "Mairie de Sarlat")
        expo = next(e for e in events if "Malraux" in e.title)
        self.assertEqual((expo.start.month, expo.start.day), (7, 22))
        self.assertEqual((expo.end.month, expo.end.day), (9, 25))


class TestParseSarlatCentreCulturelHtml(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "sarlat_centreculturel_agenda.html").read_text(encoding="utf-8")

    def test_extracts_plausible_events(self):
        events = al.parse_sarlat_centreculturel_html(self.page, "Centre Culturel de Sarlat", "Centre Culturel de Sarlat")
        self.assertGreater(len(events), 0)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            self.assertTrue(e.url.startswith("https://www.sarlat-centreculturel.fr/evenement/"))
            self.assertEqual(e.place, "Centre Culturel de Sarlat")

    def test_time_parsed(self):
        events = al.parse_sarlat_centreculturel_html(self.page, "Centre Culturel de Sarlat", "Centre Culturel de Sarlat")
        sers = next(e for e in events if "Gauvain" in e.title)
        self.assertFalse(sers.all_day)
        self.assertEqual((sers.start.month, sers.start.day), (9, 26))
        self.assertEqual((sers.start.hour, sers.start.minute), (20, 30))


class TestParseVezerePerigordHtml(unittest.TestCase):
    def setUp(self):
        self.page = (FIXTURES / "vezere_perigord_agenda.html").read_text(encoding="utf-8")

    def test_extracts_plausible_events(self):
        events = al.parse_vezere_perigord_html(self.page, "Office de Tourisme Vézère Périgord Noir")
        self.assertGreater(len(events), 0)
        for e in events:
            self.assertTrue(e.title.strip())
            self.assertIsInstance(e.start, datetime)
            self.assertIsNotNone(e.start.tzinfo)
            self.assertLessEqual(e.start, e.end)
            self.assertIsNotNone(e.lat)
            self.assertIsNotNone(e.lon)

    def test_multiple_dates_become_separate_occurrences(self):
        events = al.parse_vezere_perigord_html(self.page, "Office de Tourisme Vézère Périgord Noir")
        cine = [e for e in events if "affaire turque" in e.title]
        self.assertEqual({(e.start.month, e.start.day) for e in cine}, {(10, 2), (10, 3)})
        self.assertEqual(cine[0].place, "Terrasson-Lavilledieu")
        self.assertEqual(cine[0].category, "Culture & spectacles")

    def test_many_occurrences_collapsed_to_long_running(self):
        # plus de 7 dates (ex. animation quotidienne) -> une seule ligne, comme DATAtourisme
        events = al.parse_vezere_perigord_html(self.page, "Office de Tourisme Vézère Périgord Noir")
        escape_games = [e for e in events if "Escape Game" in e.title]
        self.assertEqual(len(escape_games), 1)
        e = escape_games[0]
        self.assertTrue(e.long_running)
        self.assertEqual((e.start.month, e.start.day), (10, 19))
        self.assertEqual((e.end.month, e.end.day), (10, 30))

    def test_link_used_when_present_else_left_empty(self):
        events = al.parse_vezere_perigord_html(self.page, "Office de Tourisme Vézère Périgord Noir")
        monk = next(e for e in events if "Monk" in e.title)
        self.assertTrue(monk.url.startswith("https://www.vezere-perigord.fr/"))
        dominicirque = next(e for e in events if "Dominicirque" in e.title)
        self.assertEqual(dominicirque.url, "")


class TestLibraryHours(unittest.TestCase):
    def test_parses_real_fixture(self):
        page = (FIXTURES / "bibliotheque_page.html").read_text(encoding="utf-8")
        horaires = al.parse_library_hours(page)
        self.assertGreater(len(horaires), 0)
        days = {d for d, _ in horaires}
        self.assertTrue(any("mercredi" in d.lower() for d in days))
        for day, hours in horaires:
            self.assertTrue(day.strip())
            self.assertTrue(hours.strip())

    def test_no_horaires_block_returns_empty(self):
        self.assertEqual(al.parse_library_hours("<html><body><p>Rien ici</p></body></html>"), [])


class TestCinemaInfo(unittest.TestCase):
    def test_parses_real_fixture(self):
        page = (FIXTURES / "cinema_page.html").read_text(encoding="utf-8")
        info = al.parse_cinema_info(page)
        self.assertIn("24290", info.get("adresse", ""))
        self.assertRegex(info.get("telephone", ""), r"\d{2} \d{2} \d{2} \d{2} \d{2}")
        self.assertTrue(info.get("programme_text", "").lower().startswith("programme du"))
        self.assertTrue(info.get("programme_url", "").endswith(".pdf"))

    def test_no_info_returns_empty_dict(self):
        self.assertEqual(al.parse_cinema_info("<html><body><p>Rien ici</p></body></html>"), {})


if __name__ == "__main__":
    unittest.main()
