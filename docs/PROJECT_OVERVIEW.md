# O.M. Video Studio - project overview

A Windows desktop app that puts a friendly point-and-click interface on **ffmpeg**: choose a file, press a tool, set a few options, run. Python + tkinter, shipped as a normal installer. Repo: <https://github.com/BigsmokeLondon/OM-Video-Studio> (publisher: M.A. Industries Inc).

## What is in the repository

```
OM-Video-Studio/
|-- om_video_studio.py        The application (engine + GUI + self-test), one file
|-- om_translations.py        UI text in 13 languages + English keys (LANGS, TR)
|-- requirements.txt          Optional GUI extras: sv-ttk, tkinterdnd2, pywinstyles, certifi
|-- app.ico                   Window / exe / installer icon
|-- Resource/                 Bundled read-only files (optional start-up sound + README.txt)
|-- installer/setup.iss       Inno Setup script -> OM-Video-Studio-Setup.exe
|-- build.bat                 Local build: PyInstaller, then Inno Setup if present
|-- .github/workflows/
|   `-- release.yml           CI: test, build, package, publish a GitHub release
|-- docs/
|   |-- images/               Screenshots + demo.gif used by README
|   |-- videos/               Install, quick-tour and full-walkthrough videos (+ README.md)
|   |-- SCRIPT_OVERVIEW.md    Brief on om_video_studio.py
|   `-- PROJECT_OVERVIEW.md   This file
|-- README.md                 Public landing page: features, install, release notes
|-- LICENSE
`-- .gitignore                Ignores build/, dist/, Output/, *.spec, log file, __pycache__
```

## How the pieces fit together

```
            requirements.txt ----------------+
                                              v
 om_translations.py --imported by--> om_video_studio.py --PyInstaller--> dist/OM-Video-Studio/ (OM-Video-Studio.exe + libs)
 app.ico, Resource/ --bundled via --add-data--^                              |
                                                         Inno Setup (setup.iss) + zip
                                                                              v
                                                   Output/OM-Video-Studio-Setup.exe
                                                   Output/OM-Video-Studio-Portable.zip
                                                                              v
                                                       GitHub Release  v<VERSION>
                                                                              ^
                       the app's update check reads this release list (GITHUB_REPO)
```

- **`om_video_studio.py`** is the product. It imports `om_translations` for languages and, when frozen, reads `app.ico` and `Resource/` from PyInstaller's bundle (`RES_DIR`). User data (log, downloaded ffmpeg) goes to `%LOCALAPPDATA%\O.M. Video Studio`; settings go to `~/.om_video_studio.json`.
- **`om_translations.py`** must be named explicitly with `--hidden-import om_translations` in every PyInstaller call (workflow and `build.bat`), because the main script imports it in a way PyInstaller's scanner can miss.
- **`requirements.txt`** holds the optional look-and-feel libraries. The app still starts without them (plain theme, no drag-and-drop), but release builds include them via `--collect-all`.
- **ffmpeg itself is not in the repo.** Users either already have it, or the app offers a one-time download (~100 MB, unpacked under the data folder). CI installs ffmpeg with Chocolatey only to run the self-test.
- **`installer/setup.iss`** wraps the PyInstaller output in a per-user installer (no admin needed, desktop-shortcut task, launch-after-install, cleanup of the old "FFmpeg Studio" names). It receives the version as `/DAppVersion=`.
- **`docs/`** is documentation and marketing material only; nothing in it is used at run time.

## Build and release flow

Every push to `main` triggers `.github/workflows/release.yml` on `windows-latest`:

1. Check out, set up Python 3.12.
2. Read the version from the line `VERSION = "x.y.z"` in `om_video_studio.py`.
3. Install `requirements.txt` + PyInstaller; install ffmpeg + Inno Setup with Chocolatey (retries, since the feed is flaky).
4. **Self-test**: `python om_video_studio.py --selftest trim,split,convert,compress,extract,thumb,extras`. A failure stops the release.
5. **PyInstaller** builds `dist/OM-Video-Studio` (windowed, icon, bundled `app.ico` + `Resource`, `--collect-all` for sv_ttk, tkinterdnd2, pywinstyles, certifi, plus the hidden import).
6. **Inno Setup** compiles the installer; the same folder is zipped as the portable build.
7. Upload artifacts and publish release **`v<VERSION>`** with both files and auto-generated notes.

So the way to ship is: change code, bump `VERSION`, push. To push a docs-only or comment-only change without releasing, put `[skip ci]` in the commit message. Local equivalent: `build.bat 1.6.1`.

## Runtime flow in one picture

```
User picks file(s) -> Input card (probe via ffprobe for the info line)
   -> category button -> option dialog (live preview via ffmpeg single-frame grabs)
   -> Run / Add to queue / Copy ffmpeg command
   -> worker thread: execute() -> builder -> jobs -> ffmpeg subprocess(es)
   -> queue messages -> pump() on GUI thread -> log, progress bar, ping sound
   -> outputs next to the source (or chosen folder), never overwriting
```

Problems are written to `om_video_studio_log.txt`; ask users to send it when reporting a bug.

## Versioning history (recent)

| Version | Change |
|---|---|
| 1.2.0 | Social-media export |
| 1.3.0 | Job queue, Trim/Crop preview, Copy ffmpeg command |
| 1.4.0 | Sliders, minutes:seconds:milliseconds time boxes, before/after previews everywhere |
| 1.5.0 | Resize dropdown 144p-8K with preview sizes |
| 1.6.0 | 13 languages, completion pings with Sound toggle |
| 1.6.1 | Full file report saved as a `.txt` and opened in Notepad |

## Where to change what

| I want to... | Edit |
|---|---|
| Add or change a tool | `om_video_studio.py` - an `@op` builder (+ translations in `om_translations.py`) |
| Add a language | `om_translations.py` - one `TR` entry per English key and a `LANGS` row |
| Change the look | `PALETTE`, `apply_styles` in `gui()` |
| Change what the installer does | `installer/setup.iss` |
| Change how releases are built | `.github/workflows/release.yml` (and `build.bat` to match) |
| Change the public page | `README.md`, `docs/images`, `docs/videos` |
| Release a new version | bump `VERSION`, push to `main` |

Related brief: [`SCRIPT_OVERVIEW.md`](SCRIPT_OVERVIEW.md) covers the Python file in depth.
