# SPDX-License-Identifier: LGPL-2.1-or-later
"""Section extraction helpers for Volume Face Mill strict strategies."""

import math

import FreeCAD
import Path
import Part

from Path.Base.Generator import volume_face_mill_common as common


def depth_values_from_depthparams(depthparams, start_depth=None, final_depth=None, tolerance=1e-6):
    """Return descending unique finite Z values from depthparams and optional bounds."""

    values = []

    if depthparams is not None:
        try:
            values.extend(float(value) for value in depthparams)
        except Exception:
            data = getattr(depthparams, "data", None)
            if data is not None:
                try:
                    values.extend(float(value) for value in data)
                except Exception:
                    pass

    for value in (start_depth, final_depth):
        if value is None:
            continue
        try:
            values.append(float(getattr(value, "Value", value)))
        except Exception:
            pass

    values = [value for value in values if math.isfinite(value)]
    values.sort(reverse=True)

    unique = []
    for value in values:
        if not unique or not math.isclose(value, unique[-1], rel_tol=0.0, abs_tol=tolerance):
            unique.append(value)

    return unique


def section_shape_at_z(removal_shape, z, tolerance=1e-6):
    """Return a conservative section shape at one Z level, or None."""

    if removal_shape is None:
        return None
    if hasattr(removal_shape, "isNull") and removal_shape.isNull():
        return None

    bb = removal_shape.BoundBox
    if bb.XLength <= tolerance or bb.YLength <= tolerance:
        return None

    plane_margin = max(bb.XLength, bb.YLength, 1.0)
    plane = Part.makePlane(
        bb.XLength + (2.0 * plane_margin),
        bb.YLength + (2.0 * plane_margin),
        FreeCAD.Vector(bb.XMin - plane_margin, bb.YMin - plane_margin, float(z)),
        FreeCAD.Vector(0.0, 0.0, 1.0),
    )

    try:
        section = removal_shape.section(plane)
    except Exception as exc:
        Path.Log.warning(f"Could not section Volume Face Mill removal shape at Z {z}: {exc}")
        return None

    if section is None or (hasattr(section, "isNull") and section.isNull()):
        return None

    if not getattr(section, "Edges", []):
        return None

    return section


def wires_from_section(section_shape, tolerance=1e-6):
    """Return closed planar wires extracted from a section shape."""

    if section_shape is None:
        return []

    edges = list(getattr(section_shape, "Edges", []) or [])
    if not edges:
        return []

    try:
        sorted_edges = Part.__sortEdges__(edges)
    except Exception:
        sorted_edges = edges

    wires = []
    current_edges = []

    for edge in sorted_edges:
        current_edges.append(edge)
        try:
            wire = Part.Wire(current_edges)
            if wire.isClosed():
                wires.append(wire)
                current_edges = []
        except Exception:
            continue

    if current_edges:
        try:
            wire = Part.Wire(current_edges)
            if wire.isClosed():
                wires.append(wire)
        except Exception:
            pass

    valid_wires = []
    for wire in wires:
        try:
            if (
                wire.isClosed()
                and wire.BoundBox.XLength > tolerance
                and wire.BoundBox.YLength > tolerance
            ):
                valid_wires.append(wire)
        except Exception:
            continue

    if len(valid_wires) > 1:
        return valid_wires

    # OCC can flatten __sortEdges__ down to one loop for simple island sections.
    # Preserve the Phase 6 one-region contract by falling back only to grouped
    # closed-wire reconstruction, without reintroducing nested classification.
    try:
        sorted_groups = Part.sortEdges(edges)
    except Exception:
        return valid_wires

    fallback_wires = []
    for group in sorted_groups:
        if isinstance(group, Part.Edge):
            group = [group]
        try:
            wire = Part.Wire(list(group))
            if (
                wire.isClosed()
                and wire.BoundBox.XLength > tolerance
                and wire.BoundBox.YLength > tolerance
            ):
                fallback_wires.append(wire)
        except Exception:
            continue

    if len(fallback_wires) > len(valid_wires):
        return fallback_wires

    return valid_wires


def _wire_area(wire):
    try:
        return abs(Part.Face(wire).Area)
    except Exception:
        return 0.0


def cut_regions_from_section(
    section_shape, z, region_id_start=0, source_shape=None, tolerance=1e-6
):
    """Convert one section shape into Phase 6 cut-region metadata."""

    wires = wires_from_section(section_shape, tolerance=tolerance)
    if not wires:
        return []

    wires = sorted(wires, key=_wire_area, reverse=True)
    outer_wire = wires[0]
    inner_wires = wires[1:]

    return [
        common.CutRegion(
            z=float(z),
            outer_wire=outer_wire,
            inner_wires=inner_wires,
            region_id=int(region_id_start),
            source_shape=source_shape,
            metadata={"section_wire_count": len(wires)},
        )
    ]


def make_cut_regions(
    removal_shape, depthparams, start_depth=None, final_depth=None, tolerance=1e-6
):
    """Return a flat list of cut regions extracted from a removal shape."""

    regions = []
    next_region_id = 0

    for z in depth_values_from_depthparams(
        depthparams,
        start_depth,
        final_depth,
        tolerance=tolerance,
    ):
        section = section_shape_at_z(removal_shape, z, tolerance=tolerance)
        section_regions = cut_regions_from_section(
            section,
            z,
            region_id_start=next_region_id,
            source_shape=removal_shape,
            tolerance=tolerance,
        )
        regions.extend(section_regions)
        next_region_id += len(section_regions)

    return regions


def make_cut_regions_from_layer_volumes(layer_volumes, tolerance=1e-6):
    """Return cut regions extracted from the current allowance-layer volume tuples."""

    regions = []
    next_region_id = 0

    for cut_depth_z, layer_shape in layer_volumes or []:
        section = section_shape_at_z(layer_shape, cut_depth_z, tolerance=tolerance)
        section_regions = cut_regions_from_section(
            section,
            cut_depth_z,
            region_id_start=next_region_id,
            source_shape=layer_shape,
            tolerance=tolerance,
        )
        regions.extend(section_regions)
        next_region_id += len(section_regions)

    return regions
