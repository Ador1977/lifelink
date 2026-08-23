"""
test_eligibility_engine.py
--------------------------
Unit tests for the rule-based donation-history eligibility engine.

Run with:  python -m unittest test_eligibility_engine -v
(or:       python test_eligibility_engine.py)

Covers:
- default waiting period (male 90 / female 120)
- next eligible date math
- automatic status transitions at the waiting-period boundary
- no-history donors are eligible
- status prefers the newest Donation record over the legacy column
- configurable interval override (partial + invalid values)
- short human-readable reasons
"""

import os
import sys
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from eligibility_engine import (  # noqa: E402
    DONATION_INTERVAL_DAYS, resolve_interval_map, interval_days_for,
    last_donation_date, next_eligible_date, donation_eligibility_status,
    donation_eligibility_reasons,
)


def make_donor(gender="M", last_donation_date=None, donations=None):
    return SimpleNamespace(
        id=1, name="Test Donor", role="donor", gender=gender,
        last_donation_date=last_donation_date, donations=donations or [],
    )


def make_record(dt):
    return SimpleNamespace(donation_date=dt)


class IntervalMapTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DONATION_INTERVAL_DAYS, {"M": 90, "F": 120})

    def test_resolve_default_map_when_none(self):
        self.assertEqual(resolve_interval_map(None), {"M": 90, "F": 120})

    def test_resolve_partial_override(self):
        merged = resolve_interval_map({"M": 60})
        self.assertEqual(merged["M"], 60)
        self.assertEqual(merged["F"], 120)

    def test_resolve_rejects_invalid_values(self):
        merged = resolve_interval_map({"M": -5, "F": "banana", "X": 10})
        self.assertEqual(merged["M"], 90)
        self.assertEqual(merged["F"], 120)

    def test_interval_days_for_unknown_gender_falls_back(self):
        self.assertEqual(interval_days_for("Other"), 90)

    def test_interval_days_for_override(self):
        self.assertEqual(interval_days_for("F", {"F": 100}), 100)


class NextEligibleDateTest(unittest.TestCase):
    def test_male_90_days(self):
        last = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.assertEqual(next_eligible_date(last, "M"), last + timedelta(days=90))

    def test_female_120_days(self):
        last = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.assertEqual(next_eligible_date(last, "F"), last + timedelta(days=120))

    def test_override_applies(self):
        last = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.assertEqual(
            next_eligible_date(last, "M", {"M": 45}), last + timedelta(days=45)
        )

    def test_no_history_returns_none(self):
        self.assertIsNone(next_eligible_date(None, "M"))


class LastDonationDateTest(unittest.TestCase):
    def test_prefers_newest_record_over_column(self):
        column = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = [
            make_record(datetime(2025, 6, 1, tzinfo=timezone.utc)),
            make_record(datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]
        donor = make_donor(last_donation_date=column, donations=records)
        self.assertEqual(last_donation_date(donor),
                         datetime(2026, 2, 1, tzinfo=timezone.utc))

    def test_older_record_does_not_override_column(self):
        column = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = [make_record(datetime(2025, 6, 1, tzinfo=timezone.utc))]
        donor = make_donor(last_donation_date=column, donations=records)
        self.assertEqual(last_donation_date(donor), column)

    def test_handles_naive_datetimes(self):
        donor = make_donor(last_donation_date=datetime(2026, 1, 1))
        result = last_donation_date(donor)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.tzinfo)


class StatusTest(unittest.TestCase):
    def test_no_history_is_eligible(self):
        status = donation_eligibility_status(make_donor())
        self.assertTrue(status["eligible"])
        self.assertEqual(status["status"], "Eligible")
        self.assertIn("No donation history", status["reason"])

    def test_in_cooldown_is_not_eligible(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("M", last_donation_date=now - timedelta(days=30))
        status = donation_eligibility_status(donor, now)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["status"], "Not Eligible")
        self.assertEqual(status["cooldown_days"], 90)
        self.assertEqual(status["days_remaining"], 60)
        self.assertTrue(status["reason"].startswith("Can donate again after"))

    def test_becomes_eligible_at_boundary(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("M", last_donation_date=now - timedelta(days=90))
        status = donation_eligibility_status(donor, now)
        self.assertTrue(status["eligible"])
        self.assertEqual(status["days_remaining"], 0)

    def test_short_countdown_reason_when_close(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("M", last_donation_date=now - timedelta(days=80))
        status = donation_eligibility_status(donor, now)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["days_remaining"], 10)
        self.assertIn("Only 10 days remaining", status["reason"])

    def test_female_120_day_rule(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("F", last_donation_date=now - timedelta(days=100))
        status = donation_eligibility_status(donor, now)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["cooldown_days"], 120)
        self.assertEqual(status["days_remaining"], 20)

    def test_status_derived_from_newest_record(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=400)
        recent = now - timedelta(days=30)
        donor = make_donor(
            "M", last_donation_date=old,
            donations=[make_record(old), make_record(recent)],
        )
        status = donation_eligibility_status(donor, now)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["last_donation_date"], recent)

    def test_interval_override_blocks_otherwise_eligible(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("M", last_donation_date=now - timedelta(days=100))
        self.assertTrue(donation_eligibility_status(donor, now)["eligible"])
        status = donation_eligibility_status(donor, now, {"M": 120})
        self.assertFalse(status["eligible"])
        self.assertEqual(status["days_remaining"], 20)

    def test_interval_override_reopens_otherwise_blocked(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("M", last_donation_date=now - timedelta(days=60))
        self.assertFalse(donation_eligibility_status(donor, now)["eligible"])
        status = donation_eligibility_status(donor, now, {"M": 30})
        self.assertTrue(status["eligible"])


class ReasonsTest(unittest.TestCase):
    def test_eligible_yields_empty_list(self):
        donor = make_donor("M", last_donation_date=None)
        self.assertEqual(donation_eligibility_reasons(donor), [])

    def test_blocked_yields_single_reason(self):
        now = datetime.now(timezone.utc)
        donor = make_donor("M", last_donation_date=now - timedelta(days=10))
        reasons = donation_eligibility_reasons(donor, now)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("Can donate again after"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
