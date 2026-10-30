from datetime import datetime

import psycopg2
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth_utils import is_admin, login_required
from db_config import fetch_all, fetch_one, transaction
from helpers import audit, clean, parse_int, today

bp = Blueprint("batches", __name__, url_prefix="/batches")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------- data access ----------
def enroll_student(cur, student_id, batch_id, override=False):
    """Enrol inside the caller's transaction. Locks the batch row so two people can't both
    take the last seat. Raises ValueError with a user-facing message when not allowed."""
    cur.execute("SELECT id, name, capacity, is_active FROM batches WHERE id = %s FOR UPDATE", (batch_id,))
    batch = cur.fetchone()
    if not batch or not batch["is_active"]:
        raise ValueError("That batch is not available for enrolment.")
    cur.execute("SELECT 1 FROM batch_enrollments WHERE student_id=%s AND batch_id=%s AND ended_on IS NULL",
                (student_id, batch_id))
    if cur.fetchone():
        raise ValueError(f"The student is already in {batch['name']}.")
    cur.execute("SELECT COUNT(*) AS n FROM batch_enrollments WHERE batch_id=%s AND ended_on IS NULL", (batch_id,))
    enrolled = cur.fetchone()["n"]
    if enrolled >= batch["capacity"]:
        if not (override and is_admin()):
            raise ValueError(f"{batch['name']} is full ({enrolled} of {batch['capacity']} places). "
                             "An administrator can override the limit.")
        audit(cur, "capacity_override", "batch", batch_id, student_id=student_id,
              enrolled=enrolled, capacity=batch["capacity"])
    cur.execute("INSERT INTO batch_enrollments (student_id, batch_id, enrolled_on) VALUES (%s,%s,%s)",
                (student_id, batch_id, today()))
    audit(cur, "enrol", "batch", batch_id, student_id=student_id)


def _parse(form):
    errors, d = {}, {}
    d["name"] = clean(form.get("name"), 80)
    d["instructor"] = clean(form.get("instructor"), 80)
    d["training_days"] = [x for x in DAYS if x in form.getlist("training_days")]
    d["capacity"] = parse_int(form.get("capacity"))
    d["is_active"] = form.get("is_active") == "on"
    for key in ("start_time", "end_time"):
        try:
            d[key] = datetime.strptime(form.get(key, ""), "%H:%M").time()
        except ValueError:
            d[key] = None
    if not d["name"]:
        errors["name"] = "Enter a batch name."
    if not d["instructor"]:
        errors["instructor"] = "Enter the instructor's name."
    if not d["training_days"]:
        errors["training_days"] = "Choose at least one training day."
    if not d["start_time"]:
        errors["start_time"] = "Enter a start time."
    if not d["end_time"]:
        errors["end_time"] = "Enter an end time."
    if d["start_time"] and d["end_time"] and d["end_time"] <= d["start_time"]:
        errors["end_time"] = "The end time must be after the start time."
    if not d["capacity"] or not 1 <= d["capacity"] <= 500:
        errors["capacity"] = "Enter a capacity between 1 and 500."
    return d, errors


def _form_values(d):
    return {"name": d.get("name") or "", "instructor": d.get("instructor") or "",
            "training_days": d.get("training_days") or [], "capacity": d.get("capacity") or "",
            "start_time": d["start_time"].strftime("%H:%M") if d.get("start_time") else "",
            "end_time": d["end_time"].strftime("%H:%M") if d.get("end_time") else "",
            "is_active": d.get("is_active", True)}


# ---------- routes ----------
@bp.get("/")
@login_required
def index():
    show = request.args.get("show", "active")
    where = {"active": "WHERE b.is_active", "inactive": "WHERE NOT b.is_active"}.get(show, "")
    rows = fetch_all(f"""
        SELECT b.*, (SELECT COUNT(*) FROM batch_enrollments e WHERE e.batch_id = b.id AND e.ended_on IS NULL) AS enrolled
        FROM batches b {where} ORDER BY b.is_active DESC, b.name""")
    return render_template("batches.html", batches=rows, show=show)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    form, errors = _form_values({"is_active": True}), {}
    if request.method == "POST":
        d, errors = _parse(request.form)
        form = _form_values(d)
        if not errors:
            try:
                with transaction() as cur:
                    cur.execute("""INSERT INTO batches (batch_code, name, instructor, training_days, start_time,
                                   end_time, capacity, is_active)
                                   VALUES ('BATCH-' || lpad(nextval('batch_code_seq')::text, 3, '0'),
                                           %s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (d["name"], d["instructor"], d["training_days"], d["start_time"],
                                 d["end_time"], d["capacity"], d["is_active"]))
                    bid = cur.fetchone()["id"]
                    audit(cur, "create", "batch", bid, name=d["name"])
                flash(f"Batch “{d['name']}” created.", "success")
                return redirect(url_for("batches.detail", batch_id=bid))
            except psycopg2.errors.UniqueViolation:
                errors["name"] = "A batch with this name already exists."
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("batch_form.html", form=form, errors=errors, days=DAYS, batch=None)


@bp.route("/<int:batch_id>/edit", methods=["GET", "POST"])
@login_required
def edit(batch_id):
    batch = fetch_one("SELECT * FROM batches WHERE id = %s", (batch_id,)) or abort(404)
    form, errors = _form_values(batch), {}
    if request.method == "POST":
        d, errors = _parse(request.form)
        form = _form_values(d)
        enrolled = fetch_one("SELECT COUNT(*) AS n FROM batch_enrollments WHERE batch_id=%s AND ended_on IS NULL",
                             (batch_id,))["n"]
        if d["capacity"] and d["capacity"] < enrolled and not errors.get("capacity"):
            errors["capacity"] = f"{enrolled} students are enrolled. Move some students out before lowering the capacity."
        if not errors:
            try:
                with transaction() as cur:
                    cur.execute("""UPDATE batches SET name=%s, instructor=%s, training_days=%s, start_time=%s,
                                   end_time=%s, capacity=%s, is_active=%s WHERE id=%s""",
                                (d["name"], d["instructor"], d["training_days"], d["start_time"],
                                 d["end_time"], d["capacity"], d["is_active"], batch_id))
                    audit(cur, "update", "batch", batch_id, name=d["name"])
                flash("Batch updated.", "success")
                return redirect(url_for("batches.detail", batch_id=batch_id))
            except psycopg2.errors.UniqueViolation:
                errors["name"] = "A batch with this name already exists."
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("batch_form.html", form=form, errors=errors, days=DAYS, batch=batch)


@bp.get("/<int:batch_id>")
@login_required
def detail(batch_id):
    batch = fetch_one("SELECT * FROM batches WHERE id = %s", (batch_id,)) or abort(404)
    roster = fetch_all("""
        SELECT s.id, s.student_code, s.full_name, s.status, e.enrolled_on, b.name AS belt_name, b.color_hex
        FROM batch_enrollments e JOIN students s ON s.id = e.student_id
        JOIN belt_ranks b ON b.id = s.current_belt_id
        WHERE e.batch_id = %s AND e.ended_on IS NULL ORDER BY lower(s.full_name)""", (batch_id,))
    candidates = fetch_all("""
        SELECT s.id, s.student_code, s.full_name FROM students s
        WHERE s.status = 'active' AND NOT EXISTS (SELECT 1 FROM batch_enrollments e
              WHERE e.student_id = s.id AND e.batch_id = %s AND e.ended_on IS NULL)
        ORDER BY lower(s.full_name)""", (batch_id,))
    return render_template("batch_detail.html", batch=batch, roster=roster, candidates=candidates)


@bp.post("/<int:batch_id>/enroll")
@login_required
def enroll(batch_id):
    student_id = parse_int(request.form.get("student_id"))
    student = student_id and fetch_one("SELECT id, status FROM students WHERE id = %s", (student_id,))
    if not student or student["status"] != "active":
        flash("Choose an active student to enrol.", "error")
        return redirect(url_for("batches.detail", batch_id=batch_id))
    try:
        with transaction() as cur:
            enroll_student(cur, student_id, batch_id, override=request.form.get("override") == "on")
        flash("Student enrolled.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("batches.detail", batch_id=batch_id))


@bp.post("/<int:batch_id>/remove/<int:student_id>")
@login_required
def remove(batch_id, student_id):
    with transaction() as cur:
        cur.execute("UPDATE batch_enrollments SET ended_on = %s WHERE batch_id=%s AND student_id=%s AND ended_on IS NULL",
                    (today(), batch_id, student_id))
        if cur.rowcount:
            audit(cur, "unenrol", "batch", batch_id, student_id=student_id)
    flash("Student removed from the batch.", "success")
    return redirect(url_for("batches.detail", batch_id=batch_id))
