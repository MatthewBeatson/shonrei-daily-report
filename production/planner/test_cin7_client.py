"""Unit tests for the confirmed Cin7Client methods, against the exact
response shapes captured via scripts/dump_sample_bom.py on a live
account (see cin7_client.py's module docstring and production/README.md)
-- requests.get is mocked, no live Cin7 access needed to run these.
"""
import unittest
from unittest.mock import MagicMock, patch

from cin7_client import Assembly, Cin7Client

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


# -- write-side tests -- shapes from Cin7's own documented "Finished
# Goods" / "Stock Adjustment" endpoints (production/README.md "Confirmed
# Cin7 writes"), not yet proven against a live tenant -- see
# scripts/dump_sample_assembly_write.py.

PRODUCT_FOR_CREATE = {
    "Total": 1, "Page": 1,
    "Products": [{"ID": "product-guid-1", "SKU": "FG-ASSEMBLED", "Name": "Assembled thing", "DefaultLocation": "Main Warehouse"}],
}

CREATE_RESPONSE = {
    "AssemblyNumber": "FG-00016", "TaskID": "task-1", "Status": "DRAFT",
    "ProductCode": "FG-ASSEMBLED", "ProductID": "product-guid-1", "Quantity": 5.0,
    "OrderLines": [], "PickLines": [], "Transactions": [], "Errors": [],
}

ORDER_LINES_RESPONSE = {
    "TaskID": "task-1", "Status": "DRAFT",
    "OrderLines": [{"ProductCode": "RAW-CARDBOARD", "ProductID": "raw-guid", "Name": "Cardboard", "Quantity": 2.0, "TotalQuantity": 10.0}],
}

AUTHORISE_RESPONSE = {"TaskID": "task-1", "Status": "AUTHORISED", "OrderLines": ORDER_LINES_RESPONSE["OrderLines"]}

FULL_ASSEMBLY_AUTHORISED = {
    "TaskID": "task-1", "ID": "record-guid-1", "Status": "AUTHORISED",
    "ProductCode": "FG-ASSEMBLED", "ProductID": "product-guid-1", "Quantity": 5.0,
    "Location": "Main Warehouse", "LocationID": "loc-guid",
}

PICK_LINES_RESPONSE = {
    "TaskID": "task-1", "Status": "AUTHORISED",
    "PickLines": [{"ProductCode": "RAW-CARDBOARD", "ProductID": "raw-guid", "Name": "Cardboard", "Quantity": 10.0, "Unit": "Item"}],
}

COMPLETE_RESPONSE = {"TaskID": "task-1", "Status": "COMPLETED", "PickLines": PICK_LINES_RESPONSE["PickLines"]}

FULL_ASSEMBLY_COMPLETED = dict(FULL_ASSEMBLY_AUTHORISED, Status="COMPLETED")


def _mock_write_response(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


class CreateAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_posts_the_documented_finishedgoods_shape(self, mock_get, mock_post):
        mock_get.return_value = _mock_write_response(PRODUCT_FOR_CREATE)
        mock_post.return_value = _mock_write_response(CREATE_RESPONSE)

        assembly = self.client.create_assembly('FG-ASSEMBLED', 5.0)

        args, kwargs = mock_post.call_args
        self.assertIn('/finishedGoods', args[0])
        self.assertEqual(kwargs['json'], {
            'ProductID': 'product-guid-1', 'ProductCode': 'FG-ASSEMBLED',
            'Quantity': 5.0, 'Location': 'Main Warehouse', 'Status': 'DRAFT',
            # Confirmed required by a live 400 against WIP110, 2026-09-11 --
            # Shonrei's own tenant account codes, see cin7_client.py's module docstring.
            'Account': '720', 'WIPAccount': '721B',
        })
        self.assertEqual(assembly, Assembly(assembly_id='task-1', sku='FG-ASSEMBLED', status='DRAFT', qty=5.0))

    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_errors_array_on_a_200_response_raises(self, mock_get, mock_post):
        # Cin7 doesn't always use HTTP error statuses for a rejected write
        # -- a populated Errors array on an HTTP 200 must not be read as success.
        mock_get.return_value = _mock_write_response(PRODUCT_FOR_CREATE)
        mock_post.return_value = _mock_write_response({**CREATE_RESPONSE, 'Errors': ['Quantity must be > 0']})
        with self.assertRaises(RuntimeError):
            self.client.create_assembly('FG-ASSEMBLED', 5.0)


class AuthoriseAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_reads_order_lines_then_posts_status_authorised(self, mock_get, mock_post):
        mock_get.side_effect = [_mock_write_response(ORDER_LINES_RESPONSE), _mock_write_response(FULL_ASSEMBLY_AUTHORISED)]
        mock_post.return_value = _mock_write_response(AUTHORISE_RESPONSE)

        assembly = self.client.authorise_assembly('task-1')

        get_args, get_kwargs = mock_get.call_args_list[0]
        self.assertIn('/finishedGoods/order', get_args[0])
        self.assertEqual(get_kwargs['params'], {'TaskID': 'task-1'})
        post_args, post_kwargs = mock_post.call_args
        self.assertIn('/finishedGoods/order', post_args[0])
        self.assertEqual(post_kwargs['json'], {
            'TaskID': 'task-1', 'Status': 'AUTHORISED', 'OrderLines': ORDER_LINES_RESPONSE['OrderLines'],
        })
        self.assertEqual(assembly.status, 'AUTHORISED')


class CompleteAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_reads_pick_lines_then_posts_status_completed(self, mock_get, mock_post):
        mock_get.side_effect = [_mock_write_response(FULL_ASSEMBLY_AUTHORISED), _mock_write_response(PICK_LINES_RESPONSE), _mock_write_response(FULL_ASSEMBLY_COMPLETED)]
        mock_post.return_value = _mock_write_response(COMPLETE_RESPONSE)

        assembly = self.client.complete_assembly('task-1', 5.0)

        post_args, post_kwargs = mock_post.call_args
        self.assertIn('/finishedGoods/pick', post_args[0])
        sent = dict(post_kwargs['json'])
        completion_date, wip_date = sent.pop('CompletionDate'), sent.pop('WIPDate')
        self.assertEqual(completion_date, wip_date)  # both stamped "now" together
        self.assertEqual(sent, {
            'TaskID': 'task-1', 'Status': 'COMPLETED', 'PickLines': PICK_LINES_RESPONSE['PickLines'],
            # Same Account/WIPAccount as Create -- Cin7's docs show the
            # Complete call carrying them too, see complete_assembly's docstring.
            'Account': '720', 'WIPAccount': '721B',
        })
        self.assertEqual(assembly.status, 'COMPLETED')

    @patch('cin7_client.requests.get')
    def test_qty_mismatch_against_the_assemblys_own_quantity_raises(self, mock_get):
        # Cin7's documented pick/complete request has no field to change
        # the finished-good quantity at this stage -- a mismatch must
        # never be silently completed against the wrong quantity.
        mock_get.return_value = _mock_write_response(FULL_ASSEMBLY_AUTHORISED)  # Quantity: 5.0
        with self.assertRaises(NotImplementedError):
            self.client.complete_assembly('task-1', 999.0)


class CloseAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.client = Cin7Client(account_id='x', api_key='y')

    @patch('cin7_client.requests.delete')
    def test_deletes_with_void_true(self, mock_delete):
        mock_delete.return_value = _mock_write_response({'TaskID': 'task-1', 'Status': 'VOIDED'})
        self.client.close_assembly('task-1')
        args, kwargs = mock_delete.call_args
        self.assertIn('/finishedGoods', args[0])
        self.assertEqual(kwargs['params'], {'ID': 'task-1', 'Void': 'true'})


class AllocateAssemblyTests(unittest.TestCase):
    def test_raises_rather_than_guess_the_status_value(self):
        # Cin7's docs don't name a "picked/allocated but not completed"
        # status -- see allocate_assembly's docstring.
        client = Cin7Client(account_id='x', api_key='y')
        with self.assertRaises(NotImplementedError):
            client.allocate_assembly('task-1')


class CreateAuthorisedAssemblyTests(unittest.TestCase):
    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_composes_create_then_authorise_never_allocate_or_complete(self, mock_get, mock_post):
        mock_get.side_effect = [
            _mock_write_response(PRODUCT_FOR_CREATE),         # create_assembly's product lookup
            _mock_write_response(ORDER_LINES_RESPONSE),        # authorise_assembly's order fetch
            _mock_write_response(FULL_ASSEMBLY_AUTHORISED),    # authorise_assembly's post-authorise refetch
        ]
        mock_post.side_effect = [_mock_write_response(CREATE_RESPONSE), _mock_write_response(AUTHORISE_RESPONSE)]

        client = Cin7Client(account_id='x', api_key='y')
        assembly = client.create_authorised_assembly('FG-ASSEMBLED', 5.0)

        self.assertEqual(mock_post.call_count, 2)  # create + authorise only
        self.assertEqual(assembly.status, 'AUTHORISED')


class CompleteSmallAssemblyTests(unittest.TestCase):
    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_composes_create_authorise_complete_at_the_actual_qty(self, mock_get, mock_post):
        mock_get.side_effect = [
            _mock_write_response(PRODUCT_FOR_CREATE),          # create_assembly's product lookup
            _mock_write_response(ORDER_LINES_RESPONSE),         # authorise_assembly's order fetch
            _mock_write_response(FULL_ASSEMBLY_AUTHORISED),     # authorise_assembly's post-authorise refetch
            _mock_write_response(FULL_ASSEMBLY_AUTHORISED),     # complete_assembly's qty check (5.0 == 5.0)
            _mock_write_response(PICK_LINES_RESPONSE),          # complete_assembly's pick fetch
            _mock_write_response(FULL_ASSEMBLY_COMPLETED),      # complete_assembly's post-complete refetch
        ]
        mock_post.side_effect = [
            _mock_write_response(CREATE_RESPONSE),
            _mock_write_response(AUTHORISE_RESPONSE),
            _mock_write_response(COMPLETE_RESPONSE),
        ]

        client = Cin7Client(account_id='x', api_key='y')
        assembly = client.complete_small_assembly('FG-ASSEMBLED', 5.0)

        self.assertEqual(mock_post.call_count, 3)  # create + authorise + complete, no allocate
        self.assertEqual(assembly.status, 'COMPLETED')


class GetOpenAssembliesTests(unittest.TestCase):
    @patch('cin7_client.requests.get')
    def test_excludes_completed_and_voided_and_filters_to_exact_sku(self, mock_get):
        mock_get.return_value = _mock_write_response({
            "Page": 1, "Total": 3,
            "FinishedGoods": [
                {"ProductCode": "FG-ASSEMBLED", "Status": "AUTHORISED", "Quantity": 5.0},
                {"ProductCode": "FG-ASSEMBLED", "Status": "COMPLETED", "Quantity": 3.0},
                {"ProductCode": "FG-OTHER-MATCHING-SEARCH", "Status": "DRAFT", "Quantity": 7.0},
            ],
        })
        client = Cin7Client(account_id='x', api_key='y')
        result = client.get_open_assemblies(['FG-ASSEMBLED'])
        self.assertEqual(result, {'FG-ASSEMBLED': 5.0})


class AdjustStockOnHandTests(unittest.TestCase):
    @patch('cin7_client.requests.put')
    @patch('cin7_client.requests.post')
    @patch('cin7_client.requests.get')
    def test_two_step_draft_then_completed_with_target_qty_not_a_delta(self, mock_get, mock_post, mock_put):
        mock_get.return_value = _mock_write_response(PRODUCT_FOR_CREATE)
        draft_response = {
            "TaskID": "adj-task-1", "Status": "DRAFT", "EffectiveDate": "2026-09-09T00:00:00",
            "Lines": [{"SKU": "FG-ASSEMBLED", "ProductID": "product-guid-1", "Quantity": 600.0}],
        }
        mock_post.return_value = _mock_write_response(draft_response)
        mock_put.return_value = _mock_write_response({**draft_response, "Status": "COMPLETED"})

        client = Cin7Client(account_id='x', api_key='y')
        task_id = client.adjust_stock_on_hand('FG-ASSEMBLED', 600.0, note='stocktake variance')

        self.assertEqual(task_id, 'adj-task-1')
        post_args, post_kwargs = mock_post.call_args
        self.assertIn('/stockadjustment', post_args[0])
        self.assertEqual(post_kwargs['json']['Status'], 'DRAFT')
        self.assertEqual(post_kwargs['json']['Lines'][0]['Quantity'], 600.0)  # target level, not a delta
        put_args, put_kwargs = mock_put.call_args
        self.assertIn('/stockadjustment', put_args[0])
        self.assertEqual(put_kwargs['json']['TaskID'], 'adj-task-1')
        self.assertEqual(put_kwargs['json']['Status'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()
