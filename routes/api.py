"""JSON API routes: request recommendations, donor eligibility and donor
donation history (owner/admin only)."""

from flask import jsonify, request
from flask_login import current_user, login_required

from models import BloodRequest, Donation, User
from services import (
    _as_utc,
    _run_recommendations,
    donor_eligibility_view,
)


def register_routes(app):

    @app.route("/api/request/<int:req_id>/recommendations")
    @login_required
    def api_request_recommendations(req_id):
        """JSON endpoint the frontend can call. Same output as the page route,
        restricted to the request owner or an admin."""
        req = BloodRequest.query.get_or_404(req_id)
        if req.is_deleted:
            return jsonify({"error": "Request not found"}), 404
        if current_user.role != "admin" and req.requester_id != current_user.id:
            return jsonify({"error": "You are not authorized to view these recommendations."}), 403

        contact_pref = request.args.get("contact_pref", "").strip()
        result = _run_recommendations(req, contact_pref=contact_pref)
        return jsonify(result)

    @app.route("/api/donor/<int:donor_id>/eligibility")
    @login_required
    def api_donor_eligibility(donor_id):
        """JSON: current donation-history eligibility for one donor (owner/admin)."""
        donor = User.query.get_or_404(donor_id)
        if donor.role != "donor":
            return jsonify({"error": "Not a donor account"}), 404
        if current_user.role != "admin" and current_user.id != donor_id:
            return jsonify({"error": "You are not authorized to view this donor's eligibility."}), 403

        view = donor_eligibility_view(donor)
        status = view["status"]
        return jsonify({
            "donor_id": donor.id,
            "name": donor.name,
            "blood_group": donor.blood_group,
            "eligible": status["eligible"],
            "status": status["status"],
            "reason": status["reason"],
            "last_donation_date": status["last_donation_date"].isoformat()
            if status["last_donation_date"] else None,
            "next_eligible_date": status["next_eligible_date"].isoformat()
            if status["next_eligible_date"] else None,
            "cooldown_days": status["cooldown_days"],
            "days_remaining": status["days_remaining"],
            "total_donations": view["total_donations"],
            "total_bags": view["total_bags"],
            "interval_days": view["interval_days"],
        })

    @app.route("/api/donor/<int:donor_id>/donations")
    @login_required
    def api_donor_donations(donor_id):
        """JSON: full donation history for one donor (owner/admin)."""
        donor = User.query.get_or_404(donor_id)
        if donor.role != "donor":
            return jsonify({"error": "Not a donor account"}), 404
        if current_user.role != "admin" and current_user.id != donor_id:
            return jsonify({"error": "You are not authorized to view this donor's history."}), 403

        view = donor_eligibility_view(donor)
        records = Donation.query.filter_by(donor_id=donor_id).order_by(
            Donation.donation_date.desc()
        ).all()
        return jsonify({
            "summary": {
                "total_donations": view["total_donations"],
                "total_bags": view["total_bags"],
                "last_donation_date": view["status"]["last_donation_date"].isoformat()
                if view["status"]["last_donation_date"] else None,
                "next_eligible_date": view["status"]["next_eligible_date"].isoformat()
                if view["status"]["next_eligible_date"] else None,
                "eligible": view["status"]["eligible"],
                "status": view["status"]["status"],
                "reason": view["status"]["reason"],
                "cooldown_days": view["status"]["cooldown_days"],
                "days_remaining": view["status"]["days_remaining"],
                "interval_days": view["interval_days"],
            },
            "donations": [{
                "id": d.id,
                "donation_date": _as_utc(d.donation_date).isoformat(),
                "donation_time": d.donation_time,
                "blood_group": d.blood_group,
                "bags": d.bags,
                "donation_location": d.donation_location,
                "next_eligible_date": _as_utc(d.next_eligible_date).isoformat()
                if d.next_eligible_date else None,
                "status": d.status,
                "notes": d.notes,
            } for d in records],
        })
