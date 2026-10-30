from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth_utils import is_admin, login_required
from db_config import fetch_all, transaction
from helpers import ATT_PCT_SQL, audit, batch_choices, csv_response, paginate, parse_date, parse_int, today

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _range(args, days):
    d_to = parse_date(args.get("to")) or today()
    d_from = parse_date(args.get("from")) or d_to - timedelta(days=days)
    return d_from, d_to


def _att_cols(first):
    return [first, ("present", "Present", "int"), ("late", "Late", "int"), ("absent", "Absent", "int"),
            ("excused", "Excused", "int"), ("pct", "Attendance %", "pct")]


_ATT_COUNTS = ("COUNT(*) FILTER (WHERE a.status='present') AS present, COUNT(*) FILTER (WHERE a.status='late') AS late, "
               "COUNT(*) FILTER (WHERE a.status='absent') AS absent, COUNT(*) FILTER (WHERE a.status='excused') AS excused, "
               f"{ATT_PCT_SQL} AS pct")


# Each report returns (sql, params, columns). columns: (key, label[, kind]) kind = date|money|pct|int
def r_directory(a):
    where, params = "", []
    if a.get("status") in ("active", "inactive", "archived"):
        where, params = "WHERE s.status = %s", [a.get("status")]
    return (f"""SELECT s.student_code, s.full_name, s.dob, s.phone, s.guardian_name, s.guardian_phone, s.email,
                s.joining_date, b.name AS belt, s.status FROM students s
                JOIN belt_ranks b ON b.id = s.current_belt_id {where} ORDER BY lower(s.full_name)""", params,
            [("student_code", "Student ID"), ("full_name", "Name"), ("dob", "Date of birth", "date"),
             ("phone", "Phone"), ("guardian_name", "Guardian"), ("guardian_phone", "Guardian phone"),
             ("email", "Email"), ("joining_date", "Joined", "date"), ("belt", "Belt"), ("status", "Status")])


def r_status(a):
    return ("SELECT status, COUNT(*) AS students FROM students GROUP BY status ORDER BY status", [],
            [("status", "Status"), ("students", "Students", "int")])


def r_att_summary(a):
    d_from, d_to = _range(a, 29)
    period = a.get("period") if a.get("period") in ("day", "week", "month") else "day"
    params = [period, d_from, d_to]
    extra = ""
    if parse_int(a.get("batch")):
        extra = "AND a.batch_id = %s"
        params.append(parse_int(a.get("batch")))
    return (f"""SELECT date_trunc(%s, a.session_date)::date AS period_start, bt.name AS batch_name, {_ATT_COUNTS}
                FROM attendance a JOIN batches bt ON bt.id = a.batch_id
                WHERE a.session_date BETWEEN %s AND %s {extra}
                GROUP BY 1, bt.name ORDER BY 1 DESC, bt.name""", params,
            [("period_start", "Period starting", "date"), ("batch_name", "Batch")] + _att_cols(None)[1:])


def r_att_students(a):
    d_from, d_to = _range(a, 29)
    params, extra = [d_from, d_to], ""
    if parse_int(a.get("batch")):
        extra = "AND a.batch_id = %s"
        params.append(parse_int(a.get("batch")))
    return (f"""SELECT s.student_code, s.full_name, {_ATT_COUNTS} FROM students s
                JOIN attendance a ON a.student_id = s.id AND a.session_date BETWEEN %s AND %s {extra}
                GROUP BY s.id ORDER BY pct ASC NULLS LAST, lower(s.full_name)""", params,
            [("student_code", "Student ID"), ("full_name", "Name")] + _att_cols(None)[1:])


def r_revenue(a):
    d_from, d_to = _range(a, 365)
    return ("""SELECT to_char(date_trunc('month', paid_on), 'YYYY-MM') AS month,
                      COALESCE(SUM(amount) FILTER (WHERE kind = 'payment'), 0) AS received,
                      COALESCE(-SUM(amount) FILTER (WHERE kind = 'refund'), 0) AS refunded,
                      SUM(amount) AS net, COUNT(*) FILTER (WHERE kind = 'payment') AS payments
               FROM payments WHERE paid_on BETWEEN %s AND %s GROUP BY 1 ORDER BY 1 DESC""", [d_from, d_to],
            [("month", "Month"), ("received", "Received", "money"), ("refunded", "Refunded", "money"),
             ("net", "Net collected", "money"), ("payments", "Payments", "int")])


def r_outstanding(a):
    where, params = "f.balance > 0", []
    if a.get("status") in ("pending", "partial", "overdue"):
        where += " AND f.status = %s"
        params.append(a.get("status"))
    return (f"""SELECT s.student_code, s.full_name, s.phone, f.description, f.due_date, f.amount_due, f.amount_paid,
                       f.balance, f.status FROM v_student_fees f JOIN students s ON s.id = f.student_id
                WHERE {where} ORDER BY f.due_date, lower(s.full_name)""", params,
            [("student_code", "Student ID"), ("full_name", "Name"), ("phone", "Phone"), ("description", "Fee"),
             ("due_date", "Due", "date"), ("amount_due", "Amount due", "money"), ("amount_paid", "Paid", "money"),
             ("balance", "Balance", "money"), ("status", "Status")])


def r_belts(a):
    return ("""SELECT b.name AS belt, COUNT(s.id) AS students FROM belt_ranks b
               LEFT JOIN students s ON s.current_belt_id = b.id AND s.status = 'active'
               WHERE b.is_active OR s.id IS NOT NULL GROUP BY b.id ORDER BY b.rank_order""", [],
            [("belt", "Belt"), ("students", "Active students", "int")])


def r_exams(a):
    d_from, d_to = _range(a, 365)
    params, extra = [d_from, d_to], ""
    if a.get("result") in ("pending", "passed", "failed"):
        extra = "AND x.result = %s"
        params.append(a.get("result"))
    return (f"""SELECT x.exam_date, s.student_code, s.full_name, f.name AS from_belt, t.name AS target_belt,
                       x.result, x.score, x.remarks FROM examinations x JOIN students s ON s.id = x.student_id
                JOIN belt_ranks f ON f.id = x.from_belt_id JOIN belt_ranks t ON t.id = x.target_belt_id
                WHERE x.exam_date BETWEEN %s AND %s {extra} ORDER BY x.exam_date DESC, lower(s.full_name)""", params,
            [("exam_date", "Exam date", "date"), ("student_code", "Student ID"), ("full_name", "Name"),
             ("from_belt", "From"), ("target_belt", "Examined for"), ("result", "Result"), ("score", "Score"),
             ("remarks", "Remarks")])


def r_promotions(a):
    d_from, d_to = _range(a, 365)
    return ("""SELECT p.promoted_on, s.student_code, s.full_name, f.name AS from_belt, t.name AS to_belt,
                      ad.full_name AS approved_by, p.remarks FROM belt_promotions p
               JOIN students s ON s.id = p.student_id JOIN belt_ranks f ON f.id = p.from_belt_id
               JOIN belt_ranks t ON t.id = p.to_belt_id JOIN admins ad ON ad.id = p.approved_by
               WHERE p.promoted_on BETWEEN %s AND %s ORDER BY p.promoted_on DESC, lower(s.full_name)""", [d_from, d_to],
            [("promoted_on", "Promoted on", "date"), ("student_code", "Student ID"), ("full_name", "Name"),
             ("from_belt", "Previous belt"), ("to_belt", "New belt"), ("approved_by", "Approved by"),
             ("remarks", "Remarks")])


def r_batches(a):
    return ("""SELECT b.batch_code, b.name, b.instructor, b.capacity,
                      COUNT(e.id) AS enrolled, b.capacity - COUNT(e.id) AS free_places,
                      CASE WHEN b.is_active THEN 'Active' ELSE 'Inactive' END AS status
               FROM batches b LEFT JOIN batch_enrollments e ON e.batch_id = b.id AND e.ended_on IS NULL
               GROUP BY b.id ORDER BY b.name""", [],
            [("batch_code", "Batch ID"), ("name", "Batch"), ("instructor", "Instructor"),
             ("capacity", "Capacity", "int"), ("enrolled", "Enrolled", "int"),
             ("free_places", "Free places", "int"), ("status", "Status")])


def r_audit(a):
    d_from, d_to = _range(a, 30)
    return ("""SELECT to_char(l.created_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI') AS at,
                      COALESCE(ad.username, '—') AS user_name, l.action, l.entity, l.entity_id, l.details::text AS details
               FROM audit_logs l LEFT JOIN admins ad ON ad.id = l.admin_id
               WHERE l.created_at::date BETWEEN %s AND %s ORDER BY l.created_at DESC, l.id DESC""", [d_from, d_to],
            [("at", "When"), ("user_name", "User"), ("action", "Action"), ("entity", "Record"),
             ("entity_id", "ID"), ("details", "Details")])


# key: (title, description, builder, admin_only, filters)
REPORTS = {
    "directory": ("Student directory", "Every student with contact details.", r_directory, True, {"status"}),
    "status": ("Active and inactive students", "Head-count by status.", r_status, False, set()),
    "attendance": ("Attendance by day, week or month", "Totals per batch for a date range.", r_att_summary, False,
                   {"date", "batch", "period"}),
    "attendance_students": ("Student attendance percentages", "Lowest attendance first.", r_att_students, False,
                            {"date", "batch"}),
    "revenue": ("Monthly revenue", "Payments received, refunds and net collection.", r_revenue, True, {"date"}),
    "outstanding": ("Outstanding fees", "Unpaid and part-paid fees.", r_outstanding, True, {"fee_status"}),
    "belts": ("Belt distribution", "Active students at each belt.", r_belts, False, set()),
    "exams": ("Examination results", "Gradings in a date range.", r_exams, False, {"date", "result"}),
    "promotions": ("Belt promotion history", "Approved promotions with previous and new belt.", r_promotions, False,
                   {"date"}),
    "batches": ("Batch-wise enrolment", "Capacity and enrolment for each batch.", r_batches, False, set()),
    "audit": ("Audit log", "Who changed what, including refunds and overrides.", r_audit, True, {"date"}),
}


@bp.get("/")
@login_required
def index():
    return render_template("reports.html", reports=REPORTS, key=None)


@bp.get("/<key>")
@login_required
def view(key):
    if key not in REPORTS:
        abort(404)
    title, desc, builder, admin_only, filters = REPORTS[key]
    if admin_only and not is_admin():
        abort(403)
    sql, params, columns = builder(request.args)
    columns = [c if len(c) == 3 else (c[0], c[1], "text") for c in columns]
    d_from, d_to = _range(request.args, 29 if key.startswith("attendance") else 365 if key not in ("audit",) else 30)
    if request.args.get("format") == "csv":
        rows = fetch_all(sql, params)
        with transaction() as cur:
            audit(cur, "export_csv", "report", key, rows=len(rows))
        return csv_response(f"{key}-{today().isoformat()}.csv", [(c[0], c[1]) for c in columns], rows)
    page = paginate(sql, params, parse_int(request.args.get("page"), 1), per_page=50)
    return render_template("reports.html", reports=REPORTS, key=key, title=title, desc=desc, columns=columns,
                           page=page, filters=filters, batches=batch_choices(False), d_from=d_from, d_to=d_to)
