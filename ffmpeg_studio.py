#!/usr/bin/env python3
"""
O.M. FFmpeg Studio - point-and-click front end for ffmpeg.

Pick a file (or a folder for batch), press a button, set a few options, run.
Standard library only (tkinter). Needs ffmpeg + ffprobe on PATH.

    python ffmpeg_studio.py             # open the GUI
    python ffmpeg_studio.py --selftest  # build + run every operation on a generated clip
    python ffmpeg_studio.py --list      # list operations
"""
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

APP = "O.M. FFmpeg Studio"
VERSION = "1.0.2"
GITHUB_REPO = "BigsmokeLondon/OM-FFmpeg-Studio"  # releases are checked here for updates
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
SUB_EXT = {".srt", ".ass", ".vtt"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
STATE = {"gpu": False}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROZEN = bool(getattr(sys, "frozen", False))  # True when running as the installed .exe
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # bundled read-only files (Resource/, app.ico)
# Installed copies live in Program Files (read-only), so logs and the downloaded ffmpeg go in the user's data folder.
DATA_DIR = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "O.M. FFmpeg Studio") if FROZEN else APP_DIR
FFMPEG_HOME = DATA_DIR / "ffmpeg"


def find_tool(name):
    """ffmpeg/ffprobe/ffplay: the user's own install on PATH first, then the copy this app downloaded."""
    exe = name + (".exe" if os.name == "nt" else "")
    return shutil.which(name) or next((str(d / exe) for d in (FFMPEG_HOME / "bin", FFMPEG_HOME) if (d / exe).exists()), None)


FFMPEG = find_tool("ffmpeg") or "ffmpeg"
FFPROBE = find_tool("ffprobe") or "ffprobe"


def refresh_tools():
    global FFMPEG, FFPROBE
    FFMPEG = find_tool("ffmpeg") or "ffmpeg"
    FFPROBE = find_tool("ffprobe") or "ffprobe"
    return bool(find_tool("ffmpeg") and find_tool("ffprobe"))


FFMPEG_URLS = [  # both are the full GPL builds (include libvidstab etc.)
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]


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


def download_file(url, dest, report):
    """Download url to dest with progress. Falls back to Windows' own curl.exe (which can fetch missing root certificates)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OM-FFmpeg-Studio"})
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


def install_ffmpeg(report):
    """Download ffmpeg into DATA_DIR/ffmpeg (no admin needed). report(fraction, text). Returns (ok, message)."""
    import zipfile
    if os.name != "nt":
        return False, "Automatic install is Windows-only. Install ffmpeg with your package manager (e.g. apt/brew install ffmpeg)."
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
SETTINGS_PATH = Path.home() / ".ffmpeg_studio.json"


def _log_path():
    """Problem log lives next to the program; falls back to the home folder if that is read-only."""
    for d in (DATA_DIR, Path.home()):
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / "ffmpeg_studio_log.txt"
            with open(f, "a", encoding="utf-8"):
                pass
            return f
        except OSError:
            continue
    return None


LOG_FILE = _log_path()


def write_log(text):
    """Append a timestamped entry to ffmpeg_studio_log.txt (trimmed to ~1 MB). Never raises."""
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


def _excepthook(et, ev, tb):
    import traceback
    write_log("UNCAUGHT ERROR\n" + "".join(traceback.format_exception(et, ev, tb)))
    sys.__excepthook__(et, ev, tb)


sys.excepthook = _excepthook


# --------------------------------------------------------------------------- helpers
def tsec(s):
    """'1:02:03.5' / '90' / '' -> seconds."""
    s = str(s).strip()
    if not s:
        return 0.0
    t = 0.0
    for p in s.split(":"):
        t = t * 60 + float(p)
    return t


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


def vcodec(crf=20):
    if STATE["gpu"]:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]


AAC = ["-c:a", "aac", "-b:a", "192k"]
_AMAP = {".mp3": ["-c:a", "libmp3lame", "-q:a", "2"], ".wav": ["-c:a", "pcm_s16le"], ".flac": ["-c:a", "flac"],
         ".m4a": AAC, ".aac": AAC, ".ogg": ["-c:a", "libvorbis", "-q:a", "5"], ".opus": ["-c:a", "libopus", "-b:a", "128k"],
         ".webm": ["-c:a", "libopus", "-b:a", "128k"]}


def aargs(ext):
    return _AMAP.get(ext, AAC)


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


def job(args, out=None, dur=0.0, **kw):
    d = dict(args=args, out=out, dur=dur)
    d.update(kw)
    return d


# --------------------------------------------------------------------------- registry
OPS = []


def O(key, label, type="text", default="", choices=None, ftype="any"):
    return dict(key=key, label=label, type=type, default=default, choices=choices or [], ftype=ftype)


def register(cat, id, label, fn, opts=(), kind="video", tip="", folder_one=False):
    OPS.append(dict(cat=cat, id=id, label=label, fn=fn, opts=list(opts), kind=kind, tip=tip, folder_one=folder_one))


def op(cat, id, label, opts=(), kind="video", tip="", folder_one=False):
    def deco(fn):
        register(cat, id, label, fn, opts, kind, tip, folder_one)
        return fn
    return deco


# =========================================================================== VIDEO
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


@op("Video", "join", "Join clips into one",
    [O("files", "Clips to append after the selected file", "files")],
    tip="Selected file goes first. Clips are scaled to the first one's size. With a batch folder, joins every video in it.", folder_one=True)
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


@op("Video", "speed", "Change speed (slow-mo / time-lapse)",
    [O("factor", "Speed factor (0.5 = half speed, 4 = 4x faster)", "float", 2.0)])
def b_speed(o, c):
    f = float(o["factor"])
    a = ["-i", c.src, "-vf", f"setpts=PTS/{f}"]
    if c.has_a:
        a += ["-af", atempo(f)]
    return [job(a + vcodec() + AAC, c.out(f"_x{f:g}", ".mp4"), c.dur / f)]


@op("Video", "reverse", "Reverse playback", tip="Loads the whole clip in memory - use on short clips.")
def b_reverse(o, c):
    a = ["-i", c.src, "-vf", "reverse"]
    if c.has_a:
        a += ["-af", "areverse"]
    return [job(a + vcodec() + AAC, c.out("_reversed", ".mp4"), c.dur)]


@op("Video", "resize", "Resize",
    [O("width", "Width (-2 = auto)", default="1280"), O("height", "Height (-2 = auto)", default="-2")])
def b_resize(o, c):
    return [job(["-i", c.src, "-vf", f"scale={o['width']}:{o['height']}"] + vcodec() + AAC, c.out(f"_{o['width']}w", ".mp4"), c.dur)]


@op("Video", "crop", "Crop",
    [O("w", "Width", default="min(iw,ih)"), O("h", "Height", default="min(iw,ih)"),
     O("x", "X offset", default="(iw-ow)/2"), O("y", "Y offset", default="(ih-oh)/2")],
    tip="Defaults crop a centred square. Expressions like iw/2 work.")
def b_crop(o, c):
    e = lambda v: v.replace(",", "\\,")  # noqa: E731  commas separate filters, so escape them in expressions
    return [job(["-i", c.src, "-vf", f"crop={e(o['w'])}:{e(o['h'])}:{e(o['x'])}:{e(o['y'])}"] + vcodec() + AAC, c.out("_crop", ".mp4"), c.dur)]


@op("Video", "rotate", "Rotate",
    [O("how", "Rotation", "choice", "90 clockwise", ["90 clockwise", "90 counter-clockwise", "180"])])
def b_rotate(o, c):
    vf = {"90 clockwise": "transpose=1", "90 counter-clockwise": "transpose=2", "180": "hflip,vflip"}[o["how"]]
    return [job(["-i", c.src, "-vf", vf] + vcodec() + AAC, c.out("_rot", ".mp4"), c.dur)]


@op("Video", "flip", "Flip / mirror",
    [O("how", "Direction", "choice", "Horizontal (mirror)", ["Horizontal (mirror)", "Vertical", "Both"])])
def b_flip(o, c):
    vf = {"Horizontal (mirror)": "hflip", "Vertical": "vflip", "Both": "hflip,vflip"}[o["how"]]
    return [job(["-i", c.src, "-vf", vf] + vcodec() + AAC, c.out("_flip", ".mp4"), c.dur)]


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


@op("Video", "loop", "Loop the clip", [O("times", "Play it this many times in total", "int", 3)])
def b_loop(o, c):
    t = max(2, int(o["times"]))
    return [job(["-stream_loop", str(t - 1), "-i", c.src] + vcodec() + AAC, c.out(f"_loop{t}", ".mp4"), c.dur * t)]


@op("Video", "stabilise", "Stabilise shaky footage", [O("shake", "Shakiness 1-10", "int", 5), O("smooth", "Smoothing frames", "int", 15)],
    tip="Two passes with the vidstab filter (needs an ffmpeg build with libvidstab, e.g. the Gyan full build).")
def b_stab(o, c):
    d1 = job(["-i", c.src, "-vf", f"vidstabdetect=shakiness={o['shake']}:result=transforms.trf", "-an", "-f", "null", os.devnull],
             None, c.dur, cwd=c.tmp)
    d2 = job(["-i", c.src, "-vf", f"vidstabtransform=input=transforms.trf:smoothing={o['smooth']},unsharp=5:5:0.8:3:3:0.4"] + vcodec() + AAC,
             c.out("_stable", ".mp4"), c.dur, cwd=c.tmp)
    return [d1, d2]


# =========================================================================== FORMAT & QUALITY
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


@op("Format & Quality", "remux", "Re-wrap container (no quality loss)",
    [O("target", "New container", "choice", "mp4", ["mp4", "mkv", "mov"])],
    tip="Copies the streams untouched - takes seconds. Works when codecs fit the new container (H.264 + AAC does).")
def b_remux(o, c):
    t = o["target"]
    a = ["-i", c.src, "-map", "0:v?", "-map", "0:a?", "-c", "copy"]
    if t in ("mp4", "mov"):
        a += ["-movflags", "+faststart"]
    return [job(a, c.out("_remux", "." + t), c.dur)]


@op("Format & Quality", "faststart", "Prepare for web playback (fast-start MP4)")
def b_faststart(o, c):
    return [job(["-i", c.src, "-c", "copy", "-movflags", "+faststart"], c.out("_web", ".mp4"), c.dur)]


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


# =========================================================================== AUDIO
def _afilt(c, suffix, af):
    if c.has_v:
        ext = c.ext if c.ext in (".mp4", ".mkv", ".mov", ".m4v") else ".mkv"
        args = ["-i", c.src, "-af", af, "-c:v", "copy"] + aargs(ext)
    else:
        ext = c.aout_ext()
        args = ["-i", c.src, "-vn", "-af", af] + aargs(ext)
    return [job(args, c.out(suffix, ext), c.dur)]


def b_extract(o, c):
    t = o["format"]
    return [job(["-i", c.src, "-vn"] + aargs("." + t), c.out("_audio", "." + t), c.dur)]


_FORMATS = ["mp3", "wav", "m4a", "flac", "ogg", "opus"]
register("Audio", "extract", "Extract audio from video", b_extract, [O("format", "Format", "choice", "mp3", _FORMATS)], kind="video")
register("Audio", "aconvert", "Convert audio format", b_extract, [O("format", "Format", "choice", "mp3", _FORMATS)], kind="audio")


@op("Audio", "loudnorm", "Normalise loudness",
    [O("target", "Target", "choice", "-14 LUFS  YouTube / Spotify", ["-14 LUFS  YouTube / Spotify", "-16 LUFS  Podcast / Apple", "-23 LUFS  Broadcast (EBU R128)"])],
    kind="any")
def b_loudnorm(o, c):
    i = re.match(r"(-\d+)", o["target"]).group(1)
    return _afilt(c, "_norm", f"loudnorm=I={i}:TP=-1.5:LRA=11")


@op("Audio", "silence", "Remove silence",
    [O("db", "Silence threshold (dB)", "int", -45), O("secs", "Minimum silence to cut (seconds)", "float", 0.5)],
    kind="any", tip="Output is audio only.")
def b_silence(o, c):
    af = (f"silenceremove=start_periods=1:start_threshold={o['db']}dB:"
          f"stop_periods=-1:stop_duration={o['secs']}:stop_threshold={o['db']}dB")
    ext = ".mp3" if c.has_v else c.aout_ext()
    return [job(["-i", c.src, "-vn", "-af", af] + aargs(ext), c.out("_nosilence", ext), c.dur)]


@op("Audio", "atrim", "Trim audio", [O("start", "Start", default="0"), O("end", "End (blank = to the end)", default="")], kind="audio")
def b_atrim(o, c):
    s = tsec(o["start"])
    e = tsec(o["end"]) if o["end"] else c.dur
    ext = c.aout_ext()
    return [job(["-ss", f"{s}", "-i", c.src, "-t", f"{max(0.1, e - s)}", "-vn"] + aargs(ext), c.out("_trim", ext), e - s)]


@op("Audio", "fade", "Fade in / out",
    [O("fin", "Fade in (seconds)", "float", 2), O("fout", "Fade out (seconds)", "float", 2), O("video", "Fade the picture too", "bool", False)], kind="any")
def b_fade(o, c):
    fin, fout = float(o["fin"]), float(o["fout"])
    af = f"afade=t=in:st=0:d={fin},afade=t=out:st={max(0, c.dur - fout)}:d={fout}"
    if c.has_v and o["video"]:
        vf = f"fade=t=in:st=0:d={fin},fade=t=out:st={max(0, c.dur - fout)}:d={fout}"
        return [job(["-i", c.src, "-vf", vf, "-af", af] + vcodec() + AAC, c.out("_fade", ".mp4"), c.dur)]
    return _afilt(c, "_fade", af)


@op("Audio", "volume", "Change volume", [O("db", "Gain in dB (+6 = louder, -6 = quieter)", "float", 6)], kind="any")
def b_volume(o, c):
    return _afilt(c, "_vol", f"volume={o['db']}dB")


@op("Audio", "tempo", "Change audio speed (keeps pitch)", [O("f", "Speed factor", "float", 1.25)], kind="audio")
def b_tempo(o, c):
    return _afilt(c, "_tempo", atempo(float(o["f"])))


@op("Audio", "pitch", "Change pitch", [O("st", "Semitones (+/-12)", "float", 2)], kind="audio")
def b_pitch(o, c):
    sr = int((c.info["a"] or {}).get("sample_rate", 44100))
    r = 2 ** (float(o["st"]) / 12)
    return _afilt(c, "_pitch", f"asetrate={sr * r:.0f},aresample={sr},{atempo(1 / r)}")


@op("Audio", "denoise", "Denoise", [O("nf", "Noise floor (dB, -20 gentle ... -40 strong)", "int", -25)], kind="any")
def b_denoise(o, c):
    return _afilt(c, "_clean", f"afftdn=nf={o['nf']}")


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


_MIXCH = ["Mix under the original audio", "Replace the original audio"]
register("Audio", "mix", "Add music / replace audio", b_mix,
         [O("file", "Audio file", "file", ftype="audio"), O("mode", "Mode", "choice", _MIXCH[0], _MIXCH),
          O("ov", "Original volume (0-2)", "float", 1.0), O("nv", "New audio volume (0-2)", "float", 0.25), O("loop", "Loop the new audio", "bool", True)],
         tip="Background music under the video's own sound, or swap the sound entirely.")
register("Audio", "narration", "Add narration / voiceover", b_mix,
         [O("file", "Narration audio (e.g. from your TTS foundry)", "file", ftype="audio"), O("mode", "Mode", "choice", _MIXCH[0], _MIXCH),
          O("ov", "Original volume (0-2)", "float", 0.3), O("nv", "Narration volume (0-2)", "float", 1.0), O("loop", "Loop the narration", "bool", False)],
         tip="Lays a voiceover over the video, ducking the original sound.")


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


# =========================================================================== IMAGES & FRAMES
@op("Images & Frames", "thumb", "Grab a thumbnail / single frame", [O("t", "Time (hh:mm:ss or seconds)", default="1")])
def b_thumb(o, c):
    return [job(["-ss", f"{tsec(o['t'])}", "-i", c.src, "-frames:v", "1", "-q:v", "2"], c.out("_thumb", ".jpg"), 0)]


@op("Images & Frames", "frames", "Extract frames to a folder", [O("every", "One frame every N seconds", "float", 1)])
def b_frames(o, c):
    d = c.out("_frames", folder=True)
    return [job(["-i", c.src, "-vf", f"fps=1/{float(o['every'])}", os.path.join(d, "frame_%04d.png")], d, c.dur, mkdir=d, raw_out=True)]


@op("Images & Frames", "contact", "Contact sheet (grid of frames)",
    [O("cols", "Columns", "int", 5), O("rows", "Rows", "int", 4), O("tw", "Thumbnail width", "int", 320)])
def b_contact(o, c):
    cols, rows = int(o["cols"]), int(o["rows"])
    iv = max(0.1, c.dur / (cols * rows))
    return [job(["-i", c.src, "-vf", f"fps=1/{iv:.3f},scale={o['tw']}:-1,tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1", "-q:v", "2"],
                c.out("_contact", ".jpg"), c.dur)]


@op("Images & Frames", "gif", "Make a GIF (optimised palette)",
    [O("start", "Start", default="0"), O("dur", "Length (seconds)", "float", 5), O("fps", "Frames per second", "int", 12), O("w", "Width", "int", 480)])
def b_gif(o, c):
    vf = f"fps={o['fps']},scale={o['w']}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    return [job(["-ss", f"{tsec(o['start'])}", "-t", f"{o['dur']}", "-i", c.src, "-vf", vf, "-loop", "0"], c.out("_anim", ".gif"), float(o["dur"]))]


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


# =========================================================================== ANALYSIS
def _probe_report(c):
    def f(log):
        log(json.dumps(c.info["raw"], indent=2, ensure_ascii=False))
        return 0
    return f


@op("Analysis", "probe", "Full file report (ffprobe)", kind="any")
def b_probe(o, c):
    return [job([], None, 0, func=_probe_report(c))]


@op("Analysis", "silencedetect", "Detect silences", [O("db", "Threshold (dB)", "int", -35), O("d", "Min length (s)", "float", 0.5)], kind="any")
def b_sd(o, c):
    return [job(["-i", c.src, "-af", f"silencedetect=noise={o['db']}dB:d={o['d']}", "-vn", "-f", "null", os.devnull], None, c.dur, info=True, show=r"silence_")]


@op("Analysis", "blackdetect", "Detect black frames", [O("d", "Min length (s)", "float", 0.1)])
def b_bd(o, c):
    return [job(["-i", c.src, "-vf", f"blackdetect=d={o['d']}:pic_th=0.98", "-an", "-f", "null", os.devnull], None, c.dur, info=True, show=r"black_")]


@op("Analysis", "freezedetect", "Detect frozen frames", [O("d", "Min freeze (s)", "float", 2)])
def b_fd(o, c):
    return [job(["-i", c.src, "-vf", f"freezedetect=n=-60dB:d={o['d']}", "-an", "-f", "null", os.devnull], None, c.dur, info=True, show=r"freeze_")]


@op("Analysis", "scenes", "Detect scene changes", [O("th", "Sensitivity 0.1-0.9 (lower = more cuts)", "float", 0.4)])
def b_sc(o, c):
    return [job(["-i", c.src, "-vf", f"select='gt(scene,{o['th']})',showinfo", "-an", "-f", "null", os.devnull], None, c.dur, info=True, show=r"pts_time:[\d.]+")]


@op("Analysis", "loudness", "Measure loudness (LUFS)", kind="any")
def b_ld(o, c):
    return [job(["-i", c.src, "-af", "ebur128=peak=true", "-vn", "-f", "null", os.devnull], None, c.dur, info=True, tail=14)]


@op("Analysis", "wave", "Waveform image", kind="any")
def b_wave(o, c):
    return [job(["-i", c.src, "-filter_complex", "showwavespic=s=1600x400:colors=#3b82f6", "-frames:v", "1"], c.out("_wave", ".png"), 0)]


@op("Analysis", "spectro", "Spectrogram image", kind="any")
def b_spec(o, c):
    return [job(["-i", c.src, "-lavfi", "showspectrumpic=s=1600x800", "-frames:v", "1"], c.out("_spectrogram", ".png"), 0)]


@op("Analysis", "stripmeta", "Strip metadata (no re-encode)", kind="any")
def b_strip(o, c):
    return [job(["-i", c.src, "-map", "0", "-map_metadata", "-1", "-c", "copy"], c.out("_clean"), c.dur)]


# =========================================================================== STREAMING
@op("Streaming", "hls", "Create HLS stream (m3u8 + segments)", [O("seg", "Segment length (s)", "int", 6)])
def b_hls(o, c):
    d = c.out("_hls", folder=True)
    return [job(["-i", c.src] + vcodec() + AAC + ["-hls_time", str(o["seg"]), "-hls_playlist_type", "vod",
                 "-hls_segment_filename", os.path.join(d, "seg_%03d.ts"), os.path.join(d, "index.m3u8")], d, c.dur, mkdir=d, raw_out=True)]


# =========================================================================== VIDEO (extra)
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


# =========================================================================== COMBINE
_POS = ["Bottom-right", "Bottom-left", "Top-right", "Top-left", "Centre"]


def _xy(pos, m):
    return {"Bottom-right": (f"W-w-{m}", f"H-h-{m}"), "Bottom-left": (f"{m}", f"H-h-{m}"),
            "Top-right": (f"W-w-{m}", f"{m}"), "Top-left": (f"{m}", f"{m}"), "Centre": ("(W-w)/2", "(H-h)/2")}[pos]


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


@op("Combine", "grid", "Side-by-side / grid of videos",
    [O("files", "Other videos to add", "files"), O("layout", "Layout", "choice", "Side by side", ["Side by side", "Stacked", "Grid (2 columns)"]),
     O("cw", "Width of each cell", "int", 640)],
    tip="The selected file is first. Audio comes from the first video.", folder_one=True)
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


@op("Combine", "crossfade", "Join clips with crossfade transitions",
    [O("files", "Clips to append after the selected file", "files"), O("tr", "Transition length (seconds)", "float", 1.0),
     O("style", "Style", "choice", "fade", ["fade", "dissolve", "fadeblack", "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "circleclose", "pixelize", "radial"])],
    tip="Each clip must be longer than the transition.", folder_one=True)
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


@op("Combine", "slideshow", "Photos to video (slideshow / Ken Burns)",
    [O("files", "More photos to add", "files"), O("secs", "Seconds per photo", "float", 4),
     O("fx", "Effect", "choice", "Slow zoom in", ["Slow zoom in", "Slow zoom out", "None (still)"]),
     O("size", "Video size", "choice", "1920x1080 landscape", ["1920x1080 landscape", "1080x1920 vertical", "1080x1080 square"]),
     O("tr", "Crossfade between photos (seconds, 0 = cut)", "float", 0.5), O("music", "Music (optional)", "file", ftype="audio")],
    kind="image", tip="Pick a folder with 'Batch folder' to use every photo in it, in name order.", folder_one=True)
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


# =========================================================================== TEXT & SUBTITLES
def esc_path(p):
    p = str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return "'" + p + "'"


def esc_text(t):
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace("%", "\\%")


def fontfile():
    for p in (r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if os.path.exists(p):
            return esc_path(p)
    return None


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


@op("Text & Subtitles", "addsubs", "Add a soft subtitle track (can be turned off)",
    [O("sub", "Subtitle file (.srt)", "file", ftype="sub"), O("lang", "Language code (eng, urd, pan...)", default="eng")])
def b_addsubs(o, c):
    if not o["sub"]:
        raise ValueError("Choose a subtitle file.")
    mkv = c.ext == ".mkv"
    codec = "srt" if mkv else "mov_text"
    return [job(["-i", c.src, "-i", o["sub"], "-map", "0", "-map", "1", "-c", "copy", "-c:s", codec, "-metadata:s:s:0", f"language={o['lang']}"],
                c.out("_subs", ".mkv" if mkv else ".mp4"), c.dur)]


@op("Text & Subtitles", "extractsubs", "Extract subtitles from a video",
    [O("fmt", "Save as", "choice", "srt", ["srt", "ass", "vtt"]), O("track", "Subtitle track number (1 = first)", "int", 1)])
def b_extractsubs(o, c):
    return [job(["-i", c.src, "-map", f"0:s:{int(o['track']) - 1}"], c.out("_subs", "." + o["fmt"]), 0)]


@op("Text & Subtitles", "convsubs", "Convert subtitle file format",
    [O("fmt", "Convert to", "choice", "srt", ["srt", "ass", "vtt"])], kind="sub", tip="Select a subtitle file (.srt, .ass, .vtt) at the top first.")
def b_convsubs(o, c):
    return [job(["-i", c.src], c.out("_conv", "." + o["fmt"]), 0)]


def _textpos(pos, size):
    m = max(20, int(size) // 2)
    return {"Top": ("(w-text_w)/2", f"{m}"), "Middle": ("(w-text_w)/2", "(h-text_h)/2"), "Bottom": ("(w-text_w)/2", f"h-text_h-{m}"),
            "Top-left": (f"{m}", f"{m}"), "Top-right": (f"w-text_w-{m}", f"{m}"), "Bottom-left": (f"{m}", f"h-text_h-{m}"),
            "Bottom-right": (f"w-text_w-{m}", f"h-text_h-{m}"), "Lower third": ("w*0.05", "h*0.78")}[pos]


_TPOS = ["Bottom", "Middle", "Top", "Lower third", "Top-left", "Top-right", "Bottom-left", "Bottom-right"]


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


@op("Text & Subtitles", "drawtext", "Add a title / lower third",
    [O("text", "Text", default="My Title"), O("pos", "Position", "choice", "Bottom", _TPOS), O("size", "Font size", "int", 56),
     O("color", "Colour", default="white"), O("box", "Dark box behind the text", "bool", True),
     O("start", "Show from (seconds)", default="0"), O("end", "Hide at (blank = until the end)", default="")])
def b_drawtext(o, c):
    if not o["text"]:
        raise ValueError("Type some text.")
    return _drawtext(c, esc_text(o["text"]), o, "_title")


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


# =========================================================================== EFFECTS & COLOUR
def _vf(c, suffix, vf):
    a = ["-i", c.src, "-vf", vf] + vcodec()
    a += ["-c:a", "copy"] if c.has_a else []
    return [job(a, c.out(suffix, ".mp4"), c.dur)]


@op("Effects & Colour", "colour", "Colour correction (brightness, contrast…)",
    [O("b", "Brightness (-0.5 to 0.5)", "float", 0.05), O("c", "Contrast (0.5 to 2)", "float", 1.1),
     O("s", "Saturation (0 to 3)", "float", 1.2), O("g", "Gamma (0.5 to 2)", "float", 1.0)])
def b_colour(o, c):
    return _vf(c, "_graded", f"eq=brightness={o['b']}:contrast={o['c']}:saturation={o['s']}:gamma={o['g']}")


@op("Effects & Colour", "lut", "Apply a LUT (.cube colour preset)", [O("lut", "LUT file (.cube)", "file", ftype="lut")])
def b_lut(o, c):
    if not o["lut"]:
        raise ValueError("Choose a .cube LUT file.")
    return _vf(c, "_lut", f"lut3d=file={esc_path(o['lut'])}")


@op("Effects & Colour", "blur", "Blur", [O("amt", "Strength (1-30)", "int", 8)])
def b_blur(o, c):
    return _vf(c, "_blur", f"boxblur={int(o['amt'])}:2")


@op("Effects & Colour", "sharpen", "Sharpen", [O("amt", "Strength (0.5-3)", "float", 1.0)])
def b_sharpen(o, c):
    return _vf(c, "_sharp", f"unsharp=5:5:{o['amt']}:5:5:0")


@op("Effects & Colour", "vdenoise", "Denoise video", [O("amt", "Strength (1-10)", "float", 4)])
def b_vdenoise(o, c):
    return _vf(c, "_denoised", f"hqdn3d={o['amt']}:{o['amt']}:6:6")


@op("Effects & Colour", "vignette", "Vignette (darkened corners)", [O("angle", "Strength (0.2-1.2, lower = stronger)", "float", 0.6)])
def b_vignette(o, c):
    return _vf(c, "_vignette", f"vignette=angle=PI*{o['angle']}")


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


@op("Effects & Colour", "bw", "Black and white", [O("tone", "Look", "choice", "Black & white", ["Black & white", "Sepia"])])
def b_bw(o, c):
    if o["tone"] == "Sepia":
        vf = "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    else:
        vf = "hue=s=0"
    return _vf(c, "_bw", vf)


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


@op("Effects & Colour", "deinterlace", "Deinterlace old footage")
def b_deint(o, c):
    return _vf(c, "_deint", "yadif=mode=0")


CATEGORIES = ["Video", "Format & Quality", "Audio", "Combine", "Text & Subtitles", "Effects & Colour", "Images & Frames", "Analysis", "Streaming"]


# --------------------------------------------------------------------------- engine
def default_opts(spec):
    return {s["key"]: s["default"] for s in spec["opts"]}


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


def kind_exts(kind):
    return {"video": VIDEO_EXT, "audio": AUDIO_EXT, "image": IMAGE_EXT, "sub": SUB_EXT}.get(kind, VIDEO_EXT | AUDIO_EXT)


def run_job(j, log, progress, cancel, show_cmd=False):
    """Runs one job dict. Returns the process return code."""
    if j.get("func"):
        return j["func"](log)
    if j.get("mkdir"):
        os.makedirs(j["mkdir"], exist_ok=True)
    level = "info" if j.get("info") else "warning"
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", level, "-nostats", "-progress", "pipe:1"] + j["args"]
    if j.get("out") and not j.get("raw_out"):
        cmd.append(j["out"])
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


def output_paths(j):
    if j.get("tmpout"):
        return []
    if j.get("outs"):
        return list(j["outs"]) + ([j["out"]] if j.get("out") else [])
    return [j["out"]] if j.get("out") else []


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


STARTUP_SOUND = RES_DIR / "Resource" / "Whips _the_Llama_Ass.mp3"


def play_startup_sound():
    """Play the launch sound. Windows: convert the mp3 to a small wav with ffmpeg, then play it with winsound
    (standard library, same path as normal Windows sounds). Fallback: ffplay. Turn off with
    "startup_sound": false in the settings file. Every step is written to ffmpeg_studio_log.txt."""
    if not load_settings().get("startup_sound", True):
        return
    try:
        import atexit
        snd = STARTUP_SOUND
        if not snd.exists():  # fall back to any mp3 in the Resource folder (handles renamed files)
            found = sorted(snd.parent.glob("*.mp3")) if snd.parent.is_dir() else []
            if not found:
                write_log(f"Startup sound not found: {STARTUP_SOUND}")
                return
            snd = found[0]
        if os.name == "nt":
            try:
                import winsound
                wav = Path(tempfile.gettempdir()) / "om_ffmpeg_studio_start.wav"
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


# --------------------------------------------------------------------------- GUI
CAT_ICONS = {"Favourites": "★", "Video": "\U0001F3AC", "Format & Quality": "\U0001F527", "Audio": "\U0001F3B5", "Combine": "\U0001F9E9",
             "Text & Subtitles": "\U0001F4AC", "Effects & Colour": "\U0001F3A8", "Images & Frames": "\U0001F5BC", "Analysis": "\U0001F50D",
             "Streaming": "\U0001F4E1"}
PALETTE = {
    "light": dict(bg="#fafafa", fg="#1a1a1a", muted="#666666", log_bg="#ffffff", log_fg="#202020", sel="#cfe3ff"),
    "dark": dict(bg="#1c1c1c", fg="#f0f0f0", muted="#a6a6a6", log_bg="#141414", log_fg="#e6e6e6", sel="#33507a"),
}
BRAND_BG, BRAND_FG, BRAND_GOLD = "#0f2a47", "#ffffff", "#f2c14e"


def ff_version():
    try:
        r = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        m = re.search(r"ffmpeg version (\S+)", r.stdout)
        return m.group(1) if m else ""
    except Exception:
        return ""


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
    theme = settings.get("theme", "light") if sv_ttk else "light"
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
    f_base = tkfont.Font(family="Segoe UI", size=fsize["v"])
    f_bold = tkfont.Font(family="Segoe UI", size=fsize["v"], weight="bold")
    f_head = tkfont.Font(family="Segoe UI", size=fsize["v"] + 3, weight="bold")
    f_mono = tkfont.Font(family="Consolas", size=fsize["v"] - 1)
    f_title = tkfont.Font(family="Segoe UI", size=fsize["v"] + 9, weight="normal")
    f_brand = tkfont.Font(family="Segoe UI", size=fsize["v"] + 2, weight="normal")
    def _tk_error(et, ev, tb):
        import traceback
        write_log("GUI ERROR\n" + "".join(traceback.format_exception(et, ev, tb)))
        sys.__excepthook__(et, ev, tb)
    root.report_callback_exception = _tk_error
    write_log(f"--- {APP} v{VERSION} started ({'installed exe' if FROZEN else 'script'}, python {sys.version.split()[0]}, ffmpeg: {ff_version() if find_tool('ffmpeg') else 'not found'}) ---")
    wrapped, themed_text, themed_canvas = [], [], []
    state = {"running": False, "outputs": [], "outdir": None, "theme": theme}

    def pal():
        return PALETTE[state["theme"]]

    def apply_fonts():
        n = fsize["v"]
        for f_, extra in ((f_base, 0), (f_bold, 0), (f_head, 3), (f_mono, -1), (f_title, 9), (f_brand, 2)):
            f_.configure(size=n + extra)
        for nm in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(nm).configure(family="Segoe UI", size=n)
            except tk.TclError:
                pass
        for nm, extra in (("SunValleyBodyFont", 0), ("SunValleyCaptionFont", -1), ("SunValleyBodyStrongFont", 0),
                          ("SunValleyBodyLargeFont", 2), ("SunValleySubtitleFont", 4)):
            try:
                tkfont.nametofont(nm).configure(size=n + extra)
            except tk.TclError:
                pass

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

    def set_fsize(delta):
        fsize["v"] = max(10, min(22, fsize["v"] + delta))
        apply_styles()
        settings["fsize"] = fsize["v"]
        save_settings(settings)

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
    status_var = tk.StringVar(value="Ready")

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

    # ------------------------------------------------------------------ input card
    card = ttk.LabelFrame(root, text="Input", padding=(12, 8, 12, 8))
    card.pack(fill="x", padx=14, pady=(12, 4))
    card.columnconfigure(1, weight=1)
    ttk.Label(card, text="File or folder:").grid(row=0, column=0, sticky="w")
    ent = ttk.Entry(card, textvariable=path_var)
    ent.grid(row=0, column=1, sticky="ew", padx=8)

    def set_path(p):
        path_var.set(p)
        refresh_info()
        if os.path.isfile(p):
            rec = [x for x in settings.get("recent", []) if x != p]
            settings["recent"] = ([p] + rec)[:10]
            recent["values"] = settings["recent"]
            save_settings(settings)

    def browse_file():
        ft = [("Media files", " ".join("*" + e for e in sorted(VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | SUB_EXT))), ("All files", "*.*")]
        p = filedialog.askopenfilename(title="Choose a file", filetypes=ft, initialdir=settings.get("lastdir") or None)
        if p:
            settings["lastdir"] = str(Path(p).parent)
            set_path(os.path.normpath(p))

    def browse_folder():
        p = filedialog.askdirectory(title="Choose a folder (batch)", initialdir=settings.get("lastdir") or None)
        if p:
            settings["lastdir"] = p
            set_path(os.path.normpath(p))

    ttk.Button(card, text="Browse file…", command=browse_file).grid(row=0, column=2)
    ttk.Button(card, text="Batch folder…", command=browse_folder).grid(row=0, column=3, padx=(6, 0))
    ttk.Label(card, text="Recent:").grid(row=1, column=0, sticky="w", pady=(6, 0))
    recent = ttk.Combobox(card, values=settings.get("recent", []), state="readonly")
    recent.grid(row=1, column=1, columnspan=3, sticky="ew", padx=8, pady=(6, 0))
    recent.bind("<<ComboboxSelected>>", lambda e: set_path(recent.get()))
    ttk.Label(card, text="Save in:").grid(row=2, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(card, textvariable=out_var).grid(row=2, column=1, sticky="ew", padx=8, pady=(6, 0))

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

    def refresh_info():
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
    ttk.Checkbutton(row, text="Show ffmpeg commands", variable=cmd_var).pack(side="left", padx=10)
    if gpu_available:
        ttk.Checkbutton(row, text="Use GPU (NVENC)", variable=gpu_var, command=lambda: STATE.update(gpu=gpu_var.get())).pack(side="left")
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

    def fill_buttons(fr, ops, start_row=1):
        for i, spec in enumerate(ops):
            b = ttk.Button(fr, text=spec["label"].replace(" (", "\n("), style="Op.TButton", command=lambda s=spec: open_dialog(s))
            b.grid(row=start_row + i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
        for col in range(3):
            fr.columnconfigure(col, weight=1, uniform="c")
        for rw in range((len(ops) + 2) // 3):
            fr.rowconfigure(start_row + rw, uniform="rows", minsize=78)

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

    def show_cat():
        settings["cat"] = cat_var.get()
        save_settings(settings)
        fr = cat_frames.get(cat_var.get())
        if fr is not None:
            fr.tkraise()

    def on_drop(event):
        try:
            files = root.tk.splitlist(event.data)
        except Exception:  # noqa: BLE001
            files = [event.data]
        if files:
            set_path(os.path.normpath(files[0]))
            if len(files) > 1:
                log(f"You dropped {len(files)} items; using the first. Use 'Batch folder' to process a whole folder.")

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
    def log(msg):
        q.put(("log", msg))

    def open_folder():
        d = state["outdir"]
        if d and os.path.isdir(d):
            if os.name == "nt":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", d])

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
                elif kind == "done":
                    state["running"] = False
                    cancel_btn.configure(state="disabled")
                    ok, fail, outs = val
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

    def ft_for(t):
        ex = {"audio": AUDIO_EXT | VIDEO_EXT, "image": IMAGE_EXT, "video": VIDEO_EXT, "sub": SUB_EXT, "lut": {".cube", ".3dl"},
              "media": VIDEO_EXT | AUDIO_EXT | IMAGE_EXT}.get(t)
        return ([("Files", " ".join("*" + e for e in sorted(ex))), ("All files", "*.*")] if ex else [("All files", "*.*")])

    # ------------------------------------------------------------------ option dialog
    def open_dialog(spec):
        if state["running"]:
            messagebox.showinfo(APP, "A job is already running.")
            return
        top_ = tk.Toplevel(root)
        top_.title(spec["label"])
        top_.transient(root)
        try:
            top_.configure(bg=pal()["bg"])
        except tk.TclError:
            pass
        top_.grab_set()
        frm = ttk.Frame(top_, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=spec["label"], style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        r = 1
        if spec["tip"]:
            ttk.Label(frm, text=spec["tip"], style="Muted.TLabel", wraplength=max(500, 52 * fsize["v"])).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 8))
            r += 1
        last = settings.get("last", {}).get(spec["id"], {})
        getters = {}
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
            elif t == "bool":
                v = tk.BooleanVar(value=bool(dflt))
                ttk.Checkbutton(frm, variable=v).grid(row=r, column=1, sticky="w")
                getters[s["key"]] = v.get
            elif t == "file":
                v = tk.StringVar()
                ttk.Entry(frm, textvariable=v, width=34).grid(row=r, column=1, sticky="ew")
                ttk.Button(frm, text="…", width=3, command=lambda v=v, t=s["ftype"]: v.set(filedialog.askopenfilename(filetypes=ft_for(t)) or v.get())).grid(row=r, column=2, padx=4)
                getters[s["key"]] = v.get
            elif t == "files":
                lb = tk.Listbox(frm, height=5, width=40, relief="flat", bg=pal()["log_bg"], fg=pal()["log_fg"], selectbackground=pal()["sel"], highlightthickness=1)
                lb.grid(row=r, column=1, sticky="ew")
                bf = ttk.Frame(frm)
                bf.grid(row=r, column=2, padx=4)
                ttk.Button(bf, text="Add…", command=lambda lb=lb: [lb.insert("end", p) for p in filedialog.askopenfilenames(filetypes=ft_for("media"))]).pack(fill="x")
                ttk.Button(bf, text="Remove", command=lambda lb=lb: [lb.delete(i) for i in reversed(lb.curselection())]).pack(fill="x", pady=2)
                getters[s["key"]] = lambda lb=lb: list(lb.get(0, "end"))
            else:
                v = tk.StringVar(value=str(dflt))
                ttk.Entry(frm, textvariable=v, width=40).grid(row=r, column=1, columnspan=2, sticky="ew")
                getters[s["key"]] = v.get
            r += 1
        if not spec["opts"]:
            ttk.Label(frm, text="No options - just press Run.").grid(row=r, column=0, columnspan=3, sticky="w", pady=6)
            r += 1
        fav_var = tk.BooleanVar(value=spec["id"] in settings.get("favs", []))
        ttk.Checkbutton(frm, text="★  Add to favourites", variable=fav_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))
        bf = ttk.Frame(frm)
        bf.grid(row=r, column=1, columnspan=2, sticky="e", pady=(10, 0))

        def go():
            raw = {k: g() for k, g in getters.items()}
            try:
                opts = convert_opts(spec, raw)
            except ValueError:
                messagebox.showerror(APP, "One of the numbers is not valid.", parent=top_)
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
            start(spec, opts)
        ttk.Button(bf, text="Cancel", command=top_.destroy).pack(side="left", padx=6)
        ttk.Button(bf, text="Run", style=run_style, command=go).pack(side="left")
        top_.bind("<Return>", lambda e: go())
        top_.bind("<Escape>", lambda e: top_.destroy())
        try:
            top_.update_idletasks()
            x = root.winfo_rootx() + max(0, (root.winfo_width() - top_.winfo_reqwidth()) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - top_.winfo_reqheight()) // 3)
            top_.geometry(f"+{x}+{y}")
        except Exception:  # noqa: BLE001
            pass

    def start(spec, opts):
        if not have_ff:
            messagebox.showerror(APP, "ffmpeg is not installed yet. Restart the app and choose Install ffmpeg when asked.")
            return
        p = path_var.get().strip().strip('"')
        if not p or not os.path.exists(p):
            messagebox.showwarning(APP, "Choose a file or a folder first.")
            return
        if os.path.isdir(p):
            if any(s["type"] in ("file", "files") for s in spec["opts"]) and not spec.get("folder_one"):
                messagebox.showwarning(APP, "This operation needs extra files, so it can't run on a whole folder. Pick a single file.")
                return
            exts = kind_exts(spec["kind"])
            files = sorted(str(f) for f in Path(p).iterdir() if f.is_file() and f.suffix.lower() in exts)
            if not files:
                messagebox.showinfo(APP, "No matching files in that folder.")
                return
            if spec.get("folder_one"):
                opts = dict(opts)
                opts["files"] = files[1:]
                files = files[:1]
        else:
            files = [p]
        outdir = out_var.get().strip() or None
        if outdir:
            os.makedirs(outdir, exist_ok=True)
        state.update(running=True, outputs=[])
        cancel.clear()
        cancel_btn.configure(state="normal")
        open_btn.configure(state="disabled")
        bar["value"] = 0
        status_var.set(f"Running: {spec['label']}")
        log(f"\n##### {spec['label']}  ({len(files)} file{'s' if len(files) > 1 else ''})")

        def work():
            res = execute(spec, opts, files, outdir, log, lambda f: q.put(("prog", f)), cancel, cmd_var.get())
            q.put(("done", res))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ finish up
    for cat in ["Favourites"] + CATEGORIES:
        build_cat(cat)
    show_cat()
    def offer_ffmpeg_install():
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
    status_var.set("Ready" + (f"   |   ffmpeg {ver}" if ver else "") + ("   |   " + "; ".join(extras) if extras else ""))
    if not have_ff:
        refresh_info()
        root.after(300, offer_ffmpeg_install)
    def check_update():
        """Ask GitHub for the latest release (background thread); prompt only if a newer one exists."""
        def work():
            try:
                import urllib.request
                req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", headers={"User-Agent": "OM-FFmpeg-Studio"})
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


# --------------------------------------------------------------------------- self-test
def selftest():
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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--list" in sys.argv:
        for o in OPS:
            print(f'{o["cat"]:18} {o["id"]:14} {o["label"]}')
        sys.exit(0)
    gui()
