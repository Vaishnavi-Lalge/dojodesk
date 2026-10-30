import re
from datetime import timedelta

import psycopg2
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth_utils import admin_required, login_required
from db_config import fetch_all, fetch_one, transaction
from helpers import (ATT_PCT_SQL, audit, belt_choices, clean, like_pattern, paginate, parse_date,
                     parse_int, parse_money, today)

bp = Blueprint("exams", __name__)
RESULTS = ("pending", "passed", "failed")


def eligibility(student_id, target_belt_id):
    """Advisory checklist against the target belt's requirements. It never promotes anyone."""
    st = fetch_one("SELECT joining_date FROM students WHERE id=%s", (student_id,))
    belt = fetch_one("SELECT name, min_months, min_attendance_pct, requirements FROM belt_ranks WHERE id=%s",
                     (target_belt_id,))
    if not st or not belt:
        return None
    last = fetch_one("SELECT MAX(promoted_on) AS d FROM belt_promotions WHERE student_id=%s", (student_id,))["d"]
    since = last or st["joining_date"]
    months = (today() - since).days // 30
    att = fetch_one(f"SELECT {ATT_PCT_SQL} AS pct, COUNT(*) AS n FROM attendance a "
                    "WHERE a.student_id=%s AND a.session_date >= %s", (student_id, today() - timedelta(days=180)))
    pct = att["pct"]
    checks = [
        {"label": f"At least {belt['min_months']} months at the current belt",
         "detail": f"{months} months since {'last promotion' if last else 'joining'}",
         "ok": months >= belt["min_months"]},
        {"label": f"Attendance of {belt['min_attendance_pct']:g}% or more (last 6 months)",
         "detail": f"{pct}% over {att['n']} sessions" if pct is not None else "No attendance recorded yet",
         "ok": pct is not None and pct >= belt["min_attendance_pct"]},
    ]
    return {"belt": belt, "checks": checks, "all_ok": all(c["ok"] for c in checks)}


def _next_belt(current_belt_id):
    return fetch_one("""SELECT id FROM belt_ranks WHERE is_active AND rank_order >
                        (SELECT rank_order FROM belt_ranks WHERE id=%s) ORDER BY rank_order LIMIT 1""",
                     (current_belt_id,))


# ---------- examinations ----------
@bp.get("/exams")
@login_required
def index():
    where, params = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(s.full_name ILIKE %s OR s.student_code ILIKE %s)")
        params += [like_pattern(q)] * 2
    if request.args.get("result") in RESULTS:
        where.append("x.result = %s")
        params.append(request.args.get("result"))
    if parse_date(request.args.get("from")):
        where.append("x.exam_date >= %s")
        params.append(parse_date(request.args.get("from")))
    if parse_date(request.args.get("to")):
        where.append("x.exam_date <= %s")
        params.append(parse_date(request.args.get("to")))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT x.id, x.exam_date, x.result, x.eligible, x.exam_fee, s.id AS student_id, s.full_name,
                     s.student_code, f.name AS from_belt, t.name AS target_belt, t.color_hex,
                     (SELECT p.id FROM belt_promotions p WHERE p.exam_id = x.id) AS promotion_id
              FROM examinations x JOIN students s ON s.id=x.student_id JOIN belt_ranks f ON f.id=x.from_belt_id
              JOIN belt_ranks t ON t.id=x.target_belt_id {clause} ORDER BY x.exam_date DESC, x.id DESC"""
    return render_template("examinations.html", page=paginate(sql, params, parse_int(request.args.get("page"), 1)),
                           results=RESULTS)


@bp.route("/exams/new", methods=["GET", "POST"])
@login_required
def create():
    students = fetch_all("""SELECT s.id, s.student_code, s.full_name, s.current_belt_id, b.name AS belt_name
                            FROM students s JOIN belt_ranks b ON b.id = s.current_belt_id
                            WHERE s.status='active' ORDER BY lower(s.full_name)""")
    form, errors = {"exam_date": today().isoformat(), "exam_fee": "0"}, {}
    src = request.form if request.method == "POST" else request.args
    student_id = parse_int(src.get("student_id"))
    student = next((s for s in students if s["id"] == student_id), None)
    if student:
        form["student_id"] = student["id"]
        nb = _next_belt(student["current_belt_id"])
        form["target_belt_id"] = parse_int(src.get("target_belt_id")) or (nb["id"] if nb else "")
    if request.method == "POST":
        form.update(request.form.to_dict())
        form["eligible"] = request.form.get("eligible") == "on"
        exam_date, fee = parse_date(request.form.get("exam_date")), parse_money(request.form.get("exam_fee") or "0")
        target = fetch_one("SELECT id, name, rank_order FROM belt_ranks WHERE id=%s AND is_active",
                           (parse_int(request.form.get("target_belt_id")),))
        if not student:
            errors["student_id"] = "Choose an active student."
        if not exam_date:
            errors["exam_date"] = "Enter the examination date."
        if fee is None or fee < 0:
            errors["exam_fee"] = "Enter the fee as a number (0 if there is none)."
        if not target:
            errors["target_belt_id"] = "Choose the belt being examined for."
        elif student:
            cur_rank = fetch_one("SELECT rank_order FROM belt_ranks WHERE id=%s", (student["current_belt_id"],))
            if target["rank_order"] <= cur_rank["rank_order"]:
                errors["target_belt_id"] = "The target belt must be higher than the student's current belt."
        if not errors:
            try:
                with transaction() as cur:
                    cur.execute("""INSERT INTO examinations (student_id, exam_date, from_belt_id, target_belt_id,
                                   eligible, eligibility_notes, exam_fee, created_by)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (student["id"], exam_date, student["current_belt_id"], target["id"], form["eligible"],
                                 clean(request.form.get("eligibility_notes"), 500), fee, g.user["id"]))
                    xid = cur.fetchone()["id"]
                    if fee > 0:
                        cur.execute("""INSERT INTO student_fees (student_id, description, period_label, due_date,
                                       amount_due, created_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                                    (student["id"], f"Grading fee – {target['name']}", f"exam-{xid}", exam_date, fee,
                                     g.user["id"]))
                        cur.execute("UPDATE examinations SET fee_id=%s WHERE id=%s", (cur.fetchone()["id"], xid))
                    audit(cur, "create", "exam", xid, student_id=student["id"], target=target["name"])
                flash("Examination scheduled.", "success")
                return redirect(url_for("exams.detail", exam_id=xid))
            except psycopg2.errors.UniqueViolation:
                errors["student_id"] = "This student already has a pending examination. Record its result first."
        flash("Fix the highlighted fields and save again.", "error")
    elig = eligibility(student["id"], form["target_belt_id"]) if student and form.get("target_belt_id") else None
    return render_template("exam_form.html", form=form, errors=errors, students=students,
                           belts=belt_choices(), elig=elig)


@bp.get("/exams/<int:exam_id>")
@login_required
def detail(exam_id):
    x = fetch_one("""SELECT x.*, s.full_name, s.student_code, s.status AS student_status, s.current_belt_id,
                            f.name AS from_belt, t.name AS target_belt, t.color_hex,
                            vf.status AS fee_status, vf.balance AS fee_balance
                     FROM examinations x JOIN students s ON s.id=x.student_id
                     JOIN belt_ranks f ON f.id=x.from_belt_id JOIN belt_ranks t ON t.id=x.target_belt_id
                     LEFT JOIN v_student_fees vf ON vf.id = x.fee_id WHERE x.id=%s""", (exam_id,)) or abort(404)
    promo = fetch_one("""SELECT p.*, a.full_name AS approver FROM belt_promotions p
                         JOIN admins a ON a.id=p.approved_by WHERE p.exam_id=%s""", (exam_id,))
    return render_template("exam_detail.html", x=x, promo=promo, elig=eligibility(x["student_id"], x["target_belt_id"]),
                           today_iso=today().isoformat())


@bp.post("/exams/<int:exam_id>/result")
@login_required
def set_result(exam_id):
    result = request.form.get("result")
    score_raw = (request.form.get("score") or "").strip()
    score = parse_money(score_raw) if score_raw else None
    back = redirect(url_for("exams.detail", exam_id=exam_id))
    if result not in RESULTS or (score_raw and (score is None or not 0 <= score <= 100)):
        flash("Choose a result and enter a score between 0 and 100 (or leave it blank).", "error")
        return back
    try:
        with transaction() as cur:
            cur.execute("SELECT result FROM examinations WHERE id=%s FOR UPDATE", (exam_id,)) 
            row = cur.fetchone() or abort(404)
            cur.execute("SELECT 1 FROM belt_promotions WHERE exam_id=%s", (exam_id,))
            if cur.fetchone():
                flash("A promotion was already approved for this exam, so its result is locked.", "error")
                return back
            cur.execute("UPDATE examinations SET result=%s, score=%s, remarks=%s WHERE id=%s",
                        (result, score, clean(request.form.get("remarks"), 500), exam_id))
            audit(cur, "result", "exam", exam_id, old=row["result"], new=result, score=score)
    except psycopg2.errors.UniqueViolation:
        flash("This student has another pending examination, so this one can't be set back to pending.", "error")
        return back
    flash("Result saved. Passing an exam does not promote the student – approve the promotion below.", "success")
    return back


@bp.post("/exams/<int:exam_id>/promote")
@login_required
def promote(exam_id):
    """Explicit instructor approval – the only code path that changes a student's belt."""
    promoted_on = parse_date(request.form.get("promoted_on")) or today()
    back = redirect(url_for("exams.detail", exam_id=exam_id))
    if promoted_on > today():
        flash("The promotion date can't be in the future.", "error")
        return back
    with transaction() as cur:
        cur.execute("SELECT * FROM examinations WHERE id=%s FOR UPDATE", (exam_id,))
        x = cur.fetchone() or abort(404)
        cur.execute("SELECT current_belt_id, status FROM students WHERE id=%s FOR UPDATE", (x["student_id"],))
        s = cur.fetchone()
        cur.execute("SELECT 1 FROM belt_promotions WHERE exam_id=%s", (exam_id,))
        problem = None
        if cur.fetchone():
            problem = "This promotion has already been approved."
        elif x["result"] != "passed":
            problem = "Only a passed examination can be approved for promotion."
        elif s["status"] != "active":
            problem = "Reactivate the student before promoting them."
        elif s["current_belt_id"] != x["from_belt_id"]:
            problem = "The student's belt has changed since this exam was scheduled. Schedule a new examination."
        elif not x["eligible"] and request.form.get("confirm_ineligible") != "on":
            problem = "This exam was not marked eligible. Tick the confirmation box to approve it anyway."
        if problem:
            flash(problem, "error")
            return back
        cur.execute("""INSERT INTO belt_promotions (student_id, exam_id, from_belt_id, to_belt_id, promoted_on,
                       remarks, approved_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (x["student_id"], exam_id, x["from_belt_id"], x["target_belt_id"], promoted_on,
                     clean(request.form.get("remarks"), 500), g.user["id"]))
        pid = cur.fetchone()["id"]
        cur.execute("UPDATE students SET current_belt_id=%s WHERE id=%s", (x["target_belt_id"], x["student_id"]))
        audit(cur, "approve_promotion", "promotion", pid, exam_id=exam_id, student_id=x["student_id"],
              from_belt=x["from_belt_id"], to_belt=x["target_belt_id"])
    flash("Promotion approved and the student's belt was updated.", "success")
    return redirect(url_for("exams.certificate", promotion_id=pid))


@bp.get("/promotions/<int:promotion_id>/certificate")
@login_required
def certificate(promotion_id):
    p = fetch_one("""SELECT p.*, s.full_name, s.student_code, f.name AS from_belt, t.name AS to_belt,
                            t.color_hex, a.full_name AS approver
                     FROM belt_promotions p JOIN students s ON s.id=p.student_id
                     JOIN belt_ranks f ON f.id=p.from_belt_id JOIN belt_ranks t ON t.id=p.to_belt_id
                     JOIN admins a ON a.id=p.approved_by WHERE p.id=%s""", (promotion_id,)) or abort(404)
    return render_template("certificate.html", p=p)


# ---------- belt system (admin) ----------
@bp.route("/belts", methods=["GET", "POST"])
@admin_required
def belts():
    edit_id = parse_int(request.args.get("edit"))
    form, errors = {"color_hex": "#CBD5E1", "min_months": 0, "min_attendance_pct": 0, "is_active": True}, {}
    if edit_id:
        form = fetch_one("SELECT * FROM belt_ranks WHERE id=%s", (edit_id,)) or abort(404)
    if request.method == "POST":
        f = request.form
        belt_id = parse_int(f.get("belt_id"))
        d = {"name": clean(f.get("name"), 40), "rank_order": parse_int(f.get("rank_order")),
             "color_hex": (f.get("color_hex") or "").strip(), "min_months": parse_int(f.get("min_months")),
             "min_attendance_pct": parse_money(f.get("min_attendance_pct") or "0"),
             "requirements": clean(f.get("requirements"), 2000), "is_active": f.get("is_active") == "on"}
        form = dict(d, id=belt_id)
        if not d["name"]:
            errors["name"] = "Enter the belt name."
        if not d["rank_order"] or d["rank_order"] < 1:
            errors["rank_order"] = "Enter a whole number. Lower numbers are junior ranks."
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", d["color_hex"]):
            errors["color_hex"] = "Choose a colour."
        if d["min_months"] is None or not 0 <= d["min_months"] <= 120:
            errors["min_months"] = "Enter 0 to 120 months."
        if d["min_attendance_pct"] is None or not 0 <= d["min_attendance_pct"] <= 100:
            errors["min_attendance_pct"] = "Enter a percentage from 0 to 100."
        if not errors:
            try:
                with transaction() as cur:
                    vals = (d["name"], d["rank_order"], d["color_hex"], d["requirements"], d["min_months"],
                            d["min_attendance_pct"], d["is_active"])
                    if belt_id:
                        cur.execute("""UPDATE belt_ranks SET name=%s, rank_order=%s, color_hex=%s, requirements=%s,
                                       min_months=%s, min_attendance_pct=%s, is_active=%s WHERE id=%s""", vals + (belt_id,))
                    else:
                        cur.execute("""INSERT INTO belt_ranks (name, rank_order, color_hex, requirements, min_months,
                                       min_attendance_pct, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""", vals)
                        belt_id = cur.fetchone()["id"]
                    audit(cur, "save", "belt", belt_id, name=d["name"], rank_order=d["rank_order"])
                flash("Belt saved.", "success")
                return redirect(url_for("exams.belts"))
            except psycopg2.errors.UniqueViolation as e:
                field = "rank_order" if "rank_order" in (e.diag.constraint_name or "") else "name"
                errors[field] = "Another belt already uses this " + ("order number." if field == "rank_order" else "name.")
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("belts.html", belts=fetch_all(
        "SELECT b.*, (SELECT COUNT(*) FROM students s WHERE s.current_belt_id=b.id AND s.status<>'archived') AS students "
        "FROM belt_ranks b ORDER BY rank_order"), form=form, errors=errors)
