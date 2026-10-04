OGLO RDR02 signed application firmware 0.9.18

This is the signed production-key application, bundled unchanged.
Hardware: RDR02_FLEX5_REV_D_TIA; part: OGLO-PCBA-D.
Application SHA-256: 64275ef98a3df6679109c61c7fe119ebaf47a8e5860b6b0fdbb37e3820ca1ab1
Running image SHA-256: f83f4e5b8e706d7b53868b5537c5afb9549c9cf95c23a03be86182de2b31bdb1
Accepted source images: 0.9.16 (b1c53157...) and 0.9.17 (eddf0ca9...).
The SDK verifies the detached ECDSA-P256 signature and both image digests.
No signing private key, calibration, device inventory or lab credentials are included.
0.9.18 changes the IMU sample rate only: through 0.9.17 the 500 Hz IMU stream
carried 200 Hz of fresh samples. Its evidence is one bench unit, one session.
Source/qualification context: https://github.com/OpenGraphLabs/oglo-hardware/blob/main/docs/firmware-0.9.18-imu-odr.md
See docs/10_managed_firmware.md for compatibility and validation limits.
