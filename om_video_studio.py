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
    root.title(f"{APP}  -  O.M. Industries Inc")
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
        logo_lbl.pack(side="left", expand=True, pady=6)
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
