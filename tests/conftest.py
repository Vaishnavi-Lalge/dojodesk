"""Tests run against a REAL PostgreSQL database. Point TEST_DATABASE_URL at a throw-away
database whose name contains "test" - its public schema is dropped and rebuilt."""
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TEST_URL = os.environ.get("TEST_DATABASE_URL")
PW = "Passw0rd-long"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    if not TEST_URL:
        pytest.skip("set TEST_DATABASE_URL to run the test-suite", allow_module_level=True)
    dbname = TEST_URL.rsplit("/", 1)[-1].split("?")[0]
    assert "test" in dbname, "refusing to wipe a database whose name doesn't contain 'test'"
    os.environ["DATABASE_URL"] = TEST_URL
    os.environ.setdefault("DB_SSLMODE", "disable")
    os.environ["APP_ENV"] = "development"
    import db_config
    conn = db_config.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()
    assert db_config.run_migrations() == ["001_schema.sql", "002_seed_belts.sql", "003_parent_portal.sql"]


@pytest.fixture()
def app(_schema):
    import db_config
    from app import create_app
    from blueprints import auth
    auth._fails.clear()
    from blueprints import parents
    parents._fails.clear()
    application = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SECRET_KEY": "test-secret"})
    conn = db_config.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE admins, parent_accounts, students, batches, batch_enrollments, attendance, fee_plans, student_fees, "
                    "payments, examinations, belt_promotions, audit_logs, belt_ranks RESTART IDENTITY CASCADE")
        for seq in ("student_code_seq", "batch_code_seq", "receipt_seq"):
            cur.execute(f"ALTER SEQUENCE {seq} RESTART")
        cur.execute((Path(db_config.MIGRATIONS_DIR) / "002_seed_belts.sql").read_text())
        for name, role in (("boss", "admin"), ("sensei", "instructor")):
            cur.execute("INSERT INTO admins (username, password_hash, full_name, role) VALUES (%s,%s,%s,%s)",
                        (name, generate_password_hash(PW, method="pbkdf2:sha256:1000"), name.title(), role))
    conn.close()
    return application


def _login(app, username):
    c = app.test_client()
    r = c.post("/login", data={"username": username, "password": PW})
    assert r.status_code == 302, r.data
    return c


@pytest.fixture()
def admin(app):
    return _login(app, "boss")


@pytest.fixture()
def instructor(app):
    return _login(app, "sensei")


@pytest.fixture()
def anon(app):
    return app.test_client()


@pytest.fixture()
def q(app):
    """Run a SQL query and return rows."""
    import db_config

    def run(sql, params=None):
        with app.app_context():
            return db_config.fetch_all(sql, params)
    return run


@pytest.fixture()
def today(app):
    from helpers import today as _today
    with app.app_context():
        return _today()


@pytest.fixture()
def belt(q):
    return lambda name: q("SELECT id FROM belt_ranks WHERE name=%s", (name,))[0]["id"]


def _id(resp):
    return int(resp.headers["Location"].split("?")[0].rstrip("/").split("/")[-1])


@pytest.fixture()
def make_student(admin, belt):
    n = {"i": 0}

    def make(client=None, **over):
        n["i"] += 1
        data = dict(full_name=f"Student {n['i']}", dob="2010-05-12", gender="female", phone="9876543210",
                    guardian_name="Parent", guardian_phone="9876500000", email="", address="Pune",
                    joining_date="2025-01-10", belt_id=belt("White Belt"), batch_id="")
        data.update(over)
        r = (client or admin).post("/students/new", data=data)
        assert r.status_code == 302, r.data.decode()[:2000]
        (client or admin).get("/account/password")  # consume the success flash message
        return _id(r)
    return make


@pytest.fixture()
def make_batch(admin):
    n = {"i": 0}

    def make(**over):
        n["i"] += 1
        data = dict(name=f"Batch {n['i']}", instructor="Sensei", training_days=["Mon", "Wed"],
                    start_time="17:00", end_time="18:00", capacity="10", is_active="on")
        data.update(over)
        r = admin.post("/batches/new", data=data)
        assert r.status_code == 302, r.data.decode()[:2000]
        admin.get("/account/password")  # consume the success flash message
        return _id(r)
    return make
