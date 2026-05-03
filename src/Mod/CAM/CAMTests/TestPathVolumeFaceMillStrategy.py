# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
import Part

from CAMTests.PathTestUtils import PathTestBase
import Path.Base.Generator.volume_face_mill_common as common
import Path.Base.Generator.volume_face_mill_entry as entry
import Path.Base.Generator.volume_face_mill_sections as sections
import Path.Base.Generator.volume_face_mill_validation as validation


class TestPathVolumeFaceMillStrategy(PathTestBase):
    """Test Volume Face Mill strict-strategy metadata."""

    @staticmethod
    def _rect_face(xmin, xmax, ymin, ymax, z=0.0):
        points = [
            FreeCAD.Vector(xmin, ymin, z),
            FreeCAD.Vector(xmax, ymin, z),
            FreeCAD.Vector(xmax, ymax, z),
            FreeCAD.Vector(xmin, ymax, z),
            FreeCAD.Vector(xmin, ymin, z),
        ]
        return Part.Face(Part.makePolygon(points))

    @staticmethod
    def _stock_boundbox():
        return Part.makeBox(100.0, 100.0, 10.0, FreeCAD.Vector(0, 0, 0)).BoundBox

    @staticmethod
    def _stock_shape():
        return Part.makeBox(100.0, 100.0, 10.0, FreeCAD.Vector(0, 0, 0))

    @staticmethod
    def _make_rectangle_wire(xmin, xmax, ymin, ymax, z):
        return Part.makePolygon(
            [
                FreeCAD.Vector(xmin, ymin, z),
                FreeCAD.Vector(xmax, ymin, z),
                FreeCAD.Vector(xmax, ymax, z),
                FreeCAD.Vector(xmin, ymax, z),
                FreeCAD.Vector(xmin, ymin, z),
            ]
        )

    def _make_region(self, xmin, xmax, ymin, ymax, z, region_id=1, inner_wires=None):
        return common.CutRegion(
            z=float(z),
            outer_wire=self._make_rectangle_wire(xmin, xmax, ymin, ymax, z),
            inner_wires=list(inner_wires or []),
            region_id=int(region_id),
        )

    def test_motion_kind_constants_are_stable(self):
        self.assertEqual(common.MOTION_ENTRY_PLUNGE, "entry_plunge")
        self.assertEqual(common.MOTION_LEAD_IN, "lead_in")
        self.assertEqual(common.MOTION_CUT, "cut")
        self.assertEqual(common.MOTION_STAY_DOWN_LINK, "stay_down_link")
        self.assertEqual(common.MOTION_RETRACT, "retract")
        self.assertEqual(common.MOTION_RAPID, "rapid")
        self.assertEqual(common.MOTION_INTERNAL_REPLUNGE, "internal_replunge")
        self.assertEqual(common.MOTION_OUTSIDE_REENTRY, "outside_reentry")
        self.assertEqual(common.MOTION_EXIT, "exit")
        self.assertEqual(common.CUT_MOTION_KINDS, frozenset({"cut"}))
        self.assertEqual(
            common.DOWNWARD_PLUNGE_MOTION_KINDS,
            frozenset({"entry_plunge", "internal_replunge", "outside_reentry"}),
        )
        self.assertEqual(
            common.NON_CUTTING_MOTION_KINDS,
            frozenset(
                {
                    "entry_plunge",
                    "lead_in",
                    "stay_down_link",
                    "retract",
                    "rapid",
                    "internal_replunge",
                    "outside_reentry",
                    "exit",
                }
            ),
        )
        self.assertEqual(
            common.ALL_MOTION_KINDS, common.CUT_MOTION_KINDS | common.NON_CUTTING_MOTION_KINDS
        )

    def test_cut_region_defaults(self):
        region = common.CutRegion(z=-1.0, outer_wire=object())

        self.assertEqual(region.inner_wires, [])
        self.assertEqual(region.region_id, 0)
        self.assertEqual(region.metadata, {})

    def test_cut_segment_defaults_to_non_reversible(self):
        segment = common.CutSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(10, 0, 0),
            z=-2.0,
        )

        self.assertFalse(segment.can_reverse)
        self.assertEqual(segment.commands, [])
        self.assertEqual(segment.metadata, {})

    def test_motion_segment_metadata_fields(self):
        motion = common.MotionSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(0, 0, -5),
            z_start=0.0,
            z_end=-5.0,
            kind=common.MOTION_ENTRY_PLUNGE,
        )

        self.assertEqual(motion.kind, common.MOTION_ENTRY_PLUNGE)
        self.assertFalse(motion.is_cutting)
        self.assertFalse(motion.is_retracted)

    def test_layer_plan_and_strategy_result_defaults(self):
        first_plan = common.LayerPlan(z=-1.0)
        second_plan = common.LayerPlan(z=-2.0)
        first_plan.regions.append("region")
        first_plan.cut_segments.append("cut")
        first_plan.motions.append("motion")
        first_plan.metadata["key"] = "value"

        self.assertEqual(second_plan.regions, [])
        self.assertEqual(second_plan.cut_segments, [])
        self.assertEqual(second_plan.motions, [])
        self.assertEqual(second_plan.metadata, {})

        first_result = common.StrategyResult()
        second_result = common.StrategyResult()
        first_result.commands.append("cmd")
        first_result.layers.append(first_plan)
        first_result.validation_errors.append("error")
        first_result.metadata["strategy"] = "test"

        self.assertEqual(second_result.commands, [])
        self.assertEqual(second_result.layers, [])
        self.assertEqual(second_result.validation_errors, [])
        self.assertEqual(second_result.metadata, {})

    def test_geometry_helpers(self):
        a = FreeCAD.Vector(1.0, 2.0, 3.0)
        b = FreeCAD.Vector(4.0, 6.0, 15.0)
        copied = common.copy_vector(a)
        self.assertCoincide(copied, a)
        self.assertIsNot(copied, a)

        self.assertAlmostEqual(common.xy_distance(a, b), 5.0, places=6)
        self.assertAlmostEqual(common.xyz_distance(a, b), 13.0, places=6)

        downward = common.MotionSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(0, 0, -1),
            z_start=5.0,
            z_end=4.0,
            kind=common.MOTION_ENTRY_PLUNGE,
        )
        level = common.MotionSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(2, 0, 0),
            z_start=2.0,
            z_end=2.0,
            kind=common.MOTION_LEAD_IN,
        )
        self.assertTrue(common.motion_is_downward(downward))
        self.assertFalse(common.motion_is_downward(level))

        boundbox = Part.makeBox(10.0, 20.0, 5.0, FreeCAD.Vector(0, 0, 0)).BoundBox
        inside_point = FreeCAD.Vector(5.0, 10.0, 99.0)
        edge_point = FreeCAD.Vector(0.0, 10.0, -5.0)
        outside_point = FreeCAD.Vector(13.0, 24.0, 0.0)

        self.assertTrue(common.xy_inside_boundbox(inside_point, boundbox))
        self.assertTrue(common.xy_inside_boundbox(edge_point, boundbox))
        self.assertFalse(common.xy_inside_boundbox(outside_point, boundbox))
        self.assertFalse(common.xy_outside_boundbox(inside_point, boundbox))
        self.assertTrue(common.xy_outside_boundbox(outside_point, boundbox))

        self.assertAlmostEqual(
            common.minimum_xy_clearance_from_boundbox(outside_point, boundbox),
            5.0,
            places=6,
        )
        self.assertAlmostEqual(
            common.minimum_xy_clearance_from_boundbox(edge_point, boundbox),
            -0.0,
            places=6,
        )
        self.assertAlmostEqual(
            common.minimum_xy_clearance_from_boundbox(inside_point, boundbox),
            -5.0,
            places=6,
        )

        cut_segment = common.CutSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(3, 4, 0),
            z=0.0,
        )
        self.assertAlmostEqual(common.cut_segment_length(cut_segment), 5.0, places=6)

        motion = common.MotionSegment(
            start=FreeCAD.Vector(0, 0, 999),
            end=FreeCAD.Vector(3, 4, 999),
            z_start=0.0,
            z_end=12.0,
            kind=common.MOTION_RAPID,
        )
        self.assertAlmostEqual(common.motion_length(motion), 13.0, places=6)

    def test_validate_motion_kinds_rejects_unknown_kind(self):
        motion = common.MotionSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(0, 0, 0),
            z_start=0.0,
            z_end=0.0,
            kind="mystery",
        )

        errors = validation.validate_motion_kinds([motion])
        self.assertGreater(len(errors), 0)
        self.assertIn("unknown kind", errors[0])

    def test_validate_cut_modes_rejects_cut_mode_mismatch(self):
        cut = common.CutSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(10, 0, 0),
            z=0.0,
            cut_mode="Conventional",
        )

        errors = validation.validate_cut_modes([cut], "Climb")
        self.assertGreater(len(errors), 0)
        self.assertIn("expected Climb", errors[0])

    def test_validate_cut_modes_rejects_reversible_cut(self):
        cut = common.CutSegment(
            start=FreeCAD.Vector(0, 0, 0),
            end=FreeCAD.Vector(10, 0, 0),
            z=0.0,
            cut_mode="Climb",
            can_reverse=True,
        )

        errors = validation.validate_cut_modes([cut], "Climb")
        self.assertGreater(len(errors), 0)
        self.assertIn("reversible", errors[0])

    def test_validate_no_reversed_cuts_rejects_reversed_segment(self):
        original_start = FreeCAD.Vector(0, 0, 0)
        original_end = FreeCAD.Vector(20, 0, 0)
        cut = common.CutSegment(
            start=common.copy_vector(original_end),
            end=common.copy_vector(original_start),
            z=0.0,
            original_start=original_start,
            original_end=original_end,
        )

        errors = validation.validate_no_reversed_cuts([cut])
        self.assertGreater(len(errors), 0)
        self.assertIn("reversed", errors[0])

    def test_validate_layer_starts_with_outside_entry(self):
        stock_boundbox = self._stock_boundbox()
        invalid_layer = common.LayerPlan(
            z=0.0,
            motions=[
                common.MotionSegment(
                    start=FreeCAD.Vector(50, 50, 10),
                    end=FreeCAD.Vector(50, 50, 0),
                    z_start=10.0,
                    z_end=0.0,
                    kind=common.MOTION_ENTRY_PLUNGE,
                )
            ],
        )
        valid_layer = common.LayerPlan(
            z=0.0,
            motions=[
                common.MotionSegment(
                    start=FreeCAD.Vector(-5, 50, 10),
                    end=FreeCAD.Vector(-5, 50, 0),
                    z_start=10.0,
                    z_end=0.0,
                    kind=common.MOTION_ENTRY_PLUNGE,
                )
            ],
        )

        self.assertGreater(
            len(
                validation.validate_layer_starts_with_outside_entry(
                    invalid_layer, stock_boundbox, 2.0
                )
            ),
            0,
        )
        self.assertEqual(
            validation.validate_layer_starts_with_outside_entry(valid_layer, stock_boundbox, 2.0),
            [],
        )

    def test_validate_no_plunge_into_uncut_stock_rejects_inside_rapid(self):
        stock_boundbox = self._stock_boundbox()
        motion = common.MotionSegment(
            start=FreeCAD.Vector(50, 50, 10),
            end=FreeCAD.Vector(50, 50, 0),
            z_start=10.0,
            z_end=0.0,
            kind=common.MOTION_RAPID,
        )

        errors = validation.validate_no_plunge_into_uncut_stock([motion], stock_boundbox, [], 2.0)
        self.assertGreater(len(errors), 0)
        self.assertIn("expected internal_replunge", errors[0])

    def test_validate_internal_replunge_requires_cleared_material(self):
        stock_boundbox = self._stock_boundbox()
        clear_state = common.LayerClearState(
            z=10.0,
            cleared_region=self._rect_face(45.0, 55.0, 45.0, 55.0, z=10.0),
        )
        valid_motion = common.MotionSegment(
            start=FreeCAD.Vector(50, 50, 15),
            end=FreeCAD.Vector(50, 50, 10),
            z_start=15.0,
            z_end=10.0,
            kind=common.MOTION_INTERNAL_REPLUNGE,
            layer_z=10.0,
        )
        invalid_motion = common.MotionSegment(
            start=FreeCAD.Vector(70, 70, 15),
            end=FreeCAD.Vector(70, 70, 10),
            z_start=15.0,
            z_end=10.0,
            kind=common.MOTION_INTERNAL_REPLUNGE,
            layer_z=10.0,
        )

        self.assertEqual(
            validation.validate_no_plunge_into_uncut_stock(
                [valid_motion], stock_boundbox, [clear_state], 2.0
            ),
            [],
        )
        errors = validation.validate_no_plunge_into_uncut_stock(
            [invalid_motion], stock_boundbox, [clear_state], 2.0
        )
        self.assertGreater(len(errors), 0)
        self.assertIn("not fully inside cleared material", errors[0])

    def test_validate_internal_replunge_cannot_end_below_active_layer(self):
        stock_boundbox = self._stock_boundbox()
        motion = common.MotionSegment(
            start=FreeCAD.Vector(50, 50, 15),
            end=FreeCAD.Vector(50, 50, 5),
            z_start=15.0,
            z_end=5.0,
            kind=common.MOTION_INTERNAL_REPLUNGE,
            layer_z=10.0,
        )

        errors = validation.validate_no_plunge_into_uncut_stock([motion], stock_boundbox, [], 2.0)
        self.assertGreater(len(errors), 0)
        self.assertIn("does not end at its active layer Z", errors[0])

    def test_validate_stay_down_links_require_cleared_corridor(self):
        motion = common.MotionSegment(
            start=FreeCAD.Vector(20, 20, 0),
            end=FreeCAD.Vector(30, 20, 0),
            z_start=0.0,
            z_end=0.0,
            kind=common.MOTION_STAY_DOWN_LINK,
            layer_z=0.0,
        )
        valid_clear_state = common.LayerClearState(
            z=0.0,
            cleared_region=self._rect_face(15.0, 35.0, 15.0, 25.0, z=0.0),
        )
        invalid_clear_state = common.LayerClearState(
            z=0.0,
            cleared_region=self._rect_face(24.0, 26.0, 19.0, 21.0, z=0.0),
        )

        self.assertEqual(
            validation.validate_stay_down_links([motion], [valid_clear_state], 2.0),
            [],
        )
        errors = validation.validate_stay_down_links([motion], [invalid_clear_state], 2.0)
        self.assertGreater(len(errors), 0)
        self.assertIn("not fully inside cleared material", errors[0])

    def test_validate_no_cut_crosses_keepout_rejects_overlap(self):
        cut = common.CutSegment(
            start=FreeCAD.Vector(10, 10, 0),
            end=FreeCAD.Vector(30, 10, 0),
            z=0.0,
        )
        protected = self._rect_face(18.0, 22.0, 7.0, 13.0, z=0.0)

        errors = validation.validate_no_cut_crosses_keepout([cut], [protected], 2.0)
        self.assertGreater(len(errors), 0)
        self.assertIn("overlaps protected region", errors[0])

    def test_validate_strategy_result_aggregates_validator_errors(self):
        stock_boundbox = self._stock_boundbox()
        cut = common.CutSegment(
            start=FreeCAD.Vector(10, 10, 0),
            end=FreeCAD.Vector(20, 10, 0),
            z=0.0,
            cut_mode="Climb",
            original_start=FreeCAD.Vector(10, 10, 0),
            original_end=FreeCAD.Vector(20, 10, 0),
        )
        entry = common.MotionSegment(
            start=FreeCAD.Vector(-5, 10, 5),
            end=FreeCAD.Vector(-5, 10, 0),
            z_start=5.0,
            z_end=0.0,
            kind=common.MOTION_ENTRY_PLUNGE,
        )
        clear_state = common.LayerClearState(
            z=0.0,
            cleared_region=self._rect_face(0.0, 30.0, 0.0, 20.0, z=0.0),
        )
        layer = common.LayerPlan(
            z=0.0,
            cut_segments=[cut],
            motions=[entry],
            cleared_state=clear_state,
        )
        result = common.StrategyResult(layers=[layer])

        errors = validation.validate_strategy_result(
            result=result,
            stock_boundbox=stock_boundbox,
            expected_cut_mode="Climb",
            tool_radius=2.0,
            entry_clearance=2.0,
        )
        self.assertEqual(errors, [])
        self.assertEqual(result.validation_errors, [])

        cut.cut_mode = "Conventional"
        errors = validation.validate_strategy_result(
            result=result,
            stock_boundbox=stock_boundbox,
            expected_cut_mode="Climb",
            tool_radius=2.0,
            entry_clearance=2.0,
        )
        self.assertGreater(len(errors), 0)
        self.assertEqual(result.validation_errors, errors)

    def test_depth_values_from_depthparams_returns_descending_unique_values(self):
        values = sections.depth_values_from_depthparams(
            [5.0, 1.0, 5.0, 3.0, float("nan"), float("inf")],
            start_depth=10.0,
            final_depth=0.0,
        )

        self.assertEqual(values, [10.0, 5.0, 3.0, 1.0, 0.0])

    def test_make_cut_regions_returns_region_for_simple_box_section(self):
        removal = self._stock_shape()
        regions = sections.make_cut_regions(removal, [5.0])

        self.assertEqual(len(regions), 1)
        self.assertIsNotNone(regions[0].outer_wire)
        self.assertEqual(regions[0].z, 5.0)

    def test_cut_region_preserves_inner_wire_for_box_with_island(self):
        outer = Part.makeBox(100, 100, 10, FreeCAD.Vector(0, 0, 0))
        island = Part.makeBox(20, 20, 10, FreeCAD.Vector(40, 40, 0))
        removal = outer.cut(island)

        section = sections.section_shape_at_z(removal, 5.0)
        regions = sections.cut_regions_from_section(section, 5.0, source_shape=removal)

        self.assertEqual(len(regions), 1)
        self.assertIsNotNone(regions[0].outer_wire)
        self.assertGreaterEqual(len(regions[0].inner_wires), 1)
        self.assertEqual(regions[0].z, 5.0)

    def test_entry_side_auto_resolves_to_minus_x(self):
        self.assertEqual(entry.resolve_entry_side("Auto"), "-X")

    def test_common_plunge_point_minus_x_is_outside_stock(self):
        point = entry.common_plunge_point(self._stock_boundbox(), "-X", 10.0)
        self.assertAlmostEqual(point.x, -10.0, places=6)
        self.assertAlmostEqual(point.y, 50.0, places=6)
        self.assertTrue(common.xy_outside_boundbox(point, self._stock_boundbox()))

    def test_common_plunge_point_plus_x_is_outside_stock(self):
        point = entry.common_plunge_point(self._stock_boundbox(), "+X", 10.0)
        self.assertAlmostEqual(point.x, 110.0, places=6)
        self.assertAlmostEqual(point.y, 50.0, places=6)
        self.assertTrue(common.xy_outside_boundbox(point, self._stock_boundbox()))

    def test_common_plunge_point_minus_y_is_outside_stock(self):
        point = entry.common_plunge_point(self._stock_boundbox(), "-Y", 10.0)
        self.assertAlmostEqual(point.x, 50.0, places=6)
        self.assertAlmostEqual(point.y, -10.0, places=6)
        self.assertTrue(common.xy_outside_boundbox(point, self._stock_boundbox()))

    def test_common_plunge_point_plus_y_is_outside_stock(self):
        point = entry.common_plunge_point(self._stock_boundbox(), "+Y", 10.0)
        self.assertAlmostEqual(point.x, 50.0, places=6)
        self.assertAlmostEqual(point.y, 110.0, places=6)
        self.assertTrue(common.xy_outside_boundbox(point, self._stock_boundbox()))

    def test_layer_entry_uses_common_plunge_point_at_layer_z(self):
        plan = entry.make_entry_plan(self._stock_boundbox(), "-X", 10.0)
        layer_entry = entry.make_layer_entry(plan, 5.0)

        self.assertAlmostEqual(layer_entry.plunge_point.x, plan.common_plunge_point.x, places=6)
        self.assertAlmostEqual(layer_entry.plunge_point.y, plan.common_plunge_point.y, places=6)
        self.assertAlmostEqual(layer_entry.plunge_point.z, 5.0, places=6)

    def test_lead_in_start_aligns_with_first_cut_start_for_x_entry(self):
        plan = entry.make_entry_plan(self._stock_boundbox(), "-X", 10.0)
        first_cut_start = FreeCAD.Vector(20.0, 30.0, 5.0)
        lead_in_start = entry.lead_in_start_for_cut(plan, first_cut_start)

        self.assertAlmostEqual(lead_in_start.x, plan.common_plunge_point.x, places=6)
        self.assertAlmostEqual(lead_in_start.y, first_cut_start.y, places=6)
        self.assertAlmostEqual(lead_in_start.z, first_cut_start.z, places=6)

    def test_lead_in_start_aligns_with_first_cut_start_for_y_entry(self):
        plan = entry.make_entry_plan(self._stock_boundbox(), "+Y", 10.0)
        first_cut_start = FreeCAD.Vector(20.0, 30.0, 5.0)
        lead_in_start = entry.lead_in_start_for_cut(plan, first_cut_start)

        self.assertAlmostEqual(lead_in_start.x, first_cut_start.x, places=6)
        self.assertAlmostEqual(lead_in_start.y, plan.common_plunge_point.y, places=6)
        self.assertAlmostEqual(lead_in_start.z, first_cut_start.z, places=6)

    def test_entry_plunge_motion_is_downward_entry_plunge(self):
        plan = entry.make_entry_plan(self._stock_boundbox(), "-X", 10.0)
        layer_entry = entry.make_layer_entry(plan, 5.0)
        motion = entry.make_entry_plunge_motion(layer_entry, 15.0)

        self.assertEqual(motion.kind, common.MOTION_ENTRY_PLUNGE)
        self.assertFalse(motion.is_cutting)
        self.assertFalse(motion.is_retracted)
        self.assertEqual(motion.layer_z, 5.0)
        self.assertTrue(common.motion_is_downward(motion))

    def test_lead_in_motion_is_non_cutting(self):
        plan = entry.make_entry_plan(self._stock_boundbox(), "-X", 10.0)
        layer_entry = entry.make_layer_entry(
            plan,
            5.0,
            first_cut_start=FreeCAD.Vector(20.0, 30.0, 5.0),
        )
        motion = entry.make_lead_in_motion(layer_entry)

        self.assertIsNotNone(motion)
        self.assertEqual(motion.kind, common.MOTION_LEAD_IN)
        self.assertFalse(motion.is_cutting)
        self.assertFalse(motion.is_retracted)
        self.assertAlmostEqual(motion.z_start, 5.0, places=6)
        self.assertAlmostEqual(motion.z_end, 5.0, places=6)
