from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, send_from_directory

from media_utils import syllabus_files

bp = Blueprint("syllabus", __name__, url_prefix="/syllabus")


@bp.get("/")
def index():
    return render_template("syllabus.html", files=syllabus_files(), parent_view=False, public_view=True)


@bp.get("/<path:filename>")
def pdf(filename):
    if filename not in {item["filename"] for item in syllabus_files()}:
        abort(404)
    folder = Path(current_app.config["MEDIA_DIR"]) / "syllabus"
    response = send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=False)
    response.headers["Cache-Control"] = "public, max-age=300"
    return response
