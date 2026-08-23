"""Profile routes: profile editing, blood-group card upload/confirmation,
protected card-image serving and the location (GPS/search) APIs."""

import logging
import re
from datetime import datetime

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required

from blood_group_engine import (
    decide_extraction,
    is_valid_blood_group,
    name_similar,
    normalize_blood_group,
)
from eligibility_engine import DONATION_INTERVAL_DAYS
from location_engine import (
    gps_label,
    resolve_location_coords,
    reverse_geocode,
    search_locations,
    validate_coordinates,
)
from models import Donation, User, db
from recommendation_engine import compute_reliability
from services import (
    BLOOD_GROUP_VLM_ERROR_MESSAGES,
    _as_utc,
    _extract_blood_group_vlm,
    _log_auth_event,
    _prepare_image_bytes,
    _save_blood_group_image,
    _set_detection_state,
    donor_eligibility_view,
    load_donation_settings,
    record_donation,
    validate_password,
)
from settings import (
    BLOOD_GROUPS,
    BLOOD_GROUP_UPLOAD_DIR,
    PHONE_PATTERN,
    VLM_AVAILABLE,
)

logger = logging.getLogger("lifelink")


def register_routes(app):

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.method == "POST":
            form_type = request.form.get("form_type", "")

            if form_type == "update_profile":
                name = request.form.get("name", "").strip()
                phone = request.form.get("phone", "").strip()
                location = request.form.get("location", "").strip()
                blood_group = request.form.get("blood_group", "")
                age = request.form.get("age", type=int)
                gender = request.form.get("gender", current_user.gender)
                contact_method = request.form.get("preferred_contact_method", "Phone").strip()
                last_donation_date = request.form.get("last_donation_date", "").strip()

                if not re.match(PHONE_PATTERN, phone):
                    flash("Invalid phone number. Phone number must be exactly 11 digits.", "danger")
                    return redirect(url_for("profile"))

                if name:
                    current_user.name = name
                current_user.phone = phone
                current_user.location = location
                if blood_group:
                    # A manually selected blood group counts as the user confirming
                    # it: store the validated value and mark it verified.
                    if not is_valid_blood_group(blood_group):
                        flash("Invalid blood group. Choose one of: A+, A-, B+, B-, AB+, AB-, O+, O-.", "danger")
                        return redirect(url_for("profile"))
                    current_user.blood_group = normalize_blood_group(blood_group)
                    current_user.blood_group_verified = True
                    current_user.blood_group_source = "manual"
                    current_user.blood_group_flagged = False
                    current_user.blood_group_flagged_reason = None
                if age and 1 <= age <= 120:
                    current_user.age = age
                current_user.gender = gender
                current_user.preferred_contact_method = contact_method or "Phone"

                if last_donation_date:
                    try:
                        parsed_date = datetime.strptime(last_donation_date, "%Y-%m-%d")
                        parsed_date = _as_utc(parsed_date)
                    except ValueError:
                        flash("Invalid last donation date format. Use YYYY-MM-DD.", "danger")
                        return redirect(url_for("profile"))
                    if current_user.role == "donor":
                        # Keep the donation history consistent: a self-reported
                        # donation is appended only if it is more recent than the
                        # latest recorded donation, otherwise it is rejected.
                        latest = Donation.query.filter_by(donor_id=current_user.id).order_by(
                            Donation.donation_date.desc()
                        ).first()
                        if latest is None or parsed_date > _as_utc(latest.donation_date):
                            record_donation(
                                current_user, bags=1, location=current_user.location,
                                donation_date=parsed_date, status="Self-reported",
                                notes="Reported on profile",
                            )
                        elif parsed_date.date() != _as_utc(latest.donation_date).date():
                            flash(
                                "Last donation date can't be earlier than your latest "
                                "recorded donation.",
                                "warning",
                            )
                            return redirect(url_for("profile"))
                    current_user.last_donation_date = parsed_date

                def _parse_coord(value):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None

                # The location feature stores latitude/longitude automatically.
                # Hidden GPS/search fields carry the exact point; otherwise the
                # coordinates are resolved from the location text when it changed
                # (or when there are none yet), so changing the location always
                # keeps the coordinates in sync without clobbering precise GPS
                # values on unrelated profile edits.
                latitude = request.form.get("latitude", "").strip()
                longitude = request.form.get("longitude", "").strip()
                lat, lng = _parse_coord(latitude), _parse_coord(longitude)
                location_changed = location != (current_user.location or "")
                if latitude or longitude:
                    if (lat is None or lng is None
                            or not validate_coordinates(lat, lng)):
                        flash("Invalid coordinates. Latitude must be -90 to 90, longitude -180 to 180.", "danger")
                        return redirect(url_for("profile"))
                    current_user.latitude = round(lat, 6)
                    current_user.longitude = round(lng, 6)
                elif location and (location_changed or current_user.latitude is None):
                    resolved = resolve_location_coords(location)
                    current_user.latitude, current_user.longitude = (
                        (round(resolved[0], 6), round(resolved[1], 6))
                        if resolved else (None, None)
                    )
                elif not location:
                    current_user.latitude = None
                    current_user.longitude = None
                # else: location unchanged and coordinates already stored - keep
                # the existing (potentially GPS-precise) values untouched.

                if current_user.role == "donor":
                    current_user.reliability_score = compute_reliability(current_user)

                db.session.commit()
                flash("Profile updated successfully.", "success")

            elif form_type == "change_password":
                current_pw = request.form.get("current_password", "")
                new_pw = request.form.get("new_password", "")
                confirm_pw = request.form.get("confirm_password", "")

                if not current_user.check_password(current_pw):
                    flash("Current password is incorrect.", "danger")
                    return redirect(url_for("profile"))

                if new_pw != confirm_pw:
                    flash("New passwords do not match.", "danger")
                    return redirect(url_for("profile"))

                from services import validate_password
                pw_errors = validate_password(new_pw)
                if pw_errors:
                    for err in pw_errors:
                        flash(err, "danger")
                    return redirect(url_for("profile"))

                current_user.set_password(new_pw)
                db.session.commit()
                _log_auth_event(current_user.id, "PASSWORD_CHANGED", "Password changed")
                logger.info("Password changed for %s", current_user.email)
                flash("Password changed successfully.", "success")

            return redirect(url_for("profile"))

        donation_view = None
        donation_records = []
        settings = {}
        if current_user.role == "donor":
            donation_records = Donation.query.filter_by(donor_id=current_user.id).order_by(
                Donation.donation_date.desc()
            ).all()
            donation_view = donor_eligibility_view(current_user)
            settings = load_donation_settings()

        return render_template(
            "profile.html",
            blood_groups=BLOOD_GROUPS,
            donations=donation_records,
            donation_view=donation_view,
            interval_male=settings.get("M", DONATION_INTERVAL_DAYS["M"]),
            interval_female=settings.get("F", DONATION_INTERVAL_DAYS["F"]),
        )

    @app.route("/profile/blood-group", methods=["POST"])
    @login_required
    def profile_upload_blood_group():
        """Upload a blood-group card image, extract the group with the local
        vision model, and store it safely.

        Rules enforced here:
        - only the eight real blood groups are ever stored
        - a verified group is never overwritten automatically; a *different*
          detected group is flagged and needs explicit confirmation
        - uncertain/conflicting/unclear detections are flagged "Verification
          Required" instead of guessing
        - if the card's printed name doesn't match the account name, the record
          is flagged so the card isn't silently tied to the wrong account"""
        file = request.files.get("blood_group_image")
        if not file or not file.filename:
            flash("Please choose a blood group card image to upload.", "danger")
            return redirect(url_for("profile"))

        status, image_bytes = _prepare_image_bytes(file)
        if status != "ok":
            flash(BLOOD_GROUP_VLM_ERROR_MESSAGES["bad_image"], "danger")
            return redirect(url_for("profile"))

        filename = _save_blood_group_image(image_bytes, current_user.id)

        if not VLM_AVAILABLE:
            _set_detection_state(current_user, None, None, filename)
            current_user.blood_group_flagged = True
            current_user.blood_group_flagged_reason = (
                "Image scanning isn't set up on this server (the ollama/Pillow "
                "packages aren't installed). Please confirm your blood group manually."
            )
            db.session.commit()
            flash(current_user.blood_group_flagged_reason, "warning")
            return redirect(url_for("profile"))

        vlm_status, parsed, _reply = _extract_blood_group_vlm(image_bytes)
        if vlm_status != "ok":
            _set_detection_state(current_user, None, None, filename)
            current_user.blood_group_flagged = True
            current_user.blood_group_flagged_reason = (
                BLOOD_GROUP_VLM_ERROR_MESSAGES.get(
                    vlm_status,
                    "Couldn't scan the image. Please try again or confirm manually.",
                )
            )
            db.session.commit()
            flash(current_user.blood_group_flagged_reason, "warning")
            return redirect(url_for("profile"))

        card_name = parsed.get("card_holder_name")
        if card_name and current_user.name and not name_similar(card_name, current_user.name):
            _set_detection_state(current_user, parsed, None, filename)
            current_user.blood_group_flagged = True
            current_user.blood_group_flagged_reason = (
                f"The name on the card ('{card_name}') doesn't match your account "
                f"name ('{current_user.name}'). Please confirm your blood group "
                "manually."
            )
            db.session.commit()
            flash(current_user.blood_group_flagged_reason, "warning")
            return redirect(url_for("profile"))

        decision = decide_extraction(parsed)
        _set_detection_state(current_user, parsed, decision, filename)

        if decision["status"] == "verification_required":
            current_user.blood_group_flagged = True
            current_user.blood_group_flagged_reason = decision["reason"]
            db.session.commit()
            flash(decision["reason"], "warning")
            return redirect(url_for("profile"))

        group = decision["blood_group"]
        if current_user.blood_group_verified and current_user.blood_group and current_user.blood_group == group:
            current_user.blood_group_flagged = False
            current_user.blood_group_flagged_reason = None
            db.session.commit()
            flash(f"Card matches your verified blood group ({group}).", "success")
            return redirect(url_for("profile"))

        if current_user.blood_group_verified and current_user.blood_group and current_user.blood_group != group:
            # Never overwrite a verified group automatically.
            current_user.blood_group_flagged = True
            current_user.blood_group_flagged_reason = (
                f"The image shows {group}, which differs from your verified "
                f"{current_user.blood_group}. Confirm before updating."
            )
            db.session.commit()
            flash(current_user.blood_group_flagged_reason, "warning")
            return redirect(url_for("profile"))

        # No verified group yet: store the auto-detected value, still pending the
        # user's confirmation (it won't be used for matching until verified).
        current_user.blood_group = group
        current_user.blood_group_verified = False
        current_user.blood_group_source = "card_ai"
        current_user.blood_group_flagged = False
        current_user.blood_group_flagged_reason = None
        db.session.commit()
        flash(
            f"Auto-detected blood group {group} "
            f"({decision['confidence']:.0%} confidence). Please confirm it below.",
            "success",
        )
        return redirect(url_for("profile"))

    @app.route("/profile/blood-group/confirm", methods=["POST"])
    @login_required
    def profile_confirm_blood_group():
        """User confirms a blood group (auto-detected or picked manually).  Only
        valid values are stored; confirming marks the group as verified so it can
        be used by the recommendation system."""
        value = request.form.get("blood_group", "").strip()
        if not is_valid_blood_group(value):
            flash("Invalid blood group. Choose one of: A+, A-, B+, B-, AB+, AB-, O+, O-.", "danger")
            return redirect(url_for("profile"))

        group = normalize_blood_group(value)
        current_user.blood_group = group
        current_user.blood_group_verified = True
        current_user.blood_group_source = (
            "card_ai_confirmed" if group == current_user.blood_group_detected else "manual"
        )
        current_user.blood_group_flagged = False
        current_user.blood_group_flagged_reason = None
        db.session.commit()
        logger.info("User %s confirmed blood group %s (%s)", current_user.email, group,
                    current_user.blood_group_source)
        flash(f"Blood group {group} confirmed and marked verified.", "success")
        return redirect(url_for("profile"))

    @app.route("/uploads/blood_groups/<path:filename>")
    @login_required
    def serve_blood_group_image(filename):
        """Serve an uploaded blood-group card image.  Owner or admin only, so the
        images are never exposed through the public static folder."""
        owner = User.query.filter_by(blood_group_image=filename).first()
        if not owner or owner.is_deleted:
            abort(404)
        if current_user.role != "admin" and current_user.id != owner.id:
            abort(403)
        return send_from_directory(BLOOD_GROUP_UPLOAD_DIR, filename)

    @app.route("/api/locations/search")
    @login_required
    def api_locations_search():
        """Autocomplete over the built-in Bangladesh location table.  Used by the
        profile page's location search/select (works offline, no API key)."""
        query = request.args.get("q", "")
        if len(query.strip()) < 2:
            return jsonify({"results": []})
        return jsonify({"results": search_locations(query, limit=8)})

    @app.route("/api/profile/location", methods=["POST"])
    @login_required
    def api_profile_location():
        """Store the user's location and its coordinates.

        Accepts JSON with either:
          {"latitude": .., "longitude": ..}            (from GPS / current position)
          {"location": "Mirpur, Dhaka"}                (typed or selected text)
          {"latitude": .., "longitude": .., "location": "..."}  (both)

        - Coordinates are strictly validated before saving.
        - GPS points are reverse-geocoded to the nearest known place name when
          possible, otherwise the raw 'GPS (lat, lng)' label is stored.
        - Selected/typed locations get their coordinates resolved automatically,
          so changing the location always updates the stored coordinates."""
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form.to_dict()

        latitude = data.get("latitude")
        longitude = data.get("longitude")
        location = (data.get("location") or "").strip()

        try:
            lat = float(latitude) if latitude not in (None, "") else None
            lng = float(longitude) if longitude not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"error": "Coordinates must be numbers."}), 400

        if lat is not None and lng is not None:
            if not validate_coordinates(lat, lng):
                return jsonify({
                    "error": "Invalid coordinates. Latitude must be -90 to 90, "
                             "longitude -180 to 180.",
                }), 400
            if not location:
                place, _km = reverse_geocode(lat, lng)
                location = place or gps_label(lat, lng)
            current_user.location = location
            current_user.latitude = round(lat, 6)
            current_user.longitude = round(lng, 6)
        elif location:
            resolved = resolve_location_coords(location)
            current_user.location = location
            current_user.latitude, current_user.longitude = (
                (round(resolved[0], 6), round(resolved[1], 6))
                if resolved else (None, None)
            )
        else:
            return jsonify({"error": "Provide a location or coordinates."}), 400

        db.session.commit()
        return jsonify({
            "ok": True,
            "location": current_user.location,
            "latitude": current_user.latitude,
            "longitude": current_user.longitude,
        })
