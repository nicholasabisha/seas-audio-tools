# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Seas Audio Tools (windowed, one folder).

Build with:
    python -m PyInstaller build\seas-audio.spec --noconfirm

Output lands in build\dist\SeasAudioTools\.

The tkinterdnd2 Tcl binaries are picked up by PyInstaller's contrib hook
(hook-tkinterdnd2.py), and fpcalc.exe is bundled next to the executable so the
resolver in core/fpcalc.py finds it through sys._MEIPASS.
"""

from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parent
SOURCE = PROJECT / "src"
NAME = "SeasAudioTools"

analysis = Analysis(
    [str(SOURCE / "seas_audio" / "ui" / "app.py")],
    pathex=[str(SOURCE)],
    binaries=[(str(PROJECT / "bin" / "fpcalc.exe"), ".")],
    datas=[(str(PROJECT / "assets" / "icon.ico"), ".")],
    hiddenimports=["tkinterdnd2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pandas, scipy and friends are not used by this app; excluding them keeps
    # the folder roughly a third of the size.
    excludes=[
        "pandas",
        "scipy",
        "matplotlib",
        "IPython",
        "geopandas",
        "pytest",
        "setuptools",
        "pip",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(PROJECT / "assets" / "icon.ico"),
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)