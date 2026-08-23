"""
SQLAlchemy models for the LifeLink platform.

The ``db`` instance is created unbound here and attached to the Flask app in
``app.py`` via ``db.init_app(app)``.
"""

import logging
from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger("lifelink")

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="donor", index=True)
    blood_group = db.Column(db.String(5), index=True)
    # Blood-group verification state (from the uploaded blood-group card).
    # blood_group_verified = False means the stored/auto-detected group still
    # needs user/admin confirmation before it is used for matching.
    blood_group_verified = db.Column(db.Boolean, nullable=False, default=True)
    blood_group_source = db.Column(db.String(20), default="manual")
    blood_group_image = db.Column(db.String(255))
    blood_group_detected = db.Column(db.String(5))
    blood_group_detected_at = db.Column(db.DateTime)
    blood_group_detected_confidence = db.Column(db.Float)
    blood_group_flagged = db.Column(db.Boolean, nullable=False, default=False)
    blood_group_flagged_reason = db.Column(db.String(255))
    blood_group_card_name = db.Column(db.String(150))
    blood_group_card_id = db.Column(db.String(100))
    gender = db.Column(db.String(1), default="M")
    phone = db.Column(db.String(30))
    location = db.Column(db.String(150))
    age = db.Column(db.Integer)
    is_available_donor = db.Column(db.Boolean, default=True)
    last_donation_eligible = db.Column(db.Boolean)
    last_screening_confidence = db.Column(db.Float)
    last_donation_date = db.Column(db.DateTime)
    donation_count = db.Column(db.Integer, default=0)
    reliability_score = db.Column(db.Float, default=0.5)
    preferred_contact_method = db.Column(db.String(20), default="Phone")
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    email_verified = db.Column(db.Boolean, default=False)
    failed_login_count = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    is_deleted = db.Column(db.Boolean, default=False)
    # Temporarily restricted by an admin (cannot log in) without deletion.
    is_restricted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, onupdate=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_locked(self):
        if self.locked_until and self.locked_until > datetime.now(timezone.utc):
            return True
        return False

    def record_failed_login(self):
        self.failed_login_count = (self.failed_login_count or 0) + 1
        if self.failed_login_count >= 5:
            self.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
            logger.warning("Account locked for user %s (too many failures)", self.email)

    def clear_failed_logins(self):
        self.failed_login_count = 0
        self.locked_until = None

    @property
    def unread_notification_count(self):
        return Notification.query.filter_by(user_id=self.id, is_read=False).count()


class AuthLog(db.Model):
    __tablename__ = "auth_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    event = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.String(500))
    ip_address = db.Column(db.String(50))
    user_agent = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", backref="auth_logs")


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_token"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", backref="reset_tokens")


class BloodRequest(db.Model):
    __tablename__ = "blood_request"
    id = db.Column(db.Integer, primary_key=True)
    patient_name = db.Column(db.String(120), nullable=False)
    requester_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    blood_group = db.Column(db.String(5), nullable=False, index=True)
    units_needed = db.Column(db.Integer, nullable=False)
    hospital = db.Column(db.String(200), nullable=False)
    hospital_type = db.Column(db.Integer, default=0)
    condition_score = db.Column(db.Integer, nullable=False)
    condition_source = db.Column(db.String(20), default="manual")
    report_data = db.Column(db.Text)
    hours_needed = db.Column(db.Integer, nullable=False)
    age_risk = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    urgency_level = db.Column(db.String(20), index=True)
    urgency_confidence = db.Column(db.Float)
    status = db.Column(db.String(20), default="Pending", index=True)
    rejection_reason = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, onupdate=lambda: datetime.now(timezone.utc))

    requester = db.relationship("User", backref="requests")


class Notification(db.Model):
    __tablename__ = "notification"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), default="Notification")
    message = db.Column(db.String(500), nullable=False)
    notification_type = db.Column(db.String(30), index=True)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"), index=True)
    link = db.Column(db.String(300))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class DonorAcceptance(db.Model):
    __tablename__ = "donor_acceptance"
    id = db.Column(db.Integer, primary_key=True)
    donor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"), nullable=False)
    status = db.Column(db.String(20), default="Pending")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    donor = db.relationship("User", backref="donor_acceptances")
    blood_request = db.relationship("BloodRequest", backref="acceptances")


class RequestStatusLog(db.Model):
    __tablename__ = "request_status_log"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"), nullable=False, index=True)
    old_status = db.Column(db.String(20))
    new_status = db.Column(db.String(20), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    changed_by = db.relationship("User")


class Hospital(db.Model):
    __tablename__ = "hospital"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    location = db.Column(db.String(200))
    phone = db.Column(db.String(30))
    has_blood_bank = db.Column(db.Boolean, default=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)


class PredictionLog(db.Model):
    __tablename__ = "prediction_log"
    id = db.Column(db.Integer, primary_key=True)
    model_type = db.Column(db.String(50), nullable=False)
    input_features = db.Column(db.Text)
    prediction = db.Column(db.String(50))
    confidence = db.Column(db.Float)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class DonorRecommendationLog(db.Model):
    __tablename__ = "donor_recommendation_log"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"), nullable=False, index=True)
    donor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    rank = db.Column(db.Integer, nullable=False)
    score = db.Column(db.Float, nullable=False)
    distance_km = db.Column(db.Float)
    factors = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    blood_request = db.relationship("BloodRequest", backref="recommendations")
    donor = db.relationship("User", backref="recommendation_logs")


class Donation(db.Model):
    """One completed blood donation. Every donation is recorded as a row so
    the full history is kept (never overwritten)."""
    __tablename__ = "donation"
    id = db.Column(db.Integer, primary_key=True)
    donor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    donation_date = db.Column(db.DateTime, nullable=False, index=True)
    donation_time = db.Column(db.String(5))
    blood_group = db.Column(db.String(5), nullable=False)
    bags = db.Column(db.Integer, nullable=False, default=1)
    donation_location = db.Column(db.String(200))
    next_eligible_date = db.Column(db.DateTime)
    status = db.Column(db.String(20), default="Completed")
    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    donor = db.relationship("User", backref="donations")


class RequestReport(db.Model):
    """A user-flagged blood request (fake/inappropriate). Admins review the
    queue and either clear the reports or reject the request with a reason."""
    __tablename__ = "request_report"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"),
                           nullable=False, index=True)
    reported_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reason = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    request = db.relationship("BloodRequest", backref="reports")
    reporter = db.relationship("User")


class AppSetting(db.Model):
    """Simple key/value settings (e.g. the configurable donation waiting
    period). Keyed by name so new settings can be added without schema
    changes."""
    __tablename__ = "app_setting"
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(200), nullable=False)
    updated_at = db.Column(db.DateTime, onupdate=lambda: datetime.now(timezone.utc))
