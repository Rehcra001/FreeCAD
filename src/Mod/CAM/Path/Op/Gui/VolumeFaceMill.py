# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
import FreeCADGui
import Path
import Path.Base.Gui.Util as PathGuiUtil
import Path.Op.Gui.Base as PathOpGui
import Path.Op.Gui.PocketBase as PathPocketBaseGui
import Path.Op.PocketBase as PathPocketBase
import Path.Op.VolumeFaceMill as PathVolumeFaceMill

from PySide.QtCore import QT_TRANSLATE_NOOP

__title__ = "CAM Volume Face Mill Operation UI"
__author__ = "OpenAI Codex"
__url__ = "https://www.freecad.org"
__doc__ = "Volume Face Mill operation page controller and command implementation."

if False:
    Path.Log.setLevel(Path.Log.Level.DEBUG, Path.Log.thisModule())
    Path.Log.trackModule(Path.Log.thisModule())
else:
    Path.Log.setLevel(Path.Log.Level.INFO, Path.Log.thisModule())


class TaskPanelOpPage(PathPocketBaseGui.TaskPanelOpPage):
    """Task panel controller for Volume Face Mill."""

    class TaskPanelBaseGeometryPage(PathOpGui.TaskPanelBaseGeometryPage):
        """Base geometry page with explicit, opt-in selection for Volume Face Mill."""

        InitBase = False

    def taskPanelBaseGeometryPage(self, obj, features):
        """Return the Volume Face Mill base-geometry page controller."""

        return self.TaskPanelBaseGeometryPage(obj, features)

    def initPage(self, obj):
        """Initialize allowance widgets with QuantitySpinBox controllers."""

        self.featureAllowanceLinkedSpinBox = PathGuiUtil.QuantitySpinBox(
            self.form.featureAllowanceLinked,
            obj,
            "FeatureAllowanceXY",
        )
        self.featureAllowanceXYSpinBox = PathGuiUtil.QuantitySpinBox(
            self.form.featureAllowanceXY,
            obj,
            "FeatureAllowanceXY",
        )
        self.featureAllowanceZSpinBox = PathGuiUtil.QuantitySpinBox(
            self.form.featureAllowanceZ,
            obj,
            "FeatureAllowanceZ",
        )
        self.stockAllowanceLinkedSpinBox = PathGuiUtil.QuantitySpinBox(
            self.form.stockAllowanceLinked,
            obj,
            "StockAllowanceXY",
        )
        self.stockAllowanceXYSpinBox = PathGuiUtil.QuantitySpinBox(
            self.form.stockAllowanceXY,
            obj,
            "StockAllowanceXY",
        )
        self.stockAllowanceZSpinBox = PathGuiUtil.QuantitySpinBox(
            self.form.stockAllowanceZ,
            obj,
            "StockAllowanceZ",
        )
        self._applying_form_fields = False

    def getForm(self):
        Path.Log.track()

        form = FreeCADGui.PySideUic.loadUi(":/panels/PageOpVolumeFaceMillEdit.ui")
        combo_to_property_map = [
            ("cutMode", "CutMode"),
            ("clearingPattern", "ClearingPattern"),
            ("optimizationMode", "OptimizationMode"),
            ("featureAllowanceMode", "FeatureAllowanceMode"),
            ("stockAllowanceMode", "StockAllowanceMode"),
        ]

        enum_tups = PathPocketBase.ObjectPocket.pocketPropertyEnumerations(dataType="raw")
        enum_tups.update(
            PathVolumeFaceMill.ObjectVolumeFaceMill.propertyEnumerations(dataType="raw")
        )
        self.populateCombobox(form, enum_tups, combo_to_property_map)
        return form

    def pocketFeatures(self):
        """Return GUI feature flags for this operation."""

        return PathPocketBaseGui.FeatureFacing

    def _update_angle(self, obj, set_model=True):
        """Keep the angle field aligned with the active clearing pattern."""

        self.form.angle.setEnabled(obj.ClearingPattern != "Offset")
        if set_model:
            PathGuiUtil.updateInputField(obj, "Angle", self.form.angle)

    def _allowance_mode_value(self, combo_box):
        """Return the current allowance editing mode from a combobox."""

        return str(combo_box.currentData())

    def _sync_allowance_mode_widgets(self, obj):
        """Show the correct allowance editors for the current mode of each group."""

        feature_linked = obj.FeatureAllowanceMode == "Linked"
        stock_linked = obj.StockAllowanceMode == "Linked"
        self.form.featureAllowanceLinkedFrame.setVisible(feature_linked)
        self.form.featureAllowanceIndependentFrame.setVisible(not feature_linked)
        self.form.stockAllowanceLinkedFrame.setVisible(stock_linked)
        self.form.stockAllowanceIndependentFrame.setVisible(not stock_linked)

    def _update_allowance_widgets(self):
        """Refresh all allowance widgets from the bound properties."""

        self.featureAllowanceLinkedSpinBox.updateWidget()
        self.featureAllowanceXYSpinBox.updateWidget()
        self.featureAllowanceZSpinBox.updateWidget()
        self.stockAllowanceLinkedSpinBox.updateWidget()
        self.stockAllowanceXYSpinBox.updateWidget()
        self.stockAllowanceZSpinBox.updateWidget()

    def _update_allowance_properties_from_form(self, obj):
        """Write active allowance UI values back to the operation."""

        feature_mode = self._allowance_mode_value(self.form.featureAllowanceMode)
        feature_mode_changed = obj.FeatureAllowanceMode != feature_mode
        if obj.FeatureAllowanceMode != feature_mode:
            obj.FeatureAllowanceMode = feature_mode

        stock_mode = self._allowance_mode_value(self.form.stockAllowanceMode)
        stock_mode_changed = obj.StockAllowanceMode != stock_mode
        if obj.StockAllowanceMode != stock_mode:
            obj.StockAllowanceMode = stock_mode

        if feature_mode_changed or stock_mode_changed:
            self._sync_allowance_mode_widgets(obj)
            self._update_allowance_widgets()

        if not feature_mode_changed and feature_mode == "Linked":
            self.featureAllowanceLinkedSpinBox.updateProperty()
        elif not feature_mode_changed:
            self.featureAllowanceXYSpinBox.updateProperty()
            self.featureAllowanceZSpinBox.updateProperty()

        if not stock_mode_changed and stock_mode == "Linked":
            self.stockAllowanceLinkedSpinBox.updateProperty()
        elif not stock_mode_changed:
            self.stockAllowanceXYSpinBox.updateProperty()
            self.stockAllowanceZSpinBox.updateProperty()

        self._sync_allowance_mode_widgets(obj)

    def getFields(self, obj):
        """Set operation object values from the task panel."""

        self._applying_form_fields = True
        try:
            self.updateToolController(obj, self.form.toolController)
            self.updateCoolant(obj, self.form.coolantController)

            if obj.CutMode != str(self.form.cutMode.currentData()):
                obj.CutMode = str(self.form.cutMode.currentData())

            if obj.ClearingPattern != str(self.form.clearingPattern.currentData()):
                obj.ClearingPattern = str(self.form.clearingPattern.currentData())

            if obj.OptimizationMode != str(self.form.optimizationMode.currentData()):
                obj.OptimizationMode = str(self.form.optimizationMode.currentData())

            if obj.StepOver != self.form.stepOverPercent.value():
                obj.StepOver = self.form.stepOverPercent.value()

            PathGuiUtil.updateInputField(obj, "ExtraOffset", self.form.extraOffset)
            self._update_angle(obj)

            if obj.ProtectSelectedFeatures != self.form.protectSelectedFeatures.isChecked():
                obj.ProtectSelectedFeatures = self.form.protectSelectedFeatures.isChecked()

            if obj.ClearEdges != self.form.clearEdges.isChecked():
                obj.ClearEdges = self.form.clearEdges.isChecked()

            if obj.UseStartPoint != self.form.useStartPoint.isChecked():
                obj.UseStartPoint = self.form.useStartPoint.isChecked()

            self._update_allowance_properties_from_form(obj)
        finally:
            self._applying_form_fields = False

    def setFields(self, obj):
        """Set task panel values from the operation object."""

        self.setupToolController(obj, self.form.toolController)
        self.setupCoolant(obj, self.form.coolantController)

        self.selectInComboBox(obj.CutMode, self.form.cutMode)
        self.selectInComboBox(obj.ClearingPattern, self.form.clearingPattern)
        self.selectInComboBox(obj.OptimizationMode, self.form.optimizationMode)

        self.form.stepOverPercent.setValue(obj.StepOver)
        self.form.extraOffset.setText(
            FreeCAD.Units.Quantity(obj.ExtraOffset.Value, FreeCAD.Units.Length).UserString
        )
        self.form.angle.setText(FreeCAD.Units.Quantity(obj.Angle, FreeCAD.Units.Angle).UserString)
        self._update_angle(obj, False)

        self.form.protectSelectedFeatures.setChecked(bool(obj.ProtectSelectedFeatures))
        self.form.clearEdges.setChecked(bool(obj.ClearEdges))
        self.form.useStartPoint.setChecked(bool(obj.UseStartPoint))
        self.selectInComboBox(obj.FeatureAllowanceMode, self.form.featureAllowanceMode)
        self.selectInComboBox(obj.StockAllowanceMode, self.form.stockAllowanceMode)
        self._update_allowance_widgets()
        self._sync_allowance_mode_widgets(obj)

    def getSignalsForUpdate(self, obj):
        """Return signals that trigger task-panel updates."""

        del obj

        signals = []
        signals.append(self.form.toolController.currentIndexChanged)
        signals.append(self.form.coolantController.currentIndexChanged)
        signals.append(self.form.cutMode.currentIndexChanged)
        signals.append(self.form.clearingPattern.currentIndexChanged)
        signals.append(self.form.optimizationMode.currentIndexChanged)
        signals.append(self.form.stepOverPercent.editingFinished)
        signals.append(self.form.extraOffset.editingFinished)
        signals.append(self.form.angle.editingFinished)
        signals.append(self.form.protectSelectedFeatures.clicked)
        signals.append(self.form.clearEdges.clicked)
        signals.append(self.form.useStartPoint.clicked)
        signals.append(self.form.featureAllowanceMode.currentIndexChanged)
        signals.append(self.form.stockAllowanceMode.currentIndexChanged)
        signals.append(self.form.featureAllowanceLinked.editingFinished)
        signals.append(self.form.featureAllowanceXY.editingFinished)
        signals.append(self.form.featureAllowanceZ.editingFinished)
        signals.append(self.form.stockAllowanceLinked.editingFinished)
        signals.append(self.form.stockAllowanceXY.editingFinished)
        signals.append(self.form.stockAllowanceZ.editingFinished)
        return signals

    def updateData(self, obj, prop):
        """Refresh the page when the edited operation changes."""

        if getattr(self, "_applying_form_fields", False):
            return

        if prop in {
            "Angle",
            "ClearEdges",
            "ClearingPattern",
            "CutMode",
            "ExtraOffset",
            "FeatureAllowanceMode",
            "FeatureAllowanceXY",
            "FeatureAllowanceZ",
            "OptimizationMode",
            "ProtectSelectedFeatures",
            "StepOver",
            "StockAllowanceMode",
            "StockAllowanceXY",
            "StockAllowanceZ",
            "UseStartPoint",
        }:
            self.setFields(obj)


Command = PathOpGui.SetupOperation(
    "VolumeFaceMill",
    PathVolumeFaceMill.Create,
    TaskPanelOpPage,
    "CAM_VolumeFaceMill",
    QT_TRANSLATE_NOOP("CAM_VolumeFaceMill", "Volume Face Mill"),
    QT_TRANSLATE_NOOP(
        "CAM_VolumeFaceMill",
        "Create a stock-aware volume face milling operation",
    ),
    PathVolumeFaceMill.SetupProperties,
)

FreeCAD.Console.PrintLog("Loading PathVolumeFaceMillGui... done\n")
