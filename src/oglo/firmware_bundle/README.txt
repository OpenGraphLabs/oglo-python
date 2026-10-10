OGLO RDR02 signed application firmware 0.9.25

This is the signed production-key application, bundled unchanged.
Hardware: RDR02_FLEX5_REV_D_TIA; part: OGLO-PCBA-D.
Application SHA-256: 3e9d2f1e3c8d085c19ffd345f2f34572f7c5ce65164d2c3b9ef52148ca408d63
Running image SHA-256: d024e7296cec141ce1f02fbcdbd787895bb2da6fa2ad4808d80c174b85e6bbb4
Accepted source images: 0.9.16, 0.9.17 and 0.9.18.
The SDK verifies the detached ECDSA-P256 signature and both image digests.
No signing private key, calibration, device inventory or lab credentials are included.

This copy is the offline floor, not the current release. The SDK reads the
current signed firmware from the runtime channel at connect time, so a newer
release is used without reinstalling anything; this file is what an offline
machine falls back to.

0.9.25 fixes a USB fault where the device cleared its own USB address and
endpoint configuration on an unknown status bit the host never asked about, so
a glove stayed enumerated but silent and reconnecting the program did not
recover it. 0.9.18 before it corrected the IMU sample rate.
Source/qualification context: https://github.com/OpenGraphLabs/oglo-hardware/blob/main/FACTS.md
See docs/10_managed_firmware.md for compatibility and validation limits.
