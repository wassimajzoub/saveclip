"""
Video Downloader - Flask Backend
Supports downloading videos from TikTok and Instagram.
Uses yt-dlp under the hood.

Usage:
    pip install flask yt-dlp
    python app.py
    Open http://localhost:5000 in your browser
"""

import os
import re
import uuid
import time
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, send_file

app = Flask(__name__, static_folder="static", static_url_path="")

# Configuration
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Track active downloads: { task_id: { status, progress, filename, error, url } }
downloads = {}

# Cleanup old files after 30 minutes
CLEANUP_INTERVAL = 1800


def cleanup_old_files():
    """Remove downloaded files older than 30 minutes."""
    while True:
        time.sleep(300)  # Check every 5 minutes
        now = time.time()
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > CLEANUP_INTERVAL:
                try:
                    f.unlink()
                except Exception:
                    pass


cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()


def is_valid_url(url):
    """Validate that the URL is from TikTok or Instagram."""
    patterns = [
        r"(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/",
        r"(https?://)?(www\.)?instagram\.com/",
        r"(https?://)?ddinstagram\.com/",
    ]
    return any(re.match(p, url) for p in patterns)


def get_platform(url):
    """Detect which platform the URL belongs to."""
    if "tiktok" in url.lower():
        return "tiktok"
    elif "instagram" in url.lower() or "ddinstagram" in url.lower():
        return "instagram"
    return "unknown"


def download_video(task_id, url):
    """Download a video using yt-dlp in a background thread."""
    import yt_dlp

    downloads[task_id]["status"] = "downloading"
    downloads[task_id]["progress"] = 0

    output_template = str(DOWNLOAD_DIR / f"{task_id}_%(title).80s.%(ext)s")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                downloads[task_id]["progress"] = round(
                    (downloaded / total) * 100, 1
                )
            else:
                downloads[task_id]["progress"] = -1  # Indeterminate
        elif d["status"] == "finished":
            downloads[task_id]["progress"] = 100

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "progress_hooks": [progress_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "merge_output_format": "mp4",
        # For Instagram stories / reels that need cookies
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First extract info to get metadata
            info = ydl.extract_info(url, download=False)
            downloads[task_id]["title"] = info.get("title", "video")
            downloads[task_id]["thumbnail"] = info.get("thumbnail", "")
            downloads[task_id]["duration"] = info.get("duration", 0)
            downloads[task_id]["uploader"] = info.get("uploader", "")

            # Now download
            ydl.download([url])

        # Find the downloaded file
        for f in DOWNLOAD_DIR.iterdir():
            if f.name.startswith(task_id):
                downloads[task_id]["status"] = "complete"
                downloads[task_id]["filename"] = f.name
                downloads[task_id]["filesize"] = f.stat().st_size
                return

        downloads[task_id]["status"] = "error"
        downloads[task_id]["error"] = "Download completed but file not found."

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Private" in error_msg or "login" in error_msg.lower():
            downloads[task_id]["error"] = (
                "This content is private or requires login."
            )
        elif "not found" in error_msg.lower() or "404" in error_msg:
            downloads[task_id]["error"] = (
                "Video not found. It may have been deleted."
            )
        else:
            downloads[task_id]["error"] = (
                "Could not download the video. It may be unavailable or "
                "the platform may be blocking the request."
            )
        downloads[task_id]["status"] = "error"

    except Exception as e:
        downloads[task_id]["status"] = "error"
        downloads[task_id]["error"] = f"Unexpected error: {str(e)}"


# âââ Routes âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    """Start a video download. Returns a task ID for polling progress."""
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "Please provide a URL."}), 400

    # Add https if missing
    if not url.startswith("http"):
        url = "https://" + url

    if not is_valid_url(url):
        return jsonify({
            "error": "Please enter a valid TikTok or Instagram URL."
        }), 400

    task_id = str(uuid.uuid4())[:8]
    platform = get_platform(url)

    downloads[task_id] = {
        "status": "queued",
        "progress": 0,
        "filename": None,
        "error": None,
        "url": url,
        "platform": platform,
        "title": "",
        "thumbnail": "",
        "duration": 0,
        "uploader": "",
        "filesize": 0,
    }

    thread = threading.Thread(target=download_video, args=(task_id, url))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "platform": platform})


@app.route("/api/status/<task_id>")
def get_status(task_id):
    """Poll the status of a download task."""
    task = downloads.get(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404
    return jsonify(task)


@app.route("/api/file/<task_id>")
def download_file(task_id):
    """Download the completed video file."""
    task = downloads.get(task_id)
    if not task or task["status"] != "complete":
        return jsonify({"error": "File not ready."}), 404

    filepath = DOWNLOAD_DIR / task["filename"]
    if not filepath.exists():
        return jsonify({"error": "File not found."}), 404

    # Create a clean download name
    clean_name = re.sub(r"^[a-f0-9]+_", "", task["filename"])
    if not clean_name:
        clean_name = task["filename"]

    return send_file(filepath, as_attachment=True, download_name=clean_name)


# âââ Main âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

if __name__ == "__main__":
    print("\nð¬ Video Downloader is running!")
    print("   Open http://localhost:5000 in your browser\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
