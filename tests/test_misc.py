import re
from datetime import timedelta

import psycopg2
import pytest

from reports_keys import KEYS


def seed(admin, make_student, make_batch, belt, today):
    bid = make_batch()
    sid = make_student(batch_id=bid, full_name="Kai Mori", dob=today.replace(year=today.year - 20).isoformat(), guardian_name="", guardian_phone="")
    admin.post("/attendance/save", data={"batch_id": bid, "date": today.isoformat(), f"status_{sid}": "present"})
    admin.post("/fees/new", data=dict(target="student", student_id=sid, description="Tuition", period_label="2026-10", amount="900", due_date=(today - timedelta(days=2)).isoformat()))
    admin.post("/fees/1/pay", data={"amount": "300", "method": "cash", "idem_key": "seed"})
    admin.post("/exams/new", data=dict(student_id=sid, target_belt_id=belt("Yellow Belt"), exam_date=(today + timedelta(days=4)).isoformat(), exam_fee="100", eligible="on"))
    return bid, sid


def test_dashboard_numbers_come_from_the_database(admin, make_student, make_batch, belt, today):
    assert b"No students yet" in admin.get("/").data  # empty database is handled
    seed(admin, make_student, make_batch, belt, today)
    page = admin.get("/").data.decode()
    assert re.search(r"Registered students</span><span class=\"v\">1<", page)
    assert "₹300.00" in page and "₹600.00 overdue (1)" in page
    assert "100% of 1 marked" in page
    assert "chart-data" in page and "Kai Mori" in page


def test_dashboard_chart_json_is_valid(admin, make_student, make_batch, belt, today):
    import json
    seed(admin, make_student, make_batch, belt, today)
    html = admin.get("/").data.decode()
    data = json.loads(re.search(r'id="chart-data"[^>]*>(.*?)</script>', html, re.S).group(1))
    assert len(data["fees"]) == 6 and data["fees"][-1]["value"] == 300.0
    assert len(data["attendance"]) == 30 and data["attendance"][-1]["value"] == 100.0
    assert data["enrollment"][-1]["value"] == 1 and sum(b["value"] for b in data["belts"]) == 1


@pytest.mark.parametrize("key", KEYS)
def test_every_report_renders_and_exports(admin, make_student, make_batch, belt, today, key):
    seed(admin, make_student, make_batch, belt, today)
    assert admin.get(f"/reports/{key}").status_code == 200
    r = admin.get(f"/reports/{key}?format=csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"


def test_report_filters_and_authorization(admin, instructor, make_student, make_batch, belt, today):
    seed(admin, make_student, make_batch, belt, today)
    assert b"Tuition" in admin.get("/reports/outstanding").data
    assert b"Tuition" not in admin.get("/reports/outstanding?status=pending").data
    assert b"Tuition" in admin.get("/reports/outstanding?status=overdue").data
    assert b"Kai Mori" in admin.get("/reports/attendance_students").data
    assert instructor.get("/reports/attendance_students").status_code == 200
    assert instructor.get("/reports/outstanding?format=csv").status_code == 403
    assert b"Outstanding fees" not in instructor.get("/reports/").data
    assert admin.get("/reports/nope").status_code == 404
    assert b"March" not in admin.get("/reports/attendance?period=month&from=bad").data


def test_all_pages_render_with_data(admin, make_student, make_batch, belt, today):
    bid, sid = seed(admin, make_student, make_batch, belt, today)
    pages = ["/", "/students/", f"/students/{sid}", f"/students/{sid}/edit", "/students/new", "/attendance/", f"/attendance/?batch={bid}",
             "/attendance/records", "/batches/", f"/batches/{bid}", f"/batches/{bid}/edit", "/batches/new", "/fees/", "/fees/1", "/fees/new",
             f"/fees/new?student_id={sid}", "/fees/plans", "/fees/payments", "/fees/receipt/1", "/exams", "/exams/1", "/exams/new",
             f"/exams/new?student_id={sid}", "/belts", "/belts?edit=2", "/reports/", "/account/password"]
    for p in pages:
        r = admin.get(p)
        assert r.status_code == 200, p


def test_error_pages_are_friendly(admin, instructor, anon):
    r = admin.get("/nope")
    assert r.status_code == 404 and b"Page not found" in r.data and b"Traceback" not in r.data
    r = instructor.get("/belts")
    assert r.status_code == 403 and b"limited to administrators" in r.data
    assert anon.get("/nope").status_code == 404
    assert admin.delete("/students/new").status_code == 405


def test_database_failure_shows_error_and_saves_nothing(admin, monkeypatch, belt):
    import db_config
    def boom():
        raise psycopg2.OperationalError("could not connect: secret-host")
    monkeypatch.setattr(db_config, "get_conn", boom)
    admin.application.config["PROPAGATE_EXCEPTIONS"] = False
    r = admin.post("/students/new", data=dict(full_name="Ghost"))
    assert r.status_code == 302 or r.status_code == 500  # anonymous-style redirect or friendly error
    r = admin.get("/students/")
    assert r.status_code == 500 and b"couldn&#39;t reach the database" in r.data and b"secret-host" not in r.data


def test_failed_write_rolls_back_and_reports_error(admin, make_student, monkeypatch, q):
    import blueprints.students as st
    sid = make_student()
    original = st.audit
    def failing_audit(*a, **k):
        raise psycopg2.errors.InternalError_("audit table unavailable")
    monkeypatch.setattr(st, "audit", failing_audit)
    r = admin.post(f"/students/{sid}/edit", data=dict(full_name="Changed", dob="2010-05-12", phone="9999999999", guardian_name="P",
                                                      guardian_phone="9876500000", joining_date="2025-01-10"))
    assert r.status_code == 500 and b"Nothing was saved" in r.data
    assert q("SELECT full_name FROM students WHERE id=%s", (sid,))[0]["full_name"] != "Changed"


def test_sql_injection_is_inert(admin, make_student, q):
    make_student(full_name="Safe One")
    r = admin.get("/students/?q=%27%3B%20DROP%20TABLE%20students%3B--&sort=name%3B%20DROP%20TABLE%20students")
    assert r.status_code == 200
    assert q("SELECT count(*) AS n FROM students")[0]["n"] == 1


def test_xss_is_escaped(admin, make_student):
    sid = make_student(full_name="<script>alert(1)</script>")
    page = admin.get(f"/students/{sid}").data
    assert b"<script>alert(1)</script>" not in page and b"&lt;script&gt;" in page


def test_responsive_and_accessible_markup(admin, make_student):
    page = admin.get("/students/new").data.decode()
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in page
    assert 'id="nav-toggle"' in page and 'aria-expanded="false"' in page and 'aria-controls="sidebar"' in page
    assert 'class="skip-link"' in page and 'lang="en"' in page
    ids = set(re.findall(r'<input[^>]*\bid="([^"]+)"', page)) - {"photo"} | {"photo"}
    for i in ids:
        assert f'for="{i}"' in page, f"input {i} has no label"
    make_student()
    assert 'data-label="Student"' in admin.get("/students/").data.decode()
    css = open("static/css/style.css").read()
    assert "@media (max-width:900px)" in css and "@media (max-width:720px)" in css and "table.responsive" in css


def test_login_page_has_password_toggle(anon):
    page = anon.get("/login").data.decode()
    assert "data-toggle-password" in page and 'type="password"' in page


def test_destructive_actions_ask_for_confirmation(admin, make_student):
    sid = make_student()
    page = admin.get(f"/students/{sid}").data.decode()
    assert page.count("data-confirm=") >= 2  # deactivate and archive


def test_migrations_are_idempotent(app):
    import db_config
    assert db_config.run_migrations() == []


def test_data_persists_across_app_restarts(app, admin, make_student, q):
    sid = make_student(full_name="Persistent Pat")
    from app import create_app
    fresh = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SECRET_KEY": "another"})
    import db_config
    with fresh.app_context():
        assert db_config.fetch_one("SELECT full_name FROM students WHERE id=%s", (sid,))["full_name"] == "Persistent Pat"
