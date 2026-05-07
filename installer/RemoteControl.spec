# PyInstaller spec for RemoteControl
#
# Builds a single onefile Windows exe that contains both host and client
# behavior. Role is selected at launch via the role picker (or via CLI:
#   RemoteControl.exe host
#   RemoteControl.exe client
# ).
#
# Usage:
#   pyinstaller --clean installer/RemoteControl.spec
#
# The result is dist/RemoteControl/RemoteControl.exe (--onedir) which is
# what the Inno Setup installer wraps.

# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH).parent

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Belt + braces - these come in via av/PyQt6/dxcam transitively but
        # listing them ensures PyInstaller pulls all sub-modules.
        "av",
        "av.audio", "av.video", "av.video.frame", "av.codec", "av.container",
        "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets",
        "cryptography", "cryptography.hazmat",
        "dxcam",
        "numpy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim noisy bundles; comment out if a runtime ImportError appears.
        "tkinter", "matplotlib", "scipy", "pandas", "PIL.ImageQt",
        "IPython", "notebook", "pytest", "pytest_asyncio",
    ],
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
    name="RemoteControl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # GUI app - no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                     # add custom .ico later
    version=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RemoteControl",
)
