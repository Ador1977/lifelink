"""Admin routes: control panel, blood-group review queue, bulk donor import,
donation ledger, eligibility settings, report moderation, user management,
AI insights and auth logs."""

import logging
import re
from datetime import datetime, timezone

from flask import (
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from ai_engine import donor_recommender, health_screener, urgency_detector
from blood_group_engine import is_valid_blood_group, normalize_blood_group
from donor_import_engine import (
    DEFAULT_IMPORT_PASSWORD,
    clean_phone,
    normalize_ocr_group,
    parse_donor_text,
    record_email,
    record_errors,
)
from eligibility_engine import DONATION_INTERVAL_DAYS
from models import (
    AppSetting,
    AuthLog,
    BloodRequest,
    Donation,
    PredictionLog,
    RequestReport,
    RequestStatusLog,
    User,
    db,
)
from recommendation_engine import COMPATIBLE_DONORS, compute_reliability, resolve_coords
from services import (
    _as_utc,
    _log_auth_event,
    admin_required,
    donor_eligibility_view,
    load_donation_settings,
    record_donation,
    sorted_requests_query,
)
from settings import BLOOD_GROUPS

logger = logging.getLogger("lifelink")


def register_routes(app):

    @app.route("/admin/blood-groups")
    @admin_required
    def admin_blood_groups():
        """Admin review queue: donors whose blood group is unverified or flagged
        'Verification Required', plus every donor who has an uploaded card image."""

        needs_review = User.query.filter(
            User.role == "donor",
            User.is_deleted == False,
            (User.blood_group_flagged == True) | (User.blood_group_verified == False),
        ).order_by(User.blood_group_flagged.desc(), User.id.asc()).all()

        card_holders = User.query.filter(
            User.role == "donor",
            User.is_deleted == False,
            User.blood_group_image != None,
        ).order_by(User.blood_group_detected_at.desc().nullslast()).all()

        return render_template(
            "admin_blood_groups.html",
            needs_review=needs_review,
            card_holders=card_holders,
            blood_groups=BLOOD_GROUPS,
        )

    @app.route("/admin/blood-group/<int:user_id>/confirm", methods=["POST"])
    @admin_required
    def admin_confirm_blood_group(user_id):
        """Admin confirms/overrides a donor's blood group (the confirmation step
        required before a detected group that differs from the current one may be
        written to the database)."""
        user = User.query.get_or_404(user_id)
        value = request.form.get("blood_group", "").strip()
        if not is_valid_blood_group(value):
            flash("Invalid blood group.", "danger")
            return redirect(url_for("admin_blood_groups"))
        user.blood_group = normalize_blood_group(value)
        user.blood_group_verified = True
        user.blood_group_source = "admin"
        user.blood_group_flagged = False
        user.blood_group_flagged_reason = None
        db.session.commit()
        logger.info("Admin %s verified blood group %s for %s",
                    current_user.email, user.blood_group, user.email)
        flash(f"Blood group {user.blood_group} verified for {user.name}.", "success")
        return redirect(url_for("admin_blood_groups"))

    @app.route("/admin/blood-group/<int:user_id>/reject", methods=["POST"])
    @admin_required
    def admin_reject_blood_group(user_id):
        """Admin rejects a flagged detection: the verified group (if any) is kept,
        the pending detection is cleared, and the donor is asked to re-confirm."""
        user = User.query.get_or_404(user_id)
        user.blood_group_flagged = False
        user.blood_group_flagged_reason = None
        user.blood_group_detected = None
        user.blood_group_detected_confidence = None
        db.session.commit()
        flash(f"Detection cleared for {user.name}; their current group "
              f"({user.blood_group or 'not set'}) is kept.", "success")
        return redirect(url_for("admin_blood_groups"))

    @app.route("/admin/import-donors", methods=["GET"])
    @admin_required
    def admin_import_donors_page():
        """Admin bulk donor import - landing page with the paste box."""
        return render_template(
            "admin_import_donors.html",
            preview=None, mode="auto", donor_text="",
        )

    @app.route("/admin/import-donors/preview", methods=["POST"])
    @admin_required
    def admin_import_donors_preview():
        """Parse the pasted roster and show an editable preview table."""
        donor_text = request.form.get("donor_text", "")
        mode = request.form.get("mode", "auto")
        if not donor_text.strip():
            flash("Paste some donor text (or a CSV) to preview.", "warning")
            return redirect(url_for("admin_import_donors_page"))
        records = parse_donor_text(donor_text, mode=mode)
        for rec in records:
            rec["errors"] = record_errors(rec)
        if not records:
            flash("No donor records could be parsed from that text. Check the "
                  "format and try again.", "danger")
            return redirect(url_for("admin_import_donors_page"))
        ready = sum(1 for r in records if not r["errors"])
        flash(
            f"Parsed {len(records)} donor record(s) ({ready} ready, "
            f"{len(records) - ready} need fixes). Review below, then import.",
            "info",
        )
        return render_template(
            "admin_import_donors.html",
            preview=records, mode=mode, donor_text=donor_text,
            blood_groups=BLOOD_GROUPS,
        )

    @app.route("/admin/import-donors/commit", methods=["POST"])
    @admin_required
    def admin_import_donors_commit():
        """Create donor accounts from the confirmed preview rows."""

        indices = set()
        for key in request.form:
            m = re.match(r"include_(\d+)", key)
            if m:
                indices.add(int(m.group(1)))

        created, skipped = [], []
        for i in sorted(indices):
            name = request.form.get(f"name_{i}", "").strip()
            group = normalize_ocr_group(request.form.get(f"blood_group_{i}", ""))
            area = request.form.get(f"area_{i}", "").strip() or None
            phone = clean_phone(request.form.get(f"phone_{i}", ""))
            si = request.form.get(f"si_no_{i}", "").strip()
            rec = {
                "si_no": int(si) if si.isdigit() else None,
                "blood_group": group, "area": area, "name": name or None,
                "phones": [phone] if phone else [],
                "warnings": [], "raw_group": group,
            }
            errors = record_errors(rec)
            if errors:
                skipped.append((name or "(no name)", "; ".join(errors)))
                continue
            email = record_email(rec)
            if (User.query.filter_by(email=email).first()
                    or User.query.filter_by(phone=phone).first()):
                skipped.append((name, "already exists (same phone/email)"))
                continue
            lat = lng = None
            coords = resolve_coords(None, None, area)
            if coords:
                lat, lng = coords
            user = User(
                name=name, email=email, role="donor",
                blood_group=group, phone=phone, location=area,
                email_verified=True,
                blood_group_verified=True, blood_group_source="import",
                preferred_contact_method="Phone", latitude=lat, longitude=lng,
            )
            user.set_password(DEFAULT_IMPORT_PASSWORD)
            db.session.add(user)
            created.append(f"{name} ({group})")
        db.session.commit()
        for entry in created:
            logger.info("Admin %s imported donor %s", current_user.email, entry)
        if created:
            flash(f"Imported {len(created)} donor(s).", "success")
        else:
            flash("No donors were imported.", "warning")
        if skipped:
            preview_txt = "; ".join(
                f"{n} ({e})" for n, e in skipped[:8]
            ) + (" ..." if len(skipped) > 8 else "")
            flash(f"Skipped {len(skipped)} row(s): {preview_txt}", "warning")
        return redirect(url_for("admin_import_donors_page"))

    @app.route("/admin")
    @admin_required
    def admin_panel():
        """Backwards-compatible entry point: the control panel now lives at
        /admin-dashboard."""
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin-dashboard")
    @app.route("/admin-dashboard/")
    @admin_required
    def admin_dashboard():
        """Admin control panel: platform stats, request moderation workflow,
        user management and a recent activity/audit trail."""
        from sqlalchemy import func, or_

        q = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "")
        status_filter = request.args.get("status", "")
        blood_filter = request.args.get("blood_group", "")
        reported_filter = request.args.get("reported", type=int)

        # ---- Users (searchable + filterable by role) ----
        users_q = User.query.filter_by(is_deleted=False)
        if role_filter in ("admin", "donor", "patient"):
            users_q = users_q.filter_by(role=role_filter)
        if q:
            like = f"%{q}%"
            users_q = users_q.filter(or_(User.name.ilike(like), User.email.ilike(like)))
        users = users_q.order_by(User.created_at.desc()).all()

        # ---- Requests (searchable + filterable by status / blood group) ----
        reqs = sorted_requests_query()
        if status_filter:
            reqs = [r for r in reqs if r.status == status_filter]
        if blood_filter:
            reqs = [r for r in reqs if r.blood_group == blood_filter]
        if q:
            ql = q.lower()
            reqs = [
                r for r in reqs
                if ql in (r.patient_name or "").lower()
                or ql in (r.hospital or "").lower()
                or (r.requester and ql in (r.requester.email or "").lower())
            ]
        if reported_filter:
            reqs = [r for r in reqs if r.reports]

        # Requests that users flagged for review (fake/inappropriate).
        reported_requests = [r for r in reqs if r.reports]
        reported_requests.sort(key=lambda r: (-len(r.reports), -r.id))

        status_names = [
            "Pending", "Under Review", "Approved", "Rejected",
            "Matched", "Fulfilled", "Cancelled",
        ]
        status_counts = {
            s: BloodRequest.query.filter_by(status=s, is_deleted=False).count()
            for s in status_names
        }

        stats = {
            "total_users": User.query.filter_by(is_deleted=False).count(),
            "restricted_users": User.query.filter_by(
                is_deleted=False, is_restricted=True
            ).count(),
            "total_donors": User.query.filter_by(role="donor", is_deleted=False).count(),
            "total_patients": User.query.filter_by(role="patient", is_deleted=False).count(),
            "total_requests": BloodRequest.query.filter_by(is_deleted=False).count(),
            "critical": BloodRequest.query.filter_by(
                urgency_level="Critical", is_deleted=False
            ).count(),
            "total_donations": Donation.query.count(),
            "total_bags": db.session.query(func.sum(Donation.bags)).scalar() or 0,
            "bg_review_count": User.query.filter(
                User.role == "donor",
                User.is_deleted == False,
                (User.blood_group_flagged == True) | (User.blood_group_verified == False),
            ).count(),
            "status_counts": status_counts,
        }
        stats.update({
            "pending": status_counts["Pending"] + status_counts["Under Review"],
            "approved": status_counts["Approved"],
            "rejected": status_counts["Rejected"],
            "matched": status_counts["Matched"],
            "fulfilled": status_counts["Fulfilled"],
        })

        urgency_dist = db.session.query(
            BloodRequest.urgency_level, func.count(BloodRequest.id)
        ).filter(BloodRequest.is_deleted == False).group_by(BloodRequest.urgency_level).all()

        blood_dist = db.session.query(
            BloodRequest.blood_group, func.count(BloodRequest.id)
        ).filter(BloodRequest.is_deleted == False).group_by(BloodRequest.blood_group).all()

        donation_settings = load_donation_settings()
        recent_donations = Donation.query.order_by(
            Donation.donation_date.desc()
        ).limit(8).all()

        # Audit trail: recent status transitions + recent auth/admin events.
        audit_logs = RequestStatusLog.query.order_by(
            RequestStatusLog.created_at.desc()
        ).limit(25).all()
        auth_logs = AuthLog.query.order_by(AuthLog.created_at.desc()).limit(10).all()

        audit_entries = []
        for log in audit_logs:
            req = BloodRequest.query.get(log.request_id)
            audit_entries.append({
                "request_id": log.request_id,
                "old_status": log.old_status,
                "new_status": log.new_status,
                "changed_by": log.changed_by.name if log.changed_by else "System",
                "note": log.note,
                "created_at": log.created_at,
                "patient": req.patient_name if req else "?",
                "blood_group": req.blood_group if req else "?",
            })

        # Per-user overview helpers for the users table: donor eligibility and
        # patient emergency/request-history at a glance.
        user_status = {}
        for u in users:
            if u.role == "donor":
                try:
                    v = donor_eligibility_view(u)
                    user_status[u.id] = {
                        "eligible": v["status"]["eligible"],
                        "detail": v["status"]["reason"],
                        "next_eligible_date": v["status"]["next_eligible_date"],
                    }
                except Exception:
                    user_status[u.id] = {"eligible": None, "detail": "", "next_eligible_date": None}
            elif u.role == "patient":
                reqs_of_user = BloodRequest.query.filter_by(
                    requester_id=u.id, is_deleted=False
                ).all()
                latest = max(
                    reqs_of_user,
                    key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
                ) if reqs_of_user else None
                user_status[u.id] = {
                    "request_count": len(reqs_of_user),
                    "latest_urgency": latest.urgency_level if latest else None,
                    "latest_status": latest.status if latest else None,
                }

        return render_template(
            "admin_dashboard.html",
            users=users, requests=reqs, stats=stats, compat=COMPATIBLE_DONORS,
            urgency_dist=dict(urgency_dist), blood_dist=dict(blood_dist),
            donation_settings=donation_settings,
            recent_donations=recent_donations,
            audit_logs=audit_logs, auth_logs=auth_logs,
            audit_entries=audit_entries,
            status_names=status_names,
            reported_requests=reported_requests,
            user_status=user_status,
            q=q, role_filter=role_filter,
            status_filter=status_filter, blood_filter=blood_filter,
            reported_filter=reported_filter,
            interval_male=donation_settings.get("M", DONATION_INTERVAL_DAYS["M"]),
            interval_female=donation_settings.get("F", DONATION_INTERVAL_DAYS["F"]),
        )

    @app.route("/admin/user/<int:user_id>/toggle-availability")
    @admin_required
    def toggle_donor_availability(user_id):
        user = User.query.get_or_404(user_id)
        user.is_available_donor = not user.is_available_donor
        db.session.commit()
        _log_auth_event(
            current_user.id, "ADMIN_ACTION",
            f"Toggled availability for donor {user.email} -> {user.is_available_donor}",
        )
        flash(f"Donor availability toggled for {user.name}.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def delete_user(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash("You cannot delete your own account.", "danger")
            return redirect(url_for("admin_dashboard"))
        user.is_deleted = True
        user.is_restricted = True
        db.session.commit()
        logger.info("User %s soft-deleted by admin %s", user.email, current_user.email)
        _log_auth_event(
            current_user.id, "ADMIN_ACTION",
            f"Deactivated user {user.email}",
        )
        flash(f"User {user.name} has been deactivated.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/user/<int:user_id>/restrict", methods=["POST"])
    @admin_required
    def restrict_user(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash("You cannot restrict your own account.", "danger")
            return redirect(url_for("admin_dashboard"))
        user.is_restricted = True
        db.session.commit()
        _log_auth_event(
            current_user.id, "ADMIN_ACTION",
            f"Restricted user {user.email}",
        )
        flash(f"{user.name}'s account has been restricted (cannot log in).", "warning")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/user/<int:user_id>/restore", methods=["POST"])
    @admin_required
    def restore_user(user_id):
        user = User.query.get_or_404(user_id)
        user.is_deleted = False
        user.is_restricted = False
        db.session.commit()
        _log_auth_event(
            current_user.id, "ADMIN_ACTION",
            f"Restored user {user.email}",
        )
        flash(f"{user.name}'s account has been restored.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/donor/<int:donor_id>/donations")
    @admin_required
    def admin_donor_donations(donor_id):
        """Admin page: one donor's donation summary + full history + record form."""
        donor = User.query.get_or_404(donor_id)
        if donor.role != "donor":
            flash("That user is not a donor.", "danger")
            return redirect(url_for("admin_dashboard"))

        donations = Donation.query.filter_by(donor_id=donor_id).order_by(
            Donation.donation_date.desc()
        ).all()
        view = donor_eligibility_view(donor)
        settings = load_donation_settings()
        return render_template(
            "admin_donor_donations.html",
            donor=donor,
            donations=donations,
            view=view,
            interval_male=settings.get("M", DONATION_INTERVAL_DAYS["M"]),
            interval_female=settings.get("F", DONATION_INTERVAL_DAYS["F"]),
        )

    @app.route("/admin/donor/<int:donor_id>/donations/record", methods=["POST"])
    @admin_required
    def admin_record_donation(donor_id):
        donor = User.query.get_or_404(donor_id)
        if donor.role != "donor":
            flash("That user is not a donor.", "danger")
            return redirect(url_for("admin_dashboard"))

        bags = request.form.get("bags", type=int)
        date_str = request.form.get("donation_date", "").strip()
        time_str = request.form.get("donation_time", "").strip()
        location = request.form.get("donation_location", "").strip()

        if date_str:
            try:
                donation_date = _as_utc(datetime.strptime(date_str, "%Y-%m-%d"))
            except ValueError:
                flash("Invalid donation date. Use YYYY-MM-DD.", "danger")
                return redirect(url_for("admin_donor_donations", donor_id=donor.id))
        else:
            donation_date = datetime.now(timezone.utc)

        if bags is None or bags < 1:
            bags = 1

        record_donation(
            donor, bags=bags, location=location or donor.location,
            donation_date=donation_date, time_str=time_str or None,
            status="Completed", notes=f"Recorded by admin {current_user.email}",
        )
        db.session.commit()
        flash(
            f"Donation recorded for {donor.name} ({donation_date.strftime('%d %b %Y')}). "
            f"Next eligible date recalculated automatically.",
            "success",
        )
        return redirect(url_for("admin_donor_donations", donor_id=donor.id))

    @app.route("/admin/donation/<int:donation_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_donation(donation_id):
        record = Donation.query.get_or_404(donation_id)
        donor = record.donor
        db.session.delete(record)
        remaining = Donation.query.filter_by(donor_id=donor.id).order_by(
            Donation.donation_date.desc()
        ).all()
        donor.donation_count = len(remaining)
        donor.last_donation_date = remaining[0].donation_date if remaining else None
        donor.reliability_score = compute_reliability(donor)
        db.session.commit()
        flash("Donation record removed; donor eligibility recalculated.", "success")
        return redirect(url_for("admin_donor_donations", donor_id=donor.id))

    @app.route("/admin/donations")
    @admin_required
    def admin_donations():
        """Full donation-history ledger across all donors."""
        from sqlalchemy import func
        donations = Donation.query.order_by(Donation.donation_date.desc()).limit(100).all()
        total_records = Donation.query.count()
        total_bags = db.session.query(func.sum(Donation.bags)).scalar() or 0
        return render_template(
            "admin_donations.html",
            donations=donations,
            total_records=total_records,
            total_bags=total_bags,
        )

    @app.route("/admin/settings", methods=["POST"])
    @admin_required
    def admin_update_settings():
        """Update the configurable donation waiting period (days) per gender."""

        values = {
            "donation_interval_male": request.form.get("donation_interval_male", type=int),
            "donation_interval_female": request.form.get("donation_interval_female", type=int),
        }
        for key, value in values.items():
            if value is None or value <= 0:
                flash("Waiting periods must be positive numbers of days.", "danger")
                return redirect(url_for("admin_dashboard"))
        for key, value in values.items():
            row = AppSetting.query.get(key)
            if row:
                row.value = str(value)
            else:
                db.session.add(AppSetting(key=key, value=str(value)))
        db.session.commit()
        logger.info(
            "Admin %s updated donation waiting periods: %s",
            current_user.email, {k: v for k, v in values.items()},
        )
        flash(
            "Donation eligibility settings updated. All donors' eligibility will "
            "recalculate automatically from their history.",
            "success",
        )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/request/<int:req_id>/reports/clear", methods=["POST"])
    @admin_required
    def admin_clear_reports(req_id):
        """Admins can clear the report queue for a request once reviewed."""
        req = BloodRequest.query.get_or_404(req_id)
        count = RequestReport.query.filter_by(request_id=req.id).delete()
        db.session.commit()
        _log_auth_event(
            current_user.id, "ADMIN_ACTION",
            f"Cleared {count} report(s) on request #{req.id}",
        )
        flash(f"Cleared {count} report(s).", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/ai-insights")
    @admin_required
    def ai_insights():

        urgency_info = urgency_detector.model_info()
        health_info = health_screener.model_info()
        recommender_info = donor_recommender.model_info()
        recent_predictions = PredictionLog.query.order_by(
            PredictionLog.created_at.desc()
        ).limit(50).all()

        return render_template(
            "ai_insights.html",
            urgency_info=urgency_info,
            health_info=health_info,
            recommender_info=recommender_info,
            recent_predictions=recent_predictions,
        )

    @app.route("/admin/auth-logs")
    @admin_required
    def auth_logs():

        logs = AuthLog.query.order_by(AuthLog.created_at.desc()).limit(100).all()
        return render_template("auth_logs.html", logs=logs)
