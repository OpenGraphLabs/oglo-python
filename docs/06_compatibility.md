# Compatibility

| Component | Requirement |
| --- | --- |
| Python | 3.10+ |
| Glove firmware | 0.9.10+ with CONFIG schema 6 |
| USB | Supported tagged packets; recommended for recording |
| BLE | Experimental; throughput varies |
| OVISION example | Linux, Python 3.12+, pinned example dependencies |

The SDK rejects unsupported firmware and packet layouts.

Software tests cover Python 3.10–3.14 on Linux, macOS, and Windows. They do not
prove physical capture quality. The team reports successful firmware 0.9.16
functionality tests on its tested setups.

Use `oglo doctor` before recording and [acceptance checks](07_acceptance.md) for a
new deployment. Test long recordings on the actual host, cables, gloves, and disk.

The SDK does not provide hardware synchronization, force calibration, or fused
orientation. USB packets lack a payload checksum. See the
[data reference](02_data_reference.md) for measurement limits.
