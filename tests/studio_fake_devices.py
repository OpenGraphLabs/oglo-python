"""Synthetic devices that keep packet sequences continuous across preview/capture."""

import time

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo._device import Glove
from oglo._usb import UsbTransport


class ContinuousFakeSerial(FakeSerial):
    def _handle(self, cmd):
        if cmd.upper() == "STREAM TAG ON":
            self._streaming = True
            self._out += tagged_burst(self._burst_tactile, start_seq=self._next_tactile_seq)
            self._next_tactile_seq += self._burst_tactile
            self._next_refill = time.monotonic() + self._burst_secs
            return
        super()._handle(cmd)


def simulated_studio_glove(side):
    config = {**CFG_V6, "side": side, "serial": f"OGLO-{side}-STUDIO-TEST",
              "stream_clean": True}
    if side == "right":
        config["channels"] = list(reversed(config["channels"]))
    serial = ContinuousFakeSerial(config, stream=tagged_burst(4), hz=250)
    transport = UsbTransport(serial)
    info, caps = transport.read_config(interval=0.01, drain=0)
    return Glove(transport, info, caps)
