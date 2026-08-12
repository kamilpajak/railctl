"""The station facade's shared vocabulary, gathered from three siblings.

`types.py`, `timing.py` and `capabilities.py` each stay import-clean on
their own (`types.py` depends on `capabilities.py`; `capabilities.py`
depends on nothing in this package). This module is the one place a caller
reaches for all of it, plus the wire-level types the station and CLI layers
also need: `Direction`, `StationVersion`, `StationStatus`, `LocoInfo` and
`CvEncoding`, defined once in `xbus` and re-exported here rather than a
second time.
"""

from __future__ import annotations

from railctl.station.capabilities import (
    CAPABILITIES_VERSION,
    LEARNABLE_FIELDS,
    UNKNOWN_IDENTITY,
    Capabilities,
    ResultChannel,
)
from railctl.station.doctor import exit_code_for_report, run_probe, verdict_lines
from railctl.station.facade import Station
from railctl.station.programming import resolve_mode
from railctl.station.timing import TIMING, Timing
from railctl.station.types import (
    ADDRESS_CVS,
    BLIND_WRITE_CVS,
    CV29_LONG_ADDRESS_BIT,
    CV144,
    DECODER_TYPE_CV,
    EVENT_NAMES,
    INDEXED_CV_RANGE,
    LAYOUT_UNTOUCHED,
    MS_DECODER_TYPES,
    PAGE_SELECTOR_CVS,
    Address,
    Check,
    CvNumber,
    CvPage,
    CvReadOutcome,
    CvResult,
    CvSpec,
    DoctorReport,
    LayoutState,
    ProgMode,
    StationEvent,
    decoder_family,
    layout_json,
    treats_cv144_as_lock,
)
from railctl.xbus.dialect import CvEncoding
from railctl.xbus.replies import LocoInfo, StationStatus, StationVersion
from railctl.xbus.speed import Direction

__all__ = [
    "ADDRESS_CVS",
    "BLIND_WRITE_CVS",
    "CAPABILITIES_VERSION",
    "CV29_LONG_ADDRESS_BIT",
    "CV144",
    "DECODER_TYPE_CV",
    "EVENT_NAMES",
    "INDEXED_CV_RANGE",
    "LAYOUT_UNTOUCHED",
    "LEARNABLE_FIELDS",
    "MS_DECODER_TYPES",
    "PAGE_SELECTOR_CVS",
    "TIMING",
    "UNKNOWN_IDENTITY",
    "Address",
    "Capabilities",
    "Check",
    "CvEncoding",
    "CvNumber",
    "CvPage",
    "CvReadOutcome",
    "CvResult",
    "CvSpec",
    "Direction",
    "DoctorReport",
    "LayoutState",
    "LocoInfo",
    "ProgMode",
    "ResultChannel",
    "Station",
    "StationEvent",
    "StationStatus",
    "StationVersion",
    "Timing",
    "decoder_family",
    "exit_code_for_report",
    "layout_json",
    "resolve_mode",
    "run_probe",
    "treats_cv144_as_lock",
    "verdict_lines",
]
