# OGLO SDK documentation

This documentation belongs to the sole canonical SDK repository,
[`OpenGraphLabs/oglo-python`](https://github.com/OpenGraphLabs/oglo-python). All
issues, pull requests, tags, releases, and documentation updates belong there.

1. [Quickstart](01_quickstart.md) - install, diagnose, and read the first frames
2. [Compatibility](06_compatibility.md) - supported and measured scope
3. [Data reference](02_data_reference.md) - streams, units, frames, taxel addressing
4. [Calibration](03_calibration.md) - the sweep, raw vs clean, the deadband
5. [Recording and replay](04_recording.md) - episode format, two hands
6. [Troubleshooting](05_troubleshooting.md) - start with `oglo doctor`
7. [Test your own glove pair](07_acceptance.md) - guided public-API acceptance and reports
8. [0.9.13 release HIL](08_release_hil.md) - named-pair DTR/TAG/reconnect/stall gate and confirmed 72-hour soak

The public wire-level contract needed by SDK users is documented in the
[data reference](02_data_reference.md). TAG v1 is locked by the captured vectors
under [`spec/vectors/`](../spec/vectors/); the canonical TAG v2 contract and
synthetic vectors live in [`spec/TAG_V2.json`](../spec/TAG_V2.json).
