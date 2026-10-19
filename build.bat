@echo off
REM Local build: makes dist\OM-Video-Studio\ (portable folder) and, if Inno Setup is installed, Output\OM-Video-Studio-Setup.exe
REM Needs Python 3.10-3.13 on PATH.  Usage: build.bat [version]   e.g. build.bat 1.0
setlocal
set VER=%~1
if "%VER%"=="" set VER=1.0
python -m pip install --upgrade pip || goto :err
python -m pip install -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --clean --windowed --name OM-Video-Studio --icon app.ico ^
  --add-data "app.ico;." --add-data "Resource;Resource" ^
  --collect-all sv_ttk --collect-all tkinterdnd2 --collect-all pywinstyles --collect-all certifi --hidden-import om_translations om_video_studio.py || goto :err
where iscc >nul 2>nul && (iscc /DAppVersion=%VER% installer\setup.iss) || echo Inno Setup (iscc) not found - skipping installer. Get it from https://jrsoftware.org/isinfo.php
echo.
echo Done. Portable app: dist\OM-Video-Studio\OM-Video-Studio.exe
exit /b 0
:err
echo BUILD FAILED
exit /b 1
