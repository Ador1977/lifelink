"""Donor routes: donor control panel, donor search, availability toggle,
request acceptance and AI health screening."""

import logging

from flask import (
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from ai_engine import health_screener
from models import (
    BloodRequest,
    Donation,
    DonorAcceptance,
    Notification,
    RequestStatusLog,
    User,
    db,
)
from recommendation_engine import COMPATIBLE_DONORS, resolve_coords, score_donor
from services import (
    _maybe_notify_eligibility_restored,
    _request_distance_km,
    create_notification,
    donor_eligibility_view,
    extract_report_values_from_image,
    load_donation_settings,
    log_prediction,
    role_required,
    sorted_requests_query,
)
from settings import BLOOD_GROUPS, VLM_MODEL

logger = logging.getLogger("lifelink")


def register_routes(app):

    @app.route("/donor-dashboard")
    @app.route("/donor-dashboard/")
    @role_required("donor")
    def donor_dashboard():
        """Donor control panel: eligibility, matching requests by distance,
        donation history and availability."""
        _maybe_notify_eligibility_restored(current_user)

        donor_coords = resolve_coords(
            current_user.latitude, current_user.longitude, current_user.location
        )
        view = donor_eligibility_view(current_user)

        bg_filter = request.args.get("blood_group", "").strip()
        urgency_filter = request.args.get("urgency", "").strip()
        q = request.args.get("q", "").strip()

        # Requests that are open to matching (not rejected/fulfilled/cancelled)
        # and compatible with this donor's blood group, nearest first.
        matching = []
        for r in sorted_requests_query():
            if r.status in ("Rejected", "Fulfilled", "Cancelled"):
                continue
            if not r.requester_id:
                continue
            if current_user.blood_group not in COMPATIBLE_DONORS.get(r.blood_group, []):
                continue
            if bg_filter and r.blood_group != bg_filter:
                continue
            if urgency_filter and r.urgency_level != urgency_filter:
                continue
            if q and q.lower() not in (r.hospital or "").lower() \
                    and q.lower() not in (r.patient_name or "").lower():
                continue
            dist = _request_distance_km(r, donor_coords)
            matching.append((r, dist))
        matching.sort(key=lambda pair: (pair[1] is None, pair[1] if pair[1] is not None else 0))
        matching = matching[:20]

        donations = Donation.query.filter_by(donor_id=current_user.id).order_by(
            Donation.donation_date.desc()
        ).limit(10).all()

        unread = Notification.query.filter_by(
            user_id=current_user.id, is_read=False
        ).order_by(Notification.created_at.desc()).limit(5).all()

        my_acceptances = DonorAcceptance.query.filter_by(donor_id=current_user.id).order_by(
            DonorAcceptance.created_at.desc()
        ).limit(10).all()
        my_offer_status = {a.request_id: a.status for a in my_acceptances}

        return render_template(
            "donor_dashboard.html",
            view=view,
            matching=matching,
            donations=donations,
            recent_notifications=unread,
            my_acceptances=my_acceptances,
            my_offer_status=my_offer_status,
            compat=COMPATIBLE_DONORS,
            all_blood_groups=BLOOD_GROUPS,
            bg_filter=bg_filter,
            urgency_filter=urgency_filter,
            q=q,
        )

    @app.route("/donor-search")
    @login_required
    def donor_search():
        blood_group = request.args.get("blood_group", "")
        location = request.args.get("location", "").strip()
        results = []
        ranked = []
        result_views = []

        if blood_group or location:
            query = User.query.filter(
                User.role == "donor",
                User.blood_group_verified == True,
                User.blood_group_flagged == False,
                User.is_available_donor == True,
                User.is_deleted == False,
            )
            if blood_group:
                compatible = COMPATIBLE_DONORS.get(blood_group, [])
                query = query.filter(User.blood_group.in_(compatible))
            if location:
                query = query.filter(User.location.ilike(f"%{location}%"))
            results = query.all()
            result_views = [donor_eligibility_view(d) for d in results]

            # AI-ranks the results whenever a blood group is provided. The
            # searcher's own stored location/coordinates are the reference point.
            if blood_group and results:
                patient_location = current_user.location or "Dhaka"
                p_coords = resolve_coords(
                    current_user.latitude, current_user.longitude, patient_location
                )
                ranked = sorted(
                    (score_donor(
                        d, blood_group, p_coords, "Low",
                        contact_pref=current_user.preferred_contact_method,
                        interval_map=load_donation_settings(),
                    ) for d in results),
                    key=lambda r: (-r["score"],
                                   r["distance_km"] if r["distance_km"] is not None else float("inf")),
                )

        # Google Maps markers for the matching donors that have coordinates.
        # Ranked results include their match score/distance; plain results are
        # shown without ranking info. Donors without coordinates are skipped.
        map_markers = []
        map_center = None
        if current_user.latitude is not None and current_user.longitude is not None:
            map_center = {"lat": current_user.latitude, "lng": current_user.longitude}
        if ranked:
            for idx, r in enumerate(ranked, start=1):
                donor = r["donor"]
                if donor.latitude is None or donor.longitude is None:
                    continue
                map_markers.append({
                    "rank": idx,
                    "name": donor.name,
                    "blood_group": donor.blood_group,
                    "location": donor.location or "",
                    "lat": donor.latitude,
                    "lng": donor.longitude,
                    "score": r["score"],
                    "distance_label": r["distance_label"],
                })
        elif result_views:
            for v in result_views:
                donor = v.donor
                if donor.latitude is None or donor.longitude is None:
                    continue
                map_markers.append({
                    "name": donor.name,
                    "blood_group": donor.blood_group,
                    "location": donor.location or "",
                    "lat": donor.latitude,
                    "lng": donor.longitude,
                    "score": None,
                    "distance_label": None,
                })
        if map_markers and not map_center:
            map_center = {"lat": map_markers[0]["lat"], "lng": map_markers[0]["lng"]}

        return render_template(
            "donor_search.html",
            results=results,
            ranked=ranked,
            result_views=result_views,
            blood_groups=BLOOD_GROUPS,
            query_bg=blood_group,
            query_location=location,
            map_markers=map_markers,
            map_center=map_center,
        )

    @app.route("/donor/availability/toggle")
    @login_required
    def toggle_my_availability():
        if current_user.role != "donor":
            flash("Only donors can toggle availability.", "danger")
            return redirect(url_for("dashboard"))
        current_user.is_available_donor = not current_user.is_available_donor
        db.session.commit()
        flash(
            "You are now marked as "
            + ("available for donation." if current_user.is_available_donor
               else "unavailable for donation."),
            "success",
        )
        return redirect(url_for("donor_dashboard"))

    @app.route("/request/<int:req_id>/accept", methods=["POST"])
    @login_required
    def accept_request(req_id):
        req = BloodRequest.query.get_or_404(req_id)
        if current_user.role != "donor":
            flash("Only donors can accept requests.", "danger")
            return redirect(url_for("requests_list"))

        existing = DonorAcceptance.query.filter_by(
            donor_id=current_user.id, request_id=req_id
        ).first()
        if existing:
            flash("You have already responded to this request.", "info")
            return redirect(url_for("request_detail", req_id=req_id))

        acceptance = DonorAcceptance(
            donor_id=current_user.id,
            request_id=req_id,
            status="Pending",
        )
        db.session.add(acceptance)

        create_notification(
            req.requester_id,
            f"Donor {current_user.name} ({current_user.blood_group}) is willing to donate for request #{req.id}.",
            url_for("request_detail", req_id=req_id),
            title="Donor Offer",
            notification_type="donor_offer",
            request_id=req.id,
        )

        if req.status == "Pending":
            req.status = "Matched"
            db.session.add(RequestStatusLog(
                request_id=req.id,
                old_status="Pending",
                new_status="Matched",
                changed_by_id=current_user.id,
                note=f"Auto-matched when donor {current_user.name} offered.",
            ))

        db.session.commit()
        logger.info("Donor %s accepted request #%d", current_user.email, req_id)
        flash("Thank you! The requester has been notified.", "success")
        return redirect(url_for("request_detail", req_id=req_id))

    @app.route("/health-screening", methods=["GET", "POST"])
    @login_required
    def health_screening():
        import app as _app
        result = None
        if request.method == "POST":
            source = request.form.get("source", "manual")

            if source == "upload":
                file = request.files.get("health_report_image")
                systolic = request.form.get("systolic", type=int)
                diastolic = request.form.get("diastolic", type=int)
                pulse = request.form.get("pulse", type=int)
                weight = request.form.get("weight", type=int)

                if not file or file.filename == "":
                    flash("Please choose a health report image (JPG, JPEG or PNG) to upload.", "danger")
                elif not _app._allowed_report_image(file.filename):
                    flash("Unsupported file type. Please upload a JPG, JPEG or PNG image.", "danger")
                elif None in [systolic, diastolic, pulse, weight]:
                    flash(
                        "Weight, blood pressure and pulse aren't part of a lab report, so "
                        "please fill those in as well.",
                        "danger",
                    )
                else:
                    status, extracted, raw_text = extract_report_values_from_image(file)

                    if status == "vlm_unavailable":
                        flash(
                            "Image scanning isn't set up on this server (the ollama/Pillow "
                            "packages aren't installed). Run 'pip install -r requirements.txt', "
                            "or use Manual Selection for now.",
                            "danger",
                        )
                    elif status == "vlm_unreachable":
                        flash(
                            "Couldn't reach the local vision model server. Make sure Ollama is "
                            "running ('ollama serve', or open the Ollama app), then try again — "
                            "or use Manual Selection for now.",
                            "danger",
                        )
                    elif status == "vlm_model_missing":
                        flash(
                            f"The {VLM_MODEL} model isn't downloaded yet. Run 'ollama pull "
                            f"{VLM_MODEL}' in a terminal, then try again — or use Manual Selection "
                            "for now.",
                            "danger",
                        )
                    elif status == "bad_image":
                        flash("That file doesn't look like a valid image. Please upload a JPG, JPEG or PNG photo or scan of your lab report.", "danger")
                    elif status == "incomplete":
                        required_labels = {"name": "name", "age": "age", "gender": "gender", "hemoglobin": "hemoglobin"}
                        missing = [label for key, label in required_labels.items() if extracted.get(key) is None]
                        found = {k: v for k, v in extracted.items() if v is not None}
                        flash(
                            "We couldn't read everything we need from that image (missing: " +
                            ", ".join(missing) + "). Please upload a clearer photo or scan, or "
                            "use Manual Selection instead.",
                            "danger",
                        )
                        result = {"ocr_debug": True, "source": "upload", "found": found, "missing": missing, "raw_text": raw_text}
                    else:
                        name = extracted["name"]
                        age = extracted["age"]
                        hemoglobin = extracted["hemoglobin"]
                        gender_str = extracted["gender"]
                        gender = 1 if gender_str == "F" else 0
                        ldl = extracted.get("ldl")
                        hdl = extracted.get("hdl")

                        result = health_screener.screen(hemoglobin, systolic, diastolic, weight, pulse, age, gender)
                        result["source"] = "upload"
                        result["extracted"] = {
                            "Name (read from report)": name,
                            "Age (read from report)": age,
                            "Gender (read from report)": "Female" if gender else "Male",
                            "Hemoglobin (read from report)": hemoglobin,
                            "Weight (entered)": weight,
                            "Systolic BP (entered)": systolic,
                            "Diastolic BP (entered)": diastolic,
                            "Pulse (entered)": pulse,
                        }
                        if ldl is not None:
                            result["extracted"]["LDL Cholesterol (read from report)"] = ldl
                        if hdl is not None:
                            result["extracted"]["HDL Cholesterol (read from report)"] = hdl

                        current_user.last_donation_eligible = result["eligible"]
                        current_user.last_screening_confidence = result["confidence"]

                        log_prediction(
                            "health_screening",
                            {"name": name, "hemoglobin": hemoglobin, "systolic": systolic, "diastolic": diastolic,
                             "weight": weight, "pulse": pulse, "age": age, "gender": gender_str,
                             "ldl": ldl, "hdl": hdl, "source": "upload"},
                            "Eligible" if result["eligible"] else "Not Eligible",
                            result["confidence"],
                            current_user.id,
                        )
                        db.session.commit()
            else:
                hemoglobin = request.form.get("hemoglobin", type=float)
                systolic = request.form.get("systolic", type=int)
                diastolic = request.form.get("diastolic", type=int)
                weight = request.form.get("weight", type=int)
                pulse = request.form.get("pulse", type=int)
                age = request.form.get("age", type=int)
                gender_str = request.form.get("gender", "M")
                gender = 1 if gender_str == "F" else 0

                if None in [hemoglobin, systolic, diastolic, weight, pulse, age]:
                    flash("Please fill in all health report fields.", "danger")
                else:
                    result = health_screener.screen(hemoglobin, systolic, diastolic, weight, pulse, age, gender)
                    result["source"] = "manual"
                    current_user.last_donation_eligible = result["eligible"]
                    current_user.last_screening_confidence = result["confidence"]

                    log_prediction(
                        "health_screening",
                        {"hemoglobin": hemoglobin, "systolic": systolic, "diastolic": diastolic,
                         "weight": weight, "pulse": pulse, "age": age, "gender": gender_str,
                         "source": "manual"},
                        "Eligible" if result["eligible"] else "Not Eligible",
                        result["confidence"],
                        current_user.id,
                    )
                    db.session.commit()

        return render_template("health_screening.html", result=result)
