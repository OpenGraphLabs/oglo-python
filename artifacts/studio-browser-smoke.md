# OGLO Studio pair-only browser test

2026-09-23 · Aside visible browser · localhost server with simulated camera and USB gloves. This test does not qualify physical hardware.

I exercised the five visible steps in order. **Connect & check** showed separate left/right OGLO identities, a live camera indicator, two 80-taxel displays with different correct finger orders, and fresh sample ages. The Continue button became available only after all three streams were live. **Calibrate** ran the paired sweep and returned to Ready. **Set up button** recognized F9. In **Record & review**, F9 started and stopped capture while the camera and both tactile indicators remained live; F10 kept the validated take. **Export** downloaded a ZIP.

I then recorded a second paired take, discarded it with F11, and exported again. The page showed one kept and one discarded take. The final `dataset.json` listed only the kept pair episode. Both left/right RAW tactile files were present; all 16 archived episode files matched their SHA-256 entries. The API separately returned HTTP 422 with “Studio requires both left and right OGLO gloves” for an explicit single-glove connection request.

Simulated video and sensor files were removed after verification to avoid leaving a large fake dataset in the worktree. Physical pedal, OVISION camera, and OGLO pair validation still require the real hardware. Native OVISION timing and production importer acceptance remain separate work.
