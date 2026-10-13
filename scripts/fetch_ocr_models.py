"""Build-time-only download of pinned official Korean PP-OCRv5 ONNX models.

The shipped application never imports or calls this script. No model download is
performed at runtime. SHA256 values originate in RapidAI's default_models.yaml.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "assets" / "ocr"
BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/"
MODELS = {
    "ch_PP-OCRv5_det_mobile.onnx": {
        "url": BASE + "det/ch_PP-OCRv5_det_mobile.onnx",
        "sha256": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    },
    "korean_PP-OCRv5_rec_mobile.onnx": {
        "url": BASE + "rec/korean_PP-OCRv5_rec_mobile.onnx",
        "sha256": "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
    },
}


def fetch(item):
    filename, info = item
    path = DEST / filename
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]:
        print(f"Already verified: {filename}", flush=True)
        return
    request = urllib.request.Request(info["url"], headers={"User-Agent": "Alimi-build/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != info["sha256"]:
        raise RuntimeError(f"Model checksum mismatch: {filename}: {actual}")
    partial = path.with_suffix(".download")
    partial.write_bytes(data)
    partial.replace(path)
    print(f"Verified: {filename}, {len(data):,} bytes", flush=True)


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fetch, MODELS.items()))
    manifest = {
        "source": "https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml",
        "origin": "PaddlePaddle/PaddleOCR PP-OCRv5",
        "license": "Apache-2.0",
        "runtime_network": False,
        "models": MODELS,
    }
    (DEST / "models.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
