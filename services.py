"""
Shared service layer for the LifeLink platform.

Holds the domain helpers (eligibility, notifications, recommendation
pipeline, VLM image extraction, RBAC decorators) so route modules stay thin
and the logic is testable independently of Flask route wiring.
"""

import io
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import flash, redirect, request, url_for
from flask_login import current_user, login_required
from flask_mail import Mail, Message

import maps_engine
from blood_group_engine import parse_vlm_reply
from eligibility_engine import (
    DONATION_INTERVAL_DAYS,
    donation_eligibility_status,
    interval_days_for,
    next_eligible_date,
)
from models import (
    db,
    AppSetting,
    AuthLog,
    BloodRequest,
    Donation,
    DonorAcceptance,
    DonorRecommendationLog,
    Hospital,
    Notification,
    PredictionLog,
    RequestReport,
    RequestStatusLog,
    User,
)
from recommendation_engine import (
    COMPATIBLE_DONORS,
    compute_reliability,
    recommend_donors,
    resolve_coords,
)
from settings import (
    ALLOWED_REPORT_EXTENSIONS,
    BLOOD_GROUP_UPLOAD_DIR,
    CHAT_MODEL,
    GOOGLE_MAPS_API_KEY,
    URGENCY_ORDER,
    VLM_AVAILABLE,
    VLM_MODEL,
    Image,
    UnidentifiedImageError,
    ollama,
)

logger = logging.getLogger("lifelink")

mail = Mail()


# ---------------------------------------------------------------------------
# HELPERS - GENERAL
# ---------------------------------------------------------------------------

def validate_password(password):
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password):
        errors.append("Password must contain at least one digit.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        errors.append("Password must contain at least one special character.")
    return errors


def log_prediction(model_type, features, prediction, confidence, user_id=None):
    entry = PredictionLog(
        model_type=model_type,
        input_features=str(features),
        prediction=str(prediction),
        confidence=confidence,
        user_id=user_id,
    )
    db.session.add(entry)


def create_notification(user_id, message, link=None, title=None,
                        notification_type=None, request_id=None):
    """Add an in-app notification for one user.  The title/type/request link
    metadata powers the enhanced notification center."""
    notif = Notification(
        user_id=user_id,
        title=title or "Notification",
        message=message,
        notification_type=notification_type,
        request_id=request_id,
        link=link,
    )
    db.session.add(notif)


def notify_admins(message, link=None, title=None, notification_type=None,
                  request_id=None):
    """Fan out a notification to every active admin account."""
    admins = User.query.filter_by(role="admin", is_deleted=False).all()
    for admin in admins:
        create_notification(
            admin.id, message, link=link, title=title,
            notification_type=notification_type, request_id=request_id,
        )


def send_email(subject, recipients, body):
    try:
        msg = Message(subject=subject, recipients=recipients)
        msg.body = body
        mail.send(msg)
    except Exception as e:
        logger.error("Failed to send email to %s: %s", recipients, e)


def sorted_requests_query():
    reqs = BloodRequest.query.filter(
        BloodRequest.status != "Cancelled",
        BloodRequest.is_deleted == False,
    ).all()
    reqs.sort(key=lambda r: (URGENCY_ORDER.get(r.urgency_level, 5), -r.id))
    return reqs


def get_hospitals():
    return Hospital.query.order_by(Hospital.name).all()


# ---------------------------------------------------------------------------
# HELPERS - AI DONOR RECOMMENDATION
# ---------------------------------------------------------------------------

def _request_patient_location(req):
    """Best-effort patient location for distance ranking: the requester's
    registered location, else the hospital's location, else Dhaka."""
    if req.requester and req.requester.location:
        return req.requester.location
    hosp = Hospital.query.filter_by(name=req.hospital).first()
    if hosp and hosp.location:
        return hosp.location
    return "Dhaka"


def _hospital_coords(hospital_name):
    """Best-effort (lat, lng) for a hospital.

    Uses the stored hospital coordinates when available; otherwise resolves
    the hospital's area through the location resolver (Google Geocoding API
    when a key is configured, else the offline city/area table)."""
    if not hospital_name:
        return None
    hosp = Hospital.query.filter_by(name=hospital_name).first()
    if hosp:
        if hosp.latitude is not None and hosp.longitude is not None:
            return round(hosp.latitude, 6), round(hosp.longitude, 6)
        if hosp.location:
            coords = maps_engine.geocode(hosp.location, key=GOOGLE_MAPS_API_KEY)
            if coords:
                return coords
    return maps_engine.geocode(hospital_name, key=GOOGLE_MAPS_API_KEY)


def _request_distance_km(req, donor_coords):
    """Approximate distance (km) between a donor and a request's location.
    Uses the requester's registered coords when available, else the hospital
    coords, else None (unknown)."""
    from recommendation_engine import haversine_km
    if not donor_coords:
        return None
    lat, lng = donor_coords
    if lat is None or lng is None:
        return None
    patient_coords = None
    if req.requester:
        patient_coords = resolve_coords(
            req.requester.latitude, req.requester.longitude,
            req.requester.location,
        )
    if patient_coords is None:
        patient_coords = _hospital_coords(req.hospital)
    if not patient_coords:
        return None
    return haversine_km(lat, lng, patient_coords[0], patient_coords[1])


def _run_recommendations(req, contact_pref=""):
    """Run the AI recommendation pipeline for a request. Returns the engine
    result dict (recommended / not_eligible / excluded / summary)."""
    # Patient's stored profile coordinates are the distance reference point
    # when available; otherwise the hospital's coordinates are used (or the
    # engine falls back to the free-text location).
    patient_coords = None
    if req.requester:
        patient_coords = resolve_coords(
            req.requester.latitude, req.requester.longitude,
            req.requester.location,
        )
    if patient_coords is None:
        patient_coords = _hospital_coords(req.hospital)
    return recommend_donors(
        blood_group=req.blood_group,
        candidates=User.query.filter_by(role="donor", is_deleted=False).all(),
        patient_location=_request_patient_location(req),
        urgency_level=req.urgency_level,
        units_needed=req.units_needed,
        contact_pref=contact_pref,
        patient_coords=patient_coords,
        interval_map=load_donation_settings(),
    )


def _log_recommendations(req, result):
    """Snapshot the recommendation output so it can be audited later."""
    DonorRecommendationLog.query.filter_by(request_id=req.id).delete()
    for r in result["recommended"]:
        db.session.add(DonorRecommendationLog(
            request_id=req.id,
            donor_id=r["donor_id"],
            rank=r["rank"],
            score=r["score"],
            distance_km=r["distance_km"],
            factors=json.dumps(r["factors"], default=str),
        ))
    db.session.commit()


# ---------------------------------------------------------------------------
# HELPERS - DONATION HISTORY & ELIGIBILITY
# ---------------------------------------------------------------------------

# AppSetting keys -> gender letter used by the eligibility engine.
DONATION_SETTING_KEYS = {
    "donation_interval_male": "M",
    "donation_interval_female": "F",
}


def load_donation_settings():
    """Load the configured donation waiting period (gender -> days) from the
    AppSetting table.  Empty/missing entries fall back to the defaults in
    eligibility_engine.DONATION_INTERVAL_DAYS."""
    settings = {}
    rows = AppSetting.query.filter(AppSetting.key.in_(list(DONATION_SETTING_KEYS))).all()
    for row in rows:
        gender = DONATION_SETTING_KEYS.get(row.key)
        if not gender:
            continue
        try:
            days = int(row.value)
        except (TypeError, ValueError):
            continue
        if days > 0:
            settings[gender] = days
    return settings


def _as_utc(dt):
    """Normalize a possibly-naive datetime to aware UTC (SQLite stores naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def record_donation(donor, bags=1, location=None, donation_date=None,
                    time_str=None, status="Completed", notes=None,
                    increment_count=True):
    """Append a donation to a donor's history and update their profile.

    - Keeps every past record (rows are never overwritten)
    - Auto-computes the next eligible date from the configurable waiting period
    - Syncs donor.last_donation_date and donation_count"""
    donation_date = _as_utc(donation_date or datetime.now(timezone.utc))
    settings = load_donation_settings()
    next_eligible = next_eligible_date(donation_date, donor.gender, settings)
    bags = max(1, int(bags or 1))
    record = Donation(
        donor_id=donor.id,
        donation_date=donation_date,
        donation_time=time_str or donation_date.strftime("%H:%M"),
        blood_group=donor.blood_group or "Unknown",
        bags=bags,
        donation_location=location or donor.location or "Not specified",
        next_eligible_date=next_eligible,
        status=status,
        notes=notes,
    )
    db.session.add(record)
    donor.last_donation_date = donation_date
    if increment_count:
        donor.donation_count = (donor.donation_count or 0) + 1
    return record


def donor_total_bags(donor):
    """Total bags donated (sum across the history)."""
    donations = list(getattr(donor, "donations", None) or [])
    if donations:
        return sum((d.bags or 0) for d in donations)
    return donor.donation_count or 0


def donor_eligibility_view(donor):
    """Enriched eligibility info used by templates/APIs:
    status + history totals + configured waiting period."""
    settings = load_donation_settings()
    status = donation_eligibility_status(donor, interval_map=settings)
    donations = list(getattr(donor, "donations", None) or [])
    return {
        "donor": donor,
        "status": status,
        "total_donations": len(donations) if donations else (donor.donation_count or 0),
        "total_bags": donor_total_bags(donor),
        "interval_days": interval_days_for(donor.gender, settings),
    }


def _maybe_notify_eligibility_restored(donor):
    """Notify a donor once when a completed donation's waiting period has
    passed and they become eligible to donate again.  Idempotent: the check
    keys off the donation's next_eligible_date so each re-eligibility event
    produces at most one notification."""
    if donor.role != "donor":
        return
    status = donation_eligibility_status(
        donor, interval_map=load_donation_settings()
    )
    if not status["eligible"]:
        return
    donations = [d for d in (getattr(donor, "donations", None) or [])
                 if d.next_eligible_date is not None]
    if not donations:
        return
    latest = max(donations, key=lambda d: _as_utc(d.donation_date) or _as_utc(d.next_eligible_date))
    restored_at = _as_utc(latest.next_eligible_date)
    if restored_at > datetime.now(timezone.utc):
        return
    already_sent = Notification.query.filter_by(
        user_id=donor.id, notification_type="eligible_again"
    ).filter(
        Notification.created_at > latest.next_eligible_date
    ).first()
    if already_sent:
        return
    create_notification(
        donor.id,
        f"You are eligible to donate again (waiting period completed on "
        f"{restored_at.strftime('%d %b %Y')}). Open blood requests are on your dashboard.",
        url_for("donor_dashboard"),
        title="Eligible to Donate Again",
        notification_type="eligible_again",
    )
    db.session.commit()


# ---------------------------------------------------------------------------
# HELPERS - AUTH LOGGING
# ---------------------------------------------------------------------------

def _log_auth_event(user_id, event, detail=None):
    log = AuthLog(
        user_id=user_id,
        event=event,
        detail=detail,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", "")[:300],
    )
    db.session.add(log)
    db.session.commit()


# ---------------------------------------------------------------------------
# HELPERS - ROLE BASED ACCESS CONTROL (RBAC)
# ---------------------------------------------------------------------------

def role_required(*roles):
    """Decorator: allow the route only for users whose role is in `roles`.

    All authorization is enforced on the backend here (never trusted from the
    UI).  A disallowed user is redirected to their role dashboard."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                flash("You do not have permission to access that page.", "danger")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


admin_required = role_required("admin")


# ---------------------------------------------------------------------------
# HELPERS - REPORT IMAGE EXTRACTION (VLM)
# ---------------------------------------------------------------------------

def _allowed_report_image(filename):
    """Accept report images. Known extensions (jpg/jpeg/png, any case) pass
    immediately; anything else isn't hard-rejected here because the real
    validation happens when Pillow opens the file — so a perfectly valid
    PNG/JPG/JPEG with an unusual or renamed extension is still accepted."""
    if not filename:
        return False
    if "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_REPORT_EXTENSIONS:
        return True
    return True


def _prepare_report_image(file_storage):
    """Open, normalize (EXIF-transpose), and downsize an uploaded report
    image. Returns (status, png_bytes). status is 'vlm_unavailable' if the
    vision packages aren't installed, 'bad_image' if the file isn't readable,
    otherwise 'ok'."""
    if not VLM_AVAILABLE:
        logger.warning("VLM requested but the 'ollama' / 'Pillow' packages are not installed.")
        return "vlm_unavailable", None

    try:
        image = Image.open(file_storage.stream)
        image.load()
    except (UnidentifiedImageError, OSError):
        return "bad_image", None

    from PIL import ImageOps
    image = ImageOps.exif_transpose(image.convert("RGB"))
    max_side = 1024
    if max(image.width, image.height) > max_side:
        scale = max_side / max(image.width, image.height)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "ok", buf.getvalue()


def _call_vlm(prompt, image_bytes):
    """Call the local Qwen2.5-VL model via Ollama with an image + prompt.
    Returns (status, reply_text)."""
    try:
        response = ollama.chat(
            model=VLM_MODEL,
            messages=[{
                "role": "user",
                "content": prompt,
                "images": [image_bytes],
            }],
            options={"temperature": 0, "num_predict": 150},
            keep_alive="30m",
        )
    except ConnectionError:
        logger.error(
            "Could not reach the local Ollama server. Start it with 'ollama serve' "
            "(or make sure the Ollama app is running)."
        )
        return "vlm_unreachable", None
    except Exception as exc:
        message = str(exc).lower()
        if "not found" in message or "404" in message:
            logger.error(
                "Model '%s' isn't pulled in Ollama yet. Run: ollama pull %s",
                VLM_MODEL, VLM_MODEL,
            )
            return "vlm_model_missing", None
        logger.exception("Unexpected error calling the Qwen2.5-VL model via Ollama.")
        return "vlm_unreachable", None

    reply = ""
    if hasattr(response, "message"):
        reply = response.message.content or ""
    elif isinstance(response, dict):
        reply = response.get("message", {}).get("content", "") or ""
    return "ok", reply


# ---------------------------------------------------------------------------
# HELPERS - FAQ CHATBOT (local text LLM via Ollama)
# ---------------------------------------------------------------------------

# Shown as quick-reply buttons the moment a user opens the chat widget, so
# they don't have to think of a question first. Kept short and specific to
# what this platform actually does.
CHATBOT_COMMON_QUESTIONS = [
    "How long do I have to wait between blood donations?",
    "Am I eligible to donate blood?",
    "How does the AI donor matching work?",
    "How is the urgency of a request decided?",
    "How do I create a blood request?",
    "How is my blood group verified?",
]

# Grounding context given to the model on every turn so its answers stay
# specific to this platform instead of generic web knowledge. Deliberately
# plain text (not a prompt-injection risk) and short enough to stay cheap.
_CHATBOT_SYSTEM_PROMPT = """You are the LifeLink Assistant, a friendly FAQ helper embedded in the \
LifeLink AI-powered blood donation platform. Answer ONLY questions about how \
this platform works, blood donation eligibility, donation prep, and general \
blood-donation facts. Keep answers short (2-5 sentences), clear, and \
reassuring.

Platform facts you can rely on:
- Donors, Patients, and Admins each have accounts. Patients/hospitals create \
blood requests; the platform's AI scores each request's urgency as Critical, \
High, Medium, or Low based on the details given, and the triage board sorts \
by urgency automatically.
- The AI donor recommendation system ranks nearby donors for a request by \
blood-group compatibility, distance, past reliability, and current \
eligibility - it does not just show every donor.
- Donation eligibility is calculated automatically from a donor's donation \
history: the default waiting period is 90 days for men and 120 days for \
women after a whole-blood donation (an admin can adjust this platform-wide). \
A donor who is compatible but still waiting shows up as "Currently Not \
Eligible" with the date they become eligible again.
- Donors can verify their blood group by uploading a photo of their \
blood-group/donor card; an AI vision model reads it, and only reads that \
match one of the 8 standard groups (A+, A-, B+, B-, AB+, AB-, O+, O-) are \
accepted automatically - anything blurry or uncertain is flagged for admin \
review instead of guessed.
- An AI health-screening check looks at hemoglobin, blood pressure, weight, \
pulse, and age against standard safe-donation thresholds and explains why \
someone is or isn't currently eligible.
- General donation prep tips you can give: stay hydrated, eat iron-rich food \
beforehand, get a good night's sleep, avoid donating on an empty stomach, \
and avoid heavy exercise right after donating.

IMPORTANT rules:
- You are not a doctor. Never diagnose a medical condition or tell someone \
they are definitely eligible/ineligible for donation - point them to the \
platform's Health Screening page or a real medical professional for anything \
specific to their own health.
- If a question is unrelated to blood donation or this platform, politely \
say that's outside what you can help with here.
- Do not invent features, statistics, or policies not listed above."""

# User-facing text for the chat widget when the model can't be reached, so
# the UI never shows a raw error or hangs silently.
CHATBOT_FALLBACK_MESSAGES = {
    "llm_unavailable": (
        "The assistant isn't set up on this server yet (the 'ollama' package "
        "isn't installed). You can still browse the Health Screening or "
        "Requests pages directly."
    ),
    "llm_unreachable": (
        "I can't reach the local AI model right now. Make sure Ollama is "
        "running ('ollama serve', or open the Ollama app) and try again."
    ),
    "llm_model_missing": (
        f"The chat model isn't downloaded yet. Run 'ollama pull {CHAT_MODEL}' "
        "on the server, then try again."
    ),
}


def call_chatbot_llm(message, history, user_role):
    """Call the local text LLM (Ollama) for the FAQ chatbot.

    ``history`` is a list of prior {"role": "user"|"assistant", "content": str}
    turns from this browser session (frontend keeps it short). Returns
    (status, reply_text); status is "ok" or one of the CHATBOT_FALLBACK_MESSAGES
    keys.
    """
    if not VLM_AVAILABLE:  # same optional-dependency flag; ollama package covers both
        logger.warning("Chatbot requested but the 'ollama' package is not installed.")
        return "llm_unavailable", None

    role_note = {
        "donor": "The person asking is logged in as a Donor.",
        "patient": "The person asking is logged in as a Patient.",
        "admin": "The person asking is logged in as an Admin.",
        "guest": "The person asking is not logged in yet.",
    }.get(user_role, "")

    messages = [{"role": "system", "content": _CHATBOT_SYSTEM_PROMPT + "\n\n" + role_note}]
    # Only keep the last few turns so the prompt stays small and cheap.
    for turn in history[-6:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:800]})
    messages.append({"role": "user", "content": message[:800]})

    try:
        response = ollama.chat(
            model=CHAT_MODEL,
            messages=messages,
            options={"temperature": 0.4, "num_predict": 300},
            keep_alive="30m",
        )
    except ConnectionError:
        logger.error(
            "Could not reach the local Ollama server for the chatbot. "
            "Start it with 'ollama serve' (or open the Ollama app)."
        )
        return "llm_unreachable", None
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg:
            logger.error(
                "Chat model '%s' isn't pulled in Ollama yet. Run: ollama pull %s",
                CHAT_MODEL, CHAT_MODEL,
            )
            return "llm_model_missing", None
        logger.exception("Unexpected error calling the chat model via Ollama.")
        return "llm_unreachable", None

    reply = ""
    if hasattr(response, "message"):
        reply = response.message.content or ""
    elif isinstance(response, dict):
        reply = response.get("message", {}).get("content", "") or ""
    return "ok", reply.strip()


# ---------------------------------------------------------------------------
# HELPERS - BLOOD GROUP CARD EXTRACTION
# ---------------------------------------------------------------------------

# Blood-group card uploads use the same local vision model as the lab-report
# uploads.  The deterministic parsing/decision logic lives in
# blood_group_engine.py (unit-tested); this section only orchestrates the
# image handling and the safe association with the logged-in account.

BLOOD_GROUP_VLM_ERROR_MESSAGES = {
    "vlm_unavailable": "Image scanning isn't set up on this server (the ollama/"
                       "Pillow packages aren't installed). Please confirm your "
                       "blood group manually.",
    "vlm_unreachable": "Couldn't reach the local vision model server. Make sure "
                       "Ollama is running, then try again.",
    "vlm_model_missing": "The vision model isn't downloaded yet. Run 'ollama pull "
                         f"{VLM_MODEL}' then try again.",
    "bad_image": "That file doesn't look like a valid image. Please upload a JPG, "
                 "JPEG or PNG photo/scan of your blood group card.",
}


def _prepare_image_bytes(file_storage):
    """Validate + normalize any uploaded image (EXIF-transpose, downsize to a
    side of at most 1024px, encode as PNG).  Returns (status, png_bytes)
    where status is 'ok' or 'bad_image'."""
    try:
        from PIL import ImageOps
        image = Image.open(file_storage.stream)
        image.load()
    except (UnidentifiedImageError, OSError, Exception):
        return "bad_image", None
    image = ImageOps.exif_transpose(image.convert("RGB"))
    max_side = 1024
    if max(image.width, image.height) > max_side:
        scale = max_side / max(image.width, image.height)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "ok", buf.getvalue()


def _save_blood_group_image(png_bytes, user_id):
    """Persist the normalized blood-group card image under uploads/ so the
    original remains available for later verification.  Returns the stored
    filename (a random name, never derived from user input)."""
    filename = f"bg_{user_id}_{secrets.token_hex(8)}.png"
    path = os.path.join(BLOOD_GROUP_UPLOAD_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(png_bytes)
    return filename


def _extract_blood_group_vlm(image_bytes):
    """Ask the local vision model to read the blood group card.  Returns
    (status, parsed, raw_reply) where parsed is the dict from
    blood_group_engine.parse_vlm_reply."""
    prompt = (
        "You are reading a blood-group / donor identity card image for a blood "
        "donation platform. Extract the blood group printed on the card.\n"
        "- blood_group: the blood group. Use EXACTLY one of A+, A-, B+, B-, "
        "AB+, AB-, O+, O- or null if it can't be read\n"
        "- blood_groups: an array of ALL blood groups visible in the image if "
        "more than one appears (otherwise an empty array)\n"
        "- confidence: a number 0-1 for how sure you are about blood_group\n"
        "- card_holder_name: the donor's name printed on the card (or null)\n"
        "- card_id: the donor/blood card number printed on the card (or null)\n"
        "- found: true only if a blood group is clearly visible\n"
        "Reply with ONLY a JSON object and nothing else, in this exact form: "
        '{"found": true/false, "blood_group": <one of the 8 or null>, '
        '"blood_groups": [<all visible groups>], "confidence": <0-1 or null>, '
        '"card_holder_name": <string or null>, "card_id": <string or null>}. '
        "If the image is blurry, low quality, rotated or unclear, set found=false, "
        "blood_group=null and confidence=0 - never guess."
    )
    status, reply = _call_vlm(prompt, image_bytes)
    if status != "ok":
        return status, None, None
    return "ok", parse_vlm_reply(reply), reply


def _set_detection_state(user, parsed, decision, filename):
    """Persist what was detected, without touching the verified blood group."""
    user.blood_group_image = filename
    user.blood_group_detected = decision["blood_group"] if decision else None
    user.blood_group_detected_at = datetime.now(timezone.utc)
    user.blood_group_detected_confidence = (
        decision["confidence"] if decision else None
    )
    user.blood_group_card_name = parsed.get("card_holder_name") if parsed else None
    user.blood_group_card_id = parsed.get("card_id") if parsed else None


def extract_report_values_from_image(file_storage):
    """Use a local vision-language model (Qwen2.5-VL, served by Ollama) to
    read the donor's name, age, gender, and hemoglobin from an uploaded
    blood/lab report image - plus LDL/HDL cholesterol if the report
    happens to show them.

    Weight, pulse, and blood pressure aren't part of a standard lab report
    (weight is from registration/a scale, BP and pulse are from a physical
    check-up), so those stay as manual fields on the upload form.

    Returns a 3-tuple: (status, data, raw_reply)
      status is one of:
        "ok"                - all required fields were found (name/ldl/hdl
                               may still be None - they're optional)
        "incomplete"        - the model replied but is missing one or more
                               required fields (data has whatever it found)
        "vlm_unavailable"   - the 'ollama' / 'Pillow' packages aren't installed
        "vlm_unreachable"   - couldn't reach the local Ollama server
        "vlm_model_missing" - Ollama is running but the model isn't pulled
        "bad_image"         - the uploaded file isn't a readable image
      data is a dict with keys: name, age, gender, hemoglobin, ldl, hdl
      (any may be None). None for non-"ok"/"incomplete" statuses.
      raw_reply is the model's raw text reply (or None), useful for
      debugging what it actually saw.
    """
    if not VLM_AVAILABLE:
        logger.warning("VLM requested but the 'ollama' / 'Pillow' packages are not installed.")
        return "vlm_unavailable", None, None

    status, image_bytes = _prepare_report_image(file_storage)
    if status != "ok":
        return status, None, None

    prompt = (
        "You are reading a medical lab/blood test report image for a blood "
        "donation eligibility check. Extract these fields if visible:\n"
        "- name: the patient's full name\n"
        "- age: in years\n"
        "- gender: reply as exactly \"M\" or \"F\"\n"
        "- hemoglobin (may be labeled Hb or HGB): in g/dL\n"
        "- ldl: LDL cholesterol, in mg/dL, only if shown on the report\n"
        "- hdl: HDL cholesterol, in mg/dL, only if shown on the report\n"
        "Reply with ONLY a JSON object and nothing else, in this exact form: "
        '{"name": <string or null>, "age": <number or null>, "gender": '
        '<"M"/"F" or null>, "hemoglobin": <number or null>, "ldl": '
        '<number or null>, "hdl": <number or null>}. Use null for any field '
        "that isn't visible in the image - never guess."
    )

    status, reply = _call_vlm(prompt, image_bytes)
    if status != "ok":
        return status, None, None

    parsed = {}
    json_match = re.search(r"\{.*\}", reply, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            parsed = {}

    def as_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def as_gender(value):
        if not value or not isinstance(value, str):
            return None
        v = value.strip().lower()
        if v.startswith("f"):
            return "F"
        if v.startswith("m"):
            return "M"
        return None

    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        name = None

    age = as_float(parsed.get("age"))
    hemoglobin = as_float(parsed.get("hemoglobin"))
    gender = as_gender(parsed.get("gender"))
    ldl = as_float(parsed.get("ldl"))
    hdl = as_float(parsed.get("hdl"))

    data = {
        "name": name.strip() if name else None,
        "age": int(age) if age is not None else None,
        "gender": gender,
        "hemoglobin": hemoglobin,
        "ldl": int(ldl) if ldl is not None else None,
        "hdl": int(hdl) if hdl is not None else None,
    }

    required = ["name", "age", "gender", "hemoglobin"]
    if any(data[f] is None for f in required):
        return "incomplete", data, reply

    return "ok", data, reply


def extract_urgency_report_values_from_image(file_storage):
    """Use the local vision model to read the key blood-count values from a
    patient's lab report (hemoglobin, WBC, platelets, hematocrit, plus
    name/age/gender) so the request's condition severity and urgency can be
    scored from the patient's actual test results instead of a manual guess.

    Returns (status, data, raw_reply):
      status: 'ok' | 'incomplete' | 'vlm_unavailable' | 'vlm_unreachable'
              | 'vlm_model_missing' | 'bad_image'
      data: dict with keys name, age, gender, hemoglobin, wbc, platelet,
            hematocrit (any may be None). None for non-'ok' statuses.
    """
    if not VLM_AVAILABLE:
        logger.warning("VLM requested but the 'ollama' / 'Pillow' packages are not installed.")
        return "vlm_unavailable", None, None

    status, image_bytes = _prepare_report_image(file_storage)
    if status != "ok":
        return status, None, None

    prompt = (
        "You are reading a patient's blood test / CBC lab report image to "
        "assess how urgently they need a blood transfusion. Extract these "
        "fields if visible:\n"
        "- name: the patient's full name\n"
        "- age: in years\n"
        "- gender: reply as exactly \"M\" or \"F\"\n"
        "- hemoglobin (may be labeled Hb or HGB): in g/dL\n"
        "- wbc: white blood cell count, in 10^9/L or thousands/ul (numbers "
        "like 7.5, not the unit)\n"
        "- platelet: platelet count, in 10^9/L or thousands/ul (numbers like "
        "180)\n"
        "- hematocrit (may be labeled HCT or PCV): in % (numbers like 42)\n"
        "Reply with ONLY a JSON object and nothing else, in this exact form: "
        '{"name": <string or null>, "age": <number or null>, "gender": '
        '<"M"/"F" or null>, "hemoglobin": <number or null>, "wbc": <number '
        "or null>, \"platelet\": <number or null>, \"hematocrit\": <number "
        "or null>}. Use null for any field that isn't visible in the image - "
        "never guess."
    )

    status, reply = _call_vlm(prompt, image_bytes)
    if status != "ok":
        return status, None, None

    parsed = {}
    json_match = re.search(r"\{.*\}", reply, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            parsed = {}

    def as_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def as_gender(value):
        if not value or not isinstance(value, str):
            return None
        v = value.strip().lower()
        if v.startswith("f"):
            return "F"
        if v.startswith("m"):
            return "M"
        return None

    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        name = None

    data = {
        "name": name.strip() if name else None,
        "age": int(as_float(parsed.get("age"))) if as_float(parsed.get("age")) is not None else None,
        "gender": as_gender(parsed.get("gender")),
        "hemoglobin": as_float(parsed.get("hemoglobin")),
        "wbc": as_float(parsed.get("wbc")),
        "platelet": as_float(parsed.get("platelet")),
        "hematocrit": as_float(parsed.get("hematocrit")),
    }

    return "ok", data, reply


def _severity_buckets():
    """Marker -> function mapping a lab value to a 1-5 severity bucket
    (5 = most severe). Used by score_condition_from_report."""
    return {
        "hemoglobin": [
            (5, lambda v: v < 8),
            (4, lambda v: v < 10),
            (3, lambda v: v < 12),
            (2, lambda v: v < 14),
            (1, lambda v: True),
        ],
        "wbc": [
            (5, lambda v: v < 2 or v > 30),
            (4, lambda v: v < 4 or v > 20),
            (3, lambda v: v < 5 or v > 15),
            (2, lambda v: v > 11),
            (1, lambda v: True),
        ],
        "platelet": [
            (5, lambda v: v < 20 or v > 700),
            (4, lambda v: v < 50 or v > 500),
            (3, lambda v: v < 100 or v > 400),
            (2, lambda v: v < 150),
            (1, lambda v: True),
        ],
        "hematocrit": [
            (5, lambda v: v < 18 or v > 55),
            (4, lambda v: v < 22),
            (3, lambda v: v < 28),
            (2, lambda v: v < 36 or v > 48),
            (1, lambda v: True),
        ],
    }


def score_condition_from_report(data):
    """Convert the lab values read from a report into a 1-5 condition
    severity score. The single most abnormal marker drives the score (max),
    mirroring how triage prioritizes the worst parameter.

    Returns (score, findings) where score is None if no usable markers were
    read, and findings maps each marker to (value, bucket)."""
    findings = {}
    score = None
    for marker, buckets in _severity_buckets().items():
        value = data.get(marker)
        if value is None:
            continue
        bucket = 1
        for bucket_score, matches in buckets:
            if matches(value):
                bucket = bucket_score
                break
        findings[marker] = (value, bucket)
        score = bucket if score is None else max(score, bucket)
    return score, findings


def age_risk_from_age(age):
    """Map a patient's age to the same 1-3 risk factor used by the form."""
    if age is None:
        return None
    if age < 5 or age >= 65:
        return 3
    if age < 18 or age >= 50:
        return 2
    return 1
