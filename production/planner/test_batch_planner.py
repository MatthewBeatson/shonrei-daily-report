import unittest

from batch_planner import DemandLine, BatchLine, SuggestedBatch, InvalidRunSizeError, split_into_batches


class SplitIntoBatchesTests(unittest.TestCase):
    def test_empty_demand_produces_no_batches(self):
        self.assertEqual(split_into_batches([], 50), [])

    def test_single_line_smaller_than_run_size_is_one_batch(self):
        batches = split_into_batches([DemandLine('SO-1', 20)], 50)
        self.assertEqual(batches, [
            SuggestedBatch(20, 1, (BatchLine('SO-1', 20),)),
        ])

    def test_single_line_exactly_run_size(self):
        batches = split_into_batches([DemandLine('SO-1', 50)], 50)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].qty_planned, 50)

    def test_line_larger_than_run_size_splits_across_batches(self):
        # Highest-priority SO needs 120, run size is 50 -> 3 batches, all
        # still SO-1, still in priority order.
        batches = split_into_batches([DemandLine('SO-1', 120)], 50)
        self.assertEqual([b.qty_planned for b in batches], [50, 50, 20])
        self.assertEqual([b.priority_rank for b in batches], [1, 2, 3])
        for b in batches:
            self.assertEqual(len(b.lines), 1)
            self.assertEqual(b.lines[0].so_number, 'SO-1')

    def test_multiple_small_lines_combine_into_one_batch(self):
        # Three small backordered SOs, all fit in one run -- the floor
        # gets one batch covering all three, in priority order.
        lines = [DemandLine('SO-1', 10), DemandLine('SO-2', 15), DemandLine('SO-3', 5)]
        batches = split_into_batches(lines, 50)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].qty_planned, 30)
        self.assertEqual(
            [(l.so_number, l.qty_allocated) for l in batches[0].lines],
            [('SO-1', 10), ('SO-2', 15), ('SO-3', 5)],
        )

    def test_priority_order_preserved_when_a_line_spans_a_batch_boundary(self):
        # SO-1 (oldest, highest priority) needs 60; run size 50. First
        # batch is SO-1's first 50 units. The remaining 10 units of SO-1
        # must be covered *before* SO-2 gets any of its 30 -- SO-1 is
        # more urgent, so it can't be pushed behind a younger order.
        lines = [DemandLine('SO-1', 60), DemandLine('SO-2', 30)]
        batches = split_into_batches(lines, 50)
        self.assertEqual([b.qty_planned for b in batches], [50, 40])
        self.assertEqual(batches[0].lines, (BatchLine('SO-1', 50),))
        self.assertEqual(batches[1].lines, (BatchLine('SO-1', 10), BatchLine('SO-2', 30)))

    def test_zero_qty_lines_are_skipped(self):
        batches = split_into_batches([DemandLine('SO-1', 0), DemandLine('SO-2', 10)], 50)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].lines, (BatchLine('SO-2', 10),))

    def test_non_positive_run_size_raises(self):
        with self.assertRaises(InvalidRunSizeError):
            split_into_batches([DemandLine('SO-1', 10)], 0)
        with self.assertRaises(InvalidRunSizeError):
            split_into_batches([DemandLine('SO-1', 10)], -5)


if __name__ == '__main__':
    unittest.main()
