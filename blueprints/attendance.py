from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth_utils import is_admin, login_required
from db_config import fetch_all, fetch_one, transaction
from helpers import ATTENDANCE_STATUSES, audit, batch_choices, like_pattern, paginate, parse_date, parse_int, today

bp = Blueprint("attendance", __name__, url_prefix="/attendance")
DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _locked_for_instructor(session_date):
    """Older sessions can only be changed by an administrator."""
    from flask import current_app
    cutoff = today() - timedelta(days=current_app.config["ATTENDANCE_EDIT_WINDOW_DAYS"])
    return session_date < cutoff and not is_admin()


def _roster(batch_id, session_date):
    return fetch_all("""
        SELECT s.id, s.student_code, s.full_name, a.status, a.remarks, b.name AS belt_name, b.color_hex
        FROM batch_enrollments e JOIN students s ON s.id = e.student_id
        JOIN belt_ranks b ON b.id = s.current_belt_id
        LEFT JOIN attendance a ON a.student_id = s.id AND a.batch_id = e.batch_id AND a.session_date = %s
        WHERE e.batch_id = %s AND e.enrolled_on <= %s AND (e.ended_on IS NULL OR e.ended_on >= %s)
          AND s.status = 'active'
        ORDER BY lower(s.full_name)""", (session_date, batch_id, session_date, session_date))


@bp.get("/")
@login_required
def register():
    batches = batch_choices()
    batch_id = parse_int(request.args.get("batch")) or (batches[0]["id"] if batches else None)
    session_date = parse_date(request.args.get("date")) or today()
    batch = fetch_one("SELECT * FROM batches WHERE id = %s", (batch_id,)) if batch_id else None
    roster = _roster(batch_id, session_date) if batch else []
    marked = sum(1 for r in roster if r["status"])
    trains_today = batch and DAY_ABBR[session_date.weekday()] in batch["training_days"]
    return render_template("attendance.html", batches=batches, batch=batch, session_date=session_date,
                           roster=roster, marked=marked, is_today=session_date == today(),
                           trains_today=trains_today, locked=_locked_for_instructor(session_date),
                           future=session_date > today(), statuses=ATTENDANCE_STATUSES)


@bp.post("/save")
@login_required
def save():
    batch_id = parse_int(request.form.get("batch_id"))
    session_date = parse_date(request.form.get("date"))
    back = url_for("attendance.register", batch=batch_id, date=request.form.get("date"))
    if not batch_id or not session_date or not fetch_one("SELECT 1 FROM batches WHERE id=%s", (batch_id,)):
        abort(400)
    if session_date > today():
        flash("You can't mark attendance for a future date.", "error")
        return redirect(back)
    if _locked_for_instructor(session_date):
        flash("Attendance older than a week can only be changed by an administrator.", "error")
        return redirect(back)
    roster = {r["id"]: r for r in _roster(batch_id, session_date)}  # never trust ids from the form
    changes = []
    for sid, row in roster.items():
        status = request.form.get(f"status_{sid}")
        if status in ATTENDANCE_STATUSES:
            changes.append((sid, status, row["status"]))
    if not changes:
        flash("Choose a status for at least one student before saving.", "warning")
        return redirect(back)
    saved = edited = 0
    with transaction() as cur:
        for sid, status, old in changes:
            if old == status:
                continue
            cur.execute("""INSERT INTO attendance (student_id, batch_id, session_date, status, marked_by)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (student_id, batch_id, session_date)
                           DO UPDATE SET status = EXCLUDED.status, marked_by = EXCLUDED.marked_by""",
                        (sid, batch_id, session_date, status, g.user["id"]))
            saved += 1
            if old:
                edited += 1
                audit(cur, "attendance_edit", "attendance", sid, batch_id=batch_id,
                      date=session_date, old=old, new=status)
        audit(cur, "attendance_save", "batch", batch_id, date=session_date, saved=saved, edited=edited)
    flash(f"Attendance saved for {saved} student{'s' if saved != 1 else ''}." if saved
          else "Nothing changed – attendance already matched.", "success")
    return redirect(back)


@bp.get("/records")
@login_required
def records():
    where, params = ["TRUE"], []
    d_from = parse_date(request.args.get("from")) or today() - timedelta(days=30)
    d_to = parse_date(request.args.get("to")) or today()
    where += ["a.session_date BETWEEN %s AND %s"]
    params += [d_from, d_to]
    if parse_int(request.args.get("batch")):
        where.append("a.batch_id = %s")
        params.append(parse_int(request.args.get("batch")))
    if request.args.get("status") in ATTENDANCE_STATUSES:
        where.append("a.status = %s")
        params.append(request.args.get("status"))
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(s.full_name ILIKE %s OR s.student_code ILIKE %s)")
        params += [like_pattern(q)] * 2
    sql = f"""SELECT a.id, a.session_date, a.status, a.remarks, s.id AS student_id, s.student_code,
                     s.full_name, bt.name AS batch_name
              FROM attendance a JOIN students s ON s.id = a.student_id JOIN batches bt ON bt.id = a.batch_id
              WHERE {' AND '.join(where)} ORDER BY a.session_date DESC, lower(s.full_name)"""
    page = paginate(sql, params, parse_int(request.args.get("page"), 1))
    return render_template("attendance_records.html", page=page, batches=batch_choices(False),
                           d_from=d_from, d_to=d_to, statuses=ATTENDANCE_STATUSES)
