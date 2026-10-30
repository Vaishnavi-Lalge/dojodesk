import uuid

import psycopg2
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth_utils import admin_required, is_admin, login_required
from db_config import fetch_all, fetch_one, transaction
from helpers import (audit, batch_choices, clean, like_pattern, paginate, parse_date, parse_int,
                     parse_money, today)

bp = Blueprint("fees", __name__, url_prefix="/fees")
METHODS = [("cash", "Cash"), ("upi", "UPI"), ("card", "Card"), ("bank_transfer", "Bank transfer"),
           ("cheque", "Cheque"), ("other", "Other")]
STATUSES = ("pending", "partial", "paid", "overdue")


# ---------- plans (admin) ----------
@bp.route("/plans", methods=["GET", "POST"])
@admin_required
def plans():
    edit_id = parse_int(request.args.get("edit"))
    form = {"period": "monthly"}
    if edit_id:
        form = fetch_one("SELECT * FROM fee_plans WHERE id=%s", (edit_id,)) or abort(404)
    errors = {}
    if request.method == "POST":
        plan_id = parse_int(request.form.get("plan_id"))
        name, amount = clean(request.form.get("name"), 80), parse_money(request.form.get("amount"))
        period = request.form.get("period")
        active = request.form.get("is_active") == "on"
        form = {"id": plan_id, "name": name or "", "amount": request.form.get("amount", ""),
                "period": period, "is_active": active}
        if not name:
            errors["name"] = "Enter a plan name."
        if amount is None or amount <= 0:
            errors["amount"] = "Enter an amount greater than zero, with up to 2 decimals."
        if period not in ("monthly", "term"):
            errors["period"] = "Choose monthly or term."
        if not errors:
            try:
                with transaction() as cur:
                    if plan_id:
                        cur.execute("UPDATE fee_plans SET name=%s, amount=%s, period=%s, is_active=%s WHERE id=%s",
                                    (name, amount, period, active, plan_id))
                    else:
                        cur.execute("INSERT INTO fee_plans (name, amount, period, is_active) VALUES (%s,%s,%s,%s) "
                                    "RETURNING id", (name, amount, period, active))
                        plan_id = cur.fetchone()["id"]
                    audit(cur, "save", "fee_plan", plan_id, name=name, amount=amount)
                flash("Fee plan saved. Existing fee records keep their original amounts.", "success")
                return redirect(url_for("fees.plans"))
            except psycopg2.errors.UniqueViolation:
                errors["name"] = "A plan with this name already exists."
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("fee_plans.html", plans=fetch_all("SELECT * FROM fee_plans ORDER BY is_active DESC, name"),
                           form=form, errors=errors)


# ---------- fee records ----------
@bp.get("/")
@login_required
def index():
    where, params = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(s.full_name ILIKE %s OR s.student_code ILIKE %s)")
        params += [like_pattern(q)] * 2
    if request.args.get("status") in STATUSES:
        where.append("f.status = %s")
        params.append(request.args.get("status"))
    if parse_date(request.args.get("from")):
        where.append("f.due_date >= %s")
        params.append(parse_date(request.args.get("from")))
    if parse_date(request.args.get("to")):
        where.append("f.due_date <= %s")
        params.append(parse_date(request.args.get("to")))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT f.id, f.description, f.due_date, f.amount_due, f.amount_paid, f.balance, f.status,
                     s.id AS student_id, s.full_name, s.student_code
              FROM v_student_fees f JOIN students s ON s.id = f.student_id {clause}
              ORDER BY f.due_date DESC, f.id DESC"""
    page = paginate(sql, params, parse_int(request.args.get("page"), 1))
    totals = fetch_one("""SELECT COALESCE(SUM(balance) FILTER (WHERE status IN ('pending','partial')),0) AS pending,
                                 COALESCE(SUM(balance) FILTER (WHERE status = 'overdue'),0) AS overdue,
                                 (SELECT COALESCE(SUM(amount),0) FROM payments
                                   WHERE date_trunc('month', paid_on) = date_trunc('month', app_today())) AS month_net
                          FROM v_student_fees""")
    return render_template("fees.html", page=page, totals=totals, statuses=STATUSES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    plans_ = fetch_all("SELECT * FROM fee_plans WHERE is_active ORDER BY name")
    form, errors = {"due_date": today().isoformat(), "target": "student"}, {}
    student_id = parse_int(request.args.get("student_id"))
    if student_id:
        form["student_id"] = student_id
    if request.method == "POST":
        f = request.form
        form = dict(f)
        plan = fetch_one("SELECT * FROM fee_plans WHERE id=%s AND is_active", (parse_int(f.get("plan_id")),))
        amount = parse_money(f.get("amount")) if f.get("amount") else (plan["amount"] if plan else None)
        due = parse_date(f.get("due_date"))
        period_label = clean(f.get("period_label"), 40)
        description = clean(f.get("description"), 120) or (plan["name"] if plan else None)
        target = f.get("target")
        if amount is None or amount <= 0:
            errors["amount"] = "Enter an amount greater than zero, or pick a plan that has one."
        if not due:
            errors["due_date"] = "Enter a due date."
        if not period_label:
            errors["period_label"] = "Enter the period, for example 2026-11 or Term 1 2026."
        if not description:
            errors["description"] = "Enter a description or choose a plan."
        if target == "batch":
            ids = [r["student_id"] for r in fetch_all(
                "SELECT e.student_id FROM batch_enrollments e JOIN students s ON s.id=e.student_id "
                "WHERE e.batch_id=%s AND e.ended_on IS NULL AND s.status='active'", (parse_int(f.get("batch_id")),))]
            if not ids:
                errors["batch_id"] = "Choose a batch that has active students."
        else:
            ids = [parse_int(f.get("student_id"))]
            if not ids[0] or not fetch_one("SELECT 1 FROM students WHERE id=%s AND status='active'", (ids[0],)):
                errors["student_id"] = "Choose an active student."
        if not errors:
            created = skipped = 0
            with transaction() as cur:
                for sid in ids:
                    if plan:
                        cur.execute("SELECT 1 FROM student_fees WHERE student_id=%s AND plan_id=%s AND period_label=%s",
                                    (sid, plan["id"], period_label))
                        if cur.fetchone():
                            skipped += 1
                            continue
                    cur.execute("""INSERT INTO student_fees (student_id, plan_id, description, period_label, due_date,
                                   amount_due, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (sid, plan["id"] if plan else None, description, period_label, due, amount, g.user["id"]))
                    audit(cur, "create", "fee", cur.fetchone()["id"], student_id=sid, amount=amount)
                    created += 1
            msg = f"Created {created} fee record{'s' if created != 1 else ''}."
            if skipped:
                msg += f" Skipped {skipped} that already existed for this period."
            flash(msg, "success" if created else "warning")
            return redirect(url_for("fees.index"))
        flash("Fix the highlighted fields and save again.", "error")
    return render_template("fee_new.html", plans=plans_, form=form, errors=errors, batches=batch_choices(),
                           student_opts=[(r["id"], f"{r['full_name']} ({r['student_code']})") for r in fetch_all(
                               "SELECT id, student_code, full_name FROM students "
                               "WHERE status='active' ORDER BY lower(full_name)")])


@bp.get("/<int:fee_id>")
@login_required
def detail(fee_id):
    fee = fetch_one("""SELECT f.*, s.full_name, s.student_code, s.id AS sid FROM v_student_fees f
                       JOIN students s ON s.id = f.student_id WHERE f.id = %s""", (fee_id,)) or abort(404)
    payments = fetch_all("""SELECT p.*, a.full_name AS recorded_by_name,
                                   COALESCE((SELECT SUM(-r.amount) FROM payments r
                                             WHERE r.original_payment_id = p.id), 0) AS refunded
                            FROM payments p LEFT JOIN admins a ON a.id = p.recorded_by
                            WHERE p.fee_id = %s ORDER BY p.created_at, p.id""", (fee_id,))
    return render_template("fee_detail.html", fee=fee, payments=payments, methods=METHODS,
                           idem_key=uuid.uuid4().hex)


@bp.post("/<int:fee_id>/pay")
@login_required
def pay(fee_id):
    amount = parse_money(request.form.get("amount"))
    method = request.form.get("method")
    paid_on = parse_date(request.form.get("paid_on")) or today()
    key = clean(request.form.get("idem_key"), 64)
    back = redirect(url_for("fees.detail", fee_id=fee_id))
    if amount is None or amount <= 0:
        flash("Enter a payment amount greater than zero (up to 2 decimals).", "error")
        return back
    if method not in dict(METHODS):
        flash("Choose how the payment was made.", "error")
        return back
    if paid_on > today():
        flash("The payment date can't be in the future.", "error")
        return back
    try:
        with transaction() as cur:
            cur.execute("""SELECT f.amount_due, COALESCE((SELECT SUM(amount) FROM payments WHERE fee_id = f.id), 0) AS paid
                           FROM student_fees f WHERE f.id = %s FOR UPDATE""", (fee_id,))  # serialises concurrent payments
            row = cur.fetchone() or abort(404)
            balance = row["amount_due"] - row["paid"]
            if amount > balance:
                flash(f"That is more than the outstanding balance of {balance:,.2f}. Enter {balance:,.2f} or less.", "error")
                return back
            cur.execute("""INSERT INTO payments (receipt_no, fee_id, kind, amount, method, paid_on, reference,
                           idempotency_key, recorded_by)
                           VALUES ('RCP-' || to_char(app_today(), 'YYYY') || '-' || lpad(nextval('receipt_seq')::text, 6, '0'),
                                   %s,'payment',%s,%s,%s,%s,%s,%s) RETURNING id, receipt_no""",
                        (fee_id, amount, method, paid_on, clean(request.form.get("reference"), 80), key, g.user["id"]))
            p = cur.fetchone()
            audit(cur, "payment", "fee", fee_id, receipt=p["receipt_no"], amount=amount, method=method)
    except psycopg2.errors.UniqueViolation:
        flash("This payment was already recorded – the form was submitted twice.", "warning")
        return back
    flash(f"Payment recorded. Receipt {p['receipt_no']}.", "success")
    return redirect(url_for("fees.receipt", payment_id=p["id"]))


@bp.post("/<int:fee_id>/refund")
@admin_required
def refund(fee_id):
    payment_id = parse_int(request.form.get("payment_id"))
    amount = parse_money(request.form.get("amount"))
    reason = clean(request.form.get("reason"), 300)
    back = redirect(url_for("fees.detail", fee_id=fee_id))
    if amount is None or amount <= 0 or not reason:
        flash("Enter the amount to refund and the reason for the correction.", "error")
        return back
    with transaction() as cur:
        cur.execute("SELECT * FROM payments WHERE id=%s AND fee_id=%s AND kind='payment' FOR UPDATE",
                    (payment_id, fee_id))
        orig = cur.fetchone() or abort(404)
        cur.execute("SELECT COALESCE(SUM(-amount),0) AS r FROM payments WHERE original_payment_id=%s", (payment_id,))
        available = orig["amount"] - cur.fetchone()["r"]
        if amount > available:
            flash(f"Only {available:,.2f} of this payment can still be refunded.", "error")
            return back
        cur.execute("""INSERT INTO payments (receipt_no, fee_id, kind, amount, method, paid_on, reason,
                       original_payment_id, recorded_by)
                       VALUES ('RFD-' || to_char(app_today(), 'YYYY') || '-' || lpad(nextval('receipt_seq')::text, 6, '0'),
                               %s,'refund',%s,%s,%s,%s,%s,%s) RETURNING receipt_no""",
                    (fee_id, -amount, orig["method"], today(), reason, payment_id, g.user["id"]))
        audit(cur, "refund", "fee", fee_id, receipt=cur.fetchone()["receipt_no"], amount=amount,
              reason=reason, original=orig["receipt_no"])
    flash("Refund recorded. The balance for this fee has been updated.", "success")
    return back


# ---------- payments ----------
@bp.get("/payments")
@login_required
def payments():
    where, params = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(s.full_name ILIKE %s OR s.student_code ILIKE %s OR p.receipt_no ILIKE %s)")
        params += [like_pattern(q)] * 3
    if request.args.get("method") in dict(METHODS):
        where.append("p.method = %s")
        params.append(request.args.get("method"))
    if request.args.get("kind") in ("payment", "refund"):
        where.append("p.kind = %s")
        params.append(request.args.get("kind"))
    if parse_date(request.args.get("from")):
        where.append("p.paid_on >= %s")
        params.append(parse_date(request.args.get("from")))
    if parse_date(request.args.get("to")):
        where.append("p.paid_on <= %s")
        params.append(parse_date(request.args.get("to")))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT p.id, p.receipt_no, p.kind, p.amount, p.method, p.paid_on, p.fee_id, f.description,
                     s.full_name, s.student_code, s.id AS student_id
              FROM payments p JOIN student_fees f ON f.id = p.fee_id JOIN students s ON s.id = f.student_id
              {clause} ORDER BY p.paid_on DESC, p.id DESC"""
    return render_template("payments.html", page=paginate(sql, params, parse_int(request.args.get("page"), 1)),
                           methods=METHODS)


@bp.get("/receipt/<int:payment_id>")
@login_required
def receipt(payment_id):
    p = fetch_one("""SELECT p.*, f.description, f.period_label, f.amount_due, s.full_name, s.student_code,
                            a.full_name AS recorded_by_name,
                            (SELECT balance FROM v_student_fees WHERE id = p.fee_id) AS balance_now
                     FROM payments p JOIN student_fees f ON f.id = p.fee_id JOIN students s ON s.id = f.student_id
                     LEFT JOIN admins a ON a.id = p.recorded_by WHERE p.id = %s""", (payment_id,)) or abort(404)
    return render_template("receipt.html", p=p, methods=dict(METHODS))
