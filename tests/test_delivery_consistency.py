import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
BLOG = ROOT / "docs/Quick_Connector_AgentCore_Enterprise_Apps_Security_Blog.md"
CFN = ROOT / "cloudformation"


class DeliveryConsistencyTests(unittest.TestCase):
    def test_blog_and_cloudformation_expose_exactly_four_current_scenarios(self):
        blog = BLOG.read_text()
        for heading in (
            "## 场景一：使用 Cognito 服务身份调用 Lambda 工具",
            "## 场景二：同时接入 Basic 认证与 Entra ID 应用身份 API",
            "## 场景三：通过 Entra ID OBO 传递用户身份",
            "## 场景四：Gateway PrivateLink与VPC Lambda",
        ):
            self.assertIn(heading, blog)
        self.assertNotIn("场景五", blog)
        self.assertEqual(
            {
                "scenario1-native-lambda.yaml",
                "scenario2-mixed-auth.yaml",
                "scenario3-entra-obo.yaml",
                "scenario4-private-link.yaml",
            },
            {path.name for path in (CFN / "scenarios").glob("*.yaml")},
        )

    def test_only_portable_parameter_examples_are_delivery_inputs(self):
        expected = {f"scenario{number}.example.json" for number in range(1, 5)}
        self.assertTrue(expected.issubset({path.name for path in (CFN / "parameters").glob("*.example.json")}))
        readme = (CFN / "README.md").read_text()
        self.assertIn("示例文件必须复制到受保护位置后修改", readme)
        ignore = (ROOT / ".gitignore").read_text()
        self.assertIn("cloudformation/parameters/*.json", ignore)
        self.assertIn("!cloudformation/parameters/*.example.json", ignore)

    def test_scenarios_one_to_three_use_external_lambdas_and_no_quick_resource(self):
        rendered = "\n".join(
            path.read_text()
            for path in (CFN / "scenarios").glob("scenario[1-3]-*.yaml")
        )
        for parameter in ("OrderToolLambdaArn", "InventoryToolLambdaArn"):
            self.assertIn(parameter, rendered)
        self.assertNotIn("AWS::Lambda::Function", rendered)
        all_scenarios = "\n".join(path.read_text() for path in (CFN / "scenarios").glob("*.yaml"))
        self.assertNotIn("ActionConnector", all_scenarios)
        scenario4_services = (CFN / "modules/scenario4-vpc-services.yaml").read_text()
        self.assertIn("AWS::Serverless::Function", scenario4_services)
        readme = (CFN / "README.md").read_text()
        self.assertIn("## 9. Quick MCP 手工最后一步", readme)

    def test_scenario4_cloudformation_has_private_controls(self):
        scenario = (CFN / "scenarios/scenario4-private-link.yaml").read_text()
        network = (CFN / "modules/private-network.yaml").read_text()
        gateway_endpoint = (CFN / "modules/scenario4-gateway-endpoint.yaml").read_text()
        services = (CFN / "modules/scenario4-vpc-services.yaml").read_text()
        self.assertIn("../modules/private-network.yaml", scenario)
        self.assertIn("../modules/cognito-m2m.yaml", scenario)
        self.assertIn("AWS::QuickSight::VPCConnection", network)
        self.assertNotIn("com.amazonaws.${AWS::Region}.execute-api", network)
        self.assertNotIn("com.amazonaws.${AWS::Region}.cognito-idp", network)
        self.assertIn("com.amazonaws.${AWS::Region}.bedrock-agentcore.gateway", gateway_endpoint)
        self.assertIn("bedrock-agentcore:InvokeGateway", gateway_endpoint)
        self.assertNotIn("AWS::BedrockAgentCore::ResourcePolicy", gateway_endpoint)
        self.assertNotIn("AuthVpcConnectionArn:", scenario)
        self.assertEqual(2, services.count("AWS::Serverless::Function"))
        self.assertNotIn("AWS::EC2::FlowLog", network)

    def test_delivery_docs_do_not_reference_removed_history_trees(self):
        paths = [
            CFN / "README.md",
            ROOT / "gateway/Scenario2MixedAuth/README.md",
            ROOT / "gateway/Scenario3UserDelegation/README.md",
            ROOT / "gateway/Scenario3UserDelegation/ENTRA_SETUP.md",
            ROOT / "gateway/Scenario4PrivateLink/README.md",
        ]
        text = "\n".join(path.read_text() for path in paths)
        for removed in (
            "gateway/BlogLab",
            "gateway/legacy",
            "Scenario5MixedAuth",
            "evidence/legacy",
            "evidence/phase0",
            "tests/legacy",
        ):
            self.assertNotIn(removed, text)

    def test_delivery_sources_do_not_pin_account_or_region(self):
        roots = (
            ROOT / "business-lambdas",
            ROOT / "cloudformation",
            ROOT / "docs",
            ROOT / "gateway/Scenario2MixedAuth",
            ROOT / "gateway/Scenario3UserDelegation",
            ROOT / "gateway/Scenario4PrivateLink",
        )
        suffixes = {".py", ".sh", ".json", ".md", ".yaml"}
        text = "\n".join(
            path.read_text()
            for root in roots
            for path in root.rglob("*")
            if (
                path.is_file()
                and path.suffix in suffixes
                and "__pycache__" not in path.parts
                and not path.name.endswith(".local.json")
                and path.name != "deployed-state.json"
                and not (
                    path.parent == CFN / "parameters"
                    and not path.name.endswith(".example.json")
                )
            )
        )
        self.assertNotIn("271" + "547278201", text)
        self.assertNotIn("us-" + "east-1", text)
        deploy = (CFN / "deploy.sh").read_text()
        self.assertIn("AWS_REGION", deploy)
        self.assertIn("AWS_DEFAULT_REGION", deploy)

    def test_root_readme_is_the_delivery_entrypoint(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("四个场景的基础设施唯一由", readme)
        self.assertIn("三只共享业务Lambda", readme)
        self.assertIn("cloudformation/README.md", readme)
        self.assertIn("python3 -m unittest discover -s tests -v", readme)

    def test_blog_has_no_local_images_and_matches_current_deployment_contract(self):
        blog = BLOG.read_text()
        self.assertNotRegex(blog, r"!\[[^]]*\]\([A-Za-z]:\\")
        self.assertIn("cd quick-agentcore-mcp-connector-sample", blog)
        self.assertIn("/secure/path/<SCENARIO>.json", blog)
        self.assertNotIn("四个预部署业务 Lambda", blog)
        self.assertIn("三只共享业务Lambda", blog)
        self.assertIn(
            "连接器到 Gateway 的入站认证仍使用独立的 Cognito client credentials flow",
            blog,
        )
        self.assertNotIn("不创建 Cognito user pool", blog)

    def test_markdown_relative_links_resolve(self):
        for markdown in ROOT.rglob("*.md"):
            text = markdown.read_text()
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                target = target.strip().split(" ", 1)[0]
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                relative = unquote(target.split("#", 1)[0])
                if not relative:
                    continue
                self.assertTrue(
                    (markdown.parent / relative).resolve().exists(),
                    f"{markdown.relative_to(ROOT)} -> {target}",
                )

    def test_generated_state_evidence_and_output_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text()
        for pattern in ("evidence/", "output/", "gateway/**/config/deployed-state.json", ".kiro/"):
            self.assertIn(pattern, ignore)


if __name__ == "__main__":
    unittest.main()
