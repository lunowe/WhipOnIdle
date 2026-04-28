# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for WhipOnIdle. Works on macOS (produces WhipOnIdle.app)
# and Windows (produces dist/WhipOnIdle/WhipOnIdle.exe).
#
# Build:
#   pyinstaller whip_app.spec --clean --noconfirm

import sys

block_cipher = None

a = Analysis(
    ['whip_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('universfield-whip-06-487886.mp3', '.'),
        ('whip.wav', '.'),
    ],
    hiddenimports=[
        # pystray's backend modules are imported lazily, which PyInstaller
        # sometimes misses. List them explicitly per platform so the bundle
        # is self-contained.
        'pystray._darwin' if sys.platform == 'darwin' else 'pystray._win32',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL._tkinter_finder',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WhipOnIdle',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WhipOnIdle',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='WhipOnIdle.app',
        icon=None,
        bundle_identifier='de.versicherungsforen.whipidle',
        info_plist={
            'NSHighResolutionCapable': True,
            'LSUIElement': True,             # menu-bar-only (no Dock icon)
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'NSHumanReadableCopyright': '© 2026 — WhipOnIdle',
        },
    )
