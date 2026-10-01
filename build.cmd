@echo off
rem Rebuild the app as a single .exe in dist\.
rem Double-click it, or run it from a terminal in this folder.
rem   build.cmd            pull the latest code, test, build the current version
rem   build.cmd 2.0        build an older release: a git tag such as v2.0, or a commit id
rem                        (2.0 is commit 450382c - "build.cmd 450382c" works without the tag)
rem   build.cmd nopause    do not wait for a key at the end (combines with a version)
rem   set YTDLP_NO_PULL=1  build without pulling first (for testing local changes)
rem
rem Builds are never deleted: if dist\ already has an .exe of the version being
rem built, it is moved to dist\previous\ with a timestamp first.

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

rem -- Arguments: "nopause" and/or a version to build
set "NOPAUSE="
set "WANT="
for %%A in (%*) do (
    if /i "%%~A"=="nopause" (
        set "NOPAUSE=1"
    ) else (
        set "WANT=%%~A"
    )
)
rem    "2.0" means the tag v2.0; a commit id such as 450382c works too
set "WANT_RAW=%WANT%"
if defined WANT if /i not "%WANT:~0,1%"=="v" set "WANT=v%WANT%"

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

rem -- Build in a fresh folder in the temp directory. PyInstaller has to delete its old
rem    work folder first, and Windows refuses while anything still has a file
rem    open in it - antivirus scanning new files, an Explorer window, the
rem    indexer - which fails the whole build with "Access is denied".
set "WORK=%TEMP%\yt-dlp-gui-build"
if exist "%WORK%" rmdir /s /q "%WORK%" >nul 2>&1
if exist "%WORK%" set "WORK=%TEMP%\yt-dlp-gui-build-%RANDOM%%RANDOM%"
rem    The work folder older versions of this script left in the project
if exist "build" rmdir /s /q "build" >nul 2>&1

if defined WANT goto :build_release

rem ---------------------------------------------------------------- current
echo Running tests ...
".venv\Scripts\python.exe" -m unittest discover -s tests -q || goto :fail

call :exe_name "main.spec" || goto :fail
call :keep_old_build || goto :fail

echo Building %EXENAME% ...
".venv\Scripts\python.exe" -m PyInstaller main.spec --noconfirm --workpath "%WORK%" || goto :fail
goto :done

rem ---------------------------------------------------------------- release
rem   The release's files are exported from git into a temp folder (plus a
rem   copy of resources\), built there into this folder's dist\, and the temp
rem   folder is removed again. Your checkout is not touched.
:build_release
git fetch --tags --quiet >nul 2>&1
git rev-parse --verify --quiet "%WANT%^{commit}" >nul
if errorlevel 1 set "WANT=%WANT_RAW%"
git rev-parse --verify --quiet "%WANT%^{commit}" >nul
if errorlevel 1 (
    echo There is no version %WANT%. Versions you can build:
    git tag -l "v*"
    goto :fail
)
set "SRC=%TEMP%\yt-dlp-gui-src-%WANT%"
if exist "%SRC%" rmdir /s /q "%SRC%" >nul 2>&1
if exist "%SRC%" set "SRC=%TEMP%\yt-dlp-gui-src-%WANT%-%RANDOM%%RANDOM%"
mkdir "%SRC%" || goto :fail
echo Exporting %WANT% ...
git archive --format=tar -o "%SRC%\source.tar" "%WANT%" || goto :fail
tar -xf "%SRC%\source.tar" -C "%SRC%" || goto :fail
del "%SRC%\source.tar"
echo Copying resources ...
robocopy "resources" "%SRC%\resources" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail

call :exe_name "%SRC%\main.spec" || goto :fail
call :keep_old_build || goto :fail

echo Building %EXENAME% ...
pushd "%SRC%"
"%~dp0.venv\Scripts\python.exe" -m PyInstaller main.spec --noconfirm --workpath "%WORK%" --distpath "%~dp0dist"
set "RC=%ERRORLEVEL%"
popd
rmdir /s /q "%SRC%" >nul 2>&1
if not "%RC%"=="0" goto :fail
goto :done

rem ---------------------------------------------------------------- end
:done
if exist "%WORK%" rmdir /s /q "%WORK%" >nul 2>&1
echo.
echo Done:
for %%F in ("dist\%EXENAME%.exe") do if exist "%%~fF" echo   %%~fF  -  %%~zF bytes
if not defined NOPAUSE pause
exit /b 0

:fail
echo.
echo Build FAILED.
if not defined NOPAUSE pause
exit /b 1

rem ---------------------------------------------------------------- helpers

rem   :exe_name <spec>  ->  EXENAME, e.g. yt-dlp-gui-2.1, from the spec's name='...'
:exe_name
set "EXENAME="
for /f "usebackq tokens=2 delims='" %%N in (`findstr /c:"name='" "%~1"`) do set "EXENAME=%%N"
if defined EXENAME exit /b 0
echo Could not find the program name in %~1
exit /b 1

rem   :keep_old_build  ->  moves an existing dist\EXENAME.exe to dist\previous\
rem   with a timestamp, so a new build never destroys an older one. If it cannot
rem   be moved, the app is still running.
:keep_old_build
if not exist "dist\%EXENAME%.exe" exit /b 0
if not exist "dist\previous" mkdir "dist\previous"
set "STAMP="
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"
if not defined STAMP set "STAMP=%RANDOM%%RANDOM%"
move "dist\%EXENAME%.exe" "dist\previous\%EXENAME%-%STAMP%.exe" >nul 2>&1
if exist "dist\%EXENAME%.exe" (
    echo dist\%EXENAME%.exe is in use. Close the app and run the build again.
    exit /b 1
)
echo Kept the previous build as dist\previous\%EXENAME%-%STAMP%.exe
exit /b 0
