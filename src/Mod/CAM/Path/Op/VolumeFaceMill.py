# SPDX-License-Identifier: LGPL-2.1-or-later

import math

import FreeCAD
import Path
import Path.Op.Area as PathAreaOp
import Path.Op.PocketBase as PathPocketBase
import Path.Op.VolumeFaceMillUtils as VolumeFaceMillUtils
import PathScripts.PathUtils as PathUtils

from PySide.QtCore import QT_TRANSLATE_NOOP
from lazy_loader.lazy_loader import LazyLoader

Part = LazyLoader("Part", globals(), "Part")

__title__ = "CAM Volume Face Mill Operation"
__author__ = "OpenAI Codex"
__url__ = "https://www.freecad.org"
__doc__ = "Class and implementation of stock-aware Volume Face Mill operation."


if False:
    Path.Log.setLevel(Path.Log.Level.DEBUG, Path.Log.thisModule())
    Path.Log.trackModule(Path.Log.thisModule())
else:
    Path.Log.setLevel(Path.Log.Level.INFO, Path.Log.thisModule())

translate = FreeCAD.Qt.translate

_ALLOWANCE_MODE_DEFAULT = "Linked"
_ALLOWANCE_MODE_PROPERTIES = (
    "FeatureAllowanceMode",
    "StockAllowanceMode",
)
_ALLOWANCE_GROUPS = (
    ("FeatureAllowanceMode", "FeatureAllowanceXY", "FeatureAllowanceZ"),
    ("StockAllowanceMode", "StockAllowanceXY", "StockAllowanceZ"),
)
_ALLOWANCE_DISTANCE_PROPERTIES = (
    "FeatureAllowanceXY",
    "FeatureAllowanceZ",
    "StockAllowanceXY",
    "StockAllowanceZ",
)
_LEGACY_ALLOWANCE_XY_PROPERTIES = {
    "FeatureAllowanceXY": ("FeatureAllowanceX", "FeatureAllowanceY"),
    "StockAllowanceXY": ("StockAllowanceX", "StockAllowanceY"),
}


class ObjectVolumeFaceMill(PathPocketBase.ObjectPocket):
    """Proxy object for stock-aware Volume Face Mill operation."""

    def __init__(self, obj, name, parentJob=None):
        self._initialize_runtime_state()
        super().__init__(obj, name, parentJob)

    def _initialize_runtime_state(self):
        """Initialize transient proxy state that is not persisted in documents."""

        self._forcing_compatibility_properties = False
        self._pending_standard_abort = None
        self._syncing_allowances = False
        self._syncing_depths = False

    def _ensure_runtime_state(self):
        """Restore transient proxy state for document-restored proxies."""

        if not hasattr(self, "_forcing_compatibility_properties"):
            self._forcing_compatibility_properties = False
        if not hasattr(self, "_pending_standard_abort"):
            self._pending_standard_abort = None
        if not hasattr(self, "_syncing_allowances"):
            self._syncing_allowances = False
        if not hasattr(self, "_syncing_depths"):
            self._syncing_depths = False

    @classmethod
    def propertyEnumerations(cls, dataType="data"):
        """Return Volume Face Mill property enumeration lists."""

        enums = {
            "BoundaryShape": [
                (translate("CAM_VolumeFaceMill", "Stock"), "Stock"),
            ],
            "OptimizationMode": [
                (translate("CAM_VolumeFaceMill", "None"), "None"),
                (translate("CAM_VolumeFaceMill", "Min Travel"), "MinTravel"),
            ],
            "FeatureAllowanceMode": [
                (translate("CAM_VolumeFaceMill", "Linked"), "Linked"),
                (translate("CAM_VolumeFaceMill", "Independent"), "Independent"),
            ],
            "StockAllowanceMode": [
                (translate("CAM_VolumeFaceMill", "Linked"), "Linked"),
                (translate("CAM_VolumeFaceMill", "Independent"), "Independent"),
            ],
        }

        if dataType == "raw":
            return enums

        data = []
        idx = 0 if dataType == "translated" else 1

        for name in enums:
            data.append((name, [tup[idx] for tup in enums[name]]))

        return data

    def _add_properties(self, obj):
        """Add missing Volume Face Mill properties."""

        added_properties = set()

        if not hasattr(obj, "BoundaryShape"):
            obj.addProperty(
                "App::PropertyEnumeration",
                "BoundaryShape",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Internal compatibility boundary mode.",
                ),
            )
            added_properties.add("BoundaryShape")

        if not hasattr(obj, "ProtectModel"):
            obj.addProperty(
                "App::PropertyBool",
                "ProtectModel",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP("App::Property", "Internal compatibility model-protection flag."),
            )
            added_properties.add("ProtectModel")

        if not hasattr(obj, "ProtectSelectedFeatures"):
            obj.addProperty(
                "App::PropertyBool",
                "ProtectSelectedFeatures",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Protect selected non-target geometry as additional keepout.",
                ),
            )
            added_properties.add("ProtectSelectedFeatures")

        if not hasattr(obj, "ClearEdges"):
            obj.addProperty(
                "App::PropertyBool",
                "ClearEdges",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Allow the tool center to overhang stock extents to clear stock edges.",
                ),
            )
            added_properties.add("ClearEdges")

        if not hasattr(obj, "OptimizationMode"):
            obj.addProperty(
                "App::PropertyEnumeration",
                "OptimizationMode",
                "Path",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Optimization mode for ordering generated paths.",
                ),
            )
            added_properties.add("OptimizationMode")

        if not hasattr(obj, "FeatureAllowanceMode"):
            obj.addProperty(
                "App::PropertyEnumeration",
                "FeatureAllowanceMode",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Editing mode for feature allowance values.",
                ),
            )
            added_properties.add("FeatureAllowanceMode")

        if not hasattr(obj, "FeatureAllowanceXY"):
            obj.addProperty(
                "App::PropertyDistance",
                "FeatureAllowanceXY",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Lateral XY material to leave on protected model/features.",
                ),
            )
            added_properties.add("FeatureAllowanceXY")

        if not hasattr(obj, "FeatureAllowanceZ"):
            obj.addProperty(
                "App::PropertyDistance",
                "FeatureAllowanceZ",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Vertical Z material to leave above protected model/features.",
                ),
            )
            added_properties.add("FeatureAllowanceZ")

        if not hasattr(obj, "StockAllowanceMode"):
            obj.addProperty(
                "App::PropertyEnumeration",
                "StockAllowanceMode",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Editing mode for stock allowance values.",
                ),
            )
            added_properties.add("StockAllowanceMode")

        if not hasattr(obj, "StockAllowanceXY"):
            obj.addProperty(
                "App::PropertyDistance",
                "StockAllowanceXY",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Lateral XY material to leave at the stock boundary.",
                ),
            )
            added_properties.add("StockAllowanceXY")

        if not hasattr(obj, "StockAllowanceZ"):
            obj.addProperty(
                "App::PropertyDistance",
                "StockAllowanceZ",
                "Volume Face Mill",
                QT_TRANSLATE_NOOP(
                    "App::Property",
                    "Vertical Z material to leave above stock bottom when no target face governs depth.",
                ),
            )
            added_properties.add("StockAllowanceZ")

        return added_properties

    @staticmethod
    def _distance_property_value(obj, prop_name):
        """Return a numeric value for a distance-like property, or None."""

        if not hasattr(obj, prop_name):
            return None

        try:
            value = getattr(obj, prop_name)
            return float(getattr(value, "Value", value))
        except Exception:
            return None

    def _migrate_allowance_compatibility_properties(self, obj, added_properties):
        """Backfill new XY allowance props from any legacy X/Y prototype properties."""

        migrated_properties = set()

        for xy_prop, legacy_props in _LEGACY_ALLOWANCE_XY_PROPERTIES.items():
            if not hasattr(obj, xy_prop):
                continue

            current_xy_value = self._distance_property_value(obj, xy_prop)
            if xy_prop not in added_properties and current_xy_value is not None:
                if not Path.Geom.isRoughly(current_xy_value, 0.0):
                    continue

            legacy_values = []
            for legacy_prop in legacy_props:
                legacy_value = self._distance_property_value(obj, legacy_prop)
                if legacy_value is not None:
                    legacy_values.append(max(0.0, legacy_value))

            if legacy_values:
                VolumeFaceMillUtils.set_distance_property(obj, xy_prop, max(legacy_values))
                migrated_properties.add(xy_prop)

        return migrated_properties

    def _initialize_allowance_properties(self, obj, added_properties, migrated_properties=None):
        """Initialize newly added allowance properties without overwriting existing values."""

        migrated_properties = migrated_properties or set()
        enums = self.propertyEnumerations(dataType="raw")

        for mode_prop in _ALLOWANCE_MODE_PROPERTIES:
            if not hasattr(obj, mode_prop):
                continue

            valid_values = [value for _label, value in enums[mode_prop]]
            current_value = getattr(obj, mode_prop, None)
            if mode_prop in added_properties or current_value not in valid_values:
                setattr(obj, mode_prop, _ALLOWANCE_MODE_DEFAULT)

        for distance_prop in _ALLOWANCE_DISTANCE_PROPERTIES:
            if distance_prop in added_properties and distance_prop not in migrated_properties:
                VolumeFaceMillUtils.set_distance_property(obj, distance_prop, 0.0)

    def _clamp_allowance_non_negative(self, obj, prop_name):
        """Clamp a single allowance distance property to a non-negative value."""

        value = self._distance_property_value(obj, prop_name)
        if value is None or value >= 0.0:
            return value

        Path.Log.warning(f"{prop_name} cannot be negative; clamping to 0 mm.")
        VolumeFaceMillUtils.set_distance_property(obj, prop_name, 0.0)
        return 0.0

    def _sync_linked_allowance(self, obj, changed_prop, mode_prop, xy_prop, z_prop):
        """Synchronize XY and Z allowance values when the group is in Linked mode."""

        if not hasattr(obj, mode_prop) or getattr(obj, mode_prop, None) != _ALLOWANCE_MODE_DEFAULT:
            return

        if changed_prop == xy_prop:
            target_prop = z_prop
        elif changed_prop == z_prop:
            target_prop = xy_prop
        else:
            return

        value = self._distance_property_value(obj, changed_prop)
        if value is None:
            return

        VolumeFaceMillUtils.set_distance_property(obj, target_prop, value)

    def _handle_allowance_property_change(self, obj, prop):
        """Apply Stage 2 linked-mode synchronization and non-negative clamping."""

        self._ensure_runtime_state()

        if self._syncing_allowances:
            return

        for mode_prop, xy_prop, z_prop in _ALLOWANCE_GROUPS:
            if prop not in {mode_prop, xy_prop, z_prop}:
                continue

            self._syncing_allowances = True
            try:
                if prop == mode_prop:
                    self._clamp_allowance_non_negative(obj, xy_prop)
                    self._clamp_allowance_non_negative(obj, z_prop)
                else:
                    self._clamp_allowance_non_negative(obj, prop)
                    self._sync_linked_allowance(obj, prop, mode_prop, xy_prop, z_prop)
            finally:
                self._syncing_allowances = False
            return

    def _force_compatibility_properties(self, obj):
        """Force deprecated/internal compatibility properties to safe values."""

        self._ensure_runtime_state()

        if self._forcing_compatibility_properties:
            return

        self._forcing_compatibility_properties = True
        try:
            if hasattr(obj, "BoundaryShape"):
                obj.BoundaryShape = "Stock"
                obj.setEditorMode("BoundaryShape", 2)

            if hasattr(obj, "ProtectModel"):
                obj.ProtectModel = True
                obj.setEditorMode("ProtectModel", 2)

            for legacy_prop_names in _LEGACY_ALLOWANCE_XY_PROPERTIES.values():
                for legacy_prop_name in legacy_prop_names:
                    if hasattr(obj, legacy_prop_name):
                        obj.setEditorMode(legacy_prop_name, 2)
        finally:
            self._forcing_compatibility_properties = False

    def _backfill_allowance_property_contract(self, obj):
        """Apply allowance property creation and migration without edit-time sync."""

        self._ensure_runtime_state()
        previous_sync_state = self._syncing_allowances
        self._syncing_allowances = True
        try:
            added_properties = self._add_properties(obj)
            preserved_z_values = {
                z_prop: self._distance_property_value(obj, z_prop)
                for _mode_prop, _xy_prop, z_prop in _ALLOWANCE_GROUPS
            }

            for name, values in self.propertyEnumerations():
                if hasattr(obj, name):
                    setattr(obj, name, values)

            migrated_properties = self._migrate_allowance_compatibility_properties(
                obj, added_properties
            )
            self._initialize_allowance_properties(obj, added_properties, migrated_properties)

            for _mode_prop, xy_prop, z_prop in _ALLOWANCE_GROUPS:
                if z_prop in added_properties or xy_prop not in migrated_properties:
                    continue

                preserved_z_value = preserved_z_values.get(z_prop)
                if preserved_z_value is None:
                    continue

                VolumeFaceMillUtils.set_distance_property(obj, z_prop, preserved_z_value)
        finally:
            self._syncing_allowances = previous_sync_state

        self._force_compatibility_properties(obj)

    def initPocketOp(self, obj):
        """Create Volume Face Mill specific properties."""

        Path.Log.track()
        self._ensure_runtime_state()
        self._backfill_allowance_property_contract(obj)

    def pocketInvertExtraOffset(self):
        return True

    def _sync_stock_depths(self, obj):
        """Synchronize stock-driven depths without recursive re-entry."""

        self._ensure_runtime_state()
        if self._syncing_depths:
            return VolumeFaceMillUtils.has_valid_stock(obj)

        self._syncing_depths = True
        try:
            return VolumeFaceMillUtils.sync_stock_depths(obj)
        finally:
            self._syncing_depths = False

    def _recommended_safe_travel_height(self, obj, model):
        """Return the creation-time safe travel height for Volume Face Mill."""

        stock_bb = VolumeFaceMillUtils.get_stock_boundbox(obj)
        if stock_bb is None:
            return None

        model_zmax = None
        for base in model or []:
            shape = getattr(base, "Shape", None)
            if shape is None or (hasattr(shape, "isNull") and shape.isNull()):
                continue
            model_zmax = (
                shape.BoundBox.ZMax if model_zmax is None else max(model_zmax, shape.BoundBox.ZMax)
            )

        top_z = stock_bb.ZMax if model_zmax is None else max(stock_bb.ZMax, model_zmax)
        return top_z + 5.0

    def _set_default_safe_heights(self, obj, model):
        """Set initial safe heights once while leaving later manual overrides untouched."""

        safe_height = self._recommended_safe_travel_height(obj, model)
        if safe_height is None:
            return False

        VolumeFaceMillUtils.set_distance_property(obj, "SafeHeight", safe_height)
        VolumeFaceMillUtils.set_distance_property(obj, "ClearanceHeight", safe_height)
        return True

    def _abort_no_path(self, obj, message, error=False, preserve_removalshape=False):
        """Clear generated state and abort with no path."""

        if not preserve_removalshape:
            obj.removalshape = Part.Shape()
        self.commandlist = []
        if error:
            Path.Log.error(message)
        else:
            Path.Log.warning(message)
        return []

    def _append_path_area_result(self, obj, pp, sim, sims):
        """Append one generated Path.Area result to the operation command list."""

        self.commandlist.extend(pp.Commands)
        sims.append(sim)

        if self.endVector is not None and len(self.commandlist) > 1:
            self.endVector[2] = obj.ClearanceHeight.Value
            self.commandlist.append(
                Path.Command("G0", {"Z": obj.ClearanceHeight.Value, "F": self.vertRapid})
            )

    def _depth_candidates(self, cut_depth_z, probe_epsilon=1e-6):
        """Return the ordered cut-depth candidates for one realized machining layer."""

        return [float(cut_depth_z), float(cut_depth_z) + probe_epsilon]

    def _build_allowance_layer_paths(self, obj, getsim=False):
        """Generate feature-allowance paths one cutting layer at a time.

        Non-zero feature allowance uses a dedicated per-layer execution path so
        each layer's keepout-expanded removal region is sent directly to
        Path.Area without being re-sliced as one combined 3D allowance shape.
        """

        self.endVector = None
        self.leadIn = 2.0
        self.depthparams = self._customDepthParams(obj, obj.StartDepth.Value, obj.FinalDepth.Value)

        if obj.UseStartPoint and obj.StartPoint is not None:
            start = obj.StartPoint
        else:
            start = None

        layer_volumes = VolumeFaceMillUtils.build_allowance_layer_volumes(
            obj=obj,
            model=self.model,
            tool_radius=self.radius,
            depthparams=self.depthparams,
        )
        if not layer_volumes:
            obj.removalshape = Part.Shape()
            Path.Log.warning(translate("CAM_VolumeFaceMill", "No machinable stock volume found."))
            return []

        layer_shapes = [shape for _cut_depth_z, shape in layer_volumes]
        obj.removalshape = (
            layer_shapes[0] if len(layer_shapes) == 1 else Part.makeCompound(layer_shapes)
        )

        sims = []
        saved_depthparams = self.depthparams
        try:
            for cut_depth_z, layer_shape in layer_volumes:
                _resolved_depth, pp, sim, z_levels = self._build_path_for_depth_candidates(
                    obj,
                    layer_shape,
                    False,
                    start,
                    getsim,
                    self._depth_candidates(cut_depth_z),
                )
                if not z_levels:
                    return self._abort_no_path(
                        obj,
                        f"Failed to generate an allowance layer section at Z {cut_depth_z:.6f}.",
                        preserve_removalshape=True,
                    )
                self._append_path_area_result(obj, pp, sim, sims)
        finally:
            self.depthparams = saved_depthparams

        return sims

    def _shape_has_section_at_depth(self, obj, shape, cut_depth_z):
        """Return whether Path.Area can build a usable section at ``cut_depth_z``."""

        area = Path.Area()
        area.setPlane(PathUtils.makeWorkplane(shape))
        area.add(shape)
        area.setParams(SectionTolerance=FreeCAD.Base.Precision.confusion() * 10)

        try:
            sections = area.makeSections(
                mode=0,
                project=self.areaOpUseProjection(obj),
                heights=[cut_depth_z],
            )
        except Exception:
            return False

        return bool(sections)

    def _effective_cut_depth(self, obj, shape, cut_depth_z, probe_epsilon=1e-6):
        """Return the realizable cut depth for ``shape`` near ``cut_depth_z``."""

        cut_depth_z = float(cut_depth_z)
        for candidate_depth in (cut_depth_z, cut_depth_z + probe_epsilon):
            if not self._shape_has_section_at_depth(obj, shape, candidate_depth):
                continue

            saved_end_vector = self.endVector
            try:
                _resolved_depth, _pp, _sim, z_levels = self._build_path_for_depth_candidates(
                    obj,
                    shape,
                    False,
                    None,
                    False,
                    [candidate_depth],
                )
            finally:
                self.endVector = saved_end_vector

            if z_levels:
                return candidate_depth

        return None

    def _cutting_z_levels_from_commands(self, commands):
        """Return sorted unique cutting Z levels from a command sequence."""

        z_levels = set()
        x = y = z = None
        for cmd in commands:
            params = cmd.Parameters
            if "X" in params:
                x = float(params["X"])
            if "Y" in params:
                y = float(params["Y"])
            if "Z" in params:
                z = float(params["Z"])

            if cmd.Name in ("G1", "G2", "G3") and x is not None and y is not None and z is not None:
                z_levels.add(round(z, 6))

        return sorted(z_levels)

    def _build_path_for_depth_candidates(
        self,
        obj,
        shape,
        is_hole,
        start,
        getsim,
        candidate_depths,
    ):
        """Return the first candidate depth that yields real cutting moves."""

        saved_depthparams = self.depthparams
        try:
            seen = set()
            for candidate_depth in candidate_depths:
                candidate_depth = float(candidate_depth)
                rounded = round(candidate_depth, 9)
                if rounded in seen:
                    continue
                seen.add(rounded)

                self.depthparams = [candidate_depth]
                pp, sim = self._buildPathArea(obj, shape, is_hole, start, getsim)
                z_levels = self._cutting_z_levels_from_commands(pp.Commands)
                if z_levels:
                    return candidate_depth, pp, sim, z_levels
        finally:
            self.depthparams = saved_depthparams

        return None, None, None, []

    def opExecute(self, obj, getsim=False):
        """Execute stock-driven volume face milling even without selected base geometry."""

        Path.Log.track()
        self._force_compatibility_properties(obj)

        if not VolumeFaceMillUtils.has_valid_stock(obj):
            return self._abort_no_path(
                obj,
                translate("CAM_VolumeFaceMill", "Volume Face Mill requires a valid Job stock."),
                error=True,
            )

        if not self._sync_stock_depths(obj):
            return self._abort_no_path(
                obj,
                translate("CAM_VolumeFaceMill", "Volume Face Mill requires a valid Job stock."),
                error=True,
            )

        if not self.validate_tool(obj):
            return self._abort_no_path(
                obj,
                translate("CAM_VolumeFaceMill", "Volume Face Mill requires a valid tool."),
                error=True,
            )

        if VolumeFaceMillUtils.feature_allowance_is_active(obj):
            return self._build_allowance_layer_paths(obj, getsim)

        self._pending_standard_abort = None
        result = PathAreaOp.ObjectOp.opExecute(self, obj, getsim)
        if self._pending_standard_abort is not None:
            message, error, preserve_removalshape = self._pending_standard_abort
            self._pending_standard_abort = None
            return self._abort_no_path(
                obj,
                message,
                error=error,
                preserve_removalshape=preserve_removalshape,
            )

        return result

    def _customDepthParams(self, obj, strDep, finDep):
        """Keep stock-top StartDepth without injecting a stock-top cut pass."""

        return super()._customDepthParams(obj, strDep, finDep)

    def areaOpSetDefaultValues(self, obj, job):
        """Initialize Volume Face Mill properties."""

        obj.ProtectSelectedFeatures = False
        obj.ClearEdges = False
        obj.OptimizationMode = "None"
        obj.FeatureAllowanceMode = _ALLOWANCE_MODE_DEFAULT
        obj.StockAllowanceMode = _ALLOWANCE_MODE_DEFAULT

        VolumeFaceMillUtils.set_distance_property(obj, "FeatureAllowanceXY", 0.0)
        VolumeFaceMillUtils.set_distance_property(obj, "FeatureAllowanceZ", 0.0)
        VolumeFaceMillUtils.set_distance_property(obj, "StockAllowanceXY", 0.0)
        VolumeFaceMillUtils.set_distance_property(obj, "StockAllowanceZ", 0.0)

        obj.StepOver = 50
        obj.Angle = 45
        obj.ClearingPattern = "ZigZag"
        obj.MinTravel = False

        self._force_compatibility_properties(obj)

        if job and getattr(job, "Stock", None):
            self._sync_stock_depths(obj)
            model = job.Model.Group if getattr(job, "Model", None) else self.model
            self._set_default_safe_heights(obj, model)

    def areaOpOnChanged(self, obj, prop):
        """Handle Volume Face Mill property changes."""

        Path.Log.track(prop)
        self._ensure_runtime_state()

        if prop == "StepOver" and obj.StepOver == 0:
            obj.StepOver = 1

        if prop == "OptimizationMode":
            obj.MinTravel = obj.OptimizationMode == "MinTravel"

        if prop in {"BoundaryShape", "ProtectModel"}:
            self._force_compatibility_properties(obj)

        if prop in _ALLOWANCE_MODE_PROPERTIES or prop in _ALLOWANCE_DISTANCE_PROPERTIES:
            self._handle_allowance_property_change(obj, prop)

        if prop in {"Base", "ClearEdges", "ProtectSelectedFeatures"} or (
            prop in _ALLOWANCE_DISTANCE_PROPERTIES and not self._syncing_allowances
        ):
            if VolumeFaceMillUtils.has_valid_stock(obj):
                self._sync_stock_depths(obj)

        super().areaOpOnChanged(obj, prop)

    def updateDepths(self, obj, ignoreErrors=False):
        """Short-circuit base depth updates when Job stock is missing."""

        if not VolumeFaceMillUtils.has_valid_stock(obj):
            del ignoreErrors
            obj.removalshape = Part.Shape()
            return False

        return super().updateDepths(obj, ignoreErrors)

    def opUpdateDepths(self, obj):
        """Keep Volume Face Mill depth targets stock-aware."""

        self._sync_stock_depths(obj)

    def validate_tool(self, obj):
        """Validate that the active tool can be used for Volume Face Mill."""

        del obj

        tool = getattr(self, "tool", None)
        if tool is None:
            Path.Log.error(translate("CAM_VolumeFaceMill", "Volume Face Mill requires a tool."))
            return False

        try:
            diameter = float(tool.Diameter)
        except Exception:
            Path.Log.error(
                translate(
                    "CAM_VolumeFaceMill",
                    "Volume Face Mill requires a valid tool diameter.",
                )
            )
            return False

        if diameter <= 0:
            Path.Log.error(
                translate(
                    "CAM_VolumeFaceMill",
                    "Volume Face Mill requires a tool diameter greater than zero.",
                )
            )
            return False

        tool_shape = (
            PathUtils.getToolShapeName(tool).replace(" ", "").replace("-", "").replace("_", "")
        )
        if not tool_shape:
            Path.Log.warning(
                translate(
                    "CAM_VolumeFaceMill",
                    "Tool type could not be determined; assuming a flat circular cutter footprint.",
                )
            )
            return True

        if tool_shape not in {"endmill", "facemill"}:
            Path.Log.warning(
                translate(
                    "CAM_VolumeFaceMill",
                    "Tool-specific compensation is not implemented; using a flat circular cutter footprint.",
                )
            )

        return True

    def areaOpShapes(self, obj):
        """Return the stock-minus-protected-model removal volume for Path.Area."""

        Path.Log.track()

        self.removalshapes = []
        self._pending_standard_abort = None
        self._force_compatibility_properties(obj)

        if not self.validate_tool(obj):
            obj.removalshape = Part.Shape()
            return self.removalshapes

        removal = VolumeFaceMillUtils.build_removal_volume(
            obj=obj,
            model=self.model,
            tool_radius=self.radius,
            depthparams=self.depthparams,
        )

        if VolumeFaceMillUtils.shape_is_empty(removal):
            obj.removalshape = Part.Shape()
            self._pending_standard_abort = (
                translate("CAM_VolumeFaceMill", "No machinable stock volume found."),
                False,
                False,
            )
            return self.removalshapes

        try:
            removal = removal.removeSplitter()
        except Exception as exc:
            Path.Log.debug(f"removeSplitter failed on Volume Face Mill removal shape: {exc}")

        effective_depths = []
        for cut_depth_z in self.depthparams:
            effective_cut_depth = self._effective_cut_depth(obj, removal, cut_depth_z)
            if effective_cut_depth is not None:
                effective_depths.append(effective_cut_depth)
        if not effective_depths:
            obj.removalshape = removal
            self._pending_standard_abort = (
                translate(
                    "CAM_VolumeFaceMill",
                    "No realizable cutting sections found within the permitted depth range.",
                ),
                False,
                True,
            )
            return self.removalshapes
        self.depthparams = effective_depths

        obj.removalshape = removal
        self.removalshapes.append((removal, False, "volumeFaceMill"))
        return self.removalshapes

    def areaOpAreaParams(self, obj, isHole):
        """Return Path.Area parameters."""

        return super().areaOpAreaParams(obj, isHole)

    def areaOpPathParams(self, obj, isHole):
        """Return Path.fromShapes parameters."""

        obj.MinTravel = obj.OptimizationMode == "MinTravel"
        params = super().areaOpPathParams(obj, isHole)

        if obj.OptimizationMode == "MinTravel":
            if obj.UseStartPoint and obj.StartPoint is not None:
                params["sort_mode"] = 3
            else:
                Path.Log.warning(
                    translate(
                        "CAM_VolumeFaceMill",
                        "Min Travel requires a valid start point; using normal sorting.",
                    )
                )

        return params

    def opOnDocumentRestored(self, obj):
        """Restore missing properties for old documents."""

        self._ensure_runtime_state()
        super().opOnDocumentRestored(obj)
        self._ensure_runtime_state()
        self._backfill_allowance_property_contract(obj)


def SetupProperties():
    setup = PathPocketBase.SetupProperties()
    setup.append("ProtectSelectedFeatures")
    setup.append("ClearEdges")
    setup.append("OptimizationMode")
    setup.append("FeatureAllowanceMode")
    setup.append("FeatureAllowanceXY")
    setup.append("FeatureAllowanceZ")
    setup.append("StockAllowanceMode")
    setup.append("StockAllowanceXY")
    setup.append("StockAllowanceZ")
    return setup


def Create(name, obj=None, parentJob=None):
    """Create and return a Volume Face Mill operation."""

    if obj is None:
        obj = FreeCAD.ActiveDocument.addObject("Path::FeaturePython", name)
    obj.Proxy = ObjectVolumeFaceMill(obj, name, parentJob)
    return obj
