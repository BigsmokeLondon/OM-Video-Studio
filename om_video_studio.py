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
from pathlib import Path

# --- 1. CONSTANTS & PATHS --------------------------------------------------
#  APP / VERSION: shown in the title bar. CI reads the `VERSION = "x.y.z"` line
#  with a regex (see .github/workflows/release.yml) and publishes release
#  v<VERSION>, so bumping this line is how a new release is cut.
#  *_EXT sets: decide which files the "kind" checker accepts for each operation
#  and which extensions the file pickers show.
APP = "O.M. Video Studio"
VERSION = "1.7.0"
GITHUB_REPO = "BigsmokeLondon/OM-Video-Studio"  # releases are checked here for updates
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
SUB_EXT = {".srt", ".ass", ".vtt"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
# STATE is process-wide mutable switches. "gpu" is toggled by the GPU checkbox
#  and read by vcodec(); execute() flips it back to False if NVENC fails.
STATE = {"gpu": False}
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


# H.264 encoder arguments. CPU path = libx264 veryfast + CRF; GPU path = NVIDIA NVENC with constant-quality VBR. yuv420p keeps output playable everywhere.
def vcodec(crf=20):
    if STATE["gpu"]:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
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
                if attempt == 1 and STATE["gpu"] and not cancel.is_set() and any("nvenc" in x.lower() for x in cap):
                    STATE["gpu"] = False
                    write_log(f"NVENC failed for '{spec['id']}' on {Path(f).name}; switched to CPU. ffmpeg said:\n" + "\n".join(cap[-15:]))
                    log("The GPU encoder (NVENC) failed on this computer, so I switched to CPU encoding and am retrying. "
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


# Checks that this ffmpeg lists h264_nvenc AND can actually encode a tiny test clip with it, so the GPU checkbox is only enabled when it will really work.
def detect_gpu():
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW)
        if "h264_nvenc" not in r.stdout:
            return False
        t = subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
                            "-c:v", "h264_nvenc", "-f", "null", os.devnull], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=30)
        return t.returncode == 0  # only offer the GPU option if a real test encode works
    except Exception:
        return False


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
    root.title(f"{APP}  -  M.A. Industries Inc")
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
    gpu_available = have_ff and detect_gpu()
    path_var = tk.StringVar()
    out_var = tk.StringVar(value=settings.get("outdir", ""))
    gpu_var = tk.BooleanVar(value=False)
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
    ttk.Checkbutton(row, text="Show ffmpeg commands", variable=cmd_var).pack(side="left", padx=10)
    if gpu_available:
        ttk.Checkbutton(row, text="Use GPU (NVENC)", variable=gpu_var, command=lambda: STATE.update(gpu=gpu_var.get())).pack(side="left")
    snd_var = tk.BooleanVar(value=settings.get("sounds", True))

    # Sound checkbox: persist setting and play a sample ping when switched on.
    def toggle_sound():
        settings["sounds"] = snd_var.get()
        save_settings(settings)
        if snd_var.get():
            play_ping("job")
    ttk.Checkbutton(row, text="Sound", variable=snd_var, command=toggle_sound).pack(side="left", padx=10)
    ttk.Button(row, text="A+", width=4, command=lambda: set_fsize(1)).pack(side="right")
    ttk.Button(row, text="A-", width=4, command=lambda: set_fsize(-1)).pack(side="right", padx=(12, 4))
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
        ttk.Button(bf, text="Copy ffmpeg command", command=copy_cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="Add to queue", command=lambda: go("queue")).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="Cancel", command=top_.destroy).pack(side="left", padx=6)
        ttk.Button(bf, text="Run", style=run_style, command=go).pack(side="left")
        top_.bind("<Return>", lambda e: go())
        top_.bind("<Escape>", lambda e: top_.destroy())
        if spec["id"] in ("trim", "crop") or spec["kind"] in ("video", "image"):
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
            return src_
        src = cur_src({"files": getters["files"]()} if "files" in getters else None)
        box = ttk.LabelFrame(frm, text="Live preview", padding=8)
        box.grid(row=0, column=3, rowspan=max(nrows, 4), sticky="n", padx=(18, 0))
        names = ["Start frame", "End frame"] if spec["id"] == "trim" else (["Result of the crop"] if spec["id"] == "crop" else ["Before", "After (this is what the result will look like)"])
        labels = []
        for nm in names:
            ttk.Label(box, text=nm, style="Muted.TLabel").pack(anchor="w")
            hold = ttk.Frame(box, width=430, height=245)  # fixed slot so the window does not jump when a picture appears
            hold.pack(pady=(0, 6))
            hold.pack_propagate(False)
            lab = ttk.Label(hold, text="loading…", anchor="center")
            lab.pack(expand=True)
            labels.append(lab)
        note = ttk.Label(box, text="", style="Muted.TLabel", wraplength=430)
        note.pack(anchor="w")
        tmpd = tempfile.mkdtemp(prefix="ffprev_")
        st = {"n": 0, "after": None, "imgs": []}
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
                        for i, t in enumerate((s_, max(s_, e_ - 0.1))):
                            f = os.path.join(tmpd, f"t{n}_{i}.png")
                            paths.append(f if grab_frame(src, t, f) else None)
                elif spec["id"] != "crop":
                    t0 = dur / 3 if dur else 0
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
                            msg = f"Size: {w0}x{h0}  ->  {w1}x{h1}" if (w0 and w1) else ""
                        else:
                            msg = "No preview for these settings (or this kind of operation) - Run will still work."
                else:
                    o = {k: (str(raw[k]).strip() or next(x for x in spec["opts"] if x["key"] == k)["default"]) for k in ("w", "h", "x", "y")}
                    f = os.path.join(tmpd, f"c{n}.png")
                    ok = grab_frame(src, dur / 3 if dur else 0, f, crop_filter(o))
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
                kk = max([1] + [max(-(-i_.width() // 420), -(-i_.height() // 240)) for i_ in loaded if i_ is not None])
                ks = [kk] * len(loaded)
            else:
                ks = [max(1, -(-i_.width() // 420), -(-i_.height() // 240)) if i_ is not None else 1 for i_ in loaded]
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
            note.configure(text="Updating…")
            threading.Thread(target=work, args=(st["n"], raw), daemon=True).start()

        def later(*_):
            if st["after"]:
                try:
                    top_.after_cancel(st["after"])
                except tk.TclError:
                    pass
            st["after"] = top_.after(600, kick)

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
    sh = lambda a: subprocess.run([FFMPEG, "-v", "error", "-y"] + a, check=True)  # noqa: E731
    srt = work / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,500\nHello test\n\n2\n00:00:03,000 --> 00:00:05,000\nSecond line\n", encoding="utf-8")
    cube = work / "id.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n", encoding="utf-8")
    sh(["-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-i", str(srt), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text", "-shortest", str(clip)])
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
