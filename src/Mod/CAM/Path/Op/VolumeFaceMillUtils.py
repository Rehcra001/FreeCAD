# SPDX-License-Identifier: LGPL-2.1-or-later

import math

import FreeCAD
import Path
import PathScripts.PathUtils as PathUtils

from lazy_loader.lazy_loader import LazyLoader
from types import SimpleNamespace

Part = LazyLoader("Part", globals(), "Part")


def find_job(obj):
    """Return the parent CAM Job for obj, or None."""

    return PathUtils.findParentJob(obj)


def has_valid_stock(obj):
    """Return True if obj has a parent Job with a valid stock shape."""

    stock_bb = get_stock_boundbox(obj)
    if stock_bb is None:
        return False

    return stock_bb.XLength > 0 and stock_bb.YLength > 0 and stock_bb.ZLength > 0


def get_stock_shape(obj):
    """Return a copy of the parent Job stock shape, or None if invalid."""

    job = find_job(obj)
    if not job:
        Path.Log.warning("Volume Face Mill requires a valid Job stock.")
        return None

    stock = getattr(job, "Stock", None)
    if stock is None:
        Path.Log.warning("Volume Face Mill requires a valid Job stock.")
        return None

    shape = getattr(stock, "Shape", None)
    if shape is None or (hasattr(shape, "isNull") and shape.isNull()):
        Path.Log.warning("Volume Face Mill requires a valid Job stock.")
        return None

    bb = shape.BoundBox
    if bb.XLength <= 0 or bb.YLength <= 0 or bb.ZLength <= 0:
        Path.Log.warning("Volume Face Mill requires a valid Job stock.")
        return None

    return shape.copy()


def get_stock_boundbox(obj):
    """Return the parent Job stock BoundBox, or None if invalid."""

    stock_shape = get_stock_shape(obj)
    if stock_shape is None:
        return None
    return stock_shape.BoundBox


def iter_selected_subobjects(obj):
    """Yield selected Base subobjects as dictionaries with base, name, and shape."""

    for base, sub_names in getattr(obj, "Base", []):
        base_shape = getattr(base, "Shape", None)
        if base_shape is None or (hasattr(base_shape, "isNull") and base_shape.isNull()):
            continue

        if not sub_names:
            yield {
                "base": base,
                "name": "",
                "shape": base_shape,
            }
            continue

        for sub_name in sub_names:
            try:
                sub_shape = base_shape.getElement(sub_name)
            except Exception as exc:
                Path.Log.warning(f"Could not read selected subshape {sub_name}: {exc}")
                continue

            yield {
                "base": base,
                "name": sub_name,
                "shape": sub_shape,
            }


def is_horizontal_face(face, tolerance=1e-6):
    """Return True if face is approximately horizontal."""

    if not isinstance(face, Part.Face):
        return False

    try:
        u_min, u_max, v_min, v_max = face.ParameterRange
        normal = face.normalAt(
            0.5 * (u_min + u_max),
            0.5 * (v_min + v_max),
        )
    except Exception:
        return False

    return math.isclose(abs(normal.z), 1.0, rel_tol=0.0, abs_tol=tolerance)


def face_z(face):
    """Return a stable Z value for a horizontal face."""

    return 0.5 * (face.BoundBox.ZMin + face.BoundBox.ZMax)


def selected_horizontal_faces(obj):
    """Return selected horizontal faces from obj.Base."""

    horizontal_faces = []

    for selected in iter_selected_subobjects(obj):
        shape = selected["shape"]
        if isinstance(shape, Part.Face) and is_horizontal_face(shape):
            horizontal_faces.append(shape)

    return horizontal_faces


def has_selected_geometry(obj):
    """Return whether the operation has any readable selected base geometry."""

    return any(True for _selected in iter_selected_subobjects(obj))


def allowance_distance_value(obj, prop_name):
    """Return a non-negative numeric allowance distance value."""

    try:
        value = getattr(obj, prop_name)
    except Exception:
        return 0.0

    try:
        value = float(getattr(value, "Value", value))
    except Exception:
        return 0.0

    return max(0.0, value)


def allowance_values(obj):
    """Return the current non-negative allowance distances."""

    return {
        "feature_xy": allowance_distance_value(obj, "FeatureAllowanceXY"),
        "feature_z": allowance_distance_value(obj, "FeatureAllowanceZ"),
        "stock_xy": allowance_distance_value(obj, "StockAllowanceXY"),
        "stock_z": allowance_distance_value(obj, "StockAllowanceZ"),
    }


def feature_allowance_is_active(obj, tolerance=1e-6):
    """Return whether non-zero feature allowance is active."""

    allowances = allowance_values(obj)
    return allowances["feature_xy"] > tolerance or allowances["feature_z"] > tolerance


def resolve_target_faces_and_final_depth(obj, stock_shape, tolerance=1e-6):
    """Return (target_faces, final_depth) from Base selection and stock extents."""

    stock_bb = stock_shape.BoundBox
    has_selection = has_selected_geometry(obj)
    horizontal_faces = selected_horizontal_faces(obj)
    allowances = allowance_values(obj)

    if horizontal_faces:
        lowest_z = min(face_z(face) for face in horizontal_faces)
        target_faces = [
            face
            for face in horizontal_faces
            if math.isclose(face_z(face), lowest_z, rel_tol=0.0, abs_tol=tolerance)
        ]
        final_depth = lowest_z + allowances["feature_z"]
    else:
        if has_selection:
            Path.Log.warning(
                "No selected horizontal target face found; final depth defaults to stock bottom."
            )
        target_faces = []
        final_depth = stock_bb.ZMin + allowances["stock_z"]

    final_depth = max(stock_bb.ZMin, min(final_depth, stock_bb.ZMax))
    return target_faces, final_depth


def set_distance_property(obj, prop_name, value):
    """Set a distance-like property to a numeric value in a FreeCAD-safe way."""

    if not hasattr(obj, prop_name):
        return

    try:
        current = getattr(obj, prop_name)
        current_value = getattr(current, "Value", current)
        if Path.Geom.isRoughly(current_value, value):
            return
    except Exception:
        pass

    try:
        obj.setExpression(prop_name, None)
    except Exception:
        pass

    try:
        prop = getattr(obj, prop_name)
        prop.Value = value
        return
    except Exception:
        pass

    try:
        setattr(obj, prop_name, value)
    except Exception as exc:
        Path.Log.warning(f"Unable to set {prop_name}: {exc}")


def sync_stock_depths(obj):
    """Force operation depth defaults/effective values from stock and selected target faces."""

    stock_shape = get_stock_shape(obj)
    if stock_shape is None:
        return False

    stock_bb = stock_shape.BoundBox
    _target_faces, final_depth = resolve_target_faces_and_final_depth(obj, stock_shape)
    start_depth = stock_bb.ZMax

    set_distance_property(obj, "OpStockZMax", stock_bb.ZMax)
    set_distance_property(obj, "OpStockZMin", stock_bb.ZMin)
    set_distance_property(obj, "OpStartDepth", start_depth)
    set_distance_property(obj, "OpFinalDepth", final_depth)
    set_distance_property(obj, "StartDepth", start_depth)
    set_distance_property(obj, "FinalDepth", final_depth)
    return True


def get_model_compound(model):
    """Return a compound of Job model shapes, or None."""

    shapes = []

    for base in model or []:
        shape = getattr(base, "Shape", None)
        if shape is None or (hasattr(shape, "isNull") and shape.isNull()):
            continue
        shapes.append(shape)

    if not shapes:
        return None

    return shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)


def _shape_has_volume(shape):
    """Return whether shape is a volumetric keepout already."""

    try:
        return abs(shape.Volume) > 1e-9
    except Exception:
        return False


def _shape_volume(shape):
    """Return a safe absolute volume for shape."""

    try:
        return abs(shape.Volume)
    except Exception:
        return 0.0


def _protected_covers_removal(removal, protected_shape):
    """Return whether protected_shape consumes all of removal within tolerance."""

    removal_volume = _shape_volume(removal)
    if removal_volume <= 1e-9:
        return True

    protected_volume = _shape_volume(protected_shape)
    tolerance = max(1e-6, removal_volume * 1e-6)
    return protected_volume >= (removal_volume - tolerance)


def _cut_protected_overlap(removal, protected_overlap):
    """Subtract protected overlap robustly, returning an empty shape when fully consumed."""

    try:
        return removal.cut(protected_overlap)
    except Exception as exc:
        last_exc = exc
        if _protected_covers_removal(removal, protected_overlap):
            return Part.Shape()

    try:
        cleaned_overlap = protected_overlap.removeSplitter()
        return removal.cut(cleaned_overlap)
    except Exception as exc:
        last_exc = exc
        if _protected_covers_removal(removal, protected_overlap):
            return Part.Shape()

    solids = [
        solid for solid in getattr(protected_overlap, "Solids", []) if _shape_has_volume(solid)
    ]
    if not solids:
        raise last_exc

    current = removal
    for solid in solids:
        if _protected_covers_removal(current, solid):
            return Part.Shape()
        current = current.cut(solid)

    return current


def _selected_keepout_depthparams(obj, depthparams):
    """Return a minimal depth window object suitable for PathUtils.getEnvelope()."""

    if depthparams is not None:
        return depthparams

    return SimpleNamespace(
        safe_height=max(obj.StartDepth.Value, obj.FinalDepth.Value),
        final_depth=min(obj.StartDepth.Value, obj.FinalDepth.Value),
    )


def _selected_keepout_uses_whole_base(obj, base):
    """Return whether a selected keepout should conservatively use the whole base shape."""

    job = find_job(obj)
    model_group = getattr(getattr(job, "Model", None), "Group", []) if job else []
    return base not in model_group


def _build_selected_feature_volume(base_shape, profile_shapes, keepout_depthparams):
    """Return actual protected model volume under the selected profile footprint."""

    if shape_is_empty(base_shape) or not profile_shapes:
        return None

    selection_shape = (
        profile_shapes[0] if len(profile_shapes) == 1 else Part.makeCompound(profile_shapes)
    )

    try:
        selection_envelope = PathUtils.getEnvelope(
            partshape=base_shape,
            subshape=selection_shape,
            depthparams=keepout_depthparams,
        )
    except Exception as exc:
        Path.Log.warning(f"Could not create selection envelope for protected geometry: {exc}")
        return None

    try:
        selected_volume = base_shape.common(selection_envelope)
    except Exception as exc:
        Path.Log.warning(f"Could not intersect selected envelope with model volume: {exc}")
        return None

    if not _shape_has_volume(selected_volume):
        return None

    try:
        return selected_volume.removeSplitter()
    except Exception as exc:
        Path.Log.debug(f"removeSplitter failed for selected feature volume: {exc}")
        return selected_volume


def is_target_face(shape, target_faces):
    """Return True if shape matches any resolved target face."""

    for target in target_faces:
        try:
            if shape.isSame(target):
                return True
        except Exception:
            continue

    return False


def selected_keepout_shapes(obj, target_faces, depthparams):
    """Return additional selected keepout volumes, excluding target-depth faces."""

    if not getattr(obj, "ProtectSelectedFeatures", False):
        return []

    keepout_depthparams = _selected_keepout_depthparams(obj, depthparams)
    grouped = {}

    for selected in iter_selected_subobjects(obj):
        base = selected["base"]
        shape = selected["shape"]

        if is_target_face(shape, target_faces):
            continue

        grouped.setdefault(base, []).append(shape)

    keepout_shapes = []

    for base, shapes in grouped.items():
        base_shape = getattr(base, "Shape", None)
        if base_shape is None or (hasattr(base_shape, "isNull") and base_shape.isNull()):
            continue

        if _shape_has_volume(base_shape) and _selected_keepout_uses_whole_base(obj, base):
            keepout_shapes.append(base_shape)
            continue

        volumetric_shapes = [shape for shape in shapes if _shape_has_volume(shape)]
        profile_shapes = [shape for shape in shapes if not _shape_has_volume(shape)]

        keepout_shapes.extend(volumetric_shapes)

        if not profile_shapes:
            continue

        selected_volume = _build_selected_feature_volume(
            base_shape, profile_shapes, keepout_depthparams
        )
        if selected_volume is not None:
            keepout_shapes.append(selected_volume)
            continue

        for profile_shape in profile_shapes:
            try:
                keepout = PathUtils.getEnvelope(
                    partshape=base_shape,
                    subshape=profile_shape,
                    depthparams=keepout_depthparams,
                )
            except Exception as exc:
                Path.Log.warning(f"Could not create keepout envelope for selected geometry: {exc}")
                continue

            if not shape_is_empty(keepout):
                keepout_shapes.append(keepout)

    return keepout_shapes


def effective_clear_edges(clear_edges, stock_allowance_xy, tolerance=1e-6):
    """Return whether edge overhang may be used after stock allowance rules."""

    if clear_edges and stock_allowance_xy > tolerance:
        Path.Log.warning("ClearEdges is disabled when StockAllowanceXY is non-zero.")
        return False
    return clear_edges


def build_boundary_volume(
    stock_shape,
    final_depth,
    tool_radius,
    clear_edges,
    stock_allowance_xy=0.0,
    z_margin=0.001,
    tolerance=1e-6,
):
    """Build the stock-extents machining boundary volume."""

    bb = stock_shape.BoundBox
    stock_allowance_xy = max(0.0, stock_allowance_xy)
    x_min = bb.XMin + stock_allowance_xy
    x_max = bb.XMax - stock_allowance_xy
    y_min = bb.YMin + stock_allowance_xy
    y_max = bb.YMax - stock_allowance_xy

    if (x_max - x_min) <= tolerance or (y_max - y_min) <= tolerance:
        Path.Log.warning("Stock allowance consumes all machinable stock boundary.")
        return None

    allow_edge_overhang = clear_edges and math.isclose(
        stock_allowance_xy, 0.0, rel_tol=0.0, abs_tol=tolerance
    )
    xy_margin = (tool_radius + 0.1) if allow_edge_overhang else 0.0

    z_min = max(min(final_depth, bb.ZMax), bb.ZMin)
    z_max = bb.ZMax
    height = z_max - z_min

    if height <= tolerance:
        Path.Log.warning("Allowance leaves no machinable stock depth.")
        return None

    return Part.makeBox(
        (x_max - x_min) + (2.0 * xy_margin),
        (y_max - y_min) + (2.0 * xy_margin),
        height + z_margin,
        FreeCAD.Vector(
            x_min - xy_margin,
            y_min - xy_margin,
            z_min - z_margin,
        ),
    )


def build_layer_boundary_volume(
    stock_shape,
    layer_top_z,
    layer_bottom_z,
    tool_radius,
    clear_edges,
    stock_allowance_xy=0.0,
    z_margin=0.001,
    tolerance=1e-6,
):
    """Build one stock-derived removal slab for a specific cutting layer."""

    bb = stock_shape.BoundBox
    stock_allowance_xy = max(0.0, stock_allowance_xy)
    x_min = bb.XMin + stock_allowance_xy
    x_max = bb.XMax - stock_allowance_xy
    y_min = bb.YMin + stock_allowance_xy
    y_max = bb.YMax - stock_allowance_xy

    if (x_max - x_min) <= tolerance or (y_max - y_min) <= tolerance:
        Path.Log.warning("Stock allowance consumes all machinable stock boundary.")
        return None

    allow_edge_overhang = clear_edges and math.isclose(
        stock_allowance_xy, 0.0, rel_tol=0.0, abs_tol=tolerance
    )
    xy_margin = (tool_radius + 0.1) if allow_edge_overhang else 0.0

    z_top = max(min(layer_top_z, bb.ZMax), bb.ZMin)
    z_bottom = max(min(layer_bottom_z, bb.ZMax), bb.ZMin)
    if z_top < z_bottom:
        z_top, z_bottom = z_bottom, z_top

    height = max(z_top - z_bottom, tolerance)
    return Part.makeBox(
        (x_max - x_min) + (2.0 * xy_margin),
        (y_max - y_min) + (2.0 * xy_margin),
        height + (2.0 * z_margin),
        FreeCAD.Vector(
            x_min - xy_margin,
            y_min - xy_margin,
            z_bottom - z_margin,
        ),
    )


def _depth_levels_for_allowance(depthparams, start_depth, final_depth, tolerance=1e-6):
    """Return descending cut levels used by the current operation."""

    values = []
    if depthparams is not None and hasattr(depthparams, "data"):
        try:
            values.extend(float(value) for value in depthparams.data)
        except Exception:
            pass

    values.extend([float(start_depth), float(final_depth)])
    filtered = []
    for value in values:
        if value < (final_depth - tolerance) or value > (start_depth + tolerance):
            continue
        filtered.append(value)

    filtered.sort(reverse=True)
    levels = []
    for value in filtered:
        if not levels or not math.isclose(value, levels[-1], rel_tol=0.0, abs_tol=tolerance):
            levels.append(value)

    return levels


def _layer_interval_pairs(depth_levels, final_depth, tolerance=1e-6):
    """Return [(layer_top_z, layer_bottom_z), ...] for allowance-aware slicing."""

    del final_depth

    intervals = []
    for layer_top_z, layer_bottom_z in zip(depth_levels, depth_levels[1:]):
        if (layer_top_z - layer_bottom_z) <= tolerance:
            continue
        intervals.append((layer_top_z, layer_bottom_z))

    return intervals


def _shape_has_section_geometry(shape):
    """Return whether shape has usable section geometry even without volume."""

    if shape is None:
        return False

    if hasattr(shape, "isNull") and shape.isNull():
        return False

    if getattr(shape, "Solids", []):
        return True

    if getattr(shape, "Faces", []):
        return True

    if getattr(shape, "Edges", []):
        return True

    return False


def _protected_slab(protected_shape, stock_shape, layer_top_z, feature_allowance_z, tolerance=1e-6):
    """Return the protected geometry slab that governs one cutting layer."""

    if shape_is_empty(protected_shape):
        return None

    bb = stock_shape.BoundBox
    if feature_allowance_z <= tolerance:
        slab_bottom = max(bb.ZMin, layer_top_z - tolerance)
        slab_top = min(bb.ZMax, layer_top_z + tolerance)
    else:
        slab_bottom = max(bb.ZMin, layer_top_z - feature_allowance_z)
        slab_top = max(min(layer_top_z, bb.ZMax), bb.ZMin)

    if slab_top < slab_bottom:
        slab_top, slab_bottom = slab_bottom, slab_top

    slab_box = Part.makeBox(
        bb.XLength,
        bb.YLength,
        max(slab_top - slab_bottom, tolerance),
        FreeCAD.Vector(bb.XMin, bb.YMin, slab_bottom),
    )

    try:
        protected_slab = protected_shape.common(slab_box)
    except Exception as exc:
        Path.Log.warning(f"Failed to build protected slab for allowance layer: {exc}")
        return None

    if not _shape_has_section_geometry(protected_slab):
        return None

    return protected_slab


def _layer_keepout_footprint(protected_slab, stock_shape, feature_allowance_xy):
    """Return the XY keepout footprint for one allowance-aware cutting layer."""

    try:
        offset_area = PathUtils.getOffsetArea(
            protected_slab,
            feature_allowance_xy,
            plane=stock_shape,
        )
    except Exception as exc:
        Path.Log.warning(f"Failed to offset protected geometry for allowance: {exc}")
        return None

    if not offset_area or not getattr(offset_area, "Faces", []):
        Path.Log.warning("Failed to derive a protected keepout footprint for allowance.")
        return None

    return offset_area


def _extrude_keepout_footprint(
    offset_area,
    layer_top_z,
    layer_bottom_z,
    feature_allowance_z,
    z_margin=0.001,
    tolerance=1e-6,
):
    """Return a volumetric keepout from one 2D offset footprint."""

    del feature_allowance_z

    height = max(layer_top_z - layer_bottom_z, tolerance) + (2.0 * z_margin)
    solids = []

    for face in offset_area.Faces:
        moved_face = face.copy()
        moved_face.translate(
            FreeCAD.Vector(0.0, 0.0, (layer_bottom_z - z_margin) - face.BoundBox.ZMin)
        )
        extruded = moved_face.extrude(FreeCAD.Vector(0.0, 0.0, height))
        if not shape_is_empty(extruded):
            solids.append(extruded)

    if not solids:
        Path.Log.warning("Failed to extrude a protected keepout for allowance.")
        return None

    if len(solids) == 1:
        return solids[0]
    return Part.makeCompound(solids)


def build_layered_allowance_removal_volume(
    obj,
    model,
    stock_shape,
    tool_radius,
    depthparams,
    allowances,
    target_faces,
    final_depth,
    tolerance=1e-6,
):
    """Build per-layer removal slabs for non-zero feature allowance.

    This returns ``[(cut_depth_z, layer_shape), ...]`` for the custom
    Volume Face Mill allowance executor. Non-zero feature allowance cannot use
    the default one-volume Path.Area slicing path safely, because each cutting
    layer needs its own stock boundary and protected keepout subtraction before
    toolpath generation.
    """

    stock_bb = stock_shape.BoundBox
    start_depth = stock_bb.ZMax
    if final_depth >= (start_depth - tolerance):
        Path.Log.warning("Allowance leaves no machinable stock depth.")
        return None

    clear_edges = effective_clear_edges(
        getattr(obj, "ClearEdges", False),
        allowances["stock_xy"],
        tolerance=tolerance,
    )
    depth_levels = _depth_levels_for_allowance(
        depthparams, start_depth, final_depth, tolerance=tolerance
    )
    layer_intervals = _layer_interval_pairs(depth_levels, final_depth, tolerance=tolerance)
    if not layer_intervals:
        Path.Log.warning("Allowance leaves no machinable cutting layers.")
        return None

    protected_shapes = build_protected_shapes(
        obj=obj,
        model=model,
        target_faces=target_faces,
        depthparams=depthparams,
    )

    removal_layers = []
    for layer_top_z, layer_bottom_z in layer_intervals:
        layer_boundary = build_layer_boundary_volume(
            stock_shape=stock_shape,
            layer_top_z=layer_top_z,
            layer_bottom_z=layer_bottom_z,
            tool_radius=tool_radius,
            clear_edges=clear_edges,
            stock_allowance_xy=allowances["stock_xy"],
            tolerance=tolerance,
        )
        if layer_boundary is None:
            return None

        if not protected_shapes:
            if not shape_is_empty(layer_boundary):
                removal_layers.append((layer_bottom_z, layer_boundary))
            continue

        for protected_shape in protected_shapes:
            protected_slab = _protected_slab(
                protected_shape,
                stock_shape,
                layer_bottom_z,
                allowances["feature_z"],
                tolerance=tolerance,
            )
            if protected_slab is None:
                continue

            offset_area = _layer_keepout_footprint(
                protected_slab,
                stock_shape,
                allowances["feature_xy"],
            )
            if offset_area is None:
                return None

            layer_keepout = _extrude_keepout_footprint(
                offset_area,
                layer_top_z,
                layer_bottom_z,
                allowances["feature_z"],
                tolerance=tolerance,
            )
            if layer_keepout is None:
                return None

            try:
                protected_overlap = layer_boundary.common(layer_keepout)
            except Exception as exc:
                Path.Log.warning(f"Failed to clip allowance keepout to the layer boundary: {exc}")
                protected_overlap = layer_keepout

            if not _shape_has_volume(protected_overlap):
                continue

            try:
                layer_boundary = _cut_protected_overlap(layer_boundary, protected_overlap)
            except Exception as exc:
                Path.Log.error(f"Failed to subtract allowance keepout from a layer boundary: {exc}")
                return None

        if not shape_is_empty(layer_boundary):
            removal_layers.append((layer_bottom_z, layer_boundary))

    if not removal_layers:
        return None

    return removal_layers


def build_allowance_layer_volumes(obj, model, tool_radius, depthparams):
    """Return per-layer removal volumes for the custom feature-allowance executor."""

    stock_shape = get_stock_shape(obj)
    if stock_shape is None:
        return None

    allowances = allowance_values(obj)
    target_faces, final_depth = resolve_target_faces_and_final_depth(obj, stock_shape)
    return build_layered_allowance_removal_volume(
        obj=obj,
        model=model,
        stock_shape=stock_shape,
        tool_radius=tool_radius,
        depthparams=depthparams,
        allowances=allowances,
        target_faces=target_faces,
        final_depth=final_depth,
    )


def build_protected_shapes(obj, model, target_faces, depthparams):
    """Return protected model and selected keepout shapes as separate sources."""

    protected_shapes = []

    model_shape = get_model_compound(model)
    if model_shape is not None:
        protected_shapes.append(model_shape)

    protected_shapes.extend(selected_keepout_shapes(obj, target_faces, depthparams))
    return [shape for shape in protected_shapes if not shape_is_empty(shape)]


def build_protected_shape(obj, model, target_faces, depthparams):
    """Build the always-protected model plus optional selected keepout geometry."""

    protected_shapes = build_protected_shapes(obj, model, target_faces, depthparams)

    if not protected_shapes:
        return None

    if len(protected_shapes) == 1:
        return protected_shapes[0]

    return Part.makeCompound(protected_shapes)


def shape_is_empty(shape):
    """Return True if shape is None, null, or has unusable extents."""

    if shape is None:
        return True

    if hasattr(shape, "isNull") and shape.isNull():
        return True

    bb = shape.BoundBox
    return bb.XLength <= 0 or bb.YLength <= 0 or bb.ZLength <= 0


def build_removal_volume(obj, model, tool_radius, depthparams):
    """Build stock-extents boundary volume minus protected model/keepouts."""

    stock_shape = get_stock_shape(obj)
    if stock_shape is None:
        return None

    allowances = allowance_values(obj)
    target_faces, final_depth = resolve_target_faces_and_final_depth(obj, stock_shape)
    clear_edges = effective_clear_edges(
        getattr(obj, "ClearEdges", False),
        allowances["stock_xy"],
    )

    if feature_allowance_is_active(obj):
        layer_volumes = build_allowance_layer_volumes(
            obj=obj,
            model=model,
            tool_radius=tool_radius,
            depthparams=depthparams,
        )
        if not layer_volumes:
            return None

        shapes = [shape for _layer_top_z, shape in layer_volumes]
        return shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)

    boundary = build_boundary_volume(
        stock_shape=stock_shape,
        final_depth=final_depth,
        tool_radius=tool_radius,
        clear_edges=clear_edges,
        stock_allowance_xy=allowances["stock_xy"],
    )
    if boundary is None:
        return None

    protected = build_protected_shape(
        obj=obj,
        model=model,
        target_faces=target_faces,
        depthparams=depthparams,
    )

    if protected is not None:
        try:
            protected_overlap = boundary.common(protected)
        except Exception as exc:
            Path.Log.warning(
                f"Failed to clip protected model/features to the removal volume: {exc}"
            )
            protected_overlap = protected

        if _shape_has_volume(protected_overlap):
            try:
                boundary = _cut_protected_overlap(boundary, protected_overlap)
            except Exception as exc:
                Path.Log.error(f"Failed to subtract protected model/features from stock: {exc}")
                return None

    exact_boundary = build_boundary_volume(
        stock_shape=stock_shape,
        final_depth=final_depth,
        tool_radius=tool_radius,
        clear_edges=clear_edges,
        stock_allowance_xy=allowances["stock_xy"],
        z_margin=0.0,
    )
    if exact_boundary is not None:
        try:
            boundary = boundary.common(exact_boundary)
        except Exception as exc:
            Path.Log.debug(f"Failed to trim removal volume to exact depth range: {exc}")

    try:
        boundary = boundary.removeSplitter()
    except Exception as exc:
        Path.Log.debug(f"removeSplitter failed for removal volume: {exc}")

    return boundary
