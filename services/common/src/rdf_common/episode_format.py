"""Episode recording format: the ONE place the actions parquet is defined (TDM-25).

Writers: the Jetson recorder (TDM-48), the web/phone teleop path, purchased-dataset ingest.
Readers: the reviewer (``rdf_common.review_checks``, TDM-25), dataset-purchase QA (TDM-92),
LeRobot conversion (TDM-39). Change a column here and every reader sees it.

An episode is uploaded through the TDM-4 storage interface under
``tickets/{ticket_id}/episodes/{episode_id}/``:

    <camera>.mp4        one H.264 mp4 per camera (e.g. exo.mp4, wrist.mp4), recorded at ``fps``
    actions.parquet     one row per control tick (the same clock as the camera frames)

and the ``episode`` row points at it: ``cameras_json = {"exo": "<uri of exo.mp4>", ...}``,
``actions_uri = "<uri of actions.parquet>"``, ``frame_count`` = rows in the parquet,
``duration_ms`` = last - first ``timestamp_us`` (ms), ``takeover_ts`` / ``handback_ts`` on the
same clock as ``timestamp_us``.

actions.parquet columns (``ACTION_COLUMNS``; * = required):

    frame_index*     int32         0..N-1, the control tick / video frame number
    timestamp_us*    int64         wall clock, microseconds since the Unix epoch (NTP-synced
                                   robot clock; the same clock as takeover_ts / handback_ts)
    joint_pos*       list<float32> measured joint positions after the tick, rad, len = n_joints
    gripper_pos*     float32       measured gripper opening, 0 = closed .. 1 = open
    ik_target_pos*   list<float32> commanded end-effector target position [x, y, z], m, base frame
    ik_target_quat*  list<float32> commanded end-effector target orientation [w, x, y, z]
    gripper_cmd*     float32       commanded gripper opening, 0 .. 1
    control_source*  string        who drove this tick: "policy" | "human" | "idle"
    joint_cmd        list<float32> the IK solution sent to the arm (after the safety layer), rad

This is the "IK target + resulting joint positions" pair CONTEXT.md asks for: ``ik_target_*`` and
``gripper_cmd`` are the action in the policy's action space, ``joint_pos`` / ``gripper_pos`` are
what the arm actually did.

Parquet key-value metadata under ``EPISODE_META_KEY`` (JSON)::

    {"format": "rdf.episode.actions", "version": 1, "fps": 30.0,
     "embodiment": "i2rt_yam", "joint_names": ["j1", ..., "j6"],
     "cameras": {"exo": {"frames": 300, "fps": 30.0, "width": 640, "height": 480}, ...},
     "fixture": false, "note": ""}

``fixture: true`` marks synthetic test data (never real robot data); keep it false on real
recordings. ``cameras.*.frames`` is optional; when present the reviewer counts missing video
frames as drops.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

FORMAT_NAME = "rdf.episode.actions"
FORMAT_VERSION = 1
EPISODE_META_KEY = b"rdf.episode"
ACTIONS_FILENAME = "actions.parquet"
CONTROL_SOURCES = ("policy", "human", "idle")
DEFAULT_FPS = 30.0

# name -> (arrow type name, required, description). Arrow types are built lazily in arrow_schema().
ACTION_COLUMNS: dict[str, tuple[str, bool, str]] = {
    "frame_index": ("int32", True, "control tick / video frame number, 0..N-1"),
    "timestamp_us": ("int64", True, "unix epoch microseconds, same clock as takeover_ts/handback_ts"),
    "joint_pos": ("list<float32>", True, "measured joint positions, rad"),
    "gripper_pos": ("float32", True, "measured gripper opening, 0 closed .. 1 open"),
    "ik_target_pos": ("list<float32>", True, "commanded EE position xyz, m, base frame"),
    "ik_target_quat": ("list<float32>", True, "commanded EE orientation wxyz"),
    "gripper_cmd": ("float32", True, "commanded gripper opening, 0 .. 1"),
    "control_source": ("string", True, "policy | human | idle"),
    "joint_cmd": ("list<float32>", False, "IK solution sent to the arm after the safety layer, rad"),
}
REQUIRED_COLUMNS = tuple(k for k, (_, req, _) in ACTION_COLUMNS.items() if req)


class EpisodeFormatError(ValueError):
    pass


def arrow_schema(metadata: Mapping[str, Any] | None = None, *, with_joint_cmd: bool = True):
    import pyarrow as pa

    types = {"int32": pa.int32(), "int64": pa.int64(), "float32": pa.float32(), "string": pa.string(),
             "list<float32>": pa.list_(pa.float32())}
    fields = [pa.field(n, types[t], nullable=not req) for n, (t, req, _) in ACTION_COLUMNS.items()
              if req or with_joint_cmd]
    md = {EPISODE_META_KEY: json.dumps(dict(metadata)).encode()} if metadata is not None else None
    return pa.schema(fields, metadata=md)


def make_metadata(*, fps: float = DEFAULT_FPS, joint_names: Sequence[str], embodiment: str = "i2rt_yam",
                  cameras: Mapping[str, Mapping[str, Any]] | None = None, fixture: bool = False,
                  note: str = "") -> dict:
    return {"format": FORMAT_NAME, "version": FORMAT_VERSION, "fps": float(fps), "embodiment": embodiment,
            "joint_names": list(joint_names), "cameras": {k: dict(v) for k, v in (cameras or {}).items()},
            "fixture": bool(fixture), "note": note}


def write_actions(dest: str | Path | io.IOBase, columns: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
    """Write an actions parquet. ``columns`` maps column name -> sequence / numpy array (2-D arrays
    for the list columns). Missing required columns raise; unknown columns raise."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    unknown = set(columns) - set(ACTION_COLUMNS)
    if unknown:
        raise EpisodeFormatError(f"unknown action columns {sorted(unknown)}")
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise EpisodeFormatError(f"missing required action columns {missing}")
    schema = arrow_schema(metadata, with_joint_cmd="joint_cmd" in columns)
    arrays = []
    for f in schema:
        v = columns[f.name]
        if pa.types.is_list(f.type):
            arrays.append(pa.array([list(map(float, row)) for row in np.asarray(v, dtype=np.float32)],
                                   type=f.type))
        else:
            arrays.append(pa.array(list(v) if not isinstance(v, np.ndarray) else v.tolist(), type=f.type))
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), dest)


@dataclass
class EpisodeActions:
    """A decoded actions parquet. Arrays are numpy; ``columns`` lists what the file had."""

    frame_index: Any  # (N,) int
    timestamp_us: Any  # (N,) int64
    joint_pos: Any  # (N, J) float64
    control_source: list[str]
    meta: dict = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # every other column, as numpy

    @property
    def n(self) -> int:
        return int(len(self.timestamp_us))

    @property
    def fps(self) -> float:
        try:
            return float(self.meta.get("fps") or DEFAULT_FPS)
        except (TypeError, ValueError):
            return DEFAULT_FPS

    @property
    def is_fixture(self) -> bool:
        return bool(self.meta.get("fixture"))


def read_actions(src: str | Path | bytes | io.IOBase) -> EpisodeActions:
    """Read an actions parquet (path, bytes or file object). Raises EpisodeFormatError when the
    file is not parquet or lacks a required column; values are checked by review_checks."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    if isinstance(src, (bytes, bytearray)):
        src = pa.BufferReader(bytes(src))
    try:
        table = pq.read_table(src)
    except Exception as e:  # noqa: BLE001 - any arrow error means "not a readable actions parquet"
        raise EpisodeFormatError(f"not a readable parquet file: {type(e).__name__}: {e}") from e
    cols = list(table.column_names)
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise EpisodeFormatError(f"actions parquet is missing required columns {missing}")
    raw_md = (table.schema.metadata or {}).get(EPISODE_META_KEY)
    try:
        meta = json.loads(raw_md) if raw_md else {}
    except ValueError:
        meta = {}

    def lists(name: str):
        vals = table.column(name).to_pylist()
        widths = {len(v) for v in vals if v is not None}
        if None in vals or len(widths) > 1:
            raise EpisodeFormatError(f"column {name} has null or ragged rows (widths {sorted(widths)})")
        return np.asarray(vals, dtype=np.float64).reshape(len(vals), widths.pop() if widths else 0)

    extra: dict[str, Any] = {}
    for c in cols:
        if c in ("frame_index", "timestamp_us", "joint_pos", "control_source"):
            continue
        if pa.types.is_list(table.schema.field(c).type):
            extra[c] = lists(c)
        else:
            extra[c] = np.asarray(table.column(c).to_pylist())
    return EpisodeActions(
        frame_index=np.asarray(table.column("frame_index").to_pylist(), dtype=np.int64),
        timestamp_us=np.asarray(table.column("timestamp_us").to_pylist(), dtype=np.int64),
        joint_pos=lists("joint_pos"),
        control_source=[str(s) for s in table.column("control_source").to_pylist()],
        meta=meta, columns=cols, extra=extra,
    )


def cameras_from_json(cameras_json: str | Mapping | None) -> dict[str, str]:
    """``episode.cameras_json`` -> {camera_name: uri}. Accepts ``{"exo": "<uri>"}`` or
    ``{"exo": {"uri": "<uri>", ...}}``; anything else (e.g. the fake robot's flags) is dropped."""
    if cameras_json is None or cameras_json == "":
        return {}
    try:
        cams = json.loads(cameras_json) if isinstance(cameras_json, str) else dict(cameras_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(cams, dict):
        return {}
    out = {}
    for name, v in cams.items():
        uri = v.get("uri") if isinstance(v, dict) else v
        if isinstance(uri, str) and uri:
            out[str(name)] = uri
    return out
