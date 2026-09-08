import unittest

from target_sync import ExistingTarget, TargetAction, apply_actual_to_target, plan_target_action, plan_target_actions


class PlanTargetActionTests(unittest.TestCase):
    def test_new_demand_no_existing_target_creates(self):
        action = plan_target_action('FG-A', 40, existing=None)
        self.assertEqual(action, TargetAction('FG-A', 'create', None, 40))

    def test_no_demand_no_existing_target_is_noop(self):
        action = plan_target_action('FG-A', 0, existing=None)
        self.assertEqual(action, TargetAction('FG-A', 'noop', None, None))

    def test_demand_changed_adjusts_existing_target(self):
        existing = ExistingTarget(id='t1', outstanding_qty=40)
        action = plan_target_action('FG-A', 55, existing)
        self.assertEqual(action, TargetAction('FG-A', 'adjust', 't1', 55))

    def test_demand_unchanged_is_noop(self):
        existing = ExistingTarget(id='t1', outstanding_qty=40)
        action = plan_target_action('FG-A', 40, existing)
        self.assertEqual(action, TargetAction('FG-A', 'noop', 't1', None))

    def test_demand_gone_closes_existing_target(self):
        existing = ExistingTarget(id='t1', outstanding_qty=40)
        action = plan_target_action('FG-A', 0, existing)
        self.assertEqual(action, TargetAction('FG-A', 'close', 't1', None))

    def test_batch_covers_create_adjust_close_and_untouched(self):
        demand = {'FG-NEW': 10, 'FG-SAME': 20, 'FG-GONE': 0}
        existing = {
            'FG-SAME': ExistingTarget('t2', 20),
            'FG-GONE': ExistingTarget('t3', 15),
        }
        actions = {a.sku: a for a in plan_target_actions(demand, existing)}
        self.assertEqual(actions['FG-NEW'].kind, 'create')
        self.assertEqual(actions['FG-SAME'].kind, 'noop')
        self.assertEqual(actions['FG-GONE'].kind, 'close')


class ApplyActualToTargetTests(unittest.TestCase):
    def test_normal_reduction(self):
        existing = ExistingTarget(id='t1', outstanding_qty=40)
        self.assertEqual(apply_actual_to_target(existing, 15), 25)

    def test_overrun_clamps_at_zero_not_negative(self):
        # Floor made more than was left on the target -- surplus is
        # banked stock, not a debt against the target (policy call).
        existing = ExistingTarget(id='t1', outstanding_qty=10)
        self.assertEqual(apply_actual_to_target(existing, 25), 0)

    def test_exact_completion_reaches_zero(self):
        existing = ExistingTarget(id='t1', outstanding_qty=10)
        self.assertEqual(apply_actual_to_target(existing, 10), 0)


if __name__ == '__main__':
    unittest.main()
