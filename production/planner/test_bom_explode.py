"""Unit tests for bom_explode -- no Cin7 client or database needed.

Run with: python -m pytest production/planner/test_bom_explode.py
(or `python -m unittest` -- no pytest-only features used)
"""
import unittest

from bom_explode import BomLine, BuildStep, CircularBomError, explode_bom


class ExplodeBomTests(unittest.TestCase):
    def test_single_level_raw_materials_only(self):
        # Finished good made straight from purchased components: nothing
        # to build below it, just the FG itself.
        bom = {"FG-A": [BomLine("RAW-1", 2), BomLine("RAW-2", 1)]}
        steps = explode_bom("FG-A", 10, bom=bom)
        self.assertEqual(steps, [BuildStep("FG-A", 10, level=0)])

    def test_two_level_bom_orders_sub_assembly_first(self):
        # FG-A needs 2x SUB-B per unit; SUB-B is itself an assembly made
        # from raw material. Real-world case this whole thing exists for.
        bom = {
            "FG-A": [BomLine("SUB-B", 2)],
            "SUB-B": [BomLine("RAW-1", 3)],
        }
        steps = explode_bom("FG-A", 5, bom=bom)
        self.assertEqual(
            steps,
            [BuildStep("SUB-B", 10, level=1), BuildStep("FG-A", 5, level=0)],
        )

    def test_on_hand_stock_nets_off_demand(self):
        bom = {"FG-A": [BomLine("RAW-1", 1)]}
        steps = explode_bom("FG-A", 10, bom=bom, on_hand={"FG-A": 4})
        self.assertEqual(steps, [BuildStep("FG-A", 6, level=0)])

    def test_fully_covered_by_stock_produces_no_steps(self):
        bom = {"FG-A": [BomLine("RAW-1", 1)]}
        steps = explode_bom("FG-A", 5, bom=bom, on_hand={"FG-A": 5})
        self.assertEqual(steps, [])

    def test_open_assembly_qty_also_nets_off_demand(self):
        # An assembly already created+authorised in Cin7 for this SKU
        # should count as covering demand, same as on-hand stock, so a
        # second explosion run a few minutes later doesn't double-book it.
        bom = {"FG-A": [BomLine("RAW-1", 1)]}
        steps = explode_bom(
            "FG-A", 10, bom=bom, on_hand={"FG-A": 2}, open_assembly_qty={"FG-A": 3}
        )
        self.assertEqual(steps, [BuildStep("FG-A", 5, level=0)])

    def test_shared_sub_assembly_across_two_parents_is_merged(self):
        # SUB-C is used by both FG-A and FG-B -- should be built once, for
        # the combined net requirement, not once per parent.
        bom = {
            "FG-A": [BomLine("SUB-C", 1)],
            "FG-B": [BomLine("SUB-C", 2)],
            "SUB-C": [BomLine("RAW-1", 1)],
        }
        steps_a = explode_bom("FG-A", 4, bom=bom)
        self.assertEqual(steps_a, [BuildStep("SUB-C", 4, level=1), BuildStep("FG-A", 4, level=0)])

        # A single explosion of one parent doesn't see the other parent's
        # demand -- that's expected; a real planning pass calls explode_bom
        # once per top-level demand and sums the resulting steps per SKU
        # before driving Cin7, so the merge happens at the orchestration
        # layer, not inside a single explosion.

    def test_batch_size_rounds_build_qty_up(self):
        # SUB-B is only ever built in batches of 50.
        bom = {
            "FG-A": [BomLine("SUB-B", 1)],
            "SUB-B": [BomLine("RAW-1", 1)],
        }
        steps = explode_bom("FG-A", 120, bom=bom, batch_size={"SUB-B": 50})
        sub_b = next(s for s in steps if s.sku == "SUB-B")
        self.assertEqual(sub_b.qty_to_build, 150)

    def test_three_level_bom_orders_deepest_first(self):
        bom = {
            "FG-A": [BomLine("SUB-B", 1)],
            "SUB-B": [BomLine("SUB-C", 1)],
            "SUB-C": [BomLine("RAW-1", 1)],
        }
        steps = explode_bom("FG-A", 1, bom=bom)
        self.assertEqual([s.sku for s in steps], ["SUB-C", "SUB-B", "FG-A"])
        self.assertEqual([s.level for s in steps], [2, 1, 0])

    def test_circular_bom_raises(self):
        bom = {
            "FG-A": [BomLine("SUB-B", 1)],
            "SUB-B": [BomLine("FG-A", 1)],
        }
        with self.assertRaises(CircularBomError):
            explode_bom("FG-A", 1, bom=bom)

    def test_zero_demand_produces_no_steps(self):
        bom = {"FG-A": [BomLine("RAW-1", 1)]}
        self.assertEqual(explode_bom("FG-A", 0, bom=bom), [])


if __name__ == "__main__":
    unittest.main()
