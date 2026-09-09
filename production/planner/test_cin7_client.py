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

# Captured live via scripts/dump_sample_bom.py WIPMT20T (2026-09-09), with
# IncludeBOM=true -- the actual fix confirmed for real, not just from docs.
# Trimmed to the fields get_bom touches; BillOfMaterialsServices (labour/
# service lines) is real too but get_bom/bom_explode don't model it.
REAL_PRODUCT_RESPONSE_WITH_BOM_LINES = {
    "Total": 1, "Page": 1,
    "Products": [{
        "ID": "54a590e2-eee8-4b63-b5e9-02186cd3c77e",
        "SKU": "WIPMT20T",
        "BillOfMaterial": True,
        "BOMType": "Assembly",
        "QuantityToProduce": 180.0,
        "BillOfMaterialsProducts": [
            {"ComponentProductID": "409e4427-3d03-4d51-90b2-146c8b26060f", "ProductCode": "RMFPE6BK", "Name": "FOAM PE30 6mm BLACK", "Quantity": 5.0, "WastagePercent": 0.0, "WastageQuantity": 0.0, "CostPercentage": 0.0},
            {"ComponentProductID": "078aabfc-7e7a-45c6-8833-94499b6fb30a", "ProductCode": "RMAD1181", "Name": "BOSTIK 1181S", "Quantity": 0.04, "WastagePercent": 0.0, "WastageQuantity": 0.0, "CostPercentage": 0.0},
            {"ComponentProductID": "4c31b32e-d483-4c4f-9d9f-5b42dcc72313", "ProductCode": "RMC-400-NS", "Name": "CARD 400UM 510x253mm", "Quantity": 90.0, "WastagePercent": 0.0, "WastageQuantity": 0.0, "CostPercentage": 0.0},
            {"ComponentProductID": "557cebd8-2976-4c89-9acc-49f3baa1e514", "ProductCode": "RMT195", "Name": "T195 METAL TRAY", "Quantity": 180.0, "WastagePercent": 0.0, "WastageQuantity": 0.0, "CostPercentage": 0.0},
        ],
        "BillOfMaterialsServices": [
            {"ComponentProductID": "f1bb3d21-0f1b-4c8c-90d7-ab7c4b77a80e", "Name": "LABOUR - Gluing Room", "Quantity": 4.0, "ExpenseAccount": "222/00A", "PriceTier": 1},
            {"ComponentProductID": "1196e932-d2fd-4932-9761-7783fbc90183", "Name": "LABOUR - FACTORY", "Quantity": 1.0, "ExpenseAccount": "222/00A", "PriceTier": 1},
        ],
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
    def test_hits_the_confirmed_product_endpoint_with_include_bom(self, mock_get):
        # IncludeBOM=true is required (confirmed from Cin7's docs) --
        # without it BillOfMaterialsProducts comes back empty even for a
        # real assembly, see get_bom's docstring.
        mock_get.return_value = _mock_response(REAL_PRODUCT_RESPONSE_NO_BOM_LINES)
        with self.assertRaises(NotImplementedError):
            self.client.get_bom('WIPMT20T')
        args, kwargs = mock_get.call_args
        self.assertIn('/product', args[0])
        self.assertEqual(kwargs['params'], {'SKU': 'WIPMT20T', 'IncludeBOM': 'true'})

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
    def test_populated_line_maps_the_documented_field_names_correctly(self, mock_get):
        # Shape confirmed from Cin7's own published API docs (the "Bill
        # Of Material Product Model" -- ComponentProductID, ProductCode,
        # Quantity, WastagePercent/WastageQuantity, CostPercentage), not
        # a guess -- see get_bom's docstring.
        response = {
            "Total": 1, "Page": 1,
            "Products": [{
                "SKU": "FG-ASSEMBLED",
                "BillOfMaterial": True,
                "BillOfMaterialsProducts": [
                    {"ComponentProductID": "id-1", "ProductCode": "RAW-CARDBOARD", "Name": "Cardboard", "Quantity": 2.0},
                    {"ComponentProductID": "id-2", "ProductCode": "RAW-HINGE", "Name": "Hinge", "Quantity": 4.0},
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
    def test_real_live_bom_lines_for_wipmt20t_map_correctly(self, mock_get):
        # The actual confirmation: WIPMT20T is a genuine Shonrei assembly,
        # this is IncludeBOM=true's real response (captured via
        # scripts/dump_sample_bom.py on 2026-09-09), not the docs or a
        # synthetic fixture. Closes out the "not yet seen live" caveat
        # that used to sit on get_bom.
        mock_get.return_value = _mock_response(REAL_PRODUCT_RESPONSE_WITH_BOM_LINES)
        bom = self.client.get_bom('WIPMT20T')
        self.assertEqual(bom, [
            {'component_sku': 'RMFPE6BK', 'qty_per': 5.0},
            {'component_sku': 'RMAD1181', 'qty_per': 0.04},
            {'component_sku': 'RMC-400-NS', 'qty_per': 90.0},
            {'component_sku': 'RMT195', 'qty_per': 180.0},
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
