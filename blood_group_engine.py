"""
blood_group_engine.py
---------------------
RULE-BASED extraction logic for reading a blood group from an uploaded
blood-group / donor-card image.

The heavy lifting (reading the pixels) is done by a local vision-language
model (Ollama + Qwen2.5-VL) exactly like the existing lab-report uploads.
This module contains the *deterministic* half of the feature and is kept
Flask-free so it can be unit-tested in isolation:

- blood-group validation / normalization (only the 8 real groups are ever
  stored)
- scanning raw model text (or any OCR text) for blood-group tokens
- parsing the VLM's structured JSON reply
- deciding whether a detection is trustworthy: a single confident group
  is accepted; a blurry image, a low-confidence answer, no answer, or
  *conflicting* groups are never guessed -- they become "verification
  required" instead

Design rules (mirroring the requirements):
- Only valid groups (A+/A-/B+/B-/AB+/AB-/O+/O-) are ever stored.
- We never guess: uncertain detections are flagged for manual review.
- A verified blood group on the account is never overwritten here;
  changing it requires explicit user/admin confirmation (handled by the
  routes, not by this module).
"""

import json
import re

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
VALID_BLOOD_GROUPS = frozenset(BLOOD_GROUPS)

# Extracts a blood group from free text.  Handles:
#   B+   B-   AB+   O negative   AB RhD Positive   A (II) Rh+
# and rejects ambiguous tokens such as "0+" (zero) or standalone "B".
_BG_PATTERN = re.compile(
    r"\b(ab|a|b|o)\b"                       # AB/A/B/O (longest first)
    r"(?:\s*\([ivx]+\))?"                   # European roman-numeral, e.g. A (II)
    r"(?:\s*(?:rhesus|rh\s*(?:d\s*)?))?"    # "rh", "rh d", "rhesus"
    r"\s*([+\-−–—]|positive|negative|pos|neg)",
    re.IGNORECASE,
)

_SIGN_TOKENS = {
    "positive": "+", "pos": "+",
    "negative": "-", "neg": "-",
}


def is_valid_blood_group(value):
    """True only for the eight real blood groups (any casing/spacing)."""
    return normalize_blood_group(value) is not None


def normalize_blood_group(value):
    """Return the canonical form (e.g. 'B +' -> 'B+', 'o neg' -> 'O-') or
    None when the value is missing/not one of the eight real groups."""
    if not value or not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", "", value.strip())
    letter = cleaned[:2].upper() if cleaned[:2].upper() == "AB" else cleaned[:1].upper()
    rest = cleaned[2:] if letter == "AB" else cleaned[1:]
    if letter not in ("A", "B", "O", "AB"):
        return None
    if rest == "+":
        sign = "+"
    elif rest == "-":
        sign = "-"
    else:
        lowered = rest.lower()
        sign = _SIGN_TOKENS.get(lowered)
        if sign is None:
            return None
    group = letter + sign
    return group if group in VALID_BLOOD_GROUPS else None


def scan_text_for_blood_groups(text):
    """Find every blood group mentioned in a piece of text (OCR output,
    model replies, etc.).  Returns a set of normalized groups so conflicts
    between multiple readings can be detected."""
    if not text:
        return set()
    groups = set()
    for match in _BG_PATTERN.finditer(text):
        letter = match.group(1)
        sign_token = match.group(2)
        if sign_token in ("+", "-"):
            sign = sign_token
        else:
            sign = _SIGN_TOKENS.get(sign_token.lower())
        if sign:
            groups.add(letter.upper() + sign)
    return groups


def _as_confidence(value):
    """Coerce a model confidence (0-1 or 0-100) to a 0-1 float or None."""
    if value is None:
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf > 1.0:
        conf = conf / 100.0
    if conf < 0.0:
        conf = 0.0
    if conf > 1.0:
        conf = 1.0
    return round(conf, 4)


def _as_text(value):
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def parse_vlm_reply(reply_text):
    """Turn the VLM's raw reply into a structured dict.

    The model is asked to return JSON like:
      {"found": bool, "blood_group": <one of the 8 or null>,
       "blood_groups": [<all visible groups>], "confidence": 0-1,
       "card_holder_name": str|null, "card_id": str|null}

    If the JSON is missing or malformed we fall back to scanning the raw
    text, so a well-behaved but non-JSON reply still contributes.

    Returns: {"found": bool, "blood_group": str|None, "candidates": set,
              "confidence": float|None, "card_holder_name": str|None,
              "card_id": str|None, "json_parsed": bool}
    """
    parsed = {}
    json_parsed = False
    if reply_text:
        json_match = re.search(r"\{.*\}", reply_text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                json_parsed = True
            except json.JSONDecodeError:
                parsed = {}

    candidates = set()
    if json_parsed:
        for key in ("blood_group", "blood_groups"):
            raw = parsed.get(key)
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    group = normalize_blood_group(item)
                    if group:
                        candidates.add(group)
            else:
                group = normalize_blood_group(raw)
                if group:
                    candidates.add(group)

    # Backstop: scan the raw model text for anything that looks like a group.
    candidates |= scan_text_for_blood_groups(reply_text)

    found = parsed.get("found", False)
    if isinstance(found, str):
        found = found.strip().lower() in ("true", "yes", "1")

    return {
        "found": bool(found) or len(candidates) > 0,
        "blood_group": next(iter(sorted(candidates))) if len(candidates) == 1 else None,
        "candidates": candidates,
        "confidence": _as_confidence(parsed.get("confidence")),
        "card_holder_name": _as_text(parsed.get("card_holder_name") or parsed.get("name")),
        "card_id": _as_text(parsed.get("card_id") or parsed.get("donor_id")),
        "json_parsed": json_parsed,
    }


def decide_extraction(parsed, confidence_threshold=0.6):
    """Decide whether a detection is trustworthy.

    Never guesses:
      - no blood group found           -> verification_required
      - more than one group mentioned  -> verification_required (conflict)
      - confidence below the threshold -> verification_required
      - exactly one confident group    -> ok

    Returns {"status": "ok"|"verification_required", "blood_group": str|None,
             "confidence": float|None, "reason": str|None}.
    """
    candidates = parsed.get("candidates") or set()
    if not candidates:
        return {
            "status": "verification_required",
            "blood_group": None,
            "confidence": None,
            "reason": "No blood group could be confidently identified in the image. "
                      "Please confirm your blood group manually.",
        }

    if len(candidates) > 1:
        shown = ", ".join(sorted(candidates))
        return {
            "status": "verification_required",
            "blood_group": None,
            "confidence": None,
            "reason": f"Conflicting blood groups detected ({shown}). "
                      "Please confirm your blood group manually.",
        }

    group = next(iter(candidates))
    confidence = parsed.get("confidence") or 0.0
    if confidence < confidence_threshold:
        return {
            "status": "verification_required",
            "blood_group": group,
            "confidence": confidence,
            "reason": f"Low confidence ({confidence:.0%}) reading the image. "
                      f"Please confirm {group} manually.",
        }

    return {
        "status": "ok",
        "blood_group": group,
        "confidence": confidence,
        "reason": None,
    }


def _tokens(name):
    return [t for t in re.sub(r"[^a-z ]+", " ", (name or "").lower()).split() if t]


def name_similar(a, b):
    """Best-effort match between the name printed on a blood-group card and
    the account name.  Used to keep extracted information tied to the right
    account: a mismatch flags the record for manual review instead of
    silently associating the card with the wrong user."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    fa, fb = "".join(ta), "".join(tb)
    if fa in fb or fb in fa:
        return True
    return ta[0] == tb[0]
