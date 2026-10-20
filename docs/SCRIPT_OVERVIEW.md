# `om_video_studio.py` - brief

The whole application in one file (~3,600 lines, mostly standard library). It is a tkinter window that builds and runs **ffmpeg** command lines for the user. This brief explains how the file is organised and how a click becomes a finished video. The source itself is also commented section by section.

Run modes:

```
python om_video_studio.py              # open the GUI
python om_video_studio.py --list       # print all 70 operations
python om_video_studio.py --selftest   # run every operation on a generated clip (headless)
python om_video_studio.py --selftest trim,split,extras   # only these (CI uses this)
```

## The core idea

Every feature is a small **builder function** registered with `@op(...)`. A builder never runs ffmpeg. It takes the user's options `o` and a per-file context `c` and returns a **list of jobs** (ffmpeg arguments, expected output path, duration). One engine runs those jobs. That single design is why the same code powers the real run, "Copy ffmpeg command", the queue, the live preview and the self-test.

```
click -> option dialog -> collect()/convert_opts() -> execute()
          for each file: Ctx(file) -> builder(o, c) -> [job, job...] -> run_job() -> ffmpeg
```

## Sections, top to bottom

| # | Section | What lives there |
|---|---|---|
| 1 | Constants & paths | `APP`, `VERSION` (CI reads this line to name the release), file-extension sets, `STATE` (GPU flag), `FROZEN` / `APP_DIR` / `RES_DIR` / `DATA_DIR` (source vs installed `.exe`), `SETTINGS_PATH` (`~/.om_video_studio.json`) |
| 2 | ffmpeg location & install | `find_tool` (PATH first, then the app's own download), `FFMPEG`/`FFPROBE`, `urlopen_robust` (system then certifi certificates), `download_file`, `install_ffmpeg` |
| 3 | Log | `write_log` -> `om_video_studio_log.txt` (trimmed ~1 MB), `_excepthook` for uncaught errors |
| 4 | Helpers | `tsec` (time parser), `probe` (ffprobe -> dict), `summary`, `vcodec` (CPU x264 or NVENC), `AAC`/`aargs`, `atempo` (chains for big speed factors), `Ctx` (per-file info + non-clashing output names), `job` |
| 5 | Registry | `OPS`, `O()` (option descriptor: text/int/float/bool/choice/file/files), `register`, `@op` |
| 6 | Operations | 70 builders in 9 categories (below) |
| 7 | Engine | `default_opts`, `kind_ok`/`kind_exts`, `job_cmd`, `run_job`, `output_paths`, `execute`, `convert_opts` |
| 8 | Preview / copy / queue engine | `fmt_ms`, `eval_expr`, `SLIDERS`, `format_commands`, `grab_frame`, `preview_frame`, `resolve_files`, `run_queue` |
| 9 | Settings & start-up sound | `load_settings`, `save_settings`, `play_startup_sound` |
| 10 | Languages | `LANGS`/`TR` from `om_translations.py`, `set_lang`, `tr` |
| 11 | Completion pings | `_tone_wav`, `SOUNDS`, `sound_file`, `play_ping`, `ping_kind` |
| 12 | GUI | `detect_gpu`, `CAT_ICONS`, `PALETTE`, `gui()` - the entire window |
| 13 | Self-test & CLI | `selftest_extras`, `selftest`, `__main__` dispatch |

### The 70 operations

| Category | Count | Examples |
|---|---|---|
| Video | 13 | trim, join, split, speed, reverse, resize, crop, rotate, flip, aspect ratio, loop, stabilise, freeze frame |
| Format & Quality | 6 | convert, re-wrap, fast-start, social export, compress to size (two-pass), recode |
| Audio | 14 | extract/convert, loudness normalise, remove silence, trim, fade, volume, tempo, pitch, denoise, EQ, add music / narration, channels |
| Images & Frames | 5 | thumbnail, frames, contact sheet, GIF, image convert |
| Analysis | 9 | full file report, silence / black / freeze / scene detection, loudness, waveform, spectrogram, strip metadata |
| Streaming | 1 | HLS |
| Combine | 5 | watermark, picture-in-picture, grid, crossfade join, slideshow |
| Text & Subtitles | 6 | burn-in, soft track, extract, convert, title / lower third, timer |
| Effects & Colour | 11 | colour, LUT, blur, sharpen, denoise, vignette, chroma key, fade, B&W/sepia, speed ramp, deinterlace |

Option dialogs are generated from each operation's option list, so adding an operation means writing one decorated function. `kind` (video/audio/image/sub/any) tells the engine which files an operation accepts; `folder_one=True` makes a whole folder one job (slideshow). Some operations return several jobs (freeze frame cuts temp pieces then concatenates; compress runs two passes). Pure-Python jobs use `func=` instead of ffmpeg (the full file report).

## The engine in detail

- `run_job` starts ffmpeg with `-progress pipe:1`, turns `out_time_us` into a 0-1 progress value, supports cancel (kills the process), and decides what to print: all matching lines for detectors (`show` regex), the last N lines (`tail`), or a short tail on success.
- `execute` runs one operation over many files with a scratch folder, per-file error isolation, a "does this file suit this operation" check, and a **GPU fallback**: if NVENC fails it switches to CPU and retries once. Failures write the full command and ffmpeg output to the log.
- Outputs are never overwritten: `Ctx.out()` appends `_2`, `_3`... if a name exists.

## The GUI (`gui()`)

Everything is a closure inside one function, sharing local state.

1. **Language layer** - widget classes are patched so every `text=` goes through `tr()`; changing language asks for a restart.
2. **Theme** - `sv_ttk` light/dark, fonts scaled by A+/A-, native title bar colour via `pywinstyles` (all optional, best effort).
3. **Thread safety** - worker threads never touch Tk. They put `('log'|'prog'|'info'|'call'|'done', ...)` on a queue; `pump()` drains it every 100 ms on the GUI thread, updates the log/progress, and plays the completion ping.
4. **Layout** - header (theme, language), Input card (file / folder / recents / output folder / info line), sidebar of categories with button grids and a Favourites page, bottom bar (Cancel, Open output folder, Queue, Copy command, GPU, Sound, font size, log box). Drag-and-drop is registered on every widget.
5. **`open_dialog(spec)`** - builds one widget per option, optional sliders, a live preview, a tip line, a Favourite star and the Copy / Add to queue / Run buttons.
6. **Sliders** (`add_slider`) - time, length, number and crop-box sliders kept in two-way sync with their text boxes; time boxes use `MMM:SS:mmm` with nudge buttons.
7. **Live preview** (`attach_preview`) - fixed-size picture holders, 600 ms debounce, rendered by a worker thread with `grab_frame`/`preview_frame`; before/after pairs, trim start/end frames, crop result.
8. **Queue** - `enqueue` stores `(op id, options, files)`; `open_queue` lets you reorder/remove/clear; `run_queue_gui` runs it on a worker.
9. **Start-up** - offers to install ffmpeg if missing, schedules an update check (GitHub releases API, ~3 s after launch) and the start-up sound.

## Languages and sounds

`om_translations.py` (separate file) supplies 13 translations plus English. The English strings in the code are the keys; `tr()` falls back to the English text if a key is missing, so a gap can't break the UI. Templates with `{}` (e.g. "Running: {}") are matched by pattern. Completion pings are synthesised WAVs (single job, batch/queue, error) played with `winsound`; the Sound checkbox turns them off.

## Self-test

`--selftest` makes a tiny clip, audio, image, subtitle file and LUT with ffmpeg, runs each operation through `execute()` and prints PASSED / FAILED / SKIPPED (Stabilise skips if ffmpeg lacks libvidstab). `--full` also tries every choice value; `--gpu` exercises NVENC fallback. `extras` checks things ffmpeg doesn't cover: translation coverage of every label, `tr()` behaviour, distinct ping sounds, and the full-report file naming. CI runs it before building.

## Extending it

1. Add an `@op("Category", "id", "Button text", [O(...)], kind=...)` function that returns `[job(...)]`.
2. Add the English label and option labels to `TR` in `om_translations.py` for each language (the `extras` self-test fails if any are missing).
3. Optional: add a `SLIDERS` entry for sliders/preview.
4. Bump `VERSION`, push to `main` - CI builds and publishes the release.
