from datetime import timedelta
from decimal import Decimal

import pytest


@pytest.fixture()
def exam(admin, make_student, belt, today, q):
    sid = make_student()
    r = admin.post("/exams/new", data=dict(student_id=sid, target_belt_id=belt("Yellow Belt"), exam_date=today.isoformat(),
                                           exam_fee="0", eligible="on"))
    assert r.status_code == 302
    return sid, int(r.headers["Location"].rstrip("/").split("/")[-1])


def current_belt(q, sid):
    return q("SELECT b.name FROM students s JOIN belt_ranks b ON b.id=s.current_belt_id WHERE s.id=%s", (sid,))[0]["name"]


def test_schedule_exam_and_defaults(admin, exam, q):
    assert q("SELECT result FROM examinations")[0]["result"] == "pending"
    assert b"Yellow Belt" in admin.get(f"/exams/{exam[1]}").data


def test_validation_target_must_be_higher_and_one_pending(admin, make_student, belt, today, exam):
    sid, _ = exam
    r = admin.post("/exams/new", data=dict(student_id=sid, target_belt_id=belt("Orange Belt"), exam_date=today.isoformat(), exam_fee="0"))
    assert b"already has a pending examination" in r.data
    other = make_student()
    r = admin.post("/exams/new", data=dict(student_id=other, target_belt_id=belt("White Belt"), exam_date=today.isoformat(), exam_fee="0"))
    assert b"must be higher" in r.data
    r = admin.post("/exams/new", data=dict(student_id=other, target_belt_id=belt("Yellow Belt"), exam_date="", exam_fee="-3"))
    assert b"Enter the examination date" in r.data and b"as a number" in r.data


def test_passing_never_promotes_automatically(admin, exam, q):
    sid, xid = exam
    admin.post(f"/exams/{xid}/result", data={"result": "passed", "score": "88"})
    assert q("SELECT result FROM examinations")[0]["result"] == "passed"
    assert current_belt(q, sid) == "White Belt"
    assert q("SELECT count(*) AS n FROM belt_promotions")[0]["n"] == 0
    assert b"Approve promotion" in admin.get(f"/exams/{xid}").data


def test_promotion_requires_passed_result(admin, exam, q):
    sid, xid = exam
    admin.post(f"/exams/{xid}/promote", data={})
    assert current_belt(q, sid) == "White Belt"
    admin.post(f"/exams/{xid}/result", data={"result": "failed"})
    admin.post(f"/exams/{xid}/promote", data={})
    assert current_belt(q, sid) == "White Belt" and q("SELECT count(*) AS n FROM belt_promotions")[0]["n"] == 0


def test_instructor_approves_promotion_with_history_and_certificate(admin, instructor, exam, q):
    sid, xid = exam
    instructor.post(f"/exams/{xid}/result", data={"result": "passed", "remarks": "Strong kata"})
    r = instructor.post(f"/exams/{xid}/promote", data={"remarks": "Well done"})
    assert r.status_code == 302 and "certificate" in r.headers["Location"]
    assert current_belt(q, sid) == "Yellow Belt"
    p = q("SELECT p.*, a.username FROM belt_promotions p JOIN admins a ON a.id=p.approved_by")[0]
    assert p["username"] == "sensei" and p["remarks"] == "Well done"
    cert = instructor.get(r.headers["Location"])
    assert cert.status_code == 200 and b"Certificate of Promotion" in cert.data and b"Yellow Belt" in cert.data
    # cannot be approved twice, and the result is locked
    instructor.post(f"/exams/{xid}/promote", data={})
    assert q("SELECT count(*) AS n FROM belt_promotions")[0]["n"] == 1
    instructor.post(f"/exams/{xid}/result", data={"result": "failed"})
    assert q("SELECT result FROM examinations")[0]["result"] == "passed"
    assert b"White Belt" in admin.get(f"/students/{sid}").data  # history shows previous belt


def test_ineligible_exam_needs_explicit_confirmation(admin, make_student, belt, today, q):
    sid = make_student()
    xid = int(admin.post("/exams/new", data=dict(student_id=sid, target_belt_id=belt("Yellow Belt"), exam_date=today.isoformat(),
                                                 exam_fee="0")).headers["Location"].rstrip("/").split("/")[-1])
    admin.post(f"/exams/{xid}/result", data={"result": "passed"})
    admin.post(f"/exams/{xid}/promote", data={})
    assert current_belt(q, sid) == "White Belt"
    admin.post(f"/exams/{xid}/promote", data={"confirm_ineligible": "on"})
    assert current_belt(q, sid) == "Yellow Belt"


def test_future_promotion_date_rejected(admin, exam, today, q):
    sid, xid = exam
    admin.post(f"/exams/{xid}/result", data={"result": "passed"})
    admin.post(f"/exams/{xid}/promote", data={"promoted_on": (today + timedelta(days=3)).isoformat()})
    assert current_belt(q, sid) == "White Belt"


def test_stale_exam_cannot_promote_after_belt_changed(admin, exam, belt, q):
    import db_config
    sid, xid = exam
    admin.post(f"/exams/{xid}/result", data={"result": "passed"})
    with admin.application.app_context():
        with db_config.transaction() as cur:
            cur.execute("UPDATE students SET current_belt_id=%s WHERE id=%s", (belt("Orange Belt"), sid))
    admin.post(f"/exams/{xid}/promote", data={})
    assert q("SELECT count(*) AS n FROM belt_promotions")[0]["n"] == 0


def test_exam_fee_creates_a_fee_record(admin, make_student, belt, today, q):
    sid = make_student()
    xid = int(admin.post("/exams/new", data=dict(student_id=sid, target_belt_id=belt("Yellow Belt"), exam_date=today.isoformat(),
                                                 exam_fee="250.50")).headers["Location"].rstrip("/").split("/")[-1])
    fee = q("SELECT * FROM v_student_fees")[0]
    assert fee["amount_due"] == Decimal("250.50") and fee["description"].startswith("Grading fee")
    assert q("SELECT fee_id FROM examinations")[0]["fee_id"] == fee["id"]


def test_eligibility_is_advisory(admin, make_student, belt, today):
    sid = make_student(joining_date=today.isoformat())
    page = admin.get(f"/exams/new?student_id={sid}")
    assert b"Eligibility for Yellow Belt" in page.data and b"Not met" in page.data


def test_belt_configuration_admin_only(admin, instructor, belt, q):
    assert instructor.post("/belts", data={"name": "Purple Belt"}).status_code == 403
    r = admin.post("/belts", data={"name": "Purple Belt", "rank_order": "65", "color_hex": "#7c3aed", "min_months": "4",
                                   "min_attendance_pct": "80", "requirements": "Kata 5", "is_active": "on"})
    assert r.status_code == 302
    orders = [r["name"] for r in q("SELECT name FROM belt_ranks ORDER BY rank_order")]
    assert orders.index("Purple Belt") == orders.index("Blue Belt") + 1
    assert b"already uses this order" in admin.post("/belts", data={"name": "Other", "rank_order": "65", "color_hex": "#000000", "min_months": "0", "min_attendance_pct": "0"}).data
    assert b"Choose a colour" in admin.post("/belts", data={"name": "Z", "rank_order": "66", "color_hex": "red", "min_months": "0", "min_attendance_pct": "0"}).data


def test_default_belts_seeded_in_order(q):
    names = [r["name"] for r in q("SELECT name FROM belt_ranks ORDER BY rank_order")]
    assert names == ["White Belt", "Yellow Belt", "Orange Belt", "J Green Belt", "S Green Belt", "Blue Belt",
                     "3 Brown Belt", "2 Brown Belt", "1 Brown Belt", "Black Belt"]
