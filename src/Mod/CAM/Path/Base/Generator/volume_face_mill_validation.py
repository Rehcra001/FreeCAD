# SPDX-License-Identifier: LGPL-2.1-or-later
"""Validation helpers for Volume Face Mill strict-strategy metadata."""

import math

import FreeCAD
import Path
import Part

from Path.Base.Generator import volume_face_mill_common as common


def _error(message):
    """Return a one-item validation error list."""

    return [str(message)]


def find_layer_clear_state(layer_clear_states, z, tolerance=1e-6):
    """Return the clear state matching z, or None."""

    for clear_state in layer_clear_states or []:
        if math.isclose(float(clear_state.z), float(z), rel_tol=0.0, abs_tol=tolerance):
            return clear_state
    return None


def make_circular_footprint(center, radius, z=0.0, segments=32):
    """Return a planar circular footprint face approximated by a polygon."""

    radius = max(0.0, float(radius))
    if radius <= 0.0:
        return Part.Vertex(FreeCAD.Vector(center.x, center.y, z))

    points = []
    for idx in range(segments):
        angle = (2.0 * math.pi * idx) / segments
        points.append(
            FreeCAD.Vector(
                center.x + (radius * math.cos(angle)),
                center.y + (radius * math.sin(angle)),
                z,
            )
        )
    points.append(points[0])
    return Part.Face(Part.makePolygon(points))


def shape_covers_footprint(shape, footprint, tolerance=1e-6):
    """Return True if shape fully covers footprint within tolerance."""

    if shape is None or footprint is None:
        return False

    try:
        if hasattr(shape, "isNull") and shape.isNull():
            return False
        if hasattr(footprint, "isNull") and footprint.isNull():
            return False
    except Exception:
        return False

    try:
        remaining = footprint.cut(shape)
    except Exception:
        try:
            common_shape = footprint.common(shape)
            footprint_area = abs(getattr(footprint, "Area", 0.0))
            common_area = abs(getattr(common_shape, "Area", 0.0))
            return common_area >= max(0.0, footprint_area - tolerance)
        except Exception:
            return False

    try:
        return abs(getattr(remaining, "Area", 0.0)) <= tolerance
    except Exception:
        return False


def swept_cut_footprint(cut_segment, tool_radius, tolerance=1e-6):
    """Return a conservative swept footprint for a linear cut segment."""

    start = cut_segment.start
    end = cut_segment.end
    radius = max(0.0, float(tool_radius))

    dx = float(end.x) - float(start.x)
    dy = float(end.y) - float(start.y)
    length = math.hypot(dx, dy)

    if length <= tolerance:
        return make_circular_footprint(start, radius, z=cut_segment.z)

    nx = -dy / length
    ny = dx / length

    points = [
        FreeCAD.Vector(start.x + nx * radius, start.y + ny * radius, cut_segment.z),
        FreeCAD.Vector(end.x + nx * radius, end.y + ny * radius, cut_segment.z),
        FreeCAD.Vector(end.x - nx * radius, end.y - ny * radius, cut_segment.z),
        FreeCAD.Vector(start.x - nx * radius, start.y - ny * radius, cut_segment.z),
        FreeCAD.Vector(start.x + nx * radius, start.y + ny * radius, cut_segment.z),
    ]

    corridor = Part.Face(Part.makePolygon(points))
    start_cap = make_circular_footprint(start, radius, z=cut_segment.z)
    end_cap = make_circular_footprint(end, radius, z=cut_segment.z)

    try:
        return corridor.fuse(start_cap).fuse(end_cap)
    except Exception:
        return corridor


def validate_motion_kinds(motions):
    """Validate that all motion segments use known motion kinds."""

    errors = []
    for index, motion in enumerate(motions or []):
        if motion.kind not in common.ALL_MOTION_KINDS:
            errors.append(f"Motion {index} has unknown kind: {motion.kind}")
        if motion.kind == common.MOTION_CUT and not motion.is_cutting:
            errors.append(f"Motion {index} is a cut motion but is_cutting is False")
        if motion.kind != common.MOTION_CUT and motion.is_cutting:
            errors.append(f"Motion {index} is non-cutting but is_cutting is True")
    return errors


def validate_cut_modes(cut_segments, expected_cut_mode):
    """Validate that all cut segments are tagged with the selected cut mode."""

    errors = []
    expected_cut_mode = str(expected_cut_mode)

    for index, cut in enumerate(cut_segments or []):
        if str(cut.cut_mode) != expected_cut_mode:
            errors.append(
                f"Cut segment {index} has cut mode {cut.cut_mode}, expected {expected_cut_mode}"
            )
        if cut.can_reverse:
            errors.append(f"Cut segment {index} is reversible; strict cuts must not be reversible")
    return errors


def validate_no_reversed_cuts(cut_segments, tolerance=1e-6):
    """Validate that strict cut segments were not reversed after generation."""

    errors = []

    for index, cut in enumerate(cut_segments or []):
        if cut.original_start is None or cut.original_end is None:
            continue

        start_matches = common.xy_distance(cut.start, cut.original_start) <= tolerance
        end_matches = common.xy_distance(cut.end, cut.original_end) <= tolerance
        reversed_start_matches = common.xy_distance(cut.start, cut.original_end) <= tolerance
        reversed_end_matches = common.xy_distance(cut.end, cut.original_start) <= tolerance

        if start_matches and end_matches:
            continue

        if reversed_start_matches and reversed_end_matches:
            errors.append(f"Cut segment {index} was reversed by the optimizer")
        else:
            errors.append(f"Cut segment {index} no longer matches its original direction")

    return errors


def validate_layer_starts_with_outside_entry(
    layer_plan, stock_boundbox, entry_clearance, tolerance=1e-6
):
    """Validate that a layer begins with an outside-stock entry plunge."""

    motions = list(getattr(layer_plan, "motions", []) or [])
    if not motions:
        return _error(f"Layer {layer_plan.z} has no motions")

    first_downward = None
    for motion in motions:
        if common.motion_is_downward(motion, tolerance=tolerance):
            first_downward = motion
            break

    if first_downward is None:
        return _error(f"Layer {layer_plan.z} has no downward entry motion")

    errors = []
    if first_downward.kind != common.MOTION_ENTRY_PLUNGE:
        errors.append(
            f"Layer {layer_plan.z} first downward motion is {first_downward.kind}, expected entry_plunge"
        )

    if common.xy_inside_boundbox(first_downward.end, stock_boundbox, tolerance=tolerance):
        errors.append(f"Layer {layer_plan.z} entry plunge ends inside stock XY extents")

    clearance = common.minimum_xy_clearance_from_boundbox(first_downward.end, stock_boundbox)
    if clearance + tolerance < float(entry_clearance):
        errors.append(
            f"Layer {layer_plan.z} entry clearance {clearance:.6f} is less than required {entry_clearance:.6f}"
        )

    return errors


def validate_no_plunge_into_uncut_stock(
    motions,
    stock_boundbox,
    layer_clear_states,
    tool_radius,
    tolerance=1e-6,
):
    """Reject downward moves inside stock unless they plunge into verified cleared material."""

    errors = []

    for index, motion in enumerate(motions or []):
        if not common.motion_is_downward(motion, tolerance=tolerance):
            continue

        end_point = motion.end
        inside_stock = common.xy_inside_boundbox(end_point, stock_boundbox, tolerance=tolerance)

        if not inside_stock:
            if motion.kind not in {
                common.MOTION_ENTRY_PLUNGE,
                common.MOTION_OUTSIDE_REENTRY,
            }:
                errors.append(
                    f"Downward motion {index} outside stock has invalid kind {motion.kind}"
                )
            continue

        if motion.kind != common.MOTION_INTERNAL_REPLUNGE:
            errors.append(
                f"Downward motion {index} plunges inside stock as {motion.kind}; expected internal_replunge"
            )
            continue

        if motion.layer_z is None:
            errors.append(f"Internal re-plunge motion {index} has no layer_z")
            continue

        if not math.isclose(
            float(motion.z_end), float(motion.layer_z), rel_tol=0.0, abs_tol=tolerance
        ):
            errors.append(f"Internal re-plunge motion {index} does not end at its active layer Z")
            continue

        clear_state = find_layer_clear_state(
            layer_clear_states, motion.layer_z, tolerance=tolerance
        )
        if clear_state is None or clear_state.cleared_region is None:
            errors.append(f"Internal re-plunge motion {index} has no cleared region for layer")
            continue

        footprint = make_circular_footprint(end_point, tool_radius, z=motion.layer_z)
        if not shape_covers_footprint(clear_state.cleared_region, footprint, tolerance=tolerance):
            errors.append(
                f"Internal re-plunge motion {index} footprint is not fully inside cleared material"
            )

    return errors


def validate_no_cut_crosses_keepout(cut_segments, protected_regions, tool_radius, tolerance=1e-6):
    """Validate that cut footprints do not overlap protected regions."""

    errors = []

    for cut_index, cut in enumerate(cut_segments or []):
        footprint = swept_cut_footprint(cut, tool_radius, tolerance=tolerance)
        for region_index, protected_region in enumerate(protected_regions or []):
            if protected_region is None:
                continue
            try:
                overlap = footprint.common(protected_region)
            except Exception:
                errors.append(
                    f"Could not validate cut {cut_index} against protected region {region_index}"
                )
                continue

            area = abs(getattr(overlap, "Area", 0.0))
            volume = abs(getattr(overlap, "Volume", 0.0))
            if area > tolerance or volume > tolerance:
                errors.append(f"Cut segment {cut_index} overlaps protected region {region_index}")

    return errors


def validate_stay_down_links(motions, layer_clear_states, tool_radius, tolerance=1e-6):
    """Validate that stay-down links remain inside cleared material."""

    errors = []

    for index, motion in enumerate(motions or []):
        if motion.kind != common.MOTION_STAY_DOWN_LINK:
            continue

        if motion.layer_z is None:
            errors.append(f"Stay-down link {index} has no layer_z")
            continue

        clear_state = find_layer_clear_state(
            layer_clear_states, motion.layer_z, tolerance=tolerance
        )
        if clear_state is None or clear_state.cleared_region is None:
            errors.append(f"Stay-down link {index} has no cleared region for layer")
            continue

        pseudo_cut = common.CutSegment(
            start=motion.start,
            end=motion.end,
            z=motion.layer_z,
            cut_mode="",
            material_side="",
        )
        footprint = swept_cut_footprint(pseudo_cut, tool_radius, tolerance=tolerance)
        if not shape_covers_footprint(clear_state.cleared_region, footprint, tolerance=tolerance):
            errors.append(f"Stay-down link {index} is not fully inside cleared material")

    return errors


def validate_strategy_result(
    result,
    stock_boundbox,
    expected_cut_mode,
    tool_radius,
    entry_clearance,
    protected_regions=None,
    tolerance=1e-6,
):
    """Run all generic Volume Face Mill strict-strategy validators."""

    errors = []
    protected_regions = protected_regions or []

    all_cuts = []
    all_motions = []
    all_clear_states = []

    for layer in result.layers or []:
        all_cuts.extend(layer.cut_segments or [])
        all_motions.extend(layer.motions or [])
        if layer.cleared_state is not None:
            all_clear_states.append(layer.cleared_state)
        errors.extend(
            validate_layer_starts_with_outside_entry(
                layer,
                stock_boundbox,
                entry_clearance,
                tolerance=tolerance,
            )
        )

    errors.extend(validate_motion_kinds(all_motions))
    errors.extend(validate_cut_modes(all_cuts, expected_cut_mode))
    errors.extend(validate_no_reversed_cuts(all_cuts, tolerance=tolerance))
    errors.extend(
        validate_no_plunge_into_uncut_stock(
            all_motions,
            stock_boundbox,
            all_clear_states,
            tool_radius,
            tolerance=tolerance,
        )
    )
    errors.extend(
        validate_no_cut_crosses_keepout(
            all_cuts,
            protected_regions,
            tool_radius,
            tolerance=tolerance,
        )
    )
    errors.extend(
        validate_stay_down_links(
            all_motions,
            all_clear_states,
            tool_radius,
            tolerance=tolerance,
        )
    )

    result.validation_errors = errors
    return errors
