"""Package the executable, instructions, licenses and corresponding source."""
from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from alimi import __version__
version = __version__
dist = root / "dist"
exe = dist / "SchoolAlimi.exe"
if not exe.is_file():
    raise SystemExit("Build dist/SchoolAlimi.exe first")

source_zip = dist / f"SchoolAlimi-{version}-source.zip"
source_files = [root / name for name in ("main.py", "README.txt", "requirements.txt", "requirements-ocr.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt", "SchoolAlimi.spec")]
for folder in ("alimi", "scripts", "tests", "assets"):
    source_files.extend(p for p in (root / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                        and not (folder == "tests" and any(part.startswith("tmp") for part in p.relative_to(root / folder).parts)))
with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in sorted(set(source_files)):
        if path.is_file():
            archive.write(path, "SchoolAlimi-source/" + path.relative_to(root).as_posix())

release = dist / f"SchoolAlimi-{version}-Windows-x64.zip"
with zipfile.ZipFile(release, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
    for path in (exe, source_zip, root / "README.txt", root / "LICENSE", root / "THIRD_PARTY_NOTICES.txt"):
        archive.write(path, path.name)
    for path in sorted((root / "assets" / "licenses").rglob("*")):
        if path.is_file():
            archive.write(path, "licenses/" + path.relative_to(root / "assets" / "licenses").as_posix())
    for path in sorted((root / "assets" / "ocr" / "licenses").rglob("*")):
        if path.is_file():
            archive.write(path, "licenses/ocr/" + path.name)
    archive.write(root / "assets" / "ocr" / "NOTICE.txt", "licenses/ocr/NOTICE.txt")

checksum = dist / "SHA256SUMS.txt"
checksum.write_text("\n".join(f"{hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()}  {path.name}" for path in (exe, source_zip, release)) + "\n", encoding="utf-8")
for path in (exe, release, source_zip, checksum):
    print(f"{path.name}: {path.stat().st_size:,} bytes")
