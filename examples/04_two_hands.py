#!/usr/bin/env python3
"""Record both hands at once and replay the exact directories returned.

Two things to get right, and one trap to avoid.

RIGHT:  relate transport arrivals on host_t, never across devices on t_us. Each glove
        counts microseconds from its own power-on. host_t is the shared computer's
        USB-read boundary, so samples from one read may have the same value; it is not
        hardware-trigger synchronization.

RIGHT:  finger order comes from info.channels, per hand. The left hand is reversed
        (pinky first), so one hardcoded list mislabels every left-hand dataset in a
        way the numbers never show.

TRAP:   do NOT read `next(left_iter)` then `next(right_iter)` in a loop. That locks
        the two hands together and throttles both to whichever is slower. Drain each
        hand independently -- a thread each is the simplest way.
"""

from concurrent.futures import ThreadPoolExecutor

import oglo

# Production/default: one left, one right, and the same non-empty pair_id.
# For two deliberately unprovisioned bench units only, pass allow_unpaired=True.
left, right = oglo.connect_pair()
print(f"left  {left.info.serial}  pair {left.info.pair_id}  fingers {left.info.channels}")
print(f"right {right.info.serial}  pair {right.info.pair_id}  fingers {right.info.channels}")

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
