"""Paired-glove and camera collection SDK.

Install ``oglo[collection]`` for webcam/macOS capture. On Linux, install
``oglo[collection,studio-ovision]`` for the native OVISION sensor source.

    from oglo.collection import CameraSelection, Collection

    with Collection("captures/session") as collection:
        choice = next(c for c in collection.devices()["cameras"]
                      if c["mode"] == "ovision_native_left")
        collection.connect_camera(CameraSelection.from_choice(choice))
        collection.calibrate(threshold=70)  # or reuse an already verified zero
        episode = collection.take("Pick up a cup", seconds=10)
        collection.review_video(episode["id"])  # inspect the footage
        collection.select(episode["id"], "kept")
        archive = collection.export("og_center_postprocessing")

``take`` does not judge whether hands and task are visible. Review the video
and tactile response before keeping real data; use ``start``/``stop``/``wait``
instead when a human or pedal decides the capture boundary.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Literal, Protocol

from .studio import CameraSelection, Collection

DeliveryProfile = Literal["source_archive", "annotation_handoff", "og_center_postprocessing"]


class CameraAdapter(Protocol):
    """Camera ownership boundary used by the collection controller."""

    index: int
    mode: str
    name: str | None
    eye: str | None
    size: tuple[int, int] | None
    error: str | None
    native_device_timestamps: bool
    postprocessing_capable: bool

    def preview(self) -> bytes | None: ...
    def live_status(self) -> dict: ...
    def begin(self, folder: Path, stop: Event) -> None: ...
    def finish(self, timeout: float = 10.0) -> dict: ...
    def close(self) -> None: ...


__all__ = ["CameraSelection", "Collection", "CameraAdapter", "DeliveryProfile"]
