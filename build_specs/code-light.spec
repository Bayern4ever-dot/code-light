# -*- mode: python ; coding: utf-8 -*-
import os
import sys


# Use CONDA_PREFIX from environment, or fall back to sys.prefix
_conda_prefix = os.environ.get('CONDA_PREFIX', sys.prefix)
_library_bin = os.path.join(_conda_prefix, 'Library', 'bin')

# SPECPATH = directory containing this .spec file
# Project root is one level up from build_specs/
_project_root = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, '..', 'packaging', 'pyinstaller_entry.py')],
    pathex=[os.path.join(_project_root, 'src')],
    binaries=[
        (os.path.join(_library_bin, 'libssl-3-x64.dll'), '.'),
        (os.path.join(_library_bin, 'libcrypto-3-x64.dll'), '.'),
    ],
    datas=[(os.path.join(_project_root, 'dashboard'), 'dashboard')],
    hiddenimports=['win32timezone'],
    hookspath=[],
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
    name='code-light',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(_project_root, 'packaging', 'cl-icon.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='code-light',
)
