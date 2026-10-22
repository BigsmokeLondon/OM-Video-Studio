# Quick Capture (on-demand screen / window recording)

A standalone helper, separate from O.M. Video Studio. `Quick_Capture.bat` records with ffmpeg; `Window_Rect.ps1` (must sit in the same folder) finds a window's position for option 2. Tested and working on the PC (21 Oct 2026, Calculator).

## Use
1. Double-click `Quick_Capture.bat` (in `C:\Users\Admin\Claude projects\FFmpegStudio`).
2. Choose: 1 whole screen, 2 one window, 3 region (x, y, width, height), 4 webcam.
   - For option 2, a numbered list of open windows is shown in the command window. Type the number (or part of the title). You can also type it on the menu line, e.g. `2 Calculator`.
3. Enter a frame rate (default 30), an optional auto-stop in seconds (recommended, e.g. 10-20), and whether to add a microphone.
4. Wait for the timer, or press **Q** in the black window to stop. The window stays open afterwards so you can read any error.
5. Clip is saved to `C:\Users\Admin\Videos\Captures\capture_<date>_<time>.mp4` and the folder opens.

## List_Windows.bat (optional)
The list is now built into Quick_Capture, so this is only needed on its own. Double-click any time to open a Notepad list of open window titles (also saved as `open_windows.txt` in the Captures folder). Only windows with a title are listed.

## How it works
- Finds ffmpeg on PATH, otherwise inside the O.M. Video Studio install (`%LOCALAPPDATA%\O.M. Video Studio`).
- Screen/region use ffmpeg `gdigrab`; webcam and microphone use `dshow`.
- One window: `Window_Rect.ps1` looks the window up by title (exact, then partial), restores and brings it to the front, and returns its screen rectangle. Quick_Capture then records that area of the desktop. (Capturing by `title=` gives a black video for modern Windows apps such as Calculator, Settings and Photos.)
- Records to `.mkv` first (crash-safe), then remuxes to `.mp4` with no re-encode.
- Encodes H.264 (`libx264`, ultrafast, CRF 20); odd-sized windows are padded to even dimensions.

## Limits
- Window mode records a screen area, so anything covering or overlapping the window (including the command window) is recorded too. Keep it visible and uncovered; use the timer rather than clicking into the command window to press Q.
- If the window is moved during recording, the capture does not follow it.
- System audio needs a loopback device (Stereo Mix or a virtual audio cable).
- For heavy games or 4K, OBS is smoother; Windows Snipping Tool (Win+Shift+R) is the simplest quick option.

## Related
- Claude skill: `quick-screen-capture` (writes and explains this launcher on request).
- Parked idea: a "Record screen / webcam" button inside O.M. Video Studio (device picker + Stop button). Not planned for now.

## Quick_Capture_GUI (window version)
Double-click `Quick_Capture_GUI.bat` (needs `Quick_Capture_GUI.ps1` in the same folder; nothing else to install). Pick Whole screen / One window / Region / Webcam (for Region, click **Select area with the mouse**, drag over the area, and X, Y, W, H fill in), choose the window or device from the drop-downs (Refresh re-reads them), set frame rate, optional auto-stop seconds (0 = press Stop), start delay, microphone and save folder, then **Start video recording**. The window minimises while recording; restore it from the taskbar and press **Stop** (or let the timer end it). The clip opens in its folder when done. If ffmpeg is missing, it asks permission on start-up (or via the **Install ffmpeg** button) and then installs it with winget, or downloads it from gyan.dev into `%LOCALAPPDATA%\QuickCapture` if winget is not available. ffmpeg is the only requirement. The .bat version above still works as a fallback.

### System sound (GUI)
Tick **System sound** to record what the PC is playing through its speakers or headphones (the default playback device). This uses Windows' own loopback recording built into the script, so no Stereo Mix, driver or extra download is needed. It is recorded to a temporary .wav and mixed into the final .mp4; it can be combined with the microphone. Only the default playback device is captured, and if nothing is playing the track is silent. If the PC has no playback device, the video is recorded without sound and a message says so.

### Still pictures and the folder window (GUI)
- **Take picture** saves a PNG (`picture_<date>_<time>.png`) of the chosen target: Whole screen (the screen the Quick Capture window is on), One window, or Region (use **Select area with the mouse**). Webcam is not supported for pictures. It waits only half a second (so the Quick Capture window can get out of the way) and, if ticked, also copies the picture to the clipboard.
- **Show the save folder when finished**: if an Explorer window for the save folder is already open it is brought to the front (and the new file selected) instead of opening another one; otherwise the folder is opened. The **Open folder** button behaves the same way.
