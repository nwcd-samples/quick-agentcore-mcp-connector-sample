import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFN = ROOT / "cloudformation"


def _load_validator():
    path = CFN / "validate_parameters.py"
    spec = importlib.util.spec_from_file_location("validate_parameters", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


class CloudFormationDeliveryTests(unittest.TestCase):
    def test_parameter_examples_cover_all_required_root_parameters(self):
        for number in range(1, 5):
            template = next((CFN / "scenarios").glob(f"scenario{number}-*.yaml"))
            example = CFN / "parameters" / f"scenario{number}.example.json"
            all_names, defaults = VALIDATOR.template_parameter_contract(template)
            provided = set(VALIDATOR.load_parameters(example))
            self.assertFalse(provided - all_names, template.name)
            self.assertFalse((all_names - defaults) - provided, template.name)

    def test_checked_in_examples_are_rejected_until_placeholders_are_replaced(self):
        template = CFN / "scenarios/scenario1-native-lambda.yaml"
        example = CFN / "parameters/scenario1.example.json"
        with self.assertRaisesRegex(ValueError, "placeholder"):
            VALIDATOR.validate_parameters(
                template=template,
                parameter_file=example,
                region="eu-west-1",
                account_id="444455556666",
                partition="aws",
            )

    def test_valid_parameters_must_match_deployment_context(self):
        values = [
            {"ParameterKey": "Prefix", "ParameterValue": "quick-s1"},
            {"ParameterKey": "CognitoDomainPrefix", "ParameterValue": "company-quick-s1"},
            {
                "ParameterKey": "GatewayScopeIdentifier",
                "ParameterValue": "https://quick-agentcore.company.example/scenario1",
            },
            {
                "ParameterKey": "OrderToolLambdaArn",
                "ParameterValue": "arn:aws:lambda:eu-west-1:444455556666:function:order-tool",
            },
            {
                "ParameterKey": "InventoryToolLambdaArn",
                "ParameterValue": "arn:aws:lambda:eu-west-1:444455556666:function:inventory-tool",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parameters.json"
            path.write_text(json.dumps(values))
            validated = VALIDATOR.validate_parameters(
                template=CFN / "scenarios/scenario1-native-lambda.yaml",
                parameter_file=path,
                region="eu-west-1",
                account_id="444455556666",
                partition="aws",
            )
            self.assertEqual(5, len(validated))
            values[-1]["ParameterValue"] = (
                "arn:aws:lambda:ap-southeast-2:444455556666:function:inventory-tool"
            )
            path.write_text(json.dumps(values))
            with self.assertRaisesRegex(ValueError, "region"):
                VALIDATOR.validate_parameters(
                    template=CFN / "scenarios/scenario1-native-lambda.yaml",
                    parameter_file=path,
                    region="eu-west-1",
                    account_id="444455556666",
                    partition="aws",
                )

            values[-1]["ParameterValue"] = (
                "arn:aws:lambda:eu-west-1:444455556666:function:inventory-tool"
            )
            path.write_text(json.dumps(values))
            with self.assertRaisesRegex(ValueError, "account"):
                VALIDATOR.validate_parameters(
                    template=CFN / "scenarios/scenario1-native-lambda.yaml",
                    parameter_file=path,
                    region="eu-west-1",
                    account_id="777788889999",
                    partition="aws",
                )
            with self.assertRaisesRegex(ValueError, "partition"):
                VALIDATOR.validate_parameters(
                    template=CFN / "scenarios/scenario1-native-lambda.yaml",
                    parameter_file=path,
                    region="eu-west-1",
                    account_id="444455556666",
                    partition="aws-us-gov",
                )

    def test_deploy_script_preflights_before_package(self):
        source = (CFN / "deploy.sh").read_text()
        self.assertLess(source.index("validate_parameters.py"), source.index("aws cloudformation package"))
        self.assertIn("aws sts get-caller-identity", source)


if __name__ == "__main__":
    unittest.main()
