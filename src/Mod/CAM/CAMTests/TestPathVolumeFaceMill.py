# SPDX-License-Identifier: LGPL-2.1-or-later

import importlib.util
import os
import sys
import tempfile
import types
import xml.etree.ElementTree as ET
from unittest import mock

import FreeCAD
import Part
import Path
import Path.Base.SetupSheet as PathSetupSheet
import Path.Main.Job as PathJob
import Path.Main.Stock as PathStock
import Path.Op.VolumeFaceMill as PathVolumeFaceMill
import Path.Op.VolumeFaceMillUtils as PathVolumeFaceMillUtils

from CAMTests.PathTestUtils import PathTestBase


class _FakeComboBox:
    """Minimal combo-box test double for headless task-panel tests."""

    def __init__(self, data=None):
        self._data = data
        self.currentIndexChanged = object()

    def currentData(self):
        return self._data


class _FakeCheckBox:
    """Minimal checkbox test double."""

    def __init__(self, checked=False):
        self._checked = bool(checked)
        self.clicked = object()

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = bool(checked)


class _FakeFrame:
    """Minimal frame test double that tracks visibility state."""

    def __init__(self):
        self.visible = True

    def setVisible(self, visible):
        self.visible = bool(visible)


class _FakeQuantityWidget:
    """Minimal quantity-widget test double."""

    def __init__(self, value=0.0):
        self.value = float(value)
        self.enabled = True
        self.editingFinished = object()

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _FakeSpinBoxWidget:
    """Minimal spin-box style test double."""

    def __init__(self, value=0):
        self._value = value
        self.editingFinished = object()

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value


class _FakeTextWidget:
    """Minimal text field test double with enabled state."""

    def __init__(self, text=""):
        self.text = text
        self.enabled = True
        self.editingFinished = object()

    def setText(self, text):
        self.text = text

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _FakeQuantitySpinBox:
    """Headless QuantitySpinBox stand-in that drives real op properties."""

    def __init__(self, widget, obj, prop_name):
        self.widget = widget
        self.obj = obj
        self.prop_name = prop_name

    def updateProperty(self):
        PathVolumeFaceMillUtils.set_distance_property(self.obj, self.prop_name, self.widget.value)
        if getattr(self.obj, "Proxy", None):
            self.obj.Proxy.areaOpOnChanged(self.obj, self.prop_name)

    def updateWidget(self):
        value = getattr(
            getattr(self.obj, self.prop_name), "Value", getattr(self.obj, self.prop_name)
        )
        self.widget.value = float(value)


class _FakePropertyObject:
    """Minimal property container for migration-helper tests."""

    def __init__(self, clearing_pattern=None):
        self._editor_modes = {}
        if clearing_pattern is not None:
            self.ClearingPattern = clearing_pattern

    def addProperty(self, _prop_type, name, _group, _description):
        setattr(self, name, None)
        return self

    def setEditorMode(self, name, mode):
        self._editor_modes[name] = mode

    def getEditorMode(self, name):
        return self._editor_modes.get(name)


class TestPathVolumeFaceMill(PathTestBase):
    """Test stock-aware Volume Face Mill operation."""

    def setUp(self):
        self.doc = FreeCAD.newDocument("TestPathVolumeFaceMill")

    def tearDown(self):
        doc = getattr(self, "doc", None)
        if doc is None:
            return

        try:
            doc_name = doc.Name
        except ReferenceError:
            return

        if doc_name in FreeCAD.listDocuments():
            FreeCAD.closeDocument(doc_name)

    def _make_stock(self, job, size=None, base=None):
        """Create stock block test shape."""

        return PathStock.CreateBox(
            job,
            size or FreeCAD.Vector(100, 100, 20),
            FreeCAD.Placement(base or FreeCAD.Vector(0, 0, 0), FreeCAD.Rotation()),
        )

    def _make_model_with_boss(self):
        """Create model with raised island/boss."""

        model = self.doc.addObject("Part::Feature", "BossModel")
        model.Shape = Part.makeBox(30, 30, 10, FreeCAD.Vector(35, 35, 0))
        return model

    def _make_drafted_model(self):
        """Create a drafted prism with a 45 mm top surface."""

        def rect_wire(xmin, xmax, ymin, ymax, z_value):
            points = [
                FreeCAD.Vector(xmin, ymin, z_value),
                FreeCAD.Vector(xmax, ymin, z_value),
                FreeCAD.Vector(xmax, ymax, z_value),
                FreeCAD.Vector(xmin, ymax, z_value),
                FreeCAD.Vector(xmin, ymin, z_value),
            ]
            return Part.makePolygon(points)

        model = self.doc.addObject("Part::Feature", "DraftedModel")
        bottom = rect_wire(20.0, 80.0, 30.0, 70.0, 0.0)
        top = rect_wire(28.0, 72.0, 36.0, 64.0, 45.0)
        loft = Part.makeLoft([bottom, top], True, False)
        model.Shape = Part.makeSolid(loft)
        return model

    def _make_aux_box(self, name, origin, size):
        """Create an auxiliary solid that is not part of the Job model."""

        aux = self.doc.addObject("Part::Feature", name)
        aux.Shape = Part.makeBox(size.x, size.y, size.z, origin)
        return aux

    def _make_fixture_box(self, name, origin, size):
        """Create an auxiliary non-model fixture object."""

        return self._make_aux_box(name, origin, size)

    def _make_job_with_stock_and_model(self):
        model = self._make_model_with_boss()
        job = PathJob.Create("Job", [model], None)
        job.GeometryTolerance.Value = 0.001
        job.Stock = self._make_stock(job)
        self.assertSuccessfulRecompute(self.doc)
        return job, model

    def _make_stock_only_job(self):
        """Create a Job with valid stock and no model geometry."""

        try:
            job = PathJob.Create("JobStockOnly", [], None)
        except Exception:
            temp_model = self._make_model_with_boss()
            job = PathJob.Create("JobStockOnly", [temp_model], None)
            job.Model.Group = []

        job.GeometryTolerance.Value = 0.001
        job.Stock = self._make_stock(job)
        self.assertSuccessfulRecompute(self.doc)
        return job

    def _make_volume_face_mill_setup_sheet_prototype(self, opname):
        previous_registration = PathSetupSheet._RegisteredOps.get(opname)
        PathSetupSheet.RegisterOperation(
            opname,
            PathVolumeFaceMill.Create,
            PathVolumeFaceMill.SetupProperties,
        )
        prototype = PathSetupSheet._RegisteredOps[opname].prototype(opname)
        return prototype, previous_registration

    def _make_job_without_stock(self):
        model = self._make_model_with_boss()
        job = PathJob.Create("JobNoStock", [model], None)
        job.GeometryTolerance.Value = 0.001
        if getattr(job, "Stock", None):
            self.doc.removeObject(job.Stock.Name)
            job.Stock = None
        self.assertSuccessfulRecompute(self.doc)
        return job, model

    def _make_job_with_custom_stock_and_model(self, model, stock_size, stock_base=None):
        job = PathJob.Create("Job", [model], None)
        job.GeometryTolerance.Value = 0.001
        job.Stock = self._make_stock(job, stock_size, stock_base)
        self.assertSuccessfulRecompute(self.doc)
        return job

    def _make_job_with_25mm_stock_above_model(self):
        """Create the base boss model with exactly 25 mm of stock above its top face."""

        model = self._make_model_with_boss()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 35))
        return job, model

    def _make_full_plate_model(self, size=None, base=None):
        """Create a full-width plate whose top face is at Z=0."""

        size = size or FreeCAD.Vector(100, 100, 25)
        base = base or FreeCAD.Vector(0, 0, -25)
        model = self.doc.addObject("Part::Feature", "PlateModel")
        model.Shape = Part.makeBox(size.x, size.y, size.z, base)
        return model

    def _make_plate_model_with_target_faces(self):
        """Create Job model solids with horizontal faces that can validly set target depth."""

        left = self.doc.addObject("Part::Feature", "LeftTargetModel")
        left.Shape = Part.makeBox(10, 10, 5, FreeCAD.Vector(5, 5, 0))

        right = self.doc.addObject("Part::Feature", "RightTargetModel")
        right.Shape = Part.makeBox(10, 10, 5, FreeCAD.Vector(25, 5, 0))

        high = self.doc.addObject("Part::Feature", "HighTargetModel")
        high.Shape = Part.makeBox(10, 10, 12, FreeCAD.Vector(45, 5, 0))

        return [left, right, high]

    def _make_full_plate_with_raised_feature_model(self):
        """Create a full-width plate with a raised protected feature above Z=0."""

        model = self.doc.addObject("Part::Feature", "PlateWithRaisedFeature")
        plate = Part.makeBox(200, 200, 25, FreeCAD.Vector(0, 0, -25))
        raised = Part.makeBox(60, 60, 45, FreeCAD.Vector(70, 70, 0))
        model.Shape = plate.fuse(raised)
        return model

    def _configure_tool(self, op, diameter=10.0):
        tool = op.ToolController.Tool
        tool.Diameter = diameter

        if hasattr(tool, "ShapeType"):
            try:
                shape_types = {
                    str(value).lower(): str(value)
                    for value in tool.getEnumerationsOfProperty("ShapeType")
                }
            except Exception:
                shape_types = {}

            if "facemill" in shape_types:
                tool.ShapeType = shape_types["facemill"]
            elif "endmill" in shape_types:
                tool.ShapeType = shape_types["endmill"]

    def _set_stepdown_and_heights(self, op, step_down):
        """Set non-boundary machining parameters after stock depths have synced."""

        op.setExpression("StepDown", None)
        op.StepDown.Value = step_down
        op.setExpression("SafeHeight", None)
        op.SafeHeight.Value = op.StartDepth.Value + 5.0
        op.setExpression("ClearanceHeight", None)
        op.ClearanceHeight.Value = op.StartDepth.Value + 10.0

    def _set_allowance_distance(self, op, prop_name, value):
        """Set an allowance distance and force the operation property-change path."""

        op.setExpression(prop_name, None)
        getattr(op, prop_name).Value = value
        op.Proxy.areaOpOnChanged(op, prop_name)
        op.touch()

    def _set_allowance_mode(self, op, prop_name, value):
        """Set an allowance mode and force the operation property-change path."""

        setattr(op, prop_name, value)
        op.Proxy.areaOpOnChanged(op, prop_name)
        op.touch()

    def _tool_radius(self, op):
        diameter = getattr(
            op.ToolController.Tool.Diameter, "Value", op.ToolController.Tool.Diameter
        )
        return float(diameter) / 2.0

    def _create_operation(
        self,
        *,
        name="VolumeFaceMill",
        job=None,
        model=None,
        base=None,
        step_down=20.0,
        clear_edges=False,
        optimization_mode="None",
        protect_selected_features=False,
        compat_protect_model=None,
        clearing_pattern="ZigZag",
        tool_diameter=10.0,
        override_heights=True,
    ):
        if job is None or model is None:
            job, model = self._make_job_with_stock_and_model()

        op = PathVolumeFaceMill.Create(name, parentJob=job)
        op.Label = name
        self._configure_tool(op, diameter=tool_diameter)
        op.ClearingPattern = clearing_pattern
        op.ClearEdges = clear_edges
        op.OptimizationMode = optimization_mode
        op.ProtectSelectedFeatures = protect_selected_features
        op.Base = [] if base is None else base

        if compat_protect_model is not None and hasattr(op, "ProtectModel"):
            op.ProtectModel = compat_protect_model

        self.assertSuccessfulRecompute(self.doc, op)
        if override_heights:
            self._set_stepdown_and_heights(op, step_down)
            self.assertSuccessfulRecompute(self.doc, op)
        return job, model, op

    def _cutting_moves(self, path):
        """Return cutting commands from a Path object."""

        return [cmd for cmd in path.Commands if cmd.Name in ("G1", "G2", "G3")]

    def _cutting_points(self, commands):
        points = []
        x = None
        y = None
        z = None

        for cmd in commands:
            params = cmd.Parameters
            if "X" in params:
                x = float(params["X"])
            if "Y" in params:
                y = float(params["Y"])
            if "Z" in params:
                z = float(params["Z"])

            if cmd.Name in ("G1", "G2", "G3") and x is not None and y is not None and z is not None:
                points.append((x, y, z))

        return points

    def _cutting_z_levels(self, commands):
        """Return sorted unique Z levels used by cutting commands."""

        z_levels = {round(point[2], 6) for point in self._cutting_points(commands)}
        return sorted(z_levels)

    def _cutting_z_order(self, commands, tolerance=1e-5):
        """Return the first-seen cutting Z order from a command sequence."""

        ordered_levels = []
        for _x, _y, z in self._cutting_points(commands):
            if any(abs(z - existing) <= tolerance for existing in ordered_levels):
                continue
            ordered_levels.append(z)
        return ordered_levels

    def _assert_has_z_level(self, z_levels, expected, tolerance=1e-5):
        self.assertTrue(
            any(abs(level - expected) <= tolerance for level in z_levels),
            f"Expected a cutting Z level near {expected}, got {z_levels}",
        )

    def _assert_z_levels_equal(self, z_levels, expected_levels, tolerance=1e-5):
        self.assertEqual(
            len(z_levels),
            len(expected_levels),
            f"Expected {len(expected_levels)} cutting Z levels, got {z_levels}",
        )
        for actual, expected in zip(z_levels, expected_levels):
            self.assertAlmostEqual(actual, expected, delta=tolerance)

    def _assert_z_order_equal(self, z_order, expected_levels, tolerance=1e-5):
        self.assertEqual(
            len(z_order),
            len(expected_levels),
            f"Expected cut depth order {expected_levels}, got {z_order}",
        )
        for actual, expected in zip(z_order, expected_levels):
            self.assertAlmostEqual(actual, expected, delta=tolerance)

    @staticmethod
    def _expression_for_property(obj, prop_name):
        """Return the expression currently bound to a property, or None if unbound."""

        for current_prop, expression in getattr(obj, "ExpressionEngine", ()):
            if current_prop == prop_name:
                return expression
        return None

    def _xy_inside_rect(self, x, y, xmin, xmax, ymin, ymax, tolerance=1e-6):
        """Return whether XY lies inside a rectangle."""

        return (xmin - tolerance) <= x <= (xmax + tolerance) and (ymin - tolerance) <= y <= (
            ymax + tolerance
        )

    def _center_probe(self, z_center, size=1.0, height=0.5):
        half = size / 2.0
        return Part.makeBox(
            size,
            size,
            height,
            FreeCAD.Vector(50.0 - half, 50.0 - half, z_center - (height / 2.0)),
        )

    def _probe_box(self, xmin, ymin, zmin, xlen=1.0, ylen=1.0, zlen=1.0):
        return Part.makeBox(xlen, ylen, zlen, FreeCAD.Vector(xmin, ymin, zmin))

    def _vertical_face_names(self, shape):
        """Return face names for vertical faces on a simple prismatic test shape."""

        names = []
        for idx, face in enumerate(shape.Faces, start=1):
            bb = face.BoundBox
            if (bb.ZMax - bb.ZMin) > 1e-6:
                names.append(f"Face{idx}")
        return names

    def _lowest_horizontal_face_name(self, shape):
        """Return the lowest horizontal face name on a simple prismatic test shape."""

        return self._horizontal_face_names_by_height(shape)[0]

    def _highest_horizontal_face_name(self, shape):
        """Return the highest horizontal face name on a test shape."""

        return self._horizontal_face_names_by_height(shape)[-1]

    def _horizontal_face_name_near_z(self, shape, expected_z, tolerance=1e-6):
        """Return a horizontal face name whose stable Z value is near expected_z."""

        candidates = []
        for idx, face in enumerate(shape.Faces, start=1):
            bb = face.BoundBox
            if abs(bb.ZMax - bb.ZMin) <= tolerance:
                z_value = 0.5 * (bb.ZMin + bb.ZMax)
                if abs(z_value - expected_z) <= tolerance:
                    candidates.append(f"Face{idx}")

        self.assertTrue(candidates, f"No horizontal face found near Z={expected_z}")
        return candidates[0]

    def _horizontal_face_names_by_height(self, shape):
        """Return horizontal face names sorted by increasing Z height."""

        candidates = []
        for idx, face in enumerate(shape.Faces, start=1):
            bb = face.BoundBox
            if abs(bb.ZMax - bb.ZMin) <= 1e-6:
                candidates.append((bb.ZMax, f"Face{idx}"))

        self.assertGreater(len(candidates), 0)
        return [name for _z, name in sorted(candidates, key=lambda item: item[0])]

    def _volume_face_mill_ui_path(self):
        """Return the active Volume Face Mill panel source path."""

        candidates = [
            os.path.abspath(
                os.path.join(
                    os.path.dirname(__file__),
                    "..",
                    "Gui",
                    "Resources",
                    "panels",
                    "PageOpVolumeFaceMillEdit.ui",
                )
            ),
            os.path.join(
                os.getcwd(),
                "src",
                "Mod",
                "CAM",
                "Gui",
                "Resources",
                "panels",
                "PageOpVolumeFaceMillEdit.ui",
            ),
        ]

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        self.fail("Could not locate PageOpVolumeFaceMillEdit.ui for allowance UI contract test.")

    def _volume_face_mill_gui_path(self):
        """Return the active Volume Face Mill GUI controller source path."""

        candidates = [
            os.path.abspath(
                os.path.join(
                    os.path.dirname(__file__),
                    "..",
                    "Path",
                    "Op",
                    "Gui",
                    "VolumeFaceMill.py",
                )
            ),
            os.path.join(
                os.getcwd(),
                "src",
                "Mod",
                "CAM",
                "Path",
                "Op",
                "Gui",
                "VolumeFaceMill.py",
            ),
        ]

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        self.fail("Could not locate VolumeFaceMill.py for GUI controller allowance tests.")

    def _load_headless_volume_face_mill_gui_module(self):
        """Load the GUI controller module with test doubles for GUI-only imports."""

        gui_path = self._volume_face_mill_gui_path()
        module_name = "TestPathVolumeFaceMillGuiController"

        fake_path = types.ModuleType("Path")
        fake_path.__path__ = []
        fake_path.Geom = Path.Geom
        fake_path.Log = types.SimpleNamespace(
            Level=types.SimpleNamespace(DEBUG=0, INFO=1),
            setLevel=lambda *args, **kwargs: None,
            trackModule=lambda *args, **kwargs: None,
            track=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            thisModule=lambda: "TestPathVolumeFaceMillGuiController",
        )

        fake_path_base = types.ModuleType("Path.Base")
        fake_path_base.__path__ = []
        fake_path_base_gui = types.ModuleType("Path.Base.Gui")
        fake_path_base_gui.__path__ = []
        fake_path_base_gui_util = types.ModuleType("Path.Base.Gui.Util")
        fake_path_base_gui_util.QuantitySpinBox = _FakeQuantitySpinBox
        fake_path_base_gui_util.updateInputField = lambda *args, **kwargs: None

        fake_path_op = types.ModuleType("Path.Op")
        fake_path_op.__path__ = []
        fake_path_op_gui = types.ModuleType("Path.Op.Gui")
        fake_path_op_gui.__path__ = []
        fake_path_op_gui_base = types.ModuleType("Path.Op.Gui.Base")
        fake_path_op_gui_base.TaskPanelBaseGeometryPage = type(
            "TaskPanelBaseGeometryPage",
            (),
            {},
        )
        fake_path_op_gui_base.SetupOperation = lambda *args, **kwargs: None

        class _FakePocketTaskPanelPage:
            def populateCombobox(self, form, enum_tups, combo_to_property_map):
                del form, enum_tups, combo_to_property_map

            def selectInComboBox(self, value, combo):
                combo._data = value

            def updateToolController(self, obj, widget):
                del obj, widget

            def updateCoolant(self, obj, widget):
                del obj, widget

            def setupToolController(self, obj, widget):
                del obj, widget

            def setupCoolant(self, obj, widget):
                del obj, widget

        fake_path_op_gui_pocket_base = types.ModuleType("Path.Op.Gui.PocketBase")
        fake_path_op_gui_pocket_base.TaskPanelOpPage = _FakePocketTaskPanelPage
        fake_path_op_gui_pocket_base.FeatureFacing = 0x02

        fake_path_op_pocket_base = types.ModuleType("Path.Op.PocketBase")
        fake_path_op_pocket_base.ObjectPocket = type(
            "ObjectPocket",
            (),
            {"pocketPropertyEnumerations": staticmethod(lambda dataType="raw": {})},
        )

        fake_path_op_volume_face_mill = types.ModuleType("Path.Op.VolumeFaceMill")
        fake_path_op_volume_face_mill.ObjectVolumeFaceMill = type(
            "ObjectVolumeFaceMill",
            (),
            {"propertyEnumerations": staticmethod(lambda dataType="raw": {})},
        )
        fake_path_op_volume_face_mill.Create = lambda *args, **kwargs: None
        fake_path_op_volume_face_mill.SetupProperties = lambda: []

        fake_freecad_gui = types.ModuleType("FreeCADGui")
        fake_freecad_gui.PySideUic = types.SimpleNamespace(loadUi=lambda path: None)

        fake_pyside = types.ModuleType("PySide")
        fake_pyside.__path__ = []
        fake_qtcore = types.ModuleType("PySide.QtCore")
        fake_qtcore.QT_TRANSLATE_NOOP = lambda context, text: text

        fake_modules = {
            "FreeCADGui": fake_freecad_gui,
            "Path": fake_path,
            "Path.Base": fake_path_base,
            "Path.Base.Gui": fake_path_base_gui,
            "Path.Base.Gui.Util": fake_path_base_gui_util,
            "Path.Op": fake_path_op,
            "Path.Op.Gui": fake_path_op_gui,
            "Path.Op.Gui.Base": fake_path_op_gui_base,
            "Path.Op.Gui.PocketBase": fake_path_op_gui_pocket_base,
            "Path.Op.PocketBase": fake_path_op_pocket_base,
            "Path.Op.VolumeFaceMill": fake_path_op_volume_face_mill,
            "Path.Op.VolumeFaceMillUtils": PathVolumeFaceMillUtils,
            "PySide": fake_pyside,
            "PySide.QtCore": fake_qtcore,
        }

        spec = importlib.util.spec_from_file_location(module_name, gui_path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, fake_modules):
            spec.loader.exec_module(module)
        return module

    def test_stock_is_required(self):
        job, model = self._make_job_without_stock()
        top_face = self._highest_horizontal_face_name(model.Shape)
        op = PathVolumeFaceMill.Create("stock_required", parentJob=job)
        op.Label = "stock_required"
        self._configure_tool(op)
        op.Base = [(model, [top_face])]
        self.assertSuccessfulRecompute(self.doc, op)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        self.assertIsNone(removal)
        self.assertTrue(op.removalshape.isNull())
        self.assertEqual(len(self._cutting_moves(op.Path)), 0)

    def test_stock_only_job_generates_stock_facing_path(self):
        job = self._make_stock_only_job()
        op = PathVolumeFaceMill.Create("stock_only_volume_face_mill", parentJob=job)
        op.Label = "stock_only_volume_face_mill"
        self._configure_tool(op)
        op.Base = []

        self.assertSuccessfulRecompute(self.doc, op)
        self._set_stepdown_and_heights(op, 5.0)
        self.assertSuccessfulRecompute(self.doc, op)

        cutting_moves = self._cutting_moves(op.Path)
        self.assertGreater(len(cutting_moves), 0)
        self.assertFalse(op.removalshape.isNull())
        self.assertAlmostEqual(op.OpStartDepth.Value, job.Stock.Shape.BoundBox.ZMax, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, job.Stock.Shape.BoundBox.ZMin, places=6)

    def test_model_protection_is_always_on(self):
        job, model, op = self._create_operation(
            name="model_protection_always_on",
            compat_protect_model=False,
            step_down=5.0,
        )

        if hasattr(op, "ProtectModel"):
            self.assertTrue(op.ProtectModel)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        overlap = removal.common(model.Shape)
        self.assertLessEqual(getattr(overlap, "Volume", 0.0), 1e-6)

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)
        for x, y, z in cutting_points:
            if z <= 10.0 + 1e-6:
                self.assertFalse(
                    self._xy_inside_rect(x, y, 35.0, 65.0, 35.0, 65.0),
                    f"Cutting move enters protected boss footprint at ({x}, {y}, {z})",
                )

    def test_stock_boundary_is_always_used(self):
        job, model = self._make_job_with_stock_and_model()
        _job, _model, op = self._create_operation(
            name="stock_boundary_always_used",
            job=job,
            model=model,
            base=[(model, [self._highest_horizontal_face_name(model.Shape)])],
        )

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        stock_bb = job.Stock.Shape.BoundBox
        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, stock_bb.XMin, places=6)
        self.assertAlmostEqual(removal_bb.YMin, stock_bb.YMin, places=6)
        self.assertAlmostEqual(removal_bb.XMax, stock_bb.XMax, places=6)
        self.assertAlmostEqual(removal_bb.YMax, stock_bb.YMax, places=6)

    def test_base_does_not_define_boundary(self):
        job, model = self._make_job_with_stock_and_model()
        _job, _model, op = self._create_operation(
            name="base_does_not_define_boundary",
            job=job,
            model=model,
            base=[(model, [self._highest_horizontal_face_name(model.Shape)])],
        )

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)
        self.assertTrue(
            any(
                not self._xy_inside_rect(x, y, 35.0, 65.0, 35.0, 65.0)
                for x, y, _z in cutting_points
            )
        )

    def test_lowest_selected_horizontal_face_sets_final_depth(self):
        upper = self.doc.addObject("Part::Feature", "UpperTargetModel")
        upper.Shape = Part.makeBox(10, 10, 12, FreeCAD.Vector(5, 5, 0))
        lower = self.doc.addObject("Part::Feature", "LowerTargetModel")
        lower.Shape = Part.makeBox(10, 10, 5, FreeCAD.Vector(20, 5, 0))
        job = PathJob.Create("Job", [upper, lower], None)
        job.GeometryTolerance.Value = 0.001
        job.Stock = self._make_stock(job)
        self.assertSuccessfulRecompute(self.doc)
        upper_top = self._highest_horizontal_face_name(upper.Shape)
        lower_top = self._highest_horizontal_face_name(lower.Shape)

        _job, _model, op = self._create_operation(
            name="lowest_selected_final_depth",
            job=job,
            model=lower,
            base=[(upper, [upper_top]), (lower, [lower_top])],
            step_down=5.0,
        )

        self.assertAlmostEqual(op.OpStartDepth.Value, 20.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 5.0, places=6)

    def test_coplanar_lowest_faces_are_all_target_faces(self):
        left, right, high = self._make_plate_model_with_target_faces()
        job = PathJob.Create("Job", [left, right, high], None)
        job.GeometryTolerance.Value = 0.001
        job.Stock = self._make_stock(job)
        self.assertSuccessfulRecompute(self.doc)

        _job, _model, op = self._create_operation(
            name="coplanar_lowest_faces",
            job=job,
            model=left,
            base=[
                (left, [self._highest_horizontal_face_name(left.Shape)]),
                (right, [self._highest_horizontal_face_name(right.Shape)]),
                (high, [self._highest_horizontal_face_name(high.Shape)]),
            ],
            step_down=5.0,
        )

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertAlmostEqual(final_depth, 5.0, places=6)
        self.assertEqual(len(target_faces), 2)
        for face in target_faces:
            self.assertAlmostEqual(PathVolumeFaceMillUtils.face_z(face), 5.0, places=6)

    def test_base_belongs_to_job_model_accepts_wrapped_model_group_member(self):
        model = self._make_model_with_boss()
        wrapped_member = types.SimpleNamespace(
            Objects=[model],
            LinkedObject=None,
            Source=None,
        )
        fake_job = types.SimpleNamespace(Model=types.SimpleNamespace(Group=[wrapped_member]))
        fake_op = types.SimpleNamespace()

        with mock.patch.object(PathVolumeFaceMillUtils, "find_job", return_value=fake_job):
            self.assertEqual(PathVolumeFaceMillUtils.job_model_group(fake_op), [wrapped_member])
            self.assertTrue(PathVolumeFaceMillUtils.base_belongs_to_job_model(fake_op, model))

    def test_base_belongs_to_job_model_rejects_non_model_container_that_wraps_model(self):
        model = self._make_model_with_boss()
        wrapped_member = types.SimpleNamespace(
            Objects=[model],
            LinkedObject=None,
            Source=None,
        )
        non_model_container = types.SimpleNamespace(
            Objects=[model],
            LinkedObject=None,
            Source=None,
        )
        fake_job = types.SimpleNamespace(Model=types.SimpleNamespace(Group=[wrapped_member]))
        fake_op = types.SimpleNamespace()

        with mock.patch.object(PathVolumeFaceMillUtils, "find_job", return_value=fake_job):
            self.assertFalse(
                PathVolumeFaceMillUtils.base_belongs_to_job_model(fake_op, non_model_container)
            )

    def test_selected_model_horizontal_faces_ignore_non_model_container_wrapping_model(self):
        model = self._make_model_with_boss()
        target_face_name = self._highest_horizontal_face_name(model.Shape)
        target_face = model.Shape.getElement(target_face_name)
        wrapped_member = types.SimpleNamespace(
            Objects=[model],
            LinkedObject=None,
            Source=None,
        )
        non_model_container = types.SimpleNamespace(
            Objects=[model],
            LinkedObject=None,
            Source=None,
        )
        fake_job = types.SimpleNamespace(Model=types.SimpleNamespace(Group=[wrapped_member]))
        fake_op = types.SimpleNamespace()
        fake_selection = [{"base": non_model_container, "shape": target_face}]

        with mock.patch.object(PathVolumeFaceMillUtils, "find_job", return_value=fake_job):
            with mock.patch.object(
                PathVolumeFaceMillUtils,
                "iter_selected_subobjects",
                return_value=fake_selection,
            ):
                self.assertEqual(
                    PathVolumeFaceMillUtils.selected_model_horizontal_faces(fake_op), []
                )

    def test_selected_model_horizontal_face_sets_final_depth(self):
        model = self._make_full_plate_model(
            size=FreeCAD.Vector(100, 100, 10),
            base=FreeCAD.Vector(0, 0, 5),
        )
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 30))
        target_face = self._highest_horizontal_face_name(model.Shape)

        _job, _model, op = self._create_operation(
            name="model_face_sets_depth",
            job=job,
            model=model,
            base=[(model, [target_face])],
            step_down=5.0,
        )

        expected_z = PathVolumeFaceMillUtils.face_z(model.Shape.getElement(target_face))
        self.assertAlmostEqual(op.OpFinalDepth.Value, expected_z, places=6)

    def test_model_and_fixture_selected_model_face_controls_depth(self):
        job, model = self._make_job_with_stock_and_model()
        model_top = self._highest_horizontal_face_name(model.Shape)
        fixture = self._make_fixture_box(
            "FixtureHigher",
            FreeCAD.Vector(10, 10, 15),
            FreeCAD.Vector(20, 20, 3),
        )
        fixture_top = self._highest_horizontal_face_name(fixture.Shape)

        _job, _model, op = self._create_operation(
            name="model_and_fixture_depth",
            job=job,
            model=model,
            base=[(fixture, [fixture_top]), (model, [model_top])],
            step_down=5.0,
        )

        expected_z = PathVolumeFaceMillUtils.face_z(model.Shape.getElement(model_top))
        self.assertAlmostEqual(op.OpFinalDepth.Value, expected_z, places=6)

    def test_no_horizontal_face_defaults_to_stock_bottom(self):
        job, model = self._make_job_with_stock_and_model()
        _job, _model, op = self._create_operation(
            name="vertical_only_defaults_to_stock_bottom",
            job=job,
            model=model,
            base=[(model, self._vertical_face_names(model.Shape))],
        )

        stock_bb = job.Stock.Shape.BoundBox
        self.assertAlmostEqual(op.OpFinalDepth.Value, stock_bb.ZMin, places=6)

    def test_safe_and_clearance_heights_default_to_highest_stock_or_model_plus_margin(self):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(100, 100, 20),
            FreeCAD.Vector(0, 0, 0),
        )

        _job, _model, op = self._create_operation(
            name="default_safe_heights",
            job=job,
            model=model,
            override_heights=False,
        )

        self.assertAlmostEqual(op.SafeHeight.Value, 50.0, places=6)
        self.assertAlmostEqual(op.ClearanceHeight.Value, 50.0, places=6)

    def test_user_can_lower_default_safe_and_clearance_heights(self):
        _job, _model, op = self._create_operation(
            name="manual_safe_height_override",
            override_heights=False,
        )

        self.assertAlmostEqual(op.SafeHeight.Value, 25.0, places=6)
        self.assertAlmostEqual(op.ClearanceHeight.Value, 25.0, places=6)

        op.setExpression("SafeHeight", None)
        op.SafeHeight.Value = 12.0
        op.setExpression("ClearanceHeight", None)
        op.ClearanceHeight.Value = 12.0
        op.ClearEdges = True
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertAlmostEqual(op.SafeHeight.Value, 12.0, places=6)
        self.assertAlmostEqual(op.ClearanceHeight.Value, 12.0, places=6)

    def test_allowance_properties_default_to_linked_xy_z_contract(self):
        _job, _model, op = self._create_operation(name="allowance_defaults")

        for prop_name in (
            "FeatureAllowanceMode",
            "FeatureAllowanceXY",
            "FeatureAllowanceZ",
            "StockAllowanceMode",
            "StockAllowanceXY",
            "StockAllowanceZ",
        ):
            self.assertTrue(hasattr(op, prop_name), f"Missing allowance property {prop_name}")

        for legacy_prop_name in (
            "FeatureAllowanceX",
            "FeatureAllowanceY",
            "StockAllowanceX",
            "StockAllowanceY",
        ):
            self.assertFalse(
                hasattr(op, legacy_prop_name), f"Unexpected legacy property {legacy_prop_name}"
            )

        self.assertEqual(op.FeatureAllowanceMode, "Linked")
        self.assertEqual(op.StockAllowanceMode, "Linked")
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.0, places=6)

        self.assertEqual(
            list(op.getEnumerationsOfProperty("FeatureAllowanceMode")),
            ["Linked", "Independent"],
        )
        self.assertEqual(
            list(op.getEnumerationsOfProperty("StockAllowanceMode")),
            ["Linked", "Independent"],
        )

    def test_stock_edge_clearance_properties_exist(self):
        _job, _model, op = self._create_operation(name="edge_clearance_props")

        self.assertTrue(hasattr(op, "StockEdgeClearanceX"))
        self.assertTrue(hasattr(op, "StockEdgeClearanceY"))

    def test_stock_edge_clearance_defaults_to_tool_radius_plus_margin(self):
        _job, _model, op = self._create_operation(
            name="edge_clearance_defaults",
            tool_diameter=20.0,
        )

        self.assertSuccessfulRecompute(self.doc, op)

        expected = 10.0 + 0.1
        self.assertAlmostEqual(op.StockEdgeClearanceX.Value, expected, delta=1e-6)
        self.assertAlmostEqual(op.StockEdgeClearanceY.Value, expected, delta=1e-6)

    def test_stock_edge_clearance_setup_properties(self):
        setup_properties = PathVolumeFaceMill.SetupProperties()

        self.assertIn("StockEdgeClearanceX", setup_properties)
        self.assertIn("StockEdgeClearanceY", setup_properties)
        self.assertIn("ClearEdges", setup_properties)

    def test_cutting_strategy_property_defaults_to_strict_raster(self):
        _job, _model, op = self._create_operation(name="cutting_strategy_defaults")

        self.assertTrue(hasattr(op, "CuttingStrategy"))
        self.assertEqual(op.CuttingStrategy, "StrictRaster")
        self.assertEqual(
            list(op.getEnumerationsOfProperty("CuttingStrategy")),
            ["StrictRaster", "SquareSpiral", "RoundSpiral", "OffsetLoops", "Auto"],
        )

    def test_cutting_strategy_labels_match_target_plan(self):
        raw_enums = PathVolumeFaceMill.ObjectVolumeFaceMill.propertyEnumerations(dataType="raw")

        self.assertEqual(
            raw_enums["CuttingStrategy"],
            [
                ("Strict raster", "StrictRaster"),
                ("Square spiral", "SquareSpiral"),
                ("Round spiral", "RoundSpiral"),
                ("Offset loops", "OffsetLoops"),
                ("Auto", "Auto"),
            ],
        )

    def test_cutting_strategy_setup_properties_keep_clearing_pattern_compatibility(self):
        setup_properties = PathVolumeFaceMill.SetupProperties()

        self.assertIn("CuttingStrategy", setup_properties)
        self.assertIn("ClearingPattern", setup_properties)

    def test_material_state_mode_defaults_to_full_stock(self):
        _job, _model, op = self._create_operation(name="material_state_default")

        self.assertTrue(hasattr(op, "MaterialStateMode"))
        self.assertEqual(op.MaterialStateMode, "FullStock")
        self.assertEqual(
            list(op.getEnumerationsOfProperty("MaterialStateMode")),
            ["FullStock", "RemainingMaterial"],
        )

    def test_material_state_mode_is_in_setup_properties(self):
        self.assertIn("MaterialStateMode", PathVolumeFaceMill.SetupProperties())

    def test_material_state_mode_restore_backfills_missing_property(self):
        _job, _model, op = self._create_operation(
            name="material_state_restore_backfill",
            override_heights=False,
        )

        self.assertTrue(hasattr(op, "MaterialStateMode"))
        op.removeProperty("MaterialStateMode")
        self.assertFalse(hasattr(op, "MaterialStateMode"))

        op.Proxy.opOnDocumentRestored(op)

        self.assertTrue(hasattr(op, "MaterialStateMode"))
        self.assertEqual(op.MaterialStateMode, "FullStock")
        self.assertEqual(
            list(op.getEnumerationsOfProperty("MaterialStateMode")),
            ["FullStock", "RemainingMaterial"],
        )

    def test_material_state_full_stock_generates_path(self):
        _job, _model, op = self._create_operation(name="material_state_full_stock")
        op.MaterialStateMode = "FullStock"
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertGreater(len(self._cutting_moves(op.Path)), 0)
        self.assertFalse(op.removalshape.isNull())

    def test_material_state_remaining_material_fails_safely_in_phase_4(self):
        _job, _model, op = self._create_operation(name="material_state_remaining_material")
        op.MaterialStateMode = "RemainingMaterial"

        self.assertSuccessfulRecompute(self.doc, op)

        self.assertEqual(len(self._cutting_moves(op.Path)), 0)
        self.assertTrue(op.removalshape.isNull())

    def test_material_state_mode_round_trip_preserves_persisted_value(self):
        _job, _model, op = self._create_operation(name="material_state_round_trip")
        op.MaterialStateMode = "RemainingMaterial"
        self.assertSuccessfulRecompute(self.doc, op)

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".FCStd")
        temp_file.close()
        reopened = None
        original_doc_name = self.doc.Name

        try:
            self.doc.saveAs(temp_file.name)
            reopened = FreeCAD.openDocument(temp_file.name)

            restored = reopened.getObject(op.Name)
            self.assertIsNotNone(restored)
            self.assertTrue(hasattr(restored, "MaterialStateMode"))
            self.assertEqual(restored.MaterialStateMode, "RemainingMaterial")
            self.assertEqual(len(self._cutting_moves(restored.Path)), 0)
            self.assertTrue(restored.removalshape.isNull())
        finally:
            if reopened is not None:
                FreeCAD.closeDocument(reopened.Name)
            if original_doc_name in FreeCAD.listDocuments():
                self.doc = FreeCAD.getDocument(original_doc_name)
            else:
                self.doc = FreeCAD.newDocument("TestPathVolumeFaceMill")
            os.unlink(temp_file.name)

    def test_material_state_mode_setup_sheet_template_round_trip(self):
        opname = "VolumeFaceMillMaterialStateTemplate"
        previous_registration = PathSetupSheet._RegisteredOps.get(opname)
        setup_sheet = PathSetupSheet.Create()
        restored_setup_sheet = None

        try:
            self.assertSuccessfulRecompute(self.doc, setup_sheet)
            PathSetupSheet.RegisterOperation(
                opname,
                PathVolumeFaceMill.Create,
                PathVolumeFaceMill.SetupProperties,
            )

            prototype = PathSetupSheet._RegisteredOps[opname].prototype("material_state_template")
            material_state_property = prototype.getProperty("MaterialStateMode")
            self.assertEqual(
                material_state_property.getEnumValues(),
                ["FullStock", "RemainingMaterial"],
            )
            material_state_property.setupProperty(
                setup_sheet,
                PathSetupSheet.OpPropertyName(opname, material_state_property.name),
                PathSetupSheet.OpPropertyGroup(opname),
                "RemainingMaterial",
            )

            attrs = setup_sheet.Proxy.templateAttributes(False, False, False, False, [opname])
            encoded = setup_sheet.Proxy.encodeTemplateAttributes(attrs)

            restored_setup_sheet = PathSetupSheet.Create()
            self.assertSuccessfulRecompute(self.doc, restored_setup_sheet)
            restored_setup_sheet.Proxy.setFromTemplate(encoded)

            _job, _model, op = self._create_operation(name="material_state_setup_sheet_target")
            self.assertEqual(op.MaterialStateMode, "FullStock")

            restored_setup_sheet.Proxy.setOperationProperties(op, opname)
            self.assertEqual(op.MaterialStateMode, "RemainingMaterial")
        finally:
            if previous_registration is None:
                PathSetupSheet._RegisteredOps.pop(opname, None)
            else:
                PathSetupSheet._RegisteredOps[opname] = previous_registration

    def test_setup_sheet_prototype_material_state_mode_enum_values_are_initialized(self):
        opname = "VolumeFaceMillMaterialStatePrototypeEnums"
        prototype, previous_registration = self._make_volume_face_mill_setup_sheet_prototype(opname)

        try:
            material_state_property = prototype.properties["MaterialStateMode"]
            self.assertEqual(
                list(material_state_property.getEnumValues()),
                ["FullStock", "RemainingMaterial"],
            )
        finally:
            if previous_registration is None:
                PathSetupSheet._RegisteredOps.pop(opname, None)
            else:
                PathSetupSheet._RegisteredOps[opname] = previous_registration

    def test_setup_sheet_prototype_volume_face_mill_enum_values_are_initialized(self):
        opname = "VolumeFaceMillPrototypeAllEnums"
        prototype, previous_registration = self._make_volume_face_mill_setup_sheet_prototype(opname)

        try:
            expected_enums = {
                "CuttingStrategy": [
                    "StrictRaster",
                    "SquareSpiral",
                    "RoundSpiral",
                    "OffsetLoops",
                    "Auto",
                ],
                "MaterialStateMode": [
                    "FullStock",
                    "RemainingMaterial",
                ],
                "FeatureAllowanceMode": [
                    "Linked",
                    "Independent",
                ],
                "StockAllowanceMode": [
                    "Linked",
                    "Independent",
                ],
                "OptimizationMode": [
                    "None",
                    "MinTravel",
                ],
            }

            for prop_name, expected_values in expected_enums.items():
                with self.subTest(prop_name=prop_name):
                    prop = prototype.properties[prop_name]
                    self.assertEqual(list(prop.getEnumValues()), expected_values)
        finally:
            if previous_registration is None:
                PathSetupSheet._RegisteredOps.pop(opname, None)
            else:
                PathSetupSheet._RegisteredOps[opname] = previous_registration

    def test_allowance_setup_properties_use_xy_z_contract(self):
        setup_properties = PathVolumeFaceMill.SetupProperties()

        for prop_name in (
            "FeatureAllowanceMode",
            "FeatureAllowanceXY",
            "FeatureAllowanceZ",
            "StockAllowanceMode",
            "StockAllowanceXY",
            "StockAllowanceZ",
        ):
            self.assertIn(prop_name, setup_properties)

        for legacy_prop_name in (
            "FeatureAllowanceX",
            "FeatureAllowanceY",
            "StockAllowanceX",
            "StockAllowanceY",
        ):
            self.assertNotIn(legacy_prop_name, setup_properties)

    def test_restore_backfills_allowance_properties_from_legacy_xy_props(self):
        _job, _model, op = self._create_operation(
            name="allowance_restore_backfill",
            override_heights=False,
        )

        op.addProperty(
            "App::PropertyDistance",
            "FeatureAllowanceX",
            "Volume Face Mill",
            "Legacy feature X allowance.",
        )
        op.addProperty(
            "App::PropertyDistance",
            "FeatureAllowanceY",
            "Volume Face Mill",
            "Legacy feature Y allowance.",
        )
        op.addProperty(
            "App::PropertyDistance",
            "StockAllowanceX",
            "Volume Face Mill",
            "Legacy stock X allowance.",
        )
        op.addProperty(
            "App::PropertyDistance",
            "StockAllowanceY",
            "Volume Face Mill",
            "Legacy stock Y allowance.",
        )

        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.0, places=6)

        op.FeatureAllowanceX = 1.25
        op.FeatureAllowanceY = 2.5
        op.StockAllowanceX = 3.75
        op.StockAllowanceY = 2.0

        op.Proxy.opOnDocumentRestored(op)

        self.assertEqual(op.FeatureAllowanceMode, "Linked")
        self.assertEqual(op.StockAllowanceMode, "Linked")
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 2.5, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 3.75, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.0, places=6)

        for legacy_prop_name in (
            "FeatureAllowanceX",
            "FeatureAllowanceY",
            "StockAllowanceX",
            "StockAllowanceY",
        ):
            self.assertTrue(hasattr(op, legacy_prop_name))
            editor_mode = op.getEditorMode(legacy_prop_name)
            if isinstance(editor_mode, list):
                self.assertIn("Hidden", editor_mode)
            else:
                self.assertIn(editor_mode, (2, "Hidden"))

    def test_cutting_strategy_restore_maps_legacy_clearing_pattern_when_property_missing(self):
        proxy = PathVolumeFaceMill.ObjectVolumeFaceMill.__new__(
            PathVolumeFaceMill.ObjectVolumeFaceMill
        )
        proxy._initialize_runtime_state()

        for clearing_pattern, expected_strategy in (
            ("Offset", "OffsetLoops"),
            ("ZigZag", "StrictRaster"),
        ):
            obj = _FakePropertyObject(clearing_pattern=clearing_pattern)
            added_properties = proxy._add_properties(obj)

            for name, values in proxy.propertyEnumerations():
                if hasattr(obj, name):
                    setattr(obj, name, values)

            proxy._initialize_cutting_strategy_property(obj, added_properties)
            self.assertEqual(obj.CuttingStrategy, expected_strategy)

    def test_strict_raster_ignores_hidden_compatibility_clearing_pattern(self):
        for clearing_pattern in ("ZigZag", "Line", "Grid", "Offset", "ZigZagOffset"):
            _job, _model, op = self._create_operation(
                name=f"strict_raster_{clearing_pattern}",
                clearing_pattern=clearing_pattern,
                override_heights=False,
            )
            op.CuttingStrategy = "StrictRaster"
            op.Proxy.radius = self._tool_radius(op)
            self.assertEqual(op.ClearingPattern, clearing_pattern)
            self.assertEqual(op.Proxy.areaOpAreaParams(op, False)["PocketMode"], 1)

            path_params = op.Proxy.areaOpPathParams(op, False)
            if clearing_pattern in {"Offset", "ZigZagOffset"}:
                self.assertNotIn("sort_mode", path_params)

    def test_unimplemented_cutting_strategy_aborts_safely(self):
        _job, _model, op = self._create_operation(name="unimplemented_strategy")
        op.CuttingStrategy = "SquareSpiral"
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertEqual(len(self._cutting_moves(op.Path)), 0)
        self.assertTrue(op.removalshape.isNull())

    def test_cutting_strategy_live_change_reports_unsupported_strategy_once(self):
        _job, _model, op = self._create_operation(name="cutting_strategy_live_change_error")
        error_messages = []

        def capture_error(message):
            error_messages.append(str(message))
            return None

        with mock.patch.object(Path.Log, "error", side_effect=capture_error):
            op.CuttingStrategy = "SquareSpiral"

        self.assertEqual(op.CuttingStrategy, "SquareSpiral")
        strategy_errors = [
            message
            for message in error_messages
            if "Selected Volume Face Mill cutting strategy is not implemented yet." in message
        ]
        self.assertEqual(len(strategy_errors), 1)

    def test_cutting_strategy_round_trip_preserves_persisted_value(self):
        _job, _model, op = self._create_operation(name="cutting_strategy_round_trip")
        op.CuttingStrategy = "Auto"

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".FCStd")
        temp_file.close()
        reopened = None
        original_doc_name = self.doc.Name

        try:
            self.doc.saveAs(temp_file.name)
            reopened = FreeCAD.openDocument(temp_file.name)

            restored = reopened.getObject(op.Name)
            self.assertIsNotNone(restored)
            self.assertTrue(hasattr(restored, "CuttingStrategy"))
            self.assertEqual(restored.CuttingStrategy, "Auto")
        finally:
            if reopened is not None:
                FreeCAD.closeDocument(reopened.Name)
            if original_doc_name in FreeCAD.listDocuments():
                self.doc = FreeCAD.getDocument(original_doc_name)
            else:
                self.doc = FreeCAD.newDocument("TestPathVolumeFaceMill")
            os.unlink(temp_file.name)

    def test_cutting_strategy_restore_does_not_repeat_validation_errors(self):
        _job, _model, op = self._create_operation(name="cutting_strategy_restore_errors")
        op.CuttingStrategy = "Auto"

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".FCStd")
        temp_file.close()
        reopened = None
        error_messages = []
        original_doc_name = self.doc.Name

        def capture_error(message):
            error_messages.append(str(message))
            return None

        try:
            self.doc.saveAs(temp_file.name)
            with mock.patch.object(Path.Log, "error", side_effect=capture_error):
                reopened = FreeCAD.openDocument(temp_file.name)

            restored = reopened.getObject(op.Name)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.CuttingStrategy, "Auto")

            strategy_errors = [
                message
                for message in error_messages
                if "Selected Volume Face Mill cutting strategy is not implemented yet." in message
            ]
            self.assertEqual(len(strategy_errors), 0)
        finally:
            if reopened is not None:
                FreeCAD.closeDocument(reopened.Name)
            if original_doc_name in FreeCAD.listDocuments():
                self.doc = FreeCAD.getDocument(original_doc_name)
            else:
                self.doc = FreeCAD.newDocument("TestPathVolumeFaceMill")
            os.unlink(temp_file.name)

    def test_restore_recovers_missing_runtime_sync_flags(self):
        _job, _model, op = self._create_operation(
            name="allowance_restore_missing_runtime_flags",
            override_heights=False,
        )

        for attr_name in (
            "_forcing_compatibility_properties",
            "_syncing_allowances",
            "_syncing_depths",
        ):
            if hasattr(op.Proxy, attr_name):
                delattr(op.Proxy, attr_name)

        op.Proxy.areaOpOnChanged(op, "FeatureAllowanceXY")
        self.assertTrue(hasattr(op.Proxy, "_syncing_allowances"))
        self.assertTrue(hasattr(op.Proxy, "_syncing_depths"))
        self.assertTrue(hasattr(op.Proxy, "_forcing_compatibility_properties"))

        for attr_name in (
            "_forcing_compatibility_properties",
            "_syncing_allowances",
            "_syncing_depths",
        ):
            if hasattr(op.Proxy, attr_name):
                delattr(op.Proxy, attr_name)

        op.Proxy.opOnDocumentRestored(op)
        self.assertEqual(op.FeatureAllowanceMode, "Linked")
        self.assertEqual(op.StockAllowanceMode, "Linked")
        self.assertTrue(hasattr(op.Proxy, "_syncing_allowances"))
        self.assertTrue(hasattr(op.Proxy, "_syncing_depths"))
        self.assertTrue(hasattr(op.Proxy, "_forcing_compatibility_properties"))

    def test_restored_legacy_allowance_props_do_not_drive_geometry(self):
        job, model = self._make_job_with_stock_and_model()
        target_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="allowance_legacy_props_do_not_drive_geometry",
            job=job,
            model=model,
            base=[(model, [target_face])],
            step_down=5.0,
        )

        for prop_name in (
            "FeatureAllowanceX",
            "FeatureAllowanceY",
            "StockAllowanceX",
            "StockAllowanceY",
        ):
            if not hasattr(op, prop_name):
                op.addProperty(
                    "App::PropertyDistance", prop_name, "Volume Face Mill", f"Legacy {prop_name}."
                )

        op.FeatureAllowanceX = 2.0
        op.FeatureAllowanceY = 3.0
        op.StockAllowanceX = 4.0
        op.StockAllowanceY = 1.0
        op.Proxy.opOnDocumentRestored(op)

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 0.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 1.5)
        self._set_allowance_distance(op, "StockAllowanceXY", 5.0)
        self._set_allowance_distance(op, "StockAllowanceZ", 0.0)

        op.FeatureAllowanceX = 25.0
        op.FeatureAllowanceY = 30.0
        op.StockAllowanceX = 40.0
        op.StockAllowanceY = 45.0
        self.assertSuccessfulRecompute(self.doc, op)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        self.assertAlmostEqual(op.OpFinalDepth.Value, 11.5, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 11.5, places=6)
        self.assertAlmostEqual(removal.BoundBox.XMin, 5.0, places=6)
        self.assertAlmostEqual(removal.BoundBox.YMin, 5.0, places=6)
        self.assertAlmostEqual(removal.BoundBox.XMax, 95.0, places=6)
        self.assertAlmostEqual(removal.BoundBox.YMax, 95.0, places=6)

    def test_linked_feature_allowance_xy_and_z_synchronize_both_directions(self):
        _job, _model, op = self._create_operation(name="linked_feature_allowance_sync")

        self._set_allowance_distance(op, "FeatureAllowanceXY", 0.5)
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 0.5, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 0.5, places=6)

        self._set_allowance_distance(op, "FeatureAllowanceZ", 1.25)
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 1.25, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 1.25, places=6)

    def test_linked_stock_allowance_xy_and_z_synchronize_both_directions(self):
        _job, _model, op = self._create_operation(name="linked_stock_allowance_sync")

        self._set_allowance_distance(op, "StockAllowanceXY", 0.75)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.75, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.75, places=6)

        self._set_allowance_distance(op, "StockAllowanceZ", 1.5)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 1.5, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 1.5, places=6)

    def test_feature_allowance_switching_to_linked_synchronizes_xy_z_to_safe_value(self):
        _job, _model, op = self._create_operation(name="feature_allowance_link_sync")

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 5.0)

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 5.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 5.0, places=6)

    def test_stock_allowance_switching_to_linked_synchronizes_xy_z_to_safe_value(self):
        _job, _model, op = self._create_operation(name="stock_allowance_link_sync")

        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "StockAllowanceXY", 2.0)
        self._set_allowance_distance(op, "StockAllowanceZ", 7.0)

        self._set_allowance_mode(op, "StockAllowanceMode", "Linked")
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 7.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 7.0, places=6)

    def test_switching_to_linked_clamps_negative_allowances_before_sync(self):
        _job, _model, op = self._create_operation(name="linked_negative_allowance_clamp")

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        op.setExpression("FeatureAllowanceXY", None)
        op.setExpression("FeatureAllowanceZ", None)
        op.FeatureAllowanceXY.Value = -2.0
        op.FeatureAllowanceZ.Value = 3.0

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")

        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 3.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 3.0, places=6)

    def test_negative_allowances_are_clamped_non_negative(self):
        _job, _model, op = self._create_operation(name="negative_allowance_clamp")

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")

        self._set_allowance_distance(op, "FeatureAllowanceXY", -1.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", -2.0)
        self._set_allowance_distance(op, "StockAllowanceXY", -3.0)
        self._set_allowance_distance(op, "StockAllowanceZ", -4.0)

        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.0, places=6)

    def test_negative_stock_edge_clearances_are_clamped_non_negative(self):
        _job, _model, op = self._create_operation(name="negative_edge_clearance_clamp")

        self._set_allowance_distance(op, "StockEdgeClearanceX", -1.0)
        self._set_allowance_distance(op, "StockEdgeClearanceY", -2.0)

        self.assertAlmostEqual(op.StockEdgeClearanceX.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockEdgeClearanceY.Value, 0.0, places=6)

    def test_allowance_ui_panel_exposes_linked_and_independent_contract_widgets(self):
        ui_path = self._volume_face_mill_ui_path()
        root = ET.parse(ui_path).getroot()

        widget_names = {
            widget.attrib.get("name") for widget in root.iter("widget") if widget.attrib.get("name")
        }

        for widget_name in (
            "cuttingStrategy",
            "cuttingStrategy_label",
            "featureAllowanceMode",
            "featureAllowanceLinkedFrame",
            "featureAllowanceLinked",
            "featureAllowanceIndependentFrame",
            "featureAllowanceXY",
            "featureAllowanceZ",
            "stockAllowanceMode",
            "stockAllowanceLinkedFrame",
            "stockAllowanceLinked",
            "stockAllowanceIndependentFrame",
            "stockAllowanceXY",
            "stockAllowanceZ",
        ):
            self.assertIn(widget_name, widget_names)

    def test_volume_face_mill_ui_has_stock_edge_clearance_widgets(self):
        ui_path = self._volume_face_mill_ui_path()
        root = ET.parse(ui_path).getroot()

        widget_names = {
            widget.attrib.get("name") for widget in root.iter("widget") if widget.attrib.get("name")
        }

        self.assertIn("stockEdgeClearanceX", widget_names)
        self.assertIn("stockEdgeClearanceY", widget_names)
        self.assertIn("stockEdgeClearanceX_label", widget_names)
        self.assertIn("stockEdgeClearanceY_label", widget_names)

    def test_allowance_ui_tooltips_describe_job_axes_and_xy_contract(self):
        ui_path = self._volume_face_mill_ui_path()
        root = ET.parse(ui_path).getroot()

        tooltips = {}
        for widget in root.iter("widget"):
            name = widget.attrib.get("name")
            if not name:
                continue

            for prop in widget.iter("property"):
                if prop.attrib.get("name") != "toolTip":
                    continue
                string_node = prop.find("string")
                if string_node is not None:
                    tooltips[name] = "".join(string_node.itertext())
                break

        self.assertIn("job/world axes", tooltips["featureAllowanceMode"])
        self.assertIn("not supported in this phase", tooltips["featureAllowanceMode"])
        self.assertIn(
            "Operation placement or workplane rotation does not rotate these allowance directions.",
            tooltips["featureAllowanceMode"],
        )
        self.assertIn("strict face-milling toolpaths", tooltips["cuttingStrategy"])
        self.assertIn("implemented in later phases", tooltips["cuttingStrategy"])
        self.assertIn("full Job stock", tooltips["materialStateMode"])
        self.assertIn(
            "Remaining material is reserved for a later implementation phase.",
            tooltips["materialStateMode"],
        )
        self.assertIn("job/world XY plane", tooltips["featureAllowanceXY"])
        self.assertIn("job/world +Z axis", tooltips["featureAllowanceZ"])
        self.assertIn("job/world axes", tooltips["stockAllowanceMode"])
        self.assertIn("not supported in this phase", tooltips["stockAllowanceMode"])
        self.assertIn(
            "Operation placement or workplane rotation does not rotate these allowance directions.",
            tooltips["stockAllowanceMode"],
        )
        self.assertIn("job/world XY stock boundary", tooltips["stockAllowanceXY"])
        self.assertIn("stock/world-Z oriented", tooltips["optimizationMode"])
        self.assertIn(
            "X and Y overhang distances are controlled by the edge clearance fields.",
            tooltips["clearEdges"],
        )
        self.assertIn(
            "This does not override stock allowance material-to-leave.",
            tooltips["clearEdges"],
        )
        self.assertIn("tool radius plus 0.1 mm", tooltips["stockEdgeClearanceX"])
        self.assertIn("tool radius plus 0.1 mm", tooltips["stockEdgeClearanceY"])
        self.assertIn(
            "Selected geometry does not redefine the stock boundary.",
            tooltips["protectSelectedFeatures"],
        )
        self.assertIn("Target detection uses Job/world Z.", tooltips["protectSelectedFeatures"])

    def test_volume_face_mill_ui_uses_cutting_strategy_not_clearing_pattern(self):
        ui_path = self._volume_face_mill_ui_path()
        tree = ET.parse(ui_path)
        root = tree.getroot()

        widget_names = {
            widget.attrib.get("name") for widget in root.iter("widget") if widget.attrib.get("name")
        }

        self.assertIn("cuttingStrategy", widget_names)
        self.assertIn("cuttingStrategy_label", widget_names)
        self.assertNotIn("clearingPattern", widget_names)
        self.assertNotIn("clearingPattern_label", widget_names)

    def test_volume_face_mill_ui_contains_material_state_mode(self):
        tree = ET.parse(self._volume_face_mill_ui_path())
        names = {element.attrib.get("name") for element in tree.iter() if "name" in element.attrib}

        self.assertIn("materialStateMode", names)
        self.assertIn("materialStateMode_label", names)

    def test_volume_face_mill_gui_binds_cutting_strategy_not_clearing_pattern(self):
        gui_path = self._volume_face_mill_gui_path()
        with open(gui_path, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn('("cuttingStrategy", "CuttingStrategy")', source)
        self.assertNotIn('("clearingPattern", "ClearingPattern")', source)
        self.assertIn('("materialStateMode", "MaterialStateMode")', source)
        self.assertIn("self.form.materialStateMode.currentIndexChanged", source)
        self.assertIn('"MaterialStateMode"', source)

    def test_material_state_gui_controller_round_trips_task_panel_selection(self):
        _job, _model, op = self._create_operation(name="material_state_gui_roundtrip")
        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            toolController=object(),
            coolantController=object(),
            cutMode=_FakeComboBox(op.CutMode),
            cuttingStrategy=_FakeComboBox(op.CuttingStrategy),
            optimizationMode=_FakeComboBox(op.OptimizationMode),
            materialStateMode=_FakeComboBox("RemainingMaterial"),
            stepOverPercent=_FakeSpinBoxWidget(op.StepOver),
            extraOffset=_FakeTextWidget(),
            angle=_FakeTextWidget(),
            protectSelectedFeatures=_FakeCheckBox(op.ProtectSelectedFeatures),
            clearEdges=_FakeCheckBox(op.ClearEdges),
            useStartPoint=_FakeCheckBox(op.UseStartPoint),
            featureAllowanceMode=_FakeComboBox(op.FeatureAllowanceMode),
            stockAllowanceMode=_FakeComboBox(op.StockAllowanceMode),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(op.FeatureAllowanceXY.Value),
            featureAllowanceXY=_FakeQuantityWidget(op.FeatureAllowanceXY.Value),
            featureAllowanceZ=_FakeQuantityWidget(op.FeatureAllowanceZ.Value),
            stockAllowanceLinked=_FakeQuantityWidget(op.StockAllowanceXY.Value),
            stockAllowanceXY=_FakeQuantityWidget(op.StockAllowanceXY.Value),
            stockAllowanceZ=_FakeQuantityWidget(op.StockAllowanceZ.Value),
            stockEdgeClearanceX=_FakeQuantityWidget(op.StockEdgeClearanceX.Value),
            stockEdgeClearanceY=_FakeQuantityWidget(op.StockEdgeClearanceY.Value),
        )
        controller.initPage(op)

        controller.getFields(op)
        self.assertEqual(op.MaterialStateMode, "RemainingMaterial")

        controller.form.materialStateMode._data = "FullStock"
        op.MaterialStateMode = "RemainingMaterial"
        controller.setFields(op)
        self.assertEqual(controller.form.materialStateMode._data, "RemainingMaterial")

    def test_allowance_gui_controller_linked_mode_writes_xy_and_z_and_hides_independent_fields(
        self,
    ):
        _job, _model, op = self._create_operation(name="allowance_gui_linked_mode")
        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            featureAllowanceMode=_FakeComboBox("Linked"),
            stockAllowanceMode=_FakeComboBox("Linked"),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(1.5),
            featureAllowanceXY=_FakeQuantityWidget(9.0),
            featureAllowanceZ=_FakeQuantityWidget(8.0),
            stockAllowanceLinked=_FakeQuantityWidget(0.75),
            stockAllowanceXY=_FakeQuantityWidget(7.0),
            stockAllowanceZ=_FakeQuantityWidget(6.0),
            stockEdgeClearanceX=_FakeQuantityWidget(0.0),
            stockEdgeClearanceY=_FakeQuantityWidget(0.0),
        )
        controller.initPage(op)
        controller._update_allowance_properties_from_form(op)

        self.assertEqual(op.FeatureAllowanceMode, "Linked")
        self.assertEqual(op.StockAllowanceMode, "Linked")
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 1.5, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 1.5, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.75, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.75, places=6)
        self.assertTrue(controller.form.featureAllowanceLinkedFrame.visible)
        self.assertFalse(controller.form.featureAllowanceIndependentFrame.visible)
        self.assertTrue(controller.form.stockAllowanceLinkedFrame.visible)
        self.assertFalse(controller.form.stockAllowanceIndependentFrame.visible)

    def test_allowance_gui_controller_independent_mode_preserves_distinct_values_and_shows_xy_z_fields(
        self,
    ):
        _job, _model, op = self._create_operation(name="allowance_gui_independent_mode")
        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            featureAllowanceMode=_FakeComboBox("Independent"),
            stockAllowanceMode=_FakeComboBox("Independent"),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(0.0),
            featureAllowanceXY=_FakeQuantityWidget(1.0),
            featureAllowanceZ=_FakeQuantityWidget(2.5),
            stockAllowanceLinked=_FakeQuantityWidget(0.0),
            stockAllowanceXY=_FakeQuantityWidget(1.25),
            stockAllowanceZ=_FakeQuantityWidget(0.5),
            stockEdgeClearanceX=_FakeQuantityWidget(0.0),
            stockEdgeClearanceY=_FakeQuantityWidget(0.0),
        )
        controller.initPage(op)

        controller._update_allowance_properties_from_form(op)
        self.assertEqual(op.FeatureAllowanceMode, "Independent")
        self.assertEqual(op.StockAllowanceMode, "Independent")
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.0, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.0, places=6)

        controller.form.featureAllowanceXY.value = 1.0
        controller.form.featureAllowanceZ.value = 2.5
        controller.form.stockAllowanceXY.value = 1.25
        controller.form.stockAllowanceZ.value = 0.5
        controller._update_allowance_properties_from_form(op)

        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 1.0, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 2.5, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 1.25, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.5, places=6)
        self.assertFalse(controller.form.featureAllowanceLinkedFrame.visible)
        self.assertTrue(controller.form.featureAllowanceIndependentFrame.visible)
        self.assertFalse(controller.form.stockAllowanceLinkedFrame.visible)
        self.assertTrue(controller.form.stockAllowanceIndependentFrame.visible)

    def test_allowance_gui_controller_switching_to_linked_synchronizes_to_safe_value(self):
        _job, _model, op = self._create_operation(name="allowance_gui_mode_switch")
        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 2.5)
        self._set_allowance_distance(op, "StockAllowanceXY", 1.25)
        self._set_allowance_distance(op, "StockAllowanceZ", 0.5)

        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            featureAllowanceMode=_FakeComboBox("Linked"),
            stockAllowanceMode=_FakeComboBox("Linked"),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(9.0),
            featureAllowanceXY=_FakeQuantityWidget(1.0),
            featureAllowanceZ=_FakeQuantityWidget(2.5),
            stockAllowanceLinked=_FakeQuantityWidget(8.0),
            stockAllowanceXY=_FakeQuantityWidget(1.25),
            stockAllowanceZ=_FakeQuantityWidget(0.5),
            stockEdgeClearanceX=_FakeQuantityWidget(0.0),
            stockEdgeClearanceY=_FakeQuantityWidget(0.0),
        )
        controller.initPage(op)

        controller._update_allowance_properties_from_form(op)
        self.assertEqual(op.FeatureAllowanceMode, "Linked")
        self.assertEqual(op.StockAllowanceMode, "Linked")
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 2.5, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 2.5, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 1.25, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 1.25, places=6)

        controller.form.featureAllowanceLinked.value = 3.5
        controller.form.stockAllowanceLinked.value = 0.75
        controller._update_allowance_properties_from_form(op)
        self.assertAlmostEqual(op.FeatureAllowanceXY.Value, 3.5, places=6)
        self.assertAlmostEqual(op.FeatureAllowanceZ.Value, 3.5, places=6)
        self.assertAlmostEqual(op.StockAllowanceXY.Value, 0.75, places=6)
        self.assertAlmostEqual(op.StockAllowanceZ.Value, 0.75, places=6)

    def test_stock_edge_clearance_widgets_follow_clear_edges_toggle(self):
        _job, _model, op = self._create_operation(name="edge_clearance_widget_toggle")
        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            featureAllowanceMode=_FakeComboBox("Linked"),
            stockAllowanceMode=_FakeComboBox("Linked"),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(0.0),
            featureAllowanceXY=_FakeQuantityWidget(0.0),
            featureAllowanceZ=_FakeQuantityWidget(0.0),
            stockAllowanceLinked=_FakeQuantityWidget(0.0),
            stockAllowanceXY=_FakeQuantityWidget(0.0),
            stockAllowanceZ=_FakeQuantityWidget(0.0),
            stockEdgeClearanceX=_FakeQuantityWidget(0.0),
            stockEdgeClearanceY=_FakeQuantityWidget(0.0),
        )
        controller.initPage(op)

        op.ClearEdges = False
        controller._sync_stock_edge_clearance_widgets(op)
        self.assertFalse(controller.form.stockEdgeClearanceX.enabled)
        self.assertFalse(controller.form.stockEdgeClearanceY.enabled)

        op.ClearEdges = True
        controller._sync_stock_edge_clearance_widgets(op)
        self.assertTrue(controller.form.stockEdgeClearanceX.enabled)
        self.assertTrue(controller.form.stockEdgeClearanceY.enabled)

    def test_same_value_stock_edge_clearance_edit_clears_only_edited_axis_expression(self):
        _job, _model, op = self._create_operation(name="edge_clearance_same_value_edit")
        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            featureAllowanceMode=_FakeComboBox("Linked"),
            stockAllowanceMode=_FakeComboBox("Linked"),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(0.0),
            featureAllowanceXY=_FakeQuantityWidget(0.0),
            featureAllowanceZ=_FakeQuantityWidget(0.0),
            stockAllowanceLinked=_FakeQuantityWidget(0.0),
            stockAllowanceXY=_FakeQuantityWidget(0.0),
            stockAllowanceZ=_FakeQuantityWidget(0.0),
            stockEdgeClearanceX=_FakeQuantityWidget(op.StockEdgeClearanceX.Value),
            stockEdgeClearanceY=_FakeQuantityWidget(op.StockEdgeClearanceY.Value),
        )
        controller.initPage(op)

        self.assertIsNotNone(self._expression_for_property(op, "StockEdgeClearanceX"))
        self.assertIsNotNone(self._expression_for_property(op, "StockEdgeClearanceY"))

        original_x = op.StockEdgeClearanceX.Value
        original_y = op.StockEdgeClearanceY.Value
        controller._mark_stock_edge_clearance_property_edited("StockEdgeClearanceX")
        controller._update_stock_edge_clearance_properties_from_form(op)

        self.assertIsNone(self._expression_for_property(op, "StockEdgeClearanceX"))
        self.assertIsNotNone(self._expression_for_property(op, "StockEdgeClearanceY"))
        self.assertAlmostEqual(op.StockEdgeClearanceX.Value, original_x, places=6)
        self.assertAlmostEqual(op.StockEdgeClearanceY.Value, original_y, places=6)

    def test_stock_edge_clearance_gui_helper_does_not_call_proxy_change_hook_directly(self):
        class _RaisingProxy:
            def areaOpOnChanged(self, _obj, _prop_name):
                raise AssertionError("GUI must not call the App proxy change hook directly")

        class _FakeDistanceProperty:
            def __init__(self, value):
                self.Value = float(value)

        class _FakeStockEdgeOperation:
            def __init__(self):
                self.StockEdgeClearanceX = _FakeDistanceProperty(5.0)
                self.StockEdgeClearanceY = _FakeDistanceProperty(6.0)
                self.ExpressionEngine = [
                    ("StockEdgeClearanceX", "OpToolDiameter / 2 + 0.1 mm"),
                    ("StockEdgeClearanceY", "OpToolDiameter / 2 + 0.1 mm"),
                ]
                self.Proxy = _RaisingProxy()

            def setExpression(self, prop_name, expression):
                self.ExpressionEngine = [
                    (current_prop, current_expression)
                    for current_prop, current_expression in self.ExpressionEngine
                    if current_prop != prop_name
                ]
                if expression is not None:
                    self.ExpressionEngine.append((prop_name, expression))

        op = _FakeStockEdgeOperation()
        gui_module = self._load_headless_volume_face_mill_gui_module()
        controller = gui_module.TaskPanelOpPage.__new__(gui_module.TaskPanelOpPage)
        controller.form = types.SimpleNamespace(
            featureAllowanceMode=_FakeComboBox("Linked"),
            stockAllowanceMode=_FakeComboBox("Linked"),
            featureAllowanceLinkedFrame=_FakeFrame(),
            featureAllowanceIndependentFrame=_FakeFrame(),
            stockAllowanceLinkedFrame=_FakeFrame(),
            stockAllowanceIndependentFrame=_FakeFrame(),
            featureAllowanceLinked=_FakeQuantityWidget(0.0),
            featureAllowanceXY=_FakeQuantityWidget(0.0),
            featureAllowanceZ=_FakeQuantityWidget(0.0),
            stockAllowanceLinked=_FakeQuantityWidget(0.0),
            stockAllowanceXY=_FakeQuantityWidget(0.0),
            stockAllowanceZ=_FakeQuantityWidget(0.0),
            stockEdgeClearanceX=_FakeQuantityWidget(5.0),
            stockEdgeClearanceY=_FakeQuantityWidget(6.0),
        )
        controller.initPage(op)

        controller._mark_stock_edge_clearance_property_edited("StockEdgeClearanceX")
        controller._update_stock_edge_clearance_properties_from_form(op)

        self.assertIsNone(self._expression_for_property(op, "StockEdgeClearanceX"))
        self.assertIsNotNone(self._expression_for_property(op, "StockEdgeClearanceY"))
        self.assertAlmostEqual(op.StockEdgeClearanceX.Value, 5.0, places=6)
        self.assertAlmostEqual(op.StockEdgeClearanceY.Value, 6.0, places=6)

    def test_gui_update_data_does_not_refresh_while_applying_form_fields(self):
        module = self._load_headless_volume_face_mill_gui_module()
        page = module.TaskPanelOpPage.__new__(module.TaskPanelOpPage)
        page._applying_form_fields = True
        calls = []

        def fake_set_fields(_obj):
            calls.append("setFields")

        page.setFields = fake_set_fields
        page.updateData(object(), "FeatureAllowanceXY")

        self.assertEqual(calls, [])

    def test_selected_target_face_depth_honors_feature_allowance_z(self):
        job, model = self._make_job_with_stock_and_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="feature_allowance_target_depth",
            job=job,
            model=model,
            base=[(model, [top_face])],
            step_down=5.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceZ", 1.5)
        self.assertSuccessfulRecompute(self.doc, op)

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertEqual(len(target_faces), 1)
        self.assertAlmostEqual(final_depth, 11.5, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 11.5, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 11.5, places=6)
        self._assert_has_z_level(self._cutting_z_levels(self._cutting_moves(op.Path)), 11.5)

    def test_selected_target_face_depth_reaches_face_when_feature_allowance_z_is_zero(self):
        job, model = self._make_job_with_stock_and_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="feature_allowance_target_depth_zero_z",
            job=job,
            model=model,
            base=[(model, [top_face])],
            step_down=5.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 2.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 0.0)
        self.assertSuccessfulRecompute(self.doc, op)

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertEqual(len(target_faces), 1)
        self.assertAlmostEqual(final_depth, 10.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 10.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 10.0, places=6)
        self._assert_has_z_level(self._cutting_z_levels(self._cutting_moves(op.Path)), 10.0)

    def test_selected_fixture_horizontal_face_does_not_set_final_depth(self):
        job, model = self._make_job_with_stock_and_model()
        fixture = self._make_fixture_box(
            "Fixture",
            FreeCAD.Vector(23.5, 41.25, 0.0),
            FreeCAD.Vector(17.0, 13.0, 8.0),
        )
        fixture_top = self._highest_horizontal_face_name(fixture.Shape)

        _job, _model, op = self._create_operation(
            name="fixture_face_does_not_set_depth",
            job=job,
            model=model,
            base=[(fixture, [fixture_top])],
            step_down=4.0,
        )

        stock_bb = job.Stock.Shape.BoundBox
        self.assertAlmostEqual(op.OpFinalDepth.Value, stock_bb.ZMin, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, stock_bb.ZMin, places=6)

    def test_fixture_selected_face_becomes_keepout_when_protected(self):
        job, model = self._make_job_with_stock_and_model()
        fixture = self._make_fixture_box(
            "ProtectedFixture",
            FreeCAD.Vector(20, 20, 0),
            FreeCAD.Vector(20, 20, 15),
        )
        fixture_top = self._highest_horizontal_face_name(fixture.Shape)

        _job, _model, op = self._create_operation(
            name="fixture_keepout_true",
            job=job,
            model=model,
            base=[(fixture, [fixture_top])],
            protect_selected_features=True,
            step_down=5.0,
        )

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        overlap = removal.common(fixture.Shape)
        self.assertLessEqual(getattr(overlap, "Volume", 0.0), 1e-6)

    def test_fixture_selection_ignored_when_protect_selected_features_false(self):
        job, model = self._make_job_with_stock_and_model()
        fixture = self._make_fixture_box(
            "UnprotectedFixture",
            FreeCAD.Vector(20, 20, 0),
            FreeCAD.Vector(20, 20, 15),
        )
        fixture_top = self._highest_horizontal_face_name(fixture.Shape)

        _job, _model, op = self._create_operation(
            name="fixture_keepout_false",
            job=job,
            model=model,
            base=[(fixture, [fixture_top])],
            protect_selected_features=False,
            step_down=5.0,
        )

        stock_bb = job.Stock.Shape.BoundBox
        self.assertAlmostEqual(op.OpFinalDepth.Value, stock_bb.ZMin, places=6)

    def test_is_horizontal_face_uses_face_parameter_midpoint(self):
        shape = Part.makeBox(17.0, 13.0, 8.0, FreeCAD.Vector(23.5, 41.25, 0.0))
        horizontal_faces = [
            face for face in shape.Faces if abs(face.BoundBox.ZMax - face.BoundBox.ZMin) <= 1e-6
        ]

        self.assertGreaterEqual(len(horizontal_faces), 2)
        for face in horizontal_faces:
            self.assertTrue(PathVolumeFaceMillUtils.is_horizontal_face(face))

    def test_selected_vertical_geometry_is_detected_as_selection_without_target_face(self):
        job, model = self._make_job_with_stock_and_model()
        _job, _model, op = self._create_operation(
            name="vertical_selection_detected",
            job=job,
            model=model,
            base=[(model, self._vertical_face_names(model.Shape))],
        )

        self.assertTrue(PathVolumeFaceMillUtils.has_selected_geometry(op))
        self.assertEqual(PathVolumeFaceMillUtils.selected_horizontal_faces(op), [])
        self.assertAlmostEqual(op.OpFinalDepth.Value, job.Stock.Shape.BoundBox.ZMin, places=6)

    def test_stepover_is_clamped_for_programmatic_values(self):
        _job, _model, op = self._create_operation(name="programmatic_stepover_clamp")

        op.StepOver = -10
        op.Proxy.areaOpOnChanged(op, "StepOver")
        self.assertAlmostEqual(float(op.StepOver), 1.0, places=6)

        op.StepOver = 0
        op.Proxy.areaOpOnChanged(op, "StepOver")
        self.assertAlmostEqual(float(op.StepOver), 1.0, places=6)

        op.StepOver = 250
        op.Proxy.areaOpOnChanged(op, "StepOver")
        self.assertAlmostEqual(float(op.StepOver), 100.0, places=6)

    def test_failed_depth_candidate_does_not_leak_end_vector(self):
        _job, _model, op = self._create_operation(name="depth_candidate_endvector_guard")
        proxy = op.Proxy
        original_end_vector = FreeCAD.Vector(1.0, 2.0, 3.0)
        proxy.endVector = original_end_vector
        proxy.depthparams = []

        empty_path = Path.Path([])
        good_path = Path.Path(
            [
                Path.Command("G1", {"X": 1.0, "Y": 1.0, "Z": 4.0}),
            ]
        )

        def fake_build_path_area(_obj, _shape, _is_hole, _start, _getsim):
            if proxy.depthparams == [3.0]:
                proxy.endVector = FreeCAD.Vector(99.0, 99.0, 99.0)
                return empty_path, None

            proxy.endVector = FreeCAD.Vector(10.0, 20.0, 4.0)
            return good_path, None

        shape = Part.makeBox(10, 10, 10)
        with mock.patch.object(proxy, "_buildPathArea", side_effect=fake_build_path_area):
            depth, pp, sim, z_levels = proxy._build_path_for_depth_candidates(
                op,
                shape,
                False,
                None,
                False,
                [3.0, 4.0],
            )

        self.assertAlmostEqual(depth, 4.0, places=6)
        self.assertIs(pp, good_path)
        self.assertIsNone(sim)
        self.assertEqual(z_levels, [4.0])
        self.assertAlmostEqual(proxy.endVector.x, 10.0, places=6)
        self.assertAlmostEqual(proxy.endVector.y, 20.0, places=6)
        self.assertAlmostEqual(proxy.endVector.z, 4.0, places=6)

    def test_clear_edges_true_keeps_final_pass_when_stock_above_target_face_is_25mm(self):
        job, model = self._make_job_with_25mm_stock_above_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="clear_edges_true_keeps_final_pass",
            job=job,
            model=model,
            base=[(model, [top_face])],
            clear_edges=True,
            step_down=5.0,
        )

        cutting_moves = self._cutting_moves(op.Path)
        z_levels = self._cutting_z_levels(cutting_moves)
        z_order = self._cutting_z_order(cutting_moves)
        self._assert_z_levels_equal(z_levels, [10.0, 15.0, 20.0, 25.0, 30.0])
        self._assert_z_order_equal(z_order, [30.0, 25.0, 20.0, 15.0, 10.0])
        self.assertNotIn(35.0, z_levels)

    def test_selected_target_face_starts_cutting_one_step_below_stock_top(self):
        job, model = self._make_job_with_25mm_stock_above_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="selected_target_face_starts_one_step_below_stock_top",
            job=job,
            model=model,
            base=[(model, [top_face])],
            clear_edges=False,
            step_down=5.0,
        )

        cutting_moves = self._cutting_moves(op.Path)
        z_levels = self._cutting_z_levels(cutting_moves)
        z_order = self._cutting_z_order(cutting_moves)
        self._assert_z_levels_equal(z_levels, [10.0, 15.0, 20.0, 25.0, 30.0])
        self._assert_z_order_equal(z_order, [30.0, 25.0, 20.0, 15.0, 10.0])
        self.assertNotIn(35.0, z_levels)

    def test_linked_feature_allowance_keeps_short_final_remainder_pass(self):
        job, model = self._make_job_with_25mm_stock_above_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="linked_feature_allowance_keeps_remainder_pass",
            job=job,
            model=model,
            base=[(model, [top_face])],
            clear_edges=False,
            step_down=5.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertAlmostEqual(op.OpStartDepth.Value, 35.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 11.0, places=6)
        cutting_moves = self._cutting_moves(op.Path)
        z_levels = self._cutting_z_levels(cutting_moves)
        z_order = self._cutting_z_order(cutting_moves)
        self._assert_z_levels_equal(z_levels, [11.0, 15.0, 20.0, 25.0, 30.0])
        self._assert_z_order_equal(z_order, [30.0, 25.0, 20.0, 15.0, 11.0])
        self.assertNotIn(35.0, z_levels)

    def test_allowance_layer_generation_without_realizable_section_fails_safely(self):
        job, model = self._make_job_with_25mm_stock_above_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="allowance_layer_generation_fails_safely",
            job=job,
            model=model,
            base=[(model, [top_face])],
            clear_edges=False,
            step_down=5.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)

        with mock.patch.object(
            op.Proxy,
            "_build_path_for_depth_candidates",
            return_value=(None, None, None, []),
        ), mock.patch.object(
            PathVolumeFaceMill.Path.Log,
            "warning",
        ) as warning_mock:
            self.assertSuccessfulRecompute(self.doc, op)

        self.assertEqual(len(self._cutting_moves(op.Path)), 0)
        self.assertFalse(op.removalshape.isNull())
        warning_mock.assert_called()
        self.assertIn("Feature Allowance cutting sections", warning_mock.call_args[0][0])

    def test_allowance_keepout_construction_failure_aborts_safely(self):
        job, model = self._make_job_with_25mm_stock_above_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="allowance_keepout_failure_aborts_safely",
            job=job,
            model=model,
            base=[(model, [top_face])],
            clear_edges=False,
            step_down=5.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)

        warning_messages = []

        def capture_warning(message):
            warning_messages.append(str(message))
            return None

        with mock.patch.object(
            PathVolumeFaceMillUtils,
            "_protected_slab",
            return_value=Part.makeBox(10.0, 10.0, 2.0, FreeCAD.Vector(40.0, 40.0, 8.0)),
        ), mock.patch.object(
            PathVolumeFaceMillUtils,
            "_layer_keepout_footprint",
            return_value=None,
        ), mock.patch.object(
            PathVolumeFaceMillUtils,
            "_fallback_keepout_box_from_slab",
            return_value=None,
        ), mock.patch.object(
            Path.Log,
            "warning",
            side_effect=capture_warning,
        ):
            self.assertSuccessfulRecompute(self.doc, op)

        self.assertEqual(len(self._cutting_moves(op.Path)), 0)
        self.assertTrue(op.removalshape.isNull())
        self.assertTrue(
            any(
                "aborting feature-allowance generation to avoid under-protecting geometry."
                in message
                for message in warning_messages
            )
        )

    def test_standard_generation_without_realizable_depth_fails_safely(self):
        job, model = self._make_job_with_25mm_stock_above_model()
        top_face = self._highest_horizontal_face_name(model.Shape)
        _job, _model, op = self._create_operation(
            name="standard_generation_fails_safely",
            job=job,
            model=model,
            base=[(model, [top_face])],
            clear_edges=False,
            step_down=5.0,
        )

        with mock.patch.object(
            op.Proxy,
            "_effective_cut_depth",
            return_value=None,
        ), mock.patch.object(
            PathVolumeFaceMill.Path.Log,
            "warning",
        ) as warning_mock:
            op.touch()
            self.assertSuccessfulRecompute(self.doc, op)

        self.assertEqual(len(self._cutting_moves(op.Path)), 0)
        self.assertFalse(op.removalshape.isNull())
        warning_mock.assert_called()
        self.assertIn(
            "No realizable cutting sections found within the permitted depth range.",
            warning_mock.call_args[0][0],
        )

    def test_no_base_full_plate_without_clear_edges_keeps_final_pass_at_plate_top(self):
        model = self._make_full_plate_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(100, 100, 50),
            FreeCAD.Vector(0, 0, -25),
        )
        _job, _model, op = self._create_operation(
            name="no_base_full_plate_final_floor",
            job=job,
            model=model,
            base=[],
            clear_edges=False,
            step_down=5.0,
            tool_diameter=50.0,
        )

        z_levels = self._cutting_z_levels(self._cutting_moves(op.Path))
        self._assert_has_z_level(z_levels, 0.0)
        self._assert_z_levels_equal(z_levels, [0.0, 5.0, 10.0, 15.0, 20.0])

    def test_no_base_full_plate_feature_allowance_without_clear_edges_keeps_final_pass_above_plate_top(
        self,
    ):
        model = self._make_full_plate_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(100, 100, 50),
            FreeCAD.Vector(0, 0, -25),
        )
        _job, _model, op = self._create_operation(
            name="no_base_full_plate_feature_allowance_floor",
            job=job,
            model=model,
            base=[],
            clear_edges=False,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertAlmostEqual(op.OpFinalDepth.Value, -25.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, -25.0, places=6)
        z_levels = self._cutting_z_levels(self._cutting_moves(op.Path))
        self.assertGreater(len(z_levels), 0)
        self.assertGreaterEqual(min(z_levels), 1.0 - 1e-5)
        for expected in (5.0, 10.0, 15.0, 20.0):
            self._assert_has_z_level(z_levels, expected)

    def test_no_base_raised_feature_clear_edges_false_keeps_final_floor_pass(self):
        model = self._make_full_plate_with_raised_feature_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(200, 200, 50),
            FreeCAD.Vector(0, 0, -25),
        )
        _job, _model, op = self._create_operation(
            name="no_base_raised_feature_clear_edges_false_final_floor",
            job=job,
            model=model,
            base=[],
            clear_edges=False,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self.assertAlmostEqual(op.OpFinalDepth.Value, -25.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, -25.0, places=6)
        z_levels = self._cutting_z_levels(self._cutting_moves(op.Path))
        self._assert_has_z_level(z_levels, 0.0)
        self._assert_z_levels_equal(z_levels, [0.0, 5.0, 10.0, 15.0, 20.0])

    def test_no_base_raised_feature_clear_edges_true_keeps_final_floor_pass(self):
        model = self._make_full_plate_with_raised_feature_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(200, 200, 50),
            FreeCAD.Vector(0, 0, -25),
        )
        _job, _model, op = self._create_operation(
            name="no_base_raised_feature_clear_edges_true_final_floor",
            job=job,
            model=model,
            base=[],
            clear_edges=True,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self.assertAlmostEqual(op.OpFinalDepth.Value, -25.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, -25.0, places=6)
        z_levels = self._cutting_z_levels(self._cutting_moves(op.Path))
        self._assert_has_z_level(z_levels, 0.0)
        self._assert_z_levels_equal(z_levels, [0.0, 5.0, 10.0, 15.0, 20.0])

    def test_no_base_raised_feature_allowance_false_clear_edges_keeps_final_offset_floor(self):
        model = self._make_full_plate_with_raised_feature_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(200, 200, 50),
            FreeCAD.Vector(0, 0, -25),
        )
        _job, _model, op = self._create_operation(
            name="no_base_raised_feature_allowance_false_clear_edges",
            job=job,
            model=model,
            base=[],
            clear_edges=False,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Linked")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertAlmostEqual(op.OpFinalDepth.Value, -25.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, -25.0, places=6)
        z_levels = self._cutting_z_levels(self._cutting_moves(op.Path))
        self.assertGreater(len(z_levels), 0)
        self.assertGreaterEqual(min(z_levels), 1.0 - 1e-5)
        for expected in (5.0, 10.0, 15.0, 20.0):
            self._assert_has_z_level(z_levels, expected)

    def test_no_target_depth_honors_stock_allowance_z(self):
        job, model = self._make_job_with_stock_and_model()
        _job, _model, op = self._create_operation(
            name="stock_allowance_bottom_depth",
            job=job,
            model=model,
            base=[],
            step_down=5.0,
        )

        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "StockAllowanceZ", 2.0)
        self.assertSuccessfulRecompute(self.doc, op)

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertEqual(len(target_faces), 0)
        self.assertAlmostEqual(final_depth, 2.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 2.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 2.0, places=6)
        self.assertAlmostEqual(op.removalshape.BoundBox.ZMin, 2.0, places=6)

    def test_stock_allowance_xy_shrinks_boundary(self):
        job, model, op = self._create_operation(
            name="stock_allowance_xy_boundary",
            step_down=20.0,
        )

        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "StockAllowanceXY", 5.0)
        self.assertSuccessfulRecompute(self.doc, op)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, 5.0, places=6)
        self.assertAlmostEqual(removal_bb.YMin, 5.0, places=6)
        self.assertAlmostEqual(removal_bb.XMax, 95.0, places=6)
        self.assertAlmostEqual(removal_bb.YMax, 95.0, places=6)

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)
        for x, y, _z in cutting_points:
            self.assertGreaterEqual(x, 5.0 - 1e-6)
            self.assertGreaterEqual(y, 5.0 - 1e-6)
            self.assertLessEqual(x, 95.0 + 1e-6)
            self.assertLessEqual(y, 95.0 + 1e-6)

    def test_excessive_stock_allowance_xy_fails_safely_with_no_path(self):
        job, model, op = self._create_operation(
            name="stock_allowance_xy_consumed",
            step_down=20.0,
        )

        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "StockAllowanceXY", 50.0)
        self.assertSuccessfulRecompute(self.doc, op)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        self.assertIsNone(removal)
        self.assertTrue(op.removalshape.isNull())
        self.assertEqual(len(self._cutting_moves(op.Path)), 0)

    def test_excessive_vertical_allowance_fails_safely_with_no_path(self):
        job, model, op = self._create_operation(
            name="stock_allowance_z_consumed",
            step_down=20.0,
        )

        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "StockAllowanceZ", 20.0)
        self.assertSuccessfulRecompute(self.doc, op)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        self.assertAlmostEqual(op.OpFinalDepth.Value, 20.0, places=6)
        self.assertIsNone(removal)
        self.assertTrue(op.removalshape.isNull())
        self.assertEqual(len(self._cutting_moves(op.Path)), 0)

    def test_clear_edges_with_stock_allowance_stays_inside_shrunken_boundary(self):
        _job, _model, op = self._create_operation(
            name="clear_edges_with_stock_allowance",
            clear_edges=True,
            step_down=20.0,
        )

        self._set_allowance_mode(op, "StockAllowanceMode", "Independent")
        self._set_allowance_distance(op, "StockAllowanceXY", 5.0)
        self.assertSuccessfulRecompute(self.doc, op)

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)
        for x, y, _z in cutting_points:
            self.assertGreaterEqual(x, 5.0 - 1e-6)
            self.assertGreaterEqual(y, 5.0 - 1e-6)
            self.assertLessEqual(x, 95.0 + 1e-6)
            self.assertLessEqual(y, 95.0 + 1e-6)

    def test_feature_allowance_on_full_width_target_face_generates_path(self):
        """Feature allowance must not turn the selected target floor into a keepout."""

        model = self._make_full_plate_model(
            size=FreeCAD.Vector(100, 100, 10),
            base=FreeCAD.Vector(0, 0, -10),
        )
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(100, 100, 20),
            FreeCAD.Vector(0, 0, -10),
        )

        top_face = self._highest_horizontal_face_name(model.Shape)

        _job, _model, op = self._create_operation(
            name="feature_allowance_full_width_target_generates_path",
            job=job,
            model=model,
            base=[(model, [top_face])],
            step_down=20.0,
            tool_diameter=10.0,
        )

        baseline_moves = self._cutting_moves(op.Path)
        self.assertGreater(len(baseline_moves), 0)

        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self.assertSuccessfulRecompute(self.doc, op)

        allowance_moves = self._cutting_moves(op.Path)
        self.assertGreater(
            len(allowance_moves),
            0,
            "FeatureAllowanceXY in linked mode should not suppress all toolpath.",
        )

        z_levels = self._cutting_z_levels(allowance_moves)
        self._assert_has_z_level(z_levels, 1.0)

    def test_feature_allowance_xy_only_generates_path(self):
        """XY-only feature allowance should not make the target face a keepout."""

        model = self._make_full_plate_model(
            size=FreeCAD.Vector(100, 100, 10),
            base=FreeCAD.Vector(0, 0, -10),
        )
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(100, 100, 20),
            FreeCAD.Vector(0, 0, -10),
        )

        top_face = self._highest_horizontal_face_name(model.Shape)

        _job, _model, op = self._create_operation(
            name="feature_allowance_xy_only_generates_path",
            job=job,
            model=model,
            base=[(model, [top_face])],
            step_down=20.0,
            tool_diameter=10.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 1.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 0.0)
        self.assertSuccessfulRecompute(self.doc, op)

        allowance_moves = self._cutting_moves(op.Path)
        self.assertGreater(
            len(allowance_moves),
            0,
            "XY-only FeatureAllowance should not suppress all toolpath.",
        )

        z_levels = self._cutting_z_levels(allowance_moves)
        self._assert_has_z_level(z_levels, 0.0)

    def test_feature_allowance_protects_raised_feature_and_still_generates_path(self):
        """Feature allowance should preserve a raised island while machining surrounding stock."""

        model = self._make_full_plate_with_raised_feature_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(200, 200, 75),
            FreeCAD.Vector(0, 0, -25),
        )

        # The helper creates a plate at Z=-25..0 and a raised feature at Z=0..45.
        # Select the plate top face at Z=0 as the target depth, not the raised top.
        target_face = self._horizontal_face_name_near_z(model.Shape, 0.0)

        _job, _model, op = self._create_operation(
            name="feature_allowance_raised_feature_protected",
            job=job,
            model=model,
            base=[(model, [target_face])],
            step_down=10.0,
            tool_diameter=10.0,
        )

        self._set_allowance_distance(op, "FeatureAllowanceXY", 2.0)
        self.assertSuccessfulRecompute(self.doc, op)

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)

        # Raised feature footprint is X/Y 70..130. Linked allowance adds 2 mm.
        for x, y, z in cutting_points:
            if z <= 45.0 + 2.0 + 1e-6:
                self.assertFalse(
                    self._xy_inside_rect(x, y, 68.0, 132.0, 68.0, 132.0),
                    f"Cutting move enters raised feature allowance keepout at ({x}, {y}, {z})",
                )

    def test_feature_allowance_with_coarse_stepdown_still_handles_raised_feature(self):
        """Coarse StepDown should not make allowance protection consume all layers."""

        model = self._make_full_plate_with_raised_feature_model()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(200, 200, 75),
            FreeCAD.Vector(0, 0, -25),
        )

        target_face = self._horizontal_face_name_near_z(model.Shape, 0.0)

        _job, _model, op = self._create_operation(
            name="feature_allowance_coarse_stepdown_raised_feature",
            job=job,
            model=model,
            base=[(model, [target_face])],
            step_down=100.0,
            tool_diameter=10.0,
        )

        self._set_allowance_distance(op, "FeatureAllowanceXY", 2.0)
        self.assertSuccessfulRecompute(self.doc, op)

        cutting_moves = self._cutting_moves(op.Path)
        self.assertGreater(len(cutting_moves), 0)

        z_levels = self._cutting_z_levels(cutting_moves)
        self._assert_has_z_level(z_levels, 47.0)
        self._assert_has_z_level(z_levels, 2.0)

    def test_feature_allowance_z_starts_keepout_above_raised_model_feature(self):
        model = self._make_model_with_boss()
        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(100, 100, 11),
            FreeCAD.Vector(0, 0, 0),
        )

        _job, _model, op_zero = self._create_operation(
            name="feature_allowance_z_zero",
            job=job,
            model=model,
            step_down=0.5,
        )
        _job, _model, op_allow = self._create_operation(
            name="feature_allowance_z_active",
            job=job,
            model=model,
            step_down=0.5,
        )

        self._set_allowance_mode(op_allow, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op_allow, "FeatureAllowanceZ", 0.5)
        self.assertSuccessfulRecompute(self.doc, op_zero, op_allow)

        protected_probe = self._center_probe(10.25, size=1.0, height=0.2)
        clear_probe = self._center_probe(10.85, size=1.0, height=0.1)

        self.assertGreater(
            getattr(op_zero.removalshape.common(protected_probe), "Volume", 0.0), 1e-6
        )
        self.assertLessEqual(
            getattr(op_allow.removalshape.common(protected_probe), "Volume", 0.0), 1e-6
        )
        self.assertGreater(getattr(op_allow.removalshape.common(clear_probe), "Volume", 0.0), 1e-6)

    def test_feature_xy_allowance_with_zigzag_offset_generates_layer_paths(self):
        """Regression test for valid allowance layers skipped as having no cutting moves."""

        model = self._make_full_plate_model(
            size=FreeCAD.Vector(550, 300, 25),
            base=FreeCAD.Vector(0, 0, -25),
        )

        job = self._make_job_with_custom_stock_and_model(
            model,
            FreeCAD.Vector(550, 300, 50),
            FreeCAD.Vector(0, 0, -25),
        )

        target_face = self._highest_horizontal_face_name(model.Shape)

        _job, _model, op = self._create_operation(
            name="feature_xy_allowance_zigzag_offset",
            job=job,
            model=model,
            base=[(model, [target_face])],
            step_down=5.0,
            clear_edges=True,
            clearing_pattern="ZigZagOffset",
            tool_diameter=63.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 2.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 0.0)

        self.assertSuccessfulRecompute(self.doc, op)

        cutting_moves = self._cutting_moves(op.Path)
        self.assertGreater(
            len(cutting_moves),
            0,
            "FeatureAllowanceXY with ZigZagOffset must generate cutting moves.",
        )

        z_levels = self._cutting_z_levels(cutting_moves)

        for expected_z in (0.0, 5.0, 10.0, 15.0, 20.0):
            self._assert_has_z_level(z_levels, expected_z)

    def test_feature_allowance_xy_expands_drafted_model_keepout_laterally(self):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 46))

        _job, _model, op_zero = self._create_operation(
            name="feature_allowance_xy_zero",
            job=job,
            model=model,
            step_down=5.0,
            tool_diameter=50.0,
        )
        _job, _model, op_allow = self._create_operation(
            name="feature_allowance_xy_active",
            job=job,
            model=model,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self._set_allowance_mode(op_allow, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op_allow, "FeatureAllowanceXY", 2.0)
        self.assertSuccessfulRecompute(self.doc, op_zero, op_allow)

        lateral_probe = self._probe_box(25.8, 49.5, 40.6, xlen=0.8, ylen=1.0, zlen=0.6)
        self.assertGreater(getattr(op_zero.removalshape.common(lateral_probe), "Volume", 0.0), 1e-6)
        self.assertFalse(op_allow.removalshape.isNull())
        self.assertLessEqual(
            getattr(op_allow.removalshape.common(lateral_probe), "Volume", 0.0), 1e-6
        )

    def test_feature_allowance_xy_expands_selected_keepout_laterally(self):
        job, model = self._make_job_with_stock_and_model()
        aux = self._make_aux_box(
            "AuxAllowanceKeepout", FreeCAD.Vector(10, 10, 10), FreeCAD.Vector(15, 15, 8)
        )
        target_face = self._highest_horizontal_face_name(model.Shape)

        _job, _model, op_zero = self._create_operation(
            name="selected_feature_allowance_zero",
            job=job,
            model=model,
            base=[(model, [target_face]), (aux, self._vertical_face_names(aux.Shape))],
            protect_selected_features=True,
            step_down=5.0,
        )
        _job, _model, op_allow = self._create_operation(
            name="selected_feature_allowance_active",
            job=job,
            model=model,
            base=[(model, [target_face]), (aux, self._vertical_face_names(aux.Shape))],
            protect_selected_features=True,
            step_down=5.0,
        )

        self._set_allowance_mode(op_allow, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op_allow, "FeatureAllowanceXY", 3.0)
        self.assertSuccessfulRecompute(self.doc, op_zero, op_allow)

        keepout_probe = self._probe_box(26.4, 16.0, 14.6, xlen=0.6, ylen=1.0, zlen=0.6)
        self.assertGreater(getattr(op_zero.removalshape.common(keepout_probe), "Volume", 0.0), 1e-6)
        self.assertLessEqual(
            getattr(op_allow.removalshape.common(keepout_probe), "Volume", 0.0), 1e-6
        )

    def test_selected_target_face_with_same_base_drafted_keepouts_reaches_zero_z_allowance_depth(
        self,
    ):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 46))
        target_face = self._highest_horizontal_face_name(model.Shape)
        side_faces = self._vertical_face_names(model.Shape)

        _job, _model, op = self._create_operation(
            name="same_base_target_with_drafted_keepouts",
            job=job,
            model=model,
            base=[(model, [target_face] + side_faces)],
            protect_selected_features=True,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self._set_allowance_mode(op, "FeatureAllowanceMode", "Independent")
        self._set_allowance_distance(op, "FeatureAllowanceXY", 2.0)
        self._set_allowance_distance(op, "FeatureAllowanceZ", 0.0)
        self.assertSuccessfulRecompute(self.doc, op)

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertEqual(len(target_faces), 1)
        self.assertAlmostEqual(final_depth, 45.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 45.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 45.0, places=6)
        self._assert_has_z_level(self._cutting_z_levels(self._cutting_moves(op.Path)), 45.0)

    def test_selected_target_face_with_same_base_drafted_keepouts_reaches_depth_without_active_allowance(
        self,
    ):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 46))
        target_face = self._highest_horizontal_face_name(model.Shape)
        side_faces = self._vertical_face_names(model.Shape)

        _job, _model, op = self._create_operation(
            name="same_base_target_without_active_allowance",
            job=job,
            model=model,
            base=[(model, [target_face] + side_faces)],
            protect_selected_features=True,
            step_down=5.0,
            tool_diameter=50.0,
        )

        self.assertSuccessfulRecompute(self.doc, op)

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertEqual(len(target_faces), 1)
        self.assertAlmostEqual(final_depth, 45.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 45.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 45.0, places=6)
        self._assert_has_z_level(self._cutting_z_levels(self._cutting_moves(op.Path)), 45.0)

    def test_selected_target_face_with_same_base_drafted_keepouts_reaches_depth_with_compatibility_pattern(
        self,
    ):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 46))
        target_face = self._highest_horizontal_face_name(model.Shape)
        side_faces = self._vertical_face_names(model.Shape)

        _job, _model, op = self._create_operation(
            name="same_base_target_offset_pattern",
            job=job,
            model=model,
            base=[(model, [target_face] + side_faces)],
            protect_selected_features=True,
            clear_edges=True,
            step_down=5.0,
            tool_diameter=50.0,
        )
        op.ClearingPattern = "ZigZagOffset"
        op.OptimizationMode = "MinTravel"
        op.StepOver = 70
        op.Angle = 0
        self.assertSuccessfulRecompute(self.doc, op)

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )

        self.assertEqual(len(target_faces), 1)
        self.assertAlmostEqual(final_depth, 45.0, places=6)
        self.assertAlmostEqual(op.OpFinalDepth.Value, 45.0, places=6)
        self.assertAlmostEqual(op.FinalDepth.Value, 45.0, places=6)
        self._assert_has_z_level(self._cutting_z_levels(self._cutting_moves(op.Path)), 45.0)

    def test_higher_selected_faces_become_keepouts_when_enabled(self):
        job, model = self._make_job_with_stock_and_model()
        aux = self._make_aux_box(
            "AuxKeepout", FreeCAD.Vector(10, 10, 10), FreeCAD.Vector(15, 15, 8)
        )
        target_face = self._highest_horizontal_face_name(model.Shape)
        aux_top = self._highest_horizontal_face_name(aux.Shape)

        _job, _model, op = self._create_operation(
            name="higher_faces_keepout_enabled",
            job=job,
            model=model,
            base=[(model, [target_face]), (aux, [aux_top])],
            protect_selected_features=True,
            step_down=5.0,
        )

        self.assertAlmostEqual(op.OpFinalDepth.Value, 10.0, places=6)
        probe = self._probe_box(14.0, 14.0, 14.0)
        self.assertLessEqual(getattr(op.removalshape.common(probe), "Volume", 0.0), 1e-6)

    def test_higher_selected_faces_do_not_become_extra_keepouts_when_disabled(self):
        job, model = self._make_job_with_stock_and_model()
        aux = self._make_aux_box(
            "AuxNoKeepout", FreeCAD.Vector(10, 10, 10), FreeCAD.Vector(15, 15, 8)
        )
        target_face = self._highest_horizontal_face_name(model.Shape)
        aux_top = self._highest_horizontal_face_name(aux.Shape)

        _job, _model, op = self._create_operation(
            name="higher_faces_keepout_disabled",
            job=job,
            model=model,
            base=[(model, [target_face]), (aux, [aux_top])],
            protect_selected_features=False,
            step_down=5.0,
        )

        self.assertAlmostEqual(op.OpFinalDepth.Value, 10.0, places=6)
        probe = self._probe_box(14.0, 14.0, 14.0)
        self.assertGreater(getattr(op.removalshape.common(probe), "Volume", 0.0), 1e-6)

    def test_selected_drafted_faces_follow_actual_feature_taper(self):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 46))

        _job, _model, op = self._create_operation(
            name="selected_drafted_taper",
            job=job,
            model=model,
            base=[(model, self._vertical_face_names(model.Shape))],
            protect_selected_features=True,
            step_down=5.0,
            tool_diameter=50.0,
        )

        stock_shape = PathVolumeFaceMillUtils.get_stock_shape(op)
        target_faces, _final_depth = PathVolumeFaceMillUtils.resolve_target_faces_and_final_depth(
            op, stock_shape
        )
        protected = PathVolumeFaceMillUtils.build_protected_shape(
            op,
            job.Model.Group,
            target_faces,
            type("DepthParams", (), {"safe_height": 46.0, "final_depth": 0.0})(),
        )

        outside_taper_near_top = Part.makeBox(1.0, 1.0, 1.0, FreeCAD.Vector(22.5, 49.5, 39.5))
        inside_taper_near_top = Part.makeBox(1.0, 1.0, 1.0, FreeCAD.Vector(29.5, 49.5, 39.5))

        self.assertIsNotNone(protected)
        self.assertGreater(getattr(protected.common(inside_taper_near_top), "Volume", 0.0), 1e-6)
        self.assertLessEqual(
            getattr(protected.common(outside_taper_near_top), "Volume", 0.0),
            1e-6,
        )
        self.assertGreater(
            getattr(op.removalshape.common(outside_taper_near_top), "Volume", 0.0),
            1e-6,
        )

    def test_selected_model_profiles_fall_back_to_whole_base_shape_when_precise_keepout_fails(self):
        model = self._make_drafted_model()
        job = self._make_job_with_custom_stock_and_model(model, FreeCAD.Vector(100, 100, 46))
        target_face = self._highest_horizontal_face_name(model.Shape)
        side_faces = self._vertical_face_names(model.Shape)

        _job, _model, op = self._create_operation(
            name="selected_model_profile_whole_base_fallback",
            job=job,
            model=model,
            base=[(model, [target_face] + side_faces)],
            protect_selected_features=True,
            step_down=5.0,
            tool_diameter=50.0,
        )

        warning_messages = []

        def capture_warning(message):
            warning_messages.append(str(message))
            return None

        with mock.patch.object(
            PathVolumeFaceMillUtils,
            "_build_selected_feature_volume",
            return_value=None,
        ), mock.patch.object(
            PathVolumeFaceMillUtils.PathUtils,
            "getEnvelope",
            side_effect=AssertionError(
                "Per-profile keepout envelopes should not be used when whole-base fallback exists"
            ),
        ), mock.patch.object(
            Path.Log,
            "warning",
            side_effect=capture_warning,
        ):
            op.touch()
            self.assertSuccessfulRecompute(self.doc, op)

        self.assertGreater(len(self._cutting_moves(op.Path)), 0)
        self.assertFalse(op.removalshape.isNull())
        self.assertTrue(
            any(
                "using the whole selected base shape to avoid under-protecting geometry." in message
                for message in warning_messages
            )
        )

    def test_selected_non_volumetric_keepout_failure_aborts_safely(self):
        job, model = self._make_job_with_stock_and_model()
        target_face = self._highest_horizontal_face_name(model.Shape)
        surface = self.doc.addObject("Part::Feature", "AuxSurfaceKeepout")
        surface.Shape = Part.makePlane(20.0, 20.0, FreeCAD.Vector(10.0, 10.0, 10.0))
        self.assertSuccessfulRecompute(self.doc)

        _job, _model, op = self._create_operation(
            name="selected_non_volumetric_keepout_failure",
            job=job,
            model=model,
            base=[(model, [target_face]), (surface, ["Face1"])],
            protect_selected_features=True,
            step_down=5.0,
        )

        warning_messages = []

        def capture_warning(message):
            warning_messages.append(str(message))
            return None

        with mock.patch.object(
            PathVolumeFaceMillUtils,
            "_build_selected_feature_volume",
            return_value=None,
        ), mock.patch.object(
            PathVolumeFaceMillUtils.PathUtils,
            "getEnvelope",
            side_effect=RuntimeError("surface keepout envelope failure"),
        ), mock.patch.object(
            Path.Log,
            "warning",
            side_effect=capture_warning,
        ):
            op.touch()
            self.assertSuccessfulRecompute(self.doc, op)

        self.assertEqual(len(self._cutting_moves(op.Path)), 0)
        self.assertTrue(op.removalshape.isNull())
        self.assertTrue(
            any(
                "aborting selected-geometry protection to avoid under-protecting geometry"
                in message
                for message in warning_messages
            )
        )

    def test_step_down_generates_expected_z_levels(self):
        job, model = self._make_job_with_stock_and_model()
        _job, _model, op = self._create_operation(
            name="multiple_z_levels",
            job=job,
            model=model,
            step_down=5.0,
        )

        z_levels = self._cutting_z_levels(self._cutting_moves(op.Path))
        self.assertGreaterEqual(len(z_levels), 3)
        self.assertGreaterEqual(min(z_levels), op.FinalDepth.Value - 1e-6)
        self.assertLessEqual(max(z_levels), op.StartDepth.Value + 1e-6)
        self.assertLess(max(z_levels), op.StartDepth.Value - 1e-6)
        self._assert_has_z_level(z_levels, op.StartDepth.Value - op.StepDown.Value)

    def test_clear_edges_false_ignores_edge_clearance_values(self):
        job, _model, op = self._create_operation(name="edge_clearance_disabled")
        op.ClearEdges = False
        self._set_allowance_distance(op, "StockEdgeClearanceX", 25.0)
        self._set_allowance_distance(op, "StockEdgeClearanceY", 30.0)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        stock_bb = job.Stock.Shape.BoundBox
        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, stock_bb.XMin, places=6)
        self.assertAlmostEqual(removal_bb.XMax, stock_bb.XMax, places=6)
        self.assertAlmostEqual(removal_bb.YMin, stock_bb.YMin, places=6)
        self.assertAlmostEqual(removal_bb.YMax, stock_bb.YMax, places=6)

    def test_clear_edges_x_clearance_expands_x_only(self):
        job, _model, op = self._create_operation(name="edge_clearance_x_only")
        op.ClearEdges = True
        self._set_allowance_distance(op, "StockEdgeClearanceX", 7.0)
        self._set_allowance_distance(op, "StockEdgeClearanceY", 0.0)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        stock_bb = job.Stock.Shape.BoundBox
        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, stock_bb.XMin - 7.0, places=6)
        self.assertAlmostEqual(removal_bb.XMax, stock_bb.XMax + 7.0, places=6)
        self.assertAlmostEqual(removal_bb.YMin, stock_bb.YMin, places=6)
        self.assertAlmostEqual(removal_bb.YMax, stock_bb.YMax, places=6)

    def test_clear_edges_y_clearance_expands_y_only(self):
        job, _model, op = self._create_operation(name="edge_clearance_y_only")
        op.ClearEdges = True
        self._set_allowance_distance(op, "StockEdgeClearanceX", 0.0)
        self._set_allowance_distance(op, "StockEdgeClearanceY", 9.0)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        stock_bb = job.Stock.Shape.BoundBox
        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, stock_bb.XMin, places=6)
        self.assertAlmostEqual(removal_bb.XMax, stock_bb.XMax, places=6)
        self.assertAlmostEqual(removal_bb.YMin, stock_bb.YMin - 9.0, places=6)
        self.assertAlmostEqual(removal_bb.YMax, stock_bb.YMax + 9.0, places=6)

    def test_clear_edges_xy_clearances_can_differ(self):
        job, _model, op = self._create_operation(name="edge_clearance_xy_different")
        op.ClearEdges = True
        self._set_allowance_distance(op, "StockEdgeClearanceX", 4.0)
        self._set_allowance_distance(op, "StockEdgeClearanceY", 11.0)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        stock_bb = job.Stock.Shape.BoundBox
        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, stock_bb.XMin - 4.0, places=6)
        self.assertAlmostEqual(removal_bb.XMax, stock_bb.XMax + 4.0, places=6)
        self.assertAlmostEqual(removal_bb.YMin, stock_bb.YMin - 11.0, places=6)
        self.assertAlmostEqual(removal_bb.YMax, stock_bb.YMax + 11.0, places=6)

    def test_stock_allowance_disables_stock_edge_clearance_expansion(self):
        job, _model, op = self._create_operation(name="stock_allowance_blocks_edge_clearance")
        op.ClearEdges = True
        self._set_allowance_distance(op, "StockEdgeClearanceX", 10.0)
        self._set_allowance_distance(op, "StockEdgeClearanceY", 10.0)
        self._set_allowance_distance(op, "StockAllowanceXY", 2.0)

        removal = PathVolumeFaceMillUtils.build_removal_volume(
            obj=op,
            model=job.Model.Group,
            tool_radius=self._tool_radius(op),
            depthparams=None,
        )

        stock_bb = job.Stock.Shape.BoundBox
        removal_bb = removal.BoundBox
        self.assertAlmostEqual(removal_bb.XMin, stock_bb.XMin + 2.0, places=6)
        self.assertAlmostEqual(removal_bb.XMax, stock_bb.XMax - 2.0, places=6)
        self.assertAlmostEqual(removal_bb.YMin, stock_bb.YMin + 2.0, places=6)
        self.assertAlmostEqual(removal_bb.YMax, stock_bb.YMax - 2.0, places=6)

    def test_clear_edges_false_keeps_tool_inside_stock_extents(self):
        _job, _model, op = self._create_operation(
            name="strict_boundary",
            clear_edges=False,
            step_down=20.0,
        )

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)
        for x, y, _z in cutting_points:
            self.assertGreaterEqual(x, -1e-6)
            self.assertGreaterEqual(y, -1e-6)
            self.assertLessEqual(x, 100.0 + 1e-6)
            self.assertLessEqual(y, 100.0 + 1e-6)

    def test_clear_edges_true_allows_limited_stock_edge_overhang(self):
        _job, _model, op = self._create_operation(
            name="edge_overhang",
            clear_edges=True,
            step_down=20.0,
        )

        cutting_points = self._cutting_points(self._cutting_moves(op.Path))
        self.assertGreater(len(cutting_points), 0)

        overhang_limit = self._tool_radius(op) + 0.1 + 1e-3
        saw_overhang = False
        for x, y, _z in cutting_points:
            if x < 0.0 or x > 100.0 or y < 0.0 or y > 100.0:
                saw_overhang = True

            self.assertGreaterEqual(x, -overhang_limit)
            self.assertGreaterEqual(y, -overhang_limit)
            self.assertLessEqual(x, 100.0 + overhang_limit)
            self.assertLessEqual(y, 100.0 + overhang_limit)

        self.assertTrue(saw_overhang, "Expected ClearEdges=True to allow boundary overhang.")

    def test_removalshape_is_saved(self):
        _job, _model, op = self._create_operation(name="removalshape_saved", step_down=20.0)

        self.assertFalse(op.removalshape.isNull())
        self.assertGreater(getattr(op.removalshape, "Volume", 0.0), 1e-6)
        self.assertGreater(len(self._cutting_moves(op.Path)), 0)

    def test_hidden_compatibility_properties_are_forced(self):
        _job, _model, op = self._create_operation(name="compatibility_properties_forced")

        if hasattr(op, "ProtectModel"):
            op.ProtectModel = False
        op.Proxy.opOnDocumentRestored(op)
        self.assertSuccessfulRecompute(self.doc, op)

        self.assertIn("MinTravel", op.getEnumerationsOfProperty("OptimizationMode"))
        if hasattr(op, "BoundaryShape"):
            self.assertEqual(op.BoundaryShape, "Stock")
            editor_mode = op.getEditorMode("BoundaryShape")
            if isinstance(editor_mode, list):
                self.assertIn("Hidden", editor_mode)
            else:
                self.assertIn(editor_mode, (2, "Hidden"))
        if hasattr(op, "ProtectModel"):
            self.assertTrue(op.ProtectModel)
            editor_mode = op.getEditorMode("ProtectModel")
            if isinstance(editor_mode, list):
                self.assertIn("Hidden", editor_mode)
            else:
                self.assertIn(editor_mode, (2, "Hidden"))
        if hasattr(op, "ClearingPattern"):
            editor_mode = op.getEditorMode("ClearingPattern")
            if isinstance(editor_mode, list):
                self.assertIn("Hidden", editor_mode)
            else:
                self.assertIn(editor_mode, (2, "Hidden"))

    def test_min_travel_without_start_point_falls_back_safely(self):
        _job, _model, op = self._create_operation(
            name="min_travel_fallback",
            optimization_mode="MinTravel",
            step_down=5.0,
        )

        cutting_moves = self._cutting_moves(op.Path)
        self.assertGreater(len(cutting_moves), 0)
        self.assertNotIn("sort_mode", op.PathParams)

    def test_auto_shortest_not_exposed_until_implemented(self):
        raw_enums = PathVolumeFaceMill.ObjectVolumeFaceMill.propertyEnumerations(dataType="raw")
        optimization_values = [value for _label, value in raw_enums["OptimizationMode"]]
        self.assertNotIn("AutoShortest", optimization_values)
