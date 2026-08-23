"""
AI-Powered Blood Donation Platform with Urgency Detection
North South University - CSE299 Junior Design Project

Application assembly: creates the Flask app, wires the database/mail/login
extensions, registers all routes (see the ``routes`` package), runs the
startup migrations and seeds, and re-exports the shared symbols (models,
helpers, seed functions) that the test-suite imports from ``app``.

Run with:  python app.py
Then open: http://127.0.0.1:5000
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, url_for
from flask_login import LoginManager, current_user
from flask_sqlalchemy import SQLAlchemy

from ai_engine import urgency_detector
from eligibility_engine import next_eligible_date
from models import (
    AppSetting,
    AuthLog,
    BloodRequest,
    Donation,
    DonorAcceptance,
    DonorRecommendationLog,
    Hospital,
    Notification,
    PasswordResetToken,
    PredictionLog,
    RequestReport,
    RequestStatusLog,
    User,
    db,
)
from recommendation_engine import compute_reliability, resolve_coords
from routes import register_all_routes
from services import (
    _allowed_report_image,
    _as_utc,
    age_risk_from_age,
    donor_eligibility_view,
    extract_urgency_report_values_from_image,
    get_hospitals,
    load_donation_settings,
    mail,
    record_donation,
    role_required,
    admin_required,
    score_condition_from_report,
    sorted_requests_query,
)
from settings import GOOGLE_MAPS_API_KEY, VLM_AVAILABLE

import maps_engine

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lifelink")

# ---------------------------------------------------------------------------
# APP CONFIG
# ---------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "instance", "blood_platform.db"),
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "True") == "True"
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_USERNAME", "")

db.init_app(app)
mail.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# ERROR HANDLERS
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Something went wrong on our end."), 500


# ---------------------------------------------------------------------------
# TEMPLATE CONTEXT
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals():
    hospitals = []
    try:
        hospitals = get_hospitals()
    except Exception:
        pass
    unread_count = 0
    if current_user.is_authenticated:
        unread_count = current_user.unread_notification_count
    return {
        "hospitals": hospitals,
        "unread_notification_count": unread_count,
        "vlm_available": VLM_AVAILABLE,
        "google_maps_key": GOOGLE_MAPS_API_KEY,
    }


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
register_all_routes(app)


# ---------------------------------------------------------------------------
# DATABASE INITIALIZATION + SEED DATA
# ---------------------------------------------------------------------------

# Major public (government) and private hospitals across Bangladesh,
# grouped by division. Format: (name, location, phone, has_blood_bank).

HOSPITALS_SEED = [
    # ---------------- DHAKA DIVISION ----------------
    # Government
    ("Dhaka Medical College Hospital", "Dhaka", "02-55165089", True),
    ("Bangabandhu Sheikh Mujib Medical University", "Dhaka", "02-55165596", True),
    ("Shaheed Suhrawardy Medical College Hospital", "Dhaka", "02-9122133", True),
    ("Sir Salimullah Medical College & Mitford Hospital", "Dhaka", "02-7311201", True),
    ("Mugda Medical College Hospital", "Dhaka", "02-7567866", True),
    ("Shaheed Ahsan Ullah Master General Hospital", "Dhaka", "02-9356201", True),
    ("Kurmitola General Hospital", "Dhaka", "02-8910854", True),
    ("Dhaka Dental College & Hospital", "Dhaka", "02-5861344", True),
    ("National Institute of Cardiovascular Diseases", "Dhaka", "02-8611462", True),
    ("National Institute of Neurosciences & Hospital", "Dhaka", "02-9668816", True),
    ("National Institute of Kidney Diseases & Urology", "Dhaka", "02-9035433", True),
    ("National Institute of Cancer Research & Hospital", "Dhaka", "02-9014822", True),
    ("National Institute of Diseases of the Chest & Hospital", "Dhaka", "02-9032525", True),
    ("Dhaka Shishu Hospital", "Dhaka", "02-8614085", True),
    ("Sheikh Hasina National Institute of Burn & Plastic Surgery", "Dhaka", "02-9011269", True),
    ("Shaheed Tajuddin Ahmad Medical College Hospital", "Gazipur", "02-9291262", True),
    ("Tangail Medical College Hospital", "Tangail", "0921-61465", True),
    ("Faridpur Medical College Hospital", "Faridpur", "0631-63242", True),
    ("Mymensingh Medical College Hospital", "Mymensingh", "091-62145", True),
    ("Sheikh Hasina Medical College", "Jamalpur", "0981-63450", True),
    ("Shaheed M. Monsur Ali Medical College Hospital", "Sirajganj", "0751-62343", True),
    # Private
    ("Square Hospital", "Dhaka", "02-8144400", True),
    ("United Hospital", "Dhaka", "02-8836444", True),
    ("Evercare Hospital Dhaka (Apollo)", "Dhaka", "02-9667800", True),
    ("Labaid Specialized Hospital", "Dhaka", "02-9672111", True),
    ("Ibn Sina Diagnostic & Imaging Center", "Dhaka", "02-8113044", False),
    ("BIRDEM General Hospital", "Dhaka", "02-8616641", True),
    ("Ibrahim Cardiac Hospital & Research Institute", "Dhaka", "02-9118370", True),
    ("Asgar Ali Hospital", "Dhaka", "02-9666222", True),
    ("Japan Bangladesh Friendship Hospital", "Dhaka", "02-8881635", True),
    ("Dhaka Community Hospital", "Dhaka", "02-9350391", True),
    ("Anwar Khan Modern Hospital", "Dhaka", "02-8100251", True),
    ("Ad-Din Women's Medical College Hospital", "Dhaka", "02-9009200", True),
    ("Central Hospital Ltd.", "Dhaka", "02-9896611", True),
    ("Delta Hospital Ltd.", "Dhaka", "02-9339555", True),
    ("Green Life Medical College Hospital", "Dhaka", "02-9897911", True),
    ("Islami Bank Central Hospital", "Dhaka", "02-8333000", True),
    ("National Heart Foundation Hospital & Research Institute", "Dhaka", "02-9674651", True),
    ("Holy Family Red Crescent Medical College Hospital", "Dhaka", "02-8838202", True),
    ("Shahabuddin Medical College Hospital", "Dhaka", "02-9011441", True),
    ("Uttara Adhunik Medical College Hospital", "Dhaka", "02-8911926", True),
    ("Enam Medical College & Hospital", "Savar", "02-7712109", True),
    ("Care Medical College Hospital", "Dhaka", "02-8832082", True),
    ("Popular Medical Center Hospital", "Dhaka", "02-9102677", True),
    ("Ayesha Memorial Hospital", "Dhaka", "02-9555961", True),
    ("Bangladesh Specialized Hospital", "Dhaka", "02-8825215", True),
    ("Tairunnessa Memorial Hospital", "Dhaka", "02-9342391", True),
    ("Ibn Sina Medical College Hospital", "Dhaka", "02-9012323", True),
    ("City Hospital Ltd.", "Dhaka", "02-9112722", True),
    ("Badda Al-Helal Specialized Hospital", "Dhaka", "02-8065566", True),
    ("Impulse Hospital Ltd.", "Dhaka", "02-8057405", True),
    ("Kumudini Welfare Trust Hospital", "Tangail", "09226-56113", True),
    # ---------------- CHATTOGRAM DIVISION ----------------
    ("Chattogram Medical College Hospital", "Chattogram", "031-620331", True),
    ("Cox's Bazar Medical College Hospital", "Cox's Bazar", "0341-52088", True),
    ("Cumilla Medical College Hospital", "Cumilla", "081-65513", True),
    ("Noakhali Medical College Hospital", "Noakhali", "0321-61710", True),
    ("Chittagong Metropolitan Hospital Ltd.", "Chattogram", "031-652152", True),
    ("Parkview Hospital Ltd.", "Chattogram", "031-656572", True),
    ("Evercare Hospital Chattogram", "Chattogram", "031-2550010", True),
    ("Eastern Medical College Hospital", "Cumilla", "081-76446", True),
    # ---------------- SYLHET DIVISION ----------------
    ("Sylhet MAG Osmani Medical College Hospital", "Sylhet", "0821-716000", True),
    ("North East Medical College Hospital", "Sylhet", "0821-720606", True),
    ("Mount Adora Hospital", "Sylhet", "0821-723233", True),
    ("Jalalabad Ragib-Rabeya Medical College Hospital", "Sylhet", "0821-285590", True),
    # ---------------- RAJSHAHI DIVISION ----------------
    ("Rajshahi Medical College Hospital", "Rajshahi", "0721-762230", True),
    ("Pabna Medical College Hospital", "Pabna", "0731-66025", True),
    ("Kushtia Medical College Hospital", "Kushtia", "071-73353", True),
    ("Shaheed Ziaur Rahman Medical College Hospital", "Bogura", "051-66663", True),
    ("Islami Bank Medical College Hospital", "Rajshahi", "0721-750202", True),
    ("Mohammad Ali Hospital", "Bogura", "051-67012", True),
    # ---------------- KHULNA DIVISION ----------------
    ("Khulna Medical College Hospital", "Khulna", "041-762660", True),
    ("Shaheed Sheikh Abu Naser Specialized Hospital", "Khulna", "041-731006", True),
    ("Gazi Medical College Hospital", "Khulna", "041-760723", True),
    # ---------------- BARISHAL DIVISION ----------------
    ("Sher-e-Bangla Medical College Hospital", "Barishal", "0431-63444", True),
    # ---------------- RANGPUR DIVISION ----------------
    ("Rangpur Medical College Hospital", "Rangpur", "0521-65314", True),
    ("Prime Medical College Hospital", "Rangpur", "0521-67128", True),
]


def seed_hospitals():
    added = 0
    for h_name, h_loc, h_phone, h_bb in HOSPITALS_SEED:
        if not Hospital.query.filter_by(name=h_name).first():
            db.session.add(Hospital(
                name=h_name, location=h_loc, phone=h_phone, has_blood_bank=h_bb,
            ))
            added += 1
    db.session.commit()
    if added:
        logger.info("Seeded %d new hospitals.", added)
    return added


def backfill_hospital_coords():
    """Resolve coordinates for hospitals that don't have any yet.

    Uses the built-in offline location table (from each hospital's area) so
    startup stays fast and works without an API key.  Google Geocoding is
    used lazily at page-load when a key is configured."""
    with app.app_context():
        changed = False
        for h in Hospital.query.all():
            if h.latitude is not None and h.longitude is not None:
                continue
            coords = maps_engine.geocode(
                h.location or h.name, prefer_google=False
            )
            if coords:
                h.latitude, h.longitude = coords
                changed = True
        if changed:
            db.session.commit()
            logger.info("Backfilled hospital coordinates (offline).")


def seed_demo_data():
    if User.query.filter_by(email="admin@bloodplatform.com").first():
        return

    admin = User(
        name="System Admin", email="admin@bloodplatform.com", role="admin",
        blood_group="O+", phone="0000000000", location="North South University",
        age=30, gender="M", email_verified=True,
    )
    admin.set_password("admin123")
    db.session.add(admin)

    donor = User(
        name="Rahim Uddin", email="donor@bloodplatform.com", role="donor",
        blood_group="O+", phone="01700000000", location="Dhaka",
        age=27, gender="M", email_verified=True, donation_count=4,
        preferred_contact_method="Phone", latitude=23.8103, longitude=90.4125,
    )
    donor.set_password("donor123")
    db.session.add(donor)

    donor2 = User(
        name="Fatima Khatun", email="donor2@bloodplatform.com", role="donor",
        blood_group="A+", phone="01800000001", location="Dhaka",
        age=24, gender="F", email_verified=True, donation_count=2,
        preferred_contact_method="SMS", latitude=23.8103, longitude=90.4125,
    )
    donor2.set_password("donor123")
    db.session.add(donor2)

    donor3 = User(
        name="Hasan Mahmud", email="donor3@bloodplatform.com", role="donor",
        blood_group="B+", phone="01600000002", location="Bashundhara, Dhaka",
        age=31, gender="M", email_verified=True, donation_count=6,
        preferred_contact_method="Phone", latitude=23.8162, longitude=90.4257,
        last_donation_date=datetime.now(timezone.utc) - timedelta(days=150),
    )
    donor3.set_password("donor123")
    db.session.add(donor3)

    donor4 = User(
        name="Nusrat Jahan", email="donor4@bloodplatform.com", role="donor",
        blood_group="O-", phone="01900000003", location="Mirpur, Dhaka",
        age=29, gender="F", email_verified=True, donation_count=1,
        preferred_contact_method="Email", latitude=23.8091, longitude=90.3521,
    )
    donor4.set_password("donor123")
    db.session.add(donor4)

    donor5 = User(
        name="Tanvir Ahmed", email="donor5@bloodplatform.com", role="donor",
        blood_group="AB+", phone="01500000004", location="Chattogram",
        age=35, gender="M", email_verified=True, donation_count=3,
        preferred_contact_method="Phone", latitude=22.3569, longitude=91.7832,
    )
    donor5.set_password("donor123")
    db.session.add(donor5)

    donor6 = User(
        name="Sadia Rahman", email="donor6@bloodplatform.com", role="donor",
        blood_group="B-", phone="01300000005", location="Uttara, Dhaka",
        age=22, gender="F", email_verified=True, donation_count=0,
        preferred_contact_method="SMS", latitude=23.8759, longitude=90.3795,
        last_donation_eligible=True,
    )
    donor6.set_password("donor123")
    db.session.add(donor6)

    patient = User(
        name="Karim Hossain", email="patient@bloodplatform.com", role="patient",
        blood_group="A+", phone="01800000000", location="Dhaka",
        age=45, gender="M", email_verified=True,
    )
    patient.set_password("patient123")
    db.session.add(patient)

    db.session.flush()

    sample = BloodRequest(
        patient_name="Karim Hossain", requester_id=patient.id,
        blood_group="A+", units_needed=3,
        hospital="Dhaka Medical College Hospital",
        hospital_type=0, condition_score=5, hours_needed=4, age_risk=2,
        notes="Emergency surgery required after road accident.",
    )
    result = urgency_detector.predict(3, 5, 4, 2, 0, 0)
    sample.urgency_level = result["level"]
    sample.urgency_confidence = result["confidence"]
    db.session.add(sample)
    db.session.flush()

    log_entry = RequestStatusLog(
        request_id=sample.id, old_status=None,
        new_status="Pending", changed_by_id=patient.id,
    )
    db.session.add(log_entry)

    db.session.commit()
    logger.info("Demo data seeded successfully.")


DEMO_REQUESTS = [
    # patient_name, requester_email, blood_group, units, hospital, hospital_type,
    # condition_score, hours_needed, age_risk, notes
    ("Ayesha Siddika", "patient@bloodplatform.com", "AB+", 2, "Square Hospital", 1, 4, 4, 2,
     "Post-operative anemia after C-section."),
    ("Rafiqul Islam", "patient@bloodplatform.com", "O+", 4, "Dhaka Medical College Hospital", 0, 5, 2, 2,
     "Road accident, massive blood loss."),
    ("Nusrat Jahan", "donor@bloodplatform.com", "A-", 1, "United Hospital", 1, 2, 12, 1,
     "Chronic kidney disease, routine transfusion."),
    ("Kamal Hossain", "patient@bloodplatform.com", "B+", 3, "Evercare Hospital Dhaka (Apollo)", 1, 3, 6, 2,
     "Liver cirrhosis, variceal bleed."),
    ("Farhana Akter", "donor@bloodplatform.com", "O-", 2, "Labaid Specialized Hospital", 1, 5, 1, 1,
     "Emergency surgery, ruptured ectopic pregnancy."),
    ("Sabbir Ahmed", "patient@bloodplatform.com", "A+", 2, "Chattogram Medical College Hospital", 0, 4, 4, 1,
     "Dengue fever with thrombocytopenia."),
    ("Mithun Chowdhury", "donor2@bloodplatform.com", "B-", 3, "Rajshahi Medical College Hospital", 0, 4, 8, 2,
     "Thalassemia major, regular transfusion."),
    ("Tanvir Rahman", "patient@bloodplatform.com", "O+", 5, "Khulna Medical College Hospital", 0, 5, 3, 2,
     "GI bleed, hemoglobin dropping fast."),
    ("Sharmin Sultana", "donor@bloodplatform.com", "A+", 2, "Sylhet MAG Osmani Medical College Hospital", 0, 3, 24, 3,
     "Preterm infant needs exchange transfusion."),
    ("Jannatul Ferdous", "donor2@bloodplatform.com", "AB-", 1, "Rangpur Medical College Hospital", 0, 2, 48, 1,
     "Routine pre-surgery blood reservation."),
    ("Ashraful Islam", "patient@bloodplatform.com", "B+", 4, "Dhaka Medical College Hospital", 0, 5, 2, 3,
     "Burn injury, blood and plasma needed."),
    ("Moushumi Karmokar", "donor@bloodplatform.com", "O+", 2, "Ibn Sina Diagnostic & Imaging Center", 0, 3, 12, 1,
     "Leukemia patient, platelet support."),
    ("Rakib Hasan", "patient@bloodplatform.com", "A+", 3, "Square Hospital", 1, 4, 6, 2,
     "Gastric carcinoma resection."),
    ("Sadia Afrin", "donor2@bloodplatform.com", "B+", 2, "Cumilla Medical College Hospital", 0, 4, 5, 2,
     "Postpartum hemorrhage."),
    ("Imran Hossain", "patient@bloodplatform.com", "O+", 1, "National Heart Foundation Hospital & Research Institute", 0, 2, 24, 2,
     "Open heart surgery scheduled."),
    ("Yasmin Begum", "donor@bloodplatform.com", "A-", 2, "Sher-e-Bangla Medical College Hospital", 0, 4, 4, 3,
     "Elderly patient, hip fracture surgery."),
    ("Nur Mohammad", "patient@bloodplatform.com", "AB+", 3, "Mymensingh Medical College Hospital", 0, 5, 2, 2,
     "Leukemia induction chemotherapy."),
    ("Tania Parvin", "donor2@bloodplatform.com", "B+", 1, "United Hospital", 1, 1, 72, 1,
     "Hernia repair, blood group reserve only."),
    ("Shahriar Kabir", "patient@bloodplatform.com", "O-", 2, "Pabna Medical College Hospital", 0, 4, 6, 2,
     "Malaria with severe anemia."),
    ("Lima Begum", "donor@bloodplatform.com", "A+", 3, "Cox's Bazar Medical College Hospital", 0, 4, 8, 2,
     "Snake bite with coagulopathy."),
]


def seed_demo_requests():
    if BloodRequest.query.filter(
        BloodRequest.patient_name.in_([r[0] for r in DEMO_REQUESTS])
    ).first():
        return

    added = 0
    for (p_name, req_email, bg, units, hospital, h_type,
         cond_score, hours, age_risk, notes) in DEMO_REQUESTS:
        requester = User.query.filter_by(email=req_email).first()
        if not requester:
            continue
        history_count = BloodRequest.query.filter_by(
            requester_id=requester.id, is_deleted=False
        ).count()
        result = urgency_detector.predict(
            units, cond_score, hours, age_risk, h_type, history_count,
        )
        req = BloodRequest(
            patient_name=p_name, requester_id=requester.id,
            blood_group=bg, units_needed=units, hospital=hospital,
            hospital_type=h_type, condition_score=cond_score,
            condition_source="demo", hours_needed=hours, age_risk=age_risk,
            notes=notes, urgency_level=result["level"],
            urgency_confidence=result["confidence"],
        )
        db.session.add(req)
        db.session.flush()
        db.session.add(RequestStatusLog(
            request_id=req.id, old_status=None,
            new_status="Pending", changed_by_id=requester.id,
        ))
        added += 1

    db.session.commit()
    logger.info("Seeded %d demo blood requests.", added)


DEMO_REPORT_REQUESTS = [
    # patient_name, requester_email, blood_group, units, hospital, hospital_type,
    # hours_needed, notes, extracted (name, age, gender, hemoglobin, wbc, platelet, hematocrit)
    ("Salman Khan", "patient@bloodplatform.com", "O+", 4, "Dhaka Medical College Hospital", 0, 2,
     "Road accident, critical blood loss.",
     {"name": "Salman Khan", "age": 34, "gender": "M", "hemoglobin": 6.8, "wbc": 9.5, "platelet": 180, "hematocrit": 20}),
    ("Rabeya Sultana", "donor@bloodplatform.com", "A+", 3, "Dhaka Medical College Hospital", 0, 6,
     "Dengue with very low platelets.",
     {"name": "Rabeya Sultana", "age": 28, "gender": "F", "hemoglobin": 9.2, "wbc": 6.0, "platelet": 42, "hematocrit": 27}),
    ("Abul Kalam", "patient@bloodplatform.com", "B+", 2, "United Hospital", 1, 4,
     "Severe anemia, needs packed cells.",
     {"name": "Abul Kalam", "age": 61, "gender": "M", "hemoglobin": 7.0, "wbc": 7.0, "platelet": 200, "hematocrit": 21}),
    ("Nasrin Begum", "donor2@bloodplatform.com", "A-", 2, "Square Hospital", 1, 12,
     "Chronic infection with falling hemoglobin.",
     {"name": "Nasrin Begum", "age": 47, "gender": "F", "hemoglobin": 11.5, "wbc": 18.4, "platelet": 210, "hematocrit": 34}),
    ("Rashedul Islam", "patient@bloodplatform.com", "AB+", 3, "Evercare Hospital Dhaka (Apollo)", 1, 3,
     "Acute leukemia, platelet transfusion.",
     {"name": "Rashedul Islam", "age": 19, "gender": "M", "hemoglobin": 8.5, "wbc": 22.0, "platelet": 25, "hematocrit": 25}),
    ("Parvin Akter", "donor@bloodplatform.com", "O+", 1, "Labaid Specialized Hospital", 1, 48,
     "Pre-surgery blood group reservation.",
     {"name": "Parvin Akter", "age": 40, "gender": "F", "hemoglobin": 13.5, "wbc": 7.5, "platelet": 240, "hematocrit": 40}),
    ("Jahir Uddin", "patient@bloodplatform.com", "B-", 3, "Chattogram Medical College Hospital", 0, 5,
     "Symptomatic anemia, weakness.",
     {"name": "Jahir Uddin", "age": 55, "gender": "M", "hemoglobin": 10.2, "wbc": 8.0, "platelet": 150, "hematocrit": 31}),
    ("Sumaiya Haque", "donor2@bloodplatform.com", "O+", 5, "Mymensingh Medical College Hospital", 0, 2,
     "Upper GI bleed, rapid drop in hemoglobin.",
     {"name": "Sumaiya Haque", "age": 50, "gender": "F", "hemoglobin": 6.0, "wbc": 10.0, "platelet": 160, "hematocrit": 18}),
    ("Delwar Hossain", "patient@bloodplatform.com", "A+", 2, "Rajshahi Medical College Hospital", 0, 8,
     "ITP, low platelet count.",
     {"name": "Delwar Hossain", "age": 33, "gender": "M", "hemoglobin": 12.0, "wbc": 6.5, "platelet": 90, "hematocrit": 36}),
    ("Mamunur Rashid", "donor@bloodplatform.com", "B+", 4, "Khulna Medical College Hospital", 0, 2,
     "Sepsis with severe anemia.",
     {"name": "Mamunur Rashid", "age": 58, "gender": "M", "hemoglobin": 7.8, "wbc": 32.0, "platelet": 120, "hematocrit": 23}),
]


def seed_demo_report_requests():
    if BloodRequest.query.filter(
        BloodRequest.patient_name.in_([r[0] for r in DEMO_REPORT_REQUESTS])
    ).first():
        return

    added = 0
    for (p_name, req_email, bg, units, hospital, h_type, hours, notes, extracted) in DEMO_REPORT_REQUESTS:
        requester = User.query.filter_by(email=req_email).first()
        if not requester:
            continue
        score, findings = score_condition_from_report(extracted)
        if score is None:
            continue
        age_risk = age_risk_from_age(extracted.get("age")) or 1
        history_count = BloodRequest.query.filter_by(
            requester_id=requester.id, is_deleted=False
        ).count()
        result = urgency_detector.predict(
            units, score, hours, age_risk, h_type, history_count,
        )
        import json as _json
        report_data_json = _json.dumps({
            "status": "ok",
            "extracted": extracted,
            "findings": {k: list(v) for k, v in findings.items()},
            "raw_reply": "{}",
        }, default=str)
        req = BloodRequest(
            patient_name=p_name, requester_id=requester.id,
            blood_group=bg, units_needed=units, hospital=hospital,
            hospital_type=h_type, condition_score=score,
            condition_source="report", report_data=report_data_json,
            hours_needed=hours, age_risk=age_risk, notes=notes,
            urgency_level=result["level"], urgency_confidence=result["confidence"],
        )
        db.session.add(req)
        db.session.flush()
        db.session.add(RequestStatusLog(
            request_id=req.id, old_status=None,
            new_status="Pending", changed_by_id=requester.id,
        ))
        added += 1

    db.session.commit()
    logger.info("Seeded %d demo report blood requests.", added)


def backfill_donor_attributes():
    """One-time enrichment so the recommendation engine works on existing
    data: fills in coordinates from the free-text location, sensible
    defaults for donation/reliability/contact fields."""
    with app.app_context():
        changed_any = False
        for d in User.query.filter(User.role == "donor", User.is_deleted == False).all():
            changed = False
            if d.latitude is None or d.longitude is None:
                coords = resolve_coords(None, None, d.location)
                if coords:
                    d.latitude, d.longitude = coords
                    changed = True
            if d.donation_count is None:
                d.donation_count = 0
                changed = True
            if d.reliability_score is None:
                d.reliability_score = compute_reliability(d)
                changed = True
            if not d.preferred_contact_method:
                d.preferred_contact_method = "Phone"
                changed = True
            if changed:
                changed_any = True
        if changed_any:
            db.session.commit()
            logger.info("Backfilled donor attributes for the recommendation engine.")


def backfill_donation_history():
    """Migration for the donation-history feature: existing donors have a
    donation_count / last_donation_date but no Donation rows (the history
    table is new). Build a consistent past history once so the new
    profile/admin pages have real data to show. Runs only if no history
    exists yet."""
    if Donation.query.first():
        return
    settings = load_donation_settings()
    now = datetime.now(timezone.utc)
    changed = False
    for d in User.query.filter(User.role == "donor", User.is_deleted == False).all():
        count = d.donation_count or 0
        if count <= 0:
            continue
        last = _as_utc(d.last_donation_date)
        if last is None:
            last = now - timedelta(days=count * 120)
        d.last_donation_date = last
        for i in range(count):
            donation_date = last - timedelta(days=i * 120)
            db.session.add(Donation(
                donor_id=d.id,
                donation_date=donation_date,
                donation_time=donation_date.strftime("%H:%M"),
                blood_group=d.blood_group or "Unknown",
                bags=1,
                donation_location=d.location or "Not specified",
                next_eligible_date=next_eligible_date(donation_date, d.gender, settings),
                status="Completed",
                notes="Backfilled from pre-existing record",
            ))
        changed = True
    if changed:
        db.session.commit()
        logger.info("Backfilled donation history for existing donors.")


def seed_extra_demo_donor():
    """Adds one demo donor who donated recently, so the 'Currently Not
    Eligible' flow has data to show out of the box. Idempotent."""
    if User.query.filter_by(email="donor7@bloodplatform.com").first():
        return
    settings = load_donation_settings()
    d7 = User(
        name="Mehedi Hasan", email="donor7@bloodplatform.com", role="donor",
        blood_group="O+", phone="01400000006", location="Bashundhara, Dhaka",
        age=33, gender="M", email_verified=True, donation_count=5,
        preferred_contact_method="Phone", latitude=23.8162, longitude=90.4257,
    )
    d7.set_password("donor123")
    db.session.add(d7)
    db.session.flush()
    recent = datetime.now(timezone.utc) - timedelta(days=25)
    d7.last_donation_date = recent
    for i in range(5):
        donation_date = recent - timedelta(days=i * 120)
        db.session.add(Donation(
            donor_id=d7.id,
            donation_date=donation_date,
            donation_time=donation_date.strftime("%H:%M"),
            blood_group="O+", bags=1,
            donation_location="Bashundhara Blood Bank",
            next_eligible_date=next_eligible_date(donation_date, "M", settings),
            status="Completed", notes="Demo donation history",
        ))
    db.session.commit()
    logger.info("Seeded extra demo donor with recent donation history.")


with app.app_context():
    db.create_all()

    inspector = db.inspect(db.engine)
    blood_request_cols = {c["name"] for c in inspector.get_columns("blood_request")}
    if "condition_source" not in blood_request_cols:
        db.session.execute(db.text(
            "ALTER TABLE blood_request ADD COLUMN condition_source VARCHAR(20) DEFAULT 'manual'"
        ))
    if "report_data" not in blood_request_cols:
        db.session.execute(db.text("ALTER TABLE blood_request ADD COLUMN report_data TEXT"))
    if "rejection_reason" not in blood_request_cols:
        db.session.execute(db.text("ALTER TABLE blood_request ADD COLUMN rejection_reason TEXT"))
    db.session.commit()

    # Status workflow migration: "Verified" was renamed to "Approved" and two
    # new statuses ("Under Review", "Rejected") were introduced.
    db.session.execute(db.text(
        "UPDATE blood_request SET status = 'Approved' WHERE status = 'Verified'"
    ))
    db.session.commit()

    # New donor-recommendation fields. db.create_all() only creates missing
    # *tables*, so existing databases need these columns added manually.
    user_cols = {c["name"] for c in inspector.get_columns("user")}
    user_column_ddl = {
        "last_donation_date": "DATETIME",
        "donation_count": "INTEGER DEFAULT 0",
        "reliability_score": "FLOAT DEFAULT 0.5",
        "preferred_contact_method": "VARCHAR(20) DEFAULT 'Phone'",
        "latitude": "FLOAT",
        "longitude": "FLOAT",
        # Blood-group card verification columns.  Existing rows default to
        # verified=True because their group came from registration (manual,
        # user-confirmed input); only new card-based detections start out
        # unverified until the user/admin confirms them.
        "blood_group_verified": "BOOLEAN DEFAULT 1",
        "blood_group_source": "VARCHAR(20) DEFAULT 'manual'",
        "blood_group_image": "VARCHAR(255)",
        "blood_group_detected": "VARCHAR(5)",
        "blood_group_detected_at": "DATETIME",
        "blood_group_detected_confidence": "FLOAT",
        "blood_group_flagged": "BOOLEAN DEFAULT 0",
        "blood_group_flagged_reason": "VARCHAR(255)",
        "blood_group_card_name": "VARCHAR(150)",
        "blood_group_card_id": "VARCHAR(100)",
        "is_restricted": "BOOLEAN DEFAULT 0",
    }
    for col_name, ddl in user_column_ddl.items():
        if col_name not in user_cols:
            db.session.execute(db.text(
                f"ALTER TABLE user ADD COLUMN {col_name} {ddl}"
            ))
    db.session.commit()

    # Notification center metadata columns.
    notification_cols = {c["name"] for c in inspector.get_columns("notification")}
    notification_column_ddl = {
        "title": "VARCHAR(120) DEFAULT 'Notification'",
        "notification_type": "VARCHAR(30)",
        "request_id": "INTEGER",
    }
    for col_name, ddl in notification_column_ddl.items():
        if col_name not in notification_cols:
            db.session.execute(db.text(
                f"ALTER TABLE notification ADD COLUMN {col_name} {ddl}"
            ))
    db.session.commit()

    # Hospital coordinates (for Google Maps + accurate patient<->donor
    # distances).  db.create_all() only creates missing tables, so existing
    # databases need these columns added manually too.
    hospital_cols = {c["name"] for c in inspector.get_columns("hospital")}
    for col_name, ddl in {"latitude": "FLOAT", "longitude": "FLOAT"}.items():
        if col_name not in hospital_cols:
            db.session.execute(db.text(
                f"ALTER TABLE hospital ADD COLUMN {col_name} {ddl}"
            ))
    db.session.commit()

    seed_hospitals()
    backfill_hospital_coords()
    seed_demo_data()
    seed_demo_requests()
    seed_demo_report_requests()
    backfill_donor_attributes()
    backfill_donation_history()
    seed_extra_demo_donor()


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    print("\n" + "=" * 60)
    print(" AI-Powered Blood Donation Platform")
    print(" Starting server at: http://127.0.0.1:5000")
    print(" Demo logins:")
    print("   Admin   -> admin@bloodplatform.com   / admin123")
    print("   Donor   -> donor@bloodplatform.com   / donor123")
    print("   Patient -> patient@bloodplatform.com / patient123")
    print("=" * 60 + "\n")
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
