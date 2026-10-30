from datetime import timedelta


def setup_batch(admin, make_student, make_batch, n=3):
    bid = make_batch()
    ids = [make_student() for _ in range(n)]
    for sid in ids:
        admin.post(f"/batches/{bid}/enroll", data={"student_id": sid})
    return bid, ids


def test_capacity_enforced_and_admin_override(admin, instructor, make_student, make_batch, q):
    bid = make_batch(capacity="2")
    a, b, c = make_student(), make_student(), make_student()
    for s in (a, b):
        admin.post(f"/batches/{bid}/enroll", data={"student_id": s})
    r = instructor.post(f"/batches/{bid}/enroll", data={"student_id": c, "override": "on"}, follow_redirects=True)
    assert b"is full" in r.data
    assert q("SELECT count(*) AS n FROM batch_enrollments WHERE batch_id=%s", (bid,))[0]["n"] == 2
    admin.post(f"/batches/{bid}/enroll", data={"student_id": c, "override": "on"})
    assert q("SELECT count(*) AS n FROM batch_enrollments WHERE batch_id=%s", (bid,))[0]["n"] == 3
    assert q("SELECT 1 FROM audit_logs WHERE action='capacity_override'")


def test_new_student_in_full_batch_rolls_back_whole_save(admin, make_student, make_batch, q, belt):
    bid = make_batch(capacity="1")
    make_student(batch_id=bid)
    r = admin.post("/students/new", data=dict(full_name="Late", dob="2000-01-01", phone="9876543210", joining_date="2025-01-01",
                                              belt_id=belt("White Belt"), batch_id=bid))
    assert r.status_code == 200 and b"is full" in r.data
    assert not q("SELECT 1 FROM students WHERE full_name='Late'")


def test_lower_capacity_below_enrolment_rejected(admin, make_student, make_batch):
    bid, _ = setup_batch(admin, make_student, make_batch)
    r = admin.post(f"/batches/{bid}/edit", data=dict(name="X", instructor="S", training_days=["Mon"], start_time="10:00",
                                                     end_time="11:00", capacity="2", is_active="on"))
    assert r.status_code == 200 and b"3 students are enrolled" in r.data


def test_batch_validation(admin):
    r = admin.post("/batches/new", data=dict(name="", instructor="", start_time="18:00", end_time="17:00", capacity="0"))
    assert b"Enter a batch name" in r.data and b"after the start time" in r.data and b"at least one training day" in r.data


def test_mark_attendance_bulk_and_percentage(admin, make_student, make_batch, today, q):
    bid, (a, b, c) = setup_batch(admin, make_student, make_batch)
    r = admin.post("/attendance/save", data={"batch_id": bid, "date": today.isoformat(), f"status_{a}": "present",
                                              f"status_{b}": "late", f"status_{c}": "absent"})
    assert r.status_code == 302
    rows = {r["student_id"]: r["status"] for r in q("SELECT student_id, status FROM attendance")}
    assert rows == {a: "present", b: "late", c: "absent"}
    page = admin.get(f"/students/{a}").data
    assert b"100.0%" in page
    assert b"0.0%" in admin.get(f"/students/{c}").data
    assert b"Today" in admin.get(f"/attendance/?batch={bid}").data


def test_duplicate_entries_are_prevented(admin, make_student, make_batch, today, q):
    bid, (a, *_) = setup_batch(admin, make_student, make_batch)
    for status in ("present", "present", "absent"):
        admin.post("/attendance/save", data={"batch_id": bid, "date": today.isoformat(), f"status_{a}": status})
    rows = q("SELECT status FROM attendance WHERE student_id=%s", (a,))
    assert len(rows) == 1 and rows[0]["status"] == "absent"
    assert q("SELECT 1 FROM audit_logs WHERE action='attendance_edit'")
    import psycopg2, pytest, db_config
    with pytest.raises(psycopg2.errors.UniqueViolation):  # database-level guarantee
        q("INSERT INTO attendance (student_id, batch_id, session_date, status) VALUES (%s,%s,%s,'present')", (a, bid, today))


def test_status_values_and_students_are_validated(admin, make_student, make_batch, today, q):
    bid, (a, *_) = setup_batch(admin, make_student, make_batch)
    outsider = make_student()
    admin.post("/attendance/save", data={"batch_id": bid, "date": today.isoformat(), f"status_{a}": "hacked", f"status_{outsider}": "present"})
    assert q("SELECT count(*) AS n FROM attendance")[0]["n"] == 0


def test_future_date_rejected(admin, make_student, make_batch, today, q):
    bid, (a, *_) = setup_batch(admin, make_student, make_batch)
    r = admin.post("/attendance/save", data={"batch_id": bid, "date": (today + timedelta(days=2)).isoformat(), f"status_{a}": "present"}, follow_redirects=True)
    assert b"future" in r.data and q("SELECT count(*) AS n FROM attendance")[0]["n"] == 0


def test_old_sessions_need_admin(admin, instructor, make_student, make_batch, today, q):
    bid, (a, *_) = setup_batch(admin, make_student, make_batch)
    old = (today - timedelta(days=10)).isoformat()
    # enrolment starts today, so use a student enrolled earlier via SQL
    import db_config
    with admin.application.app_context():
        with db_config.transaction() as cur:
            cur.execute("UPDATE batch_enrollments SET enrolled_on=%s", (today - timedelta(days=30),))
    r = instructor.post("/attendance/save", data={"batch_id": bid, "date": old, f"status_{a}": "present"}, follow_redirects=True)
    assert b"only be changed by an administrator" in r.data
    assert q("SELECT count(*) AS n FROM attendance")[0]["n"] == 0
    admin.post("/attendance/save", data={"batch_id": bid, "date": old, f"status_{a}": "present"})
    assert q("SELECT count(*) AS n FROM attendance")[0]["n"] == 1


def test_nothing_selected_is_not_reported_as_saved(admin, make_student, make_batch, today):
    bid, _ = setup_batch(admin, make_student, make_batch)
    r = admin.post("/attendance/save", data={"batch_id": bid, "date": today.isoformat()}, follow_redirects=True)
    assert b"Choose a status" in r.data and b"Attendance saved" not in r.data


def test_records_search_and_filters(admin, make_student, make_batch, today):
    bid, (a, b, c) = setup_batch(admin, make_student, make_batch)
    admin.post("/attendance/save", data={"batch_id": bid, "date": today.isoformat(), f"status_{a}": "present", f"status_{b}": "absent"})
    r = admin.get("/attendance/records?status=absent")
    assert b"Student 2" in r.data and b"Student 1" not in r.data
    assert b"No attendance records" in admin.get("/attendance/records?q=zzzz").data
