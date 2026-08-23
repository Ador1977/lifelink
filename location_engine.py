"""
location_engine.py
------------------
Offline geocoding helpers for the profile location feature.

Users pick their location either by letting the browser find their
GPS/current position or by searching/selecting a known place.  Either
way we store the resulting latitude/longitude on the profile so the AI
recommendation system can compute real patient <-> donor distances.

Everything here is Flask-free and works offline (no external geocoding
API / API keys), using the built-in Bangladesh location table shared with
the recommendation engine:

- validate_coordinates  - strict numeric bounds check (lat -90..90,
                          lng -180..180)
- parse_coord_pair      - turn a "23.8103, 90.4125" string into floats
- resolve_location_coords - coords for a free-text location (typed or
                          picked from search), falls back to the shared
                          CITY_COORDS table
- search_locations     - autocomplete-style search over known places
- reverse_geocode      - nearest known place for a GPS (lat, lng) point
"""

import re

from recommendation_engine import CITY_COORDS, haversine_km, resolve_coords

# Maximum distance (km) a GPS fix may be from the nearest known place for
# reverse geocoding to still label it with that place's name.
REVERSE_MAX_KM = 25.0

# Places that resolve to the same coordinates are deduplicated so the
# search/select dropdown doesn't list both "cox's bazar" and "cox'sbazar".
_PLACES = None


def _title(name):
    """'dhanmondi' -> 'Dhanmondi'; "cox's bazar" -> "Cox's Bazar"."""
    return " ".join(
        w[:1].upper() + w[1:] for w in (name or "").strip().split()
    )


def _places():
    global _PLACES
    if _PLACES is None:
        seen = {}
        for key, (lat, lng) in CITY_COORDS.items():
            key_l = key.strip().lower()
            if not key_l or key_l in seen:
                continue
            seen[key_l] = True
            _PLACES = _PLACES or []
            _PLACES.append({"name": _title(key_l), "latitude": lat,
                            "longitude": lng})
        _PLACES.sort(key=lambda p: p["name"])
    return _PLACES


def validate_coordinates(latitude, longitude):
    """True if both values are finite numbers within geographic bounds."""
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return False
    return True


def parse_coord_pair(text):
    """Try to parse a 'lat, lng' pair from a free-text string.

    Returns (lat, lng) floats or None.  Accepts '23.8103, 90.4125',
    '23.8103 90.4125', a leading 'GPS:' prefix, and the 'GPS (23.8, 90.4)'
    label written by gps_label()."""
    if not text:
        return None
    text = text.strip()
    # Strip an optional leading "GPS"/"GPS:"/"GPS (" marker and a trailing ")".
    text = re.sub(r"(?i)^gps\s*:?\s*\(?", "", text)
    text = text.rstrip(")")
    parts = re.split(r"[,\s]+", text.strip())
    if len(parts) < 2:
        return None
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except (TypeError, ValueError):
        return None
    if not validate_coordinates(lat, lng):
        return None
    return lat, lng


def resolve_location_coords(location):
    """Best-effort (lat, lng) for a location string.  Tries an explicit
    'lat, lng' pair first, then the shared CITY_COORDS table."""
    pair = parse_coord_pair(location)
    if pair:
        return round(pair[0], 6), round(pair[1], 6)
    coords = resolve_coords(None, None, location)
    if coords:
        return round(coords[0], 6), round(coords[1], 6)
    return None


def search_locations(query, limit=8):
    """Case-insensitive autocomplete over known Bangladesh places.

    Returns [{name, latitude, longitude}, ...] where the whole name
    contains the query (substring) or every word starts with a query
    word (prefix).  Short queries act as simple prefixes."""
    query = (query or "").strip().lower()
    if len(query) < 2:
        return []
    words = [w for w in query.split() if w]
    if not words:
        return []
    matches = []
    for place in _places():
        name = place["name"].lower()
        if query in name or all(name.startswith(w) or (" " + w) in name
                                for w in words):
            matches.append(place)
            if len(matches) >= limit:
                break
    return matches


def reverse_geocode(latitude, longitude, max_km=REVERSE_MAX_KM):
    """Nearest known place for a GPS point.

    Returns (place_name, distance_km) or (None, None) when the closest
    known place is farther than max_km away (we keep the raw GPS label
    then rather than a misleading place name)."""
    if not validate_coordinates(latitude, longitude):
        return None, None
    lat, lng = float(latitude), float(longitude)
    best_name, best_km = None, None
    for place in _places():
        km = haversine_km(lat, lng, place["latitude"], place["longitude"])
        if best_km is None or km < best_km:
            best_km = km
            best_name = place["name"]
    if best_km is None or best_km > max_km:
        return None, None
    return best_name, round(best_km, 2)


def gps_label(latitude, longitude):
    """Readable label for a GPS fix that maps to no known place."""
    return f"GPS ({float(latitude):.4f}, {float(longitude):.4f})"
