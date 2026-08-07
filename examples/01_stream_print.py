#!/usr/bin/env python3
"""Connect, calibrate, print. The whole SDK in fifteen lines."""

import oglo

with oglo.connect() as g:
    print(g.info)

    input("Wear the glove, then press Enter. Open and close your hand for 5 s: ")
    g.zero(sweep=5)
    g.clean(threshold=30)

    for i, f in enumerate(g.tactile()):
        peak = f.residual.max()
        which = divmod(int(f.residual.argmax()), 16)
        if i % 50 == 0:
            print(f"{f.t_us/1e6:8.2f}s  peak {peak:6.0f} on {g.info.channels[which[0]]}"
                  f"  dropped={f.dropped}  {g.rates_seen['tactile']:.0f} Hz")
        if i > 500:
            break
