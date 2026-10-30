import pytest
from conftest import PW

PROTECTED = ["/", "/students/", "/students/new", "/students/export.csv", "/attendance/", "/attendance/records",
             "/batches/", "/batches/new", "/fees/", "/fees/new", "/fees/plans", "/fees/payments", "/exams",
             "/exams/new", "/belts", "/reports/", "/reports/revenue", "/account/password", "/students/1",
             "/students/1/photo", "/fees/1", "/exams/1", "/promotions/1/certificate", "/fees/receipt/1"]


def test_login_logout_flow(app, anon):
    r = anon.post("/login", data={"username": "boss", "password": PW})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert anon.get("/").status_code == 200
    assert anon.post("/logout").status_code == 302
    assert anon.get("/").status_code == 302  # session gone


def test_wrong_password_and_validation_messages(anon):
    r = anon.post("/login", data={"username": "boss", "password": "nope"})
    assert r.status_code == 401 and b"don&#39;t match" in r.data
    r = anon.post("/login", data={"username": "", "password": ""})
    assert r.status_code == 400 and b"Enter both" in r.data
    assert anon.post("/login", data={"username": "ghost", "password": PW}).status_code == 401


def test_password_is_hashed_in_database(q):
    h = q("SELECT password_hash FROM admins WHERE username='boss'")[0]["password_hash"]
    assert PW not in h and h.startswith("pbkdf2:")


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_redirect_anonymous(anon, path):
    r = anon.get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_protected_posts_reject_anonymous(anon):
    for path in ("/students/new", "/attendance/save", "/fees/1/pay", "/exams/1/promote", "/batches/1/enroll"):
        r = anon.post(path, data={})
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_no_public_registration(anon):
    assert anon.get("/register").status_code == 404 and anon.post("/register").status_code == 404


def test_open_redirect_is_blocked(anon):
    r = anon.post("/login?next=//evil.example", data={"username": "boss", "password": PW})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]


def test_login_throttle(anon):
    for _ in range(5):
        anon.post("/login", data={"username": "boss", "password": "bad"})
    r = anon.post("/login", data={"username": "boss", "password": PW})
    assert r.status_code == 429


def test_deactivated_account_loses_access(app, admin, q):
    import db_config
    with app.app_context():
        with db_config.transaction() as cur:
            cur.execute("UPDATE admins SET is_active=FALSE WHERE username='boss'")
    assert admin.get("/").status_code == 302


def test_csrf_enforced_when_enabled(app):
    app.config["WTF_CSRF_ENABLED"] = True
    c = app.test_client()
    r = c.post("/login", data={"username": "boss", "password": PW})
    assert r.status_code == 400
    assert b"form expired" in r.data


def test_secure_cookie_flags(app):
    assert app.config["SESSION_COOKIE_HTTPONLY"] and app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_production_requires_strong_secret(monkeypatch):
    from app import create_app
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(RuntimeError):
        create_app()
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    assert create_app({"TESTING": True}).config["SESSION_COOKIE_SECURE"] is True


def test_instructor_cannot_use_admin_features(instructor):
    for path in ("/belts", "/fees/plans", "/students/export.csv", "/reports/revenue", "/reports/directory", "/reports/audit"):
        assert instructor.get(path).status_code == 403, path
    r = instructor.get("/")
    assert b"Belt system" not in r.data


def test_change_password(app, admin, anon):
    r = admin.post("/account/password", data={"current_password": PW, "new_password": "NewPassw0rd!!", "confirm_password": "NewPassw0rd!!"})
    assert r.status_code == 302
    assert anon.post("/login", data={"username": "boss", "password": "NewPassw0rd!!"}).status_code == 302
    r = admin.post("/account/password", data={"current_password": "wrong", "new_password": "NewPassw0rd!!", "confirm_password": "NewPassw0rd!!"})
    assert b"incorrect" in r.data


def test_health_endpoint_hides_details(anon, monkeypatch):
    r = anon.get("/healthz")
    assert r.status_code == 200 and r.json == {"status": "ok"}
    import db_config
    def boom():
        raise __import__("psycopg2").OperationalError("password authentication failed for user secretuser")
    monkeypatch.setattr(db_config, "get_conn", boom)
    r = anon.get("/healthz")
    assert r.status_code == 503 and b"secretuser" not in r.data
