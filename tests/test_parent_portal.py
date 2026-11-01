from io import BytesIO

from conftest import PW
from PIL import Image


def _create_parent(admin, student_id, username="parent1", password=PW):
    response = admin.post(f"/students/{student_id}/parents", data={
        "username": username,
        "full_name": "Parent One",
        "password": password,
    })
    assert response.status_code == 302
    return admin.application.test_client()


def test_parent_can_only_view_linked_students_and_their_media(app, admin, make_student, q, tmp_path):
    linked_id = make_student()
    other_id = make_student()
    parent = _create_parent(admin, linked_id)
    assert parent.post("/parent/login", data={"username": "parent1", "password": PW}).status_code == 302

    code = q("SELECT student_code FROM students WHERE id=%s", (linked_id,))[0]["student_code"]
    video_dir = tmp_path / "students" / code / "kata"
    video_dir.mkdir(parents=True)
    (video_dir / "kata.mp4").write_bytes(b"video data")
    photo_dir = tmp_path / "students" / code / "photos"
    photo_dir.mkdir(parents=True)
    (photo_dir / "training.jpg").write_bytes(b"photo data")
    app.config["MEDIA_DIR"] = tmp_path

    response = parent.get("/parent/")
    assert response.status_code == 200
    assert b"Student 1" in response.data
    assert b"Student 2" not in response.data
    assert b"Fees" not in response.data
    assert parent.get(f"/parent/students/{linked_id}/videos/kata/kata.mp4").data == b"video data"
    assert parent.get(f"/parent/students/{linked_id}/photos/training.jpg").data == b"photo data"
    assert parent.get(f"/parent/students/{other_id}/videos/kata/kata.mp4").status_code == 404
    assert parent.get(f"/parent/students/{other_id}/photos/training.jpg").status_code == 404


def test_parent_can_download_syllabus_and_change_password(app, admin, make_student, tmp_path):
    student_id = make_student()
    parent = _create_parent(admin, student_id)
    assert parent.post("/parent/login", data={"username": "parent1", "password": PW}).status_code == 302
    syllabus_dir = tmp_path / "syllabus"
    syllabus_dir.mkdir()
    (syllabus_dir / "White_Belt.pdf").write_bytes(b"%PDF-1.4 syllabus")
    app.config["MEDIA_DIR"] = tmp_path

    page = parent.get("/parent/syllabus")
    assert page.status_code == 200 and b"White Belt" in page.data
    pdf = parent.get("/parent/syllabus/White_Belt.pdf")
    assert pdf.status_code == 200 and pdf.data == b"%PDF-1.4 syllabus"

    response = parent.post("/parent/password", data={
        "current_password": PW,
        "new_password": "NewPassw0rd!",
        "confirm_password": "NewPassw0rd!",
    })
    assert response.status_code == 302
    assert parent.post("/parent/logout").status_code == 302
    assert parent.post("/parent/login", data={
        "username": "parent1", "password": "NewPassw0rd!",
    }).status_code == 302


def test_parent_accounts_are_admin_managed(admin, instructor, make_student):
    student_id = make_student()
    response = instructor.post(f"/students/{student_id}/parents", data={
        "username": "parent1", "full_name": "Parent One", "password": PW,
    })
    assert response.status_code == 403
    response = admin.post(f"/students/{student_id}/parents", data={
        "username": "parent1", "full_name": "Parent One", "password": PW,
    })
    assert response.status_code == 302
    assert admin.get(f"/students/{student_id}").status_code == 200


def test_syllabus_is_public_and_inline(anon):
    response = anon.get("/syllabus/")
    assert response.status_code == 200
    assert b"available to everyone" in response.data
    assert b"syllabus-viewer" in response.data
    pdf = anon.get("/syllabus/Karate_Syllabus.pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF-")
    assert "inline" in pdf.headers.get("Content-Disposition", "")
    assert anon.get("/parent/login").status_code == 200


def test_admin_can_upload_student_media_for_linked_parent(app, admin, instructor, make_student, q, tmp_path):
    student_id = make_student()
    student_code = q("SELECT student_code FROM students WHERE id=%s", (student_id,))[0]["student_code"]
    app.config["MEDIA_DIR"] = tmp_path
    image = BytesIO()
    Image.new("RGB", (20, 20), color="red").save(image, format="PNG")
    image.seek(0)
    video_bytes = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40

    response = admin.post(f"/students/{student_id}/media", data={
        "category": "kata",
        "video": (BytesIO(video_bytes), "kata.mp4"),
        "photos": [(image, "training.png")],
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    photos = list((tmp_path / "students" / student_code / "photos").glob("*.jpg"))
    videos = list((tmp_path / "students" / student_code / "kata").glob("*.mp4"))
    assert len(photos) == 1 and len(videos) == 1

    response = instructor.post(f"/students/{student_id}/media", data={
        "category": "kata", "video": (BytesIO(video_bytes), "instructor.mp4"),
    }, content_type="multipart/form-data")
    assert response.status_code == 403

    parent = _create_parent(admin, student_id)
    assert parent.post("/parent/login", data={"username": "parent1", "password": PW}).status_code == 302
    dashboard = parent.get("/parent/")
    assert dashboard.status_code == 200 and b"Training photos" in dashboard.data
    assert parent.get(f"/parent/students/{student_id}/photos/{photos[0].name}").status_code == 200
    assert parent.get(f"/parent/students/{student_id}/videos/kata/{videos[0].name}").status_code == 200


def test_media_upload_rejects_invalid_video(app, admin, make_student, tmp_path):
    student_id = make_student()
    app.config["MEDIA_DIR"] = tmp_path
    response = admin.post(f"/students/{student_id}/media", data={
        "category": "kata", "video": (BytesIO(b"not a video"), "fake.mp4"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    code = admin.get(f"/students/{student_id}").get_data(as_text=True)
    assert "valid video" in code
    assert not list(tmp_path.rglob("*.mp4"))


def test_private_media_requires_login(anon):
    assert anon.get("/parent/syllabus").status_code == 302
    assert anon.get("/students/syllabus").status_code == 302
