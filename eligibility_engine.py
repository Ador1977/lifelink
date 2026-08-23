"""
eligibility_engine.py
---------------------
RULE-BASED donation-history eligibility for blood donors.

This module is deliberately separate from the AI recommendation model:
every function here is deterministic, derived purely from the donor's
donation history (or the recorded last donation date), and driven by the
configured minimum donation interval.

The waiting period is configurable.  ``DONATION_INTERVAL_DAYS`` holds the
default guideline (90 days for men, 120 days for women); callers may pass
an ``interval_map`` (e.g. loaded from the app's ``AppSetting`` table) to
switch to a different guideline without touching the ranking/AI code.

What this module provides:
- next eligible donation date  = last donation + waiting period
- current eligibility status, computed from the current date every time
  (no manual "mark eligible" step anywhere)
- short, human-readable reasons such as
    "Can donate again after 15 August 2026."
    "Only 10 days remaining before eligibility."

The module is Flask-free: it accepts donor-like objects with a
``last_donation_date`` attribute and (optionally) a ``donations`` list,
so it can be unit-tested in isolation.
"""

from datetime import datetime, timedelta, timezone

# Default waiting period after a whole-blood donation before a donor can
# donate again.  Configurable per-platform via an interval_map.
DONATION_INTERVAL_DAYS = {"M": 90, "F": 120}


# ---------------------------------------------------------------------------
# TIME HELPERS
# ---------------------------------------------------------------------------
def _utc(dt):
    """Normalize a possibly-naive datetime to aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# CONFIGURABLE INTERVAL
# ---------------------------------------------------------------------------
def resolve_interval_map(interval_map=None):
    """Merge a (possibly partial) override dict into the defaults.

    interval_map keys are gender letters ('M'/'F'); values are positive
    integers (days).  Any invalid/absent entry falls back to the default
    guideline.  Returns a complete {"M": int, "F": int} dict.
    """
    merged = dict(DONATION_INTERVAL_DAYS)
    if interval_map:
        for gender, value in interval_map.items():
            try:
                days = int(value)
            except (TypeError, ValueError):
                continue
            if days > 0 and gender in merged:
                merged[gender] = days
    return merged


def interval_days_for(gender, interval_map=None):
    """Waiting period (in days) for a donor's gender under the given map."""
    merged = resolve_interval_map(interval_map)
    gender = (gender or "M").upper()
    return merged.get(gender, merged["M"])


# ---------------------------------------------------------------------------
# HISTORY HELPERS
# ---------------------------------------------------------------------------
def last_donation_date(donor):
    """Best last-donation timestamp for a donor: the newest Donation record
    if one exists (history is the source of truth), else the legacy
    ``last_donation_date`` column."""
    column = _utc(getattr(donor, "last_donation_date", None))
    donations = getattr(donor, "donations", None) or []
    from_records = []
    for d in donations:
        dt = _utc(getattr(d, "donation_date", None))
        if dt:
            from_records.append(dt)
    if from_records:
        latest = max(from_records)
        if column is None or latest > column:
            return latest
    return column


def next_eligible_date(last_donation_dt, gender, interval_map=None):
    """The date a donor may donate again: last donation + waiting period."""
    if last_donation_dt is None:
        return None
    cooldown = interval_days_for(gender, interval_map)
    return _utc(last_donation_dt) + timedelta(days=cooldown)


# ---------------------------------------------------------------------------
# STATUS / REASONS
# ---------------------------------------------------------------------------
def donation_eligibility_status(donor, now=None, interval_map=None):
    """Compute a donor's donation-history eligibility *right now*.

    The status is always derived from the donation history and the current
    date — there is no way to manually force someone to be eligible.

    Returns a dict:
    {
        "eligible": bool,
        "status": "Eligible" | "Not Eligible",
        "last_donation_date": datetime | None,
        "next_eligible_date": datetime | None,
        "cooldown_days": int,          # configured waiting period
        "days_remaining": int,         # days until eligible (0 if eligible)
        "reason": str,                 # short human-readable explanation
    }
    """
    now = _utc(now or datetime.now(timezone.utc))
    interval_map = resolve_interval_map(interval_map)
    gender = (getattr(donor, "gender", None) or "M").upper()
    cooldown = interval_map.get(gender, interval_map["M"])

    last = last_donation_date(donor)
    if last is None:
        return {
            "eligible": True,
            "status": "Eligible",
            "last_donation_date": None,
            "next_eligible_date": None,
            "cooldown_days": cooldown,
            "days_remaining": 0,
            "reason": "No donation history - available to donate.",
        }

    next_eligible = last + timedelta(days=cooldown)
    days_since = max((now - last).days, 0)
    days_remaining = max((next_eligible - now).days, 0)

    if now >= next_eligible:
        return {
            "eligible": True,
            "status": "Eligible",
            "last_donation_date": last,
            "next_eligible_date": next_eligible,
            "cooldown_days": cooldown,
            "days_remaining": 0,
            "reason": "Waiting period completed - available to donate.",
        }

    reason = f"Can donate again after {next_eligible.strftime('%d %B %Y')}."
    if days_remaining <= 14:
        reason += f" Only {days_remaining} days remaining before eligibility."
    return {
        "eligible": False,
        "status": "Not Eligible",
        "last_donation_date": last,
        "next_eligible_date": next_eligible,
        "cooldown_days": cooldown,
        "days_remaining": days_remaining,
        "reason": reason,
    }


def donation_eligibility_reasons(donor, now=None, interval_map=None):
    """Short reason list for donors who cannot donate due to their donation
    history / waiting period.  Empty list means they are eligible."""
    status = donation_eligibility_status(donor, now, interval_map)
    if status["eligible"]:
        return []
    return [status["reason"]]
