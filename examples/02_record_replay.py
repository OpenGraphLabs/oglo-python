#!/usr/bin/env python3
"""Record an episode, then read it back with no glove attached."""

import oglo

ep = oglo.record("out/", seconds=60)
print("wrote", ep)

# From here on, no hardware is involved. Everything below runs on a plane.
e = oglo.replay(ep)
print(e.summary())

for f in e:                      # tactile frames, same objects as a live stream
    values = f.residual if e.info.stream_clean else f.counts
    peak = values.max()
    if peak > 100:
        finger_index = int(values.argmax()) // 16
        finger = e.info.channels[finger_index]
        print(f"{f.device_time_us/1e6:8.2f}s  {peak:6.0f}  {finger}")
