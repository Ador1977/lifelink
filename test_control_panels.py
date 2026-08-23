"""
test_control_panels.py
----------------------
Route-level tests for the role-based control panels, RBAC, request
workflow and the centralized DB-backed notification system.

Runs the real Flask app against a throwaway SQLite database
(DATABASE_URL is pointed at a temp file before the app module is
imported), so the production instance DB is never touched.
"""

import os
import sys
import tempfile
import unittest
from io import BytesIO

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Point the app at a fresh temp database BEFORE importing it.
_TMP_DIR = tempfile.mkdtemp(prefix="lifelink_tests_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP_DIR, "test.db")

import app as app_module  # noqa: E402
from app import app, db, User, BloodRequest, Notification, Donation, DonorAcceptance, RequestStatusLog, RequestReport  # noqa: E402


def _reset_db():
    with app.app_context():
        db.drop_all()
        db.create_all()
        app_module.seed_hospitals()
        app_module.backfill_hospital_coords()
        app_module.seed_demo_data()
        app_module.seed_demo_requests()
        app_module.seed_demo_report_requests()
        app_module.backfill_donor_attributes()
        app_module.backfill_donation_history()
        app_module.seed_extra_demo_donor()


def _get(email):
    with app.app_context():
        u = User.query.filter_by(email=email).first()
        return u.id if u else None


class ControlPanelBase(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.client = app.test_client()

    # -- helpers ----------------------------------------------------------
    def login(self, email, password):
        return self.client.post(
            "/login", data={"email": email, "password": password},
            follow_redirects=False,
        )

    def login_admin(self):
        self.login("admin@bloodplatform.com", "admin123")

    def login_patient(self):
        self.login("patient@bloodplatform.com", "patient123")

    def login_donor(self):
        self.login("donor@bloodplatform.com", "donor123")

    def logout(self):
        self.client.get("/logout")

    def assert_redirects_to(self, response, path):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].rstrip("/").endswith(path.rstrip("/")),
            f"expected redirect to {path!r}, got {response.headers['Location']!r}",
        )

    def _make_request(self, requester_email, blood_group="A+", status="Pending"):
        requester_id = _get(requester_email)
        with app.app_context():
            req = BloodRequest(
                patient_name="Test Patient",
                requester_id=requester_id,
                blood_group=blood_group,
                units_needed=2,
                hospital="Dhaka Medical College Hospital",
                hospital_type=0,
                condition_score=4,
                condition_source="manual",
                hours_needed=6,
                age_risk=2,
                notes="Test request",
                urgency_level="High",
                status=status,
            )
            db.session.add(req)
            db.session.flush()
            db.session.add(RequestStatusLog(
                request_id=req.id, old_status=None,
                new_status=status, changed_by_id=requester_id,
            ))
            db.session.commit()
            return req.id

    def _accept(self, req_id, donor_email):
        donor_id = _get(donor_email)
        with app.app_context():
            acc = DonorAcceptance(donor_id=donor_id, request_id=req_id, status="Accepted")
            db.session.add(acc)
            db.session.commit()
            return acc.id


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class RbacTests(ControlPanelBase):
    def test_dashboard_redirects_by_role(self):
        self.login_admin()
        r = self.client.get("/dashboard", follow_redirects=False)
        self.assert_redirects_to(r, "/admin-dashboard")

        self.logout()
        self.login_patient()
        r = self.client.get("/dashboard", follow_redirects=False)
        self.assert_redirects_to(r, "/patient-dashboard")

        self.logout()
        self.login_donor()
        r = self.client.get("/dashboard", follow_redirects=False)
        self.assert_redirects_to(r, "/donor-dashboard")

    def test_admin_redirects_legacy_route(self):
        self.login_admin()
        r = self.client.get("/admin", follow_redirects=False)
        self.assert_redirects_to(r, "/admin-dashboard")

    def test_patient_blocked_from_admin_routes(self):
        self.login_patient()
        for path in ["/admin-dashboard", "/admin", "/admin/auth-logs",
                     "/admin/ai-insights", "/admin/donations"]:
            r = self.client.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 302, f"{path} should be blocked")
            self.assertTrue(r.headers["Location"].endswith("/dashboard"),
                            f"{path} should bounce to dashboard")

    def test_donor_blocked_from_admin_routes(self):
        self.login_donor()
        r = self.client.get("/admin-dashboard", follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_admin_blocked_from_user_dashboards(self):
        self.login_admin()
        for path in ["/patient-dashboard", "/donor-dashboard"]:
            r = self.client.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 302, f"{path} should be admin-blocked")

    def test_donor_blocked_from_patient_dashboard(self):
        self.login_donor()
        r = self.client.get("/patient-dashboard", follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get("/admin-dashboard", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_restricted_user_cannot_login(self):
        with app.app_context():
            donor = User.query.filter_by(email="donor@bloodplatform.com").first()
            donor.is_restricted = True
            db.session.commit()
        r = self.login("donor@bloodplatform.com", "donor123")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        # Not authenticated: hitting a protected page bounces to login.
        r = self.client.get("/donor-dashboard", follow_redirects=False)
        self.assertIn("/login", r.headers["Location"])

    def test_admin_cannot_restrict_self(self):
        self.login_admin()
        admin_id = _get("admin@bloodplatform.com")
        r = self.client.post(
            f"/admin/user/{admin_id}/restrict", follow_redirects=False
        )
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            admin = db.session.get(User, admin_id)
            self.assertFalse(admin.is_restricted)

    def test_admin_restrict_and_restore_donor(self):
        self.login_admin()
        donor_id = _get("donor@bloodplatform.com")
        r = self.client.post(f"/admin/user/{donor_id}/restrict", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            donor = db.session.get(User, donor_id)
            self.assertTrue(donor.is_restricted)
        # Restricted donor cannot log in.
        self.logout()
        r = self.login("donor@bloodplatform.com", "donor123")
        self.assertIn("/login", r.headers["Location"])
        # Admin restores the account.
        self.login_admin()
        r = self.client.post(f"/admin/user/{donor_id}/restore", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            donor = db.session.get(User, donor_id)
            self.assertFalse(donor.is_restricted)


# ---------------------------------------------------------------------------
# Role dashboards
# ---------------------------------------------------------------------------

class DashboardTests(ControlPanelBase):
    def test_admin_dashboard_renders(self):
        self.login_admin()
        r = self.client.get("/admin-dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        for probe in ["Admin Control Panel", "Blood Request Moderation",
                      "Registered Users", "Under Review", "Pending"]:
            self.assertIn(probe, body)

    def test_patient_dashboard_renders_own_requests(self):
        self.login_patient()
        r = self.client.get("/patient-dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        for probe in ["My Blood Requests", "Donor Offers", "New Blood Request"]:
            self.assertIn(probe, body)

    def test_donor_dashboard_renders_eligibility(self):
        self.login_donor()
        r = self.client.get("/donor-dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        for probe in ["Donation Status", "Eligibility", "Donation History", "Availability"]:
            self.assertIn(probe, body)


# ---------------------------------------------------------------------------
# Request workflow
# ---------------------------------------------------------------------------

class WorkflowTests(ControlPanelBase):
    def test_admin_approve_notifies_requester_and_donors(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_admin()
        r = self.client.post(
            f"/request/{req_id}/status",
            data={"new_status": "Approved"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Approved")
            requester_id = _get("patient@bloodplatform.com")
            has_requester_notif = Notification.query.filter_by(
                user_id=requester_id, request_id=req_id,
                notification_type="request_approved",
            ).count() >= 1
            self.assertTrue(has_requester_notif, "requester should be notified")
            donor_notifs = Notification.query.filter(
                Notification.request_id == req_id,
                Notification.notification_type == "request_approved",
                Notification.user_id != requester_id,
            ).all()
            self.assertTrue(donor_notifs, "matching donors should be notified")

    def test_admin_under_review_notifies_requester(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_admin()
        self.client.post(
            f"/request/{req_id}/status", data={"new_status": "Under Review"}
        )
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Under Review")
            requester_id = _get("patient@bloodplatform.com")
            self.assertTrue(Notification.query.filter_by(
                user_id=requester_id, request_id=req_id,
                notification_type="request_review",
            ).count() >= 1)

    def test_reject_requires_reason(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_admin()
        # Without a reason the rejection is refused.
        r = self.client.post(
            f"/request/{req_id}/status", data={"new_status": "Rejected"}
        )
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertNotEqual(req.status, "Rejected")
        # With a reason it goes through and notifies the patient.
        r = self.client.post(
            f"/request/{req_id}/status",
            data={"new_status": "Rejected", "rejection_reason": "Card missing"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Rejected")
            self.assertEqual(req.rejection_reason, "Card missing")
            requester_id = _get("patient@bloodplatform.com")
            self.assertTrue(Notification.query.filter_by(
                user_id=requester_id, request_id=req_id,
                notification_type="request_rejected",
            ).count() >= 1)

    def test_requester_can_cancel_own_request(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_patient()
        r = self.client.post(
            f"/request/{req_id}/status",
            data={"new_status": "Cancelled"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Cancelled")

    def test_requester_cannot_approve_own_request(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_patient()
        self.client.post(f"/request/{req_id}/status", data={"new_status": "Approved"})
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Pending")

    def test_donor_cannot_change_status(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_donor()
        self.client.post(f"/request/{req_id}/status", data={"new_status": "Cancelled"})
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Pending")

    def test_fulfill_records_donation_and_notifies_donor(self):
        req_id = self._make_request("patient@bloodplatform.com")
        donor_id = _get("donor@bloodplatform.com")
        with app.app_context():
            donor = db.session.get(User, donor_id)
            donor.donation_count = 0
            db.session.commit()
        self._accept(req_id, "donor@bloodplatform.com")
        self.login_admin()
        self.client.post(f"/request/{req_id}/status", data={"new_status": "Fulfilled"})
        with app.app_context():
            req = db.session.get(BloodRequest, req_id)
            self.assertEqual(req.status, "Fulfilled")
            donation = Donation.query.filter_by(donor_id=donor_id).order_by(
                Donation.id.desc()
            ).first()
            self.assertIsNotNone(donation)
            self.assertIn(f"#{req_id}", donation.notes or "")
            self.assertIsNotNone(donation.next_eligible_date)
            self.assertTrue(Notification.query.filter_by(
                user_id=donor_id, notification_type="donation_recorded",
            ).count() >= 1)

    def test_new_request_via_route_notifies_admins(self):
        # Route through the real /request/new flow with the vision model
        # stubbed out (it isn't installed on CI).
        app_module._allowed_report_image = lambda filename: True
        app_module.extract_urgency_report_values_from_image = lambda file: (
            "ok",
            {"name": "Test Patient", "age": 30, "gender": "M",
             "hemoglobin": 7.0, "wbc": 9.0, "platelet": 150, "hematocrit": 21},
            "{}",
        )
        self.login_patient()
        data = {
            "patient_name": "Test Patient",
            "blood_group": "O+",
            "units_needed": "2",
            "hospital": "Dhaka Medical College Hospital",
            "hospital_type": "0",
            "hours_needed": "6",
            "notes": "Test",
        }
        data["report_image"] = (BytesIO(b"fake-image-bytes"), "report.jpg")
        r = self.client.post("/request/new", data=data, follow_redirects=False,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        admin_id = _get("admin@bloodplatform.com")
        with app.app_context():
            self.assertTrue(Notification.query.filter_by(
                user_id=admin_id, notification_type="request_created",
            ).count() >= 1)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationTests(ControlPanelBase):
    def test_mark_all_read(self):
        donor_id = _get("donor@bloodplatform.com")
        with app.app_context():
            for i in range(3):
                db.session.add(Notification(
                    user_id=donor_id, message=f"n{i}",
                    title="Test", notification_type="test",
                ))
            db.session.commit()
        self.login_donor()
        self.client.post("/notifications/mark-read")
        with app.app_context():
            unread = Notification.query.filter_by(
                user_id=donor_id, is_read=False
            ).count()
            self.assertEqual(unread, 0)

    def test_mark_single_read(self):
        donor_id = _get("donor@bloodplatform.com")
        with app.app_context():
            n = Notification(
                user_id=donor_id, message="single", title="Test",
                notification_type="test", link=None,
            )
            db.session.add(n)
            db.session.commit()
            nid = n.id
        self.login_donor()
        self.client.get(f"/notifications/{nid}/read")
        with app.app_context():
            n = db.session.get(Notification, nid)
            self.assertTrue(n.is_read)

    def test_notification_page_shows_title_and_type(self):
        donor_id = _get("donor@bloodplatform.com")
        with app.app_context():
            db.session.add(Notification(
                user_id=donor_id, message="hello", title="Matching Blood Request",
                notification_type="request_approved", request_id=1,
            ))
            db.session.commit()
        self.login_donor()
        body = self.client.get("/notifications").get_data(as_text=True)
        self.assertIn("Matching Blood Request", body)
        self.assertIn("request approved", body)

    def test_donor_offer_notifies_requester(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_donor()
        self.client.post(f"/request/{req_id}/accept")
        requester_id = _get("patient@bloodplatform.com")
        with app.app_context():
            self.assertTrue(Notification.query.filter_by(
                user_id=requester_id, request_id=req_id,
                notification_type="donor_offer",
            ).count() >= 1)

    def test_acceptance_decision_notifies_donor(self):
        req_id = self._make_request("patient@bloodplatform.com")
        acc_id = self._accept(req_id, "donor@bloodplatform.com")
        donor_id = _get("donor@bloodplatform.com")
        self.login_patient()
        self.client.post(
            f"/acceptance/{acc_id}/status", data={"new_status": "Accepted"}
        )
        with app.app_context():
            self.assertTrue(Notification.query.filter_by(
                user_id=donor_id, notification_type="donor_offer_accepted",
            ).count() >= 1)


# ---------------------------------------------------------------------------
# Request reporting / moderation
# ---------------------------------------------------------------------------

class ReportTests(ControlPanelBase):
    def test_report_notifies_admins_and_shows_in_queue(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_donor()
        r = self.client.post(
            f"/request/{req_id}/report",
            data={"reason": "Fake contact number"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            report = RequestReport.query.filter_by(request_id=req_id).first()
            self.assertIsNotNone(report)
            self.assertEqual(report.reason, "Fake contact number")
            admin_id = _get("admin@bloodplatform.com")
            self.assertTrue(Notification.query.filter_by(
                user_id=admin_id, notification_type="request_reported",
                request_id=req_id,
            ).count() >= 1)
        # The admin moderation queue shows the reported request.
        self.logout()
        self.login_admin()
        body = self.client.get("/admin-dashboard?reported=1").get_data(as_text=True)
        self.assertIn("Fake contact number", body)
        self.assertIn("Test Patient", body)
        # Admin clears the reports.
        r = self.client.post(
            f"/admin/request/{req_id}/reports/clear", follow_redirects=False
        )
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            self.assertEqual(RequestReport.query.filter_by(request_id=req_id).count(), 0)

    def test_cannot_report_own_request(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_patient()
        self.client.post(f"/request/{req_id}/report", data={"reason": "test"})
        with app.app_context():
            self.assertEqual(RequestReport.query.filter_by(request_id=req_id).count(), 0)

    def test_duplicate_report_blocked(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_donor()
        self.client.post(f"/request/{req_id}/report", data={"reason": "one"})
        self.client.post(f"/request/{req_id}/report", data={"reason": "two"})
        with app.app_context():
            reports = RequestReport.query.filter_by(request_id=req_id).all()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].reason, "one")

    def test_report_requires_reason(self):
        req_id = self._make_request("patient@bloodplatform.com")
        self.login_donor()
        self.client.post(f"/request/{req_id}/report", data={"reason": "   "})
        with app.app_context():
            self.assertEqual(RequestReport.query.filter_by(request_id=req_id).count(), 0)


# ---------------------------------------------------------------------------
# Donor dashboard filters
# ---------------------------------------------------------------------------

class DonorFilterTests(ControlPanelBase):
    def _make_urgent_pair(self):
        # Two O+ requests compatible with the O+ demo donor.
        with app.app_context():
            patient_id = _get("patient@bloodplatform.com")
            for name, urgency in [("CritPatient", "Critical"), ("LowPatient", "Low")]:
                req = BloodRequest(
                    patient_name=name,
                    requester_id=patient_id,
                    blood_group="O+",
                    units_needed=2,
                    hospital="Dhaka Medical College Hospital",
                    hospital_type=0,
                    condition_score=4,
                    condition_source="manual",
                    hours_needed=6,
                    age_risk=2,
                    notes="filter test",
                    urgency_level=urgency,
                    status="Approved",
                )
                db.session.add(req)
            db.session.commit()

    def test_urgency_filter(self):
        self._make_urgent_pair()
        self.login_donor()
        body = self.client.get("/donor-dashboard?urgency=Critical").get_data(as_text=True)
        self.assertIn("CritPatient", body)
        self.assertNotIn("LowPatient", body)

    def test_blood_group_filter(self):
        self._make_urgent_pair()
        self.login_donor()
        body = self.client.get("/donor-dashboard?blood_group=O%2B").get_data(as_text=True)
        self.assertIn("CritPatient", body)
        self.assertIn("LowPatient", body)

    def test_search_filter(self):
        self._make_urgent_pair()
        self.login_donor()
        body = self.client.get("/donor-dashboard?q=CritPatient").get_data(as_text=True)
        self.assertIn("CritPatient", body)
        self.assertNotIn("LowPatient", body)


# ---------------------------------------------------------------------------
# Cancellation notifications, eligible-again, urgency override, patient donors
# ---------------------------------------------------------------------------

class WorkflowAdditionsTests(ControlPanelBase):
    def test_cancel_notifies_offering_donors(self):
        req_id = self._make_request("patient@bloodplatform.com")
        donor_id = _get("donor@bloodplatform.com")
        self.login_donor()
        self.client.post(f"/request/{req_id}/accept")
        self.logout()
        self.login_patient()
        self.client.post(f"/request/{req_id}/status", data={"new_status": "Cancelled"})
        with app.app_context():
            self.assertTrue(Notification.query.filter_by(
                user_id=donor_id, request_id=req_id,
                notification_type="request_cancelled",
            ).count() >= 1)

    def test_eligible_again_notification_sent_once(self):
        donor_id = _get("donor@bloodplatform.com")
        with app.app_context():
            donor = db.session.get(User, donor_id)
            donor.gender = "M"
            donor.donation_count = 1
            from datetime import datetime, timedelta, timezone as tz
            now = datetime.now(tz.utc)
            # Clear any seeded history so the latest donation is the one below.
            Donation.query.filter_by(donor_id=donor_id).delete()
            db.session.add(Donation(
                donor_id=donor_id,
                donation_date=now - timedelta(days=200),
                donation_time="10:00",
                blood_group="O+",
                bags=1,
                donation_location="Dhaka",
                next_eligible_date=now - timedelta(days=110),
                status="Completed",
            ))
            donor.last_donation_date = now - timedelta(days=200)
            db.session.commit()
        self.login_donor()
        body = self.client.get("/donor-dashboard").get_data(as_text=True)
        self.assertIn("Eligible", body)
        self.client.get("/donor-dashboard")  # second visit: no duplicate
        with app.app_context():
            count = Notification.query.filter_by(
                user_id=donor_id, notification_type="eligible_again"
            ).count()
            self.assertEqual(count, 1)

    def test_manual_urgency_override(self):
        app_module._allowed_report_image = lambda filename: True
        app_module.extract_urgency_report_values_from_image = lambda file: (
            "ok",
            {"name": "Test Patient", "age": 30, "gender": "M",
             "hemoglobin": 7.0, "wbc": 9.0, "platelet": 150, "hematocrit": 21},
            "{}",
        )
        self.login_patient()
        data = {
            "patient_name": "Override Patient",
            "blood_group": "O+",
            "units_needed": "2",
            "hospital": "Dhaka Medical College Hospital",
            "hospital_type": "0",
            "hours_needed": "6",
            "urgency_override": "Low",
            "notes": "override test",
        }
        data["report_image"] = (BytesIO(b"fake-image-bytes"), "report.jpg")
        r = self.client.post("/request/new", data=data, follow_redirects=False,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            req = BloodRequest.query.filter_by(
                patient_name="Override Patient"
            ).first()
            self.assertIsNotNone(req)
            self.assertEqual(req.urgency_level, "Low")

    def test_patient_dashboard_lists_suitable_donors(self):
        self._make_request("patient@bloodplatform.com")
        self.login_patient()
        body = self.client.get("/patient-dashboard").get_data(as_text=True)
        self.assertIn("Suitable donors", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
