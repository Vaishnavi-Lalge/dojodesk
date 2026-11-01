"""Authentication decorators and password rules."""
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

from db_config import fetch_one

MIN_PASSWORD_LEN = 10


def password_problem(pw):
    if len(pw) < MIN_PASSWORD_LEN:
        return f"Use at least {MIN_PASSWORD_LEN} characters."
    if pw.lower() == pw or pw.upper() == pw or not any(c.isdigit() for c in pw):
        return "Mix upper and lower case letters and include at least one number."
    return None


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = None
        uid = session.get("uid")
        if uid:
            user = fetch_one("SELECT id, username, full_name, role FROM admins "
                             "WHERE id = %s AND is_active", (uid,))
        if not user:
            if session.get("parent_uid"):
                return redirect(url_for("parents.index"))
            session.clear()
            nxt = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=nxt))
        g.user = user
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def parent_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        uid = session.get("parent_uid")
        user = fetch_one("SELECT id, username, full_name FROM parent_accounts "
                         "WHERE id = %s AND is_active", (uid,)) if uid else None
        if not user:
            session.pop("parent_uid", None)
            nxt = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("parents.login", next=nxt))
        g.parent_user = user
        return view(*args, **kwargs)
    return wrapper


def is_admin():
    return bool(g.get("user")) and g.user["role"] == "admin"
