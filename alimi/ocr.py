"""Offline Korean OCR backed by bundled PP-OCRv5 ONNX models.

No HTTP client, subprocess or external OCR service is used. All images and
inference stay in the current Windows process. Sessions are initialized lazily
so that starting the reminder application does not load OCR models into memory.

The preprocessing / CTC model contract is based on PaddleOCR and RapidOCR
(Apache-2.0). See assets/ocr/NOTICE.txt for model provenance and licenses.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
from pathlib import Path
import sys
import threading
import unicodedata


MODELS = {
    "ch_PP-OCRv5_det_mobile.onnx": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    "korean_PP-OCRv5_rec_mobile.onnx": "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
}


class OcrEngine:
    def __init__(self, resources_path=None):
        roots = []
        if resources_path is not None:
            root = Path(resources_path)
            roots.extend((root / "assets" / "ocr", root / "ocr", root))
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            roots.append(Path(bundle) / "assets" / "ocr")
        roots.append(Path(__file__).resolve().parents[1] / "assets" / "ocr")
        self.resources_path = next(
            (root for root in roots if all((root / name).is_file() for name in MODELS)),
            roots[0],
        )
        self._detector = None
        self._recognizer = None
        self._characters = None
        self._lock = threading.Lock()
        self._error = ""

    def available(self) -> bool:
        if self._error:
            return False
        if not all((self.resources_path / name).is_file() for name in MODELS):
            return False
        return all(importlib.util.find_spec(name) is not None for name in ("onnxruntime", "numpy", "cv2"))

    def diagnostics(self) -> str:
        if self._error:
            return "한국어 문자 인식 엔진 오류: " + self._error
        if not self.available():
            return "한국어 문자 인식 모델 또는 실행 모듈을 찾을 수 없습니다. 배포 파일을 다시 확인해 주세요."
        return "오프라인 한국어 문자 인식 준비됨 · PP-OCRv5 · 모든 분석은 이 PC에서 처리됩니다."

    def _load(self):
        if self._detector is not None:
            return
        if not self.available():
            raise RuntimeError(self.diagnostics())
        import onnxruntime as ort

        for name, expected in MODELS.items():
            path = self.resources_path / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError("한국어 문자 인식 모델이 손상되었습니다: " + name)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        detector = ort.InferenceSession(
            str(self.resources_path / "ch_PP-OCRv5_det_mobile.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        recognizer = ort.InferenceSession(
            str(self.resources_path / "korean_PP-OCRv5_rec_mobile.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        dictionary = recognizer.get_modelmeta().custom_metadata_map.get("character", "")
        if not dictionary:
            raise RuntimeError("한국어 문자 인식 모델에 글자 사전이 없습니다.")
        self._characters = [""] + dictionary.splitlines() + [" "]
        self._detector = detector
        self._recognizer = recognizer

    @staticmethod
    def _ordered_quad(points):
        import numpy as np
        points = np.asarray(points, dtype=np.float32)
        by_x = points[np.argsort(points[:, 0])]
        left = by_x[:2][np.argsort(by_x[:2, 1])]
        right = by_x[2:][np.argsort(by_x[2:, 1])]
        return np.asarray([left[0], right[0], right[1], left[1]], dtype=np.float32)

    def _detect(self, image):
        import cv2
        import numpy as np
        height, width = image.shape[:2]
        ratio = min(1.0, 1600.0 / max(height, width))
        # Text detection models require multiples of 32.
        resized_w = max(32, round(width * ratio / 32) * 32)
        resized_h = max(32, round(height * ratio / 32) * 32)
        resized = cv2.resize(image, (resized_w, resized_h))
        pixels = resized.astype(np.float32) / 255.0
        pixels -= np.array([0.485, 0.456, 0.406], dtype=np.float32)
        pixels /= np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
        prob = self._detector.run(None, {self._detector.get_inputs()[0].name: tensor})[0][0, 0]
        bitmap = (prob > 0.3).astype(np.uint8)
        bitmap = cv2.dilate(bitmap, np.ones((2, 2), dtype=np.uint8))
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        output = []
        scale = np.array([width / prob.shape[1], height / prob.shape[0]], dtype=np.float32)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:1500]:
            rect = cv2.minAreaRect(contour)
            rw, rh = rect[1]
            if min(rw, rh) < 3 or rw * rh < 20:
                continue
            x, y, bw, bh = cv2.boundingRect(contour)
            mask = np.zeros((bh, bw), dtype=np.uint8)
            local = contour.copy()
            local[:, 0, 0] -= x
            local[:, 0, 1] -= y
            cv2.fillPoly(mask, [local], 1)
            score = cv2.mean(prob[y:y + bh, x:x + bw], mask)[0]
            if score < 0.5:
                continue
            # For a rectangular text contour, area * ratio / perimeter is the
            # DB polygon offset; expanding its min-area rectangle uses that
            # same distance without a polygon clipping runtime dependency.
            distance = rw * rh * 1.6 / (2.0 * (rw + rh))
            expanded = (rect[0], (rw + distance * 2, rh + distance * 2), rect[2])
            box = self._ordered_quad(cv2.boxPoints(expanded)) * scale
            box[:, 0] = np.clip(box[:, 0], 0, width - 1)
            box[:, 1] = np.clip(box[:, 1], 0, height - 1)
            output.append(box)
        return sorted(output, key=lambda box: (float(box[:, 1].min()), float(box[:, 0].min())))

    def _read_box(self, image, box):
        import cv2
        import numpy as np
        width = max(4, round(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))))
        height = max(4, round(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))))
        destination = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
        transform = cv2.getPerspectiveTransform(box.astype(np.float32), destination)
        crop = cv2.warpPerspective(image, transform, (width, height), borderMode=cv2.BORDER_REPLICATE)
        if height / width > 1.5:
            crop = np.rot90(crop)
        ch, cw = crop.shape[:2]
        resized_w = min(4096, max(1, math.ceil(48 * cw / ch)))
        tensor_w = max(320, resized_w)
        crop = cv2.resize(crop, (resized_w, 48)).astype(np.float32)
        tensor = np.zeros((1, 3, 48, tensor_w), dtype=np.float32)
        tensor[0, :, :, :resized_w] = (crop.transpose(2, 0, 1) / 255.0 - 0.5) / 0.5
        predictions = self._recognizer.run(None, {self._recognizer.get_inputs()[0].name: tensor})[0][0]
        indices = predictions.argmax(axis=1)
        confidences = predictions.max(axis=1)
        selected = indices != 0
        selected[1:] &= indices[1:] != indices[:-1]
        chars = []
        scores = []
        for index, confidence in zip(indices[selected], confidences[selected]):
            if int(index) < len(self._characters):
                chars.append(self._characters[int(index)])
                scores.append(float(confidence))
        if not scores or sum(scores) / len(scores) < 0.4:
            return ""
        return unicodedata.normalize("NFC", "".join(chars)).strip()

    def recognize(self, image) -> str:
        """Return detected Korean/Latin text in reading order for a PIL image.

        Call from a worker thread for larger files; one engine serializes its
        sessions. Empty text is a valid result for an image without readable text.
        """
        import cv2
        import numpy as np
        if image is None or image.width < 2 or image.height < 2:
            return ""
        with self._lock:
            try:
                self._load()
            except Exception as exc:
                self._error = str(exc)
                raise RuntimeError(self.diagnostics()) from exc
            rgb = image.convert("RGB")
            # Bound oversized scans while keeping common A4 documents readable.
            if max(rgb.size) > 6000:
                rgb.thumbnail((6000, 6000))
            pixels = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
            boxes = self._detect(pixels)
            fragments = []
            for box in boxes:
                text = self._read_box(pixels, box)
                if text:
                    fragments.append((float(box[:, 1].mean()), float(box[:, 0].min()),
                                      float(box[:, 1].max() - box[:, 1].min()), text))
            # Preserve one source table row as one text line where possible,
            # which keeps its event title and date together during extraction.
            rows = []
            for cy, x, h, text in fragments:
                target = next((row for row in rows if abs(row["y"] - cy) <= max(8.0, min(row["h"], h) * 0.45)), None)
                if target is None:
                    rows.append({"y": cy, "h": h, "items": [(x, text)]})
                else:
                    target["items"].append((x, text))
            rows.sort(key=lambda row: row["y"])
            return "\n".join("  ".join(text for _, text in sorted(row["items"])) for row in rows)
