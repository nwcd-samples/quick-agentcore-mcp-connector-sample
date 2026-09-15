import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCENARIO = REPO / "gateway/Scenario3UserDelegation"
SHARED_ORDER = REPO / "gateway/QuickTwoToolDemo/order_requirements/lambda_function.py"
SHARED_INVENTORY = REPO / "gateway/QuickTwoToolDemo/inventory_availability/lambda_function.py"
TEMPLATE = REPO / "cloudformation/scenarios/scenario3-entra-obo.yaml"
TENANT_ID = "538f4efe-0a59-4988-98a0-2071f828fb70"
GATEWAY_APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OID = "11111111-1111-4111-8111-111111111111"
ORDERS_READER_ROLE = "OrdersReader"
INVENTORY_READER_ROLE = "InventoryReader"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORDER = _load("scenario3_order", SHARED_ORDER)
INVENTORY = _load("scenario3_inventory", SHARED_INVENTORY)


def _payload(response: dict) -> dict:
    return json.loads(response["body"])


def _claims(scope: str, roles: object | None = None, azp: str = GATEWAY_APP) -> dict:
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "tid": TENANT_ID,
        "oid": OID,
        "sub": "pairwise-subject",
        "azp": azp,
        "scp": scope,
    }
    if roles is not None:
        claims["roles"] = roles
    return claims


def _event(body: object, claims: dict | None, role: str) -> dict:
    return {
        "version": "2.0",
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "stageVariables": {
            "authorizationMode": "ENTRA_DELEGATED_USER_ROLE",
            "requiredUserRole": role,
            "expectedAuthorizedParty": GATEWAY_APP,
        },
        "requestContext": {
            "authorizer": {} if claims is None else {"jwt": {"claims": claims}}
        },
    }


class Scenario3StandardEntraOboTests(unittest.TestCase):
    def _order(self, roles: object = (ORDERS_READER_ROLE,)) -> dict:
        return ORDER.lambda_handler(
            _event(
                {"order_id": "ORDER-1001"},
                _claims("Orders.Read", roles),
                ORDERS_READER_ROLE,
            ),
            None,
        )

    def _inventory_body(self) -> dict:
        order = _payload(self._order())
        return {
            "order_id": order["order_id"],
            "requirements": order["requirements"],
            "requirements_checksum": order["requirements_checksum"],
        }

    def test_order_and_inventory_preserve_business_behavior(self):
        order_response = self._order()
        self.assertEqual(200, order_response["statusCode"])
        order = _payload(order_response)
        inventory_response = INVENTORY.lambda_handler(
            _event(
                self._inventory_body(),
                _claims("Inventory.Read", [INVENTORY_READER_ROLE]),
                INVENTORY_READER_ROLE,
            ),
            None,
        )
        self.assertEqual(200, inventory_response["statusCode"])
        inventory = _payload(inventory_response)
        self.assertEqual("SHORTAGE_RISK", inventory["risk"])
        self.assertEqual(2, sum(item["shortage_quantity"] for item in inventory["shortage_items"]))
        self.assertEqual(GATEWAY_APP, order["delegated_user_context"]["authorized_party"])
        self.assertEqual(GATEWAY_APP, inventory["trusted_caller_context"]["client_id"])

    def test_order_requires_reader_role_and_expected_obo_actor(self):
        for roles in (None, [], ["Support"]):
            with self.subTest(roles=roles):
                response = self._order(roles)
                self.assertEqual(403, response["statusCode"])
                self.assertEqual("ORDERS_READER_ROLE_REQUIRED", _payload(response)["error_code"])
                self.assertNotIn("requirements", _payload(response))

        wrong_actor = ORDER.lambda_handler(
            _event(
                {"order_id": "ORDER-1001"},
                _claims("Orders.Read", [ORDERS_READER_ROLE], azp="wrong-client"),
                ORDERS_READER_ROLE,
            ),
            None,
        )
        self.assertEqual(403, wrong_actor["statusCode"])
        self.assertEqual("ENTRA_CLIENT_NOT_ALLOWED", _payload(wrong_actor)["error_code"])

        missing_actor = _event(
            {"order_id": "ORDER-1001"},
            _claims("Orders.Read", [ORDERS_READER_ROLE]),
            ORDERS_READER_ROLE,
        )
        missing_actor["stageVariables"].pop("expectedAuthorizedParty")
        missing_actor_response = ORDER.lambda_handler(missing_actor, None)
        self.assertEqual(500, missing_actor_response["statusCode"])
        self.assertEqual(
            "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID",
            _payload(missing_actor_response)["error_code"],
        )

        missing_stage = _event(
            {"order_id": "ORDER-1001"},
            _claims("Orders.Read", [ORDERS_READER_ROLE]),
            ORDERS_READER_ROLE,
        )
        missing_stage.pop("stageVariables")
        self.assertEqual(500, ORDER.lambda_handler(missing_stage, None)["statusCode"])

    def test_inventory_requires_reader_role_and_expected_obo_actor(self):
        body = self._inventory_body()
        for roles in (None, [], [ORDERS_READER_ROLE]):
            with self.subTest(roles=roles):
                denied = INVENTORY.lambda_handler(
                    _event(body, _claims("Inventory.Read", roles), INVENTORY_READER_ROLE),
                    None,
                )
                self.assertEqual(403, denied["statusCode"])
                self.assertEqual("INVENTORY_READER_ROLE_REQUIRED", _payload(denied)["error_code"])

        wrong_actor = INVENTORY.lambda_handler(
            _event(
                body,
                _claims("Inventory.Read", [INVENTORY_READER_ROLE], azp="wrong-client"),
                INVENTORY_READER_ROLE,
            ),
            None,
        )
        self.assertEqual(403, wrong_actor["statusCode"])
        self.assertEqual("ENTRA_CLIENT_NOT_ALLOWED", _payload(wrong_actor)["error_code"])

        missing_actor = _event(
            body,
            _claims("Inventory.Read", [INVENTORY_READER_ROLE]),
            INVENTORY_READER_ROLE,
        )
        missing_actor["stageVariables"].pop("expectedAuthorizedParty")
        missing_actor_response = INVENTORY.lambda_handler(missing_actor, None)
        self.assertEqual(500, missing_actor_response["statusCode"])
        self.assertEqual(
            "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID",
            _payload(missing_actor_response)["error_code"],
        )

    def test_inventory_legacy_scope_only_compatibility_is_explicitly_preserved(self):
        body = self._inventory_body()
        legacy_http = _event(
            body,
            _claims("Inventory.Read", roles=[]),
            INVENTORY_READER_ROLE,
        )
        legacy_http.pop("stageVariables")
        response = INVENTORY.lambda_handler(legacy_http, None)
        self.assertEqual(200, response["statusCode"])
        self.assertEqual("JWT_HTTP", _payload(response)["trusted_caller_context"]["authorizer_type"])

        app_only_without_stage = _event(
            body,
            {"tid": TENANT_ID, "azp": GATEWAY_APP, "roles": [INVENTORY_READER_ROLE]},
            INVENTORY_READER_ROLE,
        )
        app_only_without_stage.pop("stageVariables")
        self.assertEqual(500, INVENTORY.lambda_handler(app_only_without_stage, None)["statusCode"])

        source = SHARED_INVENTORY.read_text()
        self.assertIn("Preserve legacy delegated HTTP integrations", source)

    def test_checksum_and_closed_input_contracts_are_preserved(self):
        body = self._inventory_body()
        body["requirements"][0]["required_quantity"] += 1
        mutated = INVENTORY.lambda_handler(
            _event(body, _claims("Inventory.Read", [INVENTORY_READER_ROLE]), INVENTORY_READER_ROLE),
            None,
        )
        self.assertEqual(400, mutated["statusCode"])
        self.assertEqual("REQUIREMENTS_CHECKSUM_MISMATCH", _payload(mutated)["error_code"])

    def test_cloudformation_encodes_identity_secret_and_schema_contracts(self):
        template = TEMPLATE.read_text()
        self.assertEqual(2, template.count("expectedAuthorizedParty: !Ref GatewayApiApplicationId"))
        self.assertIn("AuthorizationScopes:\n        - Orders.Read", template)
        self.assertIn("AuthorizationScopes:\n        - Inventory.Read", template)
        self.assertIn("GrantType: TOKEN_EXCHANGE", template)
        self.assertIn("AllowedValues:\n      - clientSecret", template)
        self.assertIn("DistinctEntraApplications", template)
        self.assertGreaterEqual(template.count("must differ"), 6)
        self.assertIn('"pattern":"^[0-9a-f]{64}$"', template)
        self.assertIn('"maxItems":50', template)
        self.assertIn('"maximum":1000000', template)
        self.assertNotIn("AWS::Lambda::Function", template)

    def test_cloudformation_is_the_only_scenario3_controller(self):
        for relative in (
            "deploy_scenario3.py",
            "verify_scenario3.py",
            "render_quick_entra_config.py",
            "config/entra.template.json",
            "config/gateway-targets.template.json",
            "config/architecture.json",
            "openapi/order-service.openapi.json",
            "openapi/inventory-service.openapi.json",
        ):
            self.assertFalse((SCENARIO / relative).exists(), relative)
        docs = (SCENARIO / "README.md").read_text() + (SCENARIO / "ENTRA_SETUP.md").read_text()
        self.assertIn("基础设施唯一控制器", docs)
        self.assertNotIn("deploy_scenario3.py", docs)
        self.assertNotIn("deployed-state.json", docs)
        self.assertNotIn("entra.local.json", docs)
        self.assertIn("AWS Secrets Manager", docs)


if __name__ == "__main__":
    unittest.main()
