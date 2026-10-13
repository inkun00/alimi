import onnxruntime  # Initialize native DLLs before Qt creates a Windows application.
onnxruntime.disable_telemetry_events()

from alimi.app import main

if __name__ == "__main__":
    raise SystemExit(main())
