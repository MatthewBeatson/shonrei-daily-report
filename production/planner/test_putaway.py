import unittest

from putaway import PutawayResult, check_putaway


class CheckPutawayTests(unittest.TestCase):
    def test_matching_location_is_a_match(self):
        result = check_putaway('A-03-02', home_location_code='A-03-02')
        self.assertEqual(result, PutawayResult(matched=True, expected_location_code='A-03-02', same_bay=None))

    def test_mismatched_location_is_flagged(self):
        # No bay codes given -- same_bay defaults to False (the cautious
        # "can't determine, don't treat as close enough" default).
        result = check_putaway('B-01-01', home_location_code='A-03-02')
        self.assertEqual(result, PutawayResult(matched=False, expected_location_code='A-03-02', same_bay=False))

    def test_case_and_whitespace_insensitive(self):
        result = check_putaway('  a-03-02  ', home_location_code='A-03-02')
        self.assertTrue(result.matched)

    def test_no_home_location_assigned_yet_is_neither_matched_nor_mismatched(self):
        result = check_putaway('A-03-02', home_location_code=None)
        self.assertIsNone(result.matched)
        self.assertIsNone(result.expected_location_code)
        self.assertIsNone(result.same_bay)

    def test_mismatch_within_the_same_bay_is_flagged_as_such(self):
        # Expected SRM-B1-04, scanned SRM-B1-07 -- wrong shelf, right bay.
        result = check_putaway(
            'SRM-B1-07', home_location_code='SRM-B1-04',
            scanned_bay_code='SRM-B1', home_bay_code='SRM-B1',
        )
        self.assertFalse(result.matched)
        self.assertTrue(result.same_bay)

    def test_mismatch_in_a_different_bay_is_flagged_as_such(self):
        result = check_putaway(
            'SRM-B2-01', home_location_code='SRM-B1-04',
            scanned_bay_code='SRM-B2', home_bay_code='SRM-B1',
        )
        self.assertFalse(result.matched)
        self.assertFalse(result.same_bay)

    def test_same_bay_comparison_is_case_and_whitespace_insensitive(self):
        result = check_putaway(
            'SRM-B1-07', home_location_code='SRM-B1-04',
            scanned_bay_code='  srm-b1  ', home_bay_code='SRM-B1',
        )
        self.assertTrue(result.same_bay)

    def test_unknown_scanned_bay_code_is_not_treated_as_same_bay(self):
        # The scanned code doesn't resolve to any known location at all
        # (mistyped/garbage scan) -- never wave that through as "close
        # enough" just because we can't prove otherwise.
        result = check_putaway(
            'GARBAGE-CODE', home_location_code='SRM-B1-04',
            scanned_bay_code=None, home_bay_code='SRM-B1',
        )
        self.assertFalse(result.same_bay)

    def test_missing_home_bay_code_is_not_treated_as_same_bay(self):
        result = check_putaway(
            'SRM-B1-07', home_location_code='SRM-B1-04',
            scanned_bay_code='SRM-B1', home_bay_code=None,
        )
        self.assertFalse(result.same_bay)


if __name__ == '__main__':
    unittest.main()
