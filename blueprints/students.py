import io
import re
import tempfile
import uuid
from pathlib import Path

import psycopg2
from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request,
                   send_from_directory, url_for)
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from auth_utils import admin_required, is_admin, login_required, password_problem
from blueprints.batches import enroll_student
from db_config import fetch_all, fetch_one, transaction
from helpers import (ATT_PCT_SQL, age_on, audit, batch_choices, belt_choices, clean, csv_response,
                     like_pattern, order_by, paginate, parse_date, parse_int, today, valid_email, valid_phone)
from media_utils import student_photo_files, student_video_files

bp = Blueprint("students", __name__, url_prefix="/students")
SORTS = {"name": "lower(s.full_name)", "code": "s.student_code", "belt": "b.rank_order",
         "joined": "s.joining_date", "status": "s.status"}
STATUSES = ("active", "inactive", "archived")
MAX_PHOTO = 2 * 1024 * 1024
MAX_VIDEO = 100 * 1024 * 1024
VIDEO_SIGNATURES = {".mp4": b"ftyp", ".m4v": b"ftyp", ".webm": b"\x1a\x45\xdf\xa3"}
Image.MAX_IMAGE_PIXELS = 25_000_000  # guards against decompression bombs


# ---------- data access / validation ----------
def _filters(args):
    where, params = [], []
    q = (args.get("q") or "").strip()
    if q:
        where.append("(s.full_name ILIKE %s OR s.student_code ILIKE %s OR s.phone ILIKE %s)")
        params += [like_pattern(q)] * 3
    if parse_int(args.get("belt")):
        where.append("s.current_belt_id = %s")
        params.append(parse_int(args.get("belt")))
    if parse_int(args.get("batch")):
        where.append("EXISTS (SELECT 1 FROM batch_enrollments e WHERE e.student_id = s.id "
                     "AND e.batch_id = %s AND e.ended_on IS NULL)")
        params.append(parse_int(args.get("batch")))
    status = args.get("status", "active")
    if status in STATUSES:
        where.append("s.status = %s")
        params.append(status)
    if parse_date(args.get("joined_from")):
        where.append("s.joining_date >= %s")
        params.append(parse_date(args.get("joined_from")))
    if parse_date(args.get("joined_to")):
        where.append("s.joining_date <= %s")
        params.append(parse_date(args.get("joined_to")))
    return ("WHERE " + " AND ".join(where)) if where else "", params


LIST_SQL = """
    SELECT s.id, s.student_code, s.full_name, s.phone, s.status, s.joining_date, s.dob,
           (s.photo IS NOT NULL) AS has_photo, s.guardian_name, s.guardian_phone, s.email,
           b.name AS belt_name, b.color_hex,
           (SELECT string_agg(bt.name, ', ' ORDER BY bt.name) FROM batch_enrollments e
              JOIN batches bt ON bt.id = e.batch_id WHERE e.student_id = s.id AND e.ended_on IS NULL) AS batches
    FROM students s JOIN belt_ranks b ON b.id = s.current_belt_id {where} {order}"""


def process_photo(file_storage):
    """Validate an upload with Pillow and re-encode as a small JPEG (strips metadata)."""
    data = file_storage.read(MAX_PHOTO + 1)
    if len(data) > MAX_PHOTO:
        raise ValueError("The photo is larger than 2 MB.")
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        img = Image.open(io.BytesIO(data))
        if img.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("Use a JPG, PNG or WebP photo.")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError):
        raise ValueError("That file isn't a valid image. Use a JPG, PNG or WebP photo.")
    img.thumbnail((400, 400))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def _parse(form, files, creating):
    d, errors = {}, {}
    d["full_name"] = clean(form.get("full_name"), 120)
    d["dob"] = parse_date(form.get("dob"))
    d["gender"] = form.get("gender") if form.get("gender") in ("male", "female", "other") else None
    d["phone"] = clean(form.get("phone"), 20)
    d["guardian_name"] = clean(form.get("guardian_name"), 120)
    d["guardian_phone"] = clean(form.get("guardian_phone"), 20)
    d["email"] = clean(form.get("email"), 254)
    d["address"] = clean(form.get("address"), 500)
    d["joining_date"] = parse_date(form.get("joining_date"))
    now = today()
    if not d["full_name"]:
        errors["full_name"] = "Enter the student's full name."
    if not d["dob"]:
        errors["dob"] = "Enter a valid date of birth."
    elif d["dob"] > now or age_on(d["dob"], now) > 100:
        errors["dob"] = "Check the date of birth."
    if not d["phone"]:
        errors["phone"] = "Enter a phone number."
    elif not valid_phone(d["phone"]):
        errors["phone"] = "Use 7–20 digits; spaces, +, - and brackets are fine."
    if d["guardian_phone"] and not valid_phone(d["guardian_phone"]):
        errors["guardian_phone"] = "Use 7–20 digits; spaces, +, - and brackets are fine."
    if d["dob"] and not errors.get("dob") and age_on(d["dob"], now) < 18:
        if not d["guardian_name"]:
            errors["guardian_name"] = "A parent or guardian name is required for students under 18."
        if not d["guardian_phone"]:
            errors["guardian_phone"] = "A parent or guardian phone is required for students under 18."
    if d["email"] and not valid_email(d["email"]):
        errors["email"] = "Enter a valid email address, like name@example.com."
    if not d["joining_date"]:
        errors["joining_date"] = "Enter the joining date."
    elif d["dob"] and d["joining_date"] < d["dob"]:
        errors["joining_date"] = "The joining date can't be before the date of birth."
    d["belt_id"] = parse_int(form.get("belt_id"))
    d["batch_id"] = parse_int(form.get("batch_id"))
    d["override"] = form.get("override") == "on"
    if creating and not fetch_one("SELECT 1 FROM belt_ranks WHERE id=%s AND is_active", (d["belt_id"],)):
        errors["belt_id"] = "Choose a starting belt."
    d["photo"] = None
    upload = files.get("photo")
    if upload and upload.filename:
        try:
            d["photo"] = process_photo(upload)
        except ValueError as e:
            errors["photo"] = str(e)
    d["remove_photo"] = form.get("remove_photo") == "on"
    return d, errors


def _duplicate(d, exclude_id=None):
    if not (d["full_name"] and d["dob"]):
        return None
    return fetch_one("SELECT student_code FROM students WHERE lower(full_name) = lower(%s) AND dob = %s "
                     "AND id <> %s", (d["full_name"], d["dob"], exclude_id or 0))


def _form_values(d):
    out = {k: ("" if v is None else v) for k, v in d.items() if k not in ("photo",)}
    for k in ("dob", "joining_date"):
        out[k] = d[k].isoformat() if d.get(k) else ""
    return out


# ---------- routes ----------
@bp.get("/")
@login_required
def index():
    where, params = _filters(request.args)
    order = order_by(request.args.get("sort"), request.args.get("dir"), SORTS, "name", "s.id")
    page = paginate(LIST_SQL.format(where=where, order=order), params, parse_int(request.args.get("page"), 1))
    return render_template("students.html", page=page, belts=belt_choices(False), batches=batch_choices(False))


@bp.get("/export.csv")
@admin_required
def export():
    where, params = _filters(request.args)
    rows = fetch_all(LIST_SQL.format(where=where, order="ORDER BY lower(s.full_name)"), params)
    with transaction() as cur:
        audit(cur, "export_csv", "student", None, rows=len(rows))
    cols = [("student_code", "Student ID"), ("full_name", "Name"), ("dob", "Date of birth"),
            ("phone", "Phone"), ("guardian_name", "Guardian"), ("guardian_phone", "Guardian phone"),
            ("email", "Email"), ("joining_date", "Joined"), ("belt_name", "Belt"),
            ("batches", "Batches"), ("status", "Status")]
    return csv_response("students.csv", cols, rows)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    belts = belt_choices()
    form = {"joining_date": today().isoformat(), "belt_id": belts[0]["id"] if belts else ""}
    errors = {}
    if request.method == "POST":
        d, errors = _parse(request.form, request.files, creating=True)
        form = _form_values(d)
        dup = _duplicate(d)
        if dup and not errors:
            errors["full_name"] = f"A student with this name and date of birth already exists ({dup['student_code']})."
        if not errors:
            try:
                with transaction() as cur:
                    cur.execute("SELECT 'DD-' || to_char(app_today(), 'YYYY') || '-' || "
                                "lpad(nextval('student_code_seq')::text, 4, '0') AS code")
                    code = cur.fetchone()["code"]
                    cur.execute("""INSERT INTO students (student_code, full_name, photo, photo_mime, dob, gender, phone,
                                   guardian_name, guardian_phone, email, address, joining_date, current_belt_id)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (code, d["full_name"], psycopg2.Binary(d["photo"]) if d["photo"] else None,
                                 "image/jpeg" if d["photo"] else None, d["dob"], d["gender"], d["phone"],
                                 d["guardian_name"], d["guardian_phone"], d["email"], d["address"],
                                 d["joining_date"], d["belt_id"]))
                    sid = cur.fetchone()["id"]
                    if d["batch_id"]:
                        enroll_student(cur, sid, d["batch_id"], d["override"])
                    audit(cur, "create", "student", sid, student_code=code)
                flash(f"{d['full_name']} was added with ID {code}.", "success")
                return redirect(url_for("students.detail", student_id=sid))
            except ValueError as e:  # batch full etc. – whole save rolled back
                errors["batch_id"] = str(e)
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("student_form.html", form=form, errors=errors, belts=belts,
                           batches=batch_choices(), student=None)


@bp.route("/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
def edit(student_id):
    student = fetch_one("SELECT id, student_code, full_name, dob, gender, phone, guardian_name, guardian_phone, "
                        "email, address, joining_date, current_belt_id, status, (photo IS NOT NULL) AS has_photo "
                        "FROM students WHERE id = %s", (student_id,)) or abort(404)
    form, errors = _form_values(student), {}
    if request.method == "POST":
        d, errors = _parse(request.form, request.files, creating=False)
        form = _form_values(d)
        dup = _duplicate(d, student_id)
        if dup and not errors:
            errors["full_name"] = f"Another student with this name and date of birth exists ({dup['student_code']})."
        if not errors:
            with transaction() as cur:
                cur.execute("""UPDATE students SET full_name=%s, dob=%s, gender=%s, phone=%s, guardian_name=%s,
                               guardian_phone=%s, email=%s, address=%s, joining_date=%s WHERE id=%s""",
                            (d["full_name"], d["dob"], d["gender"], d["phone"], d["guardian_name"],
                             d["guardian_phone"], d["email"], d["address"], d["joining_date"], student_id))
                if d["photo"]:
                    cur.execute("UPDATE students SET photo=%s, photo_mime='image/jpeg' WHERE id=%s",
                                (psycopg2.Binary(d["photo"]), student_id))
                elif d["remove_photo"]:
                    cur.execute("UPDATE students SET photo=NULL, photo_mime=NULL WHERE id=%s", (student_id,))
                audit(cur, "update", "student", student_id)
            flash("Student details saved.", "success")
            return redirect(url_for("students.detail", student_id=student_id))
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("student_form.html", form=form, errors=errors, belts=belt_choices(False),
                           batches=[], student=student)


@bp.get("/<int:student_id>")
@login_required
def detail(student_id):
    s = fetch_one("""SELECT s.id, s.student_code, s.full_name, s.dob, s.gender, s.phone, s.guardian_name,
                     s.guardian_phone, s.email, s.address, s.joining_date, s.status, s.created_at,
                     (s.photo IS NOT NULL) AS has_photo, b.name AS belt_name, b.color_hex, b.id AS belt_id
                     FROM students s JOIN belt_ranks b ON b.id = s.current_belt_id WHERE s.id = %s""",
                  (student_id,)) or abort(404)
    batches = fetch_all("""SELECT bt.id, bt.name, bt.batch_code, e.enrolled_on FROM batch_enrollments e
                           JOIN batches bt ON bt.id = e.batch_id WHERE e.student_id=%s AND e.ended_on IS NULL
                           ORDER BY bt.name""", (student_id,))
    att = fetch_one(f"""SELECT COUNT(*) FILTER (WHERE a.status='present') AS present,
                        COUNT(*) FILTER (WHERE a.status='late') AS late,
                        COUNT(*) FILTER (WHERE a.status='absent') AS absent,
                        COUNT(*) FILTER (WHERE a.status='excused') AS excused,
                        {ATT_PCT_SQL} AS pct FROM attendance a WHERE a.student_id = %s""", (student_id,))
    history = fetch_all("""SELECT a.session_date, a.status, bt.name AS batch_name FROM attendance a
                           JOIN batches bt ON bt.id = a.batch_id WHERE a.student_id = %s
                           ORDER BY a.session_date DESC LIMIT 15""", (student_id,))
    fees = fetch_all("SELECT id, description, due_date, amount_due, amount_paid, balance, status "
                     "FROM v_student_fees WHERE student_id = %s ORDER BY due_date DESC LIMIT 10", (student_id,))
    outstanding = fetch_one("SELECT COALESCE(SUM(balance),0) AS total FROM v_student_fees "
                            "WHERE student_id=%s AND balance > 0", (student_id,))["total"]
    promotions = fetch_all("""SELECT p.id, p.promoted_on, p.remarks, f.name AS from_belt, t.name AS to_belt,
                              t.color_hex FROM belt_promotions p JOIN belt_ranks f ON f.id=p.from_belt_id
                              JOIN belt_ranks t ON t.id=p.to_belt_id WHERE p.student_id=%s
                              ORDER BY p.promoted_on DESC, p.id DESC""", (student_id,))
    exams = fetch_all("""SELECT x.id, x.exam_date, x.result, t.name AS target_belt FROM examinations x
                         JOIN belt_ranks t ON t.id = x.target_belt_id WHERE x.student_id=%s
                         ORDER BY x.exam_date DESC LIMIT 10""", (student_id,))
    parent_accounts = fetch_all("""SELECT p.id, p.username, p.full_name, p.is_active
                                   FROM parent_students ps JOIN parent_accounts p ON p.id=ps.parent_id
                                   WHERE ps.student_id=%s ORDER BY lower(p.full_name)""", (student_id,))
    videos = student_video_files(s["student_code"])
    photos = student_photo_files(s["student_code"])
    return render_template("student_detail.html", s=s, batches=batches, att=att, history=history, fees=fees,
                           outstanding=outstanding, promotions=promotions, exams=exams,
                           is_minor=age_on(s["dob"], today()) < 18,
                           parent_accounts=parent_accounts, videos=videos, photos=photos)


def _media_filename(original, extension):
    stem = Path(secure_filename(original)).stem[:80] or "upload"
    return f"{uuid.uuid4().hex}_{stem}{extension}"


def _store_video(upload, folder):
    original = secure_filename(upload.filename or "")
    extension = Path(original).suffix.lower()
    if not original or extension not in VIDEO_SIGNATURES:
        raise ValueError("Choose an MP4, M4V, or WebM video.")
    header = upload.stream.read(32)
    if extension == ".webm":
        valid_signature = header.startswith(VIDEO_SIGNATURES[extension])
    else:
        valid_signature = header[4:8] == VIDEO_SIGNATURES[extension]
    if not valid_signature:
        raise ValueError("That file does not appear to be a valid video of the selected type.")

    folder.mkdir(parents=True, exist_ok=True)
    filename = _media_filename(original, extension)
    target = folder / filename
    temporary_path = None
    size = len(header)
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".upload-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(header)
            while chunk := upload.stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO:
                    raise ValueError("Videos must be 100 MB or smaller.")
                temporary.write(chunk)
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return filename


@bp.post("/<int:student_id>/media")
@admin_required
def upload_media(student_id):
    student = fetch_one("SELECT student_code, full_name FROM students WHERE id=%s", (student_id,)) or abort(404)
    video = request.files.get("video")
    category = request.form.get("category")
    photos = [item for item in request.files.getlist("photos") if item.filename]
    has_video = bool(video and video.filename)
    if not has_video and not photos:
        flash("Choose at least one photo or video to upload.", "error")
        return redirect(url_for("students.detail", student_id=student_id))
    if len(photos) > 10:
        flash("Upload no more than 10 photos at a time.", "error")
        return redirect(url_for("students.detail", student_id=student_id))
    if has_video and category not in ("kata", "kumite"):
        flash("Choose whether the video is kata or kumite.", "error")
        return redirect(url_for("students.detail", student_id=student_id))

    prepared_photos = []
    try:
        for upload in photos:
            prepared_photos.append((upload, process_photo(upload)))
        if has_video:
            folder = current_app.config["MEDIA_DIR"] / "students" / student["student_code"] / category
            _store_video(video, folder)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("students.detail", student_id=student_id))

    photo_folder = current_app.config["MEDIA_DIR"] / "students" / student["student_code"] / "photos"
    photo_names = []
    for upload, data in prepared_photos:
        filename = _media_filename(upload.filename, ".jpg")
        photo_folder.mkdir(parents=True, exist_ok=True)
        (photo_folder / filename).write_bytes(data)
        photo_names.append(filename)

    with transaction() as cur:
        audit(cur, "upload_student_media", "student", student_id,
              photos=len(photo_names), video_category=category if has_video else None,
              video_uploaded=has_video)
    flash(f"Uploaded {len(photo_names)} photo(s)"
          + (f" and one {category} video." if has_video else "."), "success")
    return redirect(url_for("students.detail", student_id=student_id))


@bp.post("/<int:student_id>/parents")
@admin_required
def create_parent_account(student_id):
    student = fetch_one("SELECT id, full_name FROM students WHERE id=%s AND status <> 'archived'",
                        (student_id,)) or abort(404)
    username = clean(request.form.get("username"), 80)
    full_name = clean(request.form.get("full_name"), 120)
    password = request.form.get("password") or ""
    if not re.fullmatch(r"[A-Za-z0-9._@+-]{2,80}", username):
        flash("Use 2–80 letters, numbers, dots, underscores, @, + or - for the username.", "error")
    elif not full_name:
        flash("Enter the parent's full name.", "error")
    elif fetch_one("SELECT 1 FROM parent_accounts WHERE lower(username)=lower(%s)", (username,)):
        flash("That parent username already exists. Use the existing-account link form.", "error")
    elif problem := password_problem(password):
        flash(problem, "error")
    else:
        try:
            with transaction() as cur:
                cur.execute("""INSERT INTO parent_accounts (username, password_hash, full_name)
                               VALUES (%s,%s,%s) RETURNING id""",
                            (username, generate_password_hash(password), full_name))
                parent_id = cur.fetchone()["id"]
                cur.execute("INSERT INTO parent_students (parent_id, student_id) VALUES (%s,%s)",
                            (parent_id, student_id))
                audit(cur, "create", "parent_account", parent_id, student_id=student_id)
            flash(f"Parent account created for {student['full_name']}.", "success")
            return redirect(url_for("students.detail", student_id=student_id))
        except psycopg2.errors.UniqueViolation:
            flash("That parent username already exists. Use the existing-account link form.", "error")
    return redirect(url_for("students.detail", student_id=student_id))


@bp.post("/<int:student_id>/parents/link")
@admin_required
def link_parent_account(student_id):
    if not fetch_one("SELECT id FROM students WHERE id=%s AND status <> 'archived'",
                     (student_id,)):
        abort(404)
    username = clean(request.form.get("username"), 80)
    parent = fetch_one("SELECT id, full_name FROM parent_accounts WHERE lower(username)=lower(%s) AND is_active",
                       (username,))
    if not parent:
        flash("No active parent account has that username.", "error")
    else:
        with transaction() as cur:
            cur.execute("INSERT INTO parent_students (parent_id, student_id) VALUES (%s,%s) "
                        "ON CONFLICT DO NOTHING", (parent["id"], student_id))
            linked = cur.rowcount > 0
            if linked:
                audit(cur, "link_parent", "student", student_id, parent_id=parent["id"])
        flash("Parent account linked." if linked else "That parent is already linked to this student.",
              "success" if linked else "info")
    return redirect(url_for("students.detail", student_id=student_id))


@bp.post("/<int:student_id>/parents/<int:parent_id>/password")
@admin_required
def reset_parent_password(student_id, parent_id):
    linked = fetch_one("""SELECT p.full_name FROM parent_accounts p
                          JOIN parent_students ps ON ps.parent_id=p.id
                          WHERE p.id=%s AND ps.student_id=%s""", (parent_id, student_id))
    if not linked:
        abort(404)
    password = request.form.get("password") or ""
    problem = password_problem(password)
    if problem:
        flash(problem, "error")
    else:
        with transaction() as cur:
            cur.execute("UPDATE parent_accounts SET password_hash=%s WHERE id=%s",
                        (generate_password_hash(password), parent_id))
            audit(cur, "reset_parent_password", "parent_account", parent_id, student_id=student_id)
        flash(f"Password reset for {linked['full_name']}.", "success")
    return redirect(url_for("students.detail", student_id=student_id))


@bp.post("/<int:student_id>/status")
@login_required
def set_status(student_id):
    status = request.form.get("status")
    if status not in ("active", "inactive", "archived"):
        abort(400)
    if status == "archived" and not is_admin():
        abort(403)
    s = fetch_one("SELECT full_name, status FROM students WHERE id=%s", (student_id,)) or abort(404)
    with transaction() as cur:
        cur.execute("UPDATE students SET status=%s WHERE id=%s", (status, student_id))
        if status != "active":  # free up their batch places
            cur.execute("UPDATE batch_enrollments SET ended_on=%s WHERE student_id=%s AND ended_on IS NULL",
                        (today(), student_id))
        audit(cur, "status_change", "student", student_id, old=s["status"], new=status)
    flash({"active": f"{s['full_name']} is active again. Re-enrol them in a batch to take attendance.",
           "inactive": f"{s['full_name']} was deactivated and removed from their batches.",
           "archived": f"{s['full_name']} was archived."}[status], "success")
    return redirect(url_for("students.detail", student_id=student_id))


@bp.get("/<int:student_id>/photo")
@login_required
def photo(student_id):
    row = fetch_one("SELECT photo, photo_mime FROM students WHERE id=%s", (student_id,))
    if not row or not row["photo"]:
        abort(404)
    resp = Response(bytes(row["photo"]), mimetype=row["photo_mime"] or "image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@bp.get("/<int:student_id>/videos/<category>/<path:filename>")
@login_required
def video(student_id, category, filename):
    student = fetch_one("SELECT student_code FROM students WHERE id=%s", (student_id,)) or abort(404)
    folder = current_app.config["MEDIA_DIR"] / "students" / student["student_code"] / category
    allowed = {item["filename"] for item in student_video_files(student["student_code"])
               if item["category"] == category}
    if filename not in allowed:
        abort(404)
    response = send_from_directory(folder, filename, conditional=True, as_attachment=False)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/<int:student_id>/photos/<path:filename>")
@login_required
def media_photo(student_id, filename):
    student = fetch_one("SELECT student_code FROM students WHERE id=%s", (student_id,)) or abort(404)
    allowed = set(student_photo_files(student["student_code"]))
    if filename not in allowed:
        abort(404)
    folder = current_app.config["MEDIA_DIR"] / "students" / student["student_code"] / "photos"
    response = send_from_directory(folder, filename, mimetype="image/jpeg", conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/syllabus")
@login_required
def syllabus():
    from media_utils import syllabus_files
    return render_template("syllabus.html", files=syllabus_files(), parent_view=False)


@bp.get("/syllabus/<path:filename>")
@login_required
def syllabus_pdf(filename):
    from media_utils import syllabus_files
    from pathlib import Path

    allowed = {item["filename"] for item in syllabus_files()}
    if filename not in allowed:
        abort(404)
    folder = Path(current_app.config["MEDIA_DIR"]) / "syllabus"
    response = send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response
