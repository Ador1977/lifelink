"""
test_recommendation_engine.py
-----------------------------
Unit tests for the AI-based Blood Donor Recommendation System.

Run with:  python -m unittest test_recommendation_engine -v
(or:       python test_recommendation_engine.py)

Covers:
- blood-group compatibility for every group
- exact-match prioritization
- Haversine distance
- eligibility / donation cooldown (male 90 days, female 120 days)
- the "currently not eligible" bucket (in-cooldown donors stay visible)
- configurable waiting period via interval_map
- availability and screening filters
- unverified / flagged blood-group donors are excluded from matching
- how emergency urgency changes the ranking
- ML score bounds
- unknown locations
- how many donors get recommended vs units needed
- contact-method preference scoring
"""

import os
import sys
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from recommendation_engine import (  # noqa: E402
    COMPATIBLE_DONORS, recommend_donors, score_donor, resolve_coords,
    haversine_km, format_distance, donation_eligibility_reasons,
    _contact_score, _recency_score, URGENCY_BOOST,
)


def make_donor(donor_id, blood_group, location="Dhaka", available=True,
               eligible=True, age=30, gender="M", last_donation_date=None,
               donation_count=0, contact="Phone", lat=None, lng=None,
               acceptances=None, donations=None, blood_group_verified=True,
               blood_group_flagged=False):
    """Build a lightweight donor stand-in (no database needed)."""
    return SimpleNamespace(
        id=donor_id, name=f"Donor {donor_id}", role="donor",
        blood_group=blood_group, location=location, is_deleted=False,
        is_available_donor=available, last_donation_eligible=eligible,
        age=age, gender=gender, last_donation_date=last_donation_date,
        donation_count=donation_count, preferred_contact_method=contact,
        latitude=lat, longitude=lng, phone="0170000000%d" % (donor_id % 10),
        blood_group_verified=blood_group_verified,
        blood_group_flagged=blood_group_flagged,
        donor_acceptances=acceptances or [],
        donations=donations or [],
    )


def reliable_history(n_accepted=5):
    return [SimpleNamespace(status="Accepted") for _ in range(n_accepted)]


class CompatibilityTest(unittest.TestCase):
    def test_only_compatible_donors_are_recommended(self):
        now = datetime.now(timezone.utc)
        for patient_group in COMPATIBLE_DONORS:
            candidates = [
                make_donor(i, group, lat=23.8103, lng=90.4125)
                for i, group in enumerate(COMPATIBLE_DONORS)
            ]
            result = recommend_donors(
                patient_group, candidates, "Dhaka", "Low",
                units_needed=20, patient_coords=(23.8103, 90.4125), now=now,
            )
            recommended = result["recommended"]
            self.assertGreater(len(recommended), 0, f"no donors for {patient_group}")
            allowed = set(COMPATIBLE_DONORS[patient_group])
            for r in recommended:
                self.assertIn(
                    r["blood_group"], allowed,
                    f"{r['blood_group']} cannot donate to {patient_group}",
                )
            # every compatible group should have been recommended
            recommended_groups = {r["blood_group"] for r in recommended}
            self.assertEqual(recommended_groups, allowed, patient_group)

    def test_exact_match_ranks_first_at_same_distance(self):
        exact = make_donor(1, "B+", lat=23.8103, lng=90.4125)
        compatible = make_donor(2, "O+", lat=23.8103, lng=90.4125)
        result = recommend_donors(
            "B+", [compatible, exact], "Dhaka", "Low",
            units_needed=5, patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(result["recommended"][0]["donor_id"], 1)
        self.assertTrue(result["recommended"][0]["exact_match"])


class DistanceTest(unittest.TestCase):
    def test_haversine_dhaka_chattogram(self):
        km = haversine_km(23.8103, 90.4125, 22.3569, 91.7832)
        self.assertAlmostEqual(km, 214.0, delta=5.0)

    def test_haversine_zero(self):
        self.assertEqual(haversine_km(23.81, 90.41, 23.81, 90.41), 0.0)

    def test_resolve_coords_prefers_stored(self):
        coords = resolve_coords(24.0, 91.0, "Sylhet")
        self.assertEqual(coords, (24.0, 91.0))

    def test_resolve_coords_city_fallback(self):
        coords = resolve_coords(None, None, "Bashundhara, Dhaka")
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], 23.8162, places=3)

    def test_resolve_coords_unknown_returns_none(self):
        self.assertIsNone(resolve_coords(None, None, "Atlantis"))

    def test_resolve_coords_most_specific_key_wins(self):
        # "Puran Dhaka" must resolve to Old Dhaka, not the generic "dhaka"
        # entry that also appears as a substring.
        coords = resolve_coords(None, None, "Puran Dhaka")
        self.assertEqual(coords, (23.7099, 90.4114))
        coords = resolve_coords(None, None, "Bashundhara, Dhaka")
        self.assertEqual(coords, (23.8162, 90.4257))


class EligibilityTest(unittest.TestCase):
    def test_recent_donation_kept_but_not_ranked(self):
        # A donor in the waiting period is not excluded and not ranked first:
        # they stay visible in the "currently not eligible" bucket.
        now = datetime.now(timezone.utc)
        donor = make_donor(1, "B+", last_donation_date=now - timedelta(days=30))
        reasons = donation_eligibility_reasons(donor, now)
        self.assertTrue(any("Can donate again after" in r for r in reasons))
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), now=now,
        )
        self.assertEqual(result["recommended"], [])
        self.assertEqual(result["excluded"], [])
        self.assertEqual(len(result["not_eligible"]), 1)
        entry = result["not_eligible"][0]
        self.assertEqual(entry["status"], "Not Eligible")
        self.assertTrue(entry["reason"].startswith("Can donate again after"))
        self.assertEqual(entry["days_remaining"], 60)
        self.assertEqual(entry["cooldown_days"], 90)

    def test_not_eligible_bucket_has_history_fields(self):
        now = datetime.now(timezone.utc)
        last = now - timedelta(days=30)
        donor = make_donor(1, "B+", last_donation_date=last, donation_count=4)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), now=now,
        )
        entry = result["not_eligible"][0]
        self.assertEqual(entry["donor_id"], 1)
        self.assertEqual(entry["donation_count"], 4)
        self.assertEqual(entry["last_donation_date"], last)
        self.assertEqual(entry["next_eligible_date"], last + timedelta(days=90))

    def test_male_allowed_after_90_days(self):
        now = datetime.now(timezone.utc)
        donor = make_donor(1, "B+", gender="M",
                           last_donation_date=now - timedelta(days=100))
        self.assertEqual(donation_eligibility_reasons(donor, now), [])
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), now=now,
        )
        self.assertEqual(len(result["recommended"]), 1)
        self.assertEqual(result["not_eligible"], [])

    def test_female_cooldown_is_120_days(self):
        now = datetime.now(timezone.utc)
        donor = make_donor(1, "B+", gender="F",
                           last_donation_date=now - timedelta(days=100))
        reasons = donation_eligibility_reasons(donor, now)
        self.assertTrue(any("Can donate again after" in r for r in reasons))

    def test_interval_map_override_reopens_or_blocks(self):
        now = datetime.now(timezone.utc)
        donor = make_donor(1, "B+", gender="M",
                           last_donation_date=now - timedelta(days=100))
        # Default 90-day period: eligible.
        self.assertEqual(donation_eligibility_reasons(donor, now), [])
        # A 120-day platform guideline: now not eligible.
        reasons = donation_eligibility_reasons(donor, now, interval_map={"M": 120})
        self.assertTrue(any("Can donate again after" in r for r in reasons))
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), now=now,
            interval_map={"M": 120},
        )
        self.assertEqual(result["recommended"], [])
        self.assertEqual(len(result["not_eligible"]), 1)

    def test_unavailable_donor_excluded(self):
        donor = make_donor(1, "B+", available=False)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(result["recommended"], [])
        self.assertTrue(any("unavailable" in r for e in result["excluded"] for r in e["reasons"]))

    def test_failed_screening_excluded(self):
        donor = make_donor(1, "B+", eligible=False)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(result["recommended"], [])
        self.assertTrue(any("screening" in r for e in result["excluded"] for r in e["reasons"]))

    def test_unscreened_donor_kept_but_penalised(self):
        # Not screened (None) must not be hard-excluded, but must score lower
        # than an identical screened-eligible donor.
        unscreened = make_donor(1, "B+", eligible=None)
        screened = make_donor(2, "B+", eligible=True)
        result = recommend_donors(
            "B+", [unscreened, screened], "Dhaka", "Low", units_needed=5,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(result["recommended"][0]["donor_id"], 2)
        self.assertGreater(result["recommended"][0]["score"],
                           result["recommended"][1]["score"])

    def test_recommended_entries_are_enriched_with_eligibility(self):
        now = datetime.now(timezone.utc)
        last = now - timedelta(days=200)
        donor = make_donor(1, "B+", last_donation_date=last, donation_count=3)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), now=now,
        )
        rec = result["recommended"][0]
        self.assertTrue(rec["eligibility_status"]["eligible"])
        self.assertEqual(rec["donation_count"], 3)
        self.assertEqual(rec["last_donation_date"], last)
        self.assertEqual(rec["next_eligible_date"], last + timedelta(days=90))
        self.assertIn("total_bags", rec)

    def test_summary_reports_eligibility_counts(self):
        now = datetime.now(timezone.utc)
        eligible = make_donor(1, "B+", last_donation_date=now - timedelta(days=200))
        cooling = make_donor(2, "B+", last_donation_date=now - timedelta(days=10))
        result = recommend_donors(
            "B+", [eligible, cooling], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), now=now,
        )
        summary = result["summary"]
        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["not_eligible_count"], 1)
        self.assertEqual(len(result["recommended"]), 1)
        self.assertEqual(len(result["not_eligible"]), 1)


class UrgencyRankingTest(unittest.TestCase):
    def setUp(self):
        # A nearby but brand-new donor vs a distant, very reliable donor.
        self.near = make_donor(1, "B+", location="Bashundhara",
                               lat=23.8162, lng=90.4257,
                               donation_count=0, acceptances=[])
        self.far = make_donor(2, "B+", location="Chattogram",
                              lat=22.3569, lng=91.7832,
                              donation_count=10, acceptances=reliable_history(5))
        self.coords = (23.8250, 90.4350)

    def test_critical_prefers_nearby(self):
        result = recommend_donors("B+", [self.near, self.far], "Dhaka",
                                  "Critical", units_needed=5,
                                  patient_coords=self.coords)
        self.assertEqual(result["recommended"][0]["donor_id"], 1)

    def test_low_urgency_prefers_reliable(self):
        result = recommend_donors("B+", [self.near, self.far], "Dhaka",
                                  "Low", units_needed=5,
                                  patient_coords=self.coords)
        self.assertEqual(result["recommended"][0]["donor_id"], 2)

    def test_urgency_boost_is_ordered(self):
        self.assertGreater(URGENCY_BOOST["Critical"], URGENCY_BOOST["High"])
        self.assertGreater(URGENCY_BOOST["High"], URGENCY_BOOST["Medium"])
        self.assertGreater(URGENCY_BOOST["Medium"], URGENCY_BOOST["Low"])


class MLScoreTest(unittest.TestCase):
    def test_model_score_within_bounds(self):
        donor = make_donor(1, "B+", lat=23.8103, lng=90.4125)
        info = score_donor(donor, "B+", (23.8103, 90.4125), "Low")
        self.assertGreaterEqual(info["model_score"], 0.0)
        self.assertLessEqual(info["model_score"], 1.0)
        self.assertGreaterEqual(info["score"], 0.0)
        self.assertLessEqual(info["score"], 100.0)

    def test_final_score_within_bounds_for_extremes(self):
        near = make_donor(1, "B+", lat=23.8103, lng=90.4125)
        info = score_donor(near, "B+", (23.8103, 90.4125), "Critical")
        self.assertLessEqual(info["score"], 100.0)


class EdgeCaseTest(unittest.TestCase):
    def test_unknown_location_still_recommended(self):
        donor = make_donor(1, "B+", location="Unknown Village", lat=None, lng=None)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(len(result["recommended"]), 1)
        self.assertIsNone(result["recommended"][0]["distance_km"])
        self.assertEqual(result["recommended"][0]["distance_label"], "Distance unknown")

    def test_recommend_count_respects_units(self):
        candidates = [make_donor(i, "B+", lat=23.8103, lng=90.4125)
                      for i in range(1, 8)]
        for units in (1, 4, 12):
            result = recommend_donors(
                "B+", candidates, "Dhaka", "Low", units_needed=units,
                patient_coords=(23.8103, 90.4125),
            )
            self.assertLessEqual(len(result["recommended"]), 10)
            self.assertGreaterEqual(len(result["recommended"]),
                                    min(units, len(candidates)))


class ContactPreferenceTest(unittest.TestCase):
    def test_contact_score_pure_function(self):
        phone = make_donor(1, "B+", contact="Phone")
        email = make_donor(2, "B+", contact="Email")
        none_ = make_donor(3, "B+", contact="")
        self.assertGreater(_contact_score(email, "Email"),
                           _contact_score(email, "Phone"))
        self.assertEqual(_contact_score(phone, None), 1.0)
        self.assertEqual(_contact_score(phone, "Any"), 1.0)
        self.assertEqual(_contact_score(none_, None), 0.5)

    def test_contact_method_exposed_in_output(self):
        donor = make_donor(1, "B+", contact="SMS")
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125), contact_pref="SMS",
        )
        self.assertEqual(result["recommended"][0]["contact_method"], "SMS")


class BloodGroupVerificationTest(unittest.TestCase):
    def test_unverified_group_excluded(self):
        donor = make_donor(1, "B+", blood_group_verified=False)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(result["recommended"], [])
        self.assertTrue(
            any("not verified" in r
                for e in result["excluded"] for r in e["reasons"]),
            result["excluded"],
        )

    def test_flagged_group_excluded(self):
        donor = make_donor(1, "B+", blood_group_flagged=True)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(result["recommended"], [])
        self.assertTrue(
            any("flagged" in r
                for e in result["excluded"] for r in e["reasons"]),
            result["excluded"],
        )

    def test_verified_clear_donor_still_recommended(self):
        donor = make_donor(1, "B+", blood_group_verified=True,
                           blood_group_flagged=False)
        result = recommend_donors(
            "B+", [donor], "Dhaka", "Low", units_needed=1,
            patient_coords=(23.8103, 90.4125),
        )
        self.assertEqual(len(result["recommended"]), 1)


class SummaryStructureTest(unittest.TestCase):
    def test_summary_keys_present(self):
        donor = make_donor(1, "B+")
        result = recommend_donors("B+", [donor], "Dhaka", "Critical", units_needed=2)
        summary = result["summary"]
        for key in ("blood_group", "urgency_level", "units_needed",
                    "compatible_count", "recommended_count", "urgency_boost"):
            self.assertIn(key, summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
