"""Serve Studio with simulated hardware for visible browser smoke testing."""

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import oglo
import uvicorn

from test_camera_glove import Camera
from studio_fake_devices import simulated_studio_glove
from oglo.studio import Studio, create_app
import oglo.studio as studio_module


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/browser-smoke")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18765
    original = cv2.VideoCapture
    camera = Camera()
    cv2.VideoCapture = lambda source: camera if isinstance(source, int) else original(source)
    class EyeCamera(Camera):
        def __init__(self, name, eye):
            super().__init__()
            self.name = name
            self.eye = eye

    studio_module.FFmpegEyeCapture = EyeCamera
    oglo.connect_pair = lambda: (simulated_studio_glove("left"),
                                 simulated_studio_glove("right"))
    studio_module._camera_choices = lambda: [
        {"index": 0, "mode": "default", "label": "Built-in camera · standard view"},
        {"index": 1, "mode": "ovision_left", "name": "OVISION USB camera",
         "label": "OVISION USB camera · left preview · saves both eyes"},
        {"index": 1, "mode": "ovision_right", "name": "OVISION USB camera",
         "label": "OVISION USB camera · right preview · saves both eyes"},
        {"index": 1, "mode": "default", "label": "OVISION USB camera · standard view"},
    ]
    studio_module.list_candidates = lambda: [
        SimpleNamespace(device="/dev/oglo-left", product="OGLO"),
        SimpleNamespace(device="/dev/oglo-right", product="OGLO"),
    ]
    oglo.connect = lambda *, port, timeout=6.0: simulated_studio_glove(
        "left" if port.endswith("left") else "right")
    uvicorn.run(create_app(output, studio=Studio(output)), host="127.0.0.1", port=port,
                log_level="warning")


if __name__ == "__main__":
    main()
