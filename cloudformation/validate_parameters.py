#!/usr/bin/env python3
"""Validate deployment parameters before CloudFormation package/deploy."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_PARAMETER_RE = re.compile(r"^  ([A-Za-z][A-Za-z0-9]*):\s*$")
_ARN_RE = re.compile(
    r"^arn:(?P<partition>[^:]+):(?P<service>[^:]+):(?P<region>[^:]*):"
    r"(?P<account>[0-9]{12}):(?P<resource>.+)$"
)
_PLACEHOLDER_FRAGMENTS = (
    "<AWS_REGION>",
    "<AWS_ACCOUNT_ID>",
    "<QUICK_VPC_CONNECTION_ID>",
    "111122223333",
    "EXAMPLE",
    "replace-me",
)
_PLACEHOLDER_GUIDS = {
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
    "00000000-0000-0000-0000-000000000003",
    "00000000-0000-0000-0000-000000000004",
    "00000000-0000-0000-0000-000000000005",
}


def template_parameter_contract(path: Path) -> tuple[set[str], set[str]]:
    """Return (all parameter names, names with defaults) from a root template."""
    lines = path.read_text().splitlines()
    try:
        start = lines.index("Parameters:") + 1
    except ValueError as exc:
        raise ValueError(f"Template has no Parameters section: {path}") from exc

    all_names: set[str] = set()
    defaults: set[str] = set()
    current: str | None = None
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        match = _PARAMETER_RE.match(line)
        if match:
            current = match.group(1)
            all_names.add(current)
        elif current is not None and line.startswith("    Default:"):
            defaults.add(current)
    return all_names, defaults


def load_parameters(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise TypeError("Parameter file must be a JSON array")
    result: dict[str, str] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"ParameterKey", "ParameterValue"}:
            raise ValueError(f"Parameter entry {index} must contain only ParameterKey/ParameterValue")
        key, parameter_value = item["ParameterKey"], item["ParameterValue"]
        if not isinstance(key, str) or not isinstance(parameter_value, str):
            raise TypeError(f"Parameter entry {index} key/value must be strings")
        if key in result:
            raise ValueError(f"Duplicate parameter: {key}")
        result[key] = parameter_value
    return result


def validate_parameters(
    *,
    template: Path,
    parameter_file: Path,
    region: str,
    account_id: str,
    partition: str,
) -> dict[str, str]:
    parameters = load_parameters(parameter_file)
    all_names, defaults = template_parameter_contract(template)
    unknown = set(parameters) - all_names
    missing = (all_names - defaults) - set(parameters)
    if unknown:
        raise ValueError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"Missing required parameters: {', '.join(sorted(missing))}")

    for key, value in parameters.items():
        if not value:
            raise ValueError(f"{key} must not be empty")
        if any(fragment in value for fragment in _PLACEHOLDER_FRAGMENTS):
            raise ValueError(f"{key} still contains a sample placeholder")
        if value in _PLACEHOLDER_GUIDS or re.search(r"<[^>]+>", value):
            raise ValueError(f"{key} still contains a sample placeholder")
        if not key.endswith("Arn"):
            continue
        match = _ARN_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"{key} must be a complete ARN")
        actual = match.groupdict()
        if actual["partition"] != partition:
            raise ValueError(f"{key} partition {actual['partition']} does not match {partition}")
        if actual["region"] != region:
            raise ValueError(f"{key} region {actual['region']} does not match {region}")
        if actual["account"] != account_id:
            raise ValueError(f"{key} account {actual['account']} does not match {account_id}")
    return parameters


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--partition", required=True)
    args = parser.parse_args()
    validated = validate_parameters(
        template=args.template,
        parameter_file=args.parameters,
        region=args.region,
        account_id=args.account_id,
        partition=args.partition,
    )
    print(f"Validated {len(validated)} parameters for {args.account_id} in {args.region}")


if __name__ == "__main__":
    main()
