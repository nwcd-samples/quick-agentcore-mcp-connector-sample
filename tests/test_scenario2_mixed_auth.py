import base64
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[1]
SCENARIO = REPO / "gateway/Scenario2MixedAuth"
TENANT_ID = "11111111-1111-4111-8111-111111111111"
INVENTORY_API_APP = "22222222-2222-4222-8222-222222222222"
INVENTORY_CLIENT_APP = "33333333-3333-4333-8333-333333333333"
INVENTORY_ROLE = "InventoryReader"
USERNAME = "legacy-order-reader"
PASSWORD = "unit-test-password-not-a-real-secret"
SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:111122223333:secret:scenario2-test"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASIC_ORDER = _load(
    "scenario2_basic_order",
    SCENARIO / "order_service/lambda_function.py",
)
INVENTORY = _load(
    "scenario2_shared_inventory",
    REPO / "gateway/QuickTwoToolDemo/inventory_availability/lambda_function.py",
)


def _payload(response: dict) -> dict:
    return json.loads(response["body"])


def _basic(username: str = USERNAME, password: str = PASSWORD) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _order_event(authorization: str | None) -> dict:
    return {
        "httpMethod": "POST",
        "headers": {} if authorization is None else {"Authorization": authorization},
        "body": '{"order_id":"ORDER-1001"}',
        "isBase64Encoded": False,
    }


def _inventory_event(body: dict, *, claims: dict | None, stage: dict | None) -> dict:
    event = {
        "version": "2.0",
        "headers": {"X-Tenant": "untrusted", "X-Roles": INVENTORY_ROLE},
        "requestContext": {
            "authorizer": {} if claims is None else {"jwt": {"claims": claims}}
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }
    if stage is not None:
        event["stageVariables"] = stage
    return event


class Scenario2MixedAuthTests(unittest.TestCase):
    def setUp(self):
        self.secret_client = MagicMock()
        self.secret_client.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {
                    "base64Credentials": base64.b64encode(
                        f"{USERNAME}:{PASSWORD}".encode()
                    ).decode()
                }
            ),
            "VersionId": "test-version",
        }
        BASIC_ORDER._CLIENT = self.secret_client
        BASIC_ORDER._CACHE = {"credential": None, "expires": 0.0}

    def tearDown(self):
        BASIC_ORDER._CLIENT = None
        BASIC_ORDER._CACHE = {"credential": None, "expires": 0.0}

    def _order(self, authorization: str | None) -> dict:
        with patch.dict(
            os.environ,
            {
                "ORDER_CREDENTIAL_SECRET_ARN": SECRET_ARN,
                "CREDENTIAL_CACHE_TTL_SECONDS": "0",
            },
            clear=True,
        ):
            return BASIC_ORDER.lambda_handler(_order_event(authorization), None)

    def test_basic_order_reads_shared_secret_and_rejects_invalid_credentials(self):
        success = self._order(_basic())
        self.assertEqual(200, success["statusCode"])
        self.assertEqual("SUCCESS", _payload(success)["status"])
        self.secret_client.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)
        for authorization in (None, "Bearer token", "Basic !!!", _basic(password="wrong")):
            with self.subTest(authorization=authorization):
                rejected = self._order(authorization)
                self.assertEqual(401, rejected["statusCode"])
                self.assertNotIn(PASSWORD, json.dumps(rejected))

    def test_shared_inventory_tool_enforces_entra_app_only_claim_contract(self):
        order = _payload(self._order(_basic()))
        body = {
            "order_id": order["order_id"],
            "requirements": order["requirements"],
            "requirements_checksum": order["requirements_checksum"],
        }
        claims = {
            "tid": TENANT_ID,
            "azp": INVENTORY_CLIENT_APP,
            "roles": [INVENTORY_ROLE],
        }
        stage = {
            "authorizationMode": "ENTRA_APP_ONLY",
            "expectedTenantId": TENANT_ID,
            "expectedClientId": INVENTORY_CLIENT_APP,
            "requiredAppRole": INVENTORY_ROLE,
        }
        allowed = INVENTORY.lambda_handler(
            _inventory_event(body, claims=claims, stage=stage), None
        )
        self.assertEqual(200, allowed["statusCode"])
        result = _payload(allowed)
        self.assertEqual("SHORTAGE_RISK", result["risk"])
        self.assertEqual(INVENTORY_CLIENT_APP, result["trusted_caller_context"]["client_id"])
        self.assertEqual([INVENTORY_ROLE], result["trusted_caller_context"]["roles"])

        cases = (
            (None, stage, 401),
            (claims, None, 500),
            ({**claims, "tid": "wrong"}, stage, 403),
            ({**claims, "azp": "wrong"}, stage, 403),
            ({**claims, "roles": []}, stage, 403),
        )
        for candidate_claims, candidate_stage, expected in cases:
            with self.subTest(status=expected, claims=candidate_claims):
                response = INVENTORY.lambda_handler(
                    _inventory_event(
                        body,
                        claims=candidate_claims,
                        stage=candidate_stage,
                    ),
                    None,
                )
                self.assertEqual(expected, response["statusCode"])

    def test_basic_secret_schema_rejects_extra_fields(self):
        encoded = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        self.secret_client.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {"base64Credentials": encoded, "unexpected": "not-allowed"}
            )
        }
        response = self._order(_basic())
        self.assertEqual(500, response["statusCode"])
        self.assertEqual("AUTH_CONFIGURATION_INVALID", _payload(response)["error_code"])

    def test_cloudformation_is_the_only_scenario2_infrastructure_controller(self):
        root = (REPO / "cloudformation/scenarios/scenario2-mixed-auth.yaml").read_text()
        module = (REPO / "cloudformation/modules/mixed-auth-rest-apis.yaml").read_text()
        self.assertIn("AWS::ApiGatewayV2::Authorizer", module)
        self.assertIn("AuthorizerType: JWT", module)
        self.assertIn("authorizationMode: ENTRA_APP_ONLY", module)
        self.assertIn("expectedClientId: !Ref InventoryClientApplicationId", module)
        inventory_route = module[
            module.index("  InventoryRoute:") : module.index("  InventoryStage:")
        ]
        self.assertNotIn("AuthorizationScopes", inventory_route)
        self.assertIn("api://${InventoryApiApplicationId}/.default", root)
        self.assertIn("ClientSecretSource: EXTERNAL", root)
        self.assertIn("DistinctInventoryApplications", root)
        self.assertEqual(2, root.count("AllowedValues:\n      - clientSecret") + root.count("AllowedValues:\n      - base64Credentials"))
        self.assertIn("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}", root)
        self.assertNotIn("AWS::Lambda::Function", root)

    def test_obsolete_imperative_scenario2_files_are_removed(self):
        for relative in (
            "deploy_scenario2.py",
            "verify_scenario2.py",
            "inventory_service/lambda_function.py",
            "config/gateway-targets.template.json",
            "openapi/order-service.openapi.json",
            "openapi/inventory-service.openapi.json",
        ):
            self.assertFalse((SCENARIO / relative).exists(), relative)
        readme = (SCENARIO / "README.md").read_text()
        self.assertIn("基础设施唯一实现", readme)
        self.assertIn("business-lambdas/deploy.py", readme)
        self.assertNotIn("python3 gateway/Scenario2MixedAuth/deploy_scenario2.py", readme)

    def test_scenario2_uses_the_bootstrap_basic_secret_for_both_consumers(self):
        readme = (SCENARIO / "README.md").read_text()
        blog = (REPO / "docs/Quick_Connector_AgentCore_Enterprise_Apps_Security_Blog.md").read_text()
        example = (REPO / "cloudformation/parameters/scenario2.example.json").read_text()
        self.assertIn("必须使用bootstrap输出的同一个ARN", readme)
        self.assertNotIn("quick-agentcore-s2-order-basic-auth", blog)
        self.assertIn("quick-agentcore/order-basic-auth-tool-EXAMPLE", example)

    def test_business_bootstrap_owns_the_basic_order_lambda_source(self):
        bootstrap = (REPO / "business-lambdas/deploy.py").read_text()
        self.assertIn(
            'ROOT / "gateway/Scenario2MixedAuth/order_service/lambda_function.py"',
            bootstrap,
        )
        self.assertIn('"order-basic-auth-tool"', bootstrap)


if __name__ == "__main__":
    unittest.main()
