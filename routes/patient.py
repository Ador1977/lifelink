"""Patient routes: patient control panel, request creation (with AI urgency +
lab-report extraction), request listing/detail, the request workflow, donor
response handling and request reporting/moderation."""

import json
import logging

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

import maps_engine
from ai_engine import urgency_detector
from models import (
    BloodRequest,
    DonorAcceptance,
    Notification,
    RequestReport,
    RequestStatusLog,
    User,
    db,
)
from recommendation_engine import COMPATIBLE_DONORS, compute_reliability, resolve_coords
from services import (
    _hospital_coords,
    _log_auth_event,
    _log_recommendations,
    _run_recommendations,
    age_risk_from_age,
    create_notification,
    donor_eligibility_view,
    log_prediction,
    notify_admins,
    record_donation,
    role_required,
    score_condition_from_report,
    send_email,
    sorted_requests_query,
)
from settings import BLOOD_GROUPS, GOOGLE_MAPS_API_KEY, VLM_MODEL

logger = logging.getLogger("lifelink")


def register_routes(app):

    @app.route("/patient-dashboard")
    @app.route("/patient-dashboard/")
    @role_required("patient")
    def patient_dashboard():
        """Patient control panel: my requests, donor responses, notifications."""
        my_requests = BloodRequest.query.filter_by(
            requester_id=current_user.id, is_deleted=False
        ).order_by(BloodRequest.created_at.desc()).all()

        # Donor offers against my requests (accepted/pending, most recent first).
        req_ids = [r.id for r in my_requests]
        acceptances = []
        if req_ids:
            acceptances = DonorAcceptance.query.filter(
                DonorAcceptance.request_id.in_(req_ids)
            ).order_by(DonorAcceptance.created_at.desc()).all()

        unread = Notification.query.filter_by(
            user_id=current_user.id, is_read=False
        ).order_by(Notification.created_at.desc()).limit(5).all()

        active = [r for r in my_requests if r.status not in ("Fulfilled", "Cancelled", "Rejected")]
        stats = {
            "total": len(my_requests),
            "active": len(active),
            "approved": sum(1 for r in my_requests if r.status == "Approved"),
            "pending": sum(1 for r in my_requests if r.status in ("Pending", "Under Review")),
            "fulfilled": sum(1 for r in my_requests if r.status == "Fulfilled"),
            "rejected": sum(1 for r in my_requests if r.status == "Rejected"),
        }

        # Suitable donors for each open request (compatibility + distance +
        # eligibility + availability), top 3 per request for a quick overview.
        compatible_donors = {}
        for r in active:
            try:
                result = _run_recommendations(r)
                compatible_donors[r.id] = result["recommended"][:3]
            except Exception:
                logger.exception("Failed to compute donor recommendations for request #%s", r.id)
                compatible_donors[r.id] = []

        return render_template(
            "patient_dashboard.html",
            my_requests=my_requests,
            acceptances=acceptances,
            recent_notifications=unread,
            stats=stats,
            compatible_donors=compatible_donors,
            compat=COMPATIBLE_DONORS,
        )

    @app.route("/request/new", methods=["GET", "POST"])
    @login_required
    def create_request():
        import app as _app
        if request.method == "POST":
            patient_name = request.form.get("patient_name", "").strip()
            blood_group = request.form.get("blood_group")
            units_needed = request.form.get("units_needed", type=int)
            hospital = request.form.get("hospital", "").strip()
            hours_needed = request.form.get("hours_needed", type=int)
            age_risk = request.form.get("age_risk", type=int)
            notes = request.form.get("notes", "")
            hospital_type = request.form.get("hospital_type", 0, type=int)

            condition_score = None
            condition_source = "report"
            report_data_json = None

            file = request.files.get("report_image")
            if not file or file.filename == "":
                flash("Please attach the patient's lab report image.", "danger")
                return redirect(url_for("create_request"))
            if not _app._allowed_report_image(file.filename):
                flash("Unsupported file type. Please upload a JPG, JPEG or PNG image of the lab report.", "danger")
                return redirect(url_for("create_request"))

            status, extracted, raw_reply = _app.extract_urgency_report_values_from_image(file)

            if status in ("ok", "incomplete"):
                score, findings = score_condition_from_report(extracted)
                if score is None:
                    flash(
                        "We couldn't read usable lab values (hemoglobin/WBC/platelets) from that "
                        "report. Please upload a clearer photo or scan.",
                        "danger",
                    )
                    return redirect(url_for("create_request"))
                condition_score = score
                condition_source = "report"
                report_data_json = json.dumps({
                    "status": status,
                    "extracted": extracted,
                    "findings": {k: list(v) for k, v in findings.items()},
                    "raw_reply": raw_reply,
                }, default=str)
                if extracted.get("age") is not None:
                    age_risk = age_risk_from_age(extracted["age"])
            else:
                messages = {
                    "vlm_unavailable": "Image scanning isn't set up on this server (the ollama/Pillow "
                                       "packages aren't installed). Please install them and restart.",
                    "vlm_unreachable": "Couldn't reach the local vision model server. Make sure Ollama "
                                       "is running, then try again.",
                    "vlm_model_missing": "The vision model isn't downloaded yet. Run 'ollama pull "
                                         f"{VLM_MODEL}' then try again.",
                    "bad_image": "That file doesn't look like a valid image. Please upload a JPG, "
                                 "JPEG or PNG photo/scan of the lab report.",
                }
                flash(messages.get(status, "Couldn't scan the report. Please try again."), "danger")
                return redirect(url_for("create_request"))

            if not all([patient_name, blood_group, units_needed, hospital, condition_score, hours_needed, age_risk]):
                flash("Please fill in all required fields.", "danger")
                return redirect(url_for("create_request"))

            if units_needed < 1 or units_needed > 20:
                flash("Units needed must be between 1 and 20.", "danger")
                return redirect(url_for("create_request"))
            if condition_score not in range(1, 6):
                flash("Invalid condition score.", "danger")
                return redirect(url_for("create_request"))
            if age_risk not in range(1, 4):
                flash("Invalid age risk factor.", "danger")
                return redirect(url_for("create_request"))

            history_count = BloodRequest.query.filter_by(
                requester_id=current_user.id, is_deleted=False
            ).count()

            result = urgency_detector.predict(
                units_needed, condition_score, hours_needed, age_risk,
                hospital_type, history_count,
            )

            # Patients may override the AI urgency level (default: let the model
            # decide). The model prediction is still logged for auditing.
            urgency_override = (request.form.get("urgency_override", "Auto") or "Auto").strip()
            if urgency_override not in ("Auto", "Critical", "High", "Medium", "Low"):
                urgency_override = "Auto"

            new_request = BloodRequest(
                patient_name=patient_name,
                requester_id=current_user.id,
                blood_group=blood_group,
                units_needed=units_needed,
                hospital=hospital,
                hospital_type=hospital_type,
                condition_score=condition_score,
                condition_source=condition_source,
                report_data=report_data_json,
                hours_needed=hours_needed,
                age_risk=age_risk,
                notes=notes,
                urgency_level=result["level"] if urgency_override == "Auto" else urgency_override,
                urgency_confidence=result["confidence"] if urgency_override == "Auto" else None,
            )
            db.session.add(new_request)
            db.session.flush()

            log_entry = RequestStatusLog(
                request_id=new_request.id,
                old_status=None,
                new_status="Pending",
                changed_by_id=current_user.id,
            )
            db.session.add(log_entry)
            log_prediction(
                "urgency", result["features"], result["level"],
                result["confidence"], current_user.id,
            )

            db.session.commit()

            # Notify every active admin so the new request can be reviewed.
            notify_admins(
                f"New {new_request.blood_group} blood request #{new_request.id} "
                f"({new_request.urgency_level}) needs admin review.",
                url_for("request_detail", req_id=new_request.id),
                title="New Blood Request",
                notification_type="request_created",
                request_id=new_request.id,
            )

            # Real-time donor matching: immediately check every registered donor,
            # log the AI-ranked eligible donors, and notify the top ones.
            try:
                rec_result = _run_recommendations(new_request)
                _log_recommendations(new_request, rec_result)
                for r in rec_result["recommended"][:3]:
                    create_notification(
                        r["donor_id"],
                        f"A new {new_request.blood_group} blood request needs eligible "
                        f"donors (urgency: {new_request.urgency_level}).",
                        url_for("request_detail", req_id=new_request.id),
                        title="New Blood Request",
                        notification_type="request_created",
                        request_id=new_request.id,
                    )
                db.session.commit()
            except Exception:
                logger.exception(
                    "Real-time donor matching failed for request #%d", new_request.id
                )

            if result["level"] == "Review":
                flash(
                    f"Request submitted! AI assessment: Manual review needed "
                    f"(confidence {result['confidence']}% is below threshold).",
                    "warning",
                )
            else:
                flash(
                    f"Request submitted! Condition scored from the lab report "
                    f"({condition_score}/5). AI Urgency Assessment: {result['level']} "
                    f"({result['confidence']}% confidence).",
                    "success",
                )
            logger.info(
                "Blood request #%d created by %s - Urgency: %s (condition source: %s)",
                new_request.id, current_user.email, result["level"], condition_source,
            )
            return redirect(url_for("requests_list"))

        return render_template("create_request.html", blood_groups=BLOOD_GROUPS)

    @app.route("/requests")
    @login_required
    def requests_list():
        blood_group_filter = request.args.get("blood_group", "")
        status_filter = request.args.get("status", "")

        reqs = sorted_requests_query()

        if blood_group_filter:
            reqs = [r for r in reqs if r.blood_group == blood_group_filter]
        if status_filter:
            reqs = [r for r in reqs if r.status == status_filter]

        return render_template(
            "requests_list.html",
            requests=reqs,
            compat=COMPATIBLE_DONORS,
            bg_filter=blood_group_filter,
            status_filter=status_filter,
            all_blood_groups=BLOOD_GROUPS,
        )

    @app.route("/request/<int:req_id>")
    @login_required
    def request_detail(req_id):
        req = BloodRequest.query.get_or_404(req_id)
        if req.is_deleted:
            abort(404)

        status_log = RequestStatusLog.query.filter_by(request_id=req_id).order_by(
            RequestStatusLog.created_at.asc()
        ).all()
        acceptances = DonorAcceptance.query.filter_by(request_id=req_id).all()

        compatible_donors = []
        if req.blood_group in COMPATIBLE_DONORS:
            compatible_blood_groups = COMPATIBLE_DONORS[req.blood_group]
            compatible_donors = User.query.filter(
                User.role == "donor",
                User.blood_group.in_(compatible_blood_groups),
                User.blood_group_verified == True,
                User.blood_group_flagged == False,
                User.is_available_donor == True,
                User.is_deleted == False,
            ).all()

        compatible_donor_views = [donor_eligibility_view(d) for d in compatible_donors]

        top_recommendations = []
        if current_user.role == "admin" or req.requester_id == current_user.id:
            rec_result = _run_recommendations(req)
            top_recommendations = rec_result["recommended"][:5]

        report_data = {}
        if req.report_data:
            try:
                report_data = json.loads(req.report_data)
            except (ValueError, TypeError):
                report_data = {}

        # Google Maps data: hospital coordinates + embed/directions links.
        hospital_coords = _hospital_coords(req.hospital)
        hospital_embed_url = maps_engine.embed_url(
            query=req.hospital, coords=hospital_coords,
            key=GOOGLE_MAPS_API_KEY,
        )
        hospital_directions_url = maps_engine.directions_url(
            coords=hospital_coords, query=req.hospital,
        )
        hospital_search_url = maps_engine.maps_search_url(req.hospital)
        requester_coords = None
        if req.requester:
            requester_coords = resolve_coords(
                req.requester.latitude, req.requester.longitude,
                req.requester.location,
            )

        reports = RequestReport.query.filter_by(request_id=req.id).all()
        reported_by_me = any(r.reported_by == current_user.id for r in reports)

        return render_template(
            "request_detail.html",
            req=req,
            status_log=status_log,
            acceptances=acceptances,
            compatible_donors=compatible_donors,
            compatible_donor_views=compatible_donor_views,
            top_recommendations=top_recommendations,
            compat=COMPATIBLE_DONORS,
            report_data=report_data,
            hospital_coords=hospital_coords,
            hospital_embed_url=hospital_embed_url,
            hospital_directions_url=hospital_directions_url,
            hospital_search_url=hospital_search_url,
            requester_coords=requester_coords,
            reports=reports,
            reported_by_me=reported_by_me,
        )

    @app.route("/request/<int:req_id>/status", methods=["POST"])
    @login_required
    def update_request_status(req_id):
        req = BloodRequest.query.get_or_404(req_id)

        new_status = request.form.get("new_status", "")
        note = request.form.get("note", "").strip()
        rejection_reason = request.form.get("rejection_reason", "").strip()

        valid_statuses = [
            "Pending", "Under Review", "Approved", "Rejected",
            "Matched", "Fulfilled", "Cancelled",
        ]

        if new_status not in valid_statuses:
            flash("Invalid status.", "danger")
            return redirect(url_for("requests_list"))

        # Role-based transition rules (enforced on the backend).
        if current_user.role == "admin":
            pass  # admins may move a request through the full workflow
        elif req.requester_id == current_user.id:
            # The requester (any role) may withdraw their own request while it
            # is still open.
            if new_status != "Cancelled":
                flash("You may only cancel your own request.", "danger")
                return redirect(url_for("request_detail", req_id=req.id))
            if req.status not in ("Pending", "Under Review", "Approved", "Matched"):
                flash("This request can no longer be cancelled.", "danger")
                return redirect(url_for("request_detail", req_id=req.id))
        else:
            flash("You are not authorized to update this request.", "danger")
            return redirect(url_for("requests_list"))

        # Rejection must always carry an explanation for the patient.
        if new_status == "Rejected" and not rejection_reason:
            flash("Please provide a rejection reason so the patient knows why.", "warning")
            return redirect(request.referrer or url_for("request_detail", req_id=req.id))

        old_status = req.status
        req.status = new_status
        req.rejection_reason = rejection_reason or None

        log_entry = RequestStatusLog(
            request_id=req.id,
            old_status=old_status,
            new_status=new_status,
            changed_by_id=current_user.id,
            note=rejection_reason or note or None,
        )
        db.session.add(log_entry)

        detail_url = url_for("request_detail", req_id=req.id)

        if new_status == "Under Review":
            create_notification(
                req.requester_id,
                f"Your blood request #{req.id} ({req.blood_group}) is under review by an admin.",
                detail_url,
                title="Request Under Review",
                notification_type="request_review",
                request_id=req.id,
            )
        elif new_status == "Approved":
            create_notification(
                req.requester_id,
                f"Your blood request #{req.id} for {req.blood_group} has been approved. "
                f"Compatible donors are being notified.",
                detail_url,
                title="Request Approved",
                notification_type="request_approved",
                request_id=req.id,
            )
            send_email(
                "LifeLink - Request Approved",
                [req.requester.email],
                f"Hello {req.requester.name},\n\nYour blood request #{req.id} for {req.blood_group} "
                f"has been approved and is now visible to compatible donors.\n\nLifeLink Team",
            )
            # Notify the best-matched eligible donors so they can respond.
            try:
                rec_result = _run_recommendations(req)
                _log_recommendations(req, rec_result)
                for r in rec_result["recommended"][:3]:
                    create_notification(
                        r["donor_id"],
                        f"A {req.blood_group} request at {req.hospital} is now approved "
                        f"and needs eligible donors (urgency: {req.urgency_level}).",
                        detail_url,
                        title="Matching Blood Request",
                        notification_type="request_approved",
                        request_id=req.id,
                    )
            except Exception:
                logger.exception("Approval donor matching failed for request #%d", req.id)
        elif new_status == "Rejected":
            create_notification(
                req.requester_id,
                f"Your blood request #{req.id} was rejected."
                + (f" Reason: {rejection_reason}" if rejection_reason else ""),
                detail_url,
                title="Request Rejected",
                notification_type="request_rejected",
                request_id=req.id,
            )
            send_email(
                "LifeLink - Request Rejected",
                [req.requester.email],
                f"Hello {req.requester.name},\n\nYour blood request #{req.id} for {req.blood_group} "
                f"was rejected." + (f"\nReason: {rejection_reason}\n" if rejection_reason else "\n")
                + "\nLifeLink Team",
            )
        elif new_status == "Fulfilled":
            create_notification(
                req.requester_id,
                f"Your blood request #{req.id} has been fulfilled!",
                detail_url,
                title="Request Fulfilled",
                notification_type="request_fulfilled",
                request_id=req.id,
            )
            send_email(
                "LifeLink - Request Fulfilled",
                [req.requester.email],
                f"Hello {req.requester.name},\n\nYour blood request #{req.id} for {req.blood_group} "
                f"has been marked as fulfilled. Thank you!\n\nLifeLink Team",
            )
            accepted_donors = DonorAcceptance.query.filter_by(
                request_id=req.id, status="Accepted"
            ).all()
            for a in accepted_donors:
                # Append a permanent donation-history record (never overwrites
                # older records) and auto-compute the next eligible date.
                record_donation(
                    a.donor,
                    bags=1,
                    location=req.hospital,
                    notes=f"Fulfilled request #{req.id}",
                )
                a.donor.reliability_score = compute_reliability(a.donor)
                create_notification(
                    a.donor_id,
                    f"Your donation for request #{req.id} has been recorded. "
                    f"Your next eligible date was recalculated.",
                    url_for("donor_dashboard"),
                    title="Donation Recorded",
                    notification_type="donation_recorded",
                    request_id=req.id,
                )
            if accepted_donors:
                logger.info(
                    "Request #%d fulfilled - donation recorded for %d donor(s).",
                    req.id, len(accepted_donors),
                )
        elif new_status == "Cancelled":
            create_notification(
                req.requester_id,
                f"Your blood request #{req.id} has been cancelled.",
                detail_url,
                title="Request Cancelled",
                notification_type="request_cancelled",
                request_id=req.id,
            )
            # Let donors who offered on this request know it's off the board.
            for acc in DonorAcceptance.query.filter_by(request_id=req.id).all():
                if acc.status in ("Pending", "Accepted"):
                    create_notification(
                        acc.donor_id,
                        f"Blood request #{req.id} ({req.patient_name}, {req.blood_group}) "
                        f"has been cancelled and is no longer needed.",
                        detail_url,
                        title="Request Cancelled",
                        notification_type="request_cancelled",
                        request_id=req.id,
                    )

        db.session.commit()
        logger.info("Request #%d status changed: %s -> %s by %s", req.id, old_status, new_status, current_user.email)
        flash(f"Request status updated to {new_status}.", "success")
        return redirect(request.referrer or url_for("requests_list"))

    @app.route("/acceptance/<int:acceptance_id>/status", methods=["POST"])
    @login_required
    def update_acceptance_status(acceptance_id):
        acceptance = DonorAcceptance.query.get_or_404(acceptance_id)
        req = acceptance.blood_request

        if current_user.id != req.requester_id and current_user.role != "admin":
            flash("Not authorized.", "danger")
            return redirect(url_for("requests_list"))

        new_status = request.form.get("new_status", "")
        if new_status in ["Accepted", "Rejected"]:
            acceptance.status = new_status
            create_notification(
                acceptance.donor_id,
                f"Your offer to donate for request #{req.id} has been {new_status.lower()}.",
                url_for("request_detail", req_id=req.id),
                title=f"Offer {new_status}",
                notification_type=f"donor_offer_{new_status.lower()}",
                request_id=req.id,
            )
            db.session.commit()
            flash(f"Donor response {new_status.lower()}.", "success")

        return redirect(url_for("request_detail", req_id=req.id))

    @app.route("/request/<int:req_id>/report", methods=["POST"])
    @login_required
    def report_request(req_id):
        """Flag a blood request as fake/inappropriate. Admins review the queue
        and either reject the request (with a reason) or clear the reports."""
        req = BloodRequest.query.get_or_404(req_id)
        if req.is_deleted:
            abort(404)
        if req.requester_id == current_user.id:
            flash("You cannot report your own request.", "warning")
            return redirect(url_for("request_detail", req_id=req.id))
        reason = request.form.get("reason", "").strip()[:300]
        if not reason:
            flash("Please provide a short reason for the report.", "warning")
            return redirect(url_for("request_detail", req_id=req.id))

        existing = RequestReport.query.filter_by(
            request_id=req.id, reported_by=current_user.id
        ).first()
        if existing:
            flash("You have already reported this request.", "warning")
            return redirect(url_for("request_detail", req_id=req.id))

        db.session.add(RequestReport(
            request_id=req.id,
            reported_by=current_user.id,
            reason=reason,
        ))
        db.session.commit()
        notify_admins(
            f"Blood request #{req.id} ({req.patient_name}, {req.blood_group}) was "
            f"reported as inappropriate by {current_user.name}.",
            url_for("admin_dashboard", reported=1),
            title="Request Reported",
            notification_type="request_reported",
            request_id=req.id,
        )
        _log_auth_event(
            current_user.id, "REPORT",
            f"Reported request #{req.id}: {reason}",
        )
        flash("Request reported to the admin team. Thank you.", "success")
        return redirect(url_for("request_detail", req_id=req.id))

    @app.route("/request/<int:req_id>/recommendations")
    @login_required
    def request_recommendations(req_id):
        req = BloodRequest.query.get_or_404(req_id)
        if req.is_deleted:
            abort(404)
        if current_user.role != "admin" and req.requester_id != current_user.id:
            flash("You are not authorized to view recommendations for this request.", "danger")
            return redirect(url_for("requests_list"))

        contact_pref = request.args.get("contact_pref", "").strip()
        result = _run_recommendations(req, contact_pref=contact_pref)
        _log_recommendations(req, result)

        return render_template(
            "recommendations.html",
            req=req,
            result=result,
            contact_pref=contact_pref,
        )
