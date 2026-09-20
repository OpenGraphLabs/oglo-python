"""Collection field specification for the camera/glove example format.

These TypedDict classes describe ordinary dictionaries, so JSON output and existing
recordings are unchanged. They support editor/type-checker guidance, not runtime
validation. See docs/09_data_specification.md for required completion conditions,
units, shapes, and the separate glove episode schema.
"""

from typing import Any, Literal, Optional, TypedDict


class CameraTiming(TypedDict):
    """Timing captured before encoding/writing; unknown native fields are null."""

    host_read_started_ns: Optional[int]
    host_received_ns: int
    device_timestamp: Optional[int]
    device_timestamp_unit: Optional[str]
    device_clock_domain: Optional[str]
    device_timestamp_meaning: Optional[str]


class _CameraFrame(CameraTiming):
    frame_index: int  # Zero-based decoded video frame index.


class CameraFrameData(_CameraFrame, total=False):
    """One camera/timestamps.jsonl row; native references are OVISION-only."""

    native_metadata: str  # Relative to the timestamps file's directory.
    native_frame_number: int


class CameraData(TypedDict, total=False):
    """Camera manifest entry, populated as capture progresses.

    May be empty on setup failure. A complete capture requires the common fields
    listed in the specification, plus the fields for its camera kind.
    """

    kind: Literal["usb_webcam", "ovision"]
    video: str  # Session-relative path.
    timestamps: str  # Session-relative path.
    codec: str
    requested_fps: float
    host_timestamp_meaning: str
    width: int  # Encoded image pixels; packed width for stereo.
    height: int
    frames_submitted: int
    frames_decoded: int
    first_host_received_ns: int
    last_host_received_ns: int
    # Webcam settings and backend observations.
    index: int
    playback_fps: float
    fps_request_accepted: bool
    backend: str
    # OVISION native provenance and geometry.
    model: str
    video_device: str
    usb_serial: Optional[str]
    syncfield_version: str
    native_stereo_metadata: str
    calibration: str
    eye_order: list[str]
    eye_width: int
    eye_height: int
    native_artifacts: list[str]


class _GloveData(TypedDict):
    serial: str  # Logical CONFIG serial, not USB descriptor serial.
    side: Literal["left", "right"]
    calibration: str  # Session-relative GET ZERO read-back path.
    episode: Optional[str]  # Session-relative SDK episode directory; null until saved.


class GloveData(_GloveData, total=False):
    """One independently recorded hand and its unmodified SDK episode."""

    summary: dict[str, Any]  # Episode.summary() after replay verification.
    error: str


class _OGLData(TypedDict):
    schema: Literal["oglo-camera-example.v1"]
    task_description: str
    sdk_version: str
    opencv_version: str
    requested_duration_s: float
    host_clock: Literal["time.monotonic_ns"]
    same_host: Literal[True]
    started_wall_time_ns: int  # Unix time; provenance, not stream alignment.
    started_host_monotonic_ns: int
    complete: bool
    alignment_validated: Literal[False]  # This capture format does not certify sync.
    error: Optional[str]
    camera: CameraData
    gloves: list[GloveData]


class OGLData(_OGLData, total=False):
    """One session's manifest.json, referring to video and independent sensor arrays.

    Capture starts with complete=False, camera={}, and gloves=[]. Finalization adds
    ended_wall_time_ns; successful checks add overlap_host_received_ns and set
    complete=True. See the specification before interpreting that flag.

    Use ``from oglo import OGLData`` for the session type and ``oglo.data`` for
    nested types. Constructing or annotating these dictionaries does not validate
    input, load referenced files, or synchronize samples.
    """

    ended_wall_time_ns: int
    overlap_host_received_ns: list[int]  # Exactly [start_ns, end_ns], start < end.
