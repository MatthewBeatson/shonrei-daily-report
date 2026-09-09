"""Unit tests for the confirmed Cin7Client methods, against the exact
response shapes captured via scripts/dump_sample_bom.py on a live
account (see cin7_client.py's module docstring and production/README.md)
-- requests.get is mocked, no live Cin7 access needed to run these.
"""
import unittest
from unittest.mock import MagicMock, patch

from cin7_client import Cin7Client

# Trimmed to the fields these tests actually touch -- the real response
# has ~60 fields per product, see production/README.md for the full dump.
REAL_PRODUCT_RESPONSE_NO_BOM_LINES = {
    "Total": 1, "Page": 1,
    "Products": [{
        "ID": "54a590e2-eee8-4b63-b5e9-02186cd3c77e",
        "SKU": "WIPMT20T",
        "BillOfMaterial": True,
        "BOMType": "Assembly",
        "QuantityToProduce": 180.0,
        "BillOfMaterialsProducts": [],
    }],
}

REAL_AVAILABILITY_RESPONSE = {
    "Total": 1, "Page": 1,
    "ProductAvailabilityList": [{
        "ID": "54a590e2-eee8-4b63-b5e9-02186cd3c77e",
        "SKU": "WIPMT20T",
        "OnHand": 43.0,
        "Allocated": 280.0,
        "Available": -237.0,
        "OnOrder": 250.0,
    }],
}


def _mock_response(json_body):
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


class GetBomTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.get')
    def test_hits_the_confirmed_product_endpoint(self, mock_get):
        mock_get.return_value = _mock_response(REAL_PRODUCT_RESPONSE_NO_BOM_LINES)
        with self.assertRaises(NotImplementedError):
            self.client.get_bom('WIPMT20T')
        args, kwargs = mock_get.call_args
        self.assertIn('/product', args[0])
        self.assertEqual(kwargs['params'], {'SKU': 'WIPMT20T'})

    @patch('cin7_client.requests.get')
    def test_empty_bom_lines_on_a_real_assembly_raises_not_wired(self, mock_get):
        # WIPMT20T is a real example: BillOfMaterial=true (it genuinely
        # is an assembly, confirmed in Cin7's own UI) but GET /product
        # returns no BillOfMaterialsProducts lines -- that must never be
        # read as "this SKU has no BOM" (bom_explode would then wrongly
        # treat a real assembly as a raw material).
        mock_get.return_value = _mock_response(REAL_PRODUCT_RESPONSE_NO_BOM_LINES)
        with self.assertRaises(NotImplementedError):
            self.client.get_bom('WIPMT20T')

    @patch('cin7_client.requests.get')
    def test_genuine_raw_material_returns_empty_list(self, mock_get):
        # BillOfMaterial: false -- an actual purchased/raw-material SKU,
        # the one case where [] is the correct answer.
        response = {
            "Total": 1, "Page": 1,
            "Products": [{"SKU": "RAW-CARDBOARD", "BillOfMaterial": False, "BillOfMaterialsProducts": []}],
        }
        mock_get.return_value = _mock_response(response)
        self.assertEqual(self.client.get_bom('RAW-CARDBOARD'), [])

    @patch('cin7_client.requests.get')
    def test_unknown_sku_raises(self, mock_get):
        mock_get.return_value = _mock_response({"Total": 0, "Page": 1, "Products": []})
        with self.assertRaises(ValueError):
            self.client.get_bom('NOPE')

    @patch('cin7_client.requests.get')
    def test_populated_line_with_sku_and_quantity_fields_maps_correctly(self, mock_get):
        # Best-effort field-name guess (see get_bom's docstring) -- this
        # is the shape it's expected to handle once a real populated
        # line is confirmed; update this fixture alongside the mapping
        # once scripts/dump_sample_bom.py returns one for real.
        response = {
            "Total": 1, "Page": 1,
            "Products": [{
                "SKU": "FG-ASSEMBLED",
                "BillOfMaterialsProducts": [
                    {"SKU": "RAW-CARDBOARD", "Quantity": 2.0},
                    {"SKU": "RAW-HINGE", "Quantity": 4.0},
                ],
            }],
        }
        mock_get.return_value = _mock_response(response)
        bom = self.client.get_bom('FG-ASSEMBLED')
        self.assertEqual(bom, [
            {'component_sku': 'RAW-CARDBOARD', 'qty_per': 2.0},
            {'component_sku': 'RAW-HINGE', 'qty_per': 4.0},
        ])

    @patch('cin7_client.requests.get')
    def test_unrecognised_line_shape_raises_loudly_instead_of_guessing_wrong(self, mock_get):
        response = {
            "Total": 1, "Page": 1,
            "Products": [{
                "SKU": "FG-ASSEMBLED",
                "BillOfMaterialsProducts": [{"SomeOtherField": "??"}],
            }],
        }
        mock_get.return_value = _mock_response(response)
        with self.assertRaises(NotImplementedError):
            self.client.get_bom('FG-ASSEMBLED')


class GetAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.get')
    def test_hits_the_confirmed_ref_productavailability_endpoint(self, mock_get):
        mock_get.return_value = _mock_response(REAL_AVAILABILITY_RESPONSE)
        self.client.get_availability(['WIPMT20T'])
        args, kwargs = mock_get.call_args
        self.assertIn('/ref/productavailability', args[0])
        self.assertEqual(kwargs['params'], {'SKU': 'WIPMT20T'})

    @patch('cin7_client.requests.get')
    def test_returns_the_netted_available_figure_not_raw_on_hand(self, mock_get):
        mock_get.return_value = _mock_response(REAL_AVAILABILITY_RESPONSE)
        result = self.client.get_availability(['WIPMT20T'])
        # Real data: OnHand 43, Allocated 280, Available -237 (43 - 280) --
        # get_availability should surface Cin7's own netted figure.
        self.assertEqual(result, {'WIPMT20T': -237.0})

    @patch('cin7_client.requests.get')
    def test_sku_with_no_availability_row_defaults_to_zero_not_an_error(self, mock_get):
        mock_get.return_value = _mock_response({"Total": 0, "Page": 1, "ProductAvailabilityList": []})
        result = self.client.get_availability(['NOPE'])
        self.assertEqual(result, {'NOPE': 0.0})

    @patch('cin7_client.requests.get')
    def test_one_request_per_sku(self, mock_get):
        mock_get.return_value = _mock_response(REAL_AVAILABILITY_RESPONSE)
        self.client.get_availability(['A', 'B', 'C'])
        self.assertEqual(mock_get.call_count, 3)


class GetStockOnHandTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.get')
    def test_returns_raw_on_hand_not_the_netted_available_figure(self, mock_get):
        # Stocktake compares a physical count against Cin7's physical
        # on-hand, not stock minus what's allocated elsewhere.
        mock_get.return_value = _mock_response(REAL_AVAILABILITY_RESPONSE)
        self.assertEqual(self.client.get_stock_on_hand('WIPMT20T'), 43.0)


if __name__ == '__main__':
    unittest.main()
