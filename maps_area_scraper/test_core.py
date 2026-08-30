"""Lightweight unit tests for pure functions — run: python3 test_core.py"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from area_engine import (
    AreaConfig, categories_for, grid_points, haversine_km,
    point_in_area, point_in_polygon, place_key_from_url,
)
from queue_store import canonical_place_key, lead_key, _lead_matches

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maps_lead_studio"))
from scraper import _score, _contact_confidence, build_search_url, SearchConfig


class TestGeometry(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Delhi -> Agra is roughly 180 km
        d = haversine_km((28.6139, 77.2090), (27.1767, 78.0081))
        self.assertTrue(160 < d < 200, d)

    def test_point_in_polygon(self):
        square = [(0, 0), (0, 10), (10, 10), (10, 0)]
        self.assertTrue(point_in_polygon((5, 5), square))
        self.assertFalse(point_in_polygon((15, 5), square))

    def test_point_in_circle(self):
        geometry = {"type": "circle", "center": [28.6, 77.2], "radius_m": 5000}
        self.assertTrue(point_in_area((28.6, 77.2), geometry))
        self.assertFalse(point_in_area((29.6, 77.2), geometry))

    def test_grid_points_circle(self):
        geometry = {"type": "circle", "center": [28.6, 77.2], "radius_m": 3000}
        points = grid_points(geometry, 1.0, maximum=200)
        self.assertTrue(points)
        for point in points:
            self.assertTrue(point_in_area(point, geometry))

    def test_grid_points_respects_maximum(self):
        geometry = {"type": "circle", "center": [28.6, 77.2], "radius_m": 20000}
        self.assertLessEqual(len(grid_points(geometry, 0.2, maximum=50)), 50)


class TestKeys(unittest.TestCase):
    def test_canonical_place_key_hex(self):
        url = "https://www.google.com/maps/place/X/@28.6,77.2,17z/data=!3m1!4b1!4m6!3m5!1s0x390cfd5b347eb62d:0x52c2b7494e204dce"
        self.assertEqual(canonical_place_key(url), "0x390cfd5b347eb62d:0x52c2b7494e204dce")

    def test_canonical_place_key_prefers_place_id(self):
        self.assertEqual(canonical_place_key("http://x", "ABC123"), "abc123")

    def test_place_key_from_url_matches_store(self):
        url = "https://maps.google.com/?data=!1s0xabc:0xdef!extra"
        self.assertEqual(place_key_from_url(url), canonical_place_key(url))

    def test_lead_key_fallbacks(self):
        self.assertEqual(lead_key({"place_id": "P1"}), "p1")
        self.assertEqual(lead_key({"phone": "+91 99999"}), "+91 99999")
        self.assertIn("shop|addr", lead_key({"name": "Shop", "address": "Addr"}))


class TestScoring(unittest.TestCase):
    def test_score_full_contact_is_hot(self):
        lead = {"phone": "+919999999999", "email": "a@b.com", "website": "https://b.com",
                "address": "X", "social_profiles": "fb", "whatsapp": "wa", "reviews": 250, "rating": 4.5}
        _contact_confidence(lead)
        score, tier = _score(lead)
        self.assertGreaterEqual(score, 70)
        self.assertEqual(tier, "hot")

    def test_score_empty_is_cold_or_warm(self):
        lead = {}
        _contact_confidence(lead)
        score, tier = _score(lead)
        self.assertLess(score, 70)

    def test_email_confidence_domain_match(self):
        lead = {"email": "info@example.com", "website": "https://www.example.com", "phone": ""}
        _contact_confidence(lead)
        self.assertEqual(lead["email_confidence"], 95)


class TestFilters(unittest.TestCase):
    LEAD = {"name": "Cafe One", "category": "Cafe", "lead_tier": "hot",
            "phone": "+911234", "email": "", "email_maps": "x@y.com", "website": "", "address": "MG Road"}

    def test_tier_filter(self):
        self.assertTrue(_lead_matches(self.LEAD, tier="hot"))
        self.assertFalse(_lead_matches(self.LEAD, tier="cold"))

    def test_require_email_uses_maps_email(self):
        self.assertTrue(_lead_matches(self.LEAD, require_email=True))
        self.assertFalse(_lead_matches(self.LEAD, require_website=True))

    def test_search(self):
        self.assertTrue(_lead_matches(self.LEAD, search="mg road"))
        self.assertFalse(_lead_matches(self.LEAD, search="pizza"))


class TestSearchUrl(unittest.TestCase):
    def test_coordinates_url(self):
        config = SearchConfig(categories=["cafes"], locations=[""], location_mode="coordinates",
                              latitude=28.6, longitude=77.2, radius_km=2)
        url = build_search_url("cafes", config)
        self.assertIn("@28.6,77.2,", url)
        self.assertIn("z", url.rsplit(",", 1)[-1])


class TestConfig(unittest.TestCase):
    def test_categories_custom_override(self):
        config = AreaConfig(geometry={}, custom_categories=["a", "b", "a"])
        self.assertEqual(categories_for(config), ["a", "b"])

    def test_proxy_default_empty(self):
        self.assertEqual(AreaConfig(geometry={}).proxy, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
