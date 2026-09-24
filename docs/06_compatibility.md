# Compatibility

| Component | Requirement |
| --- | --- |
| Python | 3.10+ |
| Glove firmware | 0.9.10+ with CONFIG schema 6 |
| USB | Supported tagged packets; recommended for recording |
| BLE | Experimental; throughput varies |
| Studio with a webcam | macOS, Windows, or Linux; install the `studio` extra |
| Studio with OVISION on macOS | Packed stereo video with host timing; source archive only |
| Studio with native OVISION on Linux | Python 3.12+, `v4l2-ctl`, `studio-ovision` extra (SyncField 0.8.14), valid flash calibration |

The SDK rejects unsupported firmware and packet layouts.

Only the native Linux OVISION path records camera exposure timing, IMU, and
calibration for Studio's **OG Center sensor source** profile. The macOS path
preserves both video eyes but cannot provide that native metadata. See the
[Studio guide](10_studio.md) for setup and export choices. The Linux native
Studio path has simulated-device tests; validate it on the actual camera and
gloves before relying on a large collection.

Software tests cover Python 3.10–3.14 on Linux, macOS, and Windows. They do not
prove physical capture quality. The team reports successful firmware 0.9.16
functionality tests on its tested setups.

Use `oglo doctor` before recording and [acceptance checks](07_acceptance.md) for a
new deployment. Test long recordings on the actual host, cables, gloves, and disk.

The SDK does not provide hardware synchronization, force calibration, or fused
orientation. USB packets lack a payload checksum. See the
[data reference](02_data_reference.md) for measurement limits.
