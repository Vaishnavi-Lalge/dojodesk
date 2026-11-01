import time
from collections import defaultdict, deque

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from auth_utils import parent_required, password_problem
from db_config import fetch_all, fetch_one, transaction
from helpers import ATT_PCT_SQL, audit, safe_next
from media_utils import student_photo_files, student_video_files, syllabus_files

bp = Blueprint("parents", __name__, url_prefix="/parent")
_fails = defaultdict(deque)
MAX_FAILS, WINDOW = 5, 15 * 60
_dummy_hash = None


def _throttled(key):
    q = _fails[key]
    while q and time.time() - q[0] > WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILS


@bp.route("/login", methods=["GET", "POST"])
def login():
    global _dummy_hash
    if session.get("parent_uid") and request.method == "GET":
        return redirect(url_for("parents.index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Enter both your username and your password.", "error")
            return render_template("parent_login.html", username=username), 400
        key = (request.remote_addr, username.lower())
        if _throttled(key):
            flash("Too many failed attempts. Wait 15 minutes, then try again.", "error")
            return render_template("parent_login.html", username=username), 429
        user = fetch_one("SELECT id, password_hash, is_active FROM parent_accounts "
                         "WHERE lower(username)=lower(%s)", (username,))
        if _dummy_hash is None:
            _dummy_hash = generate_password_hash("not-a-real-password")
        good = check_password_hash(user["password_hash"] if user else _dummy_hash, password)
        if not (user and user["is_active"] and good):
            _fails[key].append(time.time())
            flash("That username and password don't match. Check both and try again.", "error")
            return render_template("parent_login.html", username=username), 401
        _fails.pop(key, None)
        session.clear()
        session["parent_uid"] = user["id"]
        session.permanent = True
        g.parent_user = {"id": user["id"]}
        with transaction() as cur:
            cur.execute("UPDATE parent_accounts SET last_login_at=now() WHERE id=%s", (user["id"],))
            audit(cur, "login", "parent", user["id"])
        return redirect(safe_next(request.args.get("next")) or url_for("parents.index"))
    return render_template("parent_login.html", username="")


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("parents.login"))


@bp.get("/")
@parent_required
def index():
    students = fetch_all("""SELECT s.id, s.student_code, s.full_name, s.status, b.name AS belt_name,
                                   b.color_hex
                            FROM parent_students ps JOIN students s ON s.id=ps.student_id
                            JOIN belt_ranks b ON b.id=s.current_belt_id
                            WHERE ps.parent_id=%s AND s.status <> 'archived'
                            ORDER BY lower(s.full_name)""", (g.parent_user["id"],))
    for student in students:
        student["attendance"] = fetch_one(
            f"SELECT {ATT_PCT_SQL} AS pct, COUNT(*) AS total FROM attendance a WHERE a.student_id=%s",
            (student["id"],))
        student["history"] = fetch_all("""SELECT a.session_date, a.status, bt.name AS batch_name
                                          FROM attendance a JOIN batches bt ON bt.id=a.batch_id
                                          WHERE a.student_id=%s ORDER BY a.session_date DESC LIMIT 8""",
                                       (student["id"],))
        student["exams"] = fetch_all("""SELECT x.exam_date, x.result, x.score, t.name AS target_belt
                                        FROM examinations x JOIN belt_ranks t ON t.id=x.target_belt_id
                                        WHERE x.student_id=%s ORDER BY x.exam_date DESC LIMIT 8""",
                                     (student["id"],))
        student["promotions"] = fetch_all("""SELECT p.promoted_on, p.remarks, f.name AS from_belt,
                                                    t.name AS to_belt
                                             FROM belt_promotions p
                                             JOIN belt_ranks f ON f.id=p.from_belt_id
                                             JOIN belt_ranks t ON t.id=p.to_belt_id
                                             WHERE p.student_id=%s
                                             ORDER BY p.promoted_on DESC, p.id DESC LIMIT 8""",
                                          (student["id"],))
        student["videos"] = student_video_files(student["student_code"])
        student["photos"] = student_photo_files(student["student_code"])
    return render_template("parent_dashboard.html", students=students)


@bp.get("/password")
@parent_required
def password():
    return render_template("parent_password.html")


@bp.post("/password")
@parent_required
def change_password():
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    row = fetch_one("SELECT password_hash FROM parent_accounts WHERE id=%s", (g.parent_user["id"],))
    problem = password_problem(new)
    if not check_password_hash(row["password_hash"], current):
        flash("Your current password is incorrect.", "error")
    elif new != confirm:
        flash("The new passwords don't match.", "error")
    elif problem:
        flash(problem, "error")
    else:
        with transaction() as cur:
            cur.execute("UPDATE parent_accounts SET password_hash=%s WHERE id=%s",
                        (generate_password_hash(new), g.parent_user["id"]))
            audit(cur, "change_password", "parent", g.parent_user["id"])
        flash("Password changed.", "success")
        return redirect(url_for("parents.index"))
    return render_template("parent_password.html")


@bp.get("/syllabus")
@parent_required
def syllabus():
    return render_template("syllabus.html", files=syllabus_files(), parent_view=True)


@bp.get("/syllabus/<path:filename>")
@parent_required
def syllabus_pdf(filename):
    allowed = {item["filename"] for item in syllabus_files()}
    if filename not in allowed:
        abort(404)
    folder = current_app.config["MEDIA_DIR"] / "syllabus"
    response = send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/students/<int:student_id>/videos/<category>/<path:filename>")
@parent_required
def video(student_id, category, filename):
    student = fetch_one("""SELECT s.student_code FROM students s JOIN parent_students ps ON ps.student_id=s.id
                           WHERE s.id=%s AND ps.parent_id=%s AND s.status <> 'archived'""",
                        (student_id, g.parent_user["id"])) or abort(404)
    folder = current_app.config["MEDIA_DIR"] / "students" / student["student_code"] / category
    allowed = {item["filename"] for item in student_video_files(student["student_code"])
               if item["category"] == category}
    if filename not in allowed:
        abort(404)
    response = send_from_directory(folder, filename, conditional=True, as_attachment=False)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/students/<int:student_id>/photos/<path:filename>")
@parent_required
def photo(student_id, filename):
    student = fetch_one("""SELECT s.student_code FROM students s JOIN parent_students ps ON ps.student_id=s.id
                           WHERE s.id=%s AND ps.parent_id=%s AND s.status <> 'archived'""",
                        (student_id, g.parent_user["id"])) or abort(404)
    if filename not in set(student_photo_files(student["student_code"])):
        abort(404)
    folder = current_app.config["MEDIA_DIR"] / "students" / student["student_code"] / "photos"
    response = send_from_directory(folder, filename, mimetype="image/jpeg", conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response
