# Quick Capture GUI - on-demand screen / window / region / webcam recording with ffmpeg.
# Standalone (not part of O.M. Video Studio). Launch with Quick_Capture_GUI.bat.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class QCWin {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out RECT r, int s);
  public struct RECT { public int L, T, R, B; }
}
"@
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
using System.Diagnostics;
using System.Runtime.InteropServices;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDevEnumCom { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
  [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  [PreserveSig] int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
}

[ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioClient {
  [PreserveSig] int Initialize(int shareMode, int streamFlags, long hnsBufferDuration, long hnsPeriodicity, IntPtr pFormat, IntPtr audioSessionGuid);
  [PreserveSig] int GetBufferSize(out uint n);
  [PreserveSig] int GetStreamLatency(out long l);
  [PreserveSig] int GetCurrentPadding(out uint n);
  [PreserveSig] int IsFormatSupported(int shareMode, IntPtr fmt, out IntPtr closest);
  [PreserveSig] int GetMixFormat(out IntPtr fmt);
  [PreserveSig] int GetDevicePeriod(out long def, out long min);
  [PreserveSig] int Start();
  [PreserveSig] int Stop();
  [PreserveSig] int Reset();
  [PreserveSig] int SetEventHandle(IntPtr h);
  [PreserveSig] int GetService(ref Guid riid, [MarshalAs(UnmanagedType.IUnknown)] out object ppv);
}

[ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioCaptureClient {
  [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devPos, out ulong qpc);
  [PreserveSig] int ReleaseBuffer(uint frames);
  [PreserveSig] int GetNextPacketSize(out uint frames);
}

public class LoopRec {
  Thread th;
  volatile bool stopFlag;
  ManualResetEvent ready = new ManualResetEvent(false);
  string path;
  public string Error = "";

  public string Start(string wavPath) {
    path = wavPath;
    th = new Thread(Run);
    th.IsBackground = true;
    th.Start();
    ready.WaitOne(5000);
    return Error;
  }

  public void Stop() {
    stopFlag = true;
    if (th != null) th.Join(5000);
  }

  static void Chk(int hr, string what) {
    if (hr != 0) throw new Exception(what + " (0x" + hr.ToString("X8") + ")");
  }

  static void WriteZeros(FileStream fs, long bytes, byte[] zero) {
    while (bytes > 0) {
      int n = (int)Math.Min(bytes, (long)zero.Length);
      fs.Write(zero, 0, n);
      bytes -= n;
    }
  }

  static void WriteHeader(FileStream fs, byte[] fmt, long dataLen) {
    fs.Seek(0, SeekOrigin.Begin);
    BinaryWriter w = new BinaryWriter(fs);
    w.Write(new byte[] { 0x52, 0x49, 0x46, 0x46 });
    w.Write((uint)(4 + 8 + fmt.Length + 8 + dataLen));
    w.Write(new byte[] { 0x57, 0x41, 0x56, 0x45 });
    w.Write(new byte[] { 0x66, 0x6D, 0x74, 0x20 });
    w.Write((uint)fmt.Length);
    w.Write(fmt);
    w.Write(new byte[] { 0x64, 0x61, 0x74, 0x61 });
    w.Write((uint)dataLen);
    w.Flush();
  }

  void Run() {
    IAudioClient client = null;
    FileStream fs = null;
    IntPtr pf = IntPtr.Zero;
    bool started = false;
    try {
      IMMDeviceEnumerator en = (IMMDeviceEnumerator)new MMDevEnumCom();
      IMMDevice dev;
      Chk(en.GetDefaultAudioEndpoint(0, 0, out dev), "No default playback device found");
      Guid iidClient = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
      object o;
      Chk(dev.Activate(ref iidClient, 23, IntPtr.Zero, out o), "Could not open the playback device");
      client = (IAudioClient)o;
      Chk(client.GetMixFormat(out pf), "GetMixFormat");
      int rate = Marshal.ReadInt32(pf, 4);
      int blockAlign = Marshal.ReadInt16(pf, 12);
      int cb = Marshal.ReadInt16(pf, 16);
      byte[] fmt = new byte[18 + cb];
      Marshal.Copy(pf, fmt, 0, fmt.Length);
      Chk(client.Initialize(0, 0x00020000, 10000000L, 0L, pf, IntPtr.Zero), "Could not start loopback capture");
      Guid iidCap = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
      object oc;
      Chk(client.GetService(ref iidCap, out oc), "GetService");
      IAudioCaptureClient cap = (IAudioCaptureClient)oc;
      fs = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read);
      WriteHeader(fs, fmt, 0);
      fs.Seek(0, SeekOrigin.End);
      Chk(client.Start(), "Start");
      started = true;
      long frames = 0;
      Stopwatch sw = Stopwatch.StartNew();
      byte[] zero = new byte[65536];
      ready.Set();
      while (!stopFlag) {
        Thread.Sleep(10);
        uint pkt;
        while (cap.GetNextPacketSize(out pkt) == 0 && pkt > 0) {
          IntPtr data; uint n; uint flags; ulong dp; ulong qpc;
          if (cap.GetBuffer(out data, out n, out flags, out dp, out qpc) != 0) break;
          int bytes = (int)n * blockAlign;
          if ((flags & 2) != 0) {
            WriteZeros(fs, bytes, zero);
          } else {
            byte[] buf = new byte[bytes];
            Marshal.Copy(data, buf, 0, bytes);
            fs.Write(buf, 0, bytes);
          }
          cap.ReleaseBuffer(n);
          frames += n;
        }
        long expected = (long)(sw.Elapsed.TotalSeconds * rate);
        long deficit = expected - frames;
        if (deficit > rate / 10) {
          WriteZeros(fs, deficit * blockAlign, zero);
          frames += deficit;
        }
      }
      long dataLen = frames * blockAlign;
      WriteHeader(fs, fmt, dataLen);
    } catch (Exception e) {
      Error = e.Message;
    } finally {
      try { if (started && client != null) client.Stop(); } catch (Exception) { }
      try { if (fs != null) fs.Close(); } catch (Exception) { }
      try { if (pf != IntPtr.Zero) Marshal.FreeCoTaskMem(pf); } catch (Exception) { }
      ready.Set();
    }
  }
}
'@ -Language CSharp
[QCWin]::SetProcessDPIAware() | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()

# ---------- helpers ----------
function Find-Ffmpeg {
  if ($env:QC_FFMPEG -and (Test-Path -LiteralPath $env:QC_FFMPEG)) { return $env:QC_FFMPEG }   # handed over by O.M. Video Studio
  $c = Get-Command ffmpeg -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $roots = @("$env:LOCALAPPDATA\O.M. Video Studio", "$env:LOCALAPPDATA\Microsoft\WinGet\Packages", "$env:LOCALAPPDATA\QuickCapture", "$env:ProgramFiles\ffmpeg")
  foreach ($r in $roots) {
    if (Test-Path $r) {
      $f = Get-ChildItem -Path $r -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($f) { return $f.FullName }
    }
  }
  return $null
}

function Get-WindowTitles {
  Get-Process | Where-Object { $_.MainWindowTitle -and $_.MainWindowTitle -notlike 'Quick Capture*' } |
    ForEach-Object { $_.MainWindowTitle } | Sort-Object -Unique
}

function Get-WindowRect([string]$Title) {
  $all = Get-Process | Where-Object { $_.MainWindowTitle -and $_.MainWindowHandle -ne 0 }
  $p = $all | Where-Object { $_.MainWindowTitle -eq $Title } | Select-Object -First 1
  if (-not $p) { $p = $all | Where-Object { $_.MainWindowTitle -like "*$Title*" } | Select-Object -First 1 }
  if (-not $p) { return $null }
  $h = $p.MainWindowHandle
  if ([QCWin]::IsIconic($h)) { [QCWin]::ShowWindow($h, 9) | Out-Null }
  [QCWin]::SetForegroundWindow($h) | Out-Null
  Start-Sleep -Milliseconds 600
  $r = New-Object QCWin+RECT
  [QCWin]::DwmGetWindowAttribute($h, 9, [ref]$r, 16) | Out-Null
  $w = $r.R - $r.L; $ht = $r.B - $r.T
  if ($w % 2) { $w++ }
  if ($ht % 2) { $ht++ }
  return @{ X = $r.L; Y = $r.T; W = $w; H = $ht }
}

function Get-Devices {
  $v = @(); $a = @()
  if ($script:ff) {
    $out = cmd /c "`"$($script:ff)`" -hide_banner -list_devices true -f dshow -i dummy 2>&1"
    try { ($out | ForEach-Object { "$_" }) | Set-Content -Path (Join-Path $env:TEMP 'quick_capture_devices.txt') } catch {}
    $section = ''
    foreach ($l in $out) {
      $s = "$l"
      if ($s -match 'DirectShow video devices') { $section = 'video'; continue }
      if ($s -match 'DirectShow audio devices') { $section = 'audio'; continue }
      if ($s -match 'Alternative name') { continue }
      if ($s -match '"([^"]+)"\s*(?:\(([^)]*)\))?\s*$') {
        $n = $Matches[1]; $t = "$($Matches[2])"
        $isV = ($t -match 'video'); $isA = ($t -match 'audio')
        if (-not $isV -and -not $isA) {
          if ($section -eq 'video') { $isV = $true } elseif ($section -eq 'audio') { $isA = $true }
        }
        if ($isV) { $v += $n }
        if ($isA) { $a += $n }
      }
    }
  }
  return @{ video = $v; audio = $a }
}

# ---------- state ----------
$script:ff = Find-Ffmpeg
$script:state = 'idle'      # idle | countdown | recording | stopping
$script:proc = $null
$script:until = $null
$script:started = $null
$script:stopDeadline = $null
$script:mkv = ''; $script:mp4 = ''; $script:log = ''
$script:limit = 0
$script:rec = $null
$script:wav = ''

# ---------- form ----------
$K = 1.45   # layout scale (larger text and controls)
function Px([double]$v) { return [int][math]::Round($v * $K) }
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Quick Capture O.M. Industries Inc ' + [char]0xA9 + ' 2026'
$form.StartPosition = 'CenterScreen'
$form.AutoScaleMode = 'Dpi'
$form.AutoScaleDimensions = New-Object System.Drawing.SizeF(96, 96)
$form.ClientSize = New-Object System.Drawing.Size((Px 560), (Px 522))
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font('Segoe UI', 12)

function New-Label($text, $x, $y, $w = 100) {
  $l = New-Object System.Windows.Forms.Label
  $l.Text = $text; $l.Location = New-Object System.Drawing.Point((Px $x), (Px $y)); $l.Size = New-Object System.Drawing.Size((Px $w), (Px 24))
  $l.TextAlign = 'MiddleLeft'; $form.Controls.Add($l); return $l
}
function New-Button($text, $x, $y, $w = 90) {
  $b = New-Object System.Windows.Forms.Button
  $b.Text = $text; $b.Location = New-Object System.Drawing.Point((Px $x), (Px $y)); $b.Size = New-Object System.Drawing.Size((Px $w), (Px 28))
  $form.Controls.Add($b); return $b
}
function New-Num($x, $y, $min, $max, $val, $w = 70) {
  $n = New-Object System.Windows.Forms.NumericUpDown
  $n.Location = New-Object System.Drawing.Point((Px $x), (Px $y)); $n.Size = New-Object System.Drawing.Size((Px $w), (Px 26))
  $n.Minimum = $min; $n.Maximum = $max; $n.Value = $val; $form.Controls.Add($n); return $n
}
function New-Combo($x, $y, $w) {
  $c = New-Object System.Windows.Forms.ComboBox
  $c.Location = New-Object System.Drawing.Point((Px $x), (Px $y)); $c.Size = New-Object System.Drawing.Size((Px $w), (Px 26))
  $form.Controls.Add($c); return $c
}

[void](New-Label 'Record:' 15 15 62)
$rbScreen = New-Object System.Windows.Forms.RadioButton; $rbScreen.Text = 'Whole screen'; $rbScreen.Checked = $true
$rbWin = New-Object System.Windows.Forms.RadioButton; $rbWin.Text = 'One window'
$rbReg = New-Object System.Windows.Forms.RadioButton; $rbReg.Text = 'Region'
$rbCam = New-Object System.Windows.Forms.RadioButton; $rbCam.Text = 'Webcam'
$rbX = 80
foreach ($rb in @($rbScreen, $rbWin, $rbReg, $rbCam)) {
  $rb.Location = New-Object System.Drawing.Point((Px $rbX), (Px 15)); $rb.Size = New-Object System.Drawing.Size((Px 112), (Px 24))
  $form.Controls.Add($rb); $rbX += 116
}

[void](New-Label 'Window:' 15 58 62)
$cbWin = New-Combo 80 58 375
$btnWinRef = New-Button 'Refresh' 465 56 80

[void](New-Label 'Region:' 15 98 62)
[void](New-Label 'X' 80 98 16);  $numX = New-Num 98 98 -10000 20000 0 72
[void](New-Label 'Y' 180 98 16); $numY = New-Num 198 98 -10000 20000 0 72
[void](New-Label 'W' 280 98 20); $numW = New-Num 302 98 2 20000 1280 72
[void](New-Label 'H' 382 98 20); $numH = New-Num 404 98 2 20000 720 72
$btnPick = New-Button 'Select area with the mouse' 80 132 230

[void](New-Label 'Webcam:' 15 176 62)
$cbCam = New-Combo 80 176 375
$btnCamRef = New-Button 'Refresh' 465 174 80

[void](New-Label 'Frame rate' 15 220 75);   $numFps = New-Num 95 220 1 120 30 60
[void](New-Label 'Stop after (s)' 175 220 95); $numLen = New-Num 272 220 0 86400 0 70
[void](New-Label '0 = press Stop' 352 220 120)
[void](New-Label 'Start delay (s)' 15 256 95); $numDelay = New-Num 112 256 0 60 3 60
[void](New-Label 'Time to arrange windows first.' 182 256 330)

$chkMic = New-Object System.Windows.Forms.CheckBox
$chkMic.Text = 'Microphone'; $chkMic.Location = New-Object System.Drawing.Point((Px 15), (Px 296)); $chkMic.Size = New-Object System.Drawing.Size((Px 100), (Px 24))
$form.Controls.Add($chkMic)
$cbMic = New-Combo 120 296 335
$btnMicRef = New-Button 'Refresh' 465 294 80

$chkSys = New-Object System.Windows.Forms.CheckBox
$chkSys.Text = 'System sound (what the PC plays)'; $chkSys.Location = New-Object System.Drawing.Point((Px 15), (Px 336)); $chkSys.Size = New-Object System.Drawing.Size((Px 285), (Px 24))
$form.Controls.Add($chkSys)
[void](New-Label 'Boost (dB)' 305 336 75); $numBoost = New-Num 382 336 0 30 0 60

[void](New-Label 'Save to:' 15 376 62)
$txtOut = New-Object System.Windows.Forms.TextBox
$txtOut.Location = New-Object System.Drawing.Point((Px 80), (Px 376)); $txtOut.Size = New-Object System.Drawing.Size((Px 375), (Px 26))
$txtOut.Text = Join-Path $env:USERPROFILE 'Videos\Captures'
$form.Controls.Add($txtOut)
$btnBrowse = New-Button 'Browse...' 465 374 80

$chkOpen = New-Object System.Windows.Forms.CheckBox
$chkOpen.Text = 'Show the save folder when finished'; $chkOpen.Checked = $true
$chkOpen.Location = New-Object System.Drawing.Point((Px 15), (Px 412)); $chkOpen.Size = New-Object System.Drawing.Size((Px 285), (Px 24))
$form.Controls.Add($chkOpen)
$chkClip = New-Object System.Windows.Forms.CheckBox
$chkClip.Text = 'Copy pictures to clipboard'; $chkClip.Checked = $true
$chkClip.Location = New-Object System.Drawing.Point((Px 305), (Px 412)); $chkClip.Size = New-Object System.Drawing.Size((Px 245), (Px 24))
$form.Controls.Add($chkClip)

$btnStart = New-Button 'Start video recording' 15 450 165
$btnStop = New-Button 'Stop' 185 450 60
$btnStop.Enabled = $false
$btnFolder = New-Button 'Open folder' 250 450 100
$btnPic = New-Button 'Take picture' 355 450 100
$btnInstall = New-Button 'Install ffmpeg' 458 450 100

$lblStatus = New-Label '' 15 488 530
$lblStatus.TextAlign = 'MiddleLeft'
if ($script:ff) { $lblStatus.Text = 'Ready.' } else { $lblStatus.Text = 'ffmpeg not found.' }

# ---------- UI logic ----------
function Update-Enabled {
  $idle = ($script:state -eq 'idle')
  $cbWin.Enabled = $rbWin.Checked -and $idle; $btnWinRef.Enabled = $rbWin.Checked -and $idle
  foreach ($n in @($numX, $numY, $numW, $numH)) { $n.Enabled = $rbReg.Checked -and $idle }
  $btnPick.Enabled = $rbReg.Checked -and $idle
  $cbCam.Enabled = $rbCam.Checked -and $idle; $btnCamRef.Enabled = $rbCam.Checked -and $idle
  $cbMic.Enabled = $chkMic.Checked -and $idle; $btnMicRef.Enabled = $chkMic.Checked -and $idle
  $btnStart.Enabled = $idle -and [bool]$script:ff
  $btnPic.Enabled = $idle -and [bool]$script:ff
  $btnStop.Enabled = -not $idle
  $btnInstall.Visible = (-not $script:ff)
  $btnInstall.Enabled = $idle
  foreach ($c in @($rbScreen, $rbWin, $rbReg, $rbCam, $numFps, $numLen, $numDelay, $chkMic, $chkSys, $numBoost, $txtOut, $btnBrowse)) { $c.Enabled = $idle }
}

function Load-Windows {
  $cbWin.Items.Clear()
  foreach ($t in (Get-WindowTitles)) { [void]$cbWin.Items.Add($t) }
  if ($cbWin.Items.Count -gt 0 -and $cbWin.SelectedIndex -lt 0) { $cbWin.SelectedIndex = 0 }
}
function Load-Devices {
  $form.Cursor = 'WaitCursor'
  $d = Get-Devices
  $cbCam.Items.Clear(); foreach ($n in $d.video) { [void]$cbCam.Items.Add($n) }
  $cbMic.Items.Clear(); foreach ($n in $d.audio) { [void]$cbMic.Items.Add($n) }
  if ($cbCam.Items.Count -gt 0) { $cbCam.SelectedIndex = 0 }
  if ($cbMic.Items.Count -gt 0) { $cbMic.SelectedIndex = 0 }
  if ($d.audio.Count -eq 0) {
    $lblStatus.Text = 'No microphone found (system sound can still be recorded).'
  }
  $form.Cursor = 'Default'
}

function Install-Ffmpeg {
  $ans = [System.Windows.Forms.MessageBox]::Show("ffmpeg is the recorder Quick Capture needs, and it was not found on this PC.`r`n`r`nDownload and install it now? (about 100 MB, free, from the official Gyan.FFmpeg build via winget, or from gyan.dev if winget is missing)", 'Quick Capture - install ffmpeg', 'YesNo', 'Question')
  if ($ans -ne 'Yes') { $lblStatus.Text = 'ffmpeg not installed. Click Install ffmpeg when you are ready.'; return }
  $form.Cursor = 'WaitCursor'
  $btnStart.Enabled = $false; $btnInstall.Enabled = $false
  $done = $false
  $wg = Get-Command winget -ErrorAction SilentlyContinue
  if ($wg) {
    $lblStatus.Text = 'Installing ffmpeg with winget (a progress window opens)...'
    [System.Windows.Forms.Application]::DoEvents()
    try {
      $pr = Start-Process -FilePath $wg.Source -ArgumentList 'install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements' -PassThru
      while (-not $pr.HasExited) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 200 }
    } catch {}
    $script:ff = Find-Ffmpeg
    if ($script:ff) { $done = $true }
  }
  if (-not $done) {
    $dir = Join-Path $env:LOCALAPPDATA 'QuickCapture'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $zip = Join-Path $dir 'ffmpeg.zip'
    $url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    $lblStatus.Text = 'Downloading ffmpeg...'
    $job = Start-Job -ScriptBlock {
      param($u, $z)
      [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
      $ProgressPreference = 'SilentlyContinue'
      Invoke-WebRequest -Uri $u -OutFile $z -UseBasicParsing
    } -ArgumentList $url, $zip
    while ($job.State -eq 'Running') {
      [System.Windows.Forms.Application]::DoEvents()
      if (Test-Path $zip) { $lblStatus.Text = ('Downloading ffmpeg... {0:N0} MB' -f ((Get-Item $zip).Length / 1MB)) }
      Start-Sleep -Milliseconds 300
    }
    $ok = ($job.State -eq 'Completed')
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    if ($ok -and (Test-Path $zip)) {
      $lblStatus.Text = 'Unpacking ffmpeg...'
      [System.Windows.Forms.Application]::DoEvents()
      try { Expand-Archive -Path $zip -DestinationPath (Join-Path $dir 'ffmpeg') -Force } catch {}
      Remove-Item $zip -Force -ErrorAction SilentlyContinue
      $script:ff = Find-Ffmpeg
    }
  }
  $form.Cursor = 'Default'
  if ($script:ff) {
    $lblStatus.Text = 'ffmpeg installed: ' + $script:ff
    Load-Devices
  } else {
    $lblStatus.Text = 'Install failed. Check your internet connection and try again.'
    [void][System.Windows.Forms.MessageBox]::Show('Could not install ffmpeg automatically. Check your internet connection and click Install ffmpeg to try again, or install it yourself (winget install Gyan.FFmpeg).', 'Quick Capture')
  }
  Update-Enabled
}

function Pick-Region {
  $form.Hide()
  Start-Sleep -Milliseconds 400
  $script:pickStart = $null
  $script:pickRes = $null
  $script:ov = New-Object System.Windows.Forms.Form
  $script:ov.FormBorderStyle = 'None'
  $script:ov.StartPosition = 'Manual'
  $script:ov.ShowInTaskbar = $false
  $script:ov.TopMost = $true
  $script:ov.BackColor = [System.Drawing.Color]::Black
  $script:ov.Opacity = 0.4
  $script:ov.Cursor = [System.Windows.Forms.Cursors]::Cross
  $script:ov.KeyPreview = $true
  $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $script:ov.Bounds = $vs
  $script:box = New-Object System.Windows.Forms.Panel
  $script:box.BackColor = [System.Drawing.Color]::White
  $script:box.Visible = $false
  $script:ov.Controls.Add($script:box)
  $script:ov.Add_Paint({
    $f = New-Object System.Drawing.Font('Segoe UI', 20)
    $_.Graphics.DrawString('Drag with the mouse to select the area to record.  Esc = cancel', $f, [System.Drawing.Brushes]::White, 40, 40)
  })
  $script:ov.Add_KeyDown({ if ($_.KeyCode -eq 'Escape') { $script:ov.Close() } })
  $script:ov.Add_MouseDown({
    $script:pickStart = [System.Windows.Forms.Cursor]::Position
  })
  $script:ov.Add_MouseMove({
    if ($script:pickStart) {
      $c = [System.Windows.Forms.Cursor]::Position
      $x0 = [math]::Min($script:pickStart.X, $c.X); $y0 = [math]::Min($script:pickStart.Y, $c.Y)
      $w0 = [math]::Abs($c.X - $script:pickStart.X); $h0 = [math]::Abs($c.Y - $script:pickStart.Y)
      $script:box.Location = $script:ov.PointToClient((New-Object System.Drawing.Point($x0, $y0)))
      $script:box.Size = New-Object System.Drawing.Size($w0, $h0)
      $script:box.Visible = $true
    }
  })
  $script:ov.Add_MouseUp({
    if ($script:pickStart) {
      $c = [System.Windows.Forms.Cursor]::Position
      $script:pickRes = @{
        X = [math]::Min($script:pickStart.X, $c.X); Y = [math]::Min($script:pickStart.Y, $c.Y)
        W = [math]::Abs($c.X - $script:pickStart.X); H = [math]::Abs($c.Y - $script:pickStart.Y)
      }
      $script:ov.Close()
    }
  })
  [void]$script:ov.ShowDialog()
  $form.Show()
  $form.Activate()
  $r = $script:pickRes
  if ($r -and $r.W -ge 2 -and $r.H -ge 2) {
    $w = $r.W; $h = $r.H
    if ($w % 2) { $w++ }
    if ($h % 2) { $h++ }
    $numX.Value = [math]::Max($numX.Minimum, [math]::Min($numX.Maximum, $r.X))
    $numY.Value = [math]::Max($numY.Minimum, [math]::Min($numY.Maximum, $r.Y))
    $numW.Value = [math]::Min($numW.Maximum, $w)
    $numH.Value = [math]::Min($numH.Maximum, $h)
    $lblStatus.Text = "Area selected: $($r.X), $($r.Y)  $w x $h"
  }
}

function Stop-FfmpegHard($proc) {
  # end ffmpeg (and the cmd wrapper) directly; a .mkv survives being cut off
  try {
    if ($proc) {
      Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
  } catch {}
  try {
    if ($script:mkv) {
      Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script:mkv) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
  } catch {}
  try { if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } } catch {}
}

function Find-OldRecordings {
  try {
    Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine -like '*QuickCapture*capture_*' }
  } catch { @() }
}

function Show-InExplorer([string]$target) {
  # focus the Explorer window for this folder if it is already open, otherwise open it
  if (-not $target) { return }
  $isFile = Test-Path -LiteralPath $target -PathType Leaf
  if ($isFile) { $dir = Split-Path $target -Parent } else { $dir = $target }
  $dir = $dir.TrimEnd('\')
  $win = $null
  try {
    $sh = New-Object -ComObject Shell.Application
    foreach ($w in $sh.Windows()) {
      try {
        $pth = $w.Document.Folder.Self.Path
        if ($pth -and ($pth.TrimEnd('\') -ieq $dir)) { $win = $w; break }
      } catch {}
    }
  } catch {}
  if ($win) {
    if ($isFile) {
      try {
        $it = $win.Document.Folder.ParseName((Split-Path $target -Leaf))
        if ($it) { $win.Document.SelectItem($it, 29) }
      } catch {}
    }
    try {
      $h = [IntPtr][int64]$win.HWND
      if ([QCWin]::IsIconic($h)) { [QCWin]::ShowWindow($h, 9) | Out-Null }
      [QCWin]::SetForegroundWindow($h) | Out-Null
    } catch {}
    return
  }
  if ($isFile) { Start-Process explorer.exe -ArgumentList ('/select,"' + $target + '"') }
  else { Start-Process explorer.exe -ArgumentList ('"' + $target + '"') }
}

function Take-Picture {
  if (-not $script:ff) { return }
  if ($rbCam.Checked) { [void][System.Windows.Forms.MessageBox]::Show('Pictures can be taken of Whole screen, One window or Region. Choose one of those first.', 'Quick Capture'); return }
  if ($rbWin.Checked -and -not $cbWin.Text) { [void][System.Windows.Forms.MessageBox]::Show('Choose a window first.', 'Quick Capture'); return }
  $dir = $txtOut.Text.Trim()
  try { [void][System.IO.Directory]::CreateDirectory($dir) }
  catch { [void][System.Windows.Forms.MessageBox]::Show("Cannot use the save folder:`r`n$dir`r`n`r`n$($_.Exception.Message)", 'Quick Capture'); return }
  $scr = [System.Windows.Forms.Screen]::FromControl($form).Bounds   # the screen this window is on
  $script:state = 'picture'
  Update-Enabled
  $form.WindowState = 'Minimized'
  $end = (Get-Date).AddMilliseconds(500)   # just long enough for this window to get out of the way
  while ((Get-Date) -lt $end) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
  if ($rbScreen.Checked) {
    $rx = $scr.X; $ry = $scr.Y; $rw = $scr.Width; $rh = $scr.Height
  } elseif ($rbWin.Checked) {
    $r = Get-WindowRect $cbWin.Text
    if (-not $r) { Reset-Idle 'Could not find that window. Refresh the list and try again.'; return }
    $rx = $r.X; $ry = $r.Y; $rw = $r.W; $rh = $r.H
  } else {
    $rx = [int]$numX.Value; $ry = [int]$numY.Value; $rw = [int]$numW.Value; $rh = [int]$numH.Value
  }
  $ts = Get-Date -Format yyyyMMdd_HHmmss
  $png = Join-Path $dir "picture_$ts.png"
  $log = Join-Path $env:TEMP 'quick_capture_gui_picture.txt'
  $grab = '-y -hide_banner -loglevel error -f gdigrab -draw_mouse 0 -framerate 1 -offset_x ' + $rx + ' -offset_y ' + $ry + ' -video_size ' + $rw + 'x' + $rh + ' -i desktop -frames:v 1 "'
  Start-Process -FilePath $script:ff -ArgumentList ($grab + $png + '"') -Wait -WindowStyle Hidden -RedirectStandardError $log
  $note = ''
  if (-not ((Test-Path -LiteralPath $png) -and ((Get-Item -LiteralPath $png).Length -gt 0))) {
    $tmp = Join-Path $env:TEMP 'QuickCapture'
    try { [void][System.IO.Directory]::CreateDirectory($tmp) } catch { $tmp = $env:TEMP }
    $png2 = Join-Path $tmp "picture_$ts.png"
    Start-Process -FilePath $script:ff -ArgumentList ($grab + $png2 + '"') -Wait -WindowStyle Hidden -RedirectStandardError $log
    if ((Test-Path -LiteralPath $png2) -and ((Get-Item -LiteralPath $png2).Length -gt 0)) {
      $png = $png2
      $note = "Windows would not let ffmpeg save into your save folder, so the picture is kept here:`r`n$png2"
    } else {
      $tail = ''
      if (Test-Path $log) { $tail = (Get-Content $log -Tail 5) -join "`r`n" }
      Reset-Idle 'Picture failed.'
      [void][System.Windows.Forms.MessageBox]::Show("The picture could not be taken.`r`n$tail", 'Quick Capture')
      return
    }
  }
  if ($chkClip.Checked) {
    try {
      $bmp = New-Object System.Drawing.Bitmap($png)
      [System.Windows.Forms.Clipboard]::SetDataObject($bmp, $true)
      $bmp.Dispose()
    } catch {}
  }
  Reset-Idle ('Picture saved: ' + $png)
  if ($note) { [void][System.Windows.Forms.MessageBox]::Show($note, 'Quick Capture') }
  if ($chkOpen.Checked) { Show-InExplorer $png }
}

function Reset-Idle([string]$msg) {
  $script:state = 'idle'
  $script:proc = $null
  $timer.Stop()
  $form.WindowState = 'Normal'
  $form.Activate()
  $lblStatus.Text = $msg
  Update-Enabled
}

function Begin-Record {
  $fps = [int]$numFps.Value
  $script:limit = [int]$numLen.Value
  $src = ''
  if ($rbScreen.Checked) {
    $src = "-f gdigrab -framerate $fps -i desktop"
  } elseif ($rbWin.Checked) {
    $r = Get-WindowRect $cbWin.Text
    if (-not $r) { Reset-Idle 'Could not find that window. Refresh the list and try again.'; return }
    $src = "-f gdigrab -framerate $fps -offset_x $($r.X) -offset_y $($r.Y) -video_size $($r.W)x$($r.H) -i desktop"
  } elseif ($rbReg.Checked) {
    $w = [int]$numW.Value; $h = [int]$numH.Value
    if ($w % 2) { $w++ }; if ($h % 2) { $h++ }
    $src = "-f gdigrab -framerate $fps -offset_x $([int]$numX.Value) -offset_y $([int]$numY.Value) -video_size ${w}x${h} -i desktop"
  } else {
    $src = "-f dshow -framerate $fps -i `"video=$($cbCam.Text)`""
  }
  $aud = ''
  if ($chkMic.Checked) { $aud += " -f dshow -i `"audio=$($cbMic.Text)`"" }
  $len = ''
  if ($script:limit -gt 0) { $len = "-t $($script:limit)" }
  $argLine = "-y -hide_banner $src $aud $len -vf `"pad=ceil(iw/2)*2:ceil(ih/2)*2`" -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p -c:a aac `"$($script:mkv)`""

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'cmd.exe'
  $psi.Arguments = '/c ""' + $script:ff + '" ' + $argLine + ' 2>"' + $script:log + '""'
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.CreateNoWindow = $true
  try { $script:proc = [System.Diagnostics.Process]::Start($psi) }
  catch { Reset-Idle ('Could not start ffmpeg: ' + $_.Exception.Message); return }
  $script:state = 'recording'
  $script:started = Get-Date
  $script:rec = $null
  if ($chkSys.Checked) {
    # wait until ffmpeg has really started capturing, so sound and picture line up
    for ($i = 0; $i -lt 50; $i++) {
      Start-Sleep -Milliseconds 100
      $txt = ''
      try { $txt = Get-Content $script:log -Raw -ErrorAction SilentlyContinue } catch {}
      if ($txt -match 'Press \[q\]' -or $script:proc.HasExited) { break }
    }
    $script:wav = [System.IO.Path]::ChangeExtension($script:mkv, 'sys.wav')
    $r = New-Object LoopRec
    $err = $r.Start($script:wav)
    if ($err) {
      $lblStatus.Text = 'System sound unavailable: ' + $err
      [void][System.Windows.Forms.MessageBox]::Show("System sound could not be recorded:`r`n$err`r`n`r`nThe video will be recorded without it.", 'Quick Capture')
      $script:wav = ''
    } else { $script:rec = $r }
  }
}

function Finish-Record {
  $timer.Stop()
  $script:state = 'idle'
  $ok = (Test-Path $script:mkv) -and ((Get-Item $script:mkv).Length -gt 0)
  if (-not $ok) {
    $tail = ''
    if (Test-Path $script:log) { $tail = (Get-Content $script:log -Tail 8) -join "`r`n" }
    $where = ''
    try { $where = (Get-ChildItem (Join-Path $env:TEMP 'QuickCapture') -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Name)  $([int]($_.Length/1KB)) KB" }) -join "`r`n" } catch {}
    $run = ''
    try { $run = "ffmpeg still running: " + [bool](Get-Process ffmpeg -ErrorAction SilentlyContinue) } catch {}
    Reset-Idle 'No file was created.'
    [void][System.Windows.Forms.MessageBox]::Show("No file was created at:`r`n$($script:mkv)`r`n`r`nffmpeg said:`r`n$tail`r`n`r`nTemp folder holds:`r`n$where`r`n$run", 'Quick Capture')
    return
  }
  if ($script:rec) { try { $script:rec.Stop() } catch {}; $script:rec = $null }
  $lblStatus.Text = 'Finishing the file...'
  $form.WindowState = 'Normal'; $form.Refresh()
  $hasWav = $script:wav -and (Test-Path $script:wav) -and ((Get-Item $script:wav).Length -gt 100)
  $tmpMp4 = [System.IO.Path]::ChangeExtension($script:mkv, 'mp4')
  $remLog = Join-Path $env:TEMP 'quick_capture_gui_mux.txt'
  function Invoke-Mux([string]$out) {
    $boost = 'volume=' + [int]$numBoost.Value + 'dB,'
    if ($hasWav -and $chkMic.Checked) {
      $rem = '-y -v error -i "' + $script:mkv + '" -i "' + $script:wav + '" -filter_complex "[1:a]' + $boost + 'alimiter=limit=0.95[s];[0:a][s]amix=inputs=2:duration=first[aout]" -map 0:v -map "[aout]" -c:v copy -c:a aac -movflags +faststart "' + $out + '"'
    } elseif ($hasWav) {
      $rem = '-y -v error -i "' + $script:mkv + '" -i "' + $script:wav + '" -map 0:v -map 1:a -af "' + $boost + 'alimiter=limit=0.95,apad" -c:v copy -c:a aac -shortest -movflags +faststart "' + $out + '"'
    } else {
      $rem = '-y -v error -i "' + $script:mkv + '" -c copy -movflags +faststart "' + $out + '"'
    }
    Start-Process -FilePath $script:ff -ArgumentList $rem -Wait -WindowStyle Hidden -RedirectStandardError $remLog
    return ((Test-Path -LiteralPath $out) -and ((Get-Item -LiteralPath $out).Length -gt 0))
  }
  $final = $script:mkv
  $note = ''
  if (Invoke-Mux $script:mp4) {
    # ffmpeg wrote straight into the chosen folder
    $final = $script:mp4
  } elseif (Invoke-Mux $tmpMp4) {
    # the chosen folder is protected (for example Windows Controlled folder access): keep the file in temp
    $final = $tmpMp4
    $note = "Windows would not let ffmpeg save into:`r`n$([System.IO.Path]::GetDirectoryName($script:mp4))`r`n`r`nYour video is kept here instead:`r`n$tmpMp4`r`n`r`nPick another folder in 'Save to', or allow ffmpeg in Windows Security > Virus and threat protection > Ransomware protection > Controlled folder access."
  } else {
    $tail = ''
    if (Test-Path $remLog) { $tail = (Get-Content $remLog -Tail 6) -join "`r`n" }
    $note = "Could not build the .mp4. The raw recording is kept (video: $($script:mkv)" + $(if ($hasWav) { "; sound: $($script:wav)" } else { '' }) + ").`r`n$tail"
  }
  if ($final -ne $script:mkv) {
    Remove-Item $script:mkv -Force -ErrorAction SilentlyContinue
    if ($hasWav) { Remove-Item $script:wav -Force -ErrorAction SilentlyContinue }
  }
  Reset-Idle ('Saved: ' + $final)
  if ($note) { [void][System.Windows.Forms.MessageBox]::Show($note, 'Quick Capture') }
  if ($chkOpen.Checked) { Show-InExplorer $final }
}

function Stop-Record {
  if ($script:state -eq 'countdown') { Reset-Idle 'Cancelled.'; return }
  if ($script:state -eq 'recording' -and $script:proc -and -not $script:proc.HasExited) {
    try { $script:proc.StandardInput.WriteLine('q'); $script:proc.StandardInput.Flush() } catch {}
    $script:state = 'stopping'
    $script:stopDeadline = (Get-Date).AddSeconds(10)
    $lblStatus.Text = 'Stopping...'
  }
}

function Start-Capture {
  if (-not $script:ff) { return }
  if ($rbWin.Checked -and -not $cbWin.Text) { [void][System.Windows.Forms.MessageBox]::Show('Choose a window first.', 'Quick Capture'); return }
  if ($rbCam.Checked -and -not $cbCam.Text) { [void][System.Windows.Forms.MessageBox]::Show('No webcam selected. Click Refresh.', 'Quick Capture'); return }
  if ($chkMic.Checked -and -not $cbMic.Text) { [void][System.Windows.Forms.MessageBox]::Show('No microphone selected. Click Refresh.', 'Quick Capture'); return }
  $dir = $txtOut.Text.Trim()
  try { [void][System.IO.Directory]::CreateDirectory($dir) }
  catch { [void][System.Windows.Forms.MessageBox]::Show("Cannot use the save folder:`r`n$dir`r`n`r`n$($_.Exception.Message)", 'Quick Capture'); return }
  if (-not (Test-Path -LiteralPath $dir -PathType Container)) { [void][System.Windows.Forms.MessageBox]::Show("The save folder does not exist and could not be created:`r`n$dir", 'Quick Capture'); return }
  $tmp = Join-Path $env:TEMP 'QuickCapture'
  try { [void][System.IO.Directory]::CreateDirectory($tmp) } catch { $tmp = $env:TEMP }
  $ts = Get-Date -Format yyyyMMdd_HHmmss
  # record into the temp folder (always writable), then finish into the chosen folder
  $script:mkv = Join-Path $tmp "capture_$ts.mkv"
  $script:mp4 = Join-Path $dir "capture_$ts.mp4"
  $script:log = Join-Path $env:TEMP 'quick_capture_gui_log.txt'
  $script:state = 'countdown'
  $script:until = (Get-Date).AddSeconds([int]$numDelay.Value)
  Update-Enabled
  $lblStatus.Text = 'Starting...'
  $form.WindowState = 'Minimized'
  $timer.Start()
}

# ---------- timer ----------
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 400
$timer.Add_Tick({
  try {
  switch ($script:state) {
    'countdown' { if ((Get-Date) -ge $script:until) { Begin-Record } }
    'recording' {
      if ($script:proc.HasExited) { Finish-Record; return }
      $el = [int]((Get-Date) - $script:started).TotalSeconds
      $t = '{0:00}:{1:00}' -f [math]::Floor($el / 60), ($el % 60)
      if ($script:limit -gt 0) { $t += ' / ' + ('{0:00}:{1:00}' -f [math]::Floor($script:limit / 60), ($script:limit % 60)) }
      $lblStatus.Text = "Recording $t - restore this window from the taskbar to Stop."
    }
    'stopping' {
      if ($script:proc.HasExited) { Finish-Record }
      elseif ((Get-Date) -ge $script:stopDeadline) {
        Stop-FfmpegHard $script:proc
        Start-Sleep -Milliseconds 500
        Finish-Record
      }
    }
  }
  } catch { Reset-Idle ('Error: ' + $_.Exception.Message) }
})

# ---------- events ----------
foreach ($rb in @($rbScreen, $rbWin, $rbReg, $rbCam)) { $rb.Add_CheckedChanged({ Update-Enabled }) }
$chkMic.Add_CheckedChanged({ Update-Enabled })
$btnWinRef.Add_Click({ Load-Windows })
$btnCamRef.Add_Click({ Load-Devices })
$btnMicRef.Add_Click({ Load-Devices })
$btnBrowse.Add_Click({
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.SelectedPath = $txtOut.Text
  if ($d.ShowDialog() -eq 'OK') { $txtOut.Text = $d.SelectedPath }
})
$btnFolder.Add_Click({
  if (-not (Test-Path $txtOut.Text)) { New-Item -ItemType Directory -Path $txtOut.Text -Force | Out-Null }
  Show-InExplorer $txtOut.Text
})
$btnStart.Add_Click({ Start-Capture })
$btnInstall.Add_Click({ Install-Ffmpeg })
$btnPic.Add_Click({ Take-Picture })
$btnPick.Add_Click({ Pick-Region })
$btnStop.Add_Click({ Stop-Record })
$form.Add_FormClosing({
  if ($script:rec) { try { $script:rec.Stop() } catch {} }
  if ($script:proc -and -not $script:proc.HasExited) {
    try { $script:proc.StandardInput.WriteLine('q') } catch {}
    Start-Sleep -Milliseconds 1500
    if (-not $script:proc.HasExited) { Stop-FfmpegHard $script:proc }
  }
})
$form.Add_Shown({
  Load-Windows
  Update-Enabled
  if (-not $script:ff) { Install-Ffmpeg } else { Load-Devices }
  Update-Enabled
  $old = @(Find-OldRecordings)
  if ($old.Count -gt 0) {
    $a = [System.Windows.Forms.MessageBox]::Show("A previous Quick Capture recording is still running in the background (ffmpeg.exe). End it now?", 'Quick Capture', 'YesNo', 'Question')
    if ($a -eq 'Yes') {
      foreach ($o in $old) { Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue }
      $lblStatus.Text = 'Ended the old recording. Its video is in %TEMP%\QuickCapture.'
    }
  }
})

Update-Enabled
[void]$form.ShowDialog()
