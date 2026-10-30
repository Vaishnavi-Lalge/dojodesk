from datetime import timedelta
from decimal import Decimal

import pytest


@pytest.fixture()
def fee(admin, make_student, today):
    sid = make_student()
    r = admin.post("/fees/new", data=dict(target="student", student_id=sid, description="Monthly fee", period_label="2026-11",
                                          amount="1000.00", due_date=(today + timedelta(days=5)).isoformat()))
    assert r.status_code == 302
    return sid


def fee_row(q):
    return q("SELECT * FROM v_student_fees ORDER BY id")[0]


def test_new_fee_is_pending(fee, q):
    f = fee_row(q)
    assert f["status"] == "pending" and f["balance"] == Decimal("1000.00")


def test_partial_then_full_payment(admin, fee, q):
    r = admin.post("/fees/1/pay", data={"amount": "400", "method": "cash", "idem_key": "k1"})
    assert r.status_code == 302 and "/fees/receipt/" in r.headers["Location"]
    f = fee_row(q)
    assert f["status"] == "partial" and f["balance"] == Decimal("600.00") and f["amount_paid"] == Decimal("400.00")
    admin.post("/fees/1/pay", data={"amount": "600", "method": "upi", "idem_key": "k2"})
    f = fee_row(q)
    assert f["status"] == "paid" and f["balance"] == 0


def test_cannot_overpay_or_pay_nonsense(admin, fee, q):
    for amount in ("1000.01", "0", "-5", "abc", "10.555"):
        admin.post("/fees/1/pay", data={"amount": amount, "method": "cash", "idem_key": f"x{amount}"})
    admin.post("/fees/1/pay", data={"amount": "10", "method": "bitcoin", "idem_key": "m"})
    assert q("SELECT count(*) AS n FROM payments")[0]["n"] == 0
    assert fee_row(q)["status"] == "pending"


def test_double_submit_records_one_payment(admin, fee, q):
    for _ in range(3):
        admin.post("/fees/1/pay", data={"amount": "100", "method": "cash", "idem_key": "same-key"})
    assert q("SELECT count(*) AS n FROM payments")[0]["n"] == 1


def test_receipts_are_unique_and_printable(admin, fee, q):
    admin.post("/fees/1/pay", data={"amount": "100", "method": "cash", "idem_key": "a"})
    admin.post("/fees/1/pay", data={"amount": "100", "method": "cash", "idem_key": "b"})
    nos = [r["receipt_no"] for r in q("SELECT receipt_no FROM payments ORDER BY id")]
    assert len(set(nos)) == 2 and nos[0].startswith("RCP-")
    page = admin.get("/fees/receipt/1")
    assert page.status_code == 200 and nos[0].encode() in page.data and b"data-print" in page.data


def test_overdue_status(admin, make_student, today, q):
    sid = make_student()
    admin.post("/fees/new", data=dict(target="student", student_id=sid, description="Late", period_label="old",
                                      amount="500", due_date=(today - timedelta(days=3)).isoformat()))
    assert fee_row(q)["status"] == "overdue"
    assert b"Late" in admin.get("/fees/?status=overdue").data
    assert b"Late" not in admin.get("/fees/?status=paid").data


def test_payments_are_append_only(admin, fee, q):
    import psycopg2
    admin.post("/fees/1/pay", data={"amount": "100", "method": "cash", "idem_key": "z"})
    for sql in ("UPDATE payments SET amount = 1", "DELETE FROM payments"):
        with pytest.raises(psycopg2.Error):
            q(sql)


def test_refund_admin_only_with_audit_trail(admin, instructor, fee, q):
    admin.post("/fees/1/pay", data={"amount": "1000", "method": "cash", "idem_key": "p"})
    assert instructor.post("/fees/1/refund", data={"payment_id": 1, "amount": "100", "reason": "x"}).status_code == 403
    assert admin.post("/fees/1/refund", data={"payment_id": 1, "amount": "100", "reason": ""}, follow_redirects=True).status_code == 200
    assert q("SELECT count(*) AS n FROM payments WHERE kind='refund'")[0]["n"] == 0
    admin.post("/fees/1/refund", data={"payment_id": 1, "amount": "300", "reason": "Entered twice"})
    f = fee_row(q)
    assert f["amount_paid"] == Decimal("700.00") and f["status"] == "pending" or f["status"] == "partial"
    admin.post("/fees/1/refund", data={"payment_id": 1, "amount": "800", "reason": "too much"})
    assert q("SELECT count(*) AS n FROM payments WHERE kind='refund'")[0]["n"] == 1
    log = q("SELECT details FROM audit_logs WHERE action='refund'")
    assert log and log[0]["details"]["reason"] == "Entered twice"
    assert q("SELECT receipt_no FROM payments WHERE kind='refund'")[0]["receipt_no"].startswith("RFD-")


def test_plan_fills_amount_and_blocks_duplicate_period(admin, make_student, today, q):
    admin.post("/fees/plans", data={"name": "Monthly tuition", "amount": "1200", "period": "monthly", "is_active": "on"})
    pid = q("SELECT id FROM fee_plans")[0]["id"]
    sid = make_student()
    data = dict(target="student", student_id=sid, plan_id=pid, period_label="2026-11", amount="", description="", due_date=today.isoformat())
    admin.post("/fees/new", data=data)
    r = admin.post("/fees/new", data=data, follow_redirects=True)
    assert b"Skipped 1" in r.data or b"already existed" in r.data
    rows = q("SELECT amount_due, description FROM student_fees")
    assert len(rows) == 1 and rows[0]["amount_due"] == Decimal("1200.00") and rows[0]["description"] == "Monthly tuition"


def test_charge_whole_batch(admin, make_student, make_batch, today, q):
    bid = make_batch()
    for _ in range(3):
        admin.post(f"/batches/{bid}/enroll", data={"student_id": make_student()})
    admin.post("/fees/new", data=dict(target="batch", batch_id=bid, description="Term fee", period_label="T1", amount="750", due_date=today.isoformat()))
    assert q("SELECT count(*) AS n FROM student_fees")[0]["n"] == 3


def test_fee_validation(admin, make_student):
    sid = make_student()
    r = admin.post("/fees/new", data=dict(target="student", student_id=sid, description="", period_label="", amount="-1", due_date=""))
    assert r.status_code == 200 and b"greater than zero" in r.data and b"Enter a due date" in r.data


def test_plans_admin_only_and_validated(admin, instructor):
    assert instructor.post("/fees/plans", data={"name": "x"}).status_code == 403
    r = admin.post("/fees/plans", data={"name": "", "amount": "0", "period": "weekly"})
    assert b"Enter a plan name" in r.data


def test_payment_search_and_filter(admin, fee):
    admin.post("/fees/1/pay", data={"amount": "100", "method": "upi", "idem_key": "s"})
    assert b"RCP-" in admin.get("/fees/payments?method=upi").data
    assert b"RCP-" not in admin.get("/fees/payments?method=cash").data
    assert b"RCP-" in admin.get("/fees/payments?q=Student").data
