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
            self.assertTrue(e.url.startswith("https://data.datatourisme.fr/"))

    def test_empty_window_yields_no_events(self):
        out = al._datatourisme_object_events(self.objects[0], dt(1900, 1, 1), dt(1901, 1, 1))
        self.assertEqual(out, [])


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

    def test_openagenda_employment_events(self):
        cat = al.openagenda_category("OBJECTIF EMPLOI", [], "Mes événements France Travail")
        self.assertEqual(cat, "Emploi & formation")

    def test_openagenda_falls_back_to_autre(self):
        cat = al.openagenda_category("Réunion du conseil", [], "")
        self.assertEqual(cat, "Autre")


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


class TestWednesdayWindow(unittest.TestCase):
    def test_from_monday(self):
        monday = dt(2026, 9, 21, 8, 0)
        start, end = al.wednesday_window(monday)
        self.assertEqual(start.date(), dt(2026, 9, 23).date())
        self.assertEqual((start.hour, start.minute), (0, 0))
        self.assertEqual((end.hour, end.minute), (23, 59))

    def test_on_wednesday_keeps_today(self):
        wednesday = dt(2026, 9, 23, 18, 0)
        start, _ = al.wednesday_window(wednesday)
        self.assertEqual(start.date(), wednesday.date())


class TestBucketize(unittest.TestCase):
    def test_events_go_to_first_matching_window_in_order(self):
        e_weekend = al.Event("A", dt(2026, 9, 26, 10), dt(2026, 9, 26, 12))
        e_wed = al.Event("B", dt(2026, 9, 30, 10), dt(2026, 9, 30, 12))
        e_far = al.Event("C", dt(2026, 11, 1, 10), dt(2026, 11, 1, 12))
        windows = [
            ("Ce week-end", dt(2026, 9, 25, 17), dt(2026, 9, 27, 23, 59)),
            ("Mercredi", dt(2026, 9, 30, 0), dt(2026, 9, 30, 23, 59)),
        ]
        buckets = al._bucketize([e_weekend, e_wed, e_far], windows)
        self.assertEqual(buckets["Ce week-end"], [e_weekend])
        self.assertEqual(buckets["Mercredi"], [e_wed])
        self.assertEqual(buckets["À venir"], [e_far])


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
