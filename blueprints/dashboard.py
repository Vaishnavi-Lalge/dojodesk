from datetime import date, timedelta

from flask import Blueprint, render_template

from auth_utils import login_required
from db_config import fetch_all, fetch_one
from helpers import ATT_PCT_SQL, age_on, today

bp = Blueprint("dashboard", __name__)


def _next_birthday(dob, now):
    for year in (now.year, now.year + 1):
        try:
            d = date(year, dob.month, dob.day)
        except ValueError:  # 29 Feb in a non-leap year
            d = date(year, 3, 1)
        if d >= now:
            return d


@bp.get("/")
@login_required
def index():
    now = today()
    counts = fetch_one("""SELECT COUNT(*) FILTER (WHERE status = 'active') AS active,
                                 COUNT(*) FILTER (WHERE status = 'inactive') AS inactive
                          FROM students""")
    att = fetch_one(f"""SELECT COUNT(*) AS marked, COUNT(*) FILTER (WHERE a.status IN ('present','late')) AS attended,
                               {ATT_PCT_SQL} AS pct FROM attendance a WHERE a.session_date = app_today()""")
    fees = fetch_one("""SELECT
        (SELECT COALESCE(SUM(amount),0) FROM payments
          WHERE date_trunc('month', paid_on) = date_trunc('month', app_today())) AS collected,
        COALESCE(SUM(balance) FILTER (WHERE status IN ('pending','partial')),0) AS pending_amount,
        COUNT(*) FILTER (WHERE status IN ('pending','partial')) AS pending_count,
        COALESCE(SUM(balance) FILTER (WHERE status = 'overdue'),0) AS overdue_amount,
        COUNT(*) FILTER (WHERE status = 'overdue') AS overdue_count FROM v_student_fees""")
    exams = fetch_all("""SELECT x.id, x.exam_date, s.full_name, t.name AS target_belt FROM examinations x
                         JOIN students s ON s.id = x.student_id JOIN belt_ranks t ON t.id = x.target_belt_id
                         WHERE x.result = 'pending' AND x.exam_date >= app_today()
                         ORDER BY x.exam_date, x.id LIMIT 5""")
    recent = fetch_all("""SELECT s.id, s.full_name, s.student_code, s.created_at, b.name AS belt_name
                          FROM students s JOIN belt_ranks b ON b.id = s.current_belt_id
                          WHERE s.status <> 'archived' ORDER BY s.created_at DESC, s.id DESC LIMIT 5""")
    promotions = fetch_all("""SELECT p.id, p.promoted_on, s.id AS student_id, s.full_name, t.name AS to_belt
                              FROM belt_promotions p JOIN students s ON s.id = p.student_id
                              JOIN belt_ranks t ON t.id = p.to_belt_id
                              ORDER BY p.promoted_on DESC, p.id DESC LIMIT 5""")
    birthdays = []
    for s in fetch_all("SELECT id, full_name, dob FROM students WHERE status = 'active'"):
        nb = _next_birthday(s["dob"], now)
        if nb and nb <= now + timedelta(days=30):
            birthdays.append({"id": s["id"], "full_name": s["full_name"], "date": nb,
                              "turning": age_on(s["dob"], nb), "days": (nb - now).days})
    birthdays = sorted(birthdays, key=lambda b: (b["date"], b["full_name"]))[:6]

    charts = {
        "fees": fetch_all("""SELECT to_char(m, 'Mon YY') AS label,
                    COALESCE((SELECT SUM(amount) FROM payments WHERE paid_on >= m::date
                              AND paid_on < (m + interval '1 month')::date), 0) AS value
                    FROM generate_series((date_trunc('month', app_today()) - interval '5 months')::timestamp,
                                         date_trunc('month', app_today())::timestamp, interval '1 month') m
                    ORDER BY m"""),
        "attendance": fetch_all(f"""SELECT to_char(d, 'DD Mon') AS label, t.pct AS value
                    FROM generate_series((app_today() - 29)::timestamp, app_today()::timestamp, interval '1 day') d
                    LEFT JOIN (SELECT a.session_date, {ATT_PCT_SQL} AS pct FROM attendance a
                               WHERE a.session_date >= app_today() - 29 GROUP BY a.session_date) t
                           ON t.session_date = d::date ORDER BY d"""),
        "enrollment": fetch_all("""SELECT to_char(m, 'Mon YY') AS label,
                    (SELECT COUNT(*) FROM students WHERE joining_date < (m + interval '1 month')::date) AS value
                    FROM generate_series((date_trunc('month', app_today()) - interval '11 months')::timestamp,
                                         date_trunc('month', app_today())::timestamp, interval '1 month') m
                    ORDER BY m"""),
        "belts": fetch_all("""SELECT b.name AS label, b.color_hex AS color, COUNT(s.id) AS value
                    FROM belt_ranks b LEFT JOIN students s ON s.current_belt_id = b.id AND s.status = 'active'
                    WHERE b.is_active OR s.id IS NOT NULL GROUP BY b.id ORDER BY b.rank_order"""),
    }
    for rows in charts.values():  # JSON-friendly numbers
        for r in rows:
            r["value"] = None if r["value"] is None else float(r["value"])
    return render_template("dashboard.html", counts=counts, att=att, fees=fees, exams=exams, recent=recent,
                           promotions=promotions, birthdays=birthdays, charts=charts)
