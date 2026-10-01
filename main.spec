# -*- mode: python ; coding: utf-8 -*-
#
# Build:  .venv\Scripts\pyinstaller.exe main.spec --noconfirm
# Output: dist/yt-dlp-gui-2.1.exe   (single file, no console window)
#
# `resources` is bundled whole: yt-dlp.exe, ffmpeg/ffprobe and the av* DLLs.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('resources', 'resources'), ('assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'PIL', 'pytest', 'pydoc_data', 'test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='yt-dlp-gui-2.1',
    icon='assets/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
