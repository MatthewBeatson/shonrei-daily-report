import unittest

from putaway import PutawayResult, check_putaway


class CheckPutawayTests(unittest.TestCase):
    def test_matching_location_is_a_match(self):
        result = check_putaway('A-03-02', home_location_code='A-03-02')
        self.assertEqual(result, PutawayResult(matched=True, expected_location_code='A-03-02'))

    def test_mismatched_location_is_flagged(self):
        result = check_putaway('B-01-01', home_location_code='A-03-02')
        self.assertEqual(result, PutawayResult(matched=False, expected_location_code='A-03-02'))

    def test_case_and_whitespace_insensitive(self):
        result = check_putaway('  a-03-02  ', home_location_code='A-03-02')
        self.assertTrue(result.matched)

    def test_no_home_location_assigned_yet_is_neither_matched_nor_mismatched(self):
        result = check_putaway('A-03-02', home_location_code=None)
        self.assertIsNone(result.matched)
        self.assertIsNone(result.expected_location_code)


if __name__ == '__main__':
    unittest.main()
