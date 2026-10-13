# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = []
binaries += collect_dynamic_libs('onnxruntime')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=[('assets/ocr', 'assets/ocr'), ('assets/licenses', 'assets/licenses'), ('LICENSE', '.'), ('THIRD_PARTY_NOTICES.txt', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['scripts/rthook_ort.py'],
    excludes=['onnxruntime.tools', 'onnxruntime.quantization', 'numba', 'llvmlite', 'matplotlib', 'scipy', 'pandas', 'torch', 'tensorflow', 'fitz', 'PyQt6', 'PySide6', 'PySide2', 'pytest', 'IPython'],
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
    name='SchoolAlimi',
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
    icon=['assets\\alimi.ico'],
)
