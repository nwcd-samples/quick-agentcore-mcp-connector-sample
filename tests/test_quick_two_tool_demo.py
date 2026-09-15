from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[1] / "gateway" / "QuickTwoToolDemo"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORDER = _load("quick_demo_order", ROOT / "order_requirements" / "lambda_function.py")
INVENTORY = _load(
    "quick_demo_inventory", ROOT / "inventory_availability" / "lambda_function.py"
)


class QuickTwoToolDemoTests(unittest.TestCase):
    def _order(self, order_id: str):
        return ORDER.lambda_handler({"order_id": order_id}, None)

    def _inventory_from_order(self, order: dict):
        return INVENTORY.lambda_handler(
            {
                "order_id": order["order_id"],
                "requirements": order["requirements"],
                "requirements_checksum": order["requirements_checksum"],
            },
            None,
        )

    def test_order_1001_aggregates_duplicate_composite_key(self):
        result = self._order("ORDER-1001")
        self.assertEqual("SUCCESS", result["status"])
        self.assertTrue(result["read_only"])
        self.assertEqual(3, len(result["requirements"]))
        red = next(item for item in result["requirements"] if item["sku"] == "SKU-RED")
        self.assertEqual(
            {"sku": "SKU-RED", "warehouse_id": "WH-A", "required_quantity": 5},
            red,
        )
        self.assertRegex(result["requirements_checksum"], r"^[0-9a-f]{64}$")

    def test_order_not_found_stops_sequence(self):
        result = self._order("ORDER-9999")
        self.assertEqual("ORDER_NOT_FOUND", result["status"])
        self.assertEqual([], result["requirements"])
        self.assertNotIn("requirements_checksum", result)

    def test_order_rejects_extra_or_write_shaped_fields(self):
        result = ORDER.lambda_handler(
            {"order_id": "ORDER-1001", "action": "update"}, None
        )
        self.assertEqual("INVALID_INPUT", result["status"])
        self.assertTrue(result["read_only"])

    def test_shortage_result_is_deterministic(self):
        result = self._inventory_from_order(self._order("ORDER-1001"))
        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual("SHORTAGE_RISK", result["risk"])
        self.assertEqual(1, len(result["shortage_items"]))
        self.assertEqual(2, result["shortage_items"][0]["shortage_quantity"])
        self.assertTrue(result["read_only"])

    def test_fully_available_result(self):
        result = self._inventory_from_order(self._order("ORDER-1002"))
        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual("NO_SHORTAGE_FOUND", result["risk"])
        self.assertEqual([], result["shortage_items"])

    def test_zero_inventory_result(self):
        result = self._inventory_from_order(self._order("ORDER-1003"))
        self.assertEqual("SHORTAGE_RISK", result["risk"])
        self.assertEqual(6, result["shortage_items"][0]["shortage_quantity"])

    def test_partial_inventory_failure_never_claims_no_shortage(self):
        result = self._inventory_from_order(self._order("ORDER-1004"))
        self.assertEqual("PARTIAL", result["status"])
        self.assertEqual("UNABLE_TO_FULLY_ASSESS", result["risk"])
        self.assertEqual(1, len(result["unverified_items"]))
        self.assertNotEqual("NO_SHORTAGE_FOUND", result["risk"])

    def test_inventory_rejects_mutated_handoff(self):
        order = self._order("ORDER-1001")
        order["requirements"][0]["required_quantity"] += 1
        result = self._inventory_from_order(order)
        self.assertEqual("INVALID_INPUT", result["status"])
        self.assertEqual("REQUIREMENTS_CHECKSUM_MISMATCH", result["error_code"])

    def test_inventory_rejects_extra_or_write_shaped_fields(self):
        order = self._order("ORDER-1001")
        result = INVENTORY.lambda_handler(
            {
                "order_id": order["order_id"],
                "requirements": order["requirements"],
                "requirements_checksum": order["requirements_checksum"],
                "reserve_inventory": True,
            },
            None,
        )
        self.assertEqual("INVALID_INPUT", result["status"])

    def test_tool_schemas_are_closed_draft7_and_exactly_two_read_tools(self):
        schemas = []
        for relative in (
            Path("order_requirements/tool-schema.json"),
            Path("inventory_availability/tool-schema.json"),
        ):
            schema = json.loads((ROOT / relative).read_text())
            Draft7Validator.check_schema(schema["inputSchema"])
            self.assertFalse(schema["inputSchema"]["additionalProperties"])
            schemas.append(schema)
        self.assertEqual(
            {"get_order_requirements", "check_inventory_availability"},
            {schema["name"] for schema in schemas},
        )
        serialized = json.dumps(schemas).lower()
        for forbidden in ("create_order", "update_order", "reserve_inventory", "deduct"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
