import io

from PIL import Image


def png_bytes(size=(50, 50)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    return buf.getvalue()


def test_create_student_generates_unique_ids(make_student, q):
    a, b = make_student(), make_student()
    rows = q("SELECT student_code FROM students ORDER BY id")
    assert rows[0]["student_code"].endswith("-0001") and rows[1]["student_code"].endswith("-0002")
    assert rows[0]["student_code"].startswith("DD-")


def test_required_fields_and_friendly_errors(admin):
    r = admin.post("/students/new", data={"full_name": "", "dob": "", "phone": "", "joining_date": ""})
    assert r.status_code == 200
    for msg in (b"Enter the student&#39;s full name", b"valid date of birth", b"Enter a phone number", b"Enter the joining date"):
        assert msg in r.data


def test_minor_needs_guardian_adult_does_not(admin, make_student, q):
    r = admin.post("/students/new", data=dict(full_name="Kid", dob="2018-01-01", phone="9876543210",
                                              joining_date="2025-01-01", belt_id=1))
    assert b"required for students under 18" in r.data
    make_student(full_name="Adult", dob="1990-01-01", guardian_name="", guardian_phone="")
    assert q("SELECT count(*) AS n FROM students")[0]["n"] == 1


def test_invalid_phone_email_and_dob(admin):
    r = admin.post("/students/new", data=dict(full_name="X", dob="2999-01-01", phone="abc", email="nope",
                                              joining_date="2025-01-01", guardian_name="g", guardian_phone="12"))
    assert b"Check the date of birth" in r.data and b"Use 7" in r.data and b"valid email" in r.data


def test_duplicate_student_blocked(make_student, admin):
    make_student(full_name="Same Person", dob="2000-02-02", guardian_name="", guardian_phone="")
    r = admin.post("/students/new", data=dict(full_name="same person", dob="2000-02-02", phone="9876543210",
                                              joining_date="2025-01-01", belt_id=1))
    assert r.status_code == 200 and b"already exists" in r.data


def test_edit_student(admin, make_student, q):
    sid = make_student(full_name="Old Name")
    r = admin.post(f"/students/{sid}/edit", data=dict(full_name="New Name", dob="2010-05-12", phone="9999999999",
                                                      guardian_name="P", guardian_phone="9876500000", joining_date="2025-01-10"))
    assert r.status_code == 302
    row = q("SELECT full_name, phone FROM students WHERE id=%s", (sid,))[0]
    assert row == {"full_name": "New Name", "phone": "9999999999"}
    assert q("SELECT 1 FROM audit_logs WHERE action='update' AND entity='student'")


def test_belt_cannot_be_changed_by_editing(admin, make_student, q, belt):
    sid = make_student()
    admin.post(f"/students/{sid}/edit", data=dict(full_name="A", dob="2010-05-12", phone="9999999999", guardian_name="P",
                                                  guardian_phone="9876500000", joining_date="2025-01-10", belt_id=belt("Black Belt")))
    assert q("SELECT current_belt_id FROM students WHERE id=%s", (sid,))[0]["current_belt_id"] == belt("White Belt")


def test_deactivate_and_archive(admin, instructor, make_student, make_batch, q):
    sid, bid = make_student(), make_batch()
    admin.post(f"/batches/{bid}/enroll", data={"student_id": sid})
    assert instructor.post(f"/students/{sid}/status", data={"status": "inactive"}).status_code == 302
    assert q("SELECT status FROM students WHERE id=%s", (sid,))[0]["status"] == "inactive"
    assert not q("SELECT 1 FROM batch_enrollments WHERE student_id=%s AND ended_on IS NULL", (sid,))
    assert instructor.post(f"/students/{sid}/status", data={"status": "archived"}).status_code == 403
    assert admin.post(f"/students/{sid}/status", data={"status": "archived"}).status_code == 302
    assert admin.post(f"/students/{sid}/status", data={"status": "bogus"}).status_code == 400
    assert q("SELECT status FROM students WHERE id=%s", (sid,))[0]["status"] == "archived"


def test_search_filter_sort_paginate(admin, make_student, belt):
    for i in range(23):
        make_student(full_name=f"Pupil {i:02d}", phone=f"90000000{i:02d}")
    make_student(full_name="Zed Unique", belt_id=belt("Blue Belt"))
    r = admin.get("/students/?q=zed")
    assert b"Zed Unique" in r.data and b"Pupil 01" not in r.data
    assert b"Zed Unique" in admin.get(f"/students/?belt={belt('Blue Belt')}").data
    assert b"Pupil 05" in admin.get("/students/?q=9000000005").data
    assert b"Page 1 of 2" in admin.get("/students/").data
    assert b"Page 2 of 2" in admin.get("/students/?page=2").data
    first = admin.get("/students/?sort=name&dir=desc").data
    assert first.index(b"Zed Unique") < first.index(b"Pupil 22")
    assert b"No students match" in admin.get("/students/?q=nobody-here").data


def test_wildcards_are_escaped(admin, make_student):
    make_student(full_name="Real Name")
    assert b"Real Name" not in admin.get("/students/?q=%25").data


def test_csv_export_admin_only_and_safe(admin, instructor, make_student):
    make_student(full_name="=HYPERLINK(\"http://x\")")
    r = admin.get("/students/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert b"'=HYPERLINK" in r.data
    assert instructor.get("/students/export.csv").status_code == 403


def test_photo_upload_validation(admin, make_student, belt):
    ok = dict(full_name="Pic", dob="2010-05-12", phone="9876543210", guardian_name="P", guardian_phone="9876500000",
              joining_date="2025-01-10", belt_id=belt("White Belt"))
    r = admin.post("/students/new", data=dict(ok, photo=(io.BytesIO(png_bytes()), "me.png")), content_type="multipart/form-data")
    assert r.status_code == 302
    sid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    p = admin.get(f"/students/{sid}/photo")
    assert p.status_code == 200 and p.mimetype == "image/jpeg"
    bad = admin.post("/students/new", data=dict(ok, full_name="Bad", photo=(io.BytesIO(b"<script>alert(1)</script>"), "x.png")),
                     content_type="multipart/form-data")
    assert bad.status_code == 200 and b"valid image" in bad.data
    assert admin.get("/students/9999/photo").status_code == 404


def test_missing_student_is_404(admin):
    r = admin.get("/students/12345")
    assert r.status_code == 404 and b"Page not found" in r.data
