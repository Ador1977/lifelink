"""
maps_engine.py
--------------
Google Maps helpers for the LifeLink blood-donation platform.

Everything here is deliberately Flask-free and works *with or without* a
Google Maps API key:

- With a key (``GOOGLE_MAPS_API_KEY`` in .env): hospital and free-text
  locations are geocoded through the Google Geocoding API, map embeds use
  the Maps Embed API (place mode), and the donor-search page loads the
  interactive Maps JavaScript API.
- Without a key: geocoding falls back to the built-in Bangladesh city/area
  table (shared with the recommendation engine), and the map embeds use the
  keyless ``https://www.google.com/maps?q=...&output=embed`` form, so every
  page still works offline.

Public API:

- api_key()            - the configured Google Maps API key ('' if none)
- geocode(query, key, prefer_google) - best-effort (lat, lng) for a place
- embed_url(query, coords, key, zoom) - iframe embed URL for a map
- directions_url(coords, query)       - "Get Directions" link
- maps_search_url(query)              - "Open in Google Maps" link
- maps_js_loader(key)                 - script src for the Maps JS API
"""

import json
import os
import urllib.parse
import urllib.request

from recommendation_engine import resolve_coords

# Small in-memory cache so the same place isn't geocoded repeatedly
# (Google Geocoding API calls are metered, and page loads are the hot path).
_GOOGLE_CACHE = {}


def _key():
    """The Google Maps API key from the environment, or ''."""
    return os.getenv("GOOGLE_MAPS_API_KEY", "") or ""


def api_key():
    """Public getter for the configured Google Maps API key ('' if none)."""
    return _key()


def _geocode_google(query, key):
    """Geocode a place through the Google Geocoding API.  Returns
    (lat, lng) rounded to 6 decimals, or None on any failure."""
    cached = _GOOGLE_CACHE.get(query)
    if cached is not None:
        return cached

    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode({
        "address": query,
        "key": key,
    })
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        _GOOGLE_CACHE[query] = None
        return None

    if data.get("status") != "OK":
        _GOOGLE_CACHE[query] = None
        return None

    for result in data.get("results", []):
        loc = result.get("geometry", {}).get("location")
        if loc is not None and loc.get("lat") is not None and loc.get("lng") is not None:
            coords = (round(float(loc["lat"]), 6), round(float(loc["lng"]), 6))
            _GOOGLE_CACHE[query] = coords
            return coords

    _GOOGLE_CACHE[query] = None
    return None


def geocode(query, key=None, prefer_google=True):
    """Best-effort (lat, lng) for a place string.

    With a key and prefer_google, the Google Geocoding API is tried first;
    whatever fails (or when there is no key) falls back to the built-in
    Bangladesh city/area table so offline use always works."""
    query = (query or "").strip()
    if not query:
        return None

    key = (key if key is not None else _key()) or ""
    if prefer_google and key:
        coords = _geocode_google(query, key)
        if coords:
            return coords

    coords = resolve_coords(None, None, query)
    if coords:
        return coords
    return None


def embed_url(query=None, coords=None, key=None, zoom=14):
    """An iframe-ready Google Maps URL for a place.

    With a key: Maps Embed API ``place`` mode (nicer map, searchable label).
    Without a key: the keyless ``https://www.google.com/maps?q=...&output=embed``
    embed.  Returns None when there is nothing to show."""
    key = (key if key is not None else _key()) or ""
    q = None
    if query and query.strip():
        q = query.strip()
    elif coords:
        q = f"{coords[0]},{coords[1]}"
    if not q:
        return None

    if key:
        return "https://www.google.com/maps/embed/v1/place?" + urllib.parse.urlencode({
            "key": key,
            "q": q,
            "zoom": zoom,
            "maptype": "roadmap",
        })
    return "https://www.google.com/maps?q=" + urllib.parse.quote(q) + "&output=embed&z=" + str(zoom)


def directions_url(coords=None, query=None):
    """A keyless 'Get Directions' URL that opens the Google Maps web app
    (browser-side, no API key needed). Returns None when nothing to show."""
    dest = None
    if coords:
        dest = f"{coords[0]},{coords[1]}"
    elif query and query.strip():
        dest = query.strip()
    if not dest:
        return None
    return "https://www.google.com/maps/dir/?api=1&destination=" + urllib.parse.quote(dest)


def maps_search_url(query):
    """A keyless 'Open in Google Maps' search URL for a place string."""
    query = (query or "").strip()
    if not query:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


def maps_js_loader(key=None):
    """The <script src> URL for the interactive Maps JavaScript API.
    Returns None when no key is configured (the API can't run keyless)."""
    key = (key if key is not None else _key()) or ""
    if not key:
        return None
    return "https://maps.googleapis.com/maps/api/js?key=" + urllib.parse.quote(key)
