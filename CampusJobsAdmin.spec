import os
import sys

sys.path.insert(0, os.getcwd())

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pyinstaller_tk import collect_tkinter

datas = []
binaries = []
tkinter_datas, tkinter_binaries, tkinter_hiddenimports = collect_tkinter()
hiddenimports = tkinter_hiddenimports
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
datas += tkinter_datas
binaries += tkinter_binaries


a = Analysis(
    ['scripts\\_entry_admin.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['pyinstaller_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CampusJobsAdmin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CampusJobsAdmin',
)
