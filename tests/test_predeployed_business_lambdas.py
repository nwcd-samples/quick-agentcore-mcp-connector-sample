from __future__ import annotations

import base64
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORDER = _load(
    "predeployed_order_tool",
    "gateway/QuickTwoToolDemo/order_requirements/lambda_function.py",
)
INVENTORY = _load(
    "predeployed_inventory_tool",
    "gateway/QuickTwoToolDemo/inventory_availability/lambda_function.py",
)
BASIC = _load(
    "predeployed_basic_order_tool",
    "gateway/Scenario2MixedAuth/order_service/lambda_function.py",
)

def _payload(response: dict) -> dict:
    return json.loads(response["body"])


class PredeployedBusinessLambdaTests(unittest.TestCase):
    def test_native_order_inventory_chain(self):
        order = ORDER.lambda_handler({"order_id": "ORDER-1001"}, None)
        self.assertEqual(order["status"], "SUCCESS")
        inventory = INVENTORY.lambda_handler(
            {
                "order_id": order["order_id"],
                "requirements": order["requirements"],
                "requirements_checksum": order["requirements_checksum"],
            },
            None,
        )
        self.assertEqual(inventory["status"], "SUCCESS")
        self.assertEqual(inventory["risk"], "SHORTAGE_RISK")
        self.assertEqual(
            sum(item["shortage_quantity"] for item in inventory["shortage_items"]),
            2,
        )

    def test_order_http_api_requires_stage_contract_and_orders_reader(self):
        missing = ORDER.lambda_handler(
            {"version": "2.0", "body": '{"order_id":"ORDER-1001"}'}, None
        )
        self.assertEqual(missing["statusCode"], 401)

        event = {
            "version": "2.0",
            "requestContext": {"authorizer": {"jwt": {"claims": {
                "tid": "tenant", "oid": "user", "sub": "subject",
                "azp": "gateway-client", "scp": "Orders.Read",
                "roles": "[OrdersReader]",
            }}}},
            "body": '{"order_id":"ORDER-1001"}',
            "isBase64Encoded": False,
        }
        unconfigured = ORDER.lambda_handler(event, None)
        self.assertEqual(unconfigured["statusCode"], 500)
        self.assertEqual(
            _payload(unconfigured)["error_code"],
            "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID",
        )

        event["stageVariables"] = {
            "authorizationMode": "ENTRA_DELEGATED_USER_ROLE",
            "requiredUserRole": "OrdersReader",
            "expectedAuthorizedParty": "gateway-client",
        }
        response = ORDER.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        payload = _payload(response)
        self.assertEqual(payload["delegated_user_context"]["user_object_id"], "user")
        self.assertEqual(payload["delegated_user_context"]["scopes"], ["Orders.Read"])
        self.assertEqual(payload["delegated_user_context"]["roles"], ["OrdersReader"])

    def test_inventory_accepts_rest_and_http_authorizer_contexts(self):
        order = ORDER.lambda_handler({"order_id": "ORDER-1001"}, None)
        request = {
            "order_id": order["order_id"],
            "requirements": order["requirements"],
            "requirements_checksum": order["requirements_checksum"],
        }
        body = json.dumps(request)

        rest = INVENTORY.lambda_handler(
            {
                "httpMethod": "POST",
                "requestContext": {
                    "authorizer": {
                        "claims": {
                            "client_id": "inventory-client",
                            "scope": "inventory/read",
                        }
                    }
                },
                "body": body,
                "isBase64Encoded": False,
            },
            None,
        )
        self.assertEqual(rest["statusCode"], 200)
        self.assertEqual(
            _payload(rest)["trusted_caller_context"]["authorizer_type"],
            "COGNITO_REST",
        )

        http = INVENTORY.lambda_handler(
            {
                "version": "2.0",
                "requestContext": {
                    "authorizer": {
                        "jwt": {
                            "claims": {
                                "tid": "tenant",
                                "oid": "user",
                                "azp": "gateway-client",
                                "scp": "Inventory.Read",
                            }
                        }
                    }
                },
                "body": body,
                "isBase64Encoded": False,
            },
            None,
        )
        self.assertEqual(http["statusCode"], 200)
        self.assertEqual(
            _payload(http)["trusted_caller_context"]["authorizer_type"],
            "JWT_HTTP",
        )

        delegated_role_event = {
            "version": "2.0",
            "stageVariables": {
                "authorizationMode": "ENTRA_DELEGATED_USER_ROLE",
                "requiredUserRole": "InventoryReader",
                "expectedAuthorizedParty": "gateway-client",
            },
            "requestContext": {"authorizer": {"jwt": {"claims": {
                "tid": "tenant", "oid": "inventory-reader-user",
                "sub": "inventory-reader-subject", "azp": "gateway-client",
                "scp": "Inventory.Read", "roles": "[InventoryReader]",
            }}}},
            "body": body,
            "isBase64Encoded": False,
        }
        delegated_allowed = INVENTORY.lambda_handler(delegated_role_event, None)
        self.assertEqual(delegated_allowed["statusCode"], 200)
        self.assertEqual(
            _payload(delegated_allowed)["trusted_caller_context"]["roles"],
            ["InventoryReader"],
        )
        delegated_role_event["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = []
        delegated_denied = INVENTORY.lambda_handler(delegated_role_event, None)
        self.assertEqual(delegated_denied["statusCode"], 403)
        self.assertEqual(
            _payload(delegated_denied)["error_code"],
            "INVENTORY_READER_ROLE_REQUIRED",
        )

        app_only_event = {
            "version": "2.0",
            "stageVariables": {
                "authorizationMode": "ENTRA_APP_ONLY",
                "expectedTenantId": "tenant",
                "expectedClientId": "agentcore-inventory-client",
                "requiredAppRole": "InventoryReader",
            },
            "requestContext": {
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "tid": "tenant",
                            "azp": "agentcore-inventory-client",
                            "roles": "[InventoryReader]",
                        }
                    }
                }
            },
            "body": body,
            "isBase64Encoded": False,
        }
        app_only = INVENTORY.lambda_handler(app_only_event, None)
        self.assertEqual(app_only["statusCode"], 200)
        self.assertEqual(
            _payload(app_only)["trusted_caller_context"]["roles"],
            ["InventoryReader"],
        )
        app_only_event["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = []
        denied = INVENTORY.lambda_handler(app_only_event, None)
        self.assertEqual(denied["statusCode"], 403)
        self.assertEqual(_payload(denied)["error_code"], "INVENTORY_READER_ROLE_REQUIRED")

        app_only_event["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = ["InventoryReader"]
        app_only_event.pop("stageVariables")
        unconfigured = INVENTORY.lambda_handler(app_only_event, None)
        self.assertEqual(unconfigured["statusCode"], 500)
        self.assertEqual(
            _payload(unconfigured)["error_code"],
            "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID",
        )
        app_only_event["stageVariables"] = {"authorizationMode": "TYPO"}
        mistyped = INVENTORY.lambda_handler(app_only_event, None)
        self.assertEqual(mistyped["statusCode"], 500)

        app_only_event["stageVariables"] = {
            "authorizationMode": "ENTRA_APP_ONLY",
            "expectedTenantId": "tenant",
            "expectedClientId": "agentcore-inventory-client",
            "requiredAppRole": "Inventory.Write",
        }
        wrong_role_config = INVENTORY.lambda_handler(app_only_event, None)
        self.assertEqual(wrong_role_config["statusCode"], 500)
        self.assertEqual(
            _payload(wrong_role_config)["error_code"],
            "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID",
        )

        missing = INVENTORY.lambda_handler(
            {"version": "2.0", "body": body}, None
        )
        self.assertEqual(missing["statusCode"], 401)

    def test_basic_function_supports_shared_base64_secret_shape(self):
        encoded = base64.b64encode(b"service-user:test-password").decode("ascii")
        original = BASIC._credential
        try:
            BASIC._credential = lambda: {"base64Credentials": encoded}
            event = {
                "headers": {"Authorization": f"Basic {encoded}"},
                "body": '{"order_id":"ORDER-1001"}',
                "isBase64Encoded": False,
            }
            success = BASIC.lambda_handler(event, None)
            self.assertEqual(success["statusCode"], 200)
            self.assertEqual(_payload(success)["status"], "SUCCESS")

            event["headers"]["Authorization"] = "Basic Zm9vOmJhcg=="
            rejected = BASIC.lambda_handler(event, None)
            self.assertEqual(rejected["statusCode"], 401)
        finally:
            BASIC._credential = original

    def test_orders_reader_role_order_function_fails_closed(self):
        event = {
            "version": "2.0",
            "stageVariables": {
                "authorizationMode": "ENTRA_DELEGATED_USER_ROLE",
                "requiredUserRole": "OrdersReader",
                "expectedAuthorizedParty": "gateway-client",
            },
            "requestContext": {"authorizer": {"jwt": {"claims": {
                "tid": "tenant", "oid": "orders-reader-user", "sub": "orders-reader-subject",
                "azp": "gateway-client", "scp": "Orders.Read", "roles": "[OrdersReader]",
            }}}},
            "body": '{"order_id":"ORDER-1001"}',
            "isBase64Encoded": False,
        }
        allowed = ORDER.lambda_handler(event, None)
        self.assertEqual(allowed["statusCode"], 200)
        self.assertEqual(_payload(allowed)["delegated_user_context"]["roles"], ["OrdersReader"])
        event["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = []
        denied = ORDER.lambda_handler(event, None)
        self.assertEqual(denied["statusCode"], 403)
        self.assertEqual(_payload(denied)["error_code"], "ORDERS_READER_ROLE_REQUIRED")


    def test_bootstrap_discovers_account_partition_and_region(self):
        source = (ROOT / "business-lambdas/deploy.py").read_text()
        self.assertIn('caller = session.client("sts").get_caller_identity()', source)
        self.assertIn('partition = arn_parts[1]', source)
        self.assertIn("session.region_name", source)
        self.assertIn('quick-agentcore-{region}-{function_name}-role', source)
        self.assertIn('arn:{partition}:logs:{region}:{account}', source)
        self.assertIn('set(payload) == {"base64Credentials"}', source)
        self.assertIn('"authorizationMode": "ENTRA_APP_ONLY"', source)
        self.assertIn('"wrongAppOnlyClientInventoryStatusCode"', source)
        self.assertNotIn("EXPECTED_ACCOUNT", source)
        self.assertNotIn("DEFAULT_REGION", source)
        self.assertNotIn("271" + "547278201", source)


if __name__ == "__main__":
    unittest.main()
