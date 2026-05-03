# SPDX-License-Identifier: LGPL-2.1-or-later
"""Common metadata objects for Volume Face Mill strategy generation."""

from dataclasses import dataclass, field
import math

import FreeCAD
import Path

MOTION_ENTRY_PLUNGE = "entry_plunge"
MOTION_LEAD_IN = "lead_in"
MOTION_CUT = "cut"
MOTION_STAY_DOWN_LINK = "stay_down_link"
MOTION_RETRACT = "retract"
MOTION_RAPID = "rapid"
MOTION_INTERNAL_REPLUNGE = "internal_replunge"
MOTION_OUTSIDE_REENTRY = "outside_reentry"
MOTION_EXIT = "exit"

CUT_MOTION_KINDS = frozenset({MOTION_CUT})
DOWNWARD_PLUNGE_MOTION_KINDS = frozenset(
    {
        MOTION_ENTRY_PLUNGE,
        MOTION_INTERNAL_REPLUNGE,
        MOTION_OUTSIDE_REENTRY,
    }
)
NON_CUTTING_MOTION_KINDS = frozenset(
    {
        MOTION_ENTRY_PLUNGE,
        MOTION_LEAD_IN,
        MOTION_STAY_DOWN_LINK,
        MOTION_RETRACT,
        MOTION_RAPID,
        MOTION_INTERNAL_REPLUNGE,
        MOTION_OUTSIDE_REENTRY,
        MOTION_EXIT,
    }
)
ALL_MOTION_KINDS = CUT_MOTION_KINDS | NON_CUTTING_MOTION_KINDS


@dataclass
class CutRegion:
    """One machinable region at one Z level."""

    z: float
    outer_wire: object
    inner_wires: list = field(default_factory=list)
    region_id: int = 0
    source_shape: object = None
    metadata: dict = field(default_factory=dict)


@dataclass
class CutSegment:
    """One material-removing strict cutting segment."""

    start: FreeCAD.Vector
    end: FreeCAD.Vector
    z: float
    commands: list = field(default_factory=list)
    cut_mode: str = ""
    material_side: str = ""
    region_id: int = 0
    pass_index: int = 0
    can_reverse: bool = False
    original_start: FreeCAD.Vector = None
    original_end: FreeCAD.Vector = None
    strategy: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class MotionSegment:
    """One motion segment used for validation before Path.Command output is trusted."""

    start: FreeCAD.Vector
    end: FreeCAD.Vector
    z_start: float
    z_end: float
    kind: str
    commands: list = field(default_factory=list)
    layer_z: float = None
    is_cutting: bool = False
    is_retracted: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class LayerClearState:
    """Cleared-material state for one cutting layer."""

    z: float
    cleared_region: object = None
    cleared_segments: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class LayerPlan:
    """Planned strategy output for one Z layer."""

    z: float
    regions: list = field(default_factory=list)
    cut_segments: list = field(default_factory=list)
    motions: list = field(default_factory=list)
    cleared_state: LayerClearState = None
    metadata: dict = field(default_factory=dict)


@dataclass
class StrategyResult:
    """Complete metadata-backed result from a Volume Face Mill strategy."""

    commands: list = field(default_factory=list)
    layers: list = field(default_factory=list)
    cutting_length: float = 0.0
    rapid_length: float = 0.0
    retract_count: int = 0
    validation_errors: list = field(default_factory=list)
    strategy: str = ""
    metadata: dict = field(default_factory=dict)


def copy_vector(vector):
    """Return a defensive FreeCAD.Vector copy."""

    return FreeCAD.Vector(vector.x, vector.y, vector.z)


def xy_distance(a, b):
    """Return XY-plane distance between two vectors."""

    dx = float(a.x) - float(b.x)
    dy = float(a.y) - float(b.y)
    return math.hypot(dx, dy)


def xyz_distance(a, b):
    """Return 3D distance between two vectors."""

    dx = float(a.x) - float(b.x)
    dy = float(a.y) - float(b.y)
    dz = float(a.z) - float(b.z)
    return math.sqrt((dx * dx) + (dy * dy) + (dz * dz))


def motion_is_downward(motion, tolerance=1e-6):
    """Return True if motion lowers Z by more than tolerance."""

    return float(motion.z_end) < (float(motion.z_start) - tolerance)


def xy_inside_boundbox(point, boundbox, tolerance=1e-6):
    """Return whether point XY lies inside a bound box XY projection."""

    return (boundbox.XMin - tolerance) <= point.x <= (boundbox.XMax + tolerance) and (
        boundbox.YMin - tolerance
    ) <= point.y <= (boundbox.YMax + tolerance)


def xy_outside_boundbox(point, boundbox, tolerance=1e-6):
    """Return whether point XY lies outside a bound box XY projection."""

    return not xy_inside_boundbox(point, boundbox, tolerance=tolerance)


def minimum_xy_clearance_from_boundbox(point, boundbox):
    """Return minimum signed XY clearance from the exterior of a bound box.

    Positive means the point is outside the box by at least that distance.
    Zero or negative means the point is on or inside the XY extents.
    """

    outside_x = max(boundbox.XMin - point.x, point.x - boundbox.XMax, 0.0)
    outside_y = max(boundbox.YMin - point.y, point.y - boundbox.YMax, 0.0)

    if outside_x > 0.0 or outside_y > 0.0:
        return math.hypot(outside_x, outside_y)

    inside_to_edge = min(
        point.x - boundbox.XMin,
        boundbox.XMax - point.x,
        point.y - boundbox.YMin,
        boundbox.YMax - point.y,
    )
    return -float(inside_to_edge)


def cut_segment_length(cut_segment):
    """Return XY length of a CutSegment."""

    return xy_distance(cut_segment.start, cut_segment.end)


def motion_length(motion):
    """Return 3D length of a MotionSegment."""

    start = FreeCAD.Vector(motion.start.x, motion.start.y, motion.z_start)
    end = FreeCAD.Vector(motion.end.x, motion.end.y, motion.z_end)
    return xyz_distance(start, end)
