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
VERSION = "1.16.0"
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
    tip="Pick a folder with 'Whole folder' to convert every image in it.")
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
    kind="image", tip="Choose your photos with 'Add…' or 'Add folder…' below (the Input box can stay empty), or pick a folder with 'Whole folder'. Photos are used in name order.", folder_one=True)
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
# One icon per popular tool for the cards (other tools use their group's icon).
TOOL_ICONS = {"trim": "\u2702", "join": "\U0001F517", "split": "\u2797", "speed": "\u23E9", "reverse": "\u23EA", "resize": "\U0001F4D0",
              "crop": "\U0001F4CF", "rotate": "\U0001F504", "flip": "\u2194", "aspect": "\U0001F4F1", "loop": "\U0001F501",
              "stabilise": "\U0001F4F7", "freeze": "\u2744", "convert": "\U0001F500", "remux": "\U0001F4E6", "faststart": "\u26A1",
              "social": "\U0001F4F2", "compress": "\U0001F5DC", "extract": "\U0001F3B5", "loudnorm": "\U0001F50A", "silence": "\U0001F507",
              "volume": "\U0001F50A", "fade": "\U0001F305", "denoise": "\U0001F9F9", "narration": "\U0001F399", "thumb": "\U0001F5BC",
              "frames": "\U0001F39E", "gif": "\U0001F300", "watermark": "\U0001F4A7", "pip": "\U0001F5D7", "slideshow": "\U0001F304",
              "burnsubs": "\U0001F4AC", "drawtext": "\U0001F524", "timer": "\u23F1", "colour": "\U0001F3A8", "blur": "\U0001F32B",
              "bw": "\u26AB", "probe": "\U0001F50D"}
# Light and dark colour sets; BRAND_* are the accent colours used for headers and the primary button.
PALETTE = {
    "light": dict(bg="#fafafa", fg="#1a1a1a", muted="#666666", log_bg="#ffffff", log_fg="#202020", sel="#cfe3ff",
                  panel="#ffffff", border="#d9dde3", chip="#eceff3", hover="#f1f5fb", side_sel="#fff3d3", brand="#0f2a47", gold="#f4be46"),
    "dark": dict(bg="#1c1c1c", fg="#f0f0f0", muted="#a6a6a6", log_bg="#141414", log_fg="#e6e6e6", sel="#33507a",
                 panel="#252a31", border="#373d46", chip="#303741", hover="#2e353f", side_sel="#3a3220", brand="#0f2a47", gold="#f4be46"),
}
BRAND_BG, BRAND_FG, BRAND_GOLD = "#0f2a47", "#ffffff", "#f4be46"
# White one-colour O.M. Industries logo (transparent PNG, base64) for the header band. 1x = normal screens, 2x = high-DPI. tk.PhotoImage reads PNG natively, so no extra libraries are needed.
LOGO_B64 = {
    "1x": "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",
    "2x": "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",
}
# Full-colour badge for the start-up splash screen (palette PNG with transparency, base64).
SPLASH_B64 = {
    "1x": "iVBORw0KGgoAAAANSUhEUgAAAVQAAAFUCAMAAABMTDSHAAABIFBMVEXSl2mqZ1JYKyUAAGeko5vcspllWjtITFKbiHQaKTmLajrGdVnPzME9QToiIzOKLBh9gH6egD3ix5IADiR7NSaNNR7DZzLLhzaqVQB/goCOKyLlwnb/AACNNB4cMEJlMigAAP8Af3++wbMAAAAZKTnm1rDx58+0RyrbqD6MOibZ1cj79NoJGi+5tqrOx7QxMzmwOx5uNiyNQS/+/eLn3MQrOEWVl5Tlx670uUDbtZq5VDevPSGsiT3ssz+Odjxzd3ZLVFrIfGB2ZjwiLTpKSDrARyc4RE7x4binpZiqqqTirT6RNB2GiIQAVVUBPT1bYmXHmz4aKTpaUzvEvKwaKTlla2waKTnXqI4aKTgXKDdUXGHZ0brBaEwaKTm5kj3bqkEQgfwxAAAAYHRSTlP///4C//////9z/////wv6/////wNo//8D/xb/AaP/pQEC/wD+//////7//////////v/////////////////////////////////+/wME///N//9Q/67/MBP///+S//8Yaam+AAA7SklEQVR42u2diUPiSNrwQ3ugtrZ97O7szO6+7/d9QgQSgi0BRbnGMFyiIDReqPP//xffU2cqSQUSCGiPW7tjK+SqX567KhXl4O20q+tr/vun+/sf29vPt7c3NwlXu7m5vX3e3v5xf/+Jb359ffWGOqK8jctYY1AwzOdbD0l5uwG6Ntyr67X/QuUCSv59AJoBYXrgAtsHKrNX7x4qBfpwv307J09RbLfvH94EWOU1VR7/c//jeWGeosz+uCdg194f1KsrovGzgOagNVAbQsO/oE9mgn2wz/FeoGIpur7f9gNq5fYamxfVcnkwSKW6lcqp0CqVbjc1eCl/qV5sNvZy/mCxKVi7fh9Qr9awiN7KBbMxvKi+AEiED+ABvXK1Wr3gbada/QKwU3ST1KBcvRg25GxvscCuXf3VoeIeyonmGghnhZFCYuiv5hYyCkiYB2SPl+qFlCzherX2F4aKdPFaQhR4fhl0EZwygiNnaVly2d6Dm1FGaLuDLzKytz+u6an/glCxuNw/S4AiJMBzZyjitCwfivbXlii5wx16GAnY5/vVmgFlhULq9UwIKIjYS3VzzybEftNRwx8UhGY5vnIIMJCtvoDAI7Bu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",
    "2x": "iVBORw0KGgoAAAANSUhEUgAAAlgAAAJYCAMAAACJuGjuAAABIFBMVEWvZEyfn5oAAGXUrpLUnW/q6NRjJhgaKTmJazrGdlujiXY9QDuJLRkdHjXjyJSqVQCONR99gH6egD0Af3/IhzbmwnN5GQ9/goCPMxt0dACONB/HdjR8MyFbYF//AAC/wrsaKTkAAADx58/m1rC0RinbqD6MOifZ1cj889kMHTC4tarr3MTirT6wOx7osD+OQS/PyLQtLTfhuqGTlpUsNToqN0SLMx2Hioo4RE5xdXSQdjx1ZTyqqqNJSDpMVVuuPiFTNDOriD12NCXASSkAPj7Hmj3x4bhkamynpZlvKxq6kz3Eva1BTFQaKTkaKTkZKDkaKTm5VjgAVVXCaU3kx60aKTkYKTfUp41ZUzvcqkFlWztbYmbY0bz0uUFUXGLNh2xOyB1MAAAAYHRSTlP//wL////+bP/////6CP8DqP//Av//A/8ZAmL/T/8B//4A//////7//////////////v/////+///////////////+/wT//////////8+OLa3/A///UBT///////////8kOGtMAABsrUlEQVR42u29iVrbyrauLbqEhGSenazZrH32Oec3MsaNHJBjG9u4YZFJ42CS0CUQYJL7v4u/OknVSqXOlozredaaBIyRVa/G+OqrUVXG5qJdHR0dUf88+vHt2/fvL19+/Xpx8QW0gqTB719cfP368vv3b99+/Fimf/3o6GpxTzeN5/zhl4+OPCQgTi8RS4XQDVAGIQOMyd97AdYzQsoDCvIUBScVYoCwH88eL+MZMkUy1fKPb5CoQioN8vXtx5GTa5cXYD2LOJUmUiJejvZaXoA1lwrdY2oKSDF4UXRdLcCaP6iAmpo2UzRdjvJ6HnAZzwWq718vCjNvF1+/Pxe45hqsZQzVLAOVPHRhuJYXYOWx4X77kQhUjUajRRr4MhG4flDkL8DKV/57GxGqVmtjOLy77/f39/cPDw/Pzs6OUSuV8H+Pz87At8EP1/v3d8MhgC0OXHOaFecQrCsUqr69vAiLE4QJkAQAKtXr9R3Q6lQroUZ/B78CfPMYgLbfvx9uhEXs4iXKistXC7BykQBD5b8GAArwdAZ4qmOUPH4ouEosWDvuj5xvgn9CxN7v34MYFjpwzV1SnCuwjrCqutBGChKFgHIBcWLQ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",
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
# One plain-English description per tool (shown in the About window and in docs/ABOUT.md). Keys are the tool ids.
TOOL_DOCS = {
    # ---- Video
    "trim": "Cut one section out of a video. Type a start and end time (hh:mm:ss or plain seconds); leave End blank to go to the end. 'Re-encode' gives a frame-exact cut, 'Copy' is instant but cuts on the nearest keyframe. A film strip with draggable handles, a live preview and a Play selection button help you find the exact moment.",
    "join": "Join several clips into one video, in the order listed. Leave the Input box empty and add all your clips (or a whole folder) in the dialog, or pick a first clip and append the rest. A clip timeline lets you reorder them; everything is scaled to the first clip's size.",
    "split": "Cut a long video into equal-length parts, for example 59 seconds each for Shorts and Reels. Choose 'Re-encode' for exact lengths or 'Copy' for a fast cut on keyframes.",
    "speed": "Slow motion or time-lapse for the whole clip. 0.5 is half speed, 2 is twice as fast. The sound is speeded or slowed to match, without changing its pitch.",
    "reverse": "Play the clip backwards, picture and sound. The whole clip is held in memory, so use it on short clips.",
    "resize": "Change the picture size. Pick a size from 144p up to 8K (the number is the short side, so it works for vertical video too) or choose Custom and type a width and height. 'Never enlarge' keeps smaller videos as they are.",
    "crop": "Cut away the edges of the picture. The defaults crop a centred square; the boxes accept numbers or expressions such as iw/2 (half the width).",
    "rotate": "Turn the picture 90 degrees clockwise, 90 counter-clockwise or 180 degrees. Handy for phone clips that came out sideways.",
    "flip": "Mirror the picture horizontally, flip it vertically, or both.",
    "aspect": "Reshape a video for TikTok, Reels or Shorts (9:16), square (1:1), portrait (4:5) or landscape (16:9) without stretching it. The empty bars can be filled with a blurred copy of the video, a solid colour, or you can crop to fill the frame.",
    "loop": "Repeat a clip a chosen number of times to make a longer video, for example a short background loop.",
    "stabilise": "Smooth out shaky hand-held footage with a two-pass stabiliser. Shakiness says how bad the shake is; Smoothing says how much to even it out. Needs an ffmpeg build that includes libvidstab (the Gyan 'full' build does).",
    "freeze": "Pause the picture on one moment and hold it for a few seconds, then carry on. Type the time to freeze at and how long to hold.",
    # ---- Format & Quality
    "convert": "Change a video to another format: mp4, mov, mkv, webm, avi or an animated GIF. The video is re-encoded; lower Quality (CRF) numbers mean better quality and bigger files. For a lossless swap use Re-wrap instead.",
    "remux": "Move the video and sound into a different container (mp4, mkv or mov) without re-encoding. It takes seconds and loses no quality, as long as the codecs fit the new container.",
    "faststart": "Reorder an MP4 so it starts playing straight away on a website or phone before it has finished downloading. Nothing is re-encoded, so it is instant.",
    "social": "One click to make a video that TikTok, Instagram Reels, YouTube Shorts, YouTube, X or an Instagram feed post will accept: the right size, H.264 video, AAC sound and a fast-start MP4. If the shape does not match you choose blurred background, black bars or crop to fill.",
    "compress": "Shrink a video to a target file size with a two-pass encode. Pick a ready-made limit (Discord 25 MB, WhatsApp 16 MB, email 10 MB, Telegram 50 MB) or type your own size in MB.",
    "recode": "Full control over how the video is encoded: codec (H.264, H.265 or VP9), quality, frame rate, height in pixels and audio bitrate. Use 0 to keep the frame rate or height as it is.",
    # ---- Audio
    "extract": "Pull the sound out of a video and save it as mp3, wav, m4a, flac, ogg or opus.",
    "aconvert": "Convert an audio file to another format: mp3, wav, m4a, flac, ogg or opus.",
    "loudnorm": "Even out the loudness to a standard target: -14 LUFS for YouTube and Spotify, -16 for podcasts and Apple, or -23 for broadcast. Works on audio and video files.",
    "silence": "Cut out the quiet gaps from a recording. Set how quiet counts as silence (dB) and how long a gap must last before it is removed. The result is audio only.",
    "atrim": "Cut one section out of an audio file. Type a start and end time; leave End blank to go to the end. Has the same film strip and live preview as Trim, with a waveform.",
    "fade": "Fade the sound in at the start and out at the end, with the length of each in seconds. Tick 'Fade the picture too' to fade a video's picture as well.",
    "volume": "Make the sound louder or quieter. Gain is in decibels: +6 is roughly twice as loud, -6 roughly half.",
    "tempo": "Speed up or slow down audio without changing its pitch, so voices do not turn into chipmunks. 1.25 is a quarter faster.",
    "pitch": "Raise or lower the pitch in semitones (up to 12 either way) while keeping the speed the same.",
    "denoise": "Reduce steady background hiss and hum in a recording. A noise floor of -25 dB is gentle; -40 dB is strong.",
    "eq": "A simple high-pass and low-pass filter. Cut below a frequency to remove rumble (80 Hz is common) and/or cut above a frequency to remove hiss. 0 turns a side off.",
    "mix": "Add background music under a video's own sound, or replace the sound completely. Choose the music file, set both volumes (the music defaults to a quiet 0.25) and tick Loop to repeat short music for the whole video.",
    "narration": "Lay a voiceover on a video. The original sound is turned down (ducked) under the narration, or replaced entirely. Made for recordings from the Narration Engine, but any audio file works.",
    "channels": "Turn stereo sound into mono, or split it into separate left and right files.",
    # ---- Images & Frames
    "thumb": "Save a single frame from the video as a picture. Type the time to grab it from. A slider with a live picture lets you find the right frame.",
    "frames": "Save a series of pictures from the video into a folder, one every N seconds.",
    "contact": "Make one picture showing a grid of frames from across the whole video, like a contact sheet. Choose the number of columns, rows and the thumbnail width.",
    "gif": "Turn a section of a video into an animated GIF with an optimised colour palette. Set the start, length, frames per second and width. The film strip shows the section as a window you can drag.",
    "imgconv": "Convert a picture to jpg, png or webp and optionally resize it. Choose 'Whole folder' at the top to convert every image in a folder at once.",
    # ---- Analysis
    "probe": "Write a full technical report about a file (codecs, size, bitrate, tracks, metadata) to a text file, using ffprobe.",
    "silencedetect": "List where the quiet gaps are in a file, with start times and lengths, without changing the file.",
    "blackdetect": "List where black frames appear in a video, handy for finding fades and bad cuts.",
    "freezedetect": "List where the picture is frozen for longer than a chosen time, handy for spotting dropped frames.",
    "scenes": "List the times where the scene changes. Lower sensitivity numbers find more cuts.",
    "loudness": "Measure how loud a file is (integrated LUFS, loudness range, true peak) without changing it.",
    "wave": "Draw the sound as a waveform picture.",
    "spectro": "Draw the sound as a spectrogram picture, showing how the frequencies change over time.",
    "stripmeta": "Remove the hidden metadata (location, device, title and so on) from a file without re-encoding, so there is no quality loss.",
    # ---- Streaming
    "hls": "Cut a video into small pieces with a playlist (.m3u8) so it can be streamed from a website. Choose how long each piece is.",
    # ---- Combine
    "watermark": "Put your logo on a video. Choose the logo picture (PNG with transparency works best), where it goes, how wide it is as a percentage of the video, its opacity and the margin.",
    "pip": "Show a small video in the corner of a bigger one (picture-in-picture). Choose the small video, its corner, its size as a percentage of the main video and the margin.",
    "grid": "Put several videos on screen at once: side by side, stacked, or in a 2-column grid. Add the clips in the dialog; the sound comes from the first video.",
    "crossfade": "Join clips with a transition between them. Choose the transition length and a style such as fade, dissolve, fade to black, wipe, slide or circle open. Each clip must be longer than the transition.",
    "slideshow": "Turn photos into a video with an optional slow zoom (Ken Burns) effect. Add photos or a folder, choose seconds per photo, effect, video size, crossfade length and optional music.",
    # ---- Text & Subtitles
    "burnsubs": "Burn captions permanently into the picture from an .srt or .ass file. Choose font size, position (bottom, middle, top) and outline thickness.",
    "addsubs": "Add a subtitle track to a video that viewers can switch on and off. Choose the .srt file and its language code (eng, urd, pan and so on). No re-encoding.",
    "extractsubs": "Save a video's built-in subtitles as an srt, ass or vtt file. Choose which subtitle track to take.",
    "convsubs": "Convert a subtitle file between srt, ass and vtt. Select the subtitle file as the Input first.",
    "drawtext": "Write a title or lower third on the video. Choose the text, position, size, colour, an optional dark box behind it, and the times it appears and disappears.",
    "timer": "Put a running clock on the video: count up from 0 or count down from a number of seconds. Choose position, size, colour and an optional dark box.",
    # ---- Effects & Colour
    "colour": "Adjust brightness, contrast, saturation and gamma. The defaults give a small, pleasing lift. The dialog shows a preview of the result, and a Play sample button.",
    "lut": "Apply a colour 'look' from a .cube LUT file, like the presets sold for cinematic grading.",
    "blur": "Blur the whole picture. Strength runs from 1 to 30.",
    "sharpen": "Make the picture crisper. Strength runs from 0.5 to 3.",
    "vdenoise": "Reduce grain and noise in the picture. Strength runs from 1 to 10.",
    "vignette": "Darken the corners of the picture for a focused, cinematic look. Lower numbers give a stronger effect.",
    "chroma": "Remove a green screen (or any single colour) and replace it with a picture or a solid colour. Choose the key colour, how similar a colour must be to count, and how soft the edge is.",
    "vfade": "Fade the picture and sound in at the start and out at the end, to black or to white.",
    "bw": "Turn colour footage black and white, or give it a sepia (old photograph) tint.",
    "ramp": "Slow down or speed up just one section of the video while the rest plays normally. Choose where the section starts and ends and its speed (0.25 is slow motion, 3 is fast).",
    "deinterlace": "Remove the comb-like lines from old interlaced footage, such as video from old camcorders or TV recordings.",
}
CAT_DOCS = {
    "Video": "Cut, join, reshape and re-time video.",
    "Format & Quality": "Change formats, shrink file sizes and prepare videos for social media and the web.",
    "Audio": "Convert, clean up, fix the loudness of and add sound.",
    "Images & Frames": "Grab frames, make GIFs and contact sheets, convert pictures.",
    "Analysis": "Look inside a file without changing it: reports, silences, scenes, loudness, waveforms.",
    "Streaming": "Prepare video for streaming from a website.",
    "Combine": "Put clips, photos, logos and videos together.",
    "Text & Subtitles": "Captions, subtitle files, titles and timers.",
    "Effects & Colour": "Colour, blur, fades, green screen and speed ramps.",
}


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

    # Start-up splash: the main window stays hidden while it is built, a colour logo card shows for ~2 s, then the window appears.
    splash = {"win": None, "t0": time.time()}
    try:
        root.withdraw()
        sw = tk.Toplevel(root)
        sw.overrideredirect(True)
        sw.attributes("-topmost", True)
        sp_bg, sp_fg = "#f7f1e1", "#0f2a47"
        hi = float(root.tk.call("tk", "scaling")) >= 1.7
        sp_img = tk.PhotoImage(data=SPLASH_B64["2x" if hi else "1x"], master=sw)
        card = tk.Frame(sw, bg=sp_bg, highlightthickness=3, highlightbackground=sp_fg)
        card.pack(fill="both", expand=True)
        lb = tk.Label(card, image=sp_img, bg=sp_bg, bd=0)
        lb.image = sp_img
        lb.pack(padx=40, pady=(30, 8))
        tk.Label(card, text=f"{APP}  {VERSION}", bg=sp_bg, fg=sp_fg, font=("Segoe UI", 16, "bold")).pack()
        tk.Label(card, text="O.M. Industries Inc", bg=sp_bg, fg="#b5472a", font=("Segoe UI", 11)).pack(pady=(0, 4))
        tk.Label(card, text="Loading...", bg=sp_bg, fg="#6b7280", font=("Segoe UI", 9)).pack(pady=(0, 22))
        sw.update_idletasks()
        w_, h_ = sw.winfo_reqwidth(), sw.winfo_reqheight()
        sw.geometry(f"{w_}x{h_}+{(sw.winfo_screenwidth() - w_) // 2}+{(sw.winfo_screenheight() - h_) // 2}")
        sw.update()
        splash["win"] = sw
    except Exception:  # noqa: BLE001 - never let the splash stop the app from starting
        splash["win"] = None
        try:
            root.deiconify()
        except Exception:  # noqa: BLE001
            pass

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
    f_small = tkfont.Font(family=UI_FONT, size=fsize["v"] - 1)
    f_icon = tkfont.Font(family=UI_FONT, size=fsize["v"] + 6)
    # Report Tk callback exceptions to the log and a message box instead of failing silently.
    def _tk_error(et, ev, tb):
        import traceback
        write_log("GUI ERROR\n" + "".join(traceback.format_exception(et, ev, tb)))
        sys.__excepthook__(et, ev, tb)
    root.report_callback_exception = _tk_error
    write_log(f"--- {APP} v{VERSION} started ({'installed exe' if FROZEN else 'script'}, python {sys.version.split()[0]}, ffmpeg: {ff_version() if find_tool('ffmpeg') else 'not found'}) ---")
    wrapped, themed_text, themed_canvas = [], [], []
    themed, rethemers = [], []  # plain tk widgets that follow the light/dark palette
    state = {"running": False, "outputs": [], "outdir": None, "theme": theme, "multi": None, "multi_label": "", "queue": []}

    # Current palette (light or dark).
    def pal():
        return PALETTE[state["theme"]]

    # Scale all named fonts from the A+/A- size setting.
    def apply_fonts():
        n = fsize["v"]
        for f_, extra in ((f_base, 0), (f_bold, 0), (f_head, 3), (f_mono, -1), (f_title, 9), (f_brand, 2), (f_small, -1), (f_icon, 6)):
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
        try:
            root.configure(bg=p["bg"])
        except tk.TclError:
            pass
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
        retheme()
        settings["fsize"] = fsize["v"]
        save_settings(settings)

    # Switch light/dark (sv_ttk) and re-apply styles and title bar.
    def toggle_theme():
        if not sv_ttk:
            return
        state["theme"] = "dark" if state["theme"] == "light" else "light"
        sv_ttk.set_theme(state["theme"])
        apply_styles()
        retheme()
        for c_ in ["Favourites"] + CATEGORIES:
            build_cat(c_)
        build_search() if search_var.get().strip() else show_cat()
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

    # About window: what the app is, plus every tool in every group with a plain-English description. Written from TOOL_DOCS so it always matches the real tool list.
    ABOUT_URL = f"https://github.com/{GITHUB_REPO}/blob/main/docs/ABOUT.md"

    def show_about():
        import webbrowser
        w = tk.Toplevel(root)
        w.title(tr("About"))
        w.transient(root)
        w.geometry("820x720")
        w.configure(bg=pal()["bg"])
        top_f = ttk.Frame(w, padding=(18, 14, 18, 6))
        top_f.pack(fill="x")
        try:
            big = tk.PhotoImage(data=SPLASH_B64["1x"], master=w)
            small = big.subsample(2, 2)
            lb = tk.Label(top_f, image=small, bg=pal()["bg"], bd=0)
            lb.image = small
            lb.pack(side="left", padx=(0, 16))
        except Exception:  # noqa: BLE001
            pass
        tx = ttk.Frame(top_f)
        tx.pack(side="left", fill="x", expand=True)
        ttk.Label(tx, text=f"{APP} {VERSION}", style="Head.TLabel").pack(anchor="w")
        ttk.Label(tx, text="O.M. Industries Inc", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(tx, text="A friendly point-and-click front end for ffmpeg: choose a file, press a tool, set a few options, run. "
                           f"{len(OPS)} tools in {len(CATEGORIES)} groups, nothing to type on a command line.",
                  wraplength=520, justify="left").pack(anchor="w", pady=(6, 6))
        btns = ttk.Frame(tx)
        btns.pack(anchor="w")
        ttk.Button(btns, text="Open the illustrated guide", command=lambda: webbrowser.open(ABOUT_URL)).pack(side="left")
        ttk.Button(btns, text="GitHub page", command=lambda: webbrowser.open(f"https://github.com/{GITHUB_REPO}")).pack(side="left", padx=6)
        ttk.Button(btns, text="Close", command=w.destroy).pack(side="left")
        body_f = ttk.Frame(w, padding=(18, 6, 18, 14))
        body_f.pack(fill="both", expand=True)
        txt = tk.Text(body_f, wrap="word", bg=pal()["log_bg"], fg=pal()["log_fg"], relief="flat", padx=14, pady=10,
                      font=f_base, cursor="arrow", spacing1=2, spacing3=4)
        sb = ttk.Scrollbar(body_f, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("cat", font=f_head, foreground=BRAND_GOLD if state["theme"] == "dark" else "#0f2a47", spacing1=14, spacing3=2)
        txt.tag_configure("catd", foreground=pal()["muted"], spacing3=6)
        txt.tag_configure("tool", font=f_bold, spacing1=8)
        for cat in CATEGORIES:
            txt.insert("end", f"{CAT_ICONS.get(cat, '')}  {tr(cat)}\n", "cat")
            if CAT_DOCS.get(cat):
                txt.insert("end", CAT_DOCS[cat] + "\n", "catd")
            for o in OPS:
                if o["cat"] == cat:
                    txt.insert("end", o["label"] + "\n", "tool")
                    txt.insert("end", TOOL_DOCS.get(o["id"], o.get("tip") or "") + "\n")
        txt.insert("end", "\nGPU encoding\n", "cat")
        txt.insert("end", "If a supported graphics card is found (NVIDIA, AMD, Intel or Apple) the app uses it to encode video faster; "
                          "otherwise it uses the CPU. Settings has a Check graphics card button that shows what was found.\n")
        txt.configure(state="disabled")

    # ------------------------------------------------------------------ new look: colours, small widgets, header
    GOLD, GOLD_HOVER, ON_GOLD, GREEN = "#f4be46", "#ffd77a", "#1a1405", "#5fd08a"

    # Register a plain tk widget so its colours follow the light/dark palette (opts: option -> palette key).
    def reg(w, **opts):
        themed.append((w, opts))
        try:
            w.configure(**{k: pal()[v] for k, v in opts.items()})
        except tk.TclError:
            pass
        return w

    # Re-colour every registered widget after a theme switch.
    def retheme():
        p = pal()
        try:
            root.configure(bg=p["bg"])
        except tk.TclError:
            pass
        alive = []
        for w, opts in list(themed):
            try:
                w.configure(**{k: p[v] for k, v in opts.items()})
                alive.append((w, opts))
            except tk.TclError:
                pass  # widget was destroyed: forget it
        themed[:] = alive
        for fn in list(rethemers):
            try:
                fn()
            except tk.TclError:
                if fn in rethemers:
                    rethemers.remove(fn)
            except Exception:  # noqa: BLE001
                pass

    # Rounded rectangle on a canvas (a smoothed polygon).
    def rrect(cv, x1, y1, x2, y2, r, **kw):
        r = max(1, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
        pts = []
        for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
            for k in range(9):
                a = math.radians(a0 + k * 10)
                pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return cv.create_polygon(pts, **kw)

    # A panel with rounded corners. Returns (canvas, inner frame): pack/grid the canvas, put the content in the inner frame.
    def round_frame(parent, fill="panel", line="border", r=10, bgkey="bg"):
        m = max(3, r // 2)
        cv = tk.Canvas(parent, highlightthickness=0, bd=0, height=20)
        reg(cv, bg=bgkey)
        inner = reg(tk.Frame(cv), bg=fill)
        win = cv.create_window(m, m, window=inner, anchor="nw")

        def draw(*_):
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 8 or h < 8:
                return
            p = pal()
            try:
                cv.delete("bg")
                rrect(cv, 1, 1, w - 1, h - 1, r, fill=p[fill], outline=p[line], tags="bg")
                cv.tag_lower("bg")
                cv.itemconfigure(win, width=w - 2 * m, height=h - 2 * m)
            except tk.TclError:
                pass

        def fit(_e=None):
            try:
                cv.configure(height=inner.winfo_reqheight() + 2 * m)
            except tk.TclError:
                pass
        cv.bind("<Configure>", draw)
        inner.bind("<Configure>", fit)
        rethemers.append(draw)
        cv.redraw = draw
        return cv, inner

    # A rounded button drawn on a canvas (gold = main action, ghost = bordered, hdr = on the navy header).
    def flat_btn(parent, text, command, kind="ghost", font=None, bgkey=None):
        fnt = font or (f_bold if kind == "gold" else f_base)
        padx, pady = {"gold": (18, 9), "hdr": (12, 7)}.get(kind, (14, 8))
        bgkey = bgkey or ("brand" if kind == "hdr" else "panel")
        cv = tk.Canvas(parent, highlightthickness=0, bd=0, cursor="hand2")
        reg(cv, bg=bgkey)
        st = {"hover": False, "text": tr(text)}

        def draw(*_):
            p = pal()
            try:
                w = fnt.measure(st["text"]) + 2 * padx
                h = fnt.metrics("linespace") + 2 * pady
                cv.configure(width=w, height=h)
                cv.delete("all")
                if kind == "gold":
                    fill = line = GOLD_HOVER if st["hover"] else GOLD
                    fg = ON_GOLD
                elif kind == "hdr":
                    fill = line = "#1d4470" if st["hover"] else BRAND_BG
                    fg = "#ffffff" if st["hover"] else "#c9d6e6"
                else:
                    fill, line, fg = (p["hover"] if st["hover"] else p["panel"]), p["border"], p["fg"]
                rrect(cv, 1, 1, w - 1, h - 1, 12, fill=fill, outline=line)
                cv.create_text(w // 2, h // 2, text=st["text"], font=fnt, fill=fg)
            except tk.TclError:
                pass

        def hov(on):
            st["hover"] = on
            draw()

        def set_text(t):
            st["text"] = tr(t)
            draw()
        cv.set_text = set_text
        cv.bind("<Enter>", lambda e: hov(True))
        cv.bind("<Leave>", lambda e: hov(False))
        cv.bind("<Button-1>", lambda e: command())
        rethemers.append(draw)
        draw()
        return cv

    # On/off switch drawn on a canvas (gold when on).
    def make_switch(parent, var, command=None):
        cv = tk.Canvas(parent, width=46, height=26, highlightthickness=0, bd=0, cursor="hand2", takefocus=1)

        def draw(*_):
            p = pal()
            try:
                cv.configure(bg=p["bg"])
                cv.delete("all")
                on = bool(var.get())
                off_track = "#3a4553" if state["theme"] == "dark" else "#b8c0ca"
                dis = str(cv.cget("state")) == "disabled"
                track = (GOLD if on else off_track) if not dis else off_track
                cv.create_oval(0, 0, 26, 26, fill=track, outline=track)
                cv.create_oval(20, 0, 46, 26, fill=track, outline=track)
                cv.create_rectangle(13, 0, 33, 26, fill=track, outline=track)
                x = 23 if on else 3
                cv.create_oval(x, 3, x + 20, 23, fill=("#14181d" if on and not dis else "#e8edf2"), outline="")
            except tk.TclError:
                pass

        def click(_e=None):
            if str(cv.cget("state")) == "disabled":
                return
            var.set(not var.get())
            if command:
                command()
        cv.bind("<Button-1>", click)
        cv.bind("<space>", click)
        cv.bind("<Return>", click)
        var.trace_add("write", lambda *a: draw())
        cv.redraw = draw
        draw()
        rethemers.append(draw)
        return cv

    # ------------------------------------------------------------------ header band (logo, name, About, Settings)
    header = reg(tk.Frame(root, bg=BRAND_BG), bg="brand")
    header.pack(fill="x")
    try:
        hi_dpi = float(root.tk.call("tk", "scaling")) >= 1.7
        logo_img = tk.PhotoImage(data=LOGO_B64["2x" if hi_dpi else "1x"])
        logo_lbl = reg(tk.Label(header, image=logo_img, bg=BRAND_BG, bd=0), bg="brand")
        logo_lbl.image = logo_img  # keep a reference or tk drops the picture
        logo_lbl.pack(side="left", padx=(18, 4), pady=6)
    except Exception:  # noqa: BLE001 - the header simply stays without a logo
        pass
    reg(tk.Label(header, text=APP, bg=BRAND_BG, fg=BRAND_FG, font=f_brand), bg="brand").pack(side="left", padx=(8, 8), pady=10)
    reg(tk.Label(header, text=f"v{VERSION}", bg=BRAND_BG, fg="#9fb6cf", font=f_small), bg="brand").pack(side="left", pady=(14, 8))
    flat_btn(header, "⚙  Settings", lambda: open_settings(), "hdr").pack(side="right", padx=(0, 14), pady=10)
    flat_btn(header, "About", lambda: show_about(), "hdr").pack(side="right", padx=2, pady=10)

    lang_var = tk.StringVar(value=LANGS.get(LANG["code"], "English"))

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

    # ------------------------------------------------------------------ input card: thumbnail, name, info chips, save folder
    name_var = tk.StringVar(value="No file chosen")
    save_var = tk.StringVar()
    chips_state = {"items": []}
    hint = "Drag a file or folder onto this window, or use the buttons on the right." if dnd else "Choose a file or a folder with the buttons on the right. (pip install tkinterdnd2 adds drag-and-drop.)"
    info_var.set(hint)
    card_cv, card = round_frame(root, "panel", "border", 16)
    card_cv.pack(fill="x", padx=14, pady=(10, 4))
    card.columnconfigure(1, weight=1)
    thumb_box = reg(tk.Frame(card, width=172, height=100), bg="chip")
    thumb_box.grid(row=0, column=0, rowspan=2, sticky="nw", padx=12, pady=12)
    thumb_box.pack_propagate(False)
    thumb_lbl = reg(tk.Label(thumb_box, text="＋", font=f_title), bg="chip", fg="muted")
    thumb_lbl.pack(fill="both", expand=True)
    mid = reg(tk.Frame(card), bg="panel")
    mid.grid(row=0, column=1, sticky="nsew", pady=(12, 0))
    reg(tk.Label(mid, textvariable=name_var, font=f_head, anchor="w"), bg="panel", fg="fg").pack(fill="x")
    chips_fr = reg(tk.Frame(mid), bg="panel")
    chips_fr.pack(fill="x", pady=(6, 2))
    info_lbl = reg(tk.Label(mid, textvariable=info_var, font=f_small, anchor="w", justify="left", wraplength=700), bg="panel", fg="muted")
    info_lbl.pack(fill="x")
    wrapped.append(info_lbl)
    saves = reg(tk.Frame(mid), bg="panel")
    saves.pack(fill="x", pady=(6, 0))
    reg(tk.Label(saves, text="Saves to", font=f_small), bg="panel", fg="muted").pack(side="left")
    reg(tk.Label(saves, textvariable=save_var, font=f_small), bg="panel", fg="fg").pack(side="left", padx=(6, 8))
    chg = tk.Label(saves, text="Change", font=f_small, fg=GOLD, cursor="hand2")
    reg(chg, bg="panel")
    chg.pack(side="left")
    rst = tk.Label(saves, text="Same as source", font=f_small, fg=GOLD, cursor="hand2")
    reg(rst, bg="panel")
    rst.pack(side="left", padx=(12, 0))
    right = reg(tk.Frame(card), bg="panel")
    right.grid(row=0, column=2, sticky="ne", padx=12, pady=12)
    # Second line: a path box (paste or type a path) and the recent-files list.
    pathrow = reg(tk.Frame(card), bg="panel")
    pathrow.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=(0, 12))
    pathrow.columnconfigure(0, weight=1)
    ent = ttk.Entry(pathrow, textvariable=path_var)
    ent.grid(row=0, column=0, sticky="ew")
    recent = ttk.Combobox(pathrow, values=settings.get("recent", []), state="readonly", width=22)
    recent.grid(row=0, column=1, padx=(8, 0))
    recent.set(tr("Recent files"))

    def out_changed(*_):
        save_var.set(out_var.get() or tr("next to the original file"))
    out_var.trace_add("write", out_changed)
    out_changed()

    # Draw the little grey info chips (length, size, codec ...) under the file name.
    def set_chips(items=None):
        if items is not None:
            chips_state["items"] = list(items)
        for c in chips_fr.winfo_children():
            c.destroy()
        p = pal()
        for t in chips_state["items"]:
            tk.Label(chips_fr, text=t, font=f_small, bg=p["chip"], fg=p["fg"], padx=8, pady=2).pack(side="left", padx=(0, 6))
        register_drop_tree(chips_fr)
    rethemers.append(set_chips)

    # Thumbnail of the chosen file (or a placeholder symbol).
    def set_thumb(png=None, glyph="＋"):
        try:
            if png:
                img = tk.PhotoImage(file=png)
                thumb_lbl.configure(image=img, text="")
                thumb_lbl.image = img
            else:
                thumb_lbl.configure(image="", text=glyph)
                thumb_lbl.image = None
        except tk.TclError:
            thumb_lbl.configure(image="", text=glyph)

    # Hide the small grey message line when it is empty.
    def info_changed(*_):
        if info_var.get().strip():
            info_lbl.pack(fill="x", before=saves)
        else:
            info_lbl.pack_forget()
    info_var.trace_add("write", info_changed)

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
        name_var.set(f"{len(files)} files selected")
        set_chips([Path(f).name for f in files[:3]] + (["..."] if len(files) > 3 else []))
        set_thumb(None, "▦")
        info_var.set("Pick an operation to run it on all of them (or add it to the queue).")

    # Return the multi-selection if it is still valid.
    def current_multi():
        return state["multi"] if state["multi"] and path_var.get() == state["multi_label"] else None

    # 'Choose files...' dialog (multi-select).
    def browse_file():
        ft = [("Media files", " ".join("*" + e for e in sorted(VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | SUB_EXT))), ("All files", "*.*")]
        ps = filedialog.askopenfilenames(title="Choose one or more files", filetypes=ft, initialdir=settings.get("lastdir") or None)
        if ps:
            settings["lastdir"] = str(Path(ps[0]).parent)
            if len(ps) > 1:
                set_multi(ps)
            else:
                set_path(os.path.normpath(ps[0]))

    # 'Whole folder...' dialog.
    def browse_folder():
        p = filedialog.askdirectory(title="Choose a folder (batch)", initialdir=settings.get("lastdir") or None)
        if p:
            settings["lastdir"] = p
            set_path(os.path.normpath(p))

    flat_btn(right, "Choose files…", browse_file, "gold").pack(fill="x")
    flat_btn(right, "Whole folder…", browse_folder, "ghost").pack(fill="x", pady=(8, 0))
    recent.bind("<<ComboboxSelected>>", lambda e: set_path(recent.get()))

    # 'Change' for a custom output folder; empty = same folder as the source.
    def browse_out():
        p = filedialog.askdirectory(title="Output folder")
        if p:
            out_var.set(os.path.normpath(p))
            settings["outdir"] = out_var.get()
            save_settings(settings)
    chg.bind("<Button-1>", lambda e: browse_out())
    rst.bind("<Button-1>", lambda e: out_var.set(""))
    ent.bind("<Return>", lambda e: refresh_info())

    # Short chips for the info strip: length, size, codec ...
    def chips_for(inf):
        out = []
        if inf["dur"]:
            mm, ss = divmod(inf["dur"], 60)
            hh, mm = divmod(int(mm), 60)
            out.append(f"{hh}:{mm:02d}:{int(ss):02d}" if hh else f"{mm}:{int(ss):02d}")
        if inf["has_v"]:
            out.append(f'{inf["w"]}x{inf["h"]}')
            out.append((f'{inf["v"].get("codec_name", "?")} ' + (f'{inf["fps"]:.0f}fps' if inf["fps"] else "")).strip())
        if inf["has_a"]:
            out.append(f'audio {inf["a"].get("codec_name", "?")}')
        if inf["size"]:
            out.append(f'{inf["size"] / 1e6:.1f} MB')
        return out

    # Apply a finished file probe (only if the box still holds the same file).
    def show_file(p, items, png, glyph):
        if path_var.get().strip().strip('"') != p:
            return
        set_chips(items)
        set_thumb(png, glyph)
        info_var.set("")

    # Probe the current input in a worker thread and show its name, chips and thumbnail.
    def refresh_info():
        if current_multi():
            return
        p = path_var.get().strip().strip('"')
        if not p:
            name_var.set("No file chosen")
            set_chips([])
            set_thumb(None)
            info_var.set(hint)
            return
        if not have_ff:
            info_var.set("ffmpeg is not installed yet. Use the Install ffmpeg prompt (or restart the app) to download it.")
            return
        if os.path.isdir(p):
            n = sum(1 for f in Path(p).iterdir() if f.suffix.lower() in (VIDEO_EXT | AUDIO_EXT | IMAGE_EXT))
            name_var.set(Path(p).name or p)
            set_chips([f"{n} media files"])
            set_thumb(None, "\U0001F4C1")
            info_var.set("Whole folder: every media file in it is processed.")
        elif os.path.isfile(p):
            name_var.set(Path(p).name)
            set_chips([])
            info_var.set("Reading file…")

            def work():
                inf = probe(p)
                items = chips_for(inf)
                png, glyph = None, "\U0001F3B5"
                if inf["has_v"]:
                    glyph = "\U0001F3AC"
                    tmp = os.path.join(tempfile.gettempdir(), "om_input_thumb.png")
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
                    if grab_frame(p, min(1.0, inf["dur"] / 3) if inf["dur"] else 0, tmp, box=(168, 96)):
                        png = tmp
                q.put(("call", lambda: show_file(p, items, png, glyph)))
            threading.Thread(target=work, daemon=True).start()
        else:
            name_var.set("No file chosen")
            set_chips([])
            set_thumb(None)
            info_var.set("That path does not exist.")

    # ------------------------------------------------------------------ bottom bar (packed first so the body can expand)
    bot = reg(tk.Frame(root), bg="bg")
    bot.pack(side="bottom", fill="x")
    row_cv, row = round_frame(bot, "panel", "border", 16)
    row_cv.pack(side="bottom", fill="x", padx=14, pady=(4, 10))
    # Details panel: the full log. Hidden until "Show details" is pressed (opens by itself when something fails).
    detail = reg(tk.Frame(bot), bg="bg")
    dhead = reg(tk.Frame(detail), bg="bg")
    dhead.pack(fill="x", pady=(0, 4))
    reg(tk.Label(dhead, text="Details", font=f_bold), bg="bg", fg="fg").pack(side="left")
    ttk.Button(dhead, text="Copy last command", command=lambda: copy_last()).pack(side="right")
    logwrap = tk.Frame(detail)
    logwrap.pack(fill="x")
    logbox = tk.Text(logwrap, height=8, wrap="word", font=f_mono, state="disabled", relief="flat", borderwidth=0, padx=8, pady=6)
    logsb = ttk.Scrollbar(logwrap, orient="vertical", command=logbox.yview)
    logbox.configure(yscrollcommand=logsb.set)
    logsb.pack(side="right", fill="y")
    logbox.pack(side="left", fill="x", expand=True)
    themed_text.append(logbox)

    dot = tk.Canvas(row, width=14, height=14, highlightthickness=0, bd=0)
    reg(dot, bg="panel")
    dot.pack(side="left", padx=(12, 6), pady=12)
    dot_item = dot.create_oval(2, 2, 12, 12, fill=GREEN, outline="")
    status = reg(tk.Label(row, textvariable=status_var, font=f_base, anchor="w", width=24), bg="panel", fg="fg")
    status.pack(side="left")

    # Colour of the status dot: green = ready / finished, gold = running, red = something failed.
    def status_dot(*_):
        s = status_var.get()
        low = s.lower()
        col = GOLD if low.startswith(("running", "queue running", "reading")) else ("#ff6b6b" if ("failed" in low and " 0 failed" not in low) else GREEN)
        dot.itemconfigure(dot_item, fill=col)
    status_var.trace_add("write", status_dot)

    det = {"on": False}

    # Show or hide the details (log) panel.
    def set_details(on):
        det["on"] = bool(on)
        if on:
            detail.pack(fill="x", padx=14, pady=(6, 0), before=row_cv)
            details_btn.configure(text="Hide details")
        else:
            detail.pack_forget()
            details_btn.configure(text="Show details")
    open_btn = ttk.Button(row, text="Open output folder", state="disabled", command=lambda: open_folder())
    open_btn.pack(side="right", padx=(6, 12), pady=8)
    queue_btn = ttk.Button(row, text="Queue (0)", command=lambda: open_queue())
    queue_btn.pack(side="right", padx=3, pady=8)
    cancel_btn = ttk.Button(row, text="Cancel", state="disabled", command=lambda: cancel.set())
    cancel_btn.pack(side="right", padx=3, pady=8)
    details_btn = ttk.Button(row, text="Show details", command=lambda: set_details(not det["on"]))
    details_btn.pack(side="right", padx=3, pady=8)
    pill = tk.Label(row, text="", font=f_small, padx=10, pady=3, cursor="hand2")
    reg(pill, bg="chip")
    pill.pack(side="right", padx=8, pady=8)
    pill.bind("<Button-1>", lambda e: open_settings())
    bar = ttk.Progressbar(row, maximum=1000)
    bar.pack(side="left", fill="x", expand=True, padx=10)

    # ------------------------------------------------------------------ graphics card state (switch lives in Settings)
    gpu_info = {"label": GPU_ENCODERS[gpu_key]["label"] if gpu_available else None}
    gpu_ui = {}

    # Update the bottom-bar pill and, if open, the Settings window's graphics-card row.
    def refresh_gpu_ui():
        p = pal()
        if gpu_info["label"] and gpu_var.get():
            pill.configure(text="●  Graphics card on", fg=GREEN)
        elif gpu_info["label"]:
            pill.configure(text="Graphics card off", fg=p["muted"])
        else:
            pill.configure(text="Using the processor", fg=p["muted"])
        try:
            lbl, sw = gpu_ui.get("status"), gpu_ui.get("switch")
            if lbl is not None:
                if gpu_info["label"]:
                    lbl.configure(text=f"Graphics card found and working ({gpu_info['label']})", fg=GREEN)
                else:
                    lbl.configure(text="No graphics card found. The processor is used.", fg=p["muted"])
            if sw is not None:
                sw.configure(state="normal" if gpu_info["label"] else "disabled")
                sw.redraw()
        except tk.TclError:
            pass
    rethemers.append(refresh_gpu_ui)

    # "Check graphics card": re-runs detection (e.g. after installing a driver), shows the details, and updates the switch.
    def check_gpu():
        if not refresh_tools():
            messagebox.showinfo("Check GPU", "ffmpeg was not found, so the GPU cannot be tested yet.")
            return
        btn = gpu_ui.get("btn")
        try:
            btn.set_text("Checking...")
        except (tk.TclError, AttributeError):
            pass

        def work():
            text, key = gpu_report()
            root.after(0, lambda: done(text, key))

        def done(text, key):
            try:
                btn.set_text("Check graphics card")
            except (tk.TclError, AttributeError):
                pass
            if key:
                STATE["gpu_enc"] = key
                gpu_info["label"] = GPU_ENCODERS[key]["label"]
                gpu_var.set(True)
                STATE["gpu"] = True
            else:
                gpu_info["label"] = None
                gpu_var.set(False)
                STATE["gpu"] = False
            refresh_gpu_ui()
            messagebox.showinfo("Check GPU", text)
        threading.Thread(target=work, daemon=True).start()
    snd_var = tk.BooleanVar(value=settings.get("sounds", True))
    startup_var = tk.BooleanVar(value=settings.get("startup_sound", True))

    # Sound switches: persist the setting and play a sample ping when "job finished" is switched on.
    def toggle_sound():
        settings["sounds"] = snd_var.get()
        save_settings(settings)
        if snd_var.get():
            play_ping("job")

    def toggle_startup():
        settings["startup_sound"] = startup_var.get()
        save_settings(settings)

    # ------------------------------------------------------------------ Settings window
    settings_win = {"w": None}

    def open_settings():
        old = settings_win["w"]
        if old is not None:
            try:
                old.deiconify()
                old.lift()
                old.focus_force()
                return
            except tk.TclError:
                settings_win["w"] = None
        w = tk.Toplevel(root)
        settings_win["w"] = w
        w.title("Settings")
        w.transient(root)
        w.resizable(False, False)
        reg(w, bg="bg")
        hb = reg(tk.Frame(w, bg=BRAND_BG), bg="brand")
        hb.pack(fill="x")
        reg(tk.Label(hb, text="Settings", bg=BRAND_BG, fg=BRAND_FG, font=f_brand), bg="brand").pack(side="left", padx=24, pady=14)
        foot = reg(tk.Frame(w, highlightthickness=1), bg="panel", highlightbackground="border")
        foot.pack(side="bottom", fill="x")

        def close():
            settings_win["w"] = None
            for k in ("status", "switch", "btn"):
                gpu_ui.pop(k, None)
            w.destroy()
        flat_btn(foot, "Done", close, "gold").pack(side="right", padx=24, pady=12)
        w.protocol("WM_DELETE_WINDOW", close)
        w.bind("<Escape>", lambda e: close())
        bodyf = reg(tk.Frame(w), bg="bg")
        bodyf.pack(fill="both", expand=True, padx=24, pady=(16, 8))

        def section(title, first=False):
            if not first:
                reg(tk.Frame(bodyf, height=1), bg="border").pack(fill="x", pady=(14, 14))
            reg(tk.Label(bodyf, text=title.upper(), font=f_small, anchor="w"), bg="bg", fg="muted").pack(fill="x", pady=(0, 8))

        def line(label, sub=None):
            fr = reg(tk.Frame(bodyf), bg="bg")
            fr.pack(fill="x", pady=5)
            lf = reg(tk.Frame(fr), bg="bg")
            lf.pack(side="left", fill="x", expand=True)
            reg(tk.Label(lf, text=label, font=f_base, anchor="w", justify="left"), bg="bg", fg="fg").pack(anchor="w")
            subl = None
            if sub is not None:
                subl = reg(tk.Label(lf, text=sub, font=f_small, anchor="w", justify="left"), bg="bg", fg="muted")
                subl.pack(anchor="w")
            return fr, subl

        section("Appearance", first=True)
        if sv_ttk:
            fr, _ = line("Theme")
            seg = reg(tk.Frame(fr, highlightthickness=1), bg="chip", highlightbackground="border")
            seg.pack(side="right")
            seg_btns = {}

            def draw_seg():
                for nm, b in seg_btns.items():
                    on = state["theme"] == nm
                    try:
                        b.configure(bg=GOLD if on else pal()["chip"], fg=ON_GOLD if on else pal()["fg"], font=f_bold if on else f_base)
                    except tk.TclError:
                        pass

            def pick(nm):
                if state["theme"] != nm:
                    toggle_theme()
                draw_seg()
            for nm, txt in (("dark", "Dark"), ("light", "Light")):
                b = tk.Label(seg, text=txt, padx=16, pady=6, cursor="hand2")
                b.pack(side="left", padx=2, pady=2)
                b.bind("<Button-1>", lambda e, nm=nm: pick(nm))
                seg_btns[nm] = b
            draw_seg()
            rethemers.append(draw_seg)
        fr, _ = line("Language")
        lb = ttk.Combobox(fr, textvariable=lang_var, values=list(LANGS.values()), state="readonly", width=18)
        lb.pack(side="right")
        lb.bind("<<ComboboxSelected>>", on_lang)
        fr, _ = line("Text size")
        reg(tk.Label(fr, text="A", font=f_small), bg="bg", fg="muted").pack(side="left", padx=(0, 6))
        fs_var = tk.DoubleVar(value=fsize["v"])

        def on_fs(v):
            n = int(round(float(v)))
            if n != fsize["v"]:
                set_fsize(n - fsize["v"])
        sc = ttk.Scale(fr, from_=10, to=22, orient="horizontal", length=150, variable=fs_var, command=on_fs)
        sc.pack(side="left")
        reg(tk.Label(fr, text="A", font=f_head), bg="bg", fg="muted").pack(side="left", padx=(6, 0))

        section("Speed")
        fr, sub = line("Use the graphics card when there is one", "")
        sw = make_switch(fr, gpu_var, lambda: (STATE.update(gpu=gpu_var.get()), refresh_gpu_ui()))
        sw.pack(side="right")
        gpu_ui["status"], gpu_ui["switch"] = sub, sw
        fr, _ = line("")
        gbtn = flat_btn(fr, "Check graphics card", check_gpu, "ghost", bgkey="bg")
        gbtn.pack(side="left")
        gpu_ui["btn"] = gbtn
        refresh_gpu_ui()

        section("Sounds")
        fr, _ = line("Play a sound when a job finishes")
        make_switch(fr, snd_var, toggle_sound).pack(side="right")
        fr, _ = line("Play the start-up sound")
        make_switch(fr, startup_var, toggle_startup).pack(side="right")

        section("For experts")
        fr, _ = line("Show the ffmpeg commands in the details")
        make_switch(fr, cmd_var).pack(side="right")
        w.update_idletasks()
        try:
            w.geometry(f"+{root.winfo_rootx() + 80}+{root.winfo_rooty() + 40}")
        except tk.TclError:
            pass
        title_bar_for(w)

    # Dark native title bar for a secondary window (best effort).
    def title_bar_for(win):
        if os.name != "nt":
            return
        try:
            import pywinstyles
            pywinstyles.apply_style(win, "dark" if state["theme"] == "dark" else "normal")
        except Exception:  # noqa: BLE001
            pass

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
    content.rowconfigure(1, weight=1)
    # Search box above the tool grid.
    search_var = tk.StringVar()
    sbar = ttk.Frame(content)
    sbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    sbar.columnconfigure(1, weight=1)
    ttk.Label(sbar, text=f"\U0001F50E  Search all {len(OPS)} tools").grid(row=0, column=0, padx=(2, 10))
    search_ent = ttk.Entry(sbar, textvariable=search_var)
    search_ent.grid(row=0, column=1, sticky="ew")
    pages = ttk.Frame(content)
    pages.grid(row=1, column=0, sticky="nsew")
    pages.columnconfigure(0, weight=1)
    pages.rowconfigure(0, weight=1)

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
            messagebox.showwarning(APP, "Please select an input file or folder first.\n\nUse Choose files..., Whole folder..., or drag a file onto this window.")
            return
        open_dialog(spec)

    # One-line description for a tool card: the first sentence of its plain-English description.
    def short_doc(spec):
        d = TOOL_DOCS.get(spec["id"]) or spec.get("tip") or ""
        d = d.strip().split(". ")[0].rstrip(".")
        if len(d) > 74:
            d = d[:74].rsplit(" ", 1)[0] + "…"
        return d

    # One tool card: icon, short title and a grey one-line description. The whole card is clickable.
    def make_card(parent, spec):
        p = pal()
        outer, c = round_frame(parent, "panel", "border", 16)
        c.configure(cursor="hand2", takefocus=1)
        c.columnconfigure(1, weight=1)
        icon = TOOL_ICONS.get(spec["id"]) or CAT_ICONS.get(spec["cat"], "")
        ic = tk.Label(c, text=icon, font=f_icon, bg=p["panel"], fg=GOLD, width=2)
        ic.grid(row=0, column=0, rowspan=2, padx=(8, 4), pady=8)
        title = tr(spec["label"]).split(" (")[0]
        tl = tk.Label(c, text=title, font=f_bold, bg=p["panel"], fg=p["fg"], anchor="w", justify="left")
        tl.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(8, 0))
        dl = tk.Label(c, text=short_doc(spec), font=f_small, bg=p["panel"], fg=p["muted"], anchor="nw", justify="left")
        dl.grid(row=1, column=1, sticky="new", padx=(0, 8), pady=(0, 8))
        for x in (ic, tl, dl):
            themed.append((x, {"bg": "panel"}))
        tl_fg = (tl, {"fg": "fg"})
        themed.append(tl_fg)
        themed.append((dl, {"fg": "muted"}))
        themed.append((ic, {"fg": "gold"}))

        def inside():
            try:
                x, y = outer.winfo_pointerxy()
                return outer.winfo_rootx() <= x < outer.winfo_rootx() + outer.winfo_width() and outer.winfo_rooty() <= y < outer.winfo_rooty() + outer.winfo_height()
            except tk.TclError:
                return False

        def hover(on):
            try:
                outer.itemconfigure("bg", outline=GOLD if on else pal()["border"])
            except tk.TclError:
                pass
        for x in (outer, c, ic, tl, dl):
            x.bind("<Enter>", lambda e: hover(True))
            x.bind("<Leave>", lambda e: hover(inside()))
            x.bind("<Button-1>", lambda e, s=spec: open_op(s))
        c.bind("<Return>", lambda e, s=spec: open_op(s))
        c.bind("<space>", lambda e, s=spec: open_op(s))

        def wrap(e):
            w_ = max(120, e.width - 70)
            tl.configure(wraplength=w_)
            dl.configure(wraplength=w_)
        c.bind("<Configure>", wrap, add="+")
        return outer

    # Lay operation cards out in a responsive 3-column grid.
    def fill_buttons(fr, ops, start_row=1):
        for i, spec in enumerate(ops):
            make_card(fr, spec).grid(row=start_row + i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
        for col in range(3):
            fr.columnconfigure(col, weight=1, uniform="c")
        for rw in range((len(ops) + 2) // 3):
            fr.rowconfigure(start_row + rw, uniform="rows", minsize=88)

    # Get (or create) a scrolling page frame and clear it.
    def page(key):
        if key not in cat_frames:
            outer_, inner_ = make_scroll(pages)
            outer_.grid(row=0, column=0, sticky="nsew")
            cat_frames[key], cat_inner[key] = outer_, inner_
        fr = cat_inner[key]
        for w in fr.winfo_children():
            w.destroy()
        return fr

    # Build one category page (heading, one-line description and its tool cards).
    def build_cat(cat):
        fr = page(cat)
        if cat == "Favourites":
            ops = [ops_by_id[i] for i in settings.get("favs", []) if i in ops_by_id]
        else:
            ops = [o for o in OPS if o["cat"] == cat]
        ttk.Label(fr, text=f"{CAT_ICONS.get(cat, '')}  {cat}", style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        if not ops:
            ttk.Label(fr, text="No favourites yet.\nOpen any operation and tick ★ Add to favourites.", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=20)
        else:
            fill_buttons(fr, ops, 1)
        register_drop_tree(fr)
        style_side()

    # Show the matching tools from every group while the search box has text.
    def build_search():
        text = search_var.get().strip().lower()
        if not text:
            show_cat()
            return
        hits = [o for o in OPS if text in " ".join([o["label"], tr(o["label"]), o["cat"], TOOL_DOCS.get(o["id"], "")]).lower()]
        fr = page("__search__")
        ttk.Label(fr, text=f"\U0001F50E  {len(hits)} found for “{search_var.get().strip()}”", style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        if hits:
            fill_buttons(fr, hits, 1)
        else:
            ttk.Label(fr, text="Nothing matches. Try a shorter word, for example trim, speed or audio.", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=20)
        register_drop_tree(fr)
        cat_frames["__search__"].tkraise()
        style_side()

    search_job = {"id": None}

    def on_search(*_):
        if search_job["id"]:
            root.after_cancel(search_job["id"])
        search_job["id"] = root.after(180, build_search)
    search_var.trace_add("write", on_search)
    search_ent.bind("<Escape>", lambda e: search_var.set(""))

    # Switch the visible category page (and clear any search).
    def show_cat():
        settings["cat"] = cat_var.get()
        save_settings(settings)
        if search_var.get():
            search_var.set("")
            return
        fr = cat_frames.get(cat_var.get())
        if fr is not None:
            fr.tkraise()
        style_side()

    # Sidebar: one row per group with an icon, its tool count and a gold marker on the selected group.
    side_rows = {}

    def style_side():
        p = pal()
        searching = bool(search_var.get().strip())
        for cat, (rw, mark, ic, nm, ct) in side_rows.items():
            on = cat == cat_var.get() and not searching
            bg = p["side_sel"] if on else p["bg"]
            for x in (rw, ic, nm, ct):
                x.configure(bg=bg)
            mark.configure(bg=GOLD if on else bg)
            ic.configure(fg=GOLD if on else p["muted"])
            nm.configure(fg=p["fg"], font=f_bold if on else f_base)
            ct.configure(fg=p["muted"])
            n = len(settings.get("favs", [])) if cat == "Favourites" else sum(1 for o in OPS if o["cat"] == cat)
            ct.configure(text=str(n))

    def select_cat(cat):
        cat_var.set(cat)
        show_cat()

    def build_side():
        for w in side.winfo_children():
            w.destroy()
        side_rows.clear()
        for cat in ["Favourites"] + CATEGORIES:
            rw = tk.Frame(side, cursor="hand2", takefocus=1)
            mark = tk.Frame(rw, width=4)
            mark.pack(side="left", fill="y")
            ic = tk.Label(rw, text=CAT_ICONS.get(cat, ""), font=f_bold, width=3)
            ic.pack(side="left", pady=9)
            nm = tk.Label(rw, text=cat, anchor="w", width=17)
            nm.pack(side="left", fill="x", expand=True)
            ct = tk.Label(rw, font=f_small, width=3, anchor="e")
            ct.pack(side="right", padx=(0, 8))
            for x in (rw, ic, nm, ct):
                x.bind("<Button-1>", lambda e, c=cat: select_cat(c))
            rw.bind("<Return>", lambda e, c=cat: select_cat(c))
            rw.bind("<space>", lambda e, c=cat: select_cat(c))
            rw.pack(fill="x", pady=2)
            side_rows[cat] = (rw, mark, ic, nm, ct)
        style_side()
    rethemers.append(lambda: style_side())

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

    build_side()

    # Repaint every themed widget once the window is on screen (guards against colours being reset while it opens).
    def refresh_all():
        retheme()
        for c_ in ["Favourites"] + CATEGORIES:
            build_cat(c_)
        show_cat()
        try:
            write_log(f"colours: header={header.cget('bg')} card={card.cget('bg')} (expected {pal()['brand']} / {pal()['panel']})")
        except Exception:  # noqa: BLE001
            pass
    refresh_gpu_ui()
    status_dot()
    info_changed()
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
                    if fail:
                        set_details(True)
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

    # ------------------------------------------------------------------ documentation screenshots
    #  `python om_video_studio.py --docshots [folder]` opens every tool's window with a sample file and saves a screenshot of each
    #  (Windows only; pure standard library: screen capture via ctypes, PNG written with zlib). Used to build docs/ABOUT.md.
    def run_docshots():
        import ctypes
        import struct
        import zlib
        from ctypes import wintypes
        i_ = sys.argv.index("--docshots")
        outdir = Path(sys.argv[i_ + 1]) if len(sys.argv) > i_ + 1 and not sys.argv[i_ + 1].startswith("--") else Path(__file__).resolve().parent / "docshots"
        outdir.mkdir(parents=True, exist_ok=True)
        medir = outdir / "media"
        medir.mkdir(exist_ok=True)
        logf = outdir / "docshots_log.txt"
        logf.write_text("", encoding="utf-8")

        def dlog(msg):
            with open(logf, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")

        here = Path(__file__).resolve().parent
        clip = next((c for c in (here / "docshots_media" / "OM_Test_Clip_A_15s.mp4", here / "docs" / "test-media" / "OM_Test_Clip_A_15s.mp4") if c.exists()), None)
        photo = next((c for c in (here / "docshots_media" / "OM_Test_Photo.jpg", here / "docs" / "test-media" / "OM_Test_Photo.jpg") if c.exists()), None)
        if clip is None:
            dlog("ERROR: OM_Test_Clip_A_15s.mp4 not found (put it in a docshots_media folder next to this file).")
            return
        mp3, srt = medir / "sample.mp3", medir / "sample.srt"
        if not mp3.exists():
            subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(clip), "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(mp3)], creationflags=NO_WINDOW)
        srt.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello from O.M. Video Studio\n\n2\n00:00:05,000 --> 00:00:08,000\nSample subtitle\n", encoding="utf-8")
        for nm in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askyesnocancel"):
            setattr(messagebox, nm, lambda *a, **k: True)
        # Clean slate so the pictures show the real defaults, not this PC's last-used values, favourites or folders.
        settings["last"], settings["favs"], settings["recent"] = {}, [], []
        globals()["save_settings"] = lambda *a, **k: None
        out_var.set("C:\\Users\\You\\Videos\\Edited")

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetDC.restype, user32.GetDC.argtypes = wintypes.HDC, [wintypes.HWND]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.GetAncestor.restype, user32.GetAncestor.argtypes = wintypes.HWND, [wintypes.HWND, wintypes.UINT]
        gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = wintypes.HDC, [wintypes.HDC]
        gdi32.CreateCompatibleBitmap.restype, gdi32.CreateCompatibleBitmap.argtypes = wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        def write_png(path, w, h, rgb):
            raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

            def chunk(t, d):
                return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

        # Screenshot of one window exactly as it looks on screen (title bar included).
        def grab(win, name):
            win.update_idletasks()
            win.update()
            hwnd = user32.GetAncestor(win.winfo_id(), 2)
            rect = wintypes.RECT()
            try:
                ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
            except Exception:  # noqa: BLE001
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w <= 0 or h <= 0:
                raise RuntimeError("empty window rectangle")
            hdc = user32.GetDC(None)
            mdc = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            gdi32.SelectObject(mdc, bmp)
            gdi32.BitBlt(mdc, 0, 0, w, h, hdc, rect.left, rect.top, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
            bih = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mdc, bmp, 0, h, buf, bih, 0)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(None, hdc)
            data = buf.raw
            rgb = bytearray(w * h * 3)
            rgb[0::3], rgb[1::3], rgb[2::3] = data[2::4], data[1::4], data[0::4]
            write_png(str(outdir / (name + ".png")), w, h, bytes(rgb))
            dlog(f"saved {name}.png {w}x{h}")

        def sample_for(spec):
            k = spec["kind"]
            if k == "audio":
                return str(mp3)
            if k == "sub":
                return str(srt)
            if k == "image":
                return str(photo or clip)
            return str(clip)

        def steps():
            root.geometry("1280x900+20+20")
            root.update()
            yield 1500
            set_path(str(clip))
            yield 600
            grab(root, "main")
            for n, cat in enumerate(CATEGORIES, 1):
                cat_var.set(cat)
                show_cat()
                root.update()
                yield 600
                try:
                    grab(root, f"cat_{n:02d}")
                except Exception as e:  # noqa: BLE001
                    dlog(f"cat {cat}: {e}")
            for spec in OPS:
                before = {x for x in root.winfo_children() if isinstance(x, tk.Toplevel)}
                try:
                    set_path(sample_for(spec))
                    yield 300
                    open_dialog(spec)
                    heavy = spec["id"] in STRIP_OPS
                    yield 7000 if heavy else (3500 if any(k[0] == spec["id"] for k in SLIDERS) or spec["id"] in SAMPLE_OPS else 1500)
                    new = [x for x in root.winfo_children() if isinstance(x, tk.Toplevel) and x not in before]
                    if not new:
                        dlog(f"{spec['id']}: no window opened")
                        continue
                    top = new[-1]
                    top.geometry("+10+10")
                    top.lift()
                    top.update()
                    yield 400
                    grab(top, spec["id"])
                except Exception as e:  # noqa: BLE001
                    dlog(f"{spec['id']}: ERROR {e!r}")
                finally:
                    for x in [x for x in root.winfo_children() if isinstance(x, tk.Toplevel) and x not in before]:
                        try:
                            x.destroy()
                        except Exception:  # noqa: BLE001
                            pass
                yield 300
            try:
                show_about()
                yield 1000
                abt = [x for x in root.winfo_children() if isinstance(x, tk.Toplevel)][-1]
                abt.geometry("+10+10")
                abt.update()
                yield 300
                grab(abt, "about")
                abt.destroy()
            except Exception as e:  # noqa: BLE001
                dlog(f"about: ERROR {e!r}")
            dlog("DONE")
            (outdir / "DONE.txt").write_text("done", encoding="utf-8")

        gen = steps()

        def tick():
            try:
                delay = next(gen)
            except StopIteration:
                root.destroy()
                return
            except Exception as e:  # noqa: BLE001
                dlog(f"STOPPED: {e!r}")
                return
            root.after(delay if isinstance(delay, int) else 500, tick)
        tick()

    # Close the splash once it has been up for at least ~2 s, then show the main window.
    def end_splash():
        try:
            if splash["win"] is not None:
                splash["win"].destroy()
            root.deiconify()
            root.lift()
            root.focus_force()
            title_bar()
        except Exception:  # noqa: BLE001
            pass
    if splash["win"] is not None:
        root.after(max(50, int(2000 - (time.time() - splash["t0"]) * 1000)), end_splash)
    root.after(2600, refresh_all)

    pump()
    if "--docshots" in sys.argv and os.name == "nt":
        root.after(3500, run_docshots)  # documentation screenshot run: no update check, no sounds
    else:
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
    if "--docshots" in sys.argv and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()  # real pixels, so the screenshots are sharp and the capture rectangles line up
        except Exception:  # noqa: BLE001
            pass
    if "--list" in sys.argv:
        for o in OPS:
            print(f'{o["cat"]:18} {o["id"]:14} {o["label"]}')
        sys.exit(0)
    gui()
