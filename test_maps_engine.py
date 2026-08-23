"""
test_maps_engine.py
-------------------
Unit tests for the Google Maps helpers (geocoding fallback, embed /
directions / search URLs, keyless behaviour).

The Google Geocoding API is never actually called in these tests - they
verify the offline fallback and the URL building.  Live geocoding only
happens when a GOOGLE_MAPS_API_KEY is configured.

Run with:  python -m unittest test_maps_engine -v
(or:       python test_maps_engine.py)
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Force the key off for the keyless-path tests regardless of .env.
os.environ["GOOGLE_MAPS_API_KEY"] = ""

import maps_engine  # noqa: E402
from maps_engine import (  # noqa: E402
    geocode, embed_url, directions_url, maps_search_url,
    maps_js_loader, api_key,
)


class ApiKeyTest(unittest.TestCase):
    def tearDown(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = ""

    def test_no_key_by_default(self):
        os.environ.pop("GOOGLE_MAPS_API_KEY", None)
        self.assertEqual(api_key(), "")

    def test_key_from_env(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = "abc123"
        self.assertEqual(api_key(), "abc123")


class GeocodeFallbackTest(unittest.TestCase):
    """Without a key (or when prefer_google=False), geocode must use the
    built-in Bangladesh location table and never hit the network."""

    def test_known_place(self):
        self.assertEqual(geocode("Mirpur"), (23.8091, 90.3521))
        self.assertEqual(geocode("Badda"), (23.7844, 90.428))

    def test_known_place_with_google_disabled(self):
        self.assertEqual(geocode("Dhaka", prefer_google=False),
                         (23.8103, 90.4125))

    def test_unknown_place(self):
        self.assertIsNone(geocode("Some Unknown Place"))
        self.assertIsNone(geocode(""))

    def test_empty_or_none(self):
        self.assertIsNone(geocode(""))
        self.assertIsNone(geocode(None))

    def test_key_present_but_offline_fallback(self):
        # A key is supplied but prefer_google=False - still offline only.
        self.assertEqual(geocode("Uttara", key="fake", prefer_google=False),
                         (23.8759, 90.3795))

    def test_no_key_never_touches_network(self):
        # Even with prefer_google=True and no key, we must stay offline.
        self.assertEqual(geocode("Khulna"), (22.8456, 89.5403))


class EmbedUrlTest(unittest.TestCase):
    def tearDown(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = ""

    def test_keyless_embed_from_coords(self):
        url = embed_url(coords=(23.8103, 90.4125))
        self.assertIn("https://www.google.com/maps", url)
        self.assertIn("output=embed", url)
        self.assertIn("q=23.8103%2C90.4125", url)

    def test_keyless_embed_from_query(self):
        url = embed_url(query="Dhaka Medical College Hospital")
        self.assertIn("output=embed", url)
        self.assertIn("Dhaka", url)

    def test_embed_prioritises_query_over_coords(self):
        url = embed_url(query="Uttara", coords=(23.8103, 90.4125))
        self.assertIn("Uttara", url)
        self.assertNotIn("23.8103,90.4125", url)

    def test_embed_with_key_uses_maps_embed_api(self):
        url = embed_url(query="Dhaka", key="KEY123")
        self.assertIn("https://www.google.com/maps/embed/v1/place", url)
        self.assertIn("key=KEY123", url)
        self.assertIn("q=Dhaka", url)

    def test_embed_nothing_to_show(self):
        self.assertIsNone(embed_url())
        self.assertIsNone(embed_url(query="   "))
        self.assertIsNone(embed_url(coords=None, query=""))


class DirectionsUrlTest(unittest.TestCase):
    def test_directions_from_coords(self):
        url = directions_url(coords=(23.8103, 90.4125))
        self.assertIn("https://www.google.com/maps/dir/?api=1", url)
        self.assertIn("destination=23.8103%2C90.4125", url)

    def test_directions_from_query(self):
        url = directions_url(query="Square Hospital Dhaka")
        self.assertIn("destination=Square", url)

    def test_directions_prefers_coords(self):
        url = directions_url(coords=(1, 2), query="Dhaka")
        self.assertIn("destination=1%2C2", url)

    def test_directions_nothing(self):
        self.assertIsNone(directions_url())
        self.assertIsNone(directions_url(query="  "))


class MapsSearchUrlTest(unittest.TestCase):
    def test_basic(self):
        url = maps_search_url("Dhaka Medical College Hospital")
        self.assertIn("https://www.google.com/maps/search/?api=1", url)
        self.assertIn("Dhaka", url)

    def test_empty(self):
        self.assertIsNone(maps_search_url(""))
        self.assertIsNone(maps_search_url(None))


class MapsJsLoaderTest(unittest.TestCase):
    def tearDown(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = ""

    def test_loader_needs_key(self):
        os.environ.pop("GOOGLE_MAPS_API_KEY", None)
        self.assertIsNone(maps_js_loader())

    def test_loader_with_key(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = "abc"
        self.assertIn("https://maps.googleapis.com/maps/api/js?key=abc",
                      maps_js_loader())


if __name__ == "__main__":
    unittest.main(verbosity=2)
