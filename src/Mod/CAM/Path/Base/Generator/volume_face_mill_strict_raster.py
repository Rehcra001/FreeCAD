# SPDX-License-Identifier: LGPL-2.1-or-later
"""Strict one-way raster strategy for Volume Face Mill."""

import math

import FreeCAD
import Path

from Path.Base.Generator import facing_common
from Path.Base.Generator import volume_face_mill_common as common
from Path.Base.Generator import volume_face_mill_entry as entry
from Path.Base.Generator import volume_face_mill_validation as validation


def _unit_vectors(angle_degrees):
    """Return normalized primary and step vectors for the raster angle."""

    primary_vec, step_vec = facing_common.unit_vectors_from_angle(angle_degrees)
    if primary_vec.Length > 0.0:
        primary_vec = primary_vec.multiply(1.0 / primary_vec.Length)
    if step_vec.Length > 0.0:
        step_vec = step_vec.multiply(1.0 / step_vec.Length)
    return primary_vec, step_vec


def _projection_bounds(wires, vec, origin):
    """Return min/max vector projections for all wire vertices."""

    values = []

    for wire in wires or []:
        try:
            for vertex in getattr(wire, "Vertexes", []) or []:
                point = getattr(vertex, "Point", None)
                if point is None:
                    continue
                values.append(vec.dot(point.sub(origin)))
        except Exception:
            continue

    if not values:
        return (0.0, 0.0)

    return (min(values), max(values))


def _step_positions(region, step_vec, tool_diameter, stepover_percent, origin):
    """Return deterministic ascending step positions for one region."""

    stepover = max(
        float(tool_diameter) * (float(stepover_percent) / 100.0),
        float(tool_diameter) * 0.01,
    )
    min_t, max_t = _projection_bounds([region.outer_wire], step_vec, origin)
    if max_t < min_t:
        min_t, max_t = max_t, min_t

    span = max_t - min_t
    if span <= 1e-9:
        return [float(min_t)]

    offset = min(stepover * 0.5, span * 0.5)
    upper = max_t - offset
    values = []
    t = min_t + offset
    epsilon = max(stepover * 1e-6, 1e-9)

    while t <= upper + epsilon:
        values.append(float(t))
        t += stepover

    if not values:
        return [float(min_t + (span * 0.5))]

    return values


def _slice_wire_segments(wire, primary_vec, step_vec, t, origin):
    """Return slice intervals for one wire, or an empty list on failure."""

    try:
        return list(facing_common.slice_wire_segments(wire, primary_vec, step_vec, t, origin) or [])
    except Exception:
        return []


def _normalized_intervals(intervals, tolerance):
    """Normalize interval ordering and discard degenerate spans."""

    normalized = []
    for interval in intervals or []:
        if interval is None or len(interval) != 2:
            continue
        start = float(min(interval[0], interval[1]))
        end = float(max(interval[0], interval[1]))
        if end - start <= tolerance:
            continue
        normalized.append((start, end))
    normalized.sort(key=lambda value: (value[0], value[1]))
    return normalized


def _subtract_intervals(outer_intervals, hole_intervals, tolerance=1e-6):
    """Subtract hole intervals from outer intervals."""

    remaining = _normalized_intervals(outer_intervals, tolerance)
    holes = _normalized_intervals(hole_intervals, tolerance)

    for hole_start, hole_end in holes:
        updated = []
        for outer_start, outer_end in remaining:
            if hole_end <= outer_start + tolerance or hole_start >= outer_end - tolerance:
                updated.append((outer_start, outer_end))
                continue

            if hole_start > outer_start + tolerance:
                updated.append((outer_start, min(hole_start, outer_end)))
            if hole_end < outer_end - tolerance:
                updated.append((max(hole_end, outer_start), outer_end))
        remaining = _normalized_intervals(updated, tolerance)

    return remaining


def _region_intervals_at_t(region, primary_vec, step_vec, t, origin):
    """Return remaining cut intervals for one region at one step position."""

    outer_intervals = _slice_wire_segments(region.outer_wire, primary_vec, step_vec, t, origin)
    hole_intervals = []

    for wire in getattr(region, "inner_wires", []) or []:
        hole_intervals.extend(_slice_wire_segments(wire, primary_vec, step_vec, t, origin))

    return _subtract_intervals(outer_intervals, hole_intervals, tolerance=1e-6)


def _cut_segment_from_interval(
    region,
    primary_vec,
    step_vec,
    origin,
    t,
    interval,
    cut_mode,
    pass_index,
    strategy,
):
    """Build one strict raster cut segment from one interval."""

    if cut_mode == "Climb":
        start_s = max(interval)
        end_s = min(interval)
    else:
        start_s = min(interval)
        end_s = max(interval)

    start = FreeCAD.Vector(
        origin.x + (primary_vec.x * start_s) + (step_vec.x * t),
        origin.y + (primary_vec.y * start_s) + (step_vec.y * t),
        float(region.z),
    )
    end = FreeCAD.Vector(
        origin.x + (primary_vec.x * end_s) + (step_vec.x * t),
        origin.y + (primary_vec.y * end_s) + (step_vec.y * t),
        float(region.z),
    )

    return common.CutSegment(
        start=start,
        end=end,
        z=float(region.z),
        cut_mode=cut_mode,
        material_side="phase7_strict_raster",
        region_id=int(getattr(region, "region_id", 0)),
        pass_index=int(pass_index),
        can_reverse=False,
        original_start=common.copy_vector(start),
        original_end=common.copy_vector(end),
        strategy=strategy,
        metadata={"step_position": float(t)},
    )


def _path_command(name, params):
    """Return one Path.Command."""

    return Path.Command(name, params)


def _commands_for_motion(motion, horiz_feed, vert_feed, horiz_rapid, vert_rapid):
    """Return Path.Command objects for one planned motion."""

    commands = []
    xy_moved = not math.isclose(
        float(motion.start.x), float(motion.end.x), rel_tol=0.0, abs_tol=1e-9
    ) or not math.isclose(float(motion.start.y), float(motion.end.y), rel_tol=0.0, abs_tol=1e-9)

    if motion.kind in {common.MOTION_ENTRY_PLUNGE, common.MOTION_OUTSIDE_REENTRY}:
        command = _path_command("G1", {"Z": float(motion.z_end), "F": float(vert_feed)})
        try:
            command.Annotations = {
                "vfm_motion_kind": motion.kind,
                "vfm_is_cutting": bool(motion.is_cutting),
            }
        except Exception:
            pass
        commands.append(command)
        return commands

    if motion.kind in {common.MOTION_RETRACT, common.MOTION_EXIT}:
        command = _path_command("G0", {"Z": float(motion.z_end), "F": float(vert_rapid)})
        try:
            command.Annotations = {
                "vfm_motion_kind": motion.kind,
                "vfm_is_cutting": bool(motion.is_cutting),
            }
        except Exception:
            pass
        commands.append(command)
        return commands

    if motion.kind == common.MOTION_RAPID:
        force_xy = bool(getattr(motion, "metadata", {}).get("force_xy"))
        if not xy_moved and not force_xy:
            return commands
        params = {"F": float(horiz_rapid)}
        params["X"] = float(motion.end.x)
        params["Y"] = float(motion.end.y)
        command = _path_command("G0", params)
        try:
            command.Annotations = {
                "vfm_motion_kind": motion.kind,
                "vfm_is_cutting": bool(motion.is_cutting),
            }
        except Exception:
            pass
        commands.append(command)
        return commands

    if motion.kind in {common.MOTION_LEAD_IN, common.MOTION_CUT}:
        params = {"Z": float(motion.z_end), "F": float(horiz_feed)}
        if xy_moved:
            params["X"] = float(motion.end.x)
            params["Y"] = float(motion.end.y)
        command = _path_command("G1", params)
        try:
            command.Annotations = {
                "vfm_motion_kind": motion.kind,
                "vfm_is_cutting": bool(motion.is_cutting),
            }
        except Exception:
            pass
        commands.append(command)
        return commands

    return commands


def _make_retract_motion(current_point, clearance_height, layer_z):
    """Return a retract motion from the current cut end."""

    return common.MotionSegment(
        start=common.copy_vector(current_point),
        end=FreeCAD.Vector(current_point.x, current_point.y, float(clearance_height)),
        z_start=float(layer_z),
        z_end=float(clearance_height),
        kind=common.MOTION_RETRACT,
        layer_z=float(layer_z),
        is_cutting=False,
        is_retracted=True,
    )


def _make_rapid_motion(start, end, clearance_height, layer_z):
    """Return a horizontal rapid motion at clearance height."""

    return common.MotionSegment(
        start=FreeCAD.Vector(start.x, start.y, float(clearance_height)),
        end=FreeCAD.Vector(end.x, end.y, float(clearance_height)),
        z_start=float(clearance_height),
        z_end=float(clearance_height),
        kind=common.MOTION_RAPID,
        layer_z=float(layer_z),
        is_cutting=False,
        is_retracted=True,
    )


def _make_outside_reentry_motion(layer_entry, clearance_height):
    """Return a downward outside-stock re-entry motion."""

    return common.MotionSegment(
        start=FreeCAD.Vector(
            layer_entry.plunge_point.x,
            layer_entry.plunge_point.y,
            float(clearance_height),
        ),
        end=common.copy_vector(layer_entry.plunge_point),
        z_start=float(clearance_height),
        z_end=float(layer_entry.z),
        kind=common.MOTION_OUTSIDE_REENTRY,
        layer_z=float(layer_entry.z),
        is_cutting=False,
        is_retracted=False,
    )


def _make_initial_positioning_motion(layer_entry, safe_height):
    """Return the first explicit outside-stock XY positioning motion."""

    plunge_point = common.copy_vector(layer_entry.plunge_point)
    plunge_point.z = float(safe_height)
    return common.MotionSegment(
        start=common.copy_vector(plunge_point),
        end=common.copy_vector(plunge_point),
        z_start=float(safe_height),
        z_end=float(safe_height),
        kind=common.MOTION_RAPID,
        layer_z=float(layer_entry.z),
        is_cutting=False,
        is_retracted=True,
        metadata={"force_xy": True},
    )


def _make_lead_in_approach_motion(layer_entry, planned_lead_in_motion, tolerance=1e-6):
    """Return the outside-stock approach to the Phase 6 lead-in start, or None."""

    if planned_lead_in_motion is None:
        return None

    if common.xy_distance(layer_entry.plunge_point, planned_lead_in_motion.start) <= tolerance:
        return None

    return common.MotionSegment(
        start=common.copy_vector(layer_entry.plunge_point),
        end=common.copy_vector(planned_lead_in_motion.start),
        z_start=float(layer_entry.z),
        z_end=float(layer_entry.z),
        kind=common.MOTION_LEAD_IN,
        layer_z=float(layer_entry.z),
        is_cutting=False,
        is_retracted=False,
    )


def _make_lead_in_motion(layer_entry):
    """Return the Phase 6-planned side-aligned lead-in motion for one cut."""

    return entry.make_lead_in_motion(layer_entry)


def _make_cut_motion(cut_segment):
    """Return the cutting motion for one strict raster segment."""

    return common.MotionSegment(
        start=common.copy_vector(cut_segment.start),
        end=common.copy_vector(cut_segment.end),
        z_start=float(cut_segment.z),
        z_end=float(cut_segment.z),
        kind=common.MOTION_CUT,
        layer_z=float(cut_segment.z),
        is_cutting=True,
        is_retracted=False,
    )


def _make_exit_motion(current_point, clearance_height, layer_z):
    """Return the final layer exit retract."""

    return common.MotionSegment(
        start=common.copy_vector(current_point),
        end=FreeCAD.Vector(current_point.x, current_point.y, float(clearance_height)),
        z_start=float(layer_z),
        z_end=float(clearance_height),
        kind=common.MOTION_EXIT,
        layer_z=float(layer_z),
        is_cutting=False,
        is_retracted=True,
    )


def generate(
    regions,
    stock_boundbox,
    tool_diameter,
    stepover_percent,
    cut_mode,
    angle_degrees,
    entry_plan,
    clearance_height,
    safe_height,
    horiz_feed,
    vert_feed,
    horiz_rapid,
    vert_rapid,
    optimization_mode="None",
    protected_regions=None,
    tolerance=1e-6,
):
    """Generate the conservative Phase 7 StrictRaster strategy metadata."""

    result = common.StrategyResult(strategy="StrictRaster")
    result.metadata["optimization_mode"] = str(optimization_mode)

    if not regions:
        return result

    if float(tool_diameter) <= 0.0:
        result.validation_errors.append("Tool diameter must be greater than zero")
        return result

    if float(stepover_percent) <= 0.0:
        result.validation_errors.append("StepOver must be greater than zero")
        return result

    if str(cut_mode) not in {"Climb", "Conventional"}:
        result.validation_errors.append(f"Unsupported cut mode for StrictRaster: {cut_mode}")
        return result

    protected_regions = protected_regions or []
    primary_vec, step_vec = _unit_vectors(angle_degrees)
    origin = FreeCAD.Vector(0.0, 0.0, 0.0)
    tool_radius = float(tool_diameter) / 2.0

    layer_groups = {}
    for region in regions or []:
        layer_groups.setdefault(round(float(region.z), 6), []).append(region)

    for layer_z in sorted(layer_groups.keys(), reverse=True):
        layer_regions = list(layer_groups[layer_z])
        cut_segments = []

        for region in layer_regions:
            pass_index = 0
            for t in _step_positions(
                region,
                step_vec,
                float(tool_diameter),
                float(stepover_percent),
                origin,
            ):
                intervals = _region_intervals_at_t(region, primary_vec, step_vec, t, origin)
                for interval in intervals:
                    cut_segment = _cut_segment_from_interval(
                        region,
                        primary_vec,
                        step_vec,
                        origin,
                        t,
                        interval,
                        cut_mode,
                        pass_index,
                        "StrictRaster",
                    )
                    if common.cut_segment_length(cut_segment) <= tolerance:
                        continue
                    cut_segments.append(cut_segment)
                    pass_index += 1

        cut_segments.sort(
            key=lambda cut: (
                int(cut.region_id),
                int(cut.pass_index),
                float(cut.start.y),
                float(cut.start.x),
            )
        )
        if not cut_segments:
            continue

        clear_state = common.LayerClearState(z=float(layer_z))
        layer_plan = common.LayerPlan(
            z=float(layer_z),
            regions=list(layer_regions),
            cut_segments=list(cut_segments),
            cleared_state=clear_state,
        )

        current_point = None

        for index, cut_segment in enumerate(cut_segments):
            layer_entry = entry.make_layer_entry(
                entry_plan,
                float(layer_z),
                first_cut_start=cut_segment.start,
            )
            motions = []

            if index == 0:
                motions.append(_make_initial_positioning_motion(layer_entry, float(safe_height)))
                motions.append(entry.make_entry_plunge_motion(layer_entry, float(safe_height)))
            else:
                retract_motion = _make_retract_motion(
                    current_point,
                    float(clearance_height),
                    float(layer_z),
                )
                rapid_motion = _make_rapid_motion(
                    retract_motion.end,
                    layer_entry.plunge_point,
                    float(clearance_height),
                    float(layer_z),
                )
                motions.append(retract_motion)
                if not math.isclose(
                    common.motion_length(rapid_motion), 0.0, rel_tol=0.0, abs_tol=tolerance
                ):
                    motions.append(rapid_motion)
                motions.append(_make_outside_reentry_motion(layer_entry, float(clearance_height)))

            lead_in_motion = _make_lead_in_motion(layer_entry)
            lead_in_approach = _make_lead_in_approach_motion(
                layer_entry,
                lead_in_motion,
                tolerance=tolerance,
            )
            if lead_in_approach is not None:
                motions.append(lead_in_approach)
            if lead_in_motion is not None:
                motions.append(lead_in_motion)

            cut_motion = _make_cut_motion(cut_segment)
            motions.append(cut_motion)

            for motion in motions:
                motion.commands = _commands_for_motion(
                    motion,
                    float(horiz_feed),
                    float(vert_feed),
                    float(horiz_rapid),
                    float(vert_rapid),
                )
                layer_plan.motions.append(motion)
                result.commands.extend(motion.commands)

            cut_segment.commands = list(cut_motion.commands)
            clear_state.cleared_segments.append(cut_segment)
            footprint = validation.swept_cut_footprint(cut_segment, tool_radius)
            if clear_state.cleared_region is None:
                clear_state.cleared_region = footprint
            else:
                try:
                    clear_state.cleared_region = clear_state.cleared_region.fuse(footprint)
                except Exception:
                    pass

            current_point = common.copy_vector(cut_segment.end)

        if current_point is not None:
            exit_motion = _make_exit_motion(current_point, float(clearance_height), float(layer_z))
            exit_motion.commands = _commands_for_motion(
                exit_motion,
                float(horiz_feed),
                float(vert_feed),
                float(horiz_rapid),
                float(vert_rapid),
            )
            layer_plan.motions.append(exit_motion)
            result.commands.extend(exit_motion.commands)

        result.layers.append(layer_plan)

    all_motions = [motion for layer in result.layers for motion in layer.motions]
    result.cutting_length = sum(
        common.motion_length(motion) for motion in all_motions if motion.kind == common.MOTION_CUT
    )
    result.rapid_length = sum(
        common.motion_length(motion) for motion in all_motions if motion.kind != common.MOTION_CUT
    )
    result.retract_count = sum(1 for motion in all_motions if motion.kind == common.MOTION_RETRACT)

    errors = validation.validate_strategy_result(
        result,
        stock_boundbox=stock_boundbox,
        expected_cut_mode=cut_mode,
        tool_radius=tool_radius,
        entry_clearance=entry_plan.entry_clearance,
        protected_regions=protected_regions or [],
        tolerance=tolerance,
    )
    for error in errors:
        if error not in result.validation_errors:
            result.validation_errors.append(error)

    return result
