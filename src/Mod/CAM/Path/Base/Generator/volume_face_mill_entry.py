# SPDX-License-Identifier: LGPL-2.1-or-later
"""Outside-stock entry planning helpers for Volume Face Mill."""

from dataclasses import dataclass, field

import FreeCAD
import Path

from Path.Base.Generator import volume_face_mill_common as common


@dataclass
class EntryPlan:
    """Common outside-stock entry plan."""

    stock_boundbox: object
    entry_side: str
    entry_clearance: float
    common_plunge_point: FreeCAD.Vector
    metadata: dict = field(default_factory=dict)


@dataclass
class LayerEntry:
    """Layer-specific entry geometry derived from an EntryPlan."""

    z: float
    plunge_point: FreeCAD.Vector
    lead_in_start: FreeCAD.Vector
    first_cut_start: FreeCAD.Vector = None
    motions: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def normalize_entry_side(entry_side):
    """Return a valid Phase 6 entry-side value."""

    value = str(entry_side) if entry_side is not None else "Auto"
    if value in {"Auto", "-X", "+X", "-Y", "+Y"}:
        return value
    return "Auto"


def resolve_entry_side(entry_side, stock_boundbox=None):
    """Resolve Auto to the current fixed Phase 6 entry side."""

    del stock_boundbox

    value = normalize_entry_side(entry_side)
    if value == "Auto":
        return "-X"
    return value


def common_plunge_point(stock_boundbox, entry_side, entry_clearance):
    """Return the common outside-stock plunge point in XY."""

    entry_side = resolve_entry_side(entry_side, stock_boundbox)
    clearance = max(0.0, float(entry_clearance))
    center_x = 0.5 * (stock_boundbox.XMin + stock_boundbox.XMax)
    center_y = 0.5 * (stock_boundbox.YMin + stock_boundbox.YMax)

    if entry_side == "-X":
        return FreeCAD.Vector(stock_boundbox.XMin - clearance, center_y, 0.0)
    if entry_side == "+X":
        return FreeCAD.Vector(stock_boundbox.XMax + clearance, center_y, 0.0)
    if entry_side == "-Y":
        return FreeCAD.Vector(center_x, stock_boundbox.YMin - clearance, 0.0)
    return FreeCAD.Vector(center_x, stock_boundbox.YMax + clearance, 0.0)


def make_entry_plan(stock_boundbox, entry_side="Auto", entry_clearance=0.0):
    """Return a common entry plan for all layers of one stock envelope."""

    resolved_entry_side = resolve_entry_side(entry_side, stock_boundbox)
    clearance = max(0.0, float(entry_clearance))
    return EntryPlan(
        stock_boundbox=stock_boundbox,
        entry_side=resolved_entry_side,
        entry_clearance=clearance,
        common_plunge_point=common_plunge_point(stock_boundbox, resolved_entry_side, clearance),
    )


def lead_in_start_for_cut(entry_plan, first_cut_start):
    """Return the outside-stock lead-in start aligned to the first cut start."""

    if first_cut_start is None:
        return common.copy_vector(entry_plan.common_plunge_point)

    plunge_point = entry_plan.common_plunge_point
    if entry_plan.entry_side in {"-X", "+X"}:
        return FreeCAD.Vector(plunge_point.x, first_cut_start.y, first_cut_start.z)

    return FreeCAD.Vector(first_cut_start.x, plunge_point.y, first_cut_start.z)


def make_layer_entry(entry_plan, z, first_cut_start=None):
    """Return the layer-specific entry geometry derived from one entry plan."""

    layer_z = float(z)
    plunge_xy = entry_plan.common_plunge_point
    plunge_point = FreeCAD.Vector(plunge_xy.x, plunge_xy.y, layer_z)
    copied_cut_start = None
    if first_cut_start is not None:
        copied_cut_start = common.copy_vector(first_cut_start)
    lead_in_start = lead_in_start_for_cut(entry_plan, copied_cut_start)
    lead_in_start.z = layer_z

    return LayerEntry(
        z=layer_z,
        plunge_point=plunge_point,
        lead_in_start=lead_in_start,
        first_cut_start=copied_cut_start,
    )


def make_entry_plunge_motion(layer_entry, z_start):
    """Return the Phase 6 mandatory outside-stock plunge motion."""

    return common.MotionSegment(
        start=FreeCAD.Vector(
            layer_entry.plunge_point.x,
            layer_entry.plunge_point.y,
            float(z_start),
        ),
        end=common.copy_vector(layer_entry.plunge_point),
        z_start=float(z_start),
        z_end=float(layer_entry.z),
        kind=common.MOTION_ENTRY_PLUNGE,
        layer_z=float(layer_entry.z),
        is_cutting=False,
        is_retracted=False,
    )


def make_lead_in_motion(layer_entry):
    """Return the horizontal non-cutting lead-in motion, or None."""

    if layer_entry.first_cut_start is None:
        return None

    return common.MotionSegment(
        start=common.copy_vector(layer_entry.lead_in_start),
        end=common.copy_vector(layer_entry.first_cut_start),
        z_start=float(layer_entry.z),
        z_end=float(layer_entry.z),
        kind=common.MOTION_LEAD_IN,
        layer_z=float(layer_entry.z),
        is_cutting=False,
        is_retracted=False,
    )
