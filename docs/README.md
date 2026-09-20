# OGLO SDK documentation

Start with the [README quickstart](../README.md#install).

| Guide | Contents |
| --- | --- |
| [Data reference](02_data_reference.md) | Streams, units, axes, clocks, and loss |
| [Collection data specification](09_data_specification.md) | `OGLData`, required collection fields, shapes, units, and example records |
| [Calibration](03_calibration.md) | Sweep zero, raw/clean modes, and thresholds |
| [Recording and replay](04_recording.md) | Episodes, cancellation, and two hands |
| [USB webcam + glove](../examples/camera_glove/README.md) | Webcam setup, concurrent capture, timestamp fields, and offline joins |
| [OVISION v1 + glove](../examples/camera_glove/OVISION.md) | Native stereo/IMU capture, calibration, and camera-to-glove field mapping |
| [Troubleshooting](05_troubleshooting.md) | Connection and data problems |
| [Compatibility](06_compatibility.md) | Supported protocol and validation scope |
| [Acceptance](07_acceptance.md) | Physical glove checks and reports |
| [Candidate status](08_candidate_status.md) | Package evaluation and remaining qualification |

The wire contract is covered by the data reference and captured
[test vectors](../spec/vectors/).
