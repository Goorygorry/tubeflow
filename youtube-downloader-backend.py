# YouTube Audio Downloader - Flask Backend
# Install requirements: pip install flask yt-dlp

from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import re
import os

app = Flask(__name__)

# Support Render.com and other hosting platforms
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
CORS(app, origins=allowed_origins)

# Use /tmp on Render, otherwise user's Downloads folder
RENDER_MODE = os.environ.get("RENDER", "false").lower() == "true"
if RENDER_MODE:
    DOWNLOADS_FOLDER = "/tmp/downloads"
else:
    DOWNLOADS_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "TubeFlow")

os.makedirs(DOWNLOADS_FOLDER, exist_ok=True)

# Debug: Print all env vars (without sensitive values)
print("=== Environment Variables ===")
for key, value in os.environ.items():
    if "cookie" in key.lower() or "youtube" in key.lower():
        print(f"{key}: {value[:50]}..." if len(value) > 50 else f"{key}: {value}")
print("================================")

# YouTube cookies - read from environment variable and write to file
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")
COOKIES_FILE = os.path.join(DOWNLOADS_FOLDER, "cookies.txt")
if YOUTUBE_COOKIES:
    try:
        with open(COOKIES_FILE, "w") as f:
            f.write(YOUTUBE_COOKIES)
        print(f"✓ Cookies file created at {COOKIES_FILE}")
        print(f"  File exists: {os.path.exists(COOKIES_FILE)}")
    except Exception as e:
        print(f"✗ Error creating cookies file: {e}")
else:
    print(f"✗ No YOUTUBE_COOKIES environment variable found")
    print(
        f"  Available env vars with 'cookie': {[k for k in os.environ if 'cookie' in k.lower()]}"
    )

HTML_FILE = os.path.join(os.path.dirname(__file__), "youtube-downloader.html")


def sanitize_filename(name):
    """Remove invalid characters from filename"""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name[:200]
    return name


def rename_file(path, target_format):
    """Rename downloaded file to desired format"""
    if not path or not os.path.exists(path):
        return
    dir_name = os.path.dirname(path)
    base_name = os.path.splitext(os.path.basename(path))[0]
    new_path = os.path.join(dir_name, f"{base_name}.{target_format}")
    try:
        if os.path.exists(new_path):
            os.remove(new_path)
        os.rename(path, new_path)
    except Exception as e:
        print(f"Error renaming file: {e}")


@app.route("/api/get_video_info", methods=["POST"])
def api_get_video_info():
    data = request.json
    url = data.get("url")
    result = get_video_info(url)
    return jsonify(result)


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "youtube-downloader.html")


# Store download progress
download_progress = {}
active_downloads = {}


def get_video_info(url):
    """Get video information without downloading"""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }

    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                "success": True,
                "video": {
                    "id": info.get("id"),
                    "title": info.get("title"),
                    "thumbnail": info.get("thumbnail"),
                    "channel": info.get("uploader"),
                    "views": f"{info.get('view_count', 0):,}",
                    "date": info.get("upload_date", "Unknown"),
                    "duration": info.get("duration"),
                    "description": info.get("description", "")[:500],
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def progress_hook(progress, video_id):
    """Track download progress"""
    if progress["status"] == "downloading":
        total = progress.get("total_bytes") or progress.get("total_bytes_estimate", 0)
        downloaded = progress.get("downloaded_bytes", 0)
        if total > 0:
            percent = (downloaded / total) * 100
            download_progress[video_id] = {
                "percent": percent,
                "speed": progress.get("speed", 0),
                "status": "downloading",
            }
    elif progress["status"] == "finished":
        download_progress[video_id] = {"percent": 100, "status": "finished"}


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.json
    url = data.get("url")
    format_type = data.get("format", "mp3")
    quality = data.get("quality", "best")
    download_type = data.get("type", "audio")
    video_id = str(uuid.uuid4())[:8]

    output_template = os.path.join(DOWNLOADS_FOLDER, "%(title)s.%(ext)s")

    def convert_to_mp4(path):
        if not path or not os.path.exists(path):
            return
        if not path.endswith(".part"):
            if path.endswith(".mp4"):
                return
            mp4_path = path.rsplit(".", 1)[0] + ".mp4"
            try:
                import subprocess

                result = subprocess.run(
                    ["ffmpeg", "-i", path, "-c", "copy", "-y", mp4_path],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and os.path.exists(mp4_path):
                    os.remove(path)
                    print(f"Converted to MP4: {mp4_path}")
                else:
                    print(f"Conversion failed: {result.stderr}")
            except Exception as e:
                print(f"Conversion error: {e}")

    def convert_audio(path, out_format):
        if not path or not os.path.exists(path):
            return
        if path.endswith(".part"):
            return
        base, ext = os.path.splitext(path)
        if ext.lstrip(".") == out_format:
            return
        new_path = f"{base}.{out_format}"
        try:
            import subprocess

            codec = "libmp3lame" if out_format == "mp3" else "copy"
            result = subprocess.run(
                ["ffmpeg", "-i", path, "-vn", "-acodec", codec, "-y", new_path],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and os.path.exists(new_path):
                os.remove(path)
                print(f"Converted audio: {new_path}")
        except Exception as e:
            print(f"Audio conversion error: {e}")

    if download_type == "video":
        if quality == "best" or quality == "bestvideo" or quality == "4320":
            format_str = "bestvideo[height<=4320]+bestaudio/best"
        elif quality == "2160":
            format_str = "bestvideo[height<=2160]+bestaudio/best"
        elif quality == "1440":
            format_str = "bestvideo[height<=1440]+bestaudio/best"
        elif quality == "1080":
            format_str = "bestvideo[height<=1080]+bestaudio/best"
        elif quality == "720":
            format_str = "bestvideo[height<=720]+bestaudio/best"
        elif quality == "480":
            format_str = "bestvideo[height<=480]+bestaudio/best"
        else:
            format_str = "bestvideo[height<=1080]+bestaudio/best"

        ydl_opts = {
            "format": format_str,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [lambda p: progress_hook(p, video_id)],
            "post_hooks": [lambda path: convert_to_mp4(path)],
            "http_chunk_size": 10485760,
        }

        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE
    else:
        format_str = "bestaudio/best"
        ydl_opts = {
            "format": format_str,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [lambda p: progress_hook(p, video_id)],
            "post_hooks": [lambda path: convert_audio(path, format_type)],
            "http_chunk_size": 10485760,
        }

        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE

    try:
        ydl_opts_info = {"quiet": True}
        if os.path.exists(COOKIES_FILE):
            ydl_opts_info["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "audio")
            title = sanitize_filename(title)

        active_downloads[video_id] = {
            "url": url,
            "format": format_type,
            "quality": quality,
            "title": title,
        }

        def download_thread():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                download_progress[video_id]["status"] = "finished"
            except Exception as e:
                print(f"Download error: {e}")
                download_progress[video_id] = {"status": "error", "error": str(e)}

        thread = threading.Thread(target=download_thread)
        thread.start()

        return jsonify({"success": True, "video_id": video_id, "title": title})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def rename_file(path, target_format):
    """Rename downloaded file to desired format"""
    if not path or not os.path.exists(path):
        return
    dir_name = os.path.dirname(path)
    base_name = os.path.splitext(os.path.basename(path))[0]
    new_path = os.path.join(dir_name, f"{base_name}.{target_format}")
    try:
        if os.path.exists(new_path):
            os.remove(new_path)
        os.rename(path, new_path)
    except Exception as e:
        print(f"Error renaming file: {e}")


@app.route("/api/progress/<video_id>", methods=["GET"])
def api_progress(video_id):
    progress = download_progress.get(video_id, {"percent": 0, "status": "unknown"})
    return jsonify(progress)


@app.route("/api/downloads", methods=["GET"])
def api_downloads():
    files = []
    if os.path.exists(DOWNLOADS_FOLDER):
        for f in os.listdir(DOWNLOADS_FOLDER):
            if f.endswith((".mp3", ".m4a", ".wav", ".flac", ".mp4", ".webm")):
                path = os.path.join(DOWNLOADS_FOLDER, f)
                files.append(
                    {
                        "name": f,
                        "size": os.path.getsize(path),
                        "date": os.path.getmtime(path),
                    }
                )
    return jsonify(sorted(files, key=lambda x: x["date"], reverse=True))


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    import subprocess

    try:
        subprocess.Popen(f'explorer "{DOWNLOADS_FOLDER}"')
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/clear_history", methods=["POST"])
def api_clear_history():
    try:
        for f in os.listdir(DOWNLOADS_FOLDER):
            if f.endswith((".mp3", ".m4a", ".wav", ".flac", ".mp4", ".webm", ".mkv")):
                try:
                    os.remove(os.path.join(DOWNLOADS_FOLDER, f))
                except:
                    pass
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/trim_audio", methods=["POST"])
def api_trim_audio():
    data = request.json
    input_file = data.get("input_file")
    start_time = data.get("start_time", 0)
    end_time = data.get("end_time", 0)
    output_format = data.get("format", "mp3")

    if not input_file or not os.path.exists(input_file):
        return jsonify({"success": False, "error": "File not found"})

    base_name = os.path.splitext(os.path.basename(input_file))[0]
    output_file = os.path.join(DOWNLOADS_FOLDER, f"{base_name}_trimmed.{output_format}")

    try:
        import subprocess

        subprocess.run(
            [
                "ffmpeg",
                "-i",
                input_file,
                "-ss",
                str(start_time),
                "-to",
                str(end_time),
                "-vn",
                "-acodec",
                "libmp3lame" if output_format == "mp3" else "copy",
                "-y",
                output_file,
            ],
            capture_output=True,
            check=True,
        )
        return jsonify({"success": True, "output_file": output_file})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == "__main__":
    print("TubeFlow Pro - Web App")
    print("Downloads folder:", DOWNLOADS_FOLDER)
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
