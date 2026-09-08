import unittest

from labels import batch_label_zpl, location_label_zpl, sku_label_zpl


class SkuLabelZplTests(unittest.TestCase):
    def test_contains_the_sku_as_barcode_payload(self):
        zpl = sku_label_zpl('FG-2400-BOX')
        self.assertIn('^FDFG-2400-BOX^FS', zpl)
        self.assertIn('^BCN,140,Y,N,N', zpl)  # Code128 barcode field present

    def test_well_formed_start_and_end(self):
        zpl = sku_label_zpl('FG-2400-BOX')
        self.assertTrue(zpl.startswith('^XA\n'))
        self.assertTrue(zpl.rstrip().endswith('^XZ'))

    def test_description_included_when_given(self):
        zpl = sku_label_zpl('FG-2400-BOX', description='2400 Series Box')
        self.assertIn('2400 Series Box', zpl)

    def test_no_description_line_when_omitted(self):
        zpl = sku_label_zpl('FG-2400-BOX')
        self.assertNotIn('A0N,28,28', zpl)  # description's font size, absent entirely

    def test_caret_and_tilde_stripped_from_sku(self):
        # ZPL's own command prefixes -- must never leak into a data field
        # from a SKU someone typo'd or copy-pasted oddly.
        zpl = sku_label_zpl('FG~2400^BOX')
        self.assertIn('^FDFG2400BOX^FS', zpl)


class LocationLabelZplTests(unittest.TestCase):
    def test_location_code_is_the_only_barcode(self):
        # Shelves commonly hold several different SKUs at once, so the
        # label never tries to show "the" current SKU -- only one
        # barcode field, ever.
        zpl = location_label_zpl('A-03-02')
        self.assertIn('^FDA-03-02^FS', zpl)
        self.assertEqual(zpl.count('^BCN,'), 1)

    def test_no_stock_type_line_when_omitted(self):
        zpl = location_label_zpl('A-03-02')
        self.assertNotIn('A0N,28,28', zpl)

    def test_stock_type_printed_as_plain_text_not_a_barcode(self):
        zpl = location_label_zpl('A-03-02', stock_type='RM')
        self.assertIn('^FDRM^FS', zpl)
        self.assertEqual(zpl.count('^BCN,'), 1)  # still just the one barcode


class BatchLabelZplTests(unittest.TestCase):
    def test_contains_batch_code_sku_and_qty(self):
        zpl = batch_label_zpl('B-FG2400BOX-953E73', 'FG-2400-BOX', 30)
        self.assertIn('^FDB-FG2400BOX-953E73^FS', zpl)
        self.assertIn('FG-2400-BOX', zpl)
        self.assertIn('Qty: 30', zpl)

    def test_whole_number_qty_has_no_trailing_decimal(self):
        zpl = batch_label_zpl('B-X', 'SKU', 30.0)
        self.assertIn('Qty: 30', zpl)
        self.assertNotIn('Qty: 30.0', zpl)

    def test_fractional_qty_preserved(self):
        zpl = batch_label_zpl('B-X', 'SKU', 12.5)
        self.assertIn('Qty: 12.5', zpl)

    def test_priority_rank_included_when_given(self):
        zpl = batch_label_zpl('B-X', 'SKU', 30, priority_rank=1)
        self.assertIn('Priority 1', zpl)

    def test_no_priority_line_when_omitted(self):
        zpl = batch_label_zpl('B-X', 'SKU', 30)
        self.assertNotIn('Priority', zpl)


if __name__ == '__main__':
    unittest.main()
