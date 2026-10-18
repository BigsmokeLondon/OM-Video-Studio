# O.M. FFmpeg Studio

Point-and-click video, audio and image tools for Windows, powered by ffmpeg. Pick a file, press a button.
Trim, join, split, convert, compress, extract audio, subtitles, effects, GIFs, thumbnails and more (69 operations).

## Install (Windows 10/11)

1. Go to **[Releases](../../releases/latest)** and download **`OM-FFmpeg-Studio-Setup.exe`**.
2. Run it. No admin rights needed, and **you do not need Python**.
   - Windows may show *"Windows protected your PC"* because the app is not code-signed. Click **More info → Run anyway**.
3. Launch **O.M. FFmpeg Studio**. If ffmpeg is not on your computer, the app offers to download it for you (about 100 MB, one time).

Prefer no installer? Download `OM-FFmpeg-Studio-Portable.zip`, unzip anywhere and run `OM-FFmpeg-Studio.exe`.

## Notes

- Problems are written to `%LOCALAPPDATA%\O.M. FFmpeg Studio\ffmpeg_studio_log.txt`. Attach it if you report a bug.
- The app checks GitHub for new releases at start-up and tells you when one is available.
- Uninstall from Windows Settings → Apps.

## Build from source

```
build.bat            # needs Python 3.10-3.13; add Inno Setup to also make the installer
```
Every push to `main` makes GitHub Actions build the installer and publish release `v<VERSION>` (the `VERSION` line in `ffmpeg_studio.py`); bump it to ship a new version.
Run the app straight from source with `pip install -r requirements.txt` then `python ffmpeg_studio.py`.

## Licence

App code: MIT (see `LICENSE`). ffmpeg is a separate project with its own licence; it is downloaded from its official build providers and is not included here.
