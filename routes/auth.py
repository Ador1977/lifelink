"""Authentication routes: register, login, forgot/reset password, logout."""

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
    login_user,
    logout_user,
)

from blood_group_engine import is_valid_blood_group, normalize_blood_group
from models import PasswordResetToken, User, db
from services import (
    _log_auth_event,
    send_email,
    validate_password,
)
from settings import BLOOD_GROUPS, EMAIL_PATTERN, PHONE_PATTERN

logger = logging.getLogger("lifelink")

PASSWORD_RESET_TOKEN_TTL = timedelta(hours=1)


def _utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_password_reset_token(user):
    token = secrets.token_urlsafe(32)
    entry = PasswordResetToken(
        user_id=user.id,
        token=token,
        expires_at=_utc_now_naive() + PASSWORD_RESET_TOKEN_TTL,
    )
    db.session.add(entry)
    db.session.commit()
    return token


def register_routes(app):

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            role = request.form.get("role", "donor")
            blood_group = request.form.get("blood_group", "")
            phone = request.form.get("phone", "").strip()
            location = request.form.get("location", "").strip()
            age = request.form.get("age", type=int)
            gender = request.form.get("gender", "M")

            if not name or not email or not password:
                flash("Please fill in all required fields.", "danger")
                return redirect(url_for("register"))

            if not re.match(EMAIL_PATTERN, email):
                flash("Invalid email address. Please enter a valid email (e.g. name@gmail.com).", "danger")
                return redirect(url_for("register"))

            if not re.match(PHONE_PATTERN, phone):
                flash("Invalid phone number. Phone number must be exactly 11 digits.", "danger")
                return redirect(url_for("register"))

            if User.query.filter_by(email=email).first():
                flash("An account with this email already exists.", "danger")
                return redirect(url_for("register"))

            pw_errors = validate_password(password)
            if pw_errors:
                for err in pw_errors:
                    flash(err, "danger")
                return redirect(url_for("register"))

            if age is not None and (age < 1 or age > 120):
                flash("Please enter a valid age (1-120).", "danger")
                return redirect(url_for("register"))

            if blood_group and not is_valid_blood_group(blood_group):
                flash("Invalid blood group. Choose one of: A+, A-, B+, B-, AB+, AB-, O+, O-.", "danger")
                return redirect(url_for("register"))

            blood_group = normalize_blood_group(blood_group) or ""

            user = User(
                name=name, email=email, role=role, blood_group=blood_group,
                phone=phone, location=location, age=age, gender=gender,
                email_verified=True,
                # No group chosen at registration = nothing to trust yet, so the
                # account starts out "not verified" and the group can only become
                # verified through the card flow or an explicit confirmation.
                blood_group_verified=bool(blood_group),
                blood_group_source="manual",
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            _log_auth_event(user.id, "REGISTER", f"New {role} account created")
            logger.info("New user registered: %s (%s)", email, role)
            flash("Account created successfully! You can now log in.", "success")
            return redirect(url_for("login"))

        return render_template("register.html", blood_groups=BLOOD_GROUPS)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()

            if not user or not user.check_password(password):
                if user:
                    user.record_failed_login()
                    db.session.commit()
                    _log_auth_event(user.id, "LOGIN_FAILED", "Wrong password")
                logger.warning("Failed login attempt for %s", email)
                flash("Invalid email or password.", "danger")
                return redirect(url_for("login"))

            if user.is_locked():
                _log_auth_event(user.id, "LOGIN_BLOCKED", "Account locked")
                flash("Account temporarily locked due to too many failed attempts. Try again in 15 minutes.", "danger")
                return redirect(url_for("login"))

            if user.is_restricted:
                _log_auth_event(user.id, "LOGIN_BLOCKED", "Account restricted")
                flash(
                    "This account has been restricted by an administrator. "
                    "Contact support for assistance.",
                    "danger",
                )
                return redirect(url_for("login"))

            user.clear_failed_logins()
            db.session.commit()

            login_user(user)
            _log_auth_event(user.id, "LOGIN_SUCCESS", "Login successful")
            logger.info("User logged in: %s", user.email)
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            user = User.query.filter_by(email=email, is_deleted=False).first()

            if user:
                token = generate_password_reset_token(user)
                reset_url = url_for("reset_password", token=token, _external=True)
                send_email(
                    "LifeLink - Password Reset",
                    [user.email],
                    f"Hello {user.name},\n\n"
                    f"We received a request to reset your LifeLink password. "
                    f"Use the link below to choose a new password. It is valid "
                    f"for 1 hour:\n\n{reset_url}\n\n"
                    f"If you didn't request this, you can safely ignore this email.\n\n"
                    f"LifeLink Team",
                )
                _log_auth_event(user.id, "PASSWORD_RESET_REQUESTED", "Reset link emailed")
                logger.info("Password reset link sent to %s", user.email)

            flash(
                "If an account exists for that email, a password reset link has been "
                "sent. Please check your inbox (and spam folder).",
                "info",
            )
            return redirect(url_for("login"))

        return render_template("forgot_password.html")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        reset = PasswordResetToken.query.filter_by(token=token).first()

        if not reset or reset.is_used or reset.expires_at < _utc_now_naive():
            flash("This password reset link is invalid or has expired. Please request a new one.", "danger")
            return redirect(url_for("forgot_password"))

        user = User.query.get(reset.user_id)
        if not user or user.is_deleted:
            flash("This account is no longer available.", "danger")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if new_pw != confirm_pw:
                flash("Passwords do not match.", "danger")
                return redirect(url_for("reset_password", token=token))

            pw_errors = validate_password(new_pw)
            if pw_errors:
                for err in pw_errors:
                    flash(err, "danger")
                return redirect(url_for("reset_password", token=token))

            user.set_password(new_pw)
            user.clear_failed_logins()
            reset.is_used = True
            PasswordResetToken.query.filter_by(user_id=user.id, is_used=False).update({"is_used": True})
            db.session.commit()

            _log_auth_event(user.id, "PASSWORD_RESET", "Password reset via email link")
            logger.info("Password reset for %s", user.email)
            flash("Your password has been reset. You can now log in.", "success")
            return redirect(url_for("login"))

        return render_template("reset_password.html", token=token)

    @app.route("/logout")
    @login_required
    def logout():
        _log_auth_event(current_user.id, "LOGOUT", "User logged out")
        logger.info("User logged out: %s", current_user.email)
        logout_user()
        flash("You have been logged out.", "info")
        return redirect(url_for("index"))
