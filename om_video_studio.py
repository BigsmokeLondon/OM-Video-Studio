#!/usr/bin/env python3
"""
O.M. Video Studio - point-and-click video, audio and image tools, powered by ffmpeg.

Pick a file (or a folder for batch), press a button, set a few options, run.
Standard library only (tkinter). Needs ffmpeg + ffprobe on PATH.

    python om_video_studio.py             # open the GUI
    python om_video_studio.py --selftest  # build + run every operation on a generated clip
    python om_video_studio.py --list      # list operations
"""
# =============================================================================
#  FILE MAP (read this first)
# =============================================================================
#  This single file is the whole application. Top to bottom it is laid out as:
#
#   1. Constants & paths ........ app name/version, file-type sets, where data lives
#   2. ffmpeg location/install .. find_tool, refresh_tools, download + install_ffmpeg
#   3. Error log ................ write_log -> om_video_studio_log.txt
#   4. Helpers .................. tsec, probe, vcodec, aargs, atempo, Ctx, job
#   5. Operation registry ....... OPS list + O()/register()/@op decorator
#   6. Operations (the features). one function per button, grouped by category:
#         Video, Format & Quality, Audio, Images & Frames, Analysis, Streaming,
#         Video (extra), Combine, Text & Subtitles, Effects & Colour
#   7. Engine ................... default_opts, execute, run_job, convert_opts
#   8. Preview / queue engine ... format_commands, preview_frame, run_queue ...
#   9. Settings & startup sound . load_settings / save_settings
#  10. Languages ................ LANGS/TR (from om_translations.py), tr(), set_lang()
#  11. Completion pings ......... generated WAV tones + play_ping()
#  12. GUI ...................... gui(): the whole tkinter window, built in one function
#  13. Self-test & CLI .......... selftest(), `--selftest`, `--list`, default = GUI
#
#  HOW A BUTTON WORKS (the key idea):
#    Each feature is a *builder function* decorated with @op(...). The builder
#    does NOT run ffmpeg. It receives the user's options `o` and a per-file
#    context `c` (Ctx) and returns a list of "jobs" (dicts holding the ffmpeg
#    argument list, the output path and the expected duration). The engine
#    (execute -> run_job) then runs those jobs, streams progress, and handles
#    cancel / GPU fallback / logging. Because builders are pure "args in, args
#    out", the same code feeds the GUI, the "copy ffmpeg command" button, the
#    queue, the live preview and the headless self-test.
#
#  This file is standard-library only (tkinter + a few optional extras that the
#  GUI imports lazily: sv_ttk, tkinterdnd2, pywinstyles, certifi).
#  Comments in this file are explanatory only - they do not change behaviour.
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# --- 1. CONSTANTS & PATHS --------------------------------------------------
#  APP / VERSION: shown in the title bar. CI reads the `VERSION = "x.y.z"` line
#  with a regex (see .github/workflows/release.yml) and publishes release
#  v<VERSION>, so bumping this line is how a new release is cut.
#  *_EXT sets: decide which files the "kind" checker accepts for each operation
#  and which extensions the file pickers show.
APP = "O.M. Video Studio"
VERSION = "1.14.0"
GITHUB_REPO = "BigsmokeLondon/OM-Video-Studio"  # releases are checked here for updates
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
SUB_EXT = {".srt", ".ass", ".vtt"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
# STATE is process-wide mutable switches. "gpu" is toggled by the GPU checkbox
#  and read by vcodec(); execute() flips it back to False if the GPU encoder fails.
#  "gpu_enc" is which hardware encoder detect_gpu() found (a key of GPU_ENCODERS).
STATE = {"gpu": False, "gpu_enc": "nvenc"}
# On Windows, stops a black console window flashing for every ffmpeg/ffprobe call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Path logic - two modes:
#    * running from source  : APP_DIR = folder of this .py, DATA_DIR = same folder
#    * frozen (PyInstaller) : APP_DIR = folder of the .exe, RES_DIR = the temp
#      _MEIPASS bundle (read-only Resource/ + app.ico), DATA_DIR = %LOCALAPPDATA%
FROZEN = bool(getattr(sys, "frozen", False))  # True when running as the installed .exe
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"  # macOS support is experimental (see docs/PROJECT_OVERVIEW.md)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # bundled read-only files (Resource/, app.ico)
# Installed copies live in Program Files (read-only), so logs and the downloaded ffmpeg go in the user's data folder.
if FROZEN and IS_MAC:  # inside a .app bundle the folder is read-only/signed, so user data goes in Application Support
    DATA_DIR = Path.home() / "Library" / "Application Support" / "O.M. Video Studio"
elif FROZEN and not IS_WIN:
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "O.M. Video Studio"
else:
    DATA_DIR = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "O.M. Video Studio") if FROZEN else APP_DIR
# FFMPEG_HOME: where install_ffmpeg() unpacks a downloaded ffmpeg. SETTINGS_PATH: per-user JSON (theme, language, favourites, last values...).
FFMPEG_HOME = DATA_DIR / "ffmpeg"
SETTINGS_PATH = Path.home() / ".om_video_studio.json"


# One-off upgrade helper for the old product name (FFmpeg Studio 1.0.x). Runs once at import, never raises.
def _migrate_old_data():
    """Versions 1.0.x were called 'O.M. FFmpeg Studio': carry their data folder (incl. the downloaded ffmpeg) and settings across."""
    try:
        old_dir = DATA_DIR.parent / "O.M. FFmpeg Studio"
        if FROZEN and old_dir.is_dir() and not DATA_DIR.exists():
            try:
                old_dir.rename(DATA_DIR)
            except OSError:
                shutil.copytree(old_dir, DATA_DIR)
            old_log = DATA_DIR / "ffmpeg_studio_log.txt"
            if old_log.exists() and not (DATA_DIR / "om_video_studio_log.txt").exists():
                old_log.rename(DATA_DIR / "om_video_studio_log.txt")
        old_settings = Path.home() / ".ffmpeg_studio.json"
        if old_settings.exists() and not SETTINGS_PATH.exists():
            shutil.copy2(old_settings, SETTINGS_PATH)
    except Exception:  # noqa: BLE001  (migration is best-effort)
        pass


_migrate_old_data()


# --- 2. LOCATING / INSTALLING FFMPEG ---------------------------------------------
def find_tool(name):
    """ffmpeg/ffprobe/ffplay: the user's own install on PATH first, then the copy this app downloaded."""
    exe = name + (".exe" if os.name == "nt" else "")
    # Apps started from Finder do not inherit the shell PATH, so also look where Homebrew / MacPorts put ffmpeg.
    extra = "" if IS_WIN else os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin"])
    return shutil.which(name) or (shutil.which(name, path=extra) if extra else None) or next((str(d / exe) for d in (FFMPEG_HOME / "bin", FFMPEG_HOME) if (d / exe).exists()), None)


# FFMPEG / FFPROBE hold the executable paths used by every subprocess call. They are module globals so refresh_tools() can re-point them after an install.
FFMPEG = find_tool("ffmpeg") or "ffmpeg"
FFPROBE = find_tool("ffprobe") or "ffprobe"


# Re-run the lookup (called after install_ffmpeg succeeds).
def refresh_tools():
    global FFMPEG, FFPROBE
    FFMPEG = find_tool("ffmpeg") or "ffmpeg"
    FFPROBE = find_tool("ffprobe") or "ffprobe"
    return bool(find_tool("ffmpeg") and find_tool("ffprobe"))


# Download sources for the in-app installer, tried in order. The first is a full GPL build (includes libvidstab for Stabilise).
FFMPEG_URLS = [  # first: full GPL build (has libvidstab etc.); fallback: gyan.dev essentials (no stabilise filter)
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]


# urlopen with retries and a certifi-backed SSL context, so downloads work on machines with missing/old Windows root certificates.
def urlopen_robust(req, timeout=30):
    """urlopen that survives PCs whose Windows root-certificate store is incomplete (common on fresh installs):
    try the system certificates first, then the bundled certifi list."""
    import ssl
    import urllib.error
    import urllib.request
    ctxs = [ssl.create_default_context()]
    try:
        import certifi
        ctxs.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:  # noqa: BLE001
        pass
    last = None
    for ctx in ctxs:
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except (ssl.SSLError, urllib.error.URLError) as e:
            if isinstance(e, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
                last = e
                write_log(f"TLS certificate check failed with one trust store ({type(e).__name__}); trying the next")
                continue
            raise
    raise last


# Streams a URL to disk, calling report(text) with percentages so the GUI can show progress.
def download_file(url, dest, report):
    """Download url to dest with progress. Falls back to Windows' own curl.exe (which can fetch missing root certificates)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OM-Video-Studio"})
        with urlopen_robust(req, timeout=30) as r, open(dest, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                report(0.9 * done / total if total else 0.0, f"Downloading ffmpeg… {done // 1048576} MB" + (f" of {total // 1048576} MB" if total else ""))
        return
    except Exception as e:  # noqa: BLE001
        write_log(f"Python download failed ({type(e).__name__}: {e}); trying curl.exe")
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("download failed and curl.exe is not available")
    report(0.0, "Downloading ffmpeg with Windows curl… (no progress shown, please wait)")
    r = subprocess.run([curl, "-L", "--fail", "--silent", "--show-error", "-o", str(dest), url], capture_output=True, text=True,
                       creationflags=NO_WINDOW, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError("curl: " + (r.stderr or "").strip()[-200:])


# Download the zip, unpack into FFMPEG_HOME, locate bin/ffmpeg.exe, refresh_tools(). Returns (ok, message). Called from the GUI's offer_ffmpeg_install().
def install_ffmpeg(report):
    """Download ffmpeg into DATA_DIR/ffmpeg (no admin needed). report(fraction, text). Returns (ok, message)."""
    import zipfile
    if os.name != "nt":
        return False, "Automatic install is Windows-only. Install ffmpeg with your package manager (e.g. brew install ffmpeg or apt install ffmpeg)."
    last = ""
    for url in FFMPEG_URLS:
        zpath = Path(tempfile.gettempdir()) / "om_ffmpeg_download.zip"
        try:
            report(0.0, "Connecting…")
            download_file(url, zpath, report)
            report(0.92, "Unpacking…")
            dest = FFMPEG_HOME / "bin"
            dest.mkdir(parents=True, exist_ok=True)
            got = set()
            with zipfile.ZipFile(zpath) as z:
                for m in z.namelist():
                    low = m.lower().replace("\\", "/")
                    base = low.rsplit("/", 1)[-1]
                    if "/bin/" in low and base in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
                        with z.open(m) as src, open(dest / base, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        got.add(base)
                    elif base in ("license", "license.txt", "readme.txt") and low.count("/") <= 1:
                        with z.open(m) as src, open(FFMPEG_HOME / ("ffmpeg-" + base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            if not {"ffmpeg.exe", "ffprobe.exe"} <= got:
                last = "The download did not contain ffmpeg.exe / ffprobe.exe."
                continue
            report(0.97, "Checking…")
            if not refresh_tools():
                last = "ffmpeg was unpacked but would not start."
                continue
            t = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30)
            if t.returncode != 0:
                last = "ffmpeg was unpacked but would not start."
                continue
            report(1.0, "Done")
            write_log(f"ffmpeg installed to {dest} from {url}")
            return True, "ffmpeg installed."
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            write_log(f"ffmpeg download failed from {url}: {last}")
        finally:
            try:
                zpath.unlink()
            except OSError:
                pass
    return False, "Could not download ffmpeg (" + last + "). Check your internet connection, or install it yourself with:  winget install Gyan.FFmpeg"


# --- 3. PROBLEM LOG ---------------------------------------------------------------
def _log_path():
    """Problem log lives next to the program; falls back to the home folder if that is read-only."""
    for d in (DATA_DIR, Path.home()):
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / "om_video_studio_log.txt"
            with open(f, "a", encoding="utf-8"):
                pass
            return f
        except OSError:
            continue
    return None


LOG_FILE = _log_path()


# Used for failed ffmpeg runs (full command + output) and uncaught exceptions; ask users to send this file when reporting a bug.
def write_log(text):
    """Append a timestamped entry to om_video_studio_log.txt (trimmed to ~1 MB). Never raises."""
    if not LOG_FILE:
        return
    try:
        import datetime
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text(LOG_FILE.read_text(encoding="utf-8", errors="replace")[-400_000:], encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except Exception:  # noqa: BLE001
        pass


# Route any uncaught Python exception into the log before the default handler prints it.
def _excepthook(et, ev, tb):
    import traceback
    write_log("UNCAUGHT ERROR\n" + "".join(traceback.format_exception(et, ev, tb)))
    sys.__excepthook__(et, ev, tb)


sys.excepthook = _excepthook


# --------------------------------------------------------------------------- helpers
# --- 4. HELPERS -------------------------------------------------------------
#  tsec: lenient time parser used by every time box. Accepts plain seconds
#  ("90"), h:m:s ("1:02:03.5") and the app's slider format min:sec:millis
#  ("001:22:400").
def tsec(s):
    """'1:02:03.5' / '90' / '001:22:400' (min:sec:ms) / '' -> seconds."""
    s = str(s).strip()
    if not s:
        return 0.0
    parts = s.split(":")
    if len(parts) == 3 and len(parts[2]) == 3 and parts[2].isdigit() and "." not in s:
        return int(float(parts[0])) * 60 + float(parts[1]) + int(parts[2]) / 1000.0  # minutes:seconds:milliseconds, e.g. 001:22:400
    t = 0.0
    for p in s.split(":"):
        t = t * 60 + float(p)
    return t


# Run ffprobe once and return a flat dict: duration, first real video stream (ignores cover art), first audio stream, width/height/fps, size, bitrate, plus the raw JSON. Failures yield an 'empty' dict rather than raising.
def probe(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        j = json.loads(r.stdout or "{}")
    except Exception:
        j = {}
    streams = j.get("streams", [])
    fmt = j.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        dur = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    fps = 0.0
    if v and v.get("r_frame_rate", "0/1") not in ("0/0",):
        try:
            n, d = v["r_frame_rate"].split("/")
            fps = float(n) / float(d)
        except Exception:
            pass
    return dict(raw=j, dur=dur, v=v, a=a, has_v=v is not None, has_a=a is not None,
                w=int((v or {}).get("width", 0)), h=int((v or {}).get("height", 0)), fps=fps,
                size=int(fmt.get("size", 0) or 0), bitrate=int(fmt.get("bit_rate", 0) or 0))


# One-line description of a file for the info strip under the Input box.
def summary(info, path):
    parts = [Path(path).name]
    if info["dur"]:
        m, s = divmod(info["dur"], 60)
        parts.append(f"{int(m)}:{s:04.1f}")
    if info["has_v"]:
        parts.append(f'{info["w"]}x{info["h"]} {info["v"].get("codec_name", "?")} {info["fps"]:.2f}fps')
    if info["has_a"]:
        parts.append(f'audio {info["a"].get("codec_name", "?")} {info["a"].get("sample_rate", "")}Hz')
    if info["size"]:
        parts.append(f'{info["size"] / 1e6:.1f} MB')
    return "  |  ".join(parts)


# Hardware H.264 encoders, in the order detect_gpu() tries them. tag = text that shows up in ffmpeg's error output for that encoder (used to decide a failure was the GPU's fault). args(crf) = quality-based settings matching the CPU CRF as closely as each encoder allows.
GPU_ENCODERS = {
    "nvenc": dict(label="NVIDIA NVENC", codec="h264_nvenc", tag=("nvenc", "nvcuda", "cuda"),
                  args=lambda crf: ["-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p"]),
    "amf": dict(label="AMD AMF", codec="h264_amf", tag=("amf",),
                args=lambda crf: ["-quality", "balanced", "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf), "-pix_fmt", "yuv420p"]),
    "qsv": dict(label="Intel Quick Sync", codec="h264_qsv", tag=("qsv", "mfx", "libvpl"),
                args=lambda crf: ["-preset", "medium", "-global_quality", str(crf)]),
    "videotoolbox": dict(label="Apple VideoToolbox", codec="h264_videotoolbox", tag=("videotoolbox",),
                         args=lambda crf: ["-q:v", str(max(1, min(100, round(100 - crf * 2.2)))), "-pix_fmt", "yuv420p"]),
}


# H.264 encoder arguments. CPU path = libx264 veryfast + CRF; GPU path = whichever hardware encoder was detected (NVIDIA, AMD, Intel or Apple). yuv420p keeps output playable everywhere.
def vcodec(crf=20):
    if STATE["gpu"]:
        enc = GPU_ENCODERS[STATE["gpu_enc"]]
        return ["-c:v", enc["codec"]] + enc["args"](crf)
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]


# Audio encoder presets. aargs(ext) picks one by output extension (default AAC 192k).
AAC = ["-c:a", "aac", "-b:a", "192k"]
_AMAP = {".mp3": ["-c:a", "libmp3lame", "-q:a", "2"], ".wav": ["-c:a", "pcm_s16le"], ".flac": ["-c:a", "flac"],
         ".m4a": AAC, ".aac": AAC, ".ogg": ["-c:a", "libvorbis", "-q:a", "5"], ".opus": ["-c:a", "libopus", "-b:a", "128k"],
         ".webm": ["-c:a", "libopus", "-b:a", "128k"]}


def aargs(ext):
    return _AMAP.get(ext, AAC)


# ffmpeg's atempo only accepts 0.5-2.0 per stage, so bigger/smaller factors are expressed as a chain (e.g. 4x = atempo=2,atempo=2).
def atempo(f):
    parts = []
    while f > 2.0:
        parts.append(2.0)
        f /= 2.0
    while f < 0.5:
        parts.append(0.5)
        f /= 0.5
    parts.append(f)
    return ",".join(f"atempo={p:.5f}" for p in parts)


# Ctx = everything a builder needs to know about ONE input file: its path,
#  stem, extension, output folder, a scratch folder (tmp), and probe results.
#  Ctx.out(suffix) invents a non-clashing output name next to the source
#  (name_suffix.ext, then name_suffix_2.ext ...) so nothing is ever overwritten.
class Ctx:
    def __init__(self, src, outdir, tmp):
        p = Path(src)
        self.src = str(p)
        self.stem = p.stem
        self.ext = p.suffix.lower() or ".mp4"
        self.outdir = Path(outdir) if outdir else p.parent
        self.tmp = tmp
        self.info = probe(src)
        self.dur = self.info["dur"]
        self.has_v = self.info["has_v"]
        self.has_a = self.info["has_a"]
        self.w = self.info["w"]
        self.h = self.info["h"]

    def out(self, suffix, ext=None, folder=False):
        ext = "" if folder else (ext or self.ext)
        p = self.outdir / (self.stem + suffix + ext)
        n = 1
        while p.exists():
            n += 1
            p = self.outdir / f"{self.stem}{suffix}_{n}{ext}"
        return str(p)

    def aout_ext(self):
        return self.ext if self.ext in _AMAP else ".m4a"


# job(): the unit of work a builder returns. Keys: args (ffmpeg arguments, without
#  the leading ffmpeg and -i handling), out (expected output path), dur (seconds,
#  used for the progress bar). Extra keywords are kept - e.g. func=<callable>
#  for pure-Python jobs (the full-report writer) and tmpout for multi-step jobs.
def job(args, out=None, dur=0.0, **kw):
    d = dict(args=args, out=out, dur=dur)
    d.update(kw)
    return d


# --------------------------------------------------------------------------- registry
# OPS is the master list of operations. Everything else (the button grids, the
#  option dialogs, the queue, the self-test, the translation checks) is driven by
#  iterating this list - to add a feature you only add one @op function.
#  Each entry: cat (category tab), id (stable key, also used by the queue and
#  favourites), label (button text), fn (the builder), opts (option widgets),
#  kind (what input it accepts), tip (hint text), folder_one (see below).
OPS = []


# O(...) describes one option widget. type is one of:
#    text / int / float / bool / choice / file / files
#  `choices` is for 'choice'; `ftype` filters file pickers (audio, image, sub,
#  lut, video, any).
def O(key, label, type="text", default="", choices=None, ftype="any"):
    return dict(key=key, label=label, type=type, default=default, choices=choices or [], ftype=ftype)


# Append an operation to OPS. kind: video | audio | image | sub | any (used by kind_ok to skip incompatible files in a batch). folder_one: operations that take a whole folder as ONE job (e.g. slideshow, join) instead of one job per file.
def register(cat, id, label, fn, opts=(), kind="video", tip="", folder_one=False):
    OPS.append(dict(cat=cat, id=id, label=label, fn=fn, opts=list(opts), kind=kind, tip=tip, folder_one=folder_one))


# Decorator form of register(): @op(category, id, label, [options], kind=...).
def op(cat, id, label, opts=(), kind="video", tip="", folder_one=False):
    def deco(fn):
        register(cat, id, label, fn, opts, kind, tip, folder_one)
        return fn
    return deco


# --- 6. OPERATIONS ----------------------------------------------------------
#  Every function below follows the same contract:
#      def b_xxx(o, c):   # o = dict of option values (already type-converted)
#          ...            # c = Ctx for the current file
#          return [job(args, out, dur)]
#  Output names come from c.out(...) so originals are never touched.
#  ======================= CATEGORY: VIDEO =======================
# =========================================================================== VIDEO
# Trim: -ss + -t. 'Re-encode' gives an exact cut, 'Copy' is instant but snaps to keyframes. Has live start/end frame preview sliders.
@op("Video", "trim", "Trim / cut by time",
    [O("start", "Start (hh:mm:ss or seconds)", default="0"), O("end", "End (blank = to the end)", default=""),
     O("mode", "Mode", "choice", "Re-encode (exact cut)", ["Re-encode (exact cut)", "Copy (instant, cuts on keyframes)"])],
    tip="Cut out one section of the video.")
def b_trim(o, c):
    s = tsec(o["start"])
    e = tsec(o["end"]) if o["end"] else c.dur
    d = max(0.1, e - s)
    if o["mode"].startswith("Copy"):
        return [job(["-ss", f"{s}", "-i", c.src, "-t", f"{d}", "-c", "copy", "-avoid_negative_ts", "make_zero"], c.out("_trim"), d)]
    return [job(["-ss", f"{s}", "-i", c.src, "-t", f"{d}"] + vcodec() + AAC, c.out("_trim", ".mp4"), d)]


# Join: concat filter over the selected file plus the extra clips; every clip is scaled/padded to the first clip's size, 30 fps, stereo 48 kHz, then re-encoded.
@op("Video", "join", "Join clips into one",
    [O("files", "Clips to append after the selected file", "files")],
    tip="Easiest: leave the Input box empty and add ALL your clips below, in order (Add… or Add folder…). Or select a first clip and the list is appended after it. Clips are scaled to the first one's size.", folder_one=True)
def b_join(o, c):
    files = [c.src] + list(o["files"])
    n = len(files)
    if n < 2:
        raise ValueError("Add at least one more clip to join.")
    W, H = (c.w or 1920), (c.h or 1080)
    W -= W % 2
    H -= H % 2
    args, fc, total = [], [], c.dur
    for i, f in enumerate(files):
        args += ["-i", f]
        if i:
            total += probe(f)["dur"]
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]")
        fc.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]"] + vcodec() + AAC
    return [job(args, c.out("_joined", ".mp4"), total)]


# Split: builds one job per segment (-ss/-t), either re-encoded (exact) or stream-copied (fast); files are named name_part_01, _02 ...
@op("Video", "split", "Split into segments",
    [O("seconds", "Segment length (seconds)", "float", 59), O("mode", "Mode", "choice", "Re-encode (exact)", ["Re-encode (exact)", "Copy (fast)"])],
    tip="Cut a long video into equal parts, e.g. 59 s for Shorts/Reels.")
def b_split(o, c):
    seg = float(o["seconds"])
    n = max(1, math.ceil(c.dur / seg - 1e-6))
    jobs = []
    for i in range(n):
        d = min(seg, c.dur - i * seg)
        if o["mode"].startswith("Copy"):
            a = ["-ss", f"{i * seg}", "-i", c.src, "-t", f"{d}", "-c", "copy", "-avoid_negative_ts", "make_zero"]
            jobs.append(job(a, c.out(f"_part_{i + 1:02d}"), d))
        else:
            a = ["-ss", f"{i * seg}", "-i", c.src, "-t", f"{d}"] + vcodec() + AAC
            jobs.append(job(a, c.out(f"_part_{i + 1:02d}", ".mp4"), d))
    return jobs


# Speed: setpts for video + chained atempo for audio (see atempo()).
@op("Video", "speed", "Change speed (slow-mo / time-lapse)",
    [O("factor", "Speed factor (0.5 = half speed, 4 = 4x faster)", "float", 2.0)])
def b_speed(o, c):
    f = float(o["factor"])
    a = ["-i", c.src, "-vf", f"setpts=PTS/{f}"]
    if c.has_a:
        a += ["-af", atempo(f)]
    return [job(a + vcodec() + AAC, c.out(f"_x{f:g}", ".mp4"), c.dur / f)]


# Reverse: reverse/areverse filters. Needs the whole clip in RAM, hence the tip.
@op("Video", "reverse", "Reverse playback", tip="Loads the whole clip in memory - use on short clips.")
def b_reverse(o, c):
    a = ["-i", c.src, "-vf", "reverse"]
    if c.has_a:
        a += ["-af", "areverse"]
    return [job(a + vcodec() + AAC, c.out("_reversed", ".mp4"), c.dur)]


# Resize presets: label -> target height. RES_CUSTOM is the menu entry that switches to the width/height boxes (-2 = keep aspect, even number).
RES = {"144p": 144, "240p": 240, "320p": 320, "360p": 360, "480p": 480, "540p": 540, "720p (HD)": 720, "1080p (Full HD)": 1080,
       "1440p (2K)": 1440, "2160p (4K)": 2160, "4320p (8K)": 4320}
RES_CUSTOM = "Custom (use the width / height boxes)"


# Resize: presets name the SHORT side (so vertical videos work too); 'never enlarge' leaves smaller sources at their size; Custom uses the width/height boxes (-2 = auto, keeps aspect).
@op("Video", "resize", "Resize",
    [O("preset", "Size", "choice", "720p (HD)", list(RES) + [RES_CUSTOM]),
     O("noup", "Never enlarge (keep the original if it is already smaller)", "bool", True),
     O("width", "Custom width (-2 = auto)", default="1280"), O("height", "Custom height (-2 = auto)", default="-2")],
    tip="Pick a size from the list (from 144p up to 8K). The number is the short side, so it also works for vertical videos. "
        "For an exact size choose Custom and use the boxes.")
def b_resize(o, c):
    if o["preset"] == RES_CUSTOM:
        return [job(["-i", c.src, "-vf", f"scale={o['width']}:{o['height']}"] + vcodec() + AAC, c.out(f"_{o['width']}w", ".mp4"), c.dur)]
    n = RES[o["preset"]]
    tag = "_" + o["preset"].split(" ")[0]
    short = min(c.w, c.h) if c.w and c.h else 0
    if o["noup"] and short and n >= short:
        vf, tag = "scale=trunc(iw/2)*2:trunc(ih/2)*2", "_orig"
    elif c.h > c.w > 0:
        vf = f"scale={n}:-2"
    else:
        vf = f"scale=-2:{n}"
    return [job(["-i", c.src, "-vf", vf] + vcodec() + AAC, c.out(tag, ".mp4"), c.dur)]


# Builds the crop=w:h:x:y filter from the option dict - shared by the Crop op and the live preview so both agree.
def crop_filter(o):
    e = lambda v: v.replace(",", "\\,")  # noqa: E731  commas separate filters, so escape them in expressions
    return f"crop={e(o['w'])}:{e(o['h'])}:{e(o['x'])}:{e(o['y'])}"


# Crop: has the crop-result live preview with draggable x/y/width/height sliders.
@op("Video", "crop", "Crop",
    [O("w", "Width", default="min(iw,ih)"), O("h", "Height", default="min(iw,ih)"),
     O("x", "X offset", default="(iw-ow)/2"), O("y", "Y offset", default="(ih-oh)/2")],
    tip="Defaults crop a centred square. Expressions like iw/2 work.")
def b_crop(o, c):
    return [job(["-i", c.src, "-vf", crop_filter(o)] + vcodec() + AAC, c.out("_crop", ".mp4"), c.dur)]


# Rotate: transpose=1 / transpose=2 for 90 degrees either way, hflip+vflip for 180.
@op("Video", "rotate", "Rotate",
    [O("how", "Rotation", "choice", "90 clockwise", ["90 clockwise", "90 counter-clockwise", "180"])])
def b_rotate(o, c):
    vf = {"90 clockwise": "transpose=1", "90 counter-clockwise": "transpose=2", "180": "hflip,vflip"}[o["how"]]
    return [job(["-i", c.src, "-vf", vf] + vcodec() + AAC, c.out("_rot", ".mp4"), c.dur)]


# Flip/mirror: hflip / vflip.
@op("Video", "flip", "Flip / mirror",
    [O("how", "Direction", "choice", "Horizontal (mirror)", ["Horizontal (mirror)", "Vertical", "Both"])])
def b_flip(o, c):
    vf = {"Horizontal (mirror)": "hflip", "Vertical": "vflip", "Both": "hflip,vflip"}[o["how"]]
    return [job(["-i", c.src, "-vf", vf] + vcodec() + AAC, c.out("_flip", ".mp4"), c.dur)]


# Aspect ratio for TikTok/Reels/Shorts etc: choose blurred background, solid colour bars, or crop-to-fill (all via one filter_complex).
@op("Video", "aspect", "Change aspect ratio (TikTok / Reels / Shorts)",
    [O("target", "Target", "choice", "9:16 vertical (1080x1920)", ["9:16 vertical (1080x1920)", "1:1 square (1080x1080)", "4:5 portrait (1080x1350)", "16:9 landscape (1920x1080)"]),
     O("fill", "Fill the bars with", "choice", "Blurred background", ["Blurred background", "Solid colour", "Crop to fill"]),
     O("color", "Colour (for solid)", default="black")],
    tip="Fits the video inside the new frame without stretching.")
def b_aspect(o, c):
    W, H = map(int, re.search(r"\((\d+)x(\d+)\)", o["target"]).groups())
    f = o["fill"]
    if f.startswith("Blurred"):
        fc = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:5[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    elif f.startswith("Solid"):
        fc = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:{o['color']},setsar=1[v]"
    else:
        fc = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v]"
    tag = o["target"].split(" ")[0].replace(":", "x")
    return [job(["-i", c.src, "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"] + vcodec() + AAC, c.out(f"_{tag}", ".mp4"), c.dur)]


# Loop: -stream_loop repeats the input N-1 extra times; the result is re-encoded to MP4.
@op("Video", "loop", "Loop the clip", [O("times", "Play it this many times in total", "int", 3)])
def b_loop(o, c):
    t = max(2, int(o["times"]))
    return [job(["-stream_loop", str(t - 1), "-i", c.src] + vcodec() + AAC, c.out(f"_loop{t}", ".mp4"), c.dur * t)]


# Stabilise: two ffmpeg passes (vidstabdetect then vidstabtransform). Needs libvidstab; the self-test skips it if the build lacks it.
@op("Video", "stabilise", "Stabilise shaky footage", [O("shake", "Shakiness 1-10", "int", 5), O("smooth", "Smoothing frames", "int", 15)],
    tip="Two passes with the vidstab filter (needs an ffmpeg build with libvidstab, e.g. the Gyan full build).")
def b_stab(o, c):
    d1 = job(["-i", c.src, "-vf", f"vidstabdetect=shakiness={o['shake']}:result=transforms.trf", "-an", "-f", "null", os.devnull],
             None, c.dur, cwd=c.tmp)
    d2 = job(["-i", c.src, "-vf", f"vidstabtransform=input=transforms.trf:smoothing={o['smooth']},unsharp=5:5:0.8:3:3:0.4"] + vcodec() + AAC,
             c.out("_stable", ".mp4"), c.dur, cwd=c.tmp)
    return [d1, d2]


# ======================= CATEGORY: FORMAT & QUALITY =======================
# =========================================================================== FORMAT & QUALITY
# Convert: re-encode to mp4/mov/mkv (H.264+AAC), webm (VP9+Opus), avi (mpeg4+MP3) or an animated gif (12 fps, 480 px, optimised palette).
@op("Format & Quality", "convert", "Convert video format",
    [O("target", "Convert to", "choice", "mp4", ["mp4", "mov", "mkv", "webm", "avi", "gif"]), O("crf", "Quality (CRF, lower = better)", "int", 20)],
    tip="Re-encodes. For a lossless container swap use 'Re-wrap'.")
def b_convert(o, c):
    t, crf = o["target"], int(o["crf"])
    if t in ("mp4", "mov", "mkv"):
        a = ["-i", c.src] + vcodec(crf) + AAC
    elif t == "webm":
        a = ["-i", c.src, "-c:v", "libvpx-vp9", "-crf", str(min(63, crf + 10)), "-b:v", "0", "-c:a", "libopus", "-b:a", "128k"]
    elif t == "avi":
        a = ["-i", c.src, "-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-q:a", "3"]
    else:
        a = ["-i", c.src, "-vf", "fps=12,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", "-an"]
    return [job(a, c.out("_conv", "." + t), c.dur)]


# Remux: -c copy into a new container - instant, no quality change.
@op("Format & Quality", "remux", "Re-wrap container (no quality loss)",
    [O("target", "New container", "choice", "mp4", ["mp4", "mkv", "mov"])],
    tip="Copies the streams untouched - takes seconds. Works when codecs fit the new container (H.264 + AAC does).")
def b_remux(o, c):
    t = o["target"]
    a = ["-i", c.src, "-map", "0:v?", "-map", "0:a?", "-c", "copy"]
    if t in ("mp4", "mov"):
        a += ["-movflags", "+faststart"]
    return [job(a, c.out("_remux", "." + t), c.dur)]


# Fast-start: moves the MP4 'moov' atom to the front so web players start immediately (-movflags +faststart, stream copy).
@op("Format & Quality", "faststart", "Prepare for web playback (fast-start MP4)")
def b_faststart(o, c):
    return [job(["-i", c.src, "-c", "copy", "-movflags", "+faststart"], c.out("_web", ".mp4"), c.dur)]


# Social presets: platform name -> (width, height, fps, max-seconds hint).
SOCIAL = {  # name -> (width, height, fps or 0 = keep, max seconds hint)
    "TikTok (9:16 1080x1920)": (1080, 1920, 30, "tiktok"),
    "Instagram Reels (9:16 1080x1920)": (1080, 1920, 30, "reels"),
    "YouTube Shorts (9:16 1080x1920)": (1080, 1920, 30, "shorts"),
    "YouTube (16:9 1080p)": (1920, 1080, 0, "youtube"),
    "X / Twitter (16:9 1280x720)": (1280, 720, 30, "x"),
    "Instagram feed (4:5 1080x1350)": (1080, 1350, 30, "feed"),
}


# Social export: fits the video to the platform's frame size/fps (blurred background, black bars, or crop-to-fill), H.264 high profile + AAC 128k, fast-start.
@op("Format & Quality", "social", "Export for social media (TikTok / Reels / Shorts / YouTube / X)",
    [O("platform", "Platform", "choice", "TikTok (9:16 1080x1920)", list(SOCIAL)),
     O("fill", "If the shape does not match", "choice", "Blurred background", ["Blurred background", "Black bars", "Crop to fill"])],
    tip="One click: right size, H.264 + AAC, 8-bit, fast-start MP4 - accepted by the platform without re-processing trouble.")
def b_social(o, c):
    W, H, fps, tag = SOCIAL[o["platform"]]
    f = o["fill"]
    tail = f",fps={fps}" if fps else ""
    if f.startswith("Blurred"):
        fc = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:5[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{tail}[v]")
    elif f.startswith("Black"):
        fc = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1{tail}[v]"
    else:
        fc = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1{tail}[v]"
    a = ["-i", c.src, "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"] + vcodec(21)
    if not STATE["gpu"]:
        a += ["-profile:v", "high"]
    a += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart"]
    return [job(a, c.out(f"_{tag}", ".mp4"), c.dur)]


# Compress to a target size: bitrate = target MB / duration (95% budget, minus audio, capped at the source's own bitrate), then TWO x264 passes - pass 1 analyses to null, pass 2 writes the file.
@op("Format & Quality", "compress", "Compress to a target size",
    [O("preset", "Target", "choice", "Custom (use MB below)",
       ["Custom (use MB below)", "Discord 25 MB", "WhatsApp 16 MB", "Email 10 MB", "Telegram 50 MB"]),
     O("mb", "Custom size (MB)", "float", 25)],
    tip="Two-pass encode that aims for the chosen file size.")
def b_compress(o, c):
    mb = {"Discord 25 MB": 25, "WhatsApp 16 MB": 16, "Email 10 MB": 10, "Telegram 50 MB": 50}.get(o["preset"], float(o["mb"]))
    if c.dur <= 0:
        raise ValueError("Cannot read the duration of this file.")
    kbps = max(100, int(mb * 8192 * 0.95 / c.dur) - 128)
    if c.info["bitrate"]:
        kbps = min(kbps, int(c.info["bitrate"] / 1000))  # no point exceeding the source's own bitrate
    log = os.path.join(c.tmp, "pass")
    p1 = job(["-i", c.src, "-map", "0:v:0", "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "medium", "-b:v", f"{kbps}k", "-pass", "1", "-passlogfile", log, "-an", "-f", "null", os.devnull], None, c.dur)
    p2 = job(["-i", c.src, "-map", "0:v:0", "-map", "0:a?", "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "medium", "-b:v", f"{kbps}k", "-pass", "2", "-passlogfile", log,
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"], c.out(f"_{int(mb)}MB", ".mp4"), c.dur)
    return [p1, p2]


# Recode: pick codec (H.264 / H.265 / VP9), CRF quality, frame rate, height and audio bitrate independently; the scale/fps filters are only added when non-zero.
@op("Format & Quality", "recode", "Change codec / frame rate / resolution",
    [O("codec", "Video codec", "choice", "H.264", ["H.264", "H.265 (HEVC)", "VP9"]),
     O("crf", "Quality (CRF)", "int", 22), O("fps", "Frame rate (0 = keep)", "int", 0),
     O("height", "Height in pixels (0 = keep)", "int", 0), O("abr", "Audio bitrate (kbps)", "int", 160)])
def b_recode(o, c):
    crf = int(o["crf"])
    vf = []
    if int(o["height"]):
        vf.append(f"scale=-2:{int(o['height'])}")
    if int(o["fps"]):
        vf.append(f"fps={int(o['fps'])}")
    a = ["-i", c.src] + (["-vf", ",".join(vf)] if vf else [])
    if o["codec"] == "H.264":
        a += vcodec(crf) + ["-c:a", "aac", "-b:a", f"{o['abr']}k"]
        ext = ".mp4"
    elif o["codec"].startswith("H.265"):
        a += ["-c:v", "libx265", "-preset", "veryfast", "-crf", str(crf), "-tag:v", "hvc1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{o['abr']}k"]
        ext = ".mp4"
    else:
        a += ["-c:v", "libvpx-vp9", "-crf", str(min(63, crf + 8)), "-b:v", "0", "-c:a", "libopus", "-b:a", f"{o['abr']}k"]
        ext = ".webm"
    return [job(a, c.out("_recode", ext), c.dur)]


# ======================= CATEGORY: AUDIO =======================
# =========================================================================== AUDIO
# Shared helper: apply an audio filter chain `af` and encode in the right format for the source (used by volume, denoise, eq, fade...).
def _afilt(c, suffix, af):
    if c.has_v:
        ext = c.ext if c.ext in (".mp4", ".mkv", ".mov", ".m4v") else ".mkv"
        args = ["-i", c.src, "-af", af, "-c:v", "copy"] + aargs(ext)
    else:
        ext = c.aout_ext()
        args = ["-i", c.src, "-vn", "-af", af] + aargs(ext)
    return [job(args, c.out(suffix, ext), c.dur)]


# 'Extract audio from video' and 'Convert audio format' share this builder; they are registered with two register() calls just below.
def b_extract(o, c):
    t = o["format"]
    return [job(["-i", c.src, "-vn"] + aargs("." + t), c.out("_audio", "." + t), c.dur)]


# Audio formats offered by extract/convert.
_FORMATS = ["mp3", "wav", "m4a", "flac", "ogg", "opus"]
register("Audio", "extract", "Extract audio from video", b_extract, [O("format", "Format", "choice", "mp3", _FORMATS)], kind="video")
register("Audio", "aconvert", "Convert audio format", b_extract, [O("format", "Format", "choice", "mp3", _FORMATS)], kind="audio")


# Loudness normalise: EBU R128 loudnorm filter, target in LUFS.
@op("Audio", "loudnorm", "Normalise loudness",
    [O("target", "Target", "choice", "-14 LUFS  YouTube / Spotify", ["-14 LUFS  YouTube / Spotify", "-16 LUFS  Podcast / Apple", "-23 LUFS  Broadcast (EBU R128)"])],
    kind="any")
def b_loudnorm(o, c):
    i = re.match(r"(-\d+)", o["target"]).group(1)
    return _afilt(c, "_norm", f"loudnorm=I={i}:TP=-1.5:LRA=11")


# Remove silence: silenceremove with start/stop thresholds.
@op("Audio", "silence", "Remove silence",
    [O("db", "Silence threshold (dB)", "int", -45), O("secs", "Minimum silence to cut (seconds)", "float", 0.5)],
    kind="any", tip="Output is audio only.")
def b_silence(o, c):
    af = (f"silenceremove=start_periods=1:start_threshold={o['db']}dB:"
          f"stop_periods=-1:stop_duration={o['secs']}:stop_threshold={o['db']}dB")
    ext = ".mp3" if c.has_v else c.aout_ext()
    return [job(["-i", c.src, "-vn", "-af", af] + aargs(ext), c.out("_nosilence", ext), c.dur)]


# Trim audio by time.
@op("Audio", "atrim", "Trim audio", [O("start", "Start", default="0"), O("end", "End (blank = to the end)", default="")], kind="audio")
def b_atrim(o, c):
    s = tsec(o["start"])
    e = tsec(o["end"]) if o["end"] else c.dur
    ext = c.aout_ext()
    return [job(["-ss", f"{s}", "-i", c.src, "-t", f"{max(0.1, e - s)}", "-vn"] + aargs(ext), c.out("_trim", ext), e - s)]


# Audio fade in/out; fade-out start is computed from the file duration.
@op("Audio", "fade", "Fade in / out",
    [O("fin", "Fade in (seconds)", "float", 2), O("fout", "Fade out (seconds)", "float", 2), O("video", "Fade the picture too", "bool", False)], kind="any")
def b_fade(o, c):
    fin, fout = float(o["fin"]), float(o["fout"])
    af = f"afade=t=in:st=0:d={fin},afade=t=out:st={max(0, c.dur - fout)}:d={fout}"
    if c.has_v and o["video"]:
        vf = f"fade=t=in:st=0:d={fin},fade=t=out:st={max(0, c.dur - fout)}:d={fout}"
        return [job(["-i", c.src, "-vf", vf, "-af", af] + vcodec() + AAC, c.out("_fade", ".mp4"), c.dur)]
    return _afilt(c, "_fade", af)


# Gain in dB.
@op("Audio", "volume", "Change volume", [O("db", "Gain in dB (+6 = louder, -6 = quieter)", "float", 6)], kind="any")
def b_volume(o, c):
    return _afilt(c, "_vol", f"volume={o['db']}dB")


# Change speed without changing pitch (atempo chain).
@op("Audio", "tempo", "Change audio speed (keeps pitch)", [O("f", "Speed factor", "float", 1.25)], kind="audio")
def b_tempo(o, c):
    return _afilt(c, "_tempo", atempo(float(o["f"])))


# Pitch shift in semitones: asetrate to move pitch, then atempo to restore speed.
@op("Audio", "pitch", "Change pitch", [O("st", "Semitones (+/-12)", "float", 2)], kind="audio")
def b_pitch(o, c):
    sr = int((c.info["a"] or {}).get("sample_rate", 44100))
    r = 2 ** (float(o["st"]) / 12)
    return _afilt(c, "_pitch", f"asetrate={sr * r:.0f},aresample={sr},{atempo(1 / r)}")


# afftdn denoiser; noise floor in dB.
@op("Audio", "denoise", "Denoise", [O("nf", "Noise floor (dB, -20 gentle ... -40 strong)", "int", -25)], kind="any")
def b_denoise(o, c):
    return _afilt(c, "_clean", f"afftdn=nf={o['nf']}")


# High-pass / low-pass filters at the given cut-off frequencies (0 = off).
@op("Audio", "eq", "High-pass / low-pass filter",
    [O("hp", "Cut below (Hz, 0 = off)", "int", 80), O("lp", "Cut above (Hz, 0 = off)", "int", 0)], kind="any")
def b_eq(o, c):
    parts = []
    if int(o["hp"]):
        parts.append(f"highpass=f={o['hp']}")
    if int(o["lp"]):
        parts.append(f"lowpass=f={o['lp']}")
    if not parts:
        raise ValueError("Set at least one frequency.")
    return _afilt(c, "_eq", ",".join(parts))


# Shared by 'Add music / replace audio' and 'Add narration': mixes a second audio file under the original (amix with weights) or replaces the original, optionally looping the new audio to fit.
def b_mix(o, c):
    new = o["file"]
    if not new:
        raise ValueError("Choose the audio file to add.")
    loop = ["-stream_loop", "-1"] if o["loop"] else []
    ov, nv = float(o["ov"]), float(o["nv"])
    ext = c.ext if c.ext in (".mp4", ".mkv", ".mov", ".m4v") else ".mp4"
    if o["mode"].startswith("Replace") or not c.has_a:
        fc = f"[1:a]volume={nv}[a]"
    else:
        fc = f"[0:a]volume={ov}[o];[1:a]volume={nv}[n];[o][n]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    a = ["-i", c.src] + loop + ["-i", new, "-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy"] + AAC + ["-shortest"]
    return [job(a, c.out("_mixed", ext), c.dur)]


# Choices for what to do with the existing audio.
_MIXCH = ["Mix under the original audio", "Replace the original audio"]
register("Audio", "mix", "Add music / replace audio", b_mix,
         [O("file", "Audio file", "file", ftype="audio"), O("mode", "Mode", "choice", _MIXCH[0], _MIXCH),
          O("ov", "Original volume (0-2)", "float", 1.0), O("nv", "New audio volume (0-2)", "float", 0.25), O("loop", "Loop the new audio", "bool", True)],
         tip="Background music under the video's own sound, or swap the sound entirely.")
register("Audio", "narration", "Add narration / voiceover", b_mix,
         [O("file", "Narration audio (e.g. from your TTS foundry)", "file", ftype="audio"), O("mode", "Mode", "choice", _MIXCH[0], _MIXCH),
          O("ov", "Original volume (0-2)", "float", 0.3), O("nv", "Narration volume (0-2)", "float", 1.0), O("loop", "Loop the narration", "bool", False)],
         tip="Lays a voiceover over the video, ducking the original sound.")


# Channels: downmix to mono or split left/right into two files.
@op("Audio", "channels", "Mono / split stereo channels",
    [O("how", "Action", "choice", "Convert to mono", ["Convert to mono", "Split into left + right files"])], kind="any")
def b_channels(o, c):
    ext = c.aout_ext() if not c.has_v else ".wav"
    if o["how"].startswith("Convert"):
        if c.has_v:
            return [job(["-i", c.src, "-c:v", "copy", "-ac", "1"] + AAC, c.out("_mono", c.ext if c.ext in (".mp4", ".mkv", ".mov") else ".mkv"), c.dur)]
        return [job(["-i", c.src, "-vn", "-ac", "1"] + aargs(ext), c.out("_mono", ext), c.dur)]
    l, r = c.out("_left", ext), c.out("_right", ext)
    return [job(["-i", c.src, "-filter_complex", "channelsplit=channel_layout=stereo[l][r]", "-map", "[l]"] + aargs(ext) + [l, "-map", "[r]"] + aargs(ext),
                r, c.dur, outs=[l])]


# ======================= CATEGORY: IMAGES & FRAMES =======================
# =========================================================================== IMAGES & FRAMES
# Thumbnail: one frame at a chosen time (live preview supported).
@op("Images & Frames", "thumb", "Grab a thumbnail / single frame", [O("t", "Time (hh:mm:ss or seconds)", default="1")])
def b_thumb(o, c):
    return [job(["-ss", f"{tsec(o['t'])}", "-i", c.src, "-frames:v", "1", "-q:v", "2"], c.out("_thumb", ".jpg"), 0)]


# Frames: image sequence into a new folder, one frame every N seconds.
@op("Images & Frames", "frames", "Extract frames to a folder", [O("every", "One frame every N seconds", "float", 1)])
def b_frames(o, c):
    d = c.out("_frames", folder=True)
    return [job(["-i", c.src, "-vf", f"fps=1/{float(o['every'])}", os.path.join(d, "frame_%04d.png")], d, c.dur, mkdir=d, raw_out=True)]


# Contact sheet: tile filter builds a grid of evenly spaced frames in one image.
@op("Images & Frames", "contact", "Contact sheet (grid of frames)",
    [O("cols", "Columns", "int", 5), O("rows", "Rows", "int", 4), O("tw", "Thumbnail width", "int", 320)])
def b_contact(o, c):
    cols, rows = int(o["cols"]), int(o["rows"])
    iv = max(0.1, c.dur / (cols * rows))
    return [job(["-i", c.src, "-vf", f"fps=1/{iv:.3f},scale={o['tw']}:-1,tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1", "-q:v", "2"],
                c.out("_contact", ".jpg"), c.dur)]


# GIF: palettegen + paletteuse for good colours at small size.
@op("Images & Frames", "gif", "Make a GIF (optimised palette)",
    [O("start", "Start", default="0"), O("dur", "Length (seconds)", "float", 5), O("fps", "Frames per second", "int", 12), O("w", "Width", "int", 480)])
def b_gif(o, c):
    vf = f"fps={o['fps']},scale={o['w']}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    return [job(["-ss", f"{tsec(o['start'])}", "-t", f"{o['dur']}", "-i", c.src, "-vf", vf, "-loop", "0"], c.out("_anim", ".gif"), float(o["dur"]))]


# Image convert/resize (kind=image): jpg / png / webp with optional width (0 = keep); jpg uses -q:v 2.
@op("Images & Frames", "imgconv", "Convert / resize image",
    [O("fmt", "Format", "choice", "jpg", ["jpg", "png", "webp"]), O("w", "Width (0 = keep)", "int", 0)], kind="image",
    tip="Pick a folder with 'Batch folder' to convert every image in it.")
def b_imgconv(o, c):
    a = ["-i", c.src]
    if int(o["w"]):
        a += ["-vf", f"scale={o['w']}:-2"]
    if o["fmt"] == "jpg":
        a += ["-q:v", "2"]
    return [job(a + ["-frames:v", "1"], c.out("_conv", "." + o["fmt"]), 0)]


# ======================= CATEGORY: ANALYSIS =======================
#  These produce reports/images rather than edited media. The detectors run ffmpeg to the null muxer with info=True; run_job then shows only the log lines matching the job's `show` regex (or the last `tail` lines) in the app's log box. Nothing is written to disk.
# =========================================================================== ANALYSIS
# Builds the human-readable full ffprobe report text (format, every stream, tags, chapters).
def _probe_text(c):
    """Readable summary of a file (what you usually look for first), then the complete ffprobe data."""
    raw = c.info["raw"] or {}
    fm = raw.get("format", {})
    L = ["FULL FILE REPORT", "=" * 60, f"File:      {c.src}"]
    try:
        L.append(f"Size:      {os.path.getsize(c.src) / 1048576:.2f} MB")
    except OSError:
        pass
    if fm.get("format_long_name"):
        L.append(f"Container: {fm['format_long_name']}")
    if c.dur:
        L.append(f"Duration:  {fmt_ms(c.dur)}  ({c.dur:.3f} s)")
    if fm.get("bit_rate"):
        L.append(f"Bit rate:  {int(float(fm['bit_rate'])) // 1000} kb/s overall")
    for st in raw.get("streams", []):
        kind = st.get("codec_type", "?")
        line = f"Stream #{st.get('index')}: {kind}, {st.get('codec_name', '?')}"
        if kind == "video":
            line += f", {st.get('width')}x{st.get('height')}, {st.get('r_frame_rate', '?')} fps"
            if st.get("pix_fmt"):
                line += f", {st['pix_fmt']}"
        elif kind == "audio":
            line += f", {st.get('sample_rate', '?')} Hz, {st.get('channels', '?')} ch"
        if st.get("bit_rate"):
            line += f", {int(float(st['bit_rate'])) // 1000} kb/s"
        L.append(line)
    tags = fm.get("tags") or {}
    if tags:
        L.append("Tags:     " + "; ".join(f"{k}={v}" for k, v in tags.items()))
    L += ["", "COMPLETE ffprobe DATA", "=" * 60, json.dumps(raw, indent=2, ensure_ascii=False), ""]
    return "\n".join(L)


# Returns a function(log) that writes the report to the .txt file; stored in the job as func= so run_job calls it instead of ffmpeg. After the job, the GUI opens the file in Notepad on Windows.
def _probe_report(c, path):
    def f(log):
        text = _probe_text(c)
        Path(path).write_text(text, encoding="utf-8")
        log(text)
        log(f"\nSaved: {path}")
        if not os.environ.get("OM_NO_OPEN"):
            try:
                # show the report straight away: Notepad on Windows, TextEdit on macOS, default editor on Linux
                subprocess.Popen(["notepad.exe", path] if IS_WIN else (["open", "-t", path] if IS_MAC else ["xdg-open", path]))
            except OSError:
                pass
        return 0
    return f


# Full file report: writes 'full report <name>.txt' in the output folder (never overwrites - adds (2), (3)...).
@op("Analysis", "probe", "Full file report (ffprobe)", kind="any")
def b_probe(o, c):
    p = c.outdir / f"full report {c.stem}.txt"
    n = 1
    while p.exists():
        n += 1
        p = c.outdir / f"full report {c.stem} ({n}).txt"
    return [job([], str(p), 0, func=_probe_report(c, str(p)))]


# Detect silences (silencedetect filter); matching 'silence_' lines are shown in the log.
@op("Analysis", "silencedetect", "Detect silences", [O("db", "Threshold (dB)", "int", -35), O("d", "Min length (s)", "float", 0.5)], kind="any")
def b_sd(o, c):
    return [job(["-i", c.src, "-af", f"silencedetect=noise={o['db']}dB:d={o['d']}", "-vn", "-f", "null", os.devnull], None, c.dur, info=True, show=r"silence_")]


# Detect black frames.
@op("Analysis", "blackdetect", "Detect black frames", [O("d", "Min length (s)", "float", 0.1)])
def b_bd(o, c):
    return [job(["-i", c.src, "-vf", f"blackdetect=d={o['d']}:pic_th=0.98", "-an", "-f", "null", os.devnull], None, c.dur, info=True, show=r"black_")]


# Detect frozen frames.
@op("Analysis", "freezedetect", "Detect frozen frames", [O("d", "Min freeze (s)", "float", 2)])
def b_fd(o, c):
    return [job(["-i", c.src, "-vf", f"freezedetect=n=-60dB:d={o['d']}", "-an", "-f", "null", os.devnull], None, c.dur, info=True, show=r"freeze_")]


# Scene-change detection via select='gt(scene,T)',showinfo; the log lines containing pts_time are the cut times.
@op("Analysis", "scenes", "Detect scene changes", [O("th", "Sensitivity 0.1-0.9 (lower = more cuts)", "float", 0.4)])
def b_sc(o, c):
    return [job(["-i", c.src, "-vf", f"select='gt(scene,{o['th']})',showinfo", "-an", "-f", "null", os.devnull], None, c.dur, info=True, show=r"pts_time:[\d.]+")]


# Loudness measurement (ebur128) - prints integrated LUFS etc.
@op("Analysis", "loudness", "Measure loudness (LUFS)", kind="any")
def b_ld(o, c):
    return [job(["-i", c.src, "-af", "ebur128=peak=true", "-vn", "-f", "null", os.devnull], None, c.dur, info=True, tail=14)]


# Waveform PNG (showwavespic).
@op("Analysis", "wave", "Waveform image", kind="any")
def b_wave(o, c):
    return [job(["-i", c.src, "-filter_complex", "showwavespic=s=1600x400:colors=#3b82f6", "-frames:v", "1"], c.out("_wave", ".png"), 0)]


# Spectrogram PNG (showspectrumpic).
@op("Analysis", "spectro", "Spectrogram image", kind="any")
def b_spec(o, c):
    return [job(["-i", c.src, "-lavfi", "showspectrumpic=s=1600x800", "-frames:v", "1"], c.out("_spectrogram", ".png"), 0)]


# Strip metadata with -map_metadata -1 and stream copy.
@op("Analysis", "stripmeta", "Strip metadata (no re-encode)", kind="any")
def b_strip(o, c):
    return [job(["-i", c.src, "-map", "0", "-map_metadata", "-1", "-c", "copy"], c.out("_clean"), c.dur)]


# ======================= CATEGORY: STREAMING =======================
# =========================================================================== STREAMING
# HLS: writes an .m3u8 playlist plus .ts segments into a new folder (folder output).
@op("Streaming", "hls", "Create HLS stream (m3u8 + segments)", [O("seg", "Segment length (s)", "int", 6)])
def b_hls(o, c):
    d = c.out("_hls", folder=True)
    return [job(["-i", c.src] + vcodec() + AAC + ["-hls_time", str(o["seg"]), "-hls_playlist_type", "vod",
                 "-hls_segment_filename", os.path.join(d, "seg_%03d.ts"), os.path.join(d, "index.m3u8")], d, c.dur, mkdir=d, raw_out=True)]


# ======================= VIDEO (extra ops added later; they still appear on the Video tab) =======================
# =========================================================================== VIDEO (extra)
# Freeze frame: a MULTI-STEP op - cut before/at/after the freeze point into temp files (tmpout), hold one frame for N seconds, then concat the pieces into the final file.
@op("Video", "freeze", "Freeze a frame",
    [O("t", "Freeze at (hh:mm:ss or seconds)", default="2"), O("d", "Hold for (seconds)", "float", 3)])
def b_freeze(o, c):
    T, D = tsec(o["t"]), float(o["d"])
    if not (0 < T < c.dur):
        raise ValueError("The freeze time must be inside the video.")
    fps = c.info["fps"] or 30
    fr = f"{fps:.3f}"
    tmp = c.tmp
    a_ = os.path.join(tmp, "a.mp4")
    f_ = os.path.join(tmp, "f.mp4")
    b_ = os.path.join(tmp, "b.mp4")
    png = os.path.join(tmp, "frame.png")
    aud = ["-ar", "48000", "-ac", "2"] + AAC if c.has_a else ["-an"]
    jobs = [
        job(["-i", c.src, "-t", f"{T}", "-r", fr] + vcodec() + aud, a_, T, tmpout=True),
        job(["-ss", f"{T}", "-i", c.src, "-frames:v", "1"], png, 0, tmpout=True),
    ]
    still = ["-loop", "1", "-framerate", fr, "-i", png]
    if c.has_a:
        still += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    jobs.append(job(still + ["-t", f"{D}", "-r", fr] + vcodec() + aud, f_, D, tmpout=True))
    jobs.append(job(["-ss", f"{T}", "-i", c.src, "-r", fr] + vcodec() + aud, b_, c.dur - T, tmpout=True))
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for p in (a_, f_, b_):
            fh.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
    jobs.append(job(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy"], c.out("_freeze", ".mp4"), c.dur + D))
    return jobs


# ======================= CATEGORY: COMBINE =======================
# =========================================================================== COMBINE
# Corner/centre presets for overlays; _xy(pos, margin) converts a preset name to ffmpeg overlay x:y expressions.
_POS = ["Bottom-right", "Bottom-left", "Top-right", "Top-left", "Centre"]


def _xy(pos, m):
    return {"Bottom-right": (f"W-w-{m}", f"H-h-{m}"), "Bottom-left": (f"{m}", f"H-h-{m}"),
            "Top-right": (f"W-w-{m}", f"{m}"), "Top-left": (f"{m}", f"{m}"), "Centre": ("(W-w)/2", "(H-h)/2")}[pos]


# Logo/watermark overlay with size (% of video width), opacity and corner.
@op("Combine", "watermark", "Add logo / watermark",
    [O("img", "Logo image (PNG works best)", "file", ftype="image"), O("pos", "Position", "choice", "Bottom-right", _POS),
     O("size", "Logo width (% of video width)", "int", 15), O("opacity", "Opacity (0.1-1)", "float", 0.8), O("margin", "Margin (pixels)", "int", 24)])
def b_watermark(o, c):
    if not o["img"]:
        raise ValueError("Choose a logo image.")
    x, y = _xy(o["pos"], o["margin"])
    wpx = max(16, int((c.w or 1280) * int(o["size"]) / 100))
    fc = f"[1:v]scale={wpx}:-1,format=rgba,colorchannelmixer=aa={o['opacity']}[wm];[0:v][wm]overlay={x}:{y}[v]"
    return [job(["-i", c.src, "-i", o["img"], "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"] + vcodec() + AAC, c.out("_logo", ".mp4"), c.dur)]


# Picture-in-picture: a second video scaled and overlaid on the main one.
@op("Combine", "pip", "Picture-in-picture (small video over big)",
    [O("vid", "Small video", "file", ftype="video"), O("pos", "Position", "choice", "Bottom-right", _POS),
     O("size", "Small video width (% of main)", "int", 28), O("margin", "Margin (pixels)", "int", 24)])
def b_pip(o, c):
    if not o["vid"]:
        raise ValueError("Choose the small video.")
    x, y = _xy(o["pos"], o["margin"])
    wpx = max(32, int((c.w or 1280) * int(o["size"]) / 100)) // 2 * 2
    fc = f"[1:v]scale={wpx}:-2[p];[0:v][p]overlay={x}:{y}:eof_action=pass[v]"
    return [job(["-i", c.src, "-i", o["vid"], "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"] + vcodec() + AAC, c.out("_pip", ".mp4"), c.dur)]


# Grid: hstack/vstack/xstack to put several videos side by side.
@op("Combine", "grid", "Side-by-side / grid of videos",
    [O("files", "Other videos to add", "files"), O("layout", "Layout", "choice", "Side by side", ["Side by side", "Stacked", "Grid (2 columns)"]),
     O("cw", "Width of each cell", "int", 640)],
    tip="Leave the Input box empty and add all your clips below, in order - or select the first clip and add the rest. Audio comes from the first video.", folder_one=True)
def b_grid(o, c):
    files = [c.src] + list(o["files"])
    n = len(files)
    if n < 2:
        raise ValueError("Add at least one more video.")
    W = int(o["cw"]) // 2 * 2
    H = int(W * 9 / 16) // 2 * 2
    args, fc = [], []
    for i, f in enumerate(files):
        args += ["-i", f]
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
    ins = "".join(f"[v{i}]" for i in range(n))
    if o["layout"] == "Side by side":
        fc.append(f"{ins}hstack=inputs={n}:shortest=1[v]")
    elif o["layout"] == "Stacked":
        fc.append(f"{ins}vstack=inputs={n}:shortest=1[v]")
    else:
        lay = "|".join(f"{'0' if i % 2 == 0 else 'w0'}_{'0' if i // 2 == 0 else 'h0'}" for i in range(n))
        fc.append(f"{ins}xstack=inputs={n}:layout={lay}:shortest=1[v]")
    return [job(args + ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "0:a?"] + vcodec() + AAC + ["-shortest"], c.out("_grid", ".mp4"), c.dur)]


# Crossfade: xfade (video) + acrossfade (audio) between consecutive clips; offsets are computed from each clip's duration.
@op("Combine", "crossfade", "Join clips with crossfade transitions",
    [O("files", "Clips to append after the selected file", "files"), O("tr", "Transition length (seconds)", "float", 1.0),
     O("style", "Style", "choice", "fade", ["fade", "dissolve", "fadeblack", "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "circleclose", "pixelize", "radial"])],
    tip="Leave the Input box empty and add all your clips below, in order - or select the first clip and add the rest. Each clip must be longer than the transition.", folder_one=True)
def b_crossfade(o, c):
    files = [c.src] + list(o["files"])
    n = len(files)
    if n < 2:
        raise ValueError("Add at least one more clip.")
    tr = float(o["tr"])
    infos = [c.info] + [probe(f) for f in files[1:]]
    durs = [i["dur"] for i in infos]
    if min(durs) <= tr + 0.1:
        raise ValueError("Every clip must be longer than the transition.")
    has_a = all(i["has_a"] for i in infos)
    W, H = (c.w or 1920) // 2 * 2, (c.h or 1080) // 2 * 2
    args, fc = [], []
    for i, f in enumerate(files):
        args += ["-i", f]
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]")
        if has_a:
            fc.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    L, prev, aprev = durs[0], "v0", "a0"
    for k in range(1, n):
        lab = "v" if k == n - 1 else f"x{k}"
        fc.append(f"[{prev}][v{k}]xfade=transition={o['style']}:duration={tr}:offset={L - tr:.3f}[{lab}]")
        prev = lab
        if has_a:
            alab = "a" if k == n - 1 else f"ax{k}"
            fc.append(f"[{aprev}][a{k}]acrossfade=d={tr}[{alab}]")
            aprev = alab
        L = L + durs[k] - tr
    maps = ["-map", "[v]"] + (["-map", "[a]"] if has_a else [])
    return [job(args + ["-filter_complex", ";".join(fc)] + maps + vcodec() + (AAC if has_a else []), c.out("_xfade", ".mp4"), L)]


# Slideshow: images -> video, optional Ken Burns zoompan, optional music, crossfades between photos. Uses folder_one logic (several images = one video).
@op("Combine", "slideshow", "Photos to video (slideshow / Ken Burns)",
    [O("files", "More photos to add", "files"), O("secs", "Seconds per photo", "float", 4),
     O("fx", "Effect", "choice", "Slow zoom in", ["Slow zoom in", "Slow zoom out", "None (still)"]),
     O("size", "Video size", "choice", "1920x1080 landscape", ["1920x1080 landscape", "1080x1920 vertical", "1080x1080 square"]),
     O("tr", "Crossfade between photos (seconds, 0 = cut)", "float", 0.5), O("music", "Music (optional)", "file", ftype="audio")],
    kind="image", tip="Choose your photos with 'Add…' or 'Add folder…' below (the Input box can stay empty), or pick a folder with 'Batch folder'. Photos are used in name order.", folder_one=True)
def b_slideshow(o, c):
    imgs = [c.src] + list(o["files"])
    n = len(imgs)
    W, H = map(int, o["size"].split(" ")[0].split("x"))
    fps = 30
    sec, tr = float(o["secs"]), float(o["tr"])
    if tr >= sec:
        raise ValueError("The crossfade must be shorter than the time per photo.")
    frames = int(round((sec + tr) * fps)) if tr else int(round(sec * fps))
    args, fc = [], []
    for i, f in enumerate(imgs):
        args += ["-i", f]
        base = f"[{i}:v]scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
        if o["fx"].startswith("Slow zoom in"):
            z = "min(zoom+0.0012,1.25)"
        elif o["fx"].startswith("Slow zoom out"):
            z = "if(eq(on,0),1.25,max(zoom-0.0012,1.0))"
        else:
            z = "1"
        fc.append(base + f"zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},setsar=1,format=yuv420p[s{i}]")
    if n == 1:
        last = "s0"
    elif tr:
        prev = "s0"
        for k in range(1, n):
            lab = "v" if k == n - 1 else f"x{k}"
            fc.append(f"[{prev}][s{k}]xfade=transition=fade:duration={tr}:offset={k * sec:.3f}[{lab}]")
            prev = lab
        last = "v"
    else:
        fc.append("".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]")
        last = "v"
    total = n * sec
    a = args
    maps = ["-map", f"[{last}]"]
    if o["music"]:
        a = args + ["-stream_loop", "-1", "-i", o["music"]]
        maps += ["-map", f"{n}:a"]
    tail = vcodec() + (AAC + ["-shortest"] if o["music"] else [])
    a += ["-filter_complex", ";".join(fc)] + maps + ["-t", f"{total:.3f}"] + tail
    return [job(a, c.out("_slideshow", ".mp4"), total)]


# ======================= CATEGORY: TEXT & SUBTITLES =======================
# =========================================================================== TEXT & SUBTITLES
# Escaping helpers: ffmpeg filter strings treat : \\ ' % specially, and Windows paths contain drive colons - these make paths/text safe inside -vf.
def esc_path(p):
    p = str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return "'" + p + "'"


def esc_text(t):
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace("%", "\\%")


# Pick a font file that exists on this OS (Windows Arial, else common Linux paths) for drawtext.
def fontfile():
    for p in (r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if os.path.exists(p):
            return esc_path(p)
    return None


# Burn captions into the picture with the subtitles filter.
@op("Text & Subtitles", "burnsubs", "Burn in captions (SRT / ASS)",
    [O("sub", "Subtitle file (.srt / .ass)", "file", ftype="sub"), O("size", "Font size", "int", 24),
     O("pos", "Position", "choice", "Bottom", ["Bottom", "Middle", "Top"]), O("outline", "Outline thickness", "int", 2)])
def b_burn(o, c):
    if not o["sub"]:
        raise ValueError("Choose a subtitle file.")
    al = {"Bottom": 2, "Middle": 5, "Top": 8}[o["pos"]]
    st = f"FontSize={o['size']},Outline={o['outline']},Alignment={al}"
    vf = f"subtitles={esc_path(o['sub'])}:force_style='{st}'"
    return [job(["-i", c.src, "-vf", vf] + vcodec() + ["-c:a", "copy"], c.out("_captioned", ".mp4"), c.dur)]


# Add subtitles as a separate (switchable) track, with language tag.
@op("Text & Subtitles", "addsubs", "Add a soft subtitle track (can be turned off)",
    [O("sub", "Subtitle file (.srt)", "file", ftype="sub"), O("lang", "Language code (eng, urd, pan...)", default="eng")])
def b_addsubs(o, c):
    if not o["sub"]:
        raise ValueError("Choose a subtitle file.")
    mkv = c.ext == ".mkv"
    codec = "srt" if mkv else "mov_text"
    return [job(["-i", c.src, "-i", o["sub"], "-map", "0", "-map", "1", "-c", "copy", "-c:s", codec, "-metadata:s:s:0", f"language={o['lang']}"],
                c.out("_subs", ".mkv" if mkv else ".mp4"), c.dur)]


# Extract the Nth subtitle track to a file.
@op("Text & Subtitles", "extractsubs", "Extract subtitles from a video",
    [O("fmt", "Save as", "choice", "srt", ["srt", "ass", "vtt"]), O("track", "Subtitle track number (1 = first)", "int", 1)])
def b_extractsubs(o, c):
    return [job(["-i", c.src, "-map", f"0:s:{int(o['track']) - 1}"], c.out("_subs", "." + o["fmt"]), 0)]


# Convert between SRT / ASS / VTT.
@op("Text & Subtitles", "convsubs", "Convert subtitle file format",
    [O("fmt", "Convert to", "choice", "srt", ["srt", "ass", "vtt"])], kind="sub", tip="Select a subtitle file (.srt, .ass, .vtt) at the top first.")
def b_convsubs(o, c):
    return [job(["-i", c.src], c.out("_conv", "." + o["fmt"]), 0)]


# Maps a position name ('Lower third', 'Top-right'...) to drawtext x/y expressions. _TPOS lists the names.
def _textpos(pos, size):
    m = max(20, int(size) // 2)
    return {"Top": ("(w-text_w)/2", f"{m}"), "Middle": ("(w-text_w)/2", "(h-text_h)/2"), "Bottom": ("(w-text_w)/2", f"h-text_h-{m}"),
            "Top-left": (f"{m}", f"{m}"), "Top-right": (f"w-text_w-{m}", f"{m}"), "Bottom-left": (f"{m}", f"h-text_h-{m}"),
            "Bottom-right": (f"w-text_w-{m}", f"h-text_h-{m}"), "Lower third": ("w*0.05", "h*0.78")}[pos]


_TPOS = ["Bottom", "Middle", "Top", "Lower third", "Top-left", "Top-right", "Bottom-left", "Bottom-right"]


# Shared drawtext builder for the title and timer ops: font, size, colour, optional dark box and outline, enable='between(t,a,b)' for show/hide times.
def _drawtext(c, text, o, suffix):
    ff = fontfile()
    x, y = _textpos(o["pos"], o["size"])
    parts = [f"text='{text}'", f"fontsize={o['size']}", f"fontcolor={o['color']}", f"x={x}", f"y={y}"]
    if ff:
        parts.insert(0, f"fontfile={ff}")
    if o.get("box"):
        parts += ["box=1", "boxcolor=black@0.55", "boxborderw=14"]
    else:
        parts += ["borderw=2", "bordercolor=black"]
    if o.get("end"):
        parts.append(f"enable='between(t,{tsec(o.get('start', '0'))},{tsec(o['end'])})'")
    elif o.get("start") and tsec(o["start"]) > 0:
        parts.append(f"enable='gte(t,{tsec(o['start'])})'")
    return [job(["-i", c.src, "-vf", "drawtext=" + ":".join(parts)] + vcodec() + ["-c:a", "copy"], c.out(suffix, ".mp4"), c.dur)]


# Title / lower-third text.
@op("Text & Subtitles", "drawtext", "Add a title / lower third",
    [O("text", "Text", default="My Title"), O("pos", "Position", "choice", "Bottom", _TPOS), O("size", "Font size", "int", 56),
     O("color", "Colour", default="white"), O("box", "Dark box behind the text", "bool", True),
     O("start", "Show from (seconds)", default="0"), O("end", "Hide at (blank = until the end)", default="")])
def b_drawtext(o, c):
    if not o["text"]:
        raise ValueError("Type some text.")
    return _drawtext(c, esc_text(o["text"]), o, "_title")


# Timer / countdown using drawtext's %{eif:...} time expressions.
@op("Text & Subtitles", "timer", "Add a timer / countdown",
    [O("mode", "Type", "choice", "Count up from 0", ["Count up from 0", "Count down from…"]), O("from", "Countdown from (seconds)", "int", 60),
     O("pos", "Position", "choice", "Top-right", _TPOS), O("size", "Font size", "int", 56), O("color", "Colour", default="white"),
     O("box", "Dark box behind the text", "bool", True)])
def b_timer(o, c):
    if o["mode"].startswith("Count up"):
        text = "%{pts\\:hms}"
    else:
        text = "%{eif\\:trunc(max(0\\," + str(int(o["from"])) + "-t))\\:d}"
    return _drawtext(c, text, o, "_timer")


# ======================= CATEGORY: EFFECTS & COLOUR =======================
# =========================================================================== EFFECTS & COLOUR
# Shared helper: run a simple video-filter chain and keep audio via stream copy.
def _vf(c, suffix, vf):
    a = ["-i", c.src, "-vf", vf] + vcodec()
    a += ["-c:a", "copy"] if c.has_a else []
    return [job(a, c.out(suffix, ".mp4"), c.dur)]


# Brightness / contrast / saturation / gamma via the eq filter.
@op("Effects & Colour", "colour", "Colour correction (brightness, contrast…)",
    [O("b", "Brightness (-0.5 to 0.5)", "float", 0.05), O("c", "Contrast (0.5 to 2)", "float", 1.1),
     O("s", "Saturation (0 to 3)", "float", 1.2), O("g", "Gamma (0.5 to 2)", "float", 1.0)])
def b_colour(o, c):
    return _vf(c, "_graded", f"eq=brightness={o['b']}:contrast={o['c']}:saturation={o['s']}:gamma={o['g']}")


# Apply a .cube 3D LUT (lut3d).
@op("Effects & Colour", "lut", "Apply a LUT (.cube colour preset)", [O("lut", "LUT file (.cube)", "file", ftype="lut")])
def b_lut(o, c):
    if not o["lut"]:
        raise ValueError("Choose a .cube LUT file.")
    return _vf(c, "_lut", f"lut3d=file={esc_path(o['lut'])}")


# Box blur.
@op("Effects & Colour", "blur", "Blur", [O("amt", "Strength (1-30)", "int", 8)])
def b_blur(o, c):
    return _vf(c, "_blur", f"boxblur={int(o['amt'])}:2")


# Unsharp mask.
@op("Effects & Colour", "sharpen", "Sharpen", [O("amt", "Strength (0.5-3)", "float", 1.0)])
def b_sharpen(o, c):
    return _vf(c, "_sharp", f"unsharp=5:5:{o['amt']}:5:5:0")


# hqdn3d video denoise.
@op("Effects & Colour", "vdenoise", "Denoise video", [O("amt", "Strength (1-10)", "float", 4)])
def b_vdenoise(o, c):
    return _vf(c, "_denoised", f"hqdn3d={o['amt']}:{o['amt']}:6:6")


# Vignette filter.
@op("Effects & Colour", "vignette", "Vignette (darkened corners)", [O("angle", "Strength (0.2-1.2, lower = stronger)", "float", 0.6)])
def b_vignette(o, c):
    return _vf(c, "_vignette", f"vignette=angle=PI*{o['angle']}")


# Chroma key: colorkey/chromakey over a solid colour or a chosen background image; similarity + edge softness options.
@op("Effects & Colour", "chroma", "Remove green screen (chroma key)",
    [O("key", "Key colour", default="0x00ff00"), O("sim", "Similarity (0.05-0.4)", "float", 0.15), O("blend", "Edge softness (0-0.3)", "float", 0.1),
     O("bgimg", "Background image (optional)", "file", ftype="image"), O("bgcol", "…or background colour", default="black")],
    tip="Replaces the key colour with a picture or a solid colour.")
def b_chroma(o, c):
    W, H = (c.w or 1280) // 2 * 2, (c.h or 720) // 2 * 2
    if o["bgimg"]:
        bg = ["-loop", "1", "-i", o["bgimg"]]
    else:
        bg = ["-f", "lavfi", "-i", f"color=c={o['bgcol']}:s={W}x{H}:r=30"]
    fc = f"[0:v]chromakey={o['key']}:{o['sim']}:{o['blend']}[fg];[1:v]scale={W}:{H},setsar=1[bg];[bg][fg]overlay=shortest=1[v]"
    return [job(["-i", c.src] + bg + ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?"] + vcodec() + AAC, c.out("_keyed", ".mp4"), c.dur)]


# Fade picture and (optionally) sound in/out, to/from black or white.
@op("Effects & Colour", "vfade", "Fade in / out (picture + sound)",
    [O("fin", "Fade in (seconds)", "float", 1), O("fout", "Fade out (seconds)", "float", 1),
     O("col", "Fade to / from", "choice", "black", ["black", "white"])])
def b_vfade(o, c):
    fin, fout = float(o["fin"]), float(o["fout"])
    st = max(0, c.dur - fout)
    vf = f"fade=t=in:st=0:d={fin}:color={o['col']},fade=t=out:st={st}:d={fout}:color={o['col']}"
    a = ["-i", c.src, "-vf", vf]
    if c.has_a:
        a += ["-af", f"afade=t=in:st=0:d={fin},afade=t=out:st={st}:d={fout}"]
    return [job(a + vcodec() + AAC, c.out("_fade", ".mp4"), c.dur)]


# Black & white or sepia (colorchannelmixer).
@op("Effects & Colour", "bw", "Black and white", [O("tone", "Look", "choice", "Black & white", ["Black & white", "Sepia"])])
def b_bw(o, c):
    if o["tone"] == "Sepia":
        vf = "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    else:
        vf = "hue=s=0"
    return _vf(c, "_bw", vf)


# Speed ramp: ONE filter_complex - trim into before / section / after, change the speed of the middle part (setpts + atempo), concat the three.
@op("Effects & Colour", "ramp", "Speed ramp (slow down or speed up a section)",
    [O("s", "Section starts at (seconds)", "float", 1), O("e", "Section ends at (seconds)", "float", 3),
     O("f", "Speed in the section (0.25 = slow-mo, 3 = fast)", "float", 0.4)])
def b_ramp(o, c):
    s, e, f = float(o["s"]), float(o["e"]), float(o["f"])
    if not (0 < s < e < c.dur):
        raise ValueError("Start and end must be inside the video, with start before end.")
    fc = (f"[0:v]trim=0:{s},setpts=PTS-STARTPTS[v0];[0:v]trim={s}:{e},setpts=(PTS-STARTPTS)/{f}[v1];[0:v]trim={e},setpts=PTS-STARTPTS[v2];")
    if c.has_a:
        fc += (f"[0:a]atrim=0:{s},asetpts=PTS-STARTPTS[a0];[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS,{atempo(f)}[a1];"
               f"[0:a]atrim={e},asetpts=PTS-STARTPTS[a2];[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc += "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
        maps = ["-map", "[v]"]
    return [job(["-i", c.src, "-filter_complex", fc] + maps + vcodec() + (AAC if c.has_a else []), c.out("_ramp", ".mp4"), c.dur)]


# Deinterlace with yadif.
@op("Effects & Colour", "deinterlace", "Deinterlace old footage")
def b_deint(o, c):
    return _vf(c, "_deint", "yadif=mode=0")


CATEGORIES = ["Video", "Format & Quality", "Audio", "Combine", "Text & Subtitles", "Effects & Colour", "Images & Frames", "Analysis", "Streaming"]


# --- 7. ENGINE ---------------------------------------------------------------
#  Everything from here down to the settings section runs jobs. It is GUI-free,
#  which is why --selftest can exercise it on a headless server.
# --------------------------------------------------------------------------- engine
# Default value of every option of a spec, keyed by option key (used for the dialogs and the self-test).
def default_opts(spec):
    return {s["key"]: s["default"] for s in spec["opts"]}


# Does this file suit the operation's kind? (e.g. audio ops accept audio files or videos with sound; image ops accept images.) Used to skip wrong files in batch mode.
def kind_ok(spec, info, ext):
    k = spec["kind"]
    if k == "image":
        return ext in IMAGE_EXT
    if k == "sub":
        return ext in SUB_EXT
    if k == "video":
        return info["has_v"]
    if k == "audio":
        return info["has_a"] and not info["has_v"]
    return info["has_v"] or info["has_a"]


# Extensions that count for a kind - drives file-dialog filters and folder scanning.
def kind_exts(kind):
    return {"video": VIDEO_EXT, "audio": AUDIO_EXT, "image": IMAGE_EXT, "sub": SUB_EXT}.get(kind, VIDEO_EXT | AUDIO_EXT)


# Remembers the commands of the most recent file so 'Copy last command' works after a run.
LAST_CMDS = []  # ffmpeg commands (copy-paste form) of the file most recently processed


# Render a job as a full ffmpeg command line. clean=True returns the copy-and-paste friendly form (no -progress flags).
def job_cmd(j, clean=False):
    """Full ffmpeg argument list for a job. clean=True gives the tidy copy-paste form."""
    if clean:
        cmd = ["ffmpeg", "-y"] + j["args"]
    else:
        level = "info" if j.get("info") else "warning"
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", level, "-nostats", "-progress", "pipe:1"] + j["args"]
    if j.get("out") and not j.get("raw_out"):
        cmd.append(j["out"])
    return cmd


# Run ONE job. Two kinds:
#    * func job  -> call j["func"](log) (pure Python, e.g. the full report)
#    * ffmpeg job -> spawn ffmpeg with -progress pipe:1, parse out_time to drive
#      the progress callback, honour `cancel` (kills the process), and return
#      (ok, output_text). Output lines are forwarded to `log`.
def run_job(j, log, progress, cancel, show_cmd=False):
    """Runs one job dict. Returns the process return code."""
    if j.get("func"):
        return j["func"](log)
    if j.get("mkdir"):
        os.makedirs(j["mkdir"], exist_ok=True)
    cmd = job_cmd(j)
    LAST_CMDS.append(subprocess.list2cmdline(job_cmd(j, clean=True)))
    if show_cmd:
        log("> " + subprocess.list2cmdline(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                         cwd=j.get("cwd"), creationflags=NO_WINDOW)
    err = []
    t = threading.Thread(target=lambda: [err.append(x.rstrip()) for x in p.stderr], daemon=True)
    t.start()
    dur = j.get("dur") or 0
    for line in p.stdout:
        if cancel.is_set():
            p.kill()
            break
        if dur and line.startswith("out_time_us="):
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / dur))
            except ValueError:
                pass
    p.wait()
    t.join(timeout=5)
    lines = err
    if j.get("show"):
        lines = [x for x in err if re.search(j["show"], x)] or ["(nothing found)"]
    elif j.get("tail"):
        lines = err[-j["tail"]:]
    elif p.returncode == 0:
        lines = [x for x in err if x.strip()][-5:]
    for x in lines:
        log(x)
    progress(1.0)
    return p.returncode


# Paths of everything a job list will create (used for the self-test and 'open output folder').
def output_paths(j):
    if j.get("tmpout"):
        return []
    if j.get("outs"):
        return list(j["outs"]) + ([j["out"]] if j.get("out") else [])
    return [j["out"]] if j.get("out") else []


# Run an operation over a list of files:
#    for each file -> make a Ctx -> check kind_ok -> call the builder -> run each
#    job. If a GPU (NVENC) run fails, retry once on the CPU and switch the GPU
#    checkbox state off. Failures write the full command + output to the log.
#    Returns (ok_count, fail_count, output_paths).
def execute(spec, opts, files, outdir, log, progress, cancel, show_cmd=False):
    """Run spec on every file. Returns (ok_count, fail_count, outputs)."""
    ok = fail = 0
    outputs = []
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ffstudio_")
    try:
        for fi, f in enumerate(files):
            if cancel.is_set():
                break
            log(f"\n=== {Path(f).name} ===")
            del LAST_CMDS[:]
            try:
                c = Ctx(f, outdir, tmp)
                if not kind_ok(spec, c.info, c.ext):
                    log("Skipped: this operation does not apply to this kind of file.")
                    write_log(f"Skipped '{spec['id']}' on {f}: not applicable to this kind of file.")
                    fail += 1
                    continue
                jobs = spec["fn"](opts, c)
            except Exception as e:  # noqa: BLE001
                log(f"Error: {e}")
                write_log(f"ERROR building '{spec['id']}' for {f}: {e!r}")
                fail += 1
                continue
            good = False
            for attempt in (1, 2):
                cap = []

                def log2(m, cap=cap):
                    cap.append(str(m))
                    log(m)
                good, new_outs = True, []
                for ji, j in enumerate(jobs):
                    base = (fi + ji / len(jobs)) / len(files)
                    span = 1.0 / (len(files) * len(jobs))
                    rc = run_job(j, log2, lambda fr, b=base, s=span: progress(b + fr * s), cancel, show_cmd)
                    if rc != 0:
                        good = False
                        log(f"ffmpeg failed (exit {rc}).")
                        break
                    new_outs.extend(output_paths(j))
                if good:
                    outputs.extend(new_outs)
                    break
                enc_ = GPU_ENCODERS[STATE["gpu_enc"]]
                if attempt == 1 and STATE["gpu"] and not cancel.is_set() and any(t_ in x.lower() for x in cap for t_ in enc_["tag"]):
                    STATE["gpu"] = False
                    write_log(f"{enc_['label']} failed for '{spec['id']}' on {Path(f).name}; switched to CPU. ffmpeg said:\n" + "\n".join(cap[-15:]))
                    log(f"The GPU encoder ({enc_['label']}) failed on this computer, so I switched to CPU encoding and am retrying. "
                        "(GPU stays off until you restart the app.)")
                    try:
                        jobs = spec["fn"](opts, c)
                    except Exception as e:  # noqa: BLE001
                        log(f"Error: {e}")
                        break
                    continue
                break
            if not good and not cancel.is_set():
                write_log(f"FAILED operation '{spec['id']}' on {f}\noptions: {opts}\n" + "\n".join(cap[-40:]))
                log(f"Details saved to {LOG_FILE}")
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    progress(1.0)
    return ok, fail, outputs


# Turn the dialog's raw string values into typed values (int/float/bool/file lists). Raises ValueError for a bad number, which the GUI shows as 'One of the numbers isn't valid.'
def convert_opts(spec, raw):
    out = {}
    for s in spec["opts"]:
        v = raw.get(s["key"], s["default"])
        t = s["type"]
        if t == "int":
            v = int(float(v))
        elif t == "float":
            v = float(v)
        elif t == "bool":
            v = bool(v)
        elif t == "files":
            v = list(v)
        else:
            v = str(v).strip()
        out[s["key"]] = v
    return out


# --------------------------------------------------------------------------- settings
# --- 8. COPY-COMMAND / LIVE PREVIEW / QUEUE ---------------------------------
#  GUI-independent pieces that the dialog windows lean on.
# --------------------------------------------------------------------------- copy command / preview / queue (engine)
# Format seconds as MMM:SS:mmm - the same format tsec() parses back, so sliders and text boxes round-trip.
def fmt_ms(t):
    """Seconds -> 'MMM:SS:mmm' (minutes:seconds:milliseconds), the format the time boxes use."""
    ms = max(0, int(round(float(t) * 1000)))
    return f"{ms // 60000:03d}:{ms // 1000 % 60:02d}:{ms % 1000:03d}"


# Tiny, safe arithmetic evaluator (parses with ast, never eval()) for ffmpeg size expressions like min(iw,ih) or (iw-ow)/2, so the crop sliders can show real numbers.
def eval_expr(txt, env):
    """Evaluate a simple ffmpeg size expression like 'min(iw,ih)' or '(iw-ow)/2'. Returns a float, or None if it can't."""
    import ast
    import operator
    binops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name) and n.id in env:
            return float(env[n.id])
        if isinstance(n, ast.BinOp) and type(n.op) in binops:
            return binops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("min", "max") and not n.keywords and n.args:
            return (min if n.func.id == "min" else max)(*[ev(a) for a in n.args])
        raise ValueError("unsupported")
    try:
        return ev(ast.parse(str(txt).strip(), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError, RecursionError):
        return None


# Options that get a slider in their window: (operation id, option key) -> (kind, low, high)
# kinds: pos = a time inside the clip (high = clip length), posend = same but far right means "to the end" (blank),
#        len = a length in seconds, num = a plain number, cw/ch/cx/cy = crop width/height/x/y in pixels
# Which options get a slider in which operation, and what kind: pos/posend (time), len (duration), num/inum (numbers), cw/ch/cx/cy (crop box). Min/max can be expressions of the video's duration/width/height.
SLIDERS = {
    ("trim", "start"): ("pos", 0, None), ("trim", "end"): ("posend", 0, None),
    ("atrim", "start"): ("pos", 0, None), ("atrim", "end"): ("posend", 0, None),
    ("thumb", "t"): ("pos", 0, None),
    ("gif", "start"): ("pos", 0, None), ("gif", "dur"): ("len", 0.5, 30), ("gif", "fps"): ("num", 1, 30), ("gif", "w"): ("num", 120, 1280),
    ("freeze", "t"): ("pos", 0, None), ("freeze", "d"): ("len", 0.5, 20),
    ("split", "seconds"): ("len", 1, None),
    ("silence", "db"): ("num", -90, -10), ("silence", "secs"): ("len", 0.1, 5),
    ("fade", "fin"): ("len", 0, 10), ("fade", "fout"): ("len", 0, 10),
    ("vfade", "fin"): ("len", 0, 10), ("vfade", "fout"): ("len", 0, 10),
    ("crossfade", "tr"): ("len", 0.1, 5),
    ("drawtext", "start"): ("pos", 0, None), ("drawtext", "end"): ("posend", 0, None),
    ("timer", "from"): ("num", 1, 600),
    ("ramp", "s"): ("len", 0, None), ("ramp", "e"): ("len", 0, None),
    ("resize", "width"): ("inum", 160, 3840), ("resize", "height"): ("inum", 120, 2160),
    ("crop", "w"): ("cw", 2, None), ("crop", "h"): ("ch", 2, None), ("crop", "x"): ("cx", 0, None), ("crop", "y"): ("cy", 0, None),
}


# Look an operation up by id (queue items store ids, not functions).
def find_op(op_id):
    return next((s for s in OPS if s["id"] == op_id), None)


# Build the printable ffmpeg commands for a spec + options + file, without running anything ('Copy ffmpeg command' button).
def format_commands(spec, opts, f, outdir=None):
    """The ffmpeg command line(s) an operation would run on file f, as text to paste into a terminal. Runs nothing."""
    tmp = tempfile.mkdtemp(prefix="ffstudio_")
    try:
        c = Ctx(f, outdir, tmp)
        if not kind_ok(spec, c.info, c.ext):
            raise ValueError("This operation does not apply to this kind of file.")
        out = []
        for j in spec["fn"](opts, c):
            if j.get("func"):
                out.append("REM (built-in step - no ffmpeg command)")
            else:
                out.append(subprocess.list2cmdline(job_cmd(j, clean=True)))
        return "\n".join(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Grab one frame as PNG at time t with an optional filter, scaled to fit a box - the basis of every preview.
def grab_frame(path, t, out_png, vf="", box=(420, 240)):
    """Save one frame at time t (seconds) as a PNG scaled to fit inside box (box=None: full size). Returns True on success."""
    parts = [vf] if vf else []
    if box:
        parts.append(f"scale=w={box[0]}:h={box[1]}:force_original_aspect_ratio=decrease")
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1"]
    if parts:
        cmd += ["-vf", ",".join(parts)]
    cmd.append(out_png)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0


# Film strip for the Trim window: one PNG of small thumbnails tiled across the whole video.
#  Cached in the temp folder (keyed by file path, date and size) so reopening is instant.
def make_strip(path, dur, w, h, cw=560, th=48):
    """Return (png_path, width, height) of a tiled film strip, or (None, 0, 0) if it can't be made."""
    if dur <= 0 or w <= 0 or h <= 0:
        return None, 0, 0
    n = max(4, int(round(cw / max(1.0, th * w / h))))  # whole thumbnails that fill the strip width
    tw = max(24, (cw // n) // 2 * 2)
    th = max(28, min(84, int(round(tw * h / w / 2.0)) * 2))
    try:
        st_ = os.stat(path)
        key = f"{path}|{st_.st_mtime_ns}|{st_.st_size}|{tw}|{th}|{n}"
        d = os.path.join(tempfile.gettempdir(), "om_video_studio_strips")
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None, 0, 0
    out = os.path.join(d, hashlib.md5(key.encode("utf-8", "replace")).hexdigest() + ".png")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        sw, sh = png_size(out)
        return (out, sw, sh) if sw else (None, 0, 0)
    vf = (f"fps={n}/{dur:.3f},scale={tw}:{th}:force_original_aspect_ratio=decrease,"
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,tile={n}x1")
    part = out[:-4] + ".part.png"
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if dur > 180:
        cmd += ["-skip_frame", "nokey"]  # long video: decode keyframes only so the strip appears quickly
    cmd += ["-i", path, "-an", "-sn", "-vf", vf, "-frames:v", "1", part]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=240, creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
            return None, 0, 0
        os.replace(part, out)
    except (OSError, subprocess.SubprocessError):
        return None, 0, 0
    sw, sh = png_size(out)
    return (out, sw, sh) if sw else (None, 0, 0)


# Scrub cache for the Trim window: small frames (about one per second, at most ~300) saved as
#  s_00001.png, s_00002.png ... so the preview can show something instantly while a handle is
#  dragged (the sharp exact frame follows). Kept in the temp folder, keyed by the file; old
#  caches are removed after 3 days. Frame k is the picture at time (k-1) * step.
SCRUB_BOX = (280, 158)

# Which operations get the film strip with handles in their window, and which boxes the two handles control.
#  a / b = option keys; mode "range" = a is the start time and b the end time, "window" = a is the start
#  and b the LENGTH (Make a GIF); fa / fb = how a value is written into the box ("ms" = 001:22:400 like the
#  sliders use, "num" = plain seconds); blank = an empty end box means "until the end of the file".
STRIP_OPS = {
    "trim": dict(a="start", b="end", mode="range", fa="ms", fb="ms", blank=True),
    "atrim": dict(a="start", b="end", mode="range", fa="ms", fb="ms", blank=True, audio=True),
    "drawtext": dict(a="start", b="end", mode="range", fa="ms", fb="ms", blank=True),
    "ramp": dict(a="s", b="e", mode="range", fa="num", fb="num", blank=False),
    "gif": dict(a="start", b="dur", mode="window", fa="ms", fb="num", blank=False),
}


def make_wave(path, dur, cw, th=64):
    """Waveform picture for audio files (the film strip of an audio file). Returns (png_path, width, height)."""
    if dur <= 0:
        return None, 0, 0
    try:
        st_ = os.stat(path)
        key = f"{path}|{st_.st_mtime_ns}|{st_.st_size}|wave|{cw}|{th}"
        d = os.path.join(tempfile.gettempdir(), "om_video_studio_strips")
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None, 0, 0
    out = os.path.join(d, hashlib.md5(key.encode("utf-8", "replace")).hexdigest() + ".png")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        sw, sh = png_size(out)
        return (out, sw, sh) if sw else (None, 0, 0)
    part = out[:-4] + ".part.png"
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", path, "-vn", "-filter_complex",
           f"aformat=channel_layouts=mono,showwavespic=s={cw}x{th}:colors=0x4fa3e0", "-frames:v", "1", part]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=240, creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
            return None, 0, 0
        os.replace(part, out)
    except (OSError, subprocess.SubprocessError):
        return None, 0, 0
    sw, sh = png_size(out)
    return (out, sw, sh) if sw else (None, 0, 0)


def scrub_dir_for(path, step):
    try:
        st_ = os.stat(path)
    except OSError:
        return None
    key = f"{path}|{st_.st_mtime_ns}|{st_.st_size}|{step:.3f}|{SCRUB_BOX}"
    base = os.path.join(tempfile.gettempdir(), "om_video_studio_scrub")
    try:
        os.makedirs(base, exist_ok=True)
        now = time.time()
        for n_ in os.listdir(base):  # tidy up caches nobody has used for 3 days
            pth = os.path.join(base, n_)
            if os.path.isdir(pth) and now - os.path.getmtime(pth) > 3 * 86400:
                shutil.rmtree(pth, ignore_errors=True)
    except OSError:
        pass
    return os.path.join(base, hashlib.md5(key.encode("utf-8", "replace")).hexdigest())


def make_scrub(path, dur, outdir, step):
    """Fill outdir with small frames every `step` seconds. Returns True when finished (or already cached)."""
    done = os.path.join(outdir, "ok")
    if os.path.exists(done):
        try:
            os.utime(outdir)
        except OSError:
            pass
        return True
    os.makedirs(outdir, exist_ok=True)
    vf = f"fps=1/{step:.3f},scale={SCRUB_BOX[0]}:{SCRUB_BOX[1]}:force_original_aspect_ratio=decrease"
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if dur > 180:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", path, "-an", "-sn", "-vf", vf, os.path.join(outdir, "s_%05d.png")]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return False
        with open(done, "w") as f:
            f.write("ok")
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# Encoder-only ffmpeg flags (codec, CRF, preset...) that preview_frame removes from a job's arguments so a single-frame preview can be produced from the same command.
_STRIP1 = {"-c:v", "-crf", "-preset", "-pix_fmt", "-profile:v", "-c:a", "-b:a", "-ar", "-ac", "-movflags", "-b:v", "-maxrate",
           "-bufsize", "-cq", "-rc", "-tune", "-level", "-c", "-codec", "-acodec", "-vcodec", "-b:a:0"}


# Read width/height straight from a PNG header (no imaging library needed).
def png_size(path):
    """(width, height) of a PNG file, or (0, 0)."""
    try:
        with open(path, "rb") as f:
            h = f.read(24)
        if h[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(h[16:20], "big"), int.from_bytes(h[20:24], "big")
    except OSError:
        pass
    return 0, 0


# Render the 'after' picture for the live preview using the same filter the real job would use.
def preview_frame(spec, opts, src, t, out_png):
    """Render ONE frame of what an operation would do (its real filters, no encoding) to a PNG. True on success."""
    tmp = tempfile.mkdtemp(prefix="ffprev_")
    try:
        c = Ctx(src, tmp, tmp)
        j = next((x for x in spec["fn"](opts, c) if not x.get("func") and not x.get("raw_out")), None)
        if not j or "-i" not in j["args"]:
            return False
        a, out, i = list(j["args"]), [], 0
        while i < len(a):
            if a[i] in _STRIP1 and i + 1 < len(a):
                i += 2
                continue
            if a[i] == "-map" and i + 1 < len(a) and re.search(r"^\[?a|:a", a[i + 1]):
                i += 2
                continue
            out.append(a[i])
            i += 1
        if "-ss" not in out:
            k = out.index("-i")
            out[k:k] = ["-ss", f"{max(0.0, t):.3f}"]
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"] + out + ["-an", "-frames:v", "1", out_png]
        r = subprocess.run(cmd, capture_output=True, timeout=40, cwd=j.get("cwd"), creationflags=NO_WINDOW)
        return r.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Play a short sample: renders a few seconds of what an operation would do (its real settings, so you
#  see or hear the result before running the whole job), then ffplay plays it. Only for operations in
#  SAMPLE_OPS: effects, geometry, text and audio operations that work on one input. Operations that
#  look at the time of the picture (title, timer, speed ramp) keep their real timeline by seeking on
#  the OUTPUT side; everything else seeks on the input side (fast).
SAMPLE_OPS = {"colour", "blur", "sharpen", "vdenoise", "vignette", "chroma", "vfade", "bw", "ramp", "deinterlace",
              "resize", "crop", "rotate", "flip", "aspect", "speed", "reverse", "drawtext", "timer", "watermark",
              "loudnorm", "volume", "tempo", "pitch", "denoise", "eq", "fade", "mix", "narration"}
_OUT_SEEK_OPS = {"drawtext", "timer", "ramp"}
_FROM_START_OPS = {"vfade", "fade"}
_AUDIO_OUT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".mka"}


def sample_start(spec, opts, dur, secs):
    """Where in the file the sample should begin (seconds)."""
    if spec["id"] in _FROM_START_OPS:
        return 0.0
    try:
        if spec["id"] == "drawtext":
            return max(0.0, tsec(opts.get("start") or "0") - 1.0)
        if spec["id"] == "ramp":
            return max(0.0, float(opts.get("s") or 0) - 1.0)
    except (ValueError, TypeError):
        pass
    return max(0.0, min(dur / 3.0, dur - secs)) if dur else 0.0


def make_sample(spec, opts, src, t0, secs, out_dir):
    """Render the sample into out_dir. Returns (path, is_audio_only). Raises ValueError with a plain message."""
    tmp = tempfile.mkdtemp(prefix="ffsamp_")
    try:
        c = Ctx(src, tmp, tmp)
        j = next((x for x in spec["fn"](opts, c) if not x.get("func") and not x.get("raw_out") and x.get("out")), None)
        if not j or "-i" not in j["args"]:
            raise ValueError("A sample isn't available for this operation.")
        a = list(j["args"])
        if "-preset" in a and a.index("-preset") + 1 < len(a):
            a[a.index("-preset") + 1] = "ultrafast"  # a sample only needs to be quick
        if spec["id"] in _OUT_SEEK_OPS:
            a += ["-ss", f"{t0:.3f}", "-t", f"{secs}"]
        else:
            if "-ss" not in a:
                k = a.index("-i")
                a[k:k] = ["-ss", f"{t0:.3f}", "-t", f"{secs + 1}"]  # limit the INPUT too, so Reverse doesn't load the whole clip
            a += ["-t", f"{secs}"]
        ext = Path(j["out"]).suffix or ".mp4"
        out = os.path.join(out_dir, "sample" + ext)
        if os.path.exists(out):
            os.remove(out)
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"] + a + [out]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                           cwd=j.get("cwd"), creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            tail = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise ValueError("Couldn't make the sample: " + tail[0][:160])
        return out, ext.lower() in _AUDIO_OUT
    except subprocess.SubprocessError as e:
        raise ValueError(f"Couldn't make the sample: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Turn the chosen path (file / folder / multi-selection) into the actual list of files for this operation, honouring folder_one and kind filters.
def resolve_files(spec, opts, path, multi=None):
    """Work out which files an operation runs on. Returns (files, opts, error_message_or_None)."""
    needs_extra = any(s["type"] in ("file", "files") for s in spec["opts"])
    extras = list(opts.get("files", [])) if spec.get("folder_one") else []
    if (spec.get("folder_one") and extras and not multi
            and (not path or not os.path.exists(path) or (os.path.isfile(path) and Path(path).suffix.lower() not in kind_exts(spec["kind"])))):
        # clips/photos were listed in the window and the input box is empty or holds something else: use the listed ones
        return [extras[0]], {**opts, "files": extras[1:]}, None
    if multi:
        files = [f for f in multi if os.path.isfile(f)]
        if not files:
            return [], opts, "None of the selected files exist."
    elif not path or not os.path.exists(path):
        return [], opts, "Choose a file or a folder first."
    elif os.path.isdir(path):
        exts = kind_exts(spec["kind"])
        files = sorted(str(f) for f in Path(path).iterdir() if f.is_file() and f.suffix.lower() in exts)
        if not files:
            return [], opts, "No matching files in that folder."
    else:
        return [path], opts, None
    if needs_extra and not spec.get("folder_one"):
        return [], opts, "This operation needs extra files, so it can't run on several files at once. Pick a single file."
    if spec.get("folder_one"):
        opts = dict(opts)
        opts["files"] = files[1:] + extras
        files = files[:1]
    return files, opts, None


# Run queued items one after another through execute(); returns total ok / fail counts.
def run_queue(items, log, progress, cancel, show_cmd=False):
    """items: list of dicts {spec, opts, files, outdir}. Runs them one after another. Returns (ok, fail, outputs)."""
    ok = fail = 0
    outs = []
    n = len(items)
    for i, it in enumerate(items):
        if cancel.is_set():
            log(f"Queue cancelled - {n - i} job(s) not started.")
            break
        log(f"\n##### Queue {i + 1}/{n}: {it['spec']['label']}  ({len(it['files'])} file{'s' if len(it['files']) != 1 else ''})")
        o_, f_, outs_ = execute(it["spec"], it["opts"], it["files"], it.get("outdir"), log,
                                lambda fr, i=i: progress((i + fr) / n), cancel, show_cmd)
        ok, fail = ok + o_, fail + f_
        outs.extend(outs_)
    progress(1.0)
    return ok, fail, outs


# --- 9. SETTINGS & STARTUP SOUND -------------------------------------------
#  ~/.om_video_studio.json keeps: theme, language, font size, recent files,
#  favourites, last-used option values, GPU/sound switches. Both functions
#  swallow errors - settings must never stop the app starting.
def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d):
    try:
        SETTINGS_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


# Optional start-up jingle bundled in Resource/. play_startup_sound plays it asynchronously on Windows via winsound / MCI; silently does nothing elsewhere.
STARTUP_SOUND = RES_DIR / "Resource" / "Whips _the_Llama_Ass.mp3"


def play_startup_sound():
    """Play the launch sound. Windows: convert the mp3 to a small wav with ffmpeg, then play it with winsound
    (standard library, same path as normal Windows sounds). Fallback: ffplay. Turn off with
    "startup_sound": false in the settings file. Every step is written to om_video_studio_log.txt."""
    if not load_settings().get("startup_sound", True):
        return
    try:
        import atexit
        snd = STARTUP_SOUND
        if not snd.exists():  # fall back to any mp3 in the Resource folder (handles renamed files)
            found = sorted(snd.parent.glob("*.mp3")) if snd.parent.is_dir() else []
            if not found:  # normal for the public installer: it ships without a sound
                return
            snd = found[0]
        if os.name == "nt":
            try:
                import winsound
                wav = Path(tempfile.gettempdir()) / "om_video_studio_start.wav"
                r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(snd), "-vn", "-acodec", "pcm_s16le",
                                    "-ar", "44100", "-ac", "2", str(wav)], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", creationflags=NO_WINDOW, timeout=20)
                if r.returncode == 0 and wav.exists():
                    winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
                    write_log(f"Startup sound: playing {snd.name} via winsound")
                    return
                write_log("Startup sound: wav conversion failed: " + (r.stderr or "")[-300:])
            except Exception as e:  # noqa: BLE001
                write_log(f"Startup sound: winsound path failed: {e!r}")
        if IS_MAC:  # afplay plays mp3 directly
            try:
                subprocess.Popen(["afplay", str(snd)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                write_log(f"Startup sound: playing {snd.name} via afplay")
                return
            except Exception as e:  # noqa: BLE001
                write_log(f"Startup sound: afplay failed: {e!r}")
        ffplay = find_tool("ffplay") or str(Path(FFMPEG).with_name("ffplay" + (".exe" if os.name == "nt" else "")))
        if os.path.exists(ffplay):
            proc = subprocess.Popen([ffplay, "-nodisp", "-autoexit", "-loglevel", "error", str(snd)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=NO_WINDOW)
            atexit.register(lambda: proc.poll() is None and proc.kill())
            write_log(f"Startup sound: playing {snd.name} via ffplay")

            def _watch():
                try:
                    err = proc.communicate(timeout=60)[1].decode("utf-8", "replace").strip()
                    if err:
                        write_log("Startup sound (ffplay said): " + err[-300:])
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=_watch, daemon=True).start()
        else:
            write_log("Startup sound: no way to play (ffplay not found)")
    except Exception as e:  # noqa: BLE001
        write_log(f"Startup sound failed: {e!r}")


# --- 10. LANGUAGES ----------------------------------------------------------
#  English strings in the code ARE the translation keys. om_translations.py
#  supplies LANGS (code -> native name) and TR (code -> {english: translated}).
#  tr(s) looks a string up in the current language and returns s unchanged if
#  missing, so a missing translation can never crash the UI. Templates containing
#  {} (e.g. "Running: {}") are matched by pattern so dynamic text still translates.
#  The language is applied at start-up (gui() patches ttk/tk widgets so every
#  text= is passed through tr); changing language asks for a restart.
# --------------------------------------------------------------------------- languages
try:
    from om_translations import LANGS, TR  # language names + {code: {english text: translation}}
except Exception:  # noqa: BLE001 - the app still works (in English) without the translation file
    LANGS, TR = {"en": "English"}, {}
# Current language state: code and compiled template patterns.
LANG = {"code": "en", "templ": []}


# Select a language and precompile its {} templates.
def set_lang(code):
    LANG["code"] = code if code in LANGS else "en"
    tbl = TR.get(LANG["code"], {})
    # "Running: {}" style entries match any text of that shape
    LANG["templ"] = [(re.compile("^" + re.escape(k).replace(r"\{\}", "(.+?)") + "$", re.S), v) for k, v in tbl.items() if "{}" in k]


# Translate one string (handles category labels with an icon prefix and multi-line labels).
def tr(s):
    """Translate an English UI text into the chosen language (unknown texts stay English)."""
    if not isinstance(s, str) or LANG["code"] == "en" or not s.strip():
        return s
    tbl = TR.get(LANG["code"], {})
    if s in tbl:
        return tbl[s]
    if "\n(" in s and s.replace("\n(", " (") in tbl:  # operation buttons wrap their label before the bracket
        return tbl[s.replace("\n(", " (")].replace(" (", "\n(", 1)
    for rx, v in LANG["templ"]:
        m = rx.match(s)
        if m:
            parts = []
            for g in m.groups():
                parts.append(tr(g))
            return v.format(*parts) if "{}" in v else v
    m = re.match(r"^(\S{1,3}\s{2,})(.+)$", s, re.S)  # "icon  Category"
    if m and m.group(2) in tbl:
        return m.group(1) + tbl[m.group(2)]
    return s


# --- 11. COMPLETION PINGS -----------------------------------------------------
#  Three short tone patterns are synthesised with the wave module (no audio
#  files shipped): SOUNDS['job'] single job, ['batch'] several files/queue,
#  ['error'] something failed. Written once to the temp folder, played
#  asynchronously with winsound. The 'Sound' checkbox switches them off.
# --------------------------------------------------------------------------- completion sounds
# Synthesise a WAV of sine notes with a small fade to avoid clicks.
def _tone_wav(path, notes, rate=44100):
    """Write a small mono 16-bit wav of bell-like notes. notes = [(freq_hz, start_s, length_s, volume 0-1)]."""
    import math, struct, wave
    total = max(st + ln for _, st, ln, _ in notes) + 0.15
    n = int(total * rate)
    buf = [0.0] * n
    for f, st, ln, vol in notes:
        i0 = int(st * rate)
        for i in range(int(ln * rate)):
            if i0 + i >= n:
                break
            t = i / rate
            env = min(1.0, i / (0.004 * rate)) * math.exp(-4.2 * t / ln)  # quick attack, smooth decay
            v = math.sin(2 * math.pi * f * t) + 0.3 * math.sin(2 * math.pi * f * 2.01 * t) + 0.12 * math.sin(2 * math.pi * f * 3.02 * t)
            buf[i0 + i] += vol * env * v
    peak = max(1e-9, max(abs(x) for x in buf))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x / peak * 0.85)) * 32767)) for x in buf))


# kind -> notes. "job" = one job finished (short two-note ping), "batch" = queue / many files finished
# (a different, longer rising chime), "error" = something failed (low, falling).
# The three ping melodies (note frequency, length).
SOUNDS = {
    "job": [(1319, 0.00, 0.22, 0.8), (1760, 0.11, 0.30, 0.9)],
    "batch": [(523, 0.00, 0.35, 0.8), (659, 0.16, 0.35, 0.8), (784, 0.32, 0.35, 0.8), (1047, 0.48, 0.70, 0.95)],
    "error": [(330, 0.00, 0.30, 0.9), (247, 0.20, 0.45, 0.9)],
}


# Make the WAV for a ping kind on first use and return its path.
def sound_file(kind):
    p = Path(tempfile.gettempdir()) / f"om_video_studio_{kind}.wav"
    if not p.exists():
        _tone_wav(p, SOUNDS[kind])
    return p


# Fire-and-forget playback (Windows only; ignored elsewhere or on error).
def play_ping(kind):
    """Play a completion sound (Windows: winsound, asynchronous). Off when the settings file has "sounds": false."""
    try:
        if not load_settings().get("sounds", True):
            return False
        p = sound_file(kind)
        if os.name == "nt":
            import winsound
            winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return True
        if IS_MAC:  # afplay ships with macOS; Popen returns immediately so playback is asynchronous
            subprocess.Popen(["afplay", str(p)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception as e:  # noqa: BLE001
        write_log(f"Ping failed: {e!r}")
    return False


# The "you need to do something" alert: standard Windows exclamation sound / macOS Funk sound. Returns False if nothing could be played
# (the caller then falls back to Tk's bell). Deliberately ignores the Sound checkbox - it is a warning, not a completion ping.
def play_alert():
    try:
        if os.name == "nt":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            return True
        if IS_MAC:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Funk.aiff"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


# Choose which ping: error if any failure, batch if more than one file/queue, else single-job.
def ping_kind(ok, fail, batch):
    return "error" if fail and not ok else ("batch" if batch else "job")


# Looks for a hardware H.264 encoder that this ffmpeg lists AND can really use here (a tiny test encode must succeed), trying NVIDIA, AMD, Intel, then Apple. Returns the GPU_ENCODERS key, or None (CPU only).
def detect_gpu():
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW)
    except Exception:
        return None
    for key, enc in GPU_ENCODERS.items():
        if enc["codec"] not in r.stdout:
            continue
        try:
            t = subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.2",
                                "-c:v", enc["codec"]] + enc["args"](23) + ["-f", "null", os.devnull], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=30)
            if t.returncode == 0:  # only offer the GPU option if a real test encode works
                return key
        except Exception:
            continue
    return None


# Plain-English report for the "Check GPU" button: which graphics cards Windows sees, and for each hardware encoder whether this ffmpeg has it and whether a real test encode works. Returns (text, working_key_or_None).
def gpu_report():
    lines, found = [], None
    if sys.platform == "win32":
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               creationflags=NO_WINDOW, timeout=20)
            cards = [x.strip() for x in r.stdout.splitlines() if x.strip()]
            lines.append("Graphics cards Windows can see:\n  " + ("\n  ".join(cards) if cards else "none found"))
        except Exception:
            lines.append("Graphics cards Windows can see: could not check")
    try:
        enc_list = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", creationflags=NO_WINDOW).stdout
    except Exception as e:  # noqa: BLE001
        return f"Could not run ffmpeg: {e}", None
    lines.append("\nHardware encoders (a real 0.2 s test encode is tried for each):")
    for key, enc in GPU_ENCODERS.items():
        if enc["codec"] not in enc_list:
            lines.append(f"  {enc['label']}: not in this ffmpeg build")
            continue
        try:
            t = subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.2",
                                "-c:v", enc["codec"]] + enc["args"](23) + ["-f", "null", os.devnull], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=30)
            if t.returncode == 0:
                lines.append(f"  {enc['label']}: WORKS")
                found = found or key
            else:
                why = (t.stderr.strip().splitlines() or ["failed"])[0][:110]
                lines.append(f"  {enc['label']}: no card / driver here ({why})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  {enc['label']}: test failed ({e})")
    lines.append("\n" + (f"Result: {GPU_ENCODERS[found]['label']} will be used." if found else
                          "Result: no usable GPU encoder, so the CPU will be used (slower, but everything still works)."))
    return "\n".join(lines), found


# --- 12. GUI ------------------------------------------------------------------
#  The entire interface lives inside gui(). Widgets and handlers are closures
#  sharing local state, which keeps the module namespace clean and lets the file
#  stay a single script. Reading order inside gui():
#     a. imports, translation patching, theme/fonts/styles
#     b. worker<->GUI queue and pump()
#     c. header (theme toggle, language)
#     d. Input card + info strip
#     e. bottom bar (Cancel, Open folder, Queue, Copy, GPU, Sound, A+/A-, log)
#     f. sidebar categories + button grids (+ Favourites)
#     g. open_dialog(): per-operation option window (+ sliders + live preview)
#     h. run / queue / copy-command logic
#     i. ffmpeg install offer, update check, start-up
# --------------------------------------------------------------------------- GUI
# Emoji icon for each category tab (the text before the label in the sidebar).
# Interface fonts per system (Segoe UI / Consolas only exist on Windows).
UI_FONT, MONO_FONT = (("Segoe UI", "Consolas") if IS_WIN else ("Helvetica Neue", "Menlo") if IS_MAC else ("DejaVu Sans", "DejaVu Sans Mono"))
CAT_ICONS = {"Favourites": "★", "Video": "\U0001F3AC", "Format & Quality": "\U0001F527", "Audio": "\U0001F3B5", "Combine": "\U0001F9E9",
             "Text & Subtitles": "\U0001F4AC", "Effects & Colour": "\U0001F3A8", "Images & Frames": "\U0001F5BC", "Analysis": "\U0001F50D",
             "Streaming": "\U0001F4E1"}
# Light and dark colour sets; BRAND_* are the accent colours used for headers and the primary button.
PALETTE = {
    "light": dict(bg="#fafafa", fg="#1a1a1a", muted="#666666", log_bg="#ffffff", log_fg="#202020", sel="#cfe3ff"),
    "dark": dict(bg="#1c1c1c", fg="#f0f0f0", muted="#a6a6a6", log_bg="#141414", log_fg="#e6e6e6", sel="#33507a"),
}
BRAND_BG, BRAND_FG, BRAND_GOLD = "#0f2a47", "#ffffff", "#f2c14e"
# White one-colour O.M. Industries logo (transparent PNG, base64) for the header band. 1x = normal screens, 2x = high-DPI. tk.PhotoImage reads PNG natively, so no extra libraries are needed.
LOGO_B64 = {
    "1x": "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",
    "2x": "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",
}


# First line of `ffmpeg -version`, shown in the status line.
def ff_version():
    try:
        r = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        m = re.search(r"ffmpeg version (\S+)", r.stdout)
        return m.group(1) if m else ""
    except Exception:
        return ""


# Build and run the whole window. Returns when the window closes.
def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont

    try:
        import sv_ttk
    except Exception:  # noqa: BLE001
        sv_ttk = None
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
    except Exception:  # noqa: BLE001
        DND_FILES = TkinterDnD = None

    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()
        except Exception:  # noqa: BLE001
            root = None
    dnd = root is not None
    if root is None:
        root = tk.Tk()
    root.title(APP)
    root.geometry("1280x900")
    root.minsize(1020, 700)

    settings = load_settings()
    set_lang(settings.get("lang", "en"))
    if LANG["code"] != "en":
        # Translate every label / button / message at the moment it is created or changed, so the whole window follows
        # the chosen language without touching each widget. Texts with no translation simply stay English.
        for cls in (ttk.Label, ttk.Button, ttk.Checkbutton, ttk.Radiobutton, ttk.LabelFrame, tk.Label, tk.Button):
            def _patch(cls=cls):
                oi, oc = cls.__init__, cls.configure

                def init(self, *a, **k):
                    if "text" in k:
                        k["text"] = tr(k["text"])
                    oi(self, *a, **k)

                def conf(self, cnf=None, **k):
                    if "text" in k:
                        k["text"] = tr(k["text"])
                    return oc(self, cnf, **k) if cnf is not None else oc(self, **k)
                cls.__init__, cls.configure, cls.config = init, conf, conf
            _patch()
        for nm in ("showinfo", "showwarning", "showerror", "askyesno"):
            def _wrap(nm=nm, orig=getattr(messagebox, nm)):
                def f(title=None, message=None, **k):
                    return orig(tr(title), tr(message), **k)
                setattr(messagebox, nm, f)
            _wrap()
        _otitle = tk.Wm.title

        def _title(self, string=None):
            return _otitle(self, tr(string) if string is not None else None)
        tk.Wm.title = _title
        tk.Wm.wm_title = _title
    theme = settings.get("theme", "dark") if sv_ttk else "light"
    if theme not in PALETTE:
        theme = "light"
    style = ttk.Style()
    if sv_ttk:
        try:
            sv_ttk.set_theme(theme)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass

    fsize = {"v": int(settings.get("fsize", 13))}
    f_base = tkfont.Font(family=UI_FONT, size=fsize["v"])
    f_bold = tkfont.Font(family=UI_FONT, size=fsize["v"], weight="bold")
    f_head = tkfont.Font(family=UI_FONT, size=fsize["v"] + 3, weight="bold")
    f_mono = tkfont.Font(family=MONO_FONT, size=fsize["v"] - 1)
    f_title = tkfont.Font(family=UI_FONT, size=fsize["v"] + 9, weight="normal")
    f_brand = tkfont.Font(family=UI_FONT, size=fsize["v"] + 2, weight="normal")
    # Report Tk callback exceptions to the log and a message box instead of failing silently.
    def _tk_error(et, ev, tb):
        import traceback
        write_log("GUI ERROR\n" + "".join(traceback.format_exception(et, ev, tb)))
        sys.__excepthook__(et, ev, tb)
    root.report_callback_exception = _tk_error
    write_log(f"--- {APP} v{VERSION} started ({'installed exe' if FROZEN else 'script'}, python {sys.version.split()[0]}, ffmpeg: {ff_version() if find_tool('ffmpeg') else 'not found'}) ---")
    wrapped, themed_text, themed_canvas = [], [], []
    state = {"running": False, "outputs": [], "outdir": None, "theme": theme, "multi": None, "multi_label": "", "queue": []}

    # Current palette (light or dark).
    def pal():
        return PALETTE[state["theme"]]

    # Scale all named fonts from the A+/A- size setting.
    def apply_fonts():
        n = fsize["v"]
        for f_, extra in ((f_base, 0), (f_bold, 0), (f_head, 3), (f_mono, -1), (f_title, 9), (f_brand, 2)):
            f_.configure(size=n + extra)
        for nm in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(nm).configure(family=UI_FONT, size=n)
            except tk.TclError:
                pass
        for nm, extra in (("SunValleyBodyFont", 0), ("SunValleyCaptionFont", -1), ("SunValleyBodyStrongFont", 0),
                          ("SunValleyBodyLargeFont", 2), ("SunValleySubtitleFont", 4)):
            try:
                tkfont.nametofont(nm).configure(size=n + extra)
            except tk.TclError:
                pass

    # Configure ttk styles (cards, buttons, tabs) from the palette.
    def apply_styles():
        p = pal()
        apply_fonts()
        for st in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry", "TCombobox", "TLabelframe.Label", "Toolbutton"):
            try:
                style.configure(st, font=f_base)
            except tk.TclError:
                pass
        style.configure("Op.TButton", padding=(10, 14), font=f_base)
        style.configure("Run.TButton", padding=(18, 8), font=f_bold)
        try:
            style.configure("Accent.TButton", padding=(18, 8), font=f_bold)
        except tk.TclError:
            pass
        style.configure("Side.Toolbutton", padding=(14, 11), font=f_base, anchor="w")
        style.configure("Muted.TLabel", foreground=p["muted"], font=f_base)
        style.configure("Head.TLabel", font=f_head)
        root.option_add("*TCombobox*Listbox.font", f_base)
        for w in themed_text:
            try:
                w.configure(bg=p["log_bg"], fg=p["log_fg"], insertbackground=p["log_fg"], selectbackground=p["sel"])
            except tk.TclError:
                pass
        for cv_ in themed_canvas:
            try:
                cv_.configure(bg=p["bg"])
            except tk.TclError:
                pass
        for lbl in wrapped:
            lbl.configure(wraplength=max(500, 62 * fsize["v"]))

    # Dark/light native title bar on Windows 10/11 via pywinstyles (best effort).
    def title_bar():
        if os.name != "nt":
            return
        try:
            import pywinstyles
            pywinstyles.apply_style(root, "dark" if state["theme"] == "dark" else "normal")
        except Exception:  # noqa: BLE001
            pass

    apply_styles()
    run_style = "Accent.TButton" if sv_ttk else "Run.TButton"

    # A+/A- buttons: change font size and persist it.
    def set_fsize(delta):
        fsize["v"] = max(10, min(22, fsize["v"] + delta))
        apply_styles()
        settings["fsize"] = fsize["v"]
        save_settings(settings)

    # Switch light/dark (sv_ttk) and re-apply styles and title bar.
    def toggle_theme():
        if not sv_ttk:
            return
        state["theme"] = "dark" if state["theme"] == "light" else "light"
        sv_ttk.set_theme(state["theme"])
        apply_styles()
        theme_btn.configure(text="☾  Dark mode" if state["theme"] == "light" else "☀  Light mode")
        settings["theme"] = state["theme"]
        save_settings(settings)
        title_bar()

    q = queue.Queue()
    cancel = threading.Event()
    have_ff = refresh_tools()
    gpu_key = detect_gpu() if have_ff else None
    gpu_available = gpu_key is not None
    if gpu_key:
        STATE["gpu_enc"] = gpu_key
    path_var = tk.StringVar()
    out_var = tk.StringVar(value=settings.get("outdir", ""))
    gpu_var = tk.BooleanVar(value=bool(gpu_available))  # GPU on by default when a real hardware test encode works; CPU otherwise
    STATE["gpu"] = bool(gpu_available)
    cmd_var = tk.BooleanVar(value=False)
    info_var = tk.StringVar(value="Choose a file or a folder to begin.")
    status_var = tk.StringVar(value=tr("Ready"))
    _sv_set = status_var.set
    status_var.set = lambda v: _sv_set(tr(v))  # status messages follow the chosen language too

    # ------------------------------------------------------------------ header band
    header = tk.Frame(root, bg=BRAND_BG)
    header.pack(fill="x")
    hl = tk.Frame(header, bg=BRAND_BG)
    hl.pack(side="left", padx=20, pady=12)
    tk.Label(hl, text=f"{APP} {VERSION}", bg=BRAND_BG, fg=BRAND_FG, font=f_title).pack(anchor="w")
    tk.Label(hl, text="Video  ·  Audio  ·  Image toolkit", bg=BRAND_BG, fg="#9fb6cf", font=f_base).pack(anchor="w")
    hr = tk.Frame(header, bg=BRAND_BG)
    hr.pack(side="right", padx=20, pady=12)
    # Logo in the middle of the header band (white, so it reads on the dark band in both light and dark mode).
    try:
        hi_dpi = float(root.tk.call("tk", "scaling")) >= 1.7
        logo_img = tk.PhotoImage(data=LOGO_B64["2x" if hi_dpi else "1x"])
        logo_lbl = tk.Label(header, image=logo_img, bg=BRAND_BG, bd=0)
        logo_lbl.image = logo_img  # keep a reference or tk drops the picture
        logo_lbl.place(relx=0.5, rely=0.5, anchor="center")  # truly centred in the window, not just between the two side blocks
        tk.Frame(header, width=1, height=logo_img.height() + 18, bg=BRAND_BG).pack(side="left")  # keeps the band tall enough for the logo
    except Exception:  # noqa: BLE001 - the header simply stays without a logo
        pass
    theme_btn = ttk.Button(hr, text="☾  Dark mode" if theme == "light" else "☀  Light mode", command=toggle_theme)
    if sv_ttk:
        theme_btn.pack(anchor="e", pady=(6, 0))
    lang_var = tk.StringVar(value=LANGS.get(LANG["code"], "English"))
    lang_box = ttk.Combobox(hr, textvariable=lang_var, values=list(LANGS.values()), state="readonly", width=16)
    lang_box.pack(anchor="e", pady=(6, 0))

    # Language combobox handler: save the choice and offer to restart so every widget picks it up.
    def on_lang(_e=None):
        code = next((c for c, n in LANGS.items() if n == lang_var.get()), "en")
        if code == LANG["code"]:
            return
        settings["lang"] = code
        save_settings(settings)
        # the new language is read when the app starts, so offer to restart it right away
        if messagebox.askyesno(APP, "Restart the app now to switch language?\n(Your files and settings are kept.)"):
            try:
                if getattr(sys, "frozen", False):
                    subprocess.Popen([sys.executable])
                else:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve())])
                root.destroy()
            except Exception:  # noqa: BLE001
                messagebox.showinfo(APP, "Please close and reopen the app to use the new language.")
    lang_box.bind("<<ComboboxSelected>>", on_lang)

    # ------------------------------------------------------------------ input card
    card = ttk.LabelFrame(root, text="Input", padding=(12, 8, 12, 8))
    card.pack(fill="x", padx=14, pady=(12, 4))
    card.columnconfigure(1, weight=1)
    ttk.Label(card, text="File or folder:").grid(row=0, column=0, sticky="w")
    ent = ttk.Entry(card, textvariable=path_var)
    ent.grid(row=0, column=1, sticky="ew", padx=8)

    # Set the main Input path and refresh the info strip and recents.
    def set_path(p):
        state["multi"] = None
        path_var.set(p)
        refresh_info()
        if os.path.isfile(p):
            rec = [x for x in settings.get("recent", []) if x != p]
            settings["recent"] = ([p] + rec)[:10]
            recent["values"] = settings["recent"]
            save_settings(settings)

    # Hold a multi-file selection (several files chosen at once).
    def set_multi(files):
        files = [os.path.normpath(f) for f in files]
        state["multi"] = files
        state["multi_label"] = f"{len(files)} files selected: " + ", ".join(Path(f).name for f in files[:3]) + (" ..." if len(files) > 3 else "")
        path_var.set(state["multi_label"])
        info_var.set(f"Batch of {len(files)} files. Pick an operation to run it on all of them (or add it to the queue).")

    # Return the multi-selection if it is still valid.
    def current_multi():
        return state["multi"] if state["multi"] and path_var.get() == state["multi_label"] else None

    # 'Browse files...' dialog (multi-select).
    def browse_file():
        ft = [("Media files", " ".join("*" + e for e in sorted(VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | SUB_EXT))), ("All files", "*.*")]
        ps = filedialog.askopenfilenames(title="Choose one or more files", filetypes=ft, initialdir=settings.get("lastdir") or None)
        if ps:
            settings["lastdir"] = str(Path(ps[0]).parent)
            if len(ps) > 1:
                set_multi(ps)
            else:
                set_path(os.path.normpath(ps[0]))

    # 'Batch folder...' dialog.
    def browse_folder():
        p = filedialog.askdirectory(title="Choose a folder (batch)", initialdir=settings.get("lastdir") or None)
        if p:
            settings["lastdir"] = p
            set_path(os.path.normpath(p))

    ttk.Button(card, text="Browse files…", command=browse_file).grid(row=0, column=2)
    ttk.Button(card, text="Batch folder…", command=browse_folder).grid(row=0, column=3, padx=(6, 0))
    ttk.Label(card, text="Recent:").grid(row=1, column=0, sticky="w", pady=(6, 0))
    recent = ttk.Combobox(card, values=settings.get("recent", []), state="readonly")
    recent.grid(row=1, column=1, columnspan=3, sticky="ew", padx=8, pady=(6, 0))
    recent.bind("<<ComboboxSelected>>", lambda e: set_path(recent.get()))
    ttk.Label(card, text="Save in:").grid(row=2, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(card, textvariable=out_var).grid(row=2, column=1, sticky="ew", padx=8, pady=(6, 0))

    # 'Choose...' for a custom output folder; empty = same folder as the source.
    def browse_out():
        p = filedialog.askdirectory(title="Output folder")
        if p:
            out_var.set(os.path.normpath(p))
            settings["outdir"] = out_var.get()
            save_settings(settings)
    ttk.Button(card, text="Choose…", command=browse_out).grid(row=2, column=2, pady=(6, 0))
    ttk.Button(card, text="Same as source", command=lambda: out_var.set("")).grid(row=2, column=3, padx=(6, 0), pady=(6, 0))
    hint = "Tip: drag a file or folder onto this window." if dnd else "Tip: run  pip install tkinterdnd2  to enable drag-and-drop."
    info_lbl = ttk.Label(card, textvariable=info_var, style="Muted.TLabel", wraplength=1100)
    info_lbl.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
    wrapped.append(info_lbl)
    ttk.Label(card, text=hint, style="Muted.TLabel").grid(row=4, column=0, columnspan=4, sticky="w")
    ent.bind("<Return>", lambda e: refresh_info())

    # Probe the current input in a worker thread and show its one-line summary.
    def refresh_info():
        if current_multi():
            return
        p = path_var.get().strip().strip('"')
        if not have_ff:
            info_var.set("ffmpeg is not installed yet. Use the Install ffmpeg prompt (or restart the app) to download it.")
            return
        if os.path.isdir(p):
            n = sum(1 for f in Path(p).iterdir() if f.suffix.lower() in (VIDEO_EXT | AUDIO_EXT | IMAGE_EXT))
            info_var.set(f"Batch mode: {n} media files in {p}")
        elif os.path.isfile(p):
            info_var.set("Reading file…")

            def work():
                q.put(("info", summary(probe(p), p)))
            threading.Thread(target=work, daemon=True).start()
        elif p:
            info_var.set("That path does not exist.")

    # ------------------------------------------------------------------ bottom (packed first so the body can expand)
    status = ttk.Label(root, textvariable=status_var, style="Muted.TLabel", padding=(14, 4))
    status.pack(side="bottom", fill="x")
    bot = ttk.Frame(root, padding=(14, 0, 14, 4))
    bot.pack(side="bottom", fill="x")
    bar = ttk.Progressbar(bot, maximum=1000)
    bar.pack(fill="x", pady=(4, 0))
    row = ttk.Frame(bot)
    row.pack(fill="x", pady=6)
    cancel_btn = ttk.Button(row, text="Cancel", state="disabled", command=lambda: cancel.set())
    cancel_btn.pack(side="left")
    open_btn = ttk.Button(row, text="Open output folder", state="disabled", command=lambda: open_folder())
    open_btn.pack(side="left", padx=6)
    queue_btn = ttk.Button(row, text="Queue (0)", command=lambda: open_queue())
    queue_btn.pack(side="left")
    ttk.Button(row, text="Copy last command", command=lambda: copy_last()).pack(side="left", padx=6)
    ttk.Checkbutton(row, text="Show ffmpeg commands", variable=cmd_var).pack(side="left", padx=4)
    gpu_cb = ttk.Checkbutton(row, text=(f"Use GPU ({GPU_ENCODERS[gpu_key]['label']})" if gpu_available else "No GPU"),
                             variable=gpu_var, command=lambda: STATE.update(gpu=gpu_var.get()),
                             state=("normal" if gpu_available else "disabled"))
    gpu_cb.pack(side="left")

    # "Check GPU" button: re-runs detection (e.g. after installing a driver), shows the details, and enables/updates the checkbox.
    def check_gpu():
        if not refresh_tools():
            messagebox.showinfo("Check GPU", "ffmpeg was not found, so the GPU cannot be tested yet.")
            return
        gpu_btn.configure(state="disabled", text="Checking...")

        def work():
            text, key = gpu_report()
            root.after(0, lambda: done(text, key))

        def done(text, key):
            gpu_btn.configure(state="normal", text="Check GPU")
            if key:
                STATE["gpu_enc"] = key
                gpu_cb.configure(text=f"Use GPU ({GPU_ENCODERS[key]['label']})", state="normal")
                gpu_var.set(True)
                STATE["gpu"] = True
            else:
                gpu_cb.configure(text="No GPU", state="disabled")
                gpu_var.set(False)
                STATE["gpu"] = False
            messagebox.showinfo("Check GPU", text)
        threading.Thread(target=work, daemon=True).start()
    gpu_btn = ttk.Button(row, text="Check GPU", command=check_gpu)
    gpu_btn.pack(side="left", padx=2)
    snd_var = tk.BooleanVar(value=settings.get("sounds", True))

    # Sound checkbox: persist setting and play a sample ping when switched on.
    def toggle_sound():
        settings["sounds"] = snd_var.get()
        save_settings(settings)
        if snd_var.get():
            play_ping("job")
    ttk.Checkbutton(row, text="Sound", variable=snd_var, command=toggle_sound).pack(side="left", padx=10)
    ttk.Button(row, text="A+", width=4, command=lambda: set_fsize(1)).pack(side="right")
    ttk.Button(row, text="A-", width=4, command=lambda: set_fsize(-1)).pack(side="right", padx=(4, 2))
    ttk.Label(row, text="Text size:").pack(side="right")
    logbox = tk.Text(bot, height=7, wrap="word", font=f_mono, state="disabled", relief="flat", borderwidth=0, padx=8, pady=6)
    logbox.pack(fill="x")
    themed_text.append(logbox)

    # ------------------------------------------------------------------ body: sidebar + content
    body = ttk.Frame(root)
    body.pack(fill="both", expand=True, padx=14, pady=(8, 4))
    body.columnconfigure(1, weight=1)
    body.rowconfigure(0, weight=1)
    scrollers = {}

    # Create a canvas-based vertically scrolling frame (used by category pages and long dialogs).
    def make_scroll(parent, fit_width=False):
        """A frame that gets a vertical scroll bar (only when needed) and mouse-wheel scrolling."""
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        cv = tk.Canvas(outer, highlightthickness=0, borderwidth=0, yscrollincrement=24, bg=pal()["bg"])
        vs = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        inner = ttk.Frame(cv)
        win = cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=vs.set)
        cv.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        themed_canvas.append(cv)

        def sync(_e=None):
            cv.configure(scrollregion=cv.bbox("all"))
            if fit_width:
                cv.configure(width=inner.winfo_reqwidth())
            if inner.winfo_reqheight() <= cv.winfo_height():
                cv.yview_moveto(0)

        def on_canvas(e):
            cv.itemconfigure(win, width=e.width)
            sync()
        inner.bind("<Configure>", sync)
        cv.bind("<Configure>", on_canvas)
        root.after(300, sync)
        scrollers[outer] = (cv, inner)
        return outer, inner

    # Mouse-wheel routing to whichever scroll area is under the pointer.
    def on_wheel(e):
        try:
            w = root.winfo_containing(e.x_root, e.y_root)
        except Exception:  # noqa: BLE001
            return
        while w is not None:
            if w in scrollers:
                cv, inner = scrollers[w]
                if inner.winfo_reqheight() > cv.winfo_height():
                    cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
                    cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
                return
            w = getattr(w, "master", None)
    root.bind_all("<MouseWheel>", on_wheel)

    side_outer, side = make_scroll(body, fit_width=True)
    side_outer.grid(row=0, column=0, sticky="ns", padx=(0, 12))
    content = ttk.Frame(body)
    content.grid(row=0, column=1, sticky="nsew")
    content.columnconfigure(0, weight=1)
    content.rowconfigure(0, weight=1)

    cat_frames, cat_inner = {}, {}
    cat_var = tk.StringVar(value=settings.get("cat", "Video") if settings.get("cat") in CATEGORIES + ["Favourites"] else "Video")
    ops_by_id = {o["id"]: o for o in OPS}

    # Button press handler: checks an input is chosen before opening an operation's option window.
    def open_op(spec):
        """Operation button pressed. If no input file/folder is chosen yet, play the Windows warning sound, tell the user
        and put the cursor in the Input box instead of opening the option window. Operations that let you list their own
        clips/photos inside their window (join, crossfade, slideshow: folder_one) may start with an empty Input box."""
        p = path_var.get().strip().strip('"')
        if not spec.get("folder_one") and not current_multi() and not (p and os.path.exists(p)):
            if not play_alert():  # standard Windows / macOS alert sound
                root.bell()
            ent.focus_set()
            messagebox.showwarning(APP, "Please select an input file or folder first.\n\nUse Browse files..., Batch folder..., or drag a file onto this window.")
            return
        open_dialog(spec)

    # Lay operation buttons out in a responsive grid.
    def fill_buttons(fr, ops, start_row=1):
        for i, spec in enumerate(ops):
            b = ttk.Button(fr, text=spec["label"].replace(" (", "\n("), style="Op.TButton", command=lambda s=spec: open_op(s))
            b.grid(row=start_row + i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
        for col in range(3):
            fr.columnconfigure(col, weight=1, uniform="c")
        for rw in range((len(ops) + 2) // 3):
            fr.rowconfigure(start_row + rw, uniform="rows", minsize=78)

    # Build one category page (its button grid and description).
    def build_cat(cat):
        if cat not in cat_frames:
            outer_, inner_ = make_scroll(content)
            outer_.grid(row=0, column=0, sticky="nsew")
            cat_frames[cat], cat_inner[cat] = outer_, inner_
        fr = cat_inner[cat]
        for w in fr.winfo_children():
            w.destroy()
        if cat == "Favourites":
            ops = [ops_by_id[i] for i in settings.get("favs", []) if i in ops_by_id]
        else:
            ops = [o for o in OPS if o["cat"] == cat]
        ttk.Label(fr, text=f"{CAT_ICONS.get(cat, '')}  {cat}", style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        if not ops:
            ttk.Label(fr, text="No favourites yet.\nOpen any operation and tick ★ Add to favourites.", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=20)
        else:
            fill_buttons(fr, ops)
        register_drop_tree(fr)

    # Switch the visible category page.
    def show_cat():
        settings["cat"] = cat_var.get()
        save_settings(settings)
        fr = cat_frames.get(cat_var.get())
        if fr is not None:
            fr.tkraise()

    # Drag-and-drop a file/folder onto the window (tkinterdnd2).
    def on_drop(event):
        try:
            files = root.tk.splitlist(event.data)
        except Exception:  # noqa: BLE001
            files = [event.data]
        if files:
            if len(files) > 1 and all(os.path.isfile(f) for f in files):
                set_multi(files)
            else:
                set_path(os.path.normpath(files[0]))
                if len(files) > 1:
                    log(f"You dropped {len(files)} items including a folder; using the first. Drop several files together to batch them.")

    # Make every child widget a drop target too.
    def register_drop_tree(w):
        if not dnd:
            return
        try:
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", on_drop)
        except Exception:  # noqa: BLE001
            pass
        for c in w.winfo_children():
            register_drop_tree(c)

    for cat in ["Favourites"] + CATEGORIES:
        ttk.Radiobutton(side, text=f"{CAT_ICONS.get(cat, '')}  {cat}", variable=cat_var, value=cat, style="Side.Toolbutton",
                        width=22, command=show_cat).pack(fill="x", pady=2)
    # ------------------------------------------------------------------ helpers used by dialogs / worker
    # Append a line to the log box (always called on the GUI thread via the queue).
    def log(msg):
        q.put(("log", msg))

    # 'Open output folder' button (Explorer / xdg-open / open).
    def open_folder():
        d = state["outdir"]
        if d and os.path.isdir(d):
            if os.name == "nt":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["open" if IS_MAC else "xdg-open", d])

    # Heart of thread safety: worker threads never touch Tk. They put tuples on the
    #  queue `q` - ('log', text), ('prog', fraction), ('info', text), ('call', fn),
    #  ('done', ok, fail) - and pump() drains it every 100 ms on the GUI thread.
    #  The 'done' branch re-enables buttons, plays the completion ping and updates
    #  the status line.
    def pump():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", val + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                elif kind == "prog":
                    bar["value"] = val * 1000
                elif kind == "info":
                    info_var.set(val)
                elif kind == "call":
                    try:
                        val()
                    except Exception:  # noqa: BLE001
                        pass
                elif kind == "done":
                    state["running"] = False
                    cancel_btn.configure(state="disabled")
                    ok, fail, outs = val
                    play_ping(ping_kind(ok, fail, state.get("batch", False) or ok + fail > 1))
                    if outs:
                        state["outdir"] = str(Path(outs[-1]).parent if not os.path.isdir(outs[-1]) else outs[-1])
                        open_btn.configure(state="normal")
                    logbox.configure(state="normal")
                    logbox.insert("end", f"\nFinished: {ok} ok, {fail} failed.\n")
                    for o in outs[:20]:
                        logbox.insert("end", f"  -> {o}\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                    status_var.set(f"Finished: {ok} ok, {fail} failed")
        except queue.Empty:
            pass
        root.after(100, pump)

    # Map an option's ftype to the file-dialog filter list.
    def ft_for(t):
        ex = {"audio": AUDIO_EXT | VIDEO_EXT, "image": IMAGE_EXT, "video": VIDEO_EXT, "sub": SUB_EXT, "lut": {".cube", ".3dl"},
              "media": VIDEO_EXT | AUDIO_EXT | IMAGE_EXT}.get(t)
        return ([("Files", " ".join("*" + e for e in sorted(ex))), ("All files", "*.*")] if ex else [("All files", "*.*")])

    # ------------------------------------------------------------------ option dialog
    # The per-operation window: builds one widget per option in spec['opts'] (entry,
    #  spinbox-like int/float, checkbox, combobox, file picker, multi-file list),
    #  adds sliders where SLIDERS defines them, a tip line, the Favourite star, and
    #  the buttons: Copy ffmpeg command / Add to queue / Run. `collect()` reads the
    #  widgets back into a raw dict; `go(mode)` validates, then runs, queues or
    #  copies. Last-used values are remembered per operation.
    def open_dialog(spec):
        top_ = tk.Toplevel(root)
        top_.title(spec["label"])
        top_.transient(root)
        try:
            top_.configure(bg=pal()["bg"])
        except tk.TclError:
            pass
        try:
            top_.grab_set()  # modal; on some desktops the window is not viewable yet, so retry shortly
        except tk.TclError:
            top_.after(150, lambda: top_.winfo_exists() and top_.grab_set())
        frm = ttk.Frame(top_, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=spec["label"], style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        r = 1
        if spec["tip"]:
            ttk.Label(frm, text=spec["tip"], style="Muted.TLabel", wraplength=max(500, 52 * fsize["v"])).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 8))
            r += 1
        last = settings.get("last", {}).get(spec["id"], {})
        getters = {}
        tkvars = {}
        meta = {"dur": 0.0, "w": 0, "h": 0}
        if any((spec["id"], o_["key"]) in SLIDERS for o_ in spec["opts"]) and preview_source():
            try:
                inf = probe(preview_source())
                meta = {"dur": float(inf["dur"] or 0), "w": int(inf["w"] or 0), "h": int(inf["h"] or 0)}
            except Exception:  # noqa: BLE001
                pass
        for s in spec["opts"]:
            ttk.Label(frm, text=s["label"]).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 12))
            t = s["type"]
            dflt = last.get(s["key"], s["default"]) if t not in ("file", "files") else s["default"]
            if t == "choice":
                if dflt not in s["choices"]:
                    dflt = s["default"]
                v = tk.StringVar(value=dflt)
                ttk.Combobox(frm, textvariable=v, values=s["choices"], state="readonly", width=38).grid(row=r, column=1, columnspan=2, sticky="ew")
                getters[s["key"]] = v.get
                tkvars[s["key"]] = v
            elif t == "bool":
                v = tk.BooleanVar(value=bool(dflt))
                ttk.Checkbutton(frm, variable=v).grid(row=r, column=1, sticky="w")
                getters[s["key"]] = v.get
                tkvars[s["key"]] = v
            elif t == "file":
                v = tk.StringVar()
                ttk.Entry(frm, textvariable=v, width=34).grid(row=r, column=1, sticky="ew")
                ttk.Button(frm, text="…", width=3, command=lambda v=v, t=s["ftype"]: v.set(filedialog.askopenfilename(filetypes=ft_for(t)) or v.get())).grid(row=r, column=2, padx=4)
                getters[s["key"]] = v.get
                tkvars[s["key"]] = v
            elif t == "files":
                lb = tk.Listbox(frm, height=5, width=40, relief="flat", bg=pal()["log_bg"], fg=pal()["log_fg"], selectbackground=pal()["sel"], highlightthickness=1)
                lb.grid(row=r, column=1, sticky="ew")
                top_.om_files_lb = lb  # the clip timeline (Join) reads and re-orders this list
                bf = ttk.Frame(frm)
                bf.grid(row=r, column=2, padx=4)
                ttk.Button(bf, text="Add…", command=lambda lb=lb, kind=spec["kind"]: [lb.insert("end", os.path.normpath(p)) for p in filedialog.askopenfilenames(filetypes=ft_for("image" if kind == "image" else "media"))] + [top_.event_generate("<<FilesChanged>>")]).pack(fill="x")

                def add_folder(lb=lb, kind=spec["kind"]):
                    d = filedialog.askdirectory(title="Add every file in a folder", initialdir=settings.get("lastdir") or None)
                    if d:
                        ex = kind_exts(kind)
                        for f in sorted(Path(d).iterdir()):
                            if f.is_file() and f.suffix.lower() in ex:
                                lb.insert("end", os.path.normpath(str(f)))
                        top_.event_generate("<<FilesChanged>>")
                ttk.Button(bf, text="Add folder…", command=add_folder).pack(fill="x", pady=(2, 0))
                ttk.Button(bf, text="Remove", command=lambda lb=lb: [lb.delete(i) for i in reversed(lb.curselection())] + [top_.event_generate("<<FilesChanged>>")]).pack(fill="x", pady=2)
                getters[s["key"]] = lambda lb=lb: list(lb.get(0, "end"))
            else:
                v = tk.StringVar(value=str(dflt))
                ttk.Entry(frm, textvariable=v, width=40).grid(row=r, column=1, columnspan=2, sticky="ew")
                getters[s["key"]] = v.get
                tkvars[s["key"]] = v
                cfg = SLIDERS.get((spec["id"], s["key"]))
                if cfg:
                    try:
                        if add_slider(frm, r + 1, v, cfg, meta, tkvars, s["type"] == "int"):
                            r += 1
                    except Exception as e:  # noqa: BLE001
                        write_log(f"slider setup failed for {spec['id']}.{s['key']}: {e!r}")
            r += 1
        if not spec["opts"]:
            ttk.Label(frm, text="No options - just press Run.").grid(row=r, column=0, columnspan=3, sticky="w", pady=6)
            r += 1
        fav_var = tk.BooleanVar(value=spec["id"] in settings.get("favs", []))
        ttk.Checkbutton(frm, text="★  Add to favourites", variable=fav_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))
        bf = ttk.Frame(frm)
        bf.grid(row=r, column=1, columnspan=2, sticky="e", pady=(10, 0))

        def collect():
            raw = {k: g() for k, g in getters.items()}
            try:
                opts = convert_opts(spec, raw)
            except ValueError:
                messagebox.showerror(APP, "One of the numbers is not valid.", parent=top_)
                return None
            return raw, opts

        def go(mode="run"):
            if mode == "run" and state["running"]:
                messagebox.showinfo(APP, "A job is already running. Use 'Add to queue' to line this one up.", parent=top_)
                return
            got = collect()
            if got is None:
                return
            raw, opts = got
            prep = prepare(spec, opts, top_)
            if prep is None:
                return
            favs = [i for i in settings.get("favs", []) if i != spec["id"]]
            if fav_var.get():
                favs.append(spec["id"])
            changed = favs != settings.get("favs", [])
            settings["favs"] = favs
            settings.setdefault("last", {})[spec["id"]] = {k: v for k, v in raw.items() if not isinstance(v, list)
                                                              and next(x for x in spec["opts"] if x["key"] == k)["type"] != "file"}
            save_settings(settings)
            top_.destroy()
            if changed:
                build_cat("Favourites")
            if mode == "queue":
                enqueue(spec, prep)
            else:
                start(spec, prep)

        def copy_cmd():
            got = collect()
            if got is None:
                return
            prep = prepare(spec, got[1], top_)
            if prep is None:
                return
            files, opts2, outdir = prep
            try:
                text = format_commands(spec, opts2, files[0], outdir)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(APP, f"Couldn't build the command: {e}", parent=top_)
                return
            copy_text(text, "the ffmpeg command" + (f" (for {Path(files[0]).name})" if len(files) > 1 else ""))
            log("Copied ffmpeg command:\n" + text)
        if spec["id"] in SAMPLE_OPS and have_ff:
            smp = {"proc": None, "busy": False, "dir": None}
            secs_ = 10 if spec["id"] in ("loudnorm", "volume", "tempo", "pitch", "denoise", "eq", "fade", "mix", "narration") else 5

            def stop_sample():
                p_ = smp["proc"]
                smp["proc"] = None
                if p_ is not None and p_.poll() is None:
                    try:
                        p_.terminate()
                        p_.wait(timeout=3)
                    except (OSError, subprocess.SubprocessError):
                        pass

            def play_sample():
                if smp["busy"]:
                    return
                got = collect()
                if got is None:
                    return
                prep = prepare(spec, got[1], top_)
                if prep is None:
                    return
                files, opts2, _outdir = prep
                src_ = files[0]
                ff = find_tool("ffplay") or str(Path(FFMPEG).with_name("ffplay" + (".exe" if os.name == "nt" else "")))
                if not (os.path.exists(ff) or shutil.which(ff)):
                    messagebox.showinfo(APP, "ffplay (the player that comes with ffmpeg) was not found, so a sample can't be played. "
                                        "The ffmpeg that this app downloads includes it.", parent=top_)
                    return
                stop_sample()
                if not smp["dir"]:
                    smp["dir"] = tempfile.mkdtemp(prefix="ffsample_")
                smp["busy"] = True
                sbtn.configure(text="Making the sample…", state="disabled")

                def work():
                    path, audio, err = None, False, ""
                    try:
                        dur_ = float(probe(src_)["dur"] or 0)
                        path, audio = make_sample(spec, opts2, src_, sample_start(spec, opts2, dur_, secs_), secs_, smp["dir"])
                    except ValueError as e:
                        err = str(e)
                    except Exception as e:  # noqa: BLE001
                        err = f"Couldn't make the sample: {e}"
                    q.put(("call", lambda: finished(path, audio, err)))

                def finished(path, audio, err):
                    smp["busy"] = False
                    if not top_.winfo_exists():
                        return
                    sbtn.configure(text=f"▶  Play {secs_} s sample", state="normal")
                    if err:
                        messagebox.showinfo(APP, err, parent=top_)
                        return
                    cmd = [ff, "-hide_banner", "-loglevel", "error", "-autoexit", "-window_title", "Sample - close this window to stop"]
                    cmd += ["-nodisp"] if audio else ["-x", "960"]
                    try:
                        smp["proc"] = subprocess.Popen(cmd + ["-i", path], creationflags=NO_WINDOW)
                    except OSError as e:
                        messagebox.showinfo(APP, f"Couldn't start the player: {e}", parent=top_)
                threading.Thread(target=work, daemon=True).start()

            def sample_cleanup(e):
                if e.widget is top_:
                    stop_sample()
                    if smp["dir"]:
                        shutil.rmtree(smp["dir"], ignore_errors=True)
            sbtn = ttk.Button(bf, text=f"▶  Play {secs_} s sample", command=play_sample)
            sbtn.pack(side="left", padx=(0, 6))
            top_.bind("<Destroy>", sample_cleanup, add="+")
        ttk.Button(bf, text="Copy ffmpeg command", command=copy_cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="Add to queue", command=lambda: go("queue")).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="Cancel", command=top_.destroy).pack(side="left", padx=6)
        ttk.Button(bf, text="Run", style=run_style, command=go).pack(side="left")
        top_.bind("<Return>", lambda e: go())
        top_.bind("<Escape>", lambda e: top_.destroy())
        if spec["id"] in ("trim", "crop") or spec["id"] in STRIP_OPS or spec["kind"] in ("video", "image"):
            try:
                attach_preview(top_, frm, spec, getters, tkvars, r + 1)
            except Exception as e:  # noqa: BLE001
                write_log(f"preview setup failed: {e!r}")
        try:
            top_.update_idletasks()
            x = root.winfo_rootx() + max(0, (root.winfo_width() - top_.winfo_reqwidth()) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - top_.winfo_reqheight()) // 3)
            top_.geometry(f"+{x}+{y}")
        except Exception:  # noqa: BLE001
            pass

    # Validate input, convert options, resolve files and output folder; returns a 'prep' bundle or shows a message box and returns None.
    def prepare(spec, opts, parent=None):
        """Validate input and work out the files. Returns (files, opts, outdir) or None (after telling the user)."""
        if not have_ff:
            messagebox.showerror(APP, "ffmpeg is not installed yet. Restart the app and choose Install ffmpeg when asked.", parent=parent)
            return None
        p = path_var.get().strip().strip('"')
        files, opts2, err = resolve_files(spec, opts, p, current_multi())
        if err:
            messagebox.showwarning(APP, err, parent=parent)
            return None
        outdir = out_var.get().strip() or None
        if outdir:
            os.makedirs(outdir, exist_ok=True)
        return files, opts2, outdir

    # Disable buttons, reset progress and remember whether this run is a batch (decides which ping plays).
    def begin_run(label, batch=False):
        state.update(running=True, outputs=[], batch=batch)
        cancel.clear()
        cancel_btn.configure(state="normal")
        open_btn.configure(state="disabled")
        bar["value"] = 0
        status_var.set(f"Running: {label}")

    # Run one operation on a worker thread. Refuses if a job is already running.
    def start(spec, prep):
        files, opts, outdir = prep
        begin_run(spec["label"])
        log(f"\n##### {spec['label']}  ({len(files)} file{'s' if len(files) > 1 else ''})")

        def work():
            res = execute(spec, opts, files, outdir, log, lambda f: q.put(("prog", f)), cancel, cmd_var.get())
            q.put(("done", res))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ copy command
    # Clipboard helper with a friendly fallback message.
    def copy_text(text, what):
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            status_var.set(f"Copied {what} to the clipboard")
        except tk.TclError:
            messagebox.showinfo(APP, "Couldn't reach the clipboard. The command is in the log below instead.")

    # 'Copy last command' button.
    def copy_last():
        if not LAST_CMDS:
            messagebox.showinfo(APP, "Nothing to copy yet - run something first (or use 'Copy ffmpeg command' in an operation's window).")
            return
        copy_text("\n".join(LAST_CMDS), "the last ffmpeg command")

    # ------------------------------------------------------------------ job queue
    qwin = {"w": None, "refresh": None}

    # Update the 'Queue (n)' button label.
    def refresh_queue_btn():
        queue_btn.configure(text=f"Queue ({len(state['queue'])})")
        if qwin["refresh"]:
            try:
                qwin["refresh"]()
            except tk.TclError:
                pass

    # Add the prepared operation to the queue (stored by op id + options + files).
    def enqueue(spec, prep):
        files, opts, outdir = prep
        state["queue"].append(dict(spec=spec, opts=opts, files=files, outdir=outdir))
        refresh_queue_btn()
        msg = f"Added to queue: {spec['label']} ({len(files)} file{'s' if len(files) != 1 else ''}) - {len(state['queue'])} waiting."
        status_var.set(msg)
        log(msg)

    # Run the whole queue on a worker thread.
    def run_queue_gui():
        if state["running"]:
            messagebox.showinfo(APP, "A job is already running. Start the queue when it finishes.")
            return
        if not state["queue"]:
            messagebox.showinfo(APP, "The queue is empty.")
            return
        items = list(state["queue"])
        state["queue"] = []
        refresh_queue_btn()
        begin_run(f"queue of {len(items)} job(s)", batch=True)

        def work():
            res = run_queue(items, log, lambda f: q.put(("prog", f)), cancel, cmd_var.get())
            q.put(("done", res))
        threading.Thread(target=work, daemon=True).start()

    # Queue window: list, move up/down, remove, clear, run.
    def open_queue():
        if qwin["w"] is not None:
            try:
                if qwin["w"].winfo_exists():
                    qwin["w"].lift()
                    return
            except tk.TclError:
                pass
        w = tk.Toplevel(root)
        qwin["w"] = w
        w.title("Job queue")
        w.transient(root)
        try:
            w.configure(bg=pal()["bg"])
        except tk.TclError:
            pass
        fr = ttk.Frame(w, padding=16)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="Job queue", style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(fr, text="Jobs run one after another, in this order. Add jobs with 'Add to queue' in any operation's window.",
                  style="Muted.TLabel", wraplength=560).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        lb = tk.Listbox(fr, height=12, width=78, relief="flat", bg=pal()["log_bg"], fg=pal()["log_fg"],
                        selectbackground=pal()["sel"], highlightthickness=1, activestyle="none")
        lb.grid(row=2, column=0, sticky="nsew")
        side_ = ttk.Frame(fr)
        side_.grid(row=2, column=1, sticky="n", padx=(10, 0))

        def fill():
            sel = lb.curselection()
            lb.delete(0, "end")
            for i, it in enumerate(state["queue"]):
                n = len(it["files"])
                first = Path(it["files"][0]).name
                lb.insert("end", f"{i + 1}.  {it['spec']['label']}  -  " + (f"{n} files, starting {first}" if n > 1 else first))
            if sel and sel[0] < lb.size():
                lb.selection_set(sel[0])

        qwin["refresh"] = fill

        def move(d):
            sel = lb.curselection()
            if not sel:
                return
            i, j = sel[0], sel[0] + d
            qq = state["queue"]
            if 0 <= j < len(qq):
                qq[i], qq[j] = qq[j], qq[i]
                fill()
                lb.selection_clear(0, "end")
                lb.selection_set(j)

        def remove():
            for i in reversed(lb.curselection()):
                del state["queue"][i]
            refresh_queue_btn()

        def clear():
            state["queue"] = []
            refresh_queue_btn()

        def run_it():
            if state["running"]:
                messagebox.showinfo(APP, "A job is already running. Start the queue when it finishes.", parent=w)
                return
            if not state["queue"]:
                messagebox.showinfo(APP, "The queue is empty.", parent=w)
                return
            w.destroy()
            run_queue_gui()
        for text, cmd in (("Move up", lambda: move(-1)), ("Move down", lambda: move(1)), ("Remove", remove), ("Clear all", clear)):
            ttk.Button(side_, text=text, command=cmd, width=12).pack(pady=2)
        bf2 = ttk.Frame(fr)
        bf2.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bf2, text="Close", command=w.destroy).pack(side="left", padx=6)
        ttk.Button(bf2, text="Run queue", style=run_style, command=run_it).pack(side="left")
        w.bind("<Escape>", lambda e: w.destroy())
        w.bind("<Destroy>", lambda e: qwin.update(refresh=None) if e.widget is w else None)
        fill()

    # ------------------------------------------------------------------ live preview (trim / crop)
    # Which file the live preview should read (first of the selection / folder).
    def preview_source():
        try:
            src = (current_multi() or [path_var.get().strip().strip('"')])[0]
            if os.path.isdir(src):
                vids = [str(f) for f in sorted(Path(src).iterdir()) if f.is_file() and f.suffix.lower() in VIDEO_EXT]
                src = vids[0] if vids else ""
            return src if (have_ff and src and os.path.isfile(src)) else ""
        except Exception:  # noqa: BLE001
            return ""

    # Create one slider bound to a text box. The two stay in sync in both directions
    #  (typing moves the slider, dragging rewrites the text) with a lock flag to stop
    #  feedback loops; +/- nudge buttons give fine control. Kinds are described at
    #  SLIDERS above.
    def add_slider(frm, row, var, cfg, meta, tkvars, is_int=False):
        """A slider kept in step with a text box. cfg = (kind, low, high); meta = clip duration / size. Returns False if not usable."""
        kind, lo, hi = cfg
        if kind == "inum":
            kind, is_int = "num", True
        dur, W, H = meta["dur"], meta["w"], meta["h"]
        if kind in ("pos", "posend"):
            if dur <= 0:
                return False
            lo, hi = 0.0, dur
        elif hi is None:
            hi = {"cw": W, "cx": W, "ch": H, "cy": H}.get(kind) or dur
        if kind in ("len", "num") and meta["dur"] > 0 and cfg[2] is not None and kind == "len":
            hi = min(hi, max(lo + 0.1, dur))
        if not hi or hi <= lo:
            return False
        timey = kind in ("pos", "posend", "len")
        rowf = ttk.Frame(frm)
        rowf.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(0, 2))
        sc = ttk.Scale(rowf, from_=lo, to=hi, orient="horizontal")
        read = ttk.Label(rowf, text="", style="Muted.TLabel", width=11, anchor="e")
        lock = {"on": False}

        def env():
            e = {"iw": W, "ih": H, "in_w": W, "in_h": H}
            for k, nm in (("w", "ow"), ("h", "oh")):
                if k in tkvars:
                    v_ = eval_expr(tkvars[k].get(), e)
                    if v_ is not None:
                        e[nm] = v_
            return e

        def value_of_text():
            txt = str(var.get()).strip()
            if kind in ("pos", "posend"):
                if not txt:
                    return hi if kind == "posend" else 0.0
                return tsec(txt)
            if kind in ("cw", "ch", "cx", "cy"):
                return eval_expr(txt, env())
            return float(txt)

        def text_of_value(t):
            if kind == "posend" and t >= hi - 0.05:
                return ""
            if kind in ("pos", "posend"):
                return fmt_ms(t)
            if kind in ("cw", "ch", "cx", "cy"):
                return str(max(0, int(round(t / 2.0)) * 2))
            if is_int:
                return str(int(round(t)))
            return f"{t:.3f}".rstrip("0").rstrip(".") if lo < 1 or hi < 100 else f"{t:.1f}".rstrip("0").rstrip(".")

        def set_read(t):
            read.configure(text=(fmt_ms(t) if kind == "len" else (f"/ {fmt_ms(dur)}" if kind in ("pos", "posend") else "")))

        def from_scale(val):
            if lock["on"]:
                return
            t = float(val)
            lock["on"] = True
            try:
                var.set(text_of_value(t))
                set_read(t)
            finally:
                lock["on"] = False

        def from_var(*_):
            if lock["on"]:
                return
            try:
                t = value_of_text()
            except ValueError:
                return
            if t is None:
                return
            lock["on"] = True
            try:
                sc.set(min(max(t, lo), hi))
                set_read(t)
            finally:
                lock["on"] = False

        def nudge(d):
            try:
                t = value_of_text()
            except ValueError:
                return
            if t is None:
                return
            t = min(max(t + d, lo), hi)
            lock["on"] = True
            try:
                var.set(text_of_value(t))
                sc.set(t)
                set_read(t)
            finally:
                lock["on"] = False
            var.set(var.get())  # one write so the live preview refreshes
        sc.configure(command=from_scale)
        if timey:
            for text, d in (("-1s", -1.0), ("-.1s", -0.1), ("+.1s", 0.1), ("+1s", 1.0)):
                ttk.Button(rowf, text=text, width=4, command=lambda d=d: nudge(d)).pack(side="right", padx=1)
        read.pack(side="right", padx=(6, 0))
        sc.pack(side="left", fill="x", expand=True)
        var.trace_add("write", from_var)
        if kind in ("pos", "posend") and str(var.get()).strip():
            try:
                lock["on"] = True
                var.set(fmt_ms(tsec(var.get())))
            except ValueError:
                pass
            finally:
                lock["on"] = False
        from_var()
        return True

    # Film strip for Trim (CapCut style): thumbnails across the whole video, a time ruler, two
    #  draggable handles for Start and End (the parts being cut out are dimmed) and a hover
    #  line with the time. It reads and writes the same Start/End text boxes as the sliders,
    #  so all of them stay in sync. st["focus"] says which handle was moved last (the big
    #  preview shows that frame).
    def attach_strip(top_, box, tkvars, cur_src, st, q_, pw, cfg=None):
        cfg = cfg or STRIP_OPS["trim"]
        KA, KB, WINDOW = cfg["a"], cfg["b"], cfg["mode"] == "window"

        def num(t):
            return (f"{max(0.0, t):.3f}".rstrip("0").rstrip(".")) or "0"
        FMT = {"ms": fmt_ms, "num": num}
        PAD, TH0 = 8, 48
        RULER = f_base.metrics("linespace") + 8  # the time labels use the same font as the "Clip length" line
        max_labels = max(4, int((pw - 2 * PAD) / (f_base.measure("0:00:00") + 22)))
        S = {"img": None, "dur": 0.0, "sw": pw - 2 * PAD, "th": TH0, "msg": "Building film strip…", "drag": None, "prev": (None, None)}
        cv = tk.Canvas(box, width=pw, height=RULER + TH0 + 6, highlightthickness=0, bg=pal()["log_bg"], cursor="hand2")
        cv.pack(pady=(0, 6))

        # Play selection: opens ffplay in its own window with sound, playing only the part between
        #  the two handles. (tkinter has no built-in video player, so this is the closest thing.)
        play = {"proc": None}

        def stop_play():
            p_ = play["proc"]
            play["proc"] = None
            if p_ is not None and p_.poll() is None:
                try:
                    p_.terminate()
                except OSError:
                    pass

        def play_sel():
            stop_play()
            src = cur_src()
            if not src or S["dur"] <= 0:
                messagebox.showinfo(APP, "Choose a video first (and wait for the film strip to appear).", parent=top_)
                return
            ff = find_tool("ffplay") or str(Path(FFMPEG).with_name("ffplay" + (".exe" if os.name == "nt" else "")))
            if not (os.path.exists(ff) or shutil.which(ff)):
                messagebox.showinfo(APP, "ffplay (the player that comes with ffmpeg) was not found, so the selection can't be played here. "
                                    "The ffmpeg that this app downloads includes it.", parent=top_)
                return
            s_, e_ = times()
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-autoexit", "-ss", f"{s_:.3f}", "-t", f"{max(0.1, e_ - s_):.3f}",
                   "-window_title", "Your selection - close this window to stop"]
            if cfg.get("audio"):
                cmd += ["-nodisp"]
            else:
                cmd += ["-x", "960"] if S.get("vw", 16) >= S.get("vh", 9) else ["-y", "720"]
            try:
                play["proc"] = subprocess.Popen(cmd + ["-i", src], creationflags=NO_WINDOW)
            except OSError as e:
                messagebox.showinfo(APP, f"Couldn't start the player: {e}", parent=top_)
        brow = ttk.Frame(box)
        brow.pack(fill="x", pady=(0, 6))
        ttk.Button(brow, text="▶  Play selection (with sound)", command=play_sel).pack(side="left")
        ttk.Button(brow, text="■  Stop", command=stop_play).pack(side="left", padx=(6, 0))
        top_.bind("<Destroy>", lambda e: stop_play() if e.widget is top_ else None, add="+")

        def clock(t):
            t = int(round(t))
            return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"

        def times():
            d = S["dur"]
            try:
                s_ = tsec(tkvars[KA].get() or "0")
            except ValueError:
                s_ = 0.0
            try:
                et = str(tkvars[KB].get()).strip()
                if WINDOW:
                    e_ = s_ + max(0.1, float(et or 0))
                else:
                    e_ = tsec(et) if et else d
            except ValueError:
                e_ = d
            s_ = min(max(s_, 0.0), d)
            e_ = min(max(e_, 0.0), d)
            return s_, max(e_, s_)

        def x_of(t):
            return PAD + (t / S["dur"] * S["sw"] if S["dur"] > 0 else 0)

        def t_of(x):
            return min(max((x - PAD) / S["sw"] * S["dur"], 0.0), S["dur"]) if S["dur"] > 0 else 0.0

        def alive():
            try:
                return bool(cv.winfo_exists())
            except tk.TclError:
                return False

        def redraw(*_):
            if not alive():
                return
            pl = pal()
            cv.delete("all")
            cv.configure(bg=pl["log_bg"])
            d = S["dur"]
            h_ = RULER + S["th"]
            if d <= 0 or S["img"] is None:
                cv.create_text(int(cv.cget("width")) // 2, h_ // 2 + 3, text=S["msg"], fill=pl["muted"])
                return
            cv.create_image(PAD, RULER, image=S["img"], anchor="nw")
            step = next((s_ for s_ in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600) if d / s_ <= max_labels), 7200)
            t = 0
            while t <= d + 1e-6:
                x = x_of(t)
                cv.create_line(x, RULER - 5, x, RULER, fill=pl["muted"])
                cv.create_text(x + 3, RULER - 3, text=clock(t), anchor="sw", fill=pl["fg"] if "fg" in pl else pl["muted"], font=f_base)
                t += step
            s_, e_ = times()
            xs, xe = x_of(s_), x_of(e_)
            cv.create_rectangle(PAD, RULER, xs, h_, fill="#000000", stipple="gray50", width=0)
            cv.create_rectangle(xe, RULER, PAD + S["sw"], h_, fill="#000000", stipple="gray50", width=0)
            cv.create_rectangle(xs, RULER, xe, h_, outline=BRAND_GOLD, width=2)
            for x, side in ((xs, -1), (xe, 1)):
                x0, x1 = (x - 8, x) if side < 0 else (x, x + 8)
                cv.create_rectangle(x0, RULER - 2, x1, h_ + 2, fill=BRAND_GOLD, outline=BRAND_GOLD)
                cv.create_line(x0 + 4, RULER + 10, x0 + 4, h_ - 8, fill="#5a4300")

        def track(*_):
            """Remember which handle changed last (the big preview follows it), then redraw."""
            if S["dur"] > 0:
                s_, e_ = times()
                ps, pe = S["prev"]
                if ps is not None:
                    if abs(s_ - ps) > 1e-6:
                        st["focus"] = "start"
                    elif abs(e_ - pe) > 1e-6:
                        st["focus"] = "end"
                S["prev"] = (s_, e_)
                st["focus_t"] = s_ if st["focus"] == "start" else e_
                if st.get("scrub_show"):
                    st["scrub_show"](s_ if st["focus"] == "start" else e_)
            redraw()

        def hover(x):
            cv.delete("hov")
            if S["dur"] <= 0 or S["img"] is None:
                return
            x = min(max(x, PAD), PAD + S["sw"])
            cv.create_line(x, RULER, x, RULER + S["th"], fill="#ffffff", tags="hov")
            lab = fmt_ms(t_of(x))
            wlab = f_base.measure("000:00:000") + 10
            lx = min(x + 6, int(cv.cget("width")) - wlab - 2)
            ly = RULER + S["th"] - f_base.metrics("linespace") // 2 - 4
            bg_ = cv.create_rectangle(lx - 3, ly - f_base.metrics("linespace") // 2 - 1, lx + wlab - 3, ly + f_base.metrics("linespace") // 2 + 2,
                                      fill="#000000", outline="", tags="hov")
            cv.create_text(lx, ly, text=lab, anchor="w", fill="#ffffff", font=f_base, tags="hov")

        def press(e):
            if S["dur"] <= 0 or S["img"] is None:
                return
            s_, e_ = times()
            ds, de = abs(e.x - x_of(s_)), abs(e.x - x_of(e_))
            S["drag"] = "start" if ds <= de else "end"
            move(e)

        def move(e):
            if not S["drag"]:
                hover(e.x)
                return
            t = t_of(e.x)
            s_, e_ = times()
            if S["drag"] == "start":
                t = min(t, max(0.0, e_ - 0.1))
                if WINDOW:  # the length follows, so the END stays where it is
                    tkvars[KB].set(num(max(0.1, e_ - t)))
                tkvars[KA].set(FMT[cfg["fa"]](t))
            else:
                t = max(t, s_ + 0.1)
                if WINDOW:
                    tkvars[KB].set(num(min(30.0, max(0.1, t - s_))))  # a GIF longer than 30 s would be enormous
                elif cfg["blank"] and t >= S["dur"] - 0.05:
                    tkvars[KB].set("")
                else:
                    tkvars[KB].set(FMT[cfg["fb"]](t if cfg["blank"] else min(t, S["dur"] - 0.1)))
            hover(e.x)

        def release(e):
            S["drag"] = None

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", release)
        cv.bind("<Motion>", lambda e: hover(e.x) if not S["drag"] else None)
        cv.bind("<Leave>", lambda e: cv.delete("hov"))
        for v in (tkvars[KA], tkvars[KB]):
            v.trace_add("write", track)

        def install(png, dur, sw, sh):
            if not alive():
                return
            try:
                S["img"] = tk.PhotoImage(file=png)
            except tk.TclError:
                S["img"] = None
            if S["img"] is None:
                S["msg"] = "No film strip for this file"
            else:
                S.update(dur=dur, sw=sw, th=sh)
                S["prev"] = times()
                cv.configure(width=sw + 2 * PAD, height=RULER + sh + 6)
            redraw()

        def work():
            png, sw, sh, dur = None, 0, 0, 0.0
            try:
                src = cur_src()
                if src:
                    inf = probe(src)
                    dur = float(inf["dur"] or 0)
                    S["vw"], S["vh"] = int(inf["w"] or 16), int(inf["h"] or 9)
                    if cfg.get("audio"):
                        png, sw, sh = make_wave(src, dur, pw - 2 * PAD)
                    else:
                        png, sw, sh = make_strip(src, dur, int(inf["w"] or 0), int(inf["h"] or 0), cw=pw - 2 * PAD)
            except Exception as e:  # noqa: BLE001
                write_log(f"film strip failed: {e!r}")
            if png:
                q_.put(("call", lambda: install(png, dur, sw, sh)))
            else:
                def fail():
                    S["msg"] = "No film strip for this file"
                    redraw()
                q_.put(("call", fail))
        redraw()
        threading.Thread(target=work, daemon=True).start()

        # Window stretched: re-tile the strip for the new width (cached per width).
        def resize_strip(new_pw):
            S["tok"] = S.get("tok", 0) + 1
            tok = S["tok"]

            def w2():
                png, sw, sh, dur = None, 0, 0, 0.0
                try:
                    src = cur_src()
                    if src:
                        inf = probe(src)
                        dur = float(inf["dur"] or 0)
                        if cfg.get("audio"):
                            png, sw, sh = make_wave(src, dur, new_pw - 2 * PAD)
                        else:
                            png, sw, sh = make_strip(src, dur, int(inf["w"] or 0), int(inf["h"] or 0), cw=new_pw - 2 * PAD)
                except Exception as e:  # noqa: BLE001
                    write_log(f"film strip resize failed: {e!r}")
                if png:
                    q_.put(("call", lambda: install(png, dur, sw, sh) if tok == S["tok"] else None))
            threading.Thread(target=w2, daemon=True).start()
        st["resize_strip"] = resize_strip

    # Clip timeline for Join clips / Join with crossfade: every clip as a block, in order, with its first
    #  frame, name and length (block width = length). Drag a block left or right to change the order; the
    #  list in the window is re-ordered to match. A clip that comes from the Input box is locked in first
    #  place. Crossfade shows the overlap marks and the shortened total.
    def attach_timeline(top_, box, spec, getters, tkvars, st, pw, tmpd):
        lb = getattr(top_, "om_files_lb", None)
        if lb is None:
            return
        H, THUMB = 92, (80, 45)
        T = {"w": pw, "clips": [], "locked": 0, "info": {}, "imgs": {}, "drag": None, "mx": 0, "gen": 0}
        cv = tk.Canvas(box, width=pw, height=H, highlightthickness=0, bg=pal()["log_bg"], cursor="hand2")
        cv.pack(pady=(0, 6))
        hint = ttk.Label(box, text="Drag a clip left or right to change the order.", style="Muted.TLabel")
        hint.pack(anchor="w")

        def alive():
            try:
                return bool(cv.winfo_exists())
            except tk.TclError:
                return False

        def clips_now():
            extras = list(lb.get(0, "end"))
            try:
                files, opts2, err = resolve_files(spec, {"files": extras}, path_var.get().strip().strip('"'), current_multi())
            except Exception:  # noqa: BLE001
                return extras, 0
            if err or not files:
                return extras, 0
            allc = list(files) + list(opts2.get("files", []))
            return allc, max(0, len(allc) - len(extras))

        def trlen():
            try:
                return max(0.0, float(str(tkvars["tr"].get()).strip() or 0)) if "tr" in tkvars else 0.0
            except ValueError:
                return 0.0

        def layout():
            W = T["w"] - 8
            durs = [max(0.1, T["info"].get(c, (0.0,))[0] or 1.0) for c in T["clips"]]
            tot = sum(durs) or 1.0
            ws = [max(54.0, d / tot * W) for d in durs]
            sc = min(1.0, W / (sum(ws) or 1.0))
            ws = [w * sc for w in ws]
            xs, x = [], 4.0
            for w in ws:
                xs.append(x)
                x += w
            return xs, ws, durs

        def clock(t):
            t = int(round(t))
            return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"

        def redraw():
            if not alive():
                return
            pl = pal()
            cv.delete("all")
            cv.configure(bg=pl["log_bg"], width=T["w"])
            if not T["clips"]:
                cv.create_text(T["w"] // 2, H // 2, text="Add clips to see the timeline", fill=pl["muted"])
                return
            xs, ws, durs = layout()
            drag = T["drag"]
            order = list(range(len(T["clips"])))
            for i in order:
                x0, w = xs[i], ws[i]
                if drag is not None and i == drag:
                    continue
                draw_block(i, x0, w, durs[i], pl, 0)
            if drag is not None:
                draw_block(drag, T["mx"] - ws[drag] / 2, ws[drag], durs[drag], pl, 1)
                ins = insert_index(xs, ws)
                ix = xs[ins] if ins < len(xs) else xs[-1] + ws[-1]
                cv.create_line(ix, 2, ix, H - 2, fill=BRAND_GOLD, width=3)
            total = sum(T["info"].get(c, (0.0,))[0] or 0.0 for c in T["clips"])
            tr = trlen() if spec["id"] == "crossfade" else 0.0
            if tr and len(T["clips"]) > 1:
                total = max(0.0, total - tr * (len(T["clips"]) - 1))
            hint.configure(text=f"{len(T['clips'])} clips, about {clock(total)} in total" + (" (after the crossfades)" if tr else "")
                           + "  -  drag a clip to change the order")

        def draw_block(i, x0, w, dur, pl, lifted):
            c = T["clips"][i]
            locked = i < T["locked"]
            y0, y1 = (2 if lifted else 6), H - 4
            fill = "#3a5f8f" if lifted else ("#2b3f5c" if i % 2 == 0 else "#35506f")
            cv.create_rectangle(x0 + 1, y0, x0 + w - 1, y1, fill=fill, outline=BRAND_GOLD if locked else "#6f8fb8", width=2 if locked else 1)
            img = T["imgs"].get(c)
            if img is not None and w >= 92:
                cv.create_image(x0 + 6, y0 + 5, image=img, anchor="nw")
            name = Path(c).name
            maxc = max(4, int((w - 8) / 7))
            if len(name) > maxc:
                name = name[:maxc - 1] + "…"
            d = T["info"].get(c, (0.0,))[0]
            cv.create_text(x0 + 6, y1 - 22, text=name, anchor="w", fill="#ffffff", font=("TkDefaultFont", 9, "bold"))
            cv.create_text(x0 + 6, y1 - 8, text=(clock(d) if d else "…") + ("  (Input box)" if locked and w > 120 else ""), anchor="w", fill="#c8d6ea", font=("TkDefaultFont", 8))
            if spec["id"] == "crossfade" and i > 0 and not lifted:
                cv.create_text(x0, y0 + 8, text="✕", fill=BRAND_GOLD, font=("TkDefaultFont", 10, "bold"))

        def insert_index(xs, ws):
            """Where the dragged block would land (a position among the movable clips)."""
            mx = T["mx"]
            idx = len(xs)
            for j in range(len(xs)):
                if j == T["drag"] or j < T["locked"]:
                    continue
                if mx < xs[j] + ws[j] / 2:
                    idx = j
                    break
            return max(idx, T["locked"])

        def press(e):
            if not T["clips"]:
                return
            xs, ws, _ = layout()
            for i in range(len(xs)):
                if xs[i] <= e.x <= xs[i] + ws[i] and i >= T["locked"]:
                    T["drag"], T["mx"] = i, e.x
                    redraw()
                    return

        def move(e):
            if T["drag"] is not None:
                T["mx"] = e.x
                redraw()

        def release(e):
            i = T["drag"]
            if i is None:
                return
            xs, ws, _ = layout()
            T["mx"] = e.x
            ins = insert_index(xs, ws)
            T["drag"] = None
            new = list(T["clips"])
            item = new.pop(i)
            ins = ins - 1 if ins > i else ins
            new.insert(max(T["locked"], ins), item)
            if new != T["clips"]:
                extras = new[T["locked"]:]
                lb.delete(0, "end")
                for f in extras:
                    lb.insert("end", f)
                top_.event_generate("<<FilesChanged>>")
            else:
                redraw()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", release)

        def info_worker(clips, gen):
            for c in clips:
                if T["gen"] != gen:
                    return
                if c in T["info"] and c in T["imgs"]:
                    continue
                try:
                    d = float(probe(c)["dur"] or 0)
                except Exception:  # noqa: BLE001
                    d = 0.0
                png = os.path.join(tmpd, f"tl_{abs(hash(c))}.png")
                ok = os.path.exists(png) or grab_frame(c, min(1.0, d / 2) if d else 0, png, box=THUMB)

                def apply(c=c, d=d, png=png, ok=ok):
                    if not alive():
                        return
                    T["info"][c] = (d,)
                    if ok:
                        try:
                            T["imgs"][c] = tk.PhotoImage(file=png)
                        except tk.TclError:
                            pass
                    redraw()
                q.put(("call", apply))

        def refresh(*_):
            if not alive():
                return
            T["clips"], T["locked"] = clips_now()
            T["gen"] += 1
            redraw()
            threading.Thread(target=info_worker, args=(list(T["clips"]), T["gen"]), daemon=True).start()

        def resize_tl(new_pw):
            T["w"] = new_pw
            redraw()
        st["resize_tl"] = resize_tl
        if "tr" in tkvars:
            tkvars["tr"].trace_add("write", lambda *_: redraw())
        top_.bind("<<FilesChanged>>", refresh, add="+")
        refresh()

    # Live preview panel inside the dialog: fixed-size picture holders (so the window
    #  never jumps), 600 ms debounce after any change, then a worker renders frames
    #  with grab_frame/preview_frame. Shows Before/After pairs for most filters,
    #  start/end frames for Trim, and the cropped result for Crop. Images are
    #  subsampled with tk PhotoImage to fit.
    def attach_preview(top_, frm, spec, getters, tkvars, nrows):
        def cur_src(raw=None):
            src_ = preview_source()
            if spec["kind"] == "image" and (not src_ or Path(src_).suffix.lower() not in IMAGE_EXT):
                fl = (raw or {}).get("files") or []
                src_ = fl[0] if fl and os.path.isfile(fl[0]) else ""
            elif not src_ and spec.get("folder_one"):  # Join etc.: the Input box is empty, so the first listed clip is the picture
                fl = (raw or {}).get("files") or []
                src_ = fl[0] if fl and os.path.isfile(fl[0]) else ""
            return src_
        src = cur_src({"files": getters["files"]()} if "files" in getters else None)
        box = ttk.LabelFrame(frm, text="Live preview", padding=8)
        box.grid(row=0, column=3, rowspan=max(nrows, 4), sticky="n", padx=(18, 0))
        if spec["id"] == "atrim":  # audio file: no pictures, just the waveform with its two handles
            box.configure(text="Selection")
            attach_strip(top_, box, tkvars, cur_src, {"n": 0, "after": None, "imgs": [], "focus": "start"}, q, 430, STRIP_OPS["atrim"])
            return
        strip_cfg = STRIP_OPS.get(spec["id"])
        is_trim = spec["id"] == "trim"
        dim = {"w": 560, "h": 315} if is_trim else {"w": 430, "h": 245}  # picture slot size; Trim gets a bigger one and follows the window size
        holds = []
        base_w, base_h = dim["w"], dim["h"]
        names = ["Preview (the handle you moved last)"] if is_trim else (["Result of the crop"] if spec["id"] == "crop" else (["First clip", "Second clip (joined after it)"] if spec["id"] in ("join", "crossfade") else ["Before", "After (this is what the result will look like)"]))
        labels = []
        for nm in names:
            ttk.Label(box, text=nm, style="Muted.TLabel").pack(anchor="w")
            hold = ttk.Frame(box, width=dim["w"], height=dim["h"])
            holds.append(hold)  # fixed slot so the window does not jump when a picture appears
            hold.pack(pady=(0, 6))
            hold.pack_propagate(False)
            lab = ttk.Label(hold, text="loading…", anchor="center")
            lab.pack(expand=True)
            labels.append(lab)
        # "Preview at": slide through the video to see the effect at any moment (Trim and the strip tools
        #  follow their handles instead).
        pos_var, pos_cb = None, {"f": None}
        if not is_trim and not strip_cfg and spec["kind"] != "image" and spec["id"] not in ("join", "crossfade"):
            pos_var = tk.DoubleVar(value=1 / 3)
            prow = ttk.Frame(box)
            prow.pack(fill="x", pady=(0, 4))
            ttk.Label(prow, text="Preview at:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
            ttk.Scale(prow, from_=0.0, to=1.0, variable=pos_var, orient="horizontal",
                      command=lambda v: pos_cb["f"]() if pos_cb["f"] else None).pack(side="left", fill="x", expand=True)
        tmpd = tempfile.mkdtemp(prefix="ffprev_")
        st = {"n": 0, "after": None, "imgs": [], "focus": "start"}
        if is_trim:
            sc_ = {"dir": None, "step": 1.0}

            def scrub_worker():
                try:
                    src_ = cur_src()
                    if not src_:
                        return
                    dur_ = float(probe(src_)["dur"] or 0)
                    if dur_ <= 0:
                        return
                    step_ = max(1.0, round(dur_ / 300.0, 2))
                    d_ = scrub_dir_for(src_, step_)
                    if not d_:
                        return
                    sc_["step"], sc_["dir"] = step_, d_  # frames become usable while they are still being made
                    make_scrub(src_, dur_, d_, step_)
                except Exception as e:  # noqa: BLE001
                    write_log(f"scrub cache failed: {e!r}")
            threading.Thread(target=scrub_worker, daemon=True).start()

            def scrub_show(t):
                """Instant (small, a bit blurry) frame for time t from the cache; the sharp one follows."""
                d_ = sc_["dir"]
                if not d_ or not labels or not top_.winfo_exists():
                    return
                k = int(round(max(0.0, t) / sc_["step"])) + 1
                for kk in (k, k - 1, k - 2):
                    f_ = os.path.join(d_, f"s_{kk:05d}.png")
                    if kk >= 1 and os.path.exists(f_):
                        break
                else:
                    return
                try:
                    im_ = tk.PhotoImage(file=f_)
                    z_ = max(1, int(min((dim["w"] - 10) / max(1, im_.width()), (dim["h"] - 5) / max(1, im_.height())) + 0.3))
                    if z_ > 1:
                        im_ = im_.zoom(z_, z_)
                except tk.TclError:
                    return
                st["n"] += 1  # a sharp frame from an older request must not replace this one
                st["imgs"] = [im_]
                labels[0].configure(image=im_, text="")
            st["scrub_show"] = scrub_show
        if spec["id"] in ("join", "crossfade"):
            try:
                attach_timeline(top_, box, spec, getters, tkvars, st, dim["w"], tmpd)
            except Exception as e:  # noqa: BLE001
                write_log(f"clip timeline setup failed: {e!r}")
        if strip_cfg and strip_cfg["a"] in tkvars and strip_cfg["b"] in tkvars:
            try:
                attach_strip(top_, box, tkvars, cur_src, st, q, dim["w"], strip_cfg)
            except Exception as e:  # noqa: BLE001
                write_log(f"film strip setup failed: {e!r}")
        note = ttk.Label(box, text="", style="Muted.TLabel", wraplength=dim["w"] + 10)
        note.pack(anchor="w")
        pair = spec["id"] not in ("trim", "crop")

        def work(n, raw):
            paths, msg = [], ""
            src = cur_src(raw)
            try:
                if not src:
                    raise ValueError("Add some photos to see a preview." if spec["kind"] == "image" else "Choose a file in the Input box first.")
                dur = probe(src)["dur"] or 0
                if spec["id"] == "trim":
                    s_ = tsec(raw["start"] or "0")
                    e_ = tsec(raw["end"]) if str(raw["end"]).strip() else dur
                    if dur:
                        s_, e_ = min(s_, max(0.0, dur - 0.1)), min(e_, dur)
                    if e_ <= s_:
                        msg = "The end must be after the start."
                    else:
                        msg = f"Clip length: {e_ - s_:.1f} s  (video is {dur:.1f} s)" if dur else f"Clip length: {e_ - s_:.1f} s"
                        t = s_ if raw.get("__focus") != "end" else max(s_, e_ - 0.1)
                        f = os.path.join(tmpd, f"t{n}.png")
                        paths.append(f if grab_frame(src, t, f, box=(raw["__w"] - 10, raw["__h"] - 5)) else None)
                        msg += f"\nShowing the frame at {fmt_ms(t)} ({'start' if raw.get('__focus') != 'end' else 'end'} handle)"
                elif spec["id"] in ("join", "crossfade"):
                    # nothing to "preview" for a join: show the first clip and the one that follows it
                    f0, f1 = os.path.join(tmpd, f"j0_{n}.png"), os.path.join(tmpd, f"j1_{n}.png")
                    paths.append(f0 if grab_frame(src, min(1.0, dur / 2) if dur else 0, f0) else None)
                    nxt = next((x for x in (raw.get("files") or []) if os.path.isfile(x) and os.path.normpath(x) != os.path.normpath(src)), "")
                    if nxt:
                        d2 = probe(nxt)["dur"] or 0
                        paths.append(f1 if grab_frame(nxt, min(1.0, d2 / 2) if d2 else 0, f1) else None)
                        fl_ = raw.get("files") or []
                        n_cl = len(fl_) if any(os.path.normpath(x) == os.path.normpath(src) for x in fl_) else 1 + len(fl_)
                        msg = f"{n_cl} clips will be joined, in the order shown on the timeline."
                    else:
                        paths.append(None)
                        msg = "Add at least one more clip to join."
                elif spec["id"] != "crop":
                    t0 = raw["__t"] if raw.get("__t") is not None else ((raw["__f"] * dur) if (raw.get("__f") is not None and dur) else (dur / 3 if dur else 0))
                    t0 = min(max(0.0, t0), max(0.0, dur - 0.2)) if dur else 0
                    f0, f1 = os.path.join(tmpd, f"b{n}.png"), os.path.join(tmpd, f"a{n}.png")
                    paths.append(f0 if grab_frame(src, t0, f0, box=None) else None)
                    try:
                        o2 = convert_opts(spec, raw)
                    except ValueError:
                        o2 = None
                        msg = "Fill in the options to see the preview."
                    if o2 is not None:
                        ok2 = preview_frame(spec, o2, src, t0, f1)
                        paths.append(f1 if ok2 else None)
                        if ok2:
                            w1, h1 = png_size(f1)
                            w0, h0 = png_size(f0)
                            msg = (f"Size: {w0}x{h0}  ->  {w1}x{h1}" if (w0 and w1) else "") + (f"\nPreview at {fmt_ms(t0)}" if dur else "")
                        else:
                            msg = "No preview for these settings (or this kind of operation) - Run will still work."
                else:
                    o = {k: (str(raw[k]).strip() or next(x for x in spec["opts"] if x["key"] == k)["default"]) for k in ("w", "h", "x", "y")}
                    f = os.path.join(tmpd, f"c{n}.png")
                    tc = min(max(0.0, (raw["__f"] * dur) if (raw.get("__f") is not None and dur) else (dur / 3 if dur else 0)), max(0.0, dur - 0.2)) if dur else 0
                    ok = grab_frame(src, tc, f, crop_filter(o), box=(raw["__w"] - 10, raw["__h"] - 5))
                    paths.append(f if ok else None)
                    msg = "" if ok else "These crop values don't work yet - check the width, height and offsets."
            except Exception as e:  # noqa: BLE001
                msg = f"No preview: {e}"
            q.put(("call", lambda: show(n, paths, msg)))

        def show(n, paths, msg):
            if n != st["n"] or not top_.winfo_exists():
                return
            st["imgs"] = []
            loaded = []
            for pth in paths + [None] * (len(labels) - len(paths)):
                try:
                    loaded.append(tk.PhotoImage(file=pth) if pth else None)
                except tk.TclError:
                    loaded.append(None)
            if pair:  # same shrink factor for both so a smaller result really looks smaller
                kk = max([1] + [max(-(-i_.width() // (dim["w"] - 10)), -(-i_.height() // (dim["h"] - 5))) for i_ in loaded if i_ is not None])
                ks = [kk] * len(loaded)
            else:
                ks = [max(1, -(-i_.width() // (dim["w"] - 10)), -(-i_.height() // (dim["h"] - 5))) if i_ is not None else 1 for i_ in loaded]
            for lab, img, k in zip(labels, loaded, ks):
                if img is not None and k > 1:
                    img = img.subsample(k, k)
                if img:
                    st["imgs"].append(img)
                    lab.configure(image=img, text="")
                else:
                    lab.configure(image="", text="(no preview)")
            note.configure(text=msg)

        def kick():
            st["after"] = None
            if not top_.winfo_exists():
                return
            st["n"] += 1
            try:
                raw = {k: g() for k, g in getters.items()}
            except Exception:  # noqa: BLE001
                return
            raw["__focus"] = st["focus"]
            raw["__w"], raw["__h"] = dim["w"], dim["h"]
            raw["__t"] = st.get("focus_t") if spec["id"] == "gif" else None  # GIF: show the frame at the Start handle
            raw["__f"] = pos_var.get() if pos_var is not None else None  # "Preview at" position (0..1)
            note.configure(text="Updating…")
            threading.Thread(target=work, args=(st["n"], raw), daemon=True).start()

        def later(*_):
            if st["after"]:
                try:
                    top_.after_cancel(st["after"])
                except tk.TclError:
                    pass
            st["after"] = top_.after(200 if strip_cfg else (350 if pos_var is not None else 600), kick)
        pos_cb["f"] = later

        # Trim only: the dialog can be stretched. Everything extra goes to the preview and the
        #  film strip (the picture is re-made at the new size, the strip is re-tiled wider).
        rz = {"after": None}

        def apply_size():
            rz["after"] = None
            if not top_.winfo_exists():
                return
            # how much bigger the window is than the size its contents ask for = room to give to the preview
            nw = max(base_w, dim["w"] + top_.winfo_width() - top_.winfo_reqwidth())
            nh = max(base_h, dim["h"] + (top_.winfo_height() - top_.winfo_reqheight()) // max(1, len(holds)))  # extra height is shared between the pictures
            if abs(nw - dim["w"]) < 8 and abs(nh - dim["h"]) < 8:
                return
            dim["w"], dim["h"] = nw, nh
            for h_ in holds:
                h_.configure(width=nw, height=nh)
            note.configure(wraplength=nw + 10)
            if st.get("resize_strip"):
                st["resize_strip"](nw)
            if st.get("resize_tl"):
                st["resize_tl"](nw)
            later()

        def on_resize(e):
            if e.widget is not top_:
                return
            if rz["after"]:
                try:
                    top_.after_cancel(rz["after"])
                except tk.TclError:
                    pass
            rz["after"] = top_.after(300, apply_size)
        top_.bind("<Configure>", on_resize, add="+")

        def cleanup(e):
            if e.widget is top_:
                shutil.rmtree(tmpd, ignore_errors=True)
        for v in tkvars.values():
            v.trace_add("write", later)
        top_.bind("<Destroy>", cleanup, add="+")
        top_.bind("<<FilesChanged>>", later, add="+")
        kick()

    # ------------------------------------------------------------------ finish up
    for cat in ["Favourites"] + CATEGORIES:
        build_cat(cat)
    show_cat()
    # If ffmpeg/ffprobe are missing, ask the user and run install_ffmpeg in the background, then restart the app.
    def offer_ffmpeg_install():
        if IS_MAC:
            cmd = "brew install ffmpeg"
            try:
                root.clipboard_clear()
                root.clipboard_append(cmd)
            except tk.TclError:
                pass
            messagebox.showwarning(APP, "ffmpeg was not found on this Mac.\n\nOpen Terminal and run:\n\n    " + cmd + "\n\n(The command has been copied - just paste it. "
                                   "If you don't have Homebrew, get it from https://brew.sh first.) Then restart this app.")
            return
        if os.name != "nt":
            messagebox.showwarning(APP, "ffmpeg / ffprobe were not found on PATH. Install ffmpeg with your package manager, then restart.")
            return
        if not messagebox.askyesno(APP, "ffmpeg is needed to process video and audio, and it is not installed on this computer.\n\n"
                                   "Download it now? (about 100 MB, saved in your user folder, no admin rights needed)"):
            return
        dlg = tk.Toplevel(root)
        dlg.title("Installing ffmpeg")
        dlg.transient(root)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        msg_var = tk.StringVar(value="Starting…")
        ttk.Label(dlg, textvariable=msg_var, width=52).pack(padx=20, pady=(18, 6))
        bar = ttk.Progressbar(dlg, length=380, maximum=100)
        bar.pack(padx=20, pady=(0, 18))
        res = {}

        def work():
            res["ok"], res["msg"] = install_ffmpeg(lambda fr, tx: (bar.configure(value=fr * 100), msg_var.set(tx)))

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                dlg.after(150, poll)
                return
            dlg.destroy()
            if res.get("ok"):
                messagebox.showinfo(APP, "ffmpeg is installed. The app will now restart.")
                cmd = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
                subprocess.Popen(cmd, creationflags=NO_WINDOW if FROZEN else 0)
                root.destroy()
            else:
                messagebox.showerror(APP, res.get("msg", "Install failed."))
        poll()

    apply_styles()
    register_drop_tree(root)
    title_bar()
    try:
        ico = RES_DIR / "app.ico"
        if ico.exists() and os.name == "nt":
            root.iconbitmap(default=str(ico))
    except Exception:  # noqa: BLE001
        pass
    ver = ff_version() if have_ff else ""
    extras = []
    if not sv_ttk:
        extras.append("modern theme off (pip install sv-ttk)")
    if not dnd:
        extras.append("drag-and-drop off (pip install tkinterdnd2)")
    status_var.set(tr("Ready") + (f"   |   ffmpeg {ver}" if ver else "") + ("   |   " + "; ".join(extras) if extras else ""))
    if not have_ff:
        refresh_info()
        root.after(300, offer_ffmpeg_install)
    # Background thread asks GitHub's releases API for the latest tag; if newer than VERSION, shows a clickable notice. Silent on any network error.
    def check_update():
        """Ask GitHub for the latest release (background thread); prompt only if a newer one exists."""
        def work():
            try:
                import urllib.request
                req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", headers={"User-Agent": "OM-Video-Studio"})
                with urlopen_robust(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8"))
                tag = str(data.get("tag_name", "")).lstrip("vV")
                parse = lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3])  # noqa: E731
                if tag and parse(tag) > parse(VERSION):
                    url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest"

                    def ask():
                        if messagebox.askyesno(APP, f"Version {tag} is available (you have {VERSION}).\n\nOpen the download page?"):
                            import webbrowser
                            webbrowser.open(url)
                    root.after(0, ask)
            except Exception as e:  # noqa: BLE001  (offline / no releases yet is normal)
                write_log(f"Update check skipped: {type(e).__name__}")
        threading.Thread(target=work, daemon=True).start()

    pump()
    root.after(3000, check_update)
    if have_ff:
        root.after(200, play_startup_sound)
    root.mainloop()


# --- 13. SELF-TEST & COMMAND-LINE ------------------------------------------
#  `python om_video_studio.py --selftest [ids] [--full] [--gpu]`
#    generates a tiny test clip/audio/image/subtitle/LUT with ffmpeg, then runs
#    every (or the listed) operation through execute() and reports PASSED /
#    FAILED / SKIPPED. CI runs `--selftest trim,split,convert,compress,extract,
#    thumb,extras` before building the installer; 'extras' = selftest_extras().
# --------------------------------------------------------------------------- self-test
# Non-ffmpeg checks: translations cover every op/option/category, tr() behaviour, ping sounds are distinct, full-report file naming/contents, settings helpers etc. Each check() prints OK/FAIL.
def selftest_extras():
    """Tests for copy-command, preview frames, file resolving and the job queue."""
    if not refresh_tools():
        print("ffmpeg/ffprobe not found")
        return 1
    work = Path(tempfile.mkdtemp(prefix="ffextra_"))
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name, detail if not cond else "", flush=True)
        if not cond:
            fails.append(name)
    clip, clip2 = work / "a clip.mp4", work / "b.mp4"
    for c_, sz in ((clip, "640x360"), (clip2, "320x240")):
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration=4:size={sz}:rate=25", "-f", "lavfi",
                        "-i", "sine=duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(c_)], check=True)
    check("fmt_ms", fmt_ms(82.4) == "001:22:400" and fmt_ms(0) == "000:00:000" and fmt_ms(3599.9996) == "060:00:000", fmt_ms(82.4))
    check("tsec min:sec:ms", abs(tsec("001:22:400") - 82.4) < 1e-9 and tsec("1:02:03") == 3723 and tsec("90") == 90 and tsec("1:02:03.5") == 3723.5 and tsec("") == 0)
    check("tsec round trip", all(abs(tsec(fmt_ms(x)) - x) < 0.0006 for x in (0, 0.5, 44, 81.4, 241.2, 3600.123)))
    env_ = {"iw": 640, "ih": 360, "ow": 360, "oh": 360}
    check("eval_expr", eval_expr("min(iw,ih)", env_) == 360 and eval_expr("(iw-ow)/2", env_) == 140 and eval_expr("ih/2", env_) == 180
          and eval_expr("640", env_) == 640 and eval_expr("__import__('os')", env_) is None and eval_expr("iw/0", env_) is None and eval_expr("abc", env_) is None)
    bad = []
    for (oid, key), (kind, lo_, hi_) in SLIDERS.items():
        sp = find_op(oid)
        o_ = next((x for x in sp["opts"] if x["key"] == key), None) if sp else None
        if o_ is None:
            bad.append(f"{oid}.{key} missing")
        elif kind in ("pos", "posend") and o_["type"] != "text":
            bad.append(f"{oid}.{key} time slider on a {o_['type']} box")
        elif kind not in ("pos", "posend") and o_["type"] not in ("int", "float", "text"):
            bad.append(f"{oid}.{key} bad type")
    check("SLIDERS match real options", not bad, str(bad))
    trim, crop = find_op("trim"), find_op("crop")
    check("find_op", trim is not None and crop is not None and find_op("nope") is None)
    topts = convert_opts(trim, {**default_opts(trim), "start": "1", "end": "3"})
    text = format_commands(trim, topts, str(clip), str(work / "out"))
    check("format_commands trim", text.startswith("ffmpeg -y") and "-ss 1" in text and "a clip_trim.mp4" in text and '"' in text, text)
    check("format_commands has no internal flags", "-progress" not in text and "-nostats" not in text, text)
    copts = convert_opts(crop, {**default_opts(crop), "w": "min(iw,ih)", "h": "ih/2"})
    check("crop_filter escapes commas", "min(iw\\,ih)" in crop_filter(copts), crop_filter(copts))
    f1, f2 = work / "f1.png", work / "f2.png"
    check("grab_frame plain", grab_frame(str(clip), 1.0, str(f1)) and f1.stat().st_size > 500)
    check("grab_frame crop", grab_frame(str(clip), 1.0, str(f2), crop_filter(copts)) and f2.stat().st_size > 500)
    check("grab_frame bad filter fails cleanly", not grab_frame(str(clip), 1.0, str(work / "f3.png"), "crop=notanumber"))
    check("grab_frame past end does not hang", isinstance(grab_frame(str(clip), 99.0, str(work / "f4.png")), bool))
    check("resolve single", resolve_files(trim, {}, str(clip))[0] == [str(clip)])
    check("resolve missing", resolve_files(trim, {}, str(work / "none.mp4"))[2] is not None)
    fl, _, err = resolve_files(trim, {}, "", [str(clip), str(clip2)])
    check("resolve multi", fl == [str(clip), str(clip2)] and err is None)
    fl, _, err = resolve_files(trim, {}, str(work))
    check("resolve folder", len(fl) == 2 and err is None, str(fl))
    join = find_op("join")
    fl, o2, err = resolve_files(join, {"files": []}, "", [str(clip), str(clip2)])
    check("resolve folder_one with multi", fl == [str(clip)] and o2["files"] == [str(clip2)], str((fl, o2)))
    for oid in ("resize", "rotate", "social", "blur"):
        sp_ = find_op(oid)
        pf = work / f"pv_{oid}.png"
        check("preview_frame " + oid, preview_frame(sp_, convert_opts(sp_, default_opts(sp_)), str(clip), 1.0, str(pf)))
    check("preview_frame fails cleanly when it can't", not preview_frame(find_op("join"), {"files": []}, str(clip), 1.0, str(work / "pv_join.png")))
    ss = find_op("slideshow")
    photos = []
    for i_ in range(3):
        pp = work / f"photo{i_}.png"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=400x300:rate=1,hue=h={i_ * 60}", "-frames:v", "1", str(pp)], check=True)
        photos.append(str(pp))
    sopts = convert_opts(ss, {**default_opts(ss), "secs": "1", "tr": "0.3", "files": photos})
    fl, o3, err = resolve_files(ss, sopts, str(clip))  # input box holds a VIDEO, photos listed in the window
    check("slideshow: listed photos used when input is a video", fl == [photos[0]] and o3["files"] == photos[1:] and err is None, str((fl, err)))
    fl, o3, err = resolve_files(ss, sopts, "")
    check("slideshow: listed photos used when input is empty", fl == [photos[0]] and err is None)
    photo_dir = work / "photos"
    photo_dir.mkdir()
    for pp in photos:
        shutil.copy(pp, photo_dir)
    fl, o3, err = resolve_files(ss, convert_opts(ss, {**default_opts(ss), "files": [photos[0]]}), str(photo_dir))
    check("slideshow: folder + extra photos", len(fl) == 1 and len(o3["files"]) == 3 and err is None, str((fl, o3)))
    fl, o3, err = resolve_files(ss, sopts, "")
    ok_, fail_, outs_ = execute(ss, o3, fl, str(work / "ss"), lines_.append if False else (lambda m_: None), lambda f: None, threading.Event())
    check("slideshow runs from listed photos", ok_ == 1 and fail_ == 0 and outs_ and os.path.exists(outs_[0]), str((ok_, fail_, outs_)))
    jn = find_op("join")
    two = convert_opts(jn, {**default_opts(jn), "files": [str(clip), str(clip2)]})
    fl, o4, err = resolve_files(jn, two, "")
    check("join: listed clips used when input is empty", fl == [str(clip)] and o4["files"] == [str(clip2)] and err is None, str((fl, o4, err)))
    ok_, fail_, outs_ = execute(jn, o4, fl, str(work / "jn"), lambda m_: None, lambda f: None, threading.Event())
    check("join runs from listed clips", ok_ == 1 and fail_ == 0 and outs_ and os.path.exists(outs_[0]), str((ok_, fail_)))
    fl, o4, err = resolve_files(jn, convert_opts(jn, {**default_opts(jn), "files": [str(clip2)]}), str(clip))
    check("join: valid input stays first", fl == [str(clip)] and o4["files"] == [str(clip2)])
    rz = find_op("resize")
    port = work / "portrait.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=360x640:rate=25", "-pix_fmt", "yuv420p", str(port)], check=True)

    def rz_size(src_, **kw):
        o_ = convert_opts(rz, {**default_opts(rz), **kw})
        r_ = execute(rz, o_, [str(src_)], str(work / "rz"), lambda m_: None, lambda f: None, threading.Event())
        return (probe(r_[2][0])["w"], probe(r_[2][0])["h"]) if r_[0] == 1 else None
    check("resize 240p landscape", rz_size(clip, preset="240p") == (426, 240), str(rz_size(clip, preset="240p")))
    check("resize 720p never enlarges", rz_size(clip, preset="720p (HD)") == (640, 360), str(rz_size(clip, preset="720p (HD)")))
    check("resize 720p enlarge allowed", rz_size(clip, preset="720p (HD)", noup=False) == (1280, 720))
    check("resize 320p portrait uses short side", rz_size(port, preset="320p") == (320, 568), str(rz_size(port, preset="320p")))
    check("resize custom boxes", rz_size(clip, preset=RES_CUSTOM, width="200", height="-2") == (200, 112))
    check("resize presets cover 320p..8K", "320p" in RES and "4320p (8K)" in RES and RES["2160p (4K)"] == 2160)
    fbig = work / "big.png"
    check("grab_frame box=None keeps full size", grab_frame(str(clip), 1.0, str(fbig), box=None) and png_size(str(fbig)) == (640, 360), str(png_size(str(fbig))))
    pw, ph = png_size(str(f1))
    check("png_size", pw > 0 and ph > 0, str((pw, ph)))
    lines = []
    ev = threading.Event()
    prog = []
    items = [dict(spec=trim, opts=topts, files=[str(clip), str(clip2)], outdir=str(work / "q")),
             dict(spec=crop, opts=copts, files=[str(clip)], outdir=str(work / "q"))]
    ok, fail, outs = run_queue(items, lines.append, prog.append, ev)
    check("run_queue results", ok == 3 and fail == 0 and len(outs) == 3 and all(os.path.exists(o) for o in outs), str((ok, fail, outs, lines[-5:])))
    check("run_queue progress ends at 1", prog and prog[-1] == 1.0)
    check("LAST_CMDS filled", len(LAST_CMDS) == 1 and LAST_CMDS[0].startswith("ffmpeg -y"), str(LAST_CMDS))
    ev.set()
    ok, fail, outs = run_queue(items, lines.append, lambda f: None, ev)
    check("run_queue honours cancel", ok == 0 and fail == 0 and outs == [])
    shutil.rmtree(work, ignore_errors=True)
    # ---- languages: every language must cover every operation, category and option text
    cats = {sp["cat"] for sp in OPS} | {"Favourites"}
    need = cats | {sp["label"] for sp in OPS} | {o["label"] for sp in OPS for o in sp["opts"]}
    check("14 languages listed", len(LANGS) == 14 and "en" in LANGS, str(len(LANGS)))
    for code in LANGS:
        if code == "en":
            continue
        tbl = TR.get(code, {})
        miss = [k for k in need if k not in tbl]
        check(f"language {code}: all operations/options translated", not miss, str(miss[:3]))
        check(f"language {code}: templates keep their placeholders", all(k.count("{}") == v.count("{}") for k, v in tbl.items()))
    set_lang("de")
    check("tr: plain text", tr("Cancel") == TR["de"]["Cancel"])
    check("tr: button label wrapped before the bracket", tr("Resize") == TR["de"]["Resize"] and "\n(" in tr("Change speed\n(slow-mo / time-lapse)"))
    check("tr: template", tr("Running: Resize") == "Läuft: " + TR["de"]["Resize"], tr("Running: Resize"))
    check("tr: icon + category", tr("\U0001F3AC  Video").endswith(TR["de"]["Video"]))
    check("tr: unknown text stays English", tr("Some unknown text") == "Some unknown text")
    set_lang("en")
    check("tr: English is untouched", tr("Cancel") == "Cancel")
    set_lang("xx")
    check("tr: unknown language falls back to English", LANG["code"] == "en")
    # ---- completion sounds
    import wave
    lens = {}
    for k in SOUNDS:
        f = sound_file(k)
        with wave.open(str(f)) as w_:
            lens[k] = w_.getnframes() / w_.getframerate()
    check("three different sounds exist", len(set(round(v, 2) for v in lens.values())) == 3, str(lens))
    check("batch chime is longer than the single ping", lens["batch"] > lens["job"] + 0.3)
    check("ping kinds", ping_kind(1, 0, False) == "job" and ping_kind(3, 0, True) == "batch" and ping_kind(0, 2, False) == "error")
    os.environ["OM_NO_OPEN"] = "1"
    pr = find_op("probe")
    work.mkdir(exist_ok=True)  # earlier tests may have cleaned it up
    rclip = work / "rep src.mp4"
    r_ = subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=160x120", "-f", "lavfi", "-i", "sine=d=1", "-shortest", str(rclip)],
                   creationflags=NO_WINDOW, capture_output=True, text=True)
    if not rclip.exists():
        print("report clip not made:", r_.stderr[-300:] if hasattr(r_, "stderr") else "")
    rlog = []
    ok_, fail_, outs_ = execute(pr, {}, [str(rclip)], str(work / "rep"), rlog.append, lambda f: None, threading.Event())
    if not outs_:
        print("\n".join(rlog[-8:]), rclip.exists())
    rep = Path(outs_[0]) if outs_ else None
    check("report: file named 'full report <name>.txt' in the output folder", bool(rep) and rep.name == "full report rep src.txt" and rep.parent == work / "rep", str(outs_))
    txt = rep.read_text(encoding="utf-8") if rep and rep.exists() else ""
    check("report: has summary and full data", "FULL FILE REPORT" in txt and "Stream #0" in txt and '"streams"' in txt)
    ok2, fail2, outs2 = execute(pr, {}, [str(rclip)], str(work / "rep"), lambda m_: None, lambda f: None, threading.Event())
    check("report: second run does not overwrite", bool(outs2) and outs2[0] != outs_[0] and Path(outs2[0]).exists(), str(outs2))
    shutil.rmtree(work, ignore_errors=True)
    print("EXTRAS", "FAILED: " + ", ".join(fails) if fails else "all passed")
    return 1 if fails else 0


# Runs the operations (see banner above).
def selftest():
    wanted = [x for a_ in sys.argv[2:] if not a_.startswith("--") for x in a_.split(",") if x]
    if "extras" in wanted:
        wanted.remove("extras")
        sys.argv = sys.argv[:2] + [",".join(wanted)] + [a_ for a_ in sys.argv[3:] if a_.startswith("--")] if wanted else sys.argv[:2]
        rc = selftest_extras()
        return rc if not wanted else (rc or selftest())
    if not refresh_tools():
        print("ffmpeg/ffprobe not found")
        return 1
    work = Path(tempfile.mkdtemp(prefix="ffself_"))
    clip, music, pic = work / "clip.mp4", work / "music.mp3", work / "pic.png"
    sh = lambda a: subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y"] + a, check=True, stdin=subprocess.DEVNULL, timeout=120)  # noqa: E731
    srt = work / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,500\nHello test\n\n2\n00:00:03,000 --> 00:00:05,000\nSecond line\n", encoding="utf-8")
    cube = work / "id.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n", encoding="utf-8")
    sh(["-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-i", str(srt), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text", "-t", "6", str(clip)])  # -t, not -shortest: newer ffmpeg can hang on -shortest with a subtitle input
    sh(["-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(music)])
    sh(["-f", "lavfi", "-i", "testsrc=size=320x240", "-frames:v", "1", str(pic)])
    outdir = work / "out"
    outdir.mkdir()
    no_cancel = threading.Event()
    passed, failed, skipped = [], [], []
    full = "--full" in sys.argv
    gpu_test = "--gpu" in sys.argv
    only = [x for a_ in sys.argv[2:] if not a_.startswith("--") for x in a_.split(",") if x]
    if gpu_test:
        STATE["gpu"] = True
    for spec in OPS:
        if only and spec["id"] not in only:
            continue
        src = {"image": pic, "audio": music, "sub": srt}.get(spec["kind"], clip)
        raw = default_opts(spec)
        assets = {"audio": music, "image": pic, "sub": srt, "lut": cube, "video": clip, "any": music}
        for s in spec["opts"]:
            if s["type"] == "file" and not (spec["id"] == "chroma" and s["key"] == "bgimg"):
                raw[s["key"]] = str(assets[s["ftype"]])
            if s["type"] == "files":
                raw[s["key"]] = [str(pic if spec["kind"] == "image" else clip)]
        variants = [("", raw)]
        if full:
            for o_ in spec["opts"]:
                if o_["type"] == "choice":
                    for ch in o_["choices"]:
                        if str(ch) != str(raw.get(o_["key"])):
                            variants.append((f"[{o_['key']}={ch}]", {**raw, o_["key"]: ch}))
        for vtag, vraw in variants:
            name = spec["id"] + vtag
            lines = []
            print("testing", name, flush=True)
            try:
                ok, fail, outs = execute(spec, convert_opts(spec, vraw), [str(src)], str(outdir), lines.append, lambda f: None, no_cancel)
            except Exception as e:  # noqa: BLE001
                ok, fail, outs = 0, 1, []
                lines.append("EXCEPTION " + repr(e))
            text = "\n".join(lines)
            if fail and spec["id"] == "stabilise" and ("vidstab" in text.lower() or "no such filter" in text.lower()):
                skipped.append(name + " (no libvidstab in this ffmpeg build)")
            elif fail:
                failed.append((name, text[-400:]))
            else:
                missing = [o for o in outs if not os.path.exists(o)]
                (failed.append((name, "missing output " + str(missing))) if missing else passed.append(name))
        if gpu_test and STATE["gpu"] is False:
            print("GPU fallback triggered OK for", spec["id"], flush=True)
    print(f"PASSED {len(passed)}/{len(passed) + len(failed) + len(skipped)}: {', '.join(passed)}")
    for s in skipped:
        print("SKIPPED:", s)
    for n, t in failed:
        print(f"FAILED {n}:\n{t}\n")
    shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


# Entry point: --selftest runs tests, --list prints operations, otherwise open the GUI.
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--list" in sys.argv:
        for o in OPS:
            print(f'{o["cat"]:18} {o["id"]:14} {o["label"]}')
        sys.exit(0)
    gui()
