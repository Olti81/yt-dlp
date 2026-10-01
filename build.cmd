@echo off
rem Rebuild dist\yt-dlp-gui-<version>.exe from source.
rem Double-click it, or run "build.cmd" from a terminal in this folder.
rem   build.cmd           update the build environment, test, build
rem   build.cmd nopause   same, but do not wait for a key at the end

setlocal
cd /d "%~dp0"

rem -- Python: the py launcher picks 3.13 if it is installed, else plain python
set "PY="
py -3.13 -c "pass" >nul 2>&1 && set "PY=py -3.13"
if not defined PY (
    python -c "pass" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python was not found. Install Python 3.13 from https://www.python.org/ and try again.
    goto :fail
)

rem -- The bundled binaries are not in git; the build is useless without them
if not exist "resources\yt-dlp.exe" (
    echo resources\yt-dlp.exe is missing.
    echo Create a "resources" folder next to main.py with yt-dlp.exe, ffmpeg.exe,
    echo ffprobe.exe and the ffmpeg DLLs - see "Running from source" in README.md.
    goto :fail
)
if not exist "resources\ffmpeg.exe" (
    echo resources\ffmpeg.exe is missing - merging and audio conversion would fail.
    goto :fail
)

rem -- A private virtual environment, so PyInstaller does not touch your global Python
if not exist ".venv\Scripts\python.exe" (
    echo Creating build environment in .venv ...
    %PY% -m venv .venv || goto :fail
)
echo Updating PyInstaller ...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip pyinstaller || goto :fail

echo Running tests ...
".venv\Scripts\python.exe" -m unittest discover -s tests -q || goto :fail

echo Building ...
".venv\Scripts\python.exe" -m PyInstaller main.spec --noconfirm --clean || goto :fail

echo.
echo Done:
for %%F in (dist\yt-dlp-gui-*.exe) do echo   %%~fF   (%%~zF bytes)
if /i not "%~1"=="nopause" pause
exit /b 0

:fail
echo.
echo Build FAILED.
if /i not "%~1"=="nopause" pause
exit /b 1
