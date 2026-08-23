"""Public routes: landing page, health check, role-aware dashboard and the
shared notification center."""

from datetime import datetime, timezone

from flask import (
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from models import BloodRequest, Notification, User, db


def register_routes(app):

    @app.route("/")
    def index():
        total_donors = User.query.filter_by(role="donor", is_deleted=False).count()
        total_requests = BloodRequest.query.filter_by(is_deleted=False).count()
        fulfilled = BloodRequest.query.filter_by(status="Fulfilled", is_deleted=False).count()
        critical_active = BloodRequest.query.filter(
            BloodRequest.urgency_level == "Critical",
            BloodRequest.status.in_(["Pending", "Under Review", "Approved", "Matched"]),
            BloodRequest.is_deleted == False,
        ).count()
        return render_template(
            "index.html",
            total_donors=total_donors,
            total_requests=total_requests,
            fulfilled=fulfilled,
            critical_active=critical_active,
        )

    @app.route("/health")
    def health_check():
        return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

    @app.route("/dashboard")
    @login_required
    def dashboard():
        """Role-aware landing page: every role gets its own control panel."""
        if current_user.role == "admin":
            return redirect(url_for("admin_dashboard"))
        if current_user.role == "patient":
            return redirect(url_for("patient_dashboard"))
        return redirect(url_for("donor_dashboard"))

    @app.route("/notifications")
    @login_required
    def notifications():
        notifs = Notification.query.filter_by(user_id=current_user.id).order_by(
            Notification.created_at.desc()
        ).all()
        return render_template("notifications.html", notifications=notifs)

    @app.route("/notifications/mark-read", methods=["POST"])
    @login_required
    def mark_notifications_read():
        Notification.query.filter_by(
            user_id=current_user.id, is_read=False
        ).update({"is_read": True})
        db.session.commit()
        return redirect(url_for("notifications"))

    @app.route("/notifications/<int:notif_id>/read")
    @login_required
    def mark_single_read(notif_id):
        notif = Notification.query.get_or_404(notif_id)
        if notif.user_id != current_user.id:
            abort(403)
        notif.is_read = True
        db.session.commit()
        if notif.link:
            return redirect(notif.link)
        return redirect(url_for("notifications"))
