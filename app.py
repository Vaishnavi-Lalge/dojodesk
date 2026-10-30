"""DojoDesk – karate dojo management. Entry point (application factory)."""
import logging
import os
import secrets
import sys
from datetime import timedelta
from decimal import Decimal

import click
import psycopg2
from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

import db_config
from auth_utils import is_admin, password_problem

csrf = CSRFProtect()
log = logging.getLogger("dojodesk")


def create_app(test_config=None):
    load_dotenv()  # real environment variables always win over .env
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    app = Flask(__name__)
    env = os.environ.get("APP_ENV", "development").lower()
    secret = os.environ.get("SECRET_KEY", "").strip()
    if env == "production" and len(secret) < 32:
        raise RuntimeError("SECRET_KEY must be set to a random string of 32+ characters in production.")
    app.config.update(
        APP_ENV=env,
        SECRET_KEY=secret or secrets.token_hex(32),  # dev only: sessions reset on restart
        TIMEZONE=os.environ.get("TIMEZONE", "Asia/Kolkata"),
        DOJO_NAME=os.environ.get("DOJO_NAME", "DojoDesk Karate Dojo"),
        CURRENCY_SYMBOL=os.environ.get("CURRENCY_SYMBOL", "₹"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(env == "production"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        WTF_CSRF_TIME_LIMIT=None,
        ATTENDANCE_EDIT_WINDOW_DAYS=7,
    )
    if test_config:
        app.config.update(test_config)
    if env == "production":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    csrf.init_app(app)
    app.teardown_appcontext(db_config.close_conn)

    from blueprints import attendance, auth, batches, dashboard, exams, fees, reports, students
    for module in (auth, dashboard, students, batches, attendance, fees, exams, reports):
        app.register_blueprint(module.bp)

    register_filters(app)
    register_errors(app)
    register_cli(app)

    @app.get("/healthz")
    def healthz():
        try:
            db_config.fetch_one("SELECT 1 AS ok")
            return jsonify(status="ok")
        except Exception:  # never leak connection details
            log.exception("Health check failed")
            return jsonify(status="unavailable"), 503

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    return app


def register_filters(app):
    from helpers import age_on, today

    @app.template_filter("money")
    def money(v):
        return f"{app.config['CURRENCY_SYMBOL']}{Decimal(v or 0):,.2f}"

    @app.template_filter("dfmt")
    def dfmt(v):
        return v.strftime("%d %b %Y") if v else "—"

    @app.template_filter("tfmt")
    def tfmt(v):
        return v.strftime("%I:%M %p").lstrip("0") if v else ""

    @app.template_filter("pairs")
    def pairs(rows, value="id", text="name"):
        return [(r[value], r[text]) for r in rows]

    @app.template_filter("age")
    def age(dob):
        return age_on(dob, today()) if dob else ""

    @app.context_processor
    def inject():
        def url_with(**overrides):
            args = request.args.to_dict()
            args.update(overrides)
            args = {k: v for k, v in args.items() if v not in (None, "")}
            return url_for(request.endpoint, **(request.view_args or {}), **args)
        return {"dojo_name": app.config["DOJO_NAME"], "url_with": url_with, "today": today,
                "is_admin": is_admin, "current_user": g.get("user")}


def register_errors(app):
    def page(code, title, message):
        return render_template("errors/error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def e400(e):
        return page(400, "That request didn't look right", "Go back, check the form and try again.")

    @app.errorhandler(CSRFError)
    def ecsrf(e):
        return page(400, "Your form expired",
                    "For your security the form could not be verified. Reload the page and try again.")

    @app.errorhandler(403)
    def e403(e):
        return page(403, "You don't have access to this",
                    "This action is limited to administrators. Ask an administrator if you need it.")

    @app.errorhandler(404)
    def e404(e):
        return page(404, "Page not found", "The page or record you asked for doesn't exist or was removed.")

    @app.errorhandler(405)
    def e405(e):
        return page(405, "That action isn't allowed here", "Go back to the previous page and use the buttons provided.")

    @app.errorhandler(413)
    def e413(e):
        return page(413, "That file is too large", "Choose a photo smaller than 2 MB.")

    @app.errorhandler(psycopg2.Error)
    def edb(e):
        log.error("Database error on %s %s: %s", request.method, request.path, type(e).__name__)
        log.debug("Database error detail", exc_info=True)
        return page(500, "We couldn't reach the database",
                    "Nothing was saved. Wait a moment and try again; if it keeps happening, contact the administrator.")

    @app.errorhandler(500)
    def e500(e):
        return page(500, "Something went wrong", "Nothing was saved. Try again; if it keeps happening, contact the administrator.")


def register_cli(app):
    @app.cli.command("db-upgrade")
    def db_upgrade():
        """Apply pending SQL migrations (and create the first admin from env, if needed)."""
        applied = db_config.run_migrations()
        click.echo("Applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        pw_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
        if username and pw_hash:
            with db_config.transaction() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM admins")
                if cur.fetchone()["n"] == 0:
                    cur.execute("INSERT INTO admins (username, password_hash, full_name, role) "
                                "VALUES (%s, %s, %s, 'admin')",
                                (username, pw_hash, os.environ.get("ADMIN_FULL_NAME", "Administrator")))
                    click.echo(f"Created first administrator '{username}'.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.option("--full-name", prompt=True)
    @click.option("--role", type=click.Choice(["admin", "instructor"]), default="admin", show_default=True)
    @click.password_option(prompt="Password")
    def create_admin(username, full_name, role, password):
        """Create an administrator or instructor account."""
        problem = password_problem(password)
        if problem:
            raise click.ClickException(problem)
        try:
            with db_config.transaction() as cur:
                cur.execute("INSERT INTO admins (username, password_hash, full_name, role) VALUES (%s,%s,%s,%s)",
                            (username.strip(), generate_password_hash(password), full_name.strip(), role))
        except psycopg2.errors.UniqueViolation:
            raise click.ClickException("That username already exists.")
        click.echo(f"Created {role} '{username}'.")

    @app.cli.command("reset-password")
    @click.argument("username")
    @click.password_option(prompt="New password")
    def reset_password(username, password):
        """Set a new password for an existing account."""
        problem = password_problem(password)
        if problem:
            raise click.ClickException(problem)
        with db_config.transaction() as cur:
            cur.execute("UPDATE admins SET password_hash=%s, is_active=TRUE WHERE lower(username)=lower(%s)",
                        (generate_password_hash(password), username))
            if cur.rowcount == 0:
                raise click.ClickException("No such user.")
        click.echo("Password updated.")

    @app.cli.command("hash-password")
    @click.password_option(prompt="Password")
    def hash_password(password):
        """Print a password hash for ADMIN_PASSWORD_HASH (no database needed)."""
        problem = password_problem(password)
        if problem:
            raise click.ClickException(problem)
        click.echo(generate_password_hash(password))


if __name__ == "__main__":
    create_app().run(debug=os.environ.get("APP_ENV", "development") != "production")
