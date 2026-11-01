import time
from collections import defaultdict, deque

from flask import (Blueprint, flash, g, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from auth_utils import login_required, password_problem
from db_config import fetch_one, transaction
from helpers import audit, safe_next

bp = Blueprint("auth", __name__)

# Failed-login throttle (per process; with several workers the limit is per worker).
MAX_FAILS, WINDOW = 5, 15 * 60
_fails = defaultdict(deque)
_dummy_hash = None


def _throttled(key):
    q = _fails[key]
    while q and time.time() - q[0] > WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILS


@bp.route("/login", methods=["GET", "POST"])
def login():
    global _dummy_hash
    if request.method == "GET":
        if session.get("parent_uid"):
            return redirect(url_for("parents.index"))
        if session.get("uid"):
            return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Enter both your username and your password.", "error")
            return render_template("login.html", username=username), 400
        key = (request.remote_addr, username.lower())
        if _throttled(key):
            flash("Too many failed attempts. Wait 15 minutes, then try again.", "error")
            return render_template("login.html", username=username), 429
        user = fetch_one("SELECT id, password_hash, is_active FROM admins "
                         "WHERE lower(username) = lower(%s)", (username,))
        if _dummy_hash is None:
            _dummy_hash = generate_password_hash("not-a-real-password")
        # always verify a hash so response time does not reveal whether the user exists
        good = check_password_hash(user["password_hash"] if user else _dummy_hash, password)
        if not (user and user["is_active"] and good):
            _fails[key].append(time.time())
            flash("That username and password don't match. Check both and try again.", "error")
            return render_template("login.html", username=username), 401
        _fails.pop(key, None)
        session.clear()  # new session on login (prevents session fixation)
        session["uid"] = user["id"]
        session.permanent = True
        g.user = {"id": user["id"]}
        with transaction() as cur:
            cur.execute("UPDATE admins SET last_login_at = now() WHERE id = %s", (user["id"],))
            audit(cur, "login", "admin", user["id"])
        return redirect(safe_next(request.args.get("next")) or url_for("dashboard.index"))
    return render_template("login.html", username="")


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        row = fetch_one("SELECT password_hash FROM admins WHERE id = %s", (g.user["id"],))
        problem = password_problem(new)
        if not check_password_hash(row["password_hash"], current):
            flash("Your current password is incorrect.", "error")
        elif new != confirm:
            flash("The new passwords don't match.", "error")
        elif problem:
            flash(problem, "error")
        else:
            with transaction() as cur:
                cur.execute("UPDATE admins SET password_hash = %s WHERE id = %s",
                            (generate_password_hash(new), g.user["id"]))
                audit(cur, "change_password", "admin", g.user["id"])
            flash("Password changed.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("account.html")
