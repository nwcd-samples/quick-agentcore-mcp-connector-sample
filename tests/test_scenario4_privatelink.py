import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "gateway/Scenario4PrivateLink"
SCENARIO = ROOT / "cloudformation/scenarios/scenario4-private-link.yaml"
NETWORK = ROOT / "cloudformation/modules/private-network.yaml"
SERVICES = ROOT / "cloudformation/modules/scenario4-vpc-services.yaml"
GATEWAY = ROOT / "cloudformation/modules/scenario4-private-native-lambda-gateway.yaml"
GATEWAY_ENDPOINT = ROOT / "cloudformation/modules/scenario4-gateway-endpoint.yaml"
SHARED_IDENTITY = ROOT / "cloudformation/modules/cognito-m2m.yaml"
SHARED_GATEWAY = ROOT / "cloudformation/modules/native-lambda-gateway.yaml"


class Scenario4PrivateLinkTests(unittest.TestCase):
    def test_cloudformation_is_the_only_scenario4_controller(self):
        for name in ("deploy_scenario4.py", "create_quick_connector.py", "verify_scenario4.py"):
            self.assertFalse((SCENARIO_DIR / name).exists(), name)
        readme = (SCENARIO_DIR / "README.md").read_text()
        self.assertIn("cloudformation/scenarios/scenario4-private-link.yaml", readme)

    def test_scenario4_owns_two_dedicated_vpc_business_lambdas(self):
        root = SCENARIO.read_text()
        services = SERVICES.read_text()
        self.assertNotIn("OrderToolLambdaArn:", root)
        self.assertNotIn("InventoryToolLambdaArn:", root)
        self.assertIn("../modules/scenario4-vpc-services.yaml", root)
        self.assertEqual(2, services.count("AWS::Serverless::Function"))
        self.assertEqual(2, services.count("VpcConfig:"))
        self.assertIn("../../gateway/QuickTwoToolDemo/order_requirements/", services)
        self.assertIn("../../gateway/QuickTwoToolDemo/inventory_availability/", services)
        self.assertIn("BusinessLambdaSecurityGroupId", services)

    def test_gateway_resource_path_uses_privatelink(self):
        network = NETWORK.read_text()
        endpoint = GATEWAY_ENDPOINT.read_text()
        root = SCENARIO.read_text()
        self.assertIn("AWS::QuickSight::VPCConnection", network)
        self.assertIn("QuickToGatewayHttps", network)
        self.assertIn("ResolverInboundEndpoint", network)
        self.assertIn("com.amazonaws.${AWS::Region}.bedrock-agentcore.gateway", endpoint)
        self.assertIn("Action: bedrock-agentcore:InvokeGateway", endpoint)
        self.assertIn("Resource: !Ref GatewayArn", endpoint)
        self.assertNotIn("AWS::BedrockAgentCore::ResourcePolicy", endpoint)
        self.assertIn("QuickVpcConnectionArn:", root)
        self.assertIn("QuickVpcConnectionId:", root)
        self.assertIn("QuickVpcConnectionId: !Ref QuickVpcConnectionId", root)
        self.assertIn("QuickVpcConnectionId:", network)
        self.assertIn("VPCConnectionId: !Ref QuickVpcConnectionId", network)
        self.assertIn("Name: !Ref QuickVpcConnectionId", network)
        self.assertIn("QuickVpcConnectionId", (ROOT / "cloudformation/parameters/scenario4.example.json").read_text())
        self.assertIn("DELETED`墓碑", (SCENARIO_DIR / "README.md").read_text())

    def test_cognito_auth_endpoint_remains_public_by_design(self):
        root = SCENARIO.read_text()
        network = NETWORK.read_text()
        readme = (SCENARIO_DIR / "README.md").read_text()
        self.assertIn("../modules/cognito-m2m.yaml", root)
        self.assertIn("CognitoDomainPrefix:", root)
        self.assertIn("InboundIdentity.Outputs.TokenEndpoint", root)
        self.assertNotIn("AuthVpcConnectionArn:", root)
        self.assertNotIn("com.amazonaws.${AWS::Region}.execute-api", network)
        self.assertNotIn("com.amazonaws.${AWS::Region}.cognito-idp", network)
        self.assertNotIn("GetClientToken", root + network + services_text())
        self.assertIn("不是Quick的限制", readme)
        self.assertIn("Auth-server VPC connection | **Public network**", readme)

    def test_network_has_no_nat_and_business_lambdas_have_no_egress(self):
        network = NETWORK.read_text()
        self.assertNotIn("AWS::EC2::NatGateway", network)
        self.assertNotIn("AWS::EC2::InternetGateway", network)
        self.assertIn("BusinessLambdaSecurityGroup", network)
        self.assertIn("SecurityGroupEgress: []", network)
        self.assertNotIn("AuthLambdaSecurityGroup", network)
        self.assertNotIn("ExecuteApiVpcEndpoint", network)
        self.assertNotIn("CognitoVpcEndpoint", network)

    def test_scenario4_isolated_modules_do_not_change_other_scenarios(self):
        scenario1 = (ROOT / "cloudformation/scenarios/scenario1-native-lambda.yaml").read_text()
        scenario2 = (ROOT / "cloudformation/scenarios/scenario2-mixed-auth.yaml").read_text()
        scenario3 = (ROOT / "cloudformation/scenarios/scenario3-entra-obo.yaml").read_text()
        self.assertIn("../modules/cognito-m2m.yaml", scenario1)
        self.assertIn("../modules/native-lambda-gateway.yaml", scenario1)
        self.assertIn("InventoryToolLambdaArn", scenario2)
        self.assertIn("OrderToolLambdaArn", scenario3)
        self.assertIn("InventoryToolLambdaArn", scenario3)
        self.assertNotIn("scenario4-", SHARED_IDENTITY.read_text())
        self.assertNotIn("scenario4-", SHARED_GATEWAY.read_text())

    def test_account_region_and_partition_are_pseudo_parameters(self):
        rendered = "\n".join(
            path.read_text()
            for path in (SCENARIO, NETWORK, SERVICES, GATEWAY, GATEWAY_ENDPOINT)
        )
        self.assertIn("AWS::AccountId", rendered)
        self.assertIn("AWS::Region", rendered)
        self.assertIn("AWS::Partition", rendered)
        self.assertNotIn("271" + "547278201", rendered)


def services_text() -> str:
    return SERVICES.read_text()


if __name__ == "__main__":
    unittest.main()
