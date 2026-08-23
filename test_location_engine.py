"""
test_location_engine.py
-----------------------
Unit tests for the offline geocoding helpers behind the profile location
feature (GPS detection + search/select).

Run with:  python -m unittest test_location_engine -v
(or:       python test_location_engine.py)
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from location_engine import (  # noqa: E402
    validate_coordinates, parse_coord_pair, resolve_location_coords,
    search_locations, reverse_geocode, gps_label, _title,
)


class ValidateCoordinatesTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(validate_coordinates(23.8103, 90.4125))
        self.assertTrue(validate_coordinates(-90, -180))
        self.assertTrue(validate_coordinates(90, 180))
        self.assertTrue(validate_coordinates(0, 0))
        self.assertTrue(validate_coordinates("23.8", "90.4"))

    def test_invalid_bounds(self):
        self.assertFalse(validate_coordinates(90.1, 0))
        self.assertFalse(validate_coordinates(-90.1, 0))
        self.assertFalse(validate_coordinates(0, 180.1))
        self.assertFalse(validate_coordinates(0, -180.1))

    def test_invalid_types(self):
        self.assertFalse(validate_coordinates("abc", 90))
        self.assertFalse(validate_coordinates(None, 90))
        self.assertFalse(validate_coordinates(23, None))
        self.assertFalse(validate_coordinates("", ""))
        self.assertFalse(validate_coordinates([23], 90))


class ParseCoordPairTest(unittest.TestCase):
    def test_comma_and_space_forms(self):
        self.assertEqual(parse_coord_pair("23.8103, 90.4125"), (23.8103, 90.4125))
        self.assertEqual(parse_coord_pair("23.8103 90.4125"), (23.8103, 90.4125))
        self.assertEqual(parse_coord_pair("GPS: 23.8 90.4"), (23.8, 90.4))
        self.assertEqual(parse_coord_pair("GPS 23.8,90.4"), (23.8, 90.4))

    def test_gps_label_format_round_trips(self):
        # gps_label() writes "GPS (23.8103, 90.4125)"; re-saving that string
        # as a location must not wipe the coordinates.
        self.assertEqual(parse_coord_pair("GPS (23.8103, 90.4125)"),
                         (23.8103, 90.4125))
        self.assertEqual(resolve_location_coords("GPS (23.8103, 90.4125)"),
                         (23.8103, 90.4125))

    def test_invalid(self):
        self.assertIsNone(parse_coord_pair(""))
        self.assertIsNone(parse_coord_pair(None))
        self.assertIsNone(parse_coord_pair("just words"))
        self.assertIsNone(parse_coord_pair("91, 0"))       # lat out of bounds
        self.assertIsNone(parse_coord_pair("23, 181"))     # lng out of bounds
        self.assertIsNone(parse_coord_pair("a, b"))


class ResolveLocationCoordsTest(unittest.TestCase):
    def test_known_places(self):
        self.assertEqual(resolve_location_coords("Badda"), (23.7844, 90.428))
        self.assertEqual(resolve_location_coords("Mirpur"), (23.8091, 90.3521))
        self.assertEqual(resolve_location_coords("North South University"),
                         (23.8153, 90.4257))

    def test_substring_in_text(self):
        # resolve_coords matches the location string against the table.
        self.assertIsNotNone(resolve_location_coords("Uttara, Dhaka"))
        self.assertIsNotNone(resolve_location_coords("Dhanmondi 27"))

    def test_explicit_pair(self):
        self.assertEqual(resolve_location_coords("23.8, 90.4"), (23.8, 90.4))

    def test_unknown(self):
        self.assertIsNone(resolve_location_coords("Some Unknown Place"))
        self.assertIsNone(resolve_location_coords(None))


class SearchLocationsTest(unittest.TestCase):
    def test_returns_expected_places(self):
        names = {r["name"].lower() for r in search_locations("mir")}
        self.assertIn("mirpur", names)
        self.assertTrue(all(r["name"] for r in search_locations("mir")))
        self.assertTrue(all("latitude" in r and "longitude" in r
                            for r in search_locations("mir")))

    def test_exact_query(self):
        results = search_locations("badda")
        self.assertTrue(any(r["name"].lower() == "badda" for r in results))

    def test_coords_are_valid(self):
        for r in search_locations("dha"):
            self.assertTrue(validate_coordinates(r["latitude"], r["longitude"]))

    def test_short_or_empty_query(self):
        self.assertEqual(search_locations(""), [])
        self.assertEqual(search_locations("a"), [])  # under min length
        self.assertEqual(search_locations(None), [])


class ReverseGeocodeTest(unittest.TestCase):
    def test_point_near_dhaka(self):
        name, km = reverse_geocode(23.8103, 90.4125)  # exactly Dhaka
        self.assertEqual(name, "Dhaka")
        self.assertEqual(km, 0.0)

    def test_point_in_dhaka_area(self):
        name, km = reverse_geocode(23.78, 90.41)
        self.assertIsNotNone(name)
        self.assertIsNotNone(km)
        self.assertLess(km, 5)

    def test_point_far_from_any_known_place(self):
        name, km = reverse_geocode(51.5074, -0.1278)  # London
        self.assertIsNone(name)
        self.assertIsNone(km)

    def test_invalid_input(self):
        self.assertEqual(reverse_geocode("x", 90), (None, None))
        self.assertEqual(reverse_geocode(95, 0), (None, None))


class GpsLabelAndTitleTest(unittest.TestCase):
    def test_gps_label_format(self):
        self.assertEqual(gps_label(23.8103, 90.4125), "GPS (23.8103, 90.4125)")

    def test_title_case(self):
        self.assertEqual(_title("dhanmondi"), "Dhanmondi")
        self.assertEqual(_title("cox's bazar"), "Cox's Bazar")
        self.assertEqual(_title("  badda  "), "Badda")


if __name__ == "__main__":
    unittest.main(verbosity=2)
