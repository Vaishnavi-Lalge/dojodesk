"""Small shared helpers: dates, validation, pagination, CSV, audit trail."""
import csv
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Response, current_app, g, request
from psycopg2.extras import Json

from db_config import fetch_all, fetch_one

PER_PAGE = 20
ATTENDANCE_STATUSES = ("present", "absent", "late", "excused")
# Attendance % = (present + late) / (present + late + absent). Excused sessions are ignored.
# Expects the attendance table to be aliased as "a".
ATT_PCT_SQL = ("ROUND(100.0 * COUNT(*) FILTER (WHERE a.status IN ('present', 'late')) / "
               "NULLIF(COUNT(*) FILTER (WHERE a.status <> 'excused'), 0), 1)")

_PHONE_RE = re.compile(r"^[0-9+()\-\s]{7,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def today():
    return datetime.now(ZoneInfo(current_app.config["TIMEZONE"])).date()


def parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_money(value):
    """Decimal with at most 2 places, or None when invalid."""
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or d != d.quantize(Decimal("0.01")):
        return None
    return d


def clean(value, max_len=500):
    value = (value or "").strip()
    return value[:max_len] or None


def valid_phone(v):
    return bool(v and _PHONE_RE.match(v))


def valid_email(v):
    return bool(v and _EMAIL_RE.match(v) and len(v) <= 254)


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def like_pattern(term):
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def order_by(sort, direction, allowed, default, tiebreak="1"):
    col = allowed.get(sort) or allowed[default]
    d = "DESC" if direction == "desc" else "ASC"
    return f"ORDER BY {col} {d}, {tiebreak}"


def paginate(sql, params, page, per_page=PER_PAGE):
    params = list(params)
    total = fetch_one(f"SELECT COUNT(*) AS n FROM ({sql}) q", params)["n"]
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    items = fetch_all(f"{sql} LIMIT %s OFFSET %s", params + [per_page, (page - 1) * per_page])
    return {"rows": items, "page": page, "pages": pages, "total": total, "per_page": per_page}


def _csv_safe(v):
    if v is None:
        return ""
    s = v.isoformat() if isinstance(v, (date, datetime)) else str(v)
    # neutralise spreadsheet formula injection
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_response(filename, columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow([_csv_safe(r.get(k)) for k, _ in columns])
    resp = Response("\ufeff" + buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def audit(cur, action, entity, entity_id=None, **details):
    """Write an audit row using the caller's cursor so it commits/rolls back with the change."""
    user = g.get("user")
    cur.execute(
        "INSERT INTO audit_logs (admin_id, action, entity, entity_id, details, ip_address) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user["id"] if user else None, action, entity,
         None if entity_id is None else str(entity_id),
         Json(details, dumps=lambda o: json.dumps(o, default=str)), request.remote_addr))


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def belt_choices(active_only=True):
    return fetch_all("SELECT id, name, rank_order, color_hex FROM belt_ranks "
                     + ("WHERE is_active " if active_only else "") + "ORDER BY rank_order")


def batch_choices(active_only=True):
    return fetch_all("SELECT id, batch_code, name FROM batches "
                     + ("WHERE is_active " if active_only else "") + "ORDER BY name")
