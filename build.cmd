@echo off
rem Rebuild dist\yt-dlp-gui-<version>.exe from source.
rem Double-click it, or run "build.cmd" from a terminal in this folder.
rem   build.cmd           pull the latest code, update the build environment, test, build
rem   build.cmd nopause   same, but do not wait for a key at the end
rem   set YTDLP_NO_PULL=1 build without pulling (for testing local changes)

setlocal
cd /d "%~dp0"

rem -- Get the latest version first. git may rewrite this very script, and cmd
rem    reads a running script line by line from a byte offset, so the pull and a
rem    restart of the updated script happen inside one block, which cmd has
rem    already read in full before any of it runs.
if not defined YTDLP_PULLED if not defined YTDLP_NO_PULL (
    set "YTDLP_PULLED=1"
    where git >nul 2>&1
    if errorlevel 1 (
        echo git was not found - building the code as it is on disk.
    ) else if not exist ".git" (
        echo This folder is not a git checkout - building the code as it is on disk.
    ) else (
        echo Pulling the latest version ...
        git pull --ff-only
        if errorlevel 1 (
            echo.
            echo WARNING: git pull failed - no network, or local changes that conflict.
            echo Building the code as it is on disk.
            echo.
        )
    )
    call "%~f0" %*
    exit /b
)

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
