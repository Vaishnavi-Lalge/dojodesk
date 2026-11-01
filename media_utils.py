"""Private media folder helpers."""
from pathlib import Path

from flask import current_app

VIDEO_CATEGORIES = ("kata", "kumite")
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def student_video_files(student_code):
    root = Path(current_app.config["MEDIA_DIR"]) / "students" / student_code
    videos = []
    for category in VIDEO_CATEGORIES:
        folder = root / category
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in VIDEO_EXTENSIONS:
                videos.append({"category": category, "filename": path.name})
    return videos


def student_photo_files(student_code):
    folder = Path(current_app.config["MEDIA_DIR"]) / "students" / student_code / "photos"
    if not folder.is_dir():
        return []
    return [
        path.name for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in PHOTO_EXTENSIONS
    ]


def syllabus_files():
    folder = Path(current_app.config["MEDIA_DIR"]) / "syllabus"
    if not folder.is_dir():
        return []
    return [
        {"filename": path.name, "title": path.stem.replace("_", " ").replace("-", " ").title()}
        for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".pdf"
    ]
