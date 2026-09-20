#!/usr/bin/env python3
"""Record both hands at once and replay the exact directories returned.

Use host_t for approximate alignment: device clocks have independent origins.
Samples from one USB read can share a host timestamp; there is no hardware sync.
Finger order comes from each glove's info.channels (left is pinky-first).
Read each hand on its own thread so the slower hand does not throttle the other.
"""

from concurrent.futures import ThreadPoolExecutor

import oglo

left, right = oglo.connect_pair()
print(f"left  {left.info.serial}  fingers {left.info.channels}")
print(f"right {right.info.serial}  fingers {right.info.channels}")

try:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            g.info.side: pool.submit(oglo.record, f"out/{g.info.side}", 60, glove=g)
            for g in (left, right)
        }
        # future.result() propagates a recorder failure instead of silently leaving
        # one missing hand and continuing to replay a stale directory.
        episodes = {side: future.result() for side, future in futures.items()}
finally:
    left.close()
    right.close()

for side, episode in episodes.items():
    print(side, oglo.replay(episode).summary())
