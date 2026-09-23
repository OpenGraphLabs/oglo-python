#!/usr/bin/env python3
"""Record both hands at once and replay the exact directories returned.

Use host_t for approximate alignment: device clocks have independent origins.
Samples from one USB read can share a host timestamp; there is no hardware sync.
Finger order comes from each glove's info.channels (left is pinky-first).
Read each hand on its own thread so the slower hand does not throttle the other.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event

import oglo

left, right = oglo.connect_pair()
print(f"left  {left.info.serial}  fingers {left.info.channels}")
print(f"right {right.info.serial}  fingers {right.info.channels}")

stop = Event()
episodes = {}


def capture(glove):
    try:
        return oglo.record(f"out/{glove.info.side}", 60, glove=glove, stop_event=stop)
    finally:
        # record() resumes caller-owned gloves. Do not leave one transmitting
        # while waiting for its peer or replaying the saved files.
        glove.stop()


try:
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            futures = {pool.submit(capture, g): g.info.side for g in (left, right)}
            for future in as_completed(futures):
                episodes[futures[future]] = future.result()
        except BaseException:
            # Set this BEFORE executor shutdown waits for the peer recorder.
            stop.set()
            raise
finally:
    left.close()
    right.close()

for side, episode in episodes.items():
    print(side, oglo.replay(episode).summary())
