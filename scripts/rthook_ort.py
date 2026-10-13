"""Initialize OCR native libraries before PyInstaller's Qt runtime hook.

Qt creates an embedded resource during its hook and loads native DLLs. Loading
ONNX Runtime first avoids a Windows DLL initialization conflict with Qt 5.
"""
import sys

try:
    import onnxruntime
    onnxruntime.disable_telemetry_events()
except Exception:
    if "--smoke-test" in sys.argv:
        import json
        import traceback
        from pathlib import Path
        destination = Path(sys.argv[sys.argv.index("--smoke-test") + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"success": False, "error": traceback.format_exc()},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(1)
    raise
