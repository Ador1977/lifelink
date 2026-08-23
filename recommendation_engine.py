"""
recommendation_engine.py
------------------------
AI-based Blood Donor Recommendation System.

How it works (three layers):

1. RULE-BASED HARD FILTERS (deterministic, safety-critical)
   Every registered donor is first checked against fixed medical/safety
   rules. Anyone who fails is excluded and reported *with a reason*:
   - account must be an active (non-deleted) donor
   - must be marked available to donate
   - blood group must be compatible with the patient's group
   - must not have failed a health screening
   - age must be within the 18-65 donation range

   Separately, the donation-history waiting period (see eligibility_engine.py)
   decides whether a donor is ELIGIBLE *right now*. Donors who are still in
   the cooldown are never ranked as primary recommendations - they are kept
   visible under "Currently Not Eligible" with a short reason and their next
   eligible date.

2. MACHINE-LEARNING SOFT SCORING (the "AI" part)
   For donors who pass the hard filters, the trained DonorRecommender
   (GradientBoostingRegressor in ai_engine.py) predicts a 0-1
   "donation suitability" score from normalized donor attributes:
   compatibility exactness, distance, availability, screening
   eligibility, reliability history, donation experience, donation
   recency, and reachability via preferred contact method.

3. RULE-BASED URGENCY BOOST (deterministic)
   The final score blends the ML score with the proximity score. The
   more urgent the request, the more weight proximity carries, so
   nearby donors rise to the top in emergencies:

       final = (1 - boost) * model_score + boost * proximity_score

   An exact blood-group match also receives a small fixed bonus.

This module is deliberately Flask-free: app.py passes in donor objects
and the request attributes, keeping the matching logic easy to test.
"""

import math
from datetime import datetime, timezone

from ai_engine import donor_recommender
from eligibility_engine import (
    DONATION_INTERVAL_DAYS,
    donation_eligibility_status,
    interval_days_for,
    resolve_interval_map,
)

# ---------------------------------------------------------------------------
# BLOOD GROUP COMPATIBILITY
# Patient's group -> donor groups that may donate to them.
# ---------------------------------------------------------------------------
COMPATIBLE_DONORS = {
    "A+": ["A+", "A-", "O+", "O-"],
    "A-": ["A-", "O-"],
    "B+": ["B+", "B-", "O+", "O-"],
    "B-": ["B-", "O-"],
    "AB+": ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"],
    "AB-": ["A-", "B-", "AB-", "O-"],
    "O+": ["O+", "O-"],
    "O-": ["O-"],
}

# ---------------------------------------------------------------------------
# LOCATION DATA (Bangladesh) for distance calculation.
# When a donor or patient has stored latitude/longitude that is used first;
# otherwise the free-text location is matched against this table.
# ---------------------------------------------------------------------------
CITY_COORDS = {
    "bashundhara": (23.8162, 90.4257),
    "gulshan": (23.7925, 90.4074),
    "banani": (23.7945, 90.4073),
    "dhanmondi": (23.7461, 90.3767),
    "mirpur": (23.8091, 90.3521),
    "uttara": (23.8759, 90.3795),
    "motijheel": (23.7330, 90.4170),
    "badda": (23.7844, 90.4280),
    "khilgaon": (23.7506, 90.4198),
    "mogbazar": (23.7528, 90.4085),
    "rampura": (23.7667, 90.4333),
    "tejgaon": (23.7586, 90.3946),
    "savar": (23.8583, 90.2667),
    "narayanganj": (23.6238, 90.5000),
    "dhaka": (23.8103, 90.4125),
    "chattogram": (22.3569, 91.7832),
    "chittagong": (22.3569, 91.7832),
    "cox's bazar": (21.4272, 92.0058),
    "cox'sbazar": (21.4272, 92.0058),
    "cumilla": (23.4607, 91.1809),
    "comilla": (23.4607, 91.1809),
    "noakhali": (22.8696, 91.0994),
    "sylhet": (24.8949, 91.8687),
    "moulvibazar": (24.4814, 91.7736),
    "rajshahi": (24.3745, 88.6042),
    "pabna": (24.0064, 89.2372),
    "kushtia": (23.9013, 89.1205),
    "bogura": (24.8464, 89.3704),
    "bogra": (24.8464, 89.3704),
    "sirajganj": (24.4534, 89.7000),
    "khulna": (22.8456, 89.5403),
    "jessore": (23.1634, 89.2160),
    "barishal": (22.7010, 90.3535),
    "rangpur": (25.7466, 89.2517),
    "dinajpur": (25.6218, 88.6373),
    "mymensingh": (24.7471, 90.4203),
    "gazipur": (23.9999, 90.4203),
    "tangail": (24.2513, 89.9167),
    "jamalpur": (24.9376, 89.9391),
    "faridpur": (23.6042, 89.8428),
    "north south university": (23.8153, 90.4257),
    "north south": (23.8153, 90.4257),
    # Extra Dhaka areas commonly written on donor rosters / profiles.
    "ashulia": (23.8975, 90.3230),
    "zirabo": (23.8975, 90.3230),
    "bashabo": (23.7395, 90.4340),
    "polashpur": (23.7450, 90.3500),
    "puran dhaka": (23.7099, 90.4114),
    "purana paltan": (23.7333, 90.4167),
    "ullapara": (23.8667, 90.3333),
    "turag": (23.8833, 90.3833),
    "wari": (23.7119, 90.4140),
    "paltan": (23.7333, 90.4167),
    "panthapath": (23.7424, 90.3830),
    "rayer bazar": (23.7300, 90.3600),
    "rk mission road": (23.7350, 90.3980),
    "sabujbag": (23.7450, 90.4270),
    "segun bagicha": (23.7394, 90.4050),
    "shahbag": (23.7381, 90.3954),
    "shahbagh": (23.7381, 90.3954),
    "shahjadpur": (23.7620, 90.4060),
    "shamibag": (23.7550, 90.4200),
    "shantibag": (23.7600, 90.4150),
    "shanti nagar": (23.7500, 90.4120),
    "shyamoli": (23.7717, 90.3536),
    "shyampur": (23.6860, 90.4100),
    "taltola": (23.7500, 90.4200),
    "tikkapara": (23.7800, 90.3600),
    "tongi": (23.8913, 90.4058),
    "khilkhet": (23.8200, 90.4200),
    "konapara": (23.7000, 90.4300),
    "kotwali": (23.7100, 90.4120),
    "kuril": (23.8211, 90.4242),
    "lalbag": (23.7160, 90.3850),
    "malibag": (23.7550, 90.4150),
    "mir hajir bag": (23.7400, 90.3700),
    "mohakhali": (23.7753, 90.4048),
    "mohammadpur": (23.7658, 90.3585),
    "mugdapara": (23.7400, 90.4300),
    "dania": (23.6660, 90.4500),
    "dilkusha": (23.7330, 90.4140),
    "hazaribag": (23.7250, 90.3750),
    "kallyanpur": (23.7550, 90.3350),
    "kamalapur": (23.7330, 90.4260),
    "kamarpara": (23.7300, 90.3600),
    "cantonment": (23.8145, 90.4075),
    # Remaining Bangladesh district towns so users anywhere in the country
    # can pick a location and still get distance-based matching.
    "bandarban": (22.1953, 92.2183),
    "khagrachari": (23.1192, 91.9847),
    "rangamati": (22.6473, 92.1779),
    "madaripur": (23.1641, 90.2097),
    "gopalganj": (23.0050, 89.8263),
    "shariatpur": (23.2423, 90.4348),
    "munshiganj": (23.5422, 90.5308),
    "chandpur": (23.2334, 90.6712),
    "feni": (23.0159, 91.3976),
    "brahmanbaria": (23.9571, 91.1119),
    "laxmipur": (22.9444, 90.8302),
    "lakhsmipur": (22.9444, 90.8302),
    "bhola": (22.6878, 90.6485),
    "pirojpur": (22.5841, 89.9720),
    "jhalokathi": (22.6406, 90.1987),
    "barguna": (22.1591, 90.1262),
    "patuakhali": (22.3596, 90.3294),
    "satkhira": (22.7185, 89.0705),
    "magura": (23.4870, 89.4199),
    "jhenaidah": (23.5528, 89.1555),
    "narail": (23.1557, 89.4962),
    "chuadanga": (23.6401, 88.8571),
    "meherpur": (23.7622, 88.6318),
    "joypurhat": (25.0968, 89.0227),
    "naogaon": (24.7936, 88.9318),
    "natore": (24.4130, 89.0000),
    "chapainawabganj": (24.5965, 88.2770),
    "sherpur": (25.0189, 90.0175),
    "netrokona": (24.8705, 90.7283),
    "kishoreganj": (24.4449, 90.7768),
    "manikganj": (23.8617, 90.0016),
    "lalmonirhat": (25.9000, 89.4500),
    "kurigram": (25.8044, 89.6557),
    "gaibandha": (25.3288, 89.5421),
    "nilphamari": (25.9312, 88.8565),
    "thakurgaon": (26.0337, 88.4616),
    "panchagarh": (26.3337, 88.5543),
    "sunamganj": (25.0715, 91.3999),
    "habiganj": (24.3745, 91.4155),
    "hobiganj": (24.3745, 91.4155),
}

# Post-donation cooldown before a donor can give again.  These are the
# platform defaults; the value can be overridden at runtime via an
# interval_map (see eligibility_engine), e.g. from the AppSetting table.
DONATION_COOLDOWN_DAYS = DONATION_INTERVAL_DAYS

MIN_RECOMMENDED = 3      # always recommend at least this many (if available)
MAX_RECOMMENDED = 10     # never recommend more than this
DEFAULT_PATIENT_COORDS = (23.8103, 90.4125)  # Dhaka fallback

# How strongly proximity influences the final rank per urgency level.
URGENCY_BOOST = {
    "Critical": 0.30,
    "High": 0.20,
    "Medium": 0.10,
    "Low": 0.05,
    "Review": 0.05,
}

MATCH_BONUS = 2.0        # extra points for an exact blood-group match
DISTANCE_REFERENCE_KM = 5.0  # distance that maps to a 0.5 proximity score


def _utc(dt):
    """Normalize a possibly-naive datetime to aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clip01(value):
    return max(0.0, min(1.0, float(value)))


def _clip100(value):
    return max(0.0, min(100.0, float(value)))


# ---------------------------------------------------------------------------
# DISTANCE
# ---------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in kilometres."""
    earth_radius_km = 6371.0
    p1 = math.radians(lat1), math.radians(lon1)
    p2 = math.radians(lat2), math.radians(lon2)
    dlat = p2[0] - p1[0]
    dlon = p2[1] - p1[1]
    a = (math.sin(dlat / 2) ** 2
         + math.cos(p1[0]) * math.cos(p2[0]) * math.sin(dlon / 2) ** 2)
    return round(earth_radius_km * 2 * math.asin(math.sqrt(a)), 2)


def resolve_coords(latitude, longitude, location):
    """Best-effort (lat, lng) for a donor or patient. Prefers stored
    coordinates, falls back to the built-in city/area table, else None."""
    try:
        lat = float(latitude)
        lng = float(longitude)
        if lat is not None and lng is not None:
            return round(lat, 6), round(lng, 6)
    except (TypeError, ValueError):
        pass
    if location:
        s = location.strip().lower()
        best_key = None
        for key in CITY_COORDS:
            # Longest matching key wins so "Puran Dhaka" maps to Old Dhaka
            # instead of the generic "dhaka" entry.
            if key in s and (best_key is None or len(key) > len(best_key)):
                best_key = key
        if best_key:
            return CITY_COORDS[best_key]
    return None


def format_distance(km):
    if km is None:
        return "Distance unknown"
    if km < 1:
        return f"{int(round(km * 1000))} m away"
    return f"{km:.1f} km away"


def proximity_score(distance_km):
    """Map a distance to a 0-1 score (1.0 at 0 km, 0.5 at reference km)."""
    if distance_km is None:
        return 0.5  # neutral when the location can't be resolved
    return _clip01(1.0 / (1.0 + distance_km / DISTANCE_REFERENCE_KM))


# ---------------------------------------------------------------------------
# RELIABILITY (derived from donor response history)
# ---------------------------------------------------------------------------
def compute_reliability(donor):
    """A 0-1 estimate of how reliable a donor is, from their past responses:
    an accepted donation is worth 1.0, a pending offer 0.4, a rejected one 0.
    Donors with no history get a neutral baseline of 0.5."""
    responses = list(getattr(donor, "donor_acceptances", None) or [])
    if not responses:
        return 0.5
    positive = 0.0
    for a in responses:
        status = getattr(a, "status", "")
        if status == "Accepted":
            positive += 1.0
        elif status == "Pending":
            positive += 0.4
    ratio = positive / len(responses)
    return round(0.3 + 0.7 * ratio, 3)


# ---------------------------------------------------------------------------
# ELIGIBILITY (rule-based hard checks)
# ---------------------------------------------------------------------------
def _non_history_reasons(donor):
    """Hard rule checks that have nothing to do with donation history:
    account state, availability, health screening, age range.  These are
    pure safety filters; the donation-history waiting period is computed
    separately by the eligibility engine."""
    reasons = []

    if getattr(donor, "role", None) != "donor":
        reasons.append("Not registered as a donor account")
    if getattr(donor, "is_deleted", False):
        reasons.append("Account deactivated")
    if not getattr(donor, "is_available_donor", False):
        reasons.append("Marked as unavailable to donate")
    if getattr(donor, "last_donation_eligible", None) is False:
        reasons.append("Failed the latest health screening")
    if getattr(donor, "blood_group_verified", True) is not True:
        # A blood group that came from a card image but hasn't been confirmed
        # yet must never be used for matching -- a wrong group could be fatal.
        reasons.append("Blood group not verified yet")
    if getattr(donor, "blood_group_flagged", False):
        # A card scan contradicted this donor's group (or the detection was
        # uncertain); keep them out of matching until the flag is resolved.
        reasons.append("Blood group flagged for verification")

    age = getattr(donor, "age", None)
    if age is not None and not (18 <= age <= 65):
        reasons.append(f"Age {age} is outside the 18-65 donation range")

    return reasons


def donation_eligibility_reasons(donor, now=None, interval_map=None):
    """Return human-readable reasons a donor cannot donate right now.
    An empty list means the donor passes every hard rule.

    Combines the non-history safety filters with the donation-history
    waiting-period check from the eligibility engine.  The interval_map
    (gender -> days) may override the configured guideline."""
    now = now or datetime.now(timezone.utc)
    reasons = _non_history_reasons(donor)
    history = donation_eligibility_status(donor, now, interval_map)
    if not history["eligible"]:
        reasons.append(history["reason"])
    return reasons


def donor_total_bags(donor):
    """Total number of blood bags donated across all recorded donations.
    Falls back to the donation count when no history rows exist yet."""
    donations = getattr(donor, "donations", None) or []
    if donations:
        return sum((getattr(d, "bags", None) or 0) for d in donations)
    return getattr(donor, "donation_count", 0) or 0


# ---------------------------------------------------------------------------
# PER-DONOR SCORING (ML + urgency-aware rule blending)
# ---------------------------------------------------------------------------
def _contact_score(donor, contact_pref=None):
    """0-1 reachability. If the requester specified a preferred outreach
    channel, donors who match it score higher."""
    method = (getattr(donor, "preferred_contact_method", None) or "").strip()
    pref = (contact_pref or "").strip()
    if not pref or pref.lower() in ("any", ""):
        return 1.0 if method else 0.5
    if not method:
        return 0.5
    if method.lower() == "any" or method.lower() == pref.lower():
        return 1.0
    return 0.5


def _recency_score(donor, now):
    """0-1 for how long since the donor's last donation (365 days -> 1.0).
    First-time donors get a neutral 0.5."""
    last_date = _utc(getattr(donor, "last_donation_date", None))
    if last_date is None:
        return 0.5
    days = max((now - last_date).days, 0)
    return _clip01(days / 365.0)


def score_donor(donor, blood_group, patient_coords, urgency_level,
                contact_pref=None, now=None, interval_map=None):
    """Compute the full recommendation score for one donor.

    Returns a dict with the raw ML score, the blended final score (0-100),
    distance info, reliability, eligibility flags + donation-history status,
    and the per-factor breakdown used for transparency on the frontend."""
    now = now or datetime.now(timezone.utc)
    exact_match = getattr(donor, "blood_group", None) == blood_group

    d_coords = resolve_coords(
        getattr(donor, "latitude", None),
        getattr(donor, "longitude", None),
        getattr(donor, "location", None),
    )
    distance_km = None
    if d_coords and patient_coords:
        distance_km = haversine_km(
            patient_coords[0], patient_coords[1], d_coords[0], d_coords[1]
        )
    distance_score = proximity_score(distance_km)

    screened = getattr(donor, "last_donation_eligible", None)
    if screened is True:
        eligibility = 1.0
    elif screened is None:
        eligibility = 0.6  # not screened - kept but penalised
    else:
        eligibility = 0.0  # safety net; hard filter already removed these

    reliability = compute_reliability(donor)

    features = {
        "compat": 1.0 if exact_match else 0.85,
        "distance": distance_score,
        "availability": 1.0,
        "eligibility": eligibility,
        "reliability": reliability,
        "experience": _clip01((getattr(donor, "donation_count", 0) or 0) / 10.0),
        "recency": _recency_score(donor, now),
        "contact": _contact_score(donor, contact_pref),
    }

    model_score = donor_recommender.predict(features)

    # Urgency-aware blend: the more urgent the request, the more the final
    # score leans on proximity so nearby donors rank higher in emergencies.
    boost = URGENCY_BOOST.get(urgency_level, 0.05)
    final_score = (1 - boost) * (model_score * 100) + boost * (distance_score * 100)
    if exact_match:
        final_score += MATCH_BONUS
    final_score = _clip100(final_score)

    history = donation_eligibility_status(donor, now, interval_map)

    return {
        "donor": donor,
        "distance_km": distance_km,
        "distance_label": format_distance(distance_km),
        "exact_match": exact_match,
        "eligible": screened is not False,
        "screened": screened is not None,
        "reliability": reliability,
        "model_score": round(model_score, 4),
        "score": round(final_score, 2),
        "factors": features,
        "eligibility_status": history,
        "donation_count": getattr(donor, "donation_count", 0) or 0,
        "total_bags": donor_total_bags(donor),
        "last_donation_date": history["last_donation_date"],
        "next_eligible_date": history["next_eligible_date"],
    }


# ---------------------------------------------------------------------------
# TOP-LEVEL RECOMMENDATION PIPELINE
# ---------------------------------------------------------------------------
def recommend_donors(blood_group, candidates, patient_location, urgency_level,
                     units_needed=1, contact_pref=None, patient_coords=None,
                     now=None, interval_map=None):
    """Rank donor candidates for a blood request.

    blood_group      - patient's required blood group (e.g. "B+")
    candidates       - iterable of donor objects (role 'donor', not deleted)
    patient_location - free-text patient/hospital location
    urgency_level    - "Critical" | "High" | "Medium" | "Low" | "Review"
    units_needed     - how many units (drives how many donors to recommend)
    contact_pref     - optional preferred outreach channel ("Phone"/"SMS"/
                       "Email"/"Any")
    patient_coords   - optional explicit (lat, lng) override
    now              - optional datetime (for tests)
    interval_map     - optional {"M": days, "F": days} waiting-period
                       override for the donation-history eligibility check

    Donors are split into three groups:
      recommended  - compatible, pass every rule, and donation-history
                     ELIGIBLE right now (ranked by ML + urgency blend)
      not_eligible - compatible but currently in the waiting period
                     (kept visible, each with a short reason)
      excluded     - fail a hard safety filter (incompatible blood group,
                     unavailable, failed screening, age, inactive account)

    Returns {recommended: [...], not_eligible: [...], excluded: [...],
    summary: {...}}."""
    now = now or datetime.now(timezone.utc)
    interval_map = resolve_interval_map(interval_map)
    compatible_groups = set(COMPATIBLE_DONORS.get(blood_group, []))
    p_coords = patient_coords or resolve_coords(None, None, patient_location) \
        or DEFAULT_PATIENT_COORDS

    pool, excluded, not_eligible = [], [], []
    for donor in candidates:
        # 1) Blood-group compatibility (safety-critical, rule-based)
        if not compatible_groups:
            excluded.append({
                "donor": donor,
                "reasons": [f"Unknown blood group '{blood_group}'"],
            })
            continue
        if getattr(donor, "blood_group", None) not in compatible_groups:
            excluded.append({
                "donor": donor,
                "reasons": [f"Blood group {donor.blood_group} cannot donate to {blood_group}"],
            })
            continue
        # 2) Non-history hard filters (availability, screening, age, ...)
        hard = _non_history_reasons(donor)
        if hard:
            excluded.append({"donor": donor, "reasons": hard})
            continue
        # 3) Donation-history eligibility (waiting period, configurable).
        #    Donors still in the cooldown are NOT ranked as primary - they
        #    go into a separate "currently not eligible" bucket with a reason.
        history = donation_eligibility_status(donor, now, interval_map)
        if not history["eligible"]:
            not_eligible.append({"donor": donor, "status": history})
            continue
        pool.append(donor)

    ranked = [score_donor(d, blood_group, p_coords, urgency_level, contact_pref,
                          now, interval_map)
              for d in pool]
    ranked.sort(key=lambda r: (
        -r["score"],
        r["distance_km"] if r["distance_km"] is not None else float("inf"),
        -r["reliability"],
    ))

    recommend_count = min(max(int(units_needed or 1), MIN_RECOMMENDED), MAX_RECOMMENDED)

    recommended = []
    for i, r in enumerate(ranked[:recommend_count], start=1):
        donor = r["donor"]
        recommended.append({
            "rank": i,
            "donor_id": getattr(donor, "id", None),
            "name": getattr(donor, "name", ""),
            "blood_group": getattr(donor, "blood_group", ""),
            "location": getattr(donor, "location", ""),
            "phone": getattr(donor, "phone", ""),
            "contact_method": getattr(donor, "preferred_contact_method", "") or "Phone",
            "distance_km": r["distance_km"],
            "distance_label": r["distance_label"],
            "exact_match": r["exact_match"],
            "eligible": r["eligible"],
            "screened": r["screened"],
            "reliability": r["reliability"],
            "model_score": r["model_score"],
            "score": r["score"],
            "factors": r["factors"],
            # Donation-history eligibility + summary (new in this feature)
            "eligibility_status": r["eligibility_status"],
            "donation_count": r["donation_count"],
            "total_bags": r["total_bags"],
            "last_donation_date": r["last_donation_date"],
            "next_eligible_date": r["next_eligible_date"],
        })

    not_eligible_out = []
    for entry in not_eligible:
        donor = entry["donor"]
        history = entry["status"]
        d_coords = resolve_coords(
            getattr(donor, "latitude", None),
            getattr(donor, "longitude", None),
            getattr(donor, "location", None),
        )
        dkm = None
        if d_coords and p_coords:
            dkm = haversine_km(p_coords[0], p_coords[1], d_coords[0], d_coords[1])
        not_eligible_out.append({
            "donor_id": getattr(donor, "id", None),
            "name": getattr(donor, "name", ""),
            "blood_group": getattr(donor, "blood_group", ""),
            "location": getattr(donor, "location", ""),
            "phone": getattr(donor, "phone", ""),
            "contact_method": getattr(donor, "preferred_contact_method", "") or "Phone",
            "distance_km": dkm,
            "distance_label": format_distance(dkm),
            "status": history["status"],
            "reason": history["reason"],
            "last_donation_date": history["last_donation_date"],
            "next_eligible_date": history["next_eligible_date"],
            "cooldown_days": history["cooldown_days"],
            "days_remaining": history["days_remaining"],
            "donation_count": getattr(donor, "donation_count", 0) or 0,
            "total_bags": donor_total_bags(donor),
        })

    excluded_out = [{
        "donor_id": getattr(e["donor"], "id", None),
        "name": getattr(e["donor"], "name", ""),
        "blood_group": getattr(e["donor"], "blood_group", ""),
        "location": getattr(e["donor"], "location", ""),
        "reasons": e["reasons"],
    } for e in excluded]

    return {
        "recommended": recommended,
        "not_eligible": not_eligible_out,
        "excluded": excluded_out,
        "summary": {
            "blood_group": blood_group,
            "patient_location": patient_location,
            "urgency_level": urgency_level,
            "units_needed": units_needed,
            "compatible_count": len(pool) + len(not_eligible_out),
            "eligible_count": len(pool),
            "not_eligible_count": len(not_eligible_out),
            "excluded_count": len(excluded_out),
            "recommended_count": len(recommended),
            "urgency_boost": URGENCY_BOOST.get(urgency_level, 0.05),
        },
    }
