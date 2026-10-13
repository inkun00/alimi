$ErrorActionPreference = 'Stop'
$alimiRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $alimiRoot
python -m PyInstaller --noconfirm --onefile --windowed --name SchoolAlimi --icon assets/alimi.ico --runtime-hook scripts/rthook_ort.py --add-data 'assets/ocr;assets/ocr' --add-data 'assets/licenses;assets/licenses' --add-data 'LICENSE;.' --add-data 'THIRD_PARTY_NOTICES.txt;.' --collect-binaries onnxruntime --exclude-module onnxruntime.tools --exclude-module onnxruntime.quantization --exclude-module numba --exclude-module llvmlite --exclude-module matplotlib --exclude-module scipy --exclude-module pandas --exclude-module torch --exclude-module tensorflow --exclude-module fitz --exclude-module PyQt6 --exclude-module PySide6 --exclude-module PySide2 --exclude-module pytest --exclude-module IPython main.py
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
