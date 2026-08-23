"""
donor_import_engine.py
----------------------
Parsing + validation for the admin bulk donor-import feature.

Admins can paste a donor roster (the kind of plain-text / OCR list a blood
donation group shares on Facebook) or a CSV/TSV of donor records and turn
them into donor accounts in one shot.  This module is the Flask-free,
unit-testable half:

- OCR-tolerant blood-group normalization ("0+" -> "O+", "o neg" -> "O-")
- phone cleanup (dashes/spaces, the "8801..." prefix, junk like "!") down
  to a clean 11-digit Bangladeshi mobile number
- a free-form parser that understands the common roster layout
  (SI No. / blood group / area / name / phone, possibly spanning lines,
  with page-count markers like "25/25", footers, and merged rows)
- a CSV/TSV parser for clean tabular pastes
- per-record validation so only well-formed donors reach the database
- deterministic synthetic emails for accounts that have no real email yet

Nothing here touches the database or Flask; the routes in app.py take the
parsed records, show them in an editable preview, and create the accounts.
"""

import csv
import hashlib
import io
import re

from blood_group_engine import VALID_BLOOD_GROUPS, normalize_blood_group

# Default password for imported donor accounts.  Meets the app's password
# policy (>= 8 chars, upper + lower + digit + special) so the account can be
# logged into with the phone number as the username hint if ever needed.
DEFAULT_IMPORT_PASSWORD = "Donor@1234"

# Synthetic email domain used for imported donors who don't have a real
# email address.  The local part is derived from the phone number so
# re-importing the same person is naturally deduplicated.
IMPORT_EMAIL_DOMAIN = "import.lifelink"

# Canonical Dhaka-area names observed in donor rosters plus the main
# city centers already known to the recommendation engine.  Used by the
# free-form parser to split "area" from "name".
AREA_ALIASES = {
    "ullapara": "Ullapara",
    "turag": "Turag",
    "uttara": "Uttara",
    "uttaral": "Uttara",  # OCR typo
    "wari": "Wari",
    "zirabo": "Zirabo",
    "paltan": "Paltan",
    "panthapath": "Panthapath",
    "polashpur": "Polashpur",
    "puran dhaka": "Puran Dhaka",
    "rampura": "Rampura",
    "rayer bazar": "Rayer Bazar",
    "rk mission road": "RK Mission Road",
    "sabujbag": "Sabujbag",
    "savar": "Savar",
    "segun bagicha": "Segun Bagicha",
    "shahbag": "Shahbag",
    "shahbagh": "Shahbag",
    "shahjadpur": "Shahjadpur",
    "shamibag": "Shamibag",
    "shantibag": "Shantibag",
    "shanti nagar": "Shanti Nagar",
    "shyamoli": "Shyamoli",
    "shyampur": "Shyampur",
    "taltola": "Taltola",
    "tejgaon": "Tejgaon",
    "tikkapara": "Tikkapara",
    "tongi": "Tongi",
    "khilkhet": "Khilkhet",
    "konapara": "Konapara",
    "kotwali": "Kotwali",
    "kuril": "Kuril",
    "lalbag": "Lalbag",
    "malibag": "Malibag",
    "mir hajir bag": "Mir Hajir Bag",
    "mirpur": "Mirpur",
    "mogbazar": "Mogbazar",
    "mohakhali": "Mohakhali",
    "mohammadpur": "Mohammadpur",
    "motijheel": "Motijheel",
    "mugdapara": "Mugdapara",
    "dania": "Dania",
    "dhanmondi": "Dhanmondi",
    "dilkusha": "Dilkusha",
    "hazaribag": "Hazaribag",
    "kallyanpur": "Kallyanpur",
    "kamalapur": "Kamalapur",
    "kamarpara": "Kamarpara",
    "badda": "Badda",
    "bashabo": "Bashabo",
    "cantonment": "Cantonment",
    "gulshan": "Gulshan",
    "banani": "Banani",
    "bashundhara": "Bashundhara",
    "khilgaon": "Khilgaon",
    "dhaka": "Dhaka",
    "north south university": "North South University",
}

_NOISE_MARKERS = ("facebook", "www.", "@", "email:", "sino.", "si no",
                  "blood group area", "blood group", "contact no.")
_SI_RE = re.compile(r"\d{3,4}")
_STRAY_DIGITS_RE = re.compile(r"\d{1,2}")
_PAGE_MARKER_RE = re.compile(r"^\d+\s*/\s*\d+")


def clean_phone(raw):
    """Return a clean 11-digit Bangladeshi mobile number or None."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 13 and digits.startswith("8801"):
        digits = digits[2:]  # strip the 88 country code
    if len(digits) == 11 and digits.startswith("01"):
        return digits
    return None


def normalize_ocr_group(raw):
    """OCR-tolerant blood-group normalization: '0+' -> 'O+', 'o neg' -> 'O-',
    'B -' -> 'B-', 'A (II) Rh+' -> 'A+'.  Returns None for non-groups."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper()
    cleaned = cleaned.replace("0", "O").replace("Ø", "O")
    cleaned = cleaned.replace("POSITIVE", "+").replace("NEGATIVE", "-")
    cleaned = cleaned.replace("POS", "+").replace("NEG", "-")
    cleaned = re.sub(r"\([IVX]+\)", "", cleaned)  # European A (II) style
    cleaned = cleaned.replace("(", "").replace(")", "")
    cleaned = re.sub(r"RHESUS", "", cleaned)
    cleaned = re.sub(r"RH\s*D?", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    return normalize_blood_group(cleaned)


def is_noise_line(line):
    """True for headers, footers, page-count markers and symbol garbage."""
    low = (line or "").lower().strip()
    if not low:
        return True
    if any(marker in low for marker in _NOISE_MARKERS):
        return True
    if re.fullmatch(r"\d+\s*/\s*\d+", low):
        return True
    if not re.search(r"[A-Za-z0-9]", low):
        return True  # symbol-only garbage such as "ΣΣ"
    if low in ("si no.", "si no", "blood group", "area", "name",
               "contact no.", "contact no"):
        return True
    return False


def _tokenize(text):
    """Split the pasted text into whitespace tokens, dropping noise lines
    and leading page-count markers.  Returns a flat list of token strings."""
    tokens = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or is_noise_line(line):
            continue
        line = _PAGE_MARKER_RE.sub("", line, count=1).strip()
        for tok in line.split():
            tokens.append(tok)
    return tokens


def _new_record(pending_si, group):
    return {
        "si_no": pending_si,
        "blood_group": group,
        "area": None,
        "name": None,
        "phones": [],
        "warnings": [],
        "raw_group": group,
    }


def _flush(current, records):
    if current and (current["name"] or current["phones"]):
        current["name"] = (current["name"] or "").strip()
        if current["name"] and not current["area"]:
            current["warnings"].append("No recognized area before the name - check it.")
        records.append(current)


def parse_freeform(text):
    """Parse a pasted roster in the common layout:

        <SI no.> <blood group> <area> <name> <phone(s)>

    which may span multiple lines, contain page markers like '25/25',
    merged rows, and OCR noise.  Returns a list of record dicts."""
    tokens = _tokenize(text)
    records = []
    current = None
    pending_si = None
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        group = normalize_ocr_group(tok)
        if group:
            _flush(current, records)
            current = _new_record(pending_si, group)
            pending_si = None
            i += 1
            continue
        phone = clean_phone(tok)
        if phone:
            if current and phone not in current["phones"]:
                current["phones"].append(phone)
            i += 1
            continue
        if _SI_RE.fullmatch(tok):
            pending_si = int(tok)
            i += 1
            continue
        if _STRAY_DIGITS_RE.fullmatch(tok):
            i += 1
            continue
        if current is None:
            i += 1
            continue

        # Longest known-area phrase starting here (up to 4 tokens).
        area_len = None
        for k in range(min(4, n - i), 0, -1):
            phrase = " ".join(tokens[i + j] for j in range(k)).lower()
            if phrase in AREA_ALIASES:
                area_len = k
                break
        if area_len:
            phrase = " ".join(tokens[i + j] for j in range(area_len)).lower()
            if current["area"] is None:
                current["area"] = AREA_ALIASES[phrase]
                i += area_len
            else:
                current["name"] = ((current["name"] or "") + " " + tok).strip()
                i += 1
            continue

        # A name token.
        current["name"] = ((current["name"] or "") + " " + tok).strip()
        i += 1

    _flush(current, records)
    return records


def parse_csv(text):
    """Parse comma- or tab-separated donor rows.

    Expected columns: si_no, blood_group, area, name, phone[, phone2, ...].
    A leading si_no column is optional; extra trailing columns are treated
    as additional phone numbers."""
    records = []
    delimiter = None
    for line in (text or "").splitlines():
        if line.strip():
            delimiter = "\t" if "\t" in line else ","
            break
    if not delimiter:
        return records
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter,
                               skipinitialspace=True))
    except (csv.Error, ValueError):
        rows = []
    for row in rows:
        cells = [c.strip() for c in (row or []) if c is not None]
        if not cells or all(not c for c in cells):
            continue
        si = None
        group_index = None
        for idx, cell in enumerate(cells):
            if normalize_ocr_group(cell):
                group_index = idx
                break
        if group_index is None:
            continue  # not a donor row
        # A numeric cell just before the group is the SI number.
        for idx in range(group_index - 1, -1, -1):
            if cells[idx].isdigit():
                si = int(cells[idx])
                break
        record = _new_record(si, normalize_ocr_group(cells[group_index]))
        after = cells[group_index + 1:]
        if after and not after[0].isdigit():
            record["area"] = AREA_ALIASES.get(after[0].lower(), None)
            if not record["area"]:
                record["name"] = after[0]
            after = after[1:]
        if after and not after[0].isdigit() and record["name"] is None:
            record["name"] = after[0]
            after = after[1:]
        for cell in after:
            phone = clean_phone(cell)
            if phone:
                if phone not in record["phones"]:
                    record["phones"].append(phone)
            elif not cell.isdigit() and len(cell) > 1:
                # A stray text cell after the name (e.g. a second name).
                record["name"] = ((record["name"] or "") + " " + cell).strip()
        _flush(record, records)
    return records


def parse_donor_text(text, mode="auto"):
    """Parse donor text.  mode: 'auto' (detect CSV vs free-form), 'csv',
    or 'freeform'."""
    text = (text or "").strip()
    if not text:
        return []
    if mode == "csv":
        return parse_csv(text)
    if mode == "freeform":
        return parse_freeform(text)
    lines = [ln for ln in text.splitlines() if ln.strip() and not is_noise_line(ln)]
    if not lines:
        return []
    delimited = sum(1 for ln in lines if "," in ln or "\t" in ln)
    if delimited / len(lines) > 0.5:
        return parse_csv(text)
    return parse_freeform(text)


def record_errors(record):
    """Human-readable problems with a parsed record.  An empty list means the
    record is importable as-is."""
    errors = []
    group = record.get("blood_group")
    if not group:
        errors.append("Missing blood group")
    elif group not in VALID_BLOOD_GROUPS:
        errors.append(f"Invalid blood group '{group}'")
    if not (record.get("name") or "").strip():
        errors.append("Missing donor name")
    if not record.get("phones"):
        errors.append("Missing a valid 11-digit phone number")
    return errors


def record_email(record):
    """Deterministic synthetic email for a donor who has no real email yet."""
    phone = record["phones"][0] if record.get("phones") else None
    if phone:
        local = f"donor.{phone}"
    else:
        seed = f"{record.get('name', '')}|{record.get('si_no', '')}"
        local = f"donor.{hashlib.md5(seed.encode()).hexdigest()[:10]}"
    return f"{local}@{IMPORT_EMAIL_DOMAIN}"


def record_primary_phone(record):
    """The first cleaned phone number, or None."""
    phones = record.get("phones") or []
    return phones[0] if phones else None
