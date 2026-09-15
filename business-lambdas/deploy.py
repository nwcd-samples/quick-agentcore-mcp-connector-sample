#!/usr/bin/env python3
"""Deploy and verify the predeployed business Lambda functions.

This bootstrap is intentionally separate from the four scenario templates. It
creates only the business functions and their minimum runtime dependencies. It
never prints or writes the generated Basic credential value.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import secrets
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAME = "quick-agentcore/order-basic-auth-tool"
EVIDENCE_PATH = ROOT / "evidence/business-lambdas/deployment-and-validation.json"

TAGS = {
    "Project": "quick-agentcore-security-blog",
    "Component": "predeployed-business-lambdas",
    "ManagedBy": "business-lambdas/deploy.py",
}

FUNCTIONS = {
    "order-tool": {
        "source": ROOT / "gateway/QuickTwoToolDemo/order_requirements/lambda_function.py",
        "description": "Read-only order requirements tool; native and OrdersReader-authorized HTTP API events",
    },
    "inventory-tool": {
        "source": ROOT / "gateway/QuickTwoToolDemo/inventory_availability/lambda_function.py",
        "description": "Read-only inventory tool; native, delegated JWT, and Entra app-only JWT HTTP API events",
    },
    "order-basic-auth-tool": {
        "source": ROOT / "gateway/Scenario2MixedAuth/order_service/lambda_function.py",
        "description": "Read-only legacy order tool with in-function HTTP Basic authentication",
    },
}


def _client_error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _zip_source(path: Path) -> bytes:
    if not path.is_file():
        raise RuntimeError(f"Missing Lambda source: {path}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("lambda_function.py")
        info.date_time = (2020, 1, 1, 0, 0, 0)
        info.external_attr = 0o644 << 16
        archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _tag_dict(items: list[dict[str, str]]) -> dict[str, str]:
    return {item["Key"]: item["Value"] for item in items}


def _assert_owned(actual: dict[str, str], label: str) -> None:
    expected = {key: TAGS[key] for key in ("Project", "Component", "ManagedBy")}
    if any(actual.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Refusing to modify unowned {label}")


def _ensure_secret(
    session: boto3.Session, account: str, region: str, partition: str
) -> str:
    client = session.client("secretsmanager")
    try:
        described = client.describe_secret(SecretId=SECRET_NAME)
        tags = _tag_dict(described.get("Tags", []))
        _assert_owned(tags, f"secret {SECRET_NAME}")
        secret_arn = described["ARN"]
        # Validate only the shape; never print or persist the value.
        current = client.get_secret_value(SecretId=secret_arn)
        payload = json.loads(current["SecretString"])
        encoded = (
            payload.get("base64Credentials")
            if isinstance(payload, dict) and set(payload) == {"base64Credentials"}
            else None
        )
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("Owned Basic secret has an invalid shape")
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("Owned Basic secret has an invalid encoding") from error
        username, separator, password = decoded.partition(":")
        if not separator or not username or not password:
            raise RuntimeError("Owned Basic secret has invalid Basic credentials")
        return secret_arn
    except client.exceptions.ResourceNotFoundException:
        pass

    username = f"svc-{secrets.token_hex(8)}"
    password = secrets.token_urlsafe(36)
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    try:
        created = client.create_secret(
            Name=SECRET_NAME,
            Description="Generated Basic credential shared by AgentCore provider and order-basic-auth-tool",
            SecretString=json.dumps(
                {"base64Credentials": encoded},
                separators=(",", ":"),
            ),
            Tags=[{"Key": key, "Value": value} for key, value in TAGS.items()],
        )
    finally:
        # Best-effort removal of references; CPython strings are immutable, so
        # the security guarantee is no output/file persistence, not zeroization.
        username = password = encoded = ""
    secret_arn = created["ARN"]
    expected_prefix = f"arn:{partition}:secretsmanager:{region}:{account}:secret:{SECRET_NAME}-"
    if not secret_arn.startswith(expected_prefix):
        raise RuntimeError("Created secret ARN is outside the approved account/region")
    return secret_arn


def _ensure_log_group(logs: Any, name: str) -> str:
    response = logs.describe_log_groups(
        logGroupNamePrefix=name,
        limit=10,
    )
    existing = next(
        (item for item in response.get("logGroups", []) if item["logGroupName"] == name),
        None,
    )
    if existing is None:
        logs.create_log_group(logGroupName=name, tags=TAGS)
    else:
        arn = existing.get("arn", "").removesuffix(":*")
        _assert_owned(logs.list_tags_for_resource(resourceArn=arn).get("tags", {}), f"log group {name}")
    logs.put_retention_policy(logGroupName=name, retentionInDays=14)
    return name


def _trust_policy(account: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }


def _runtime_policy(
    *,
    account: str,
    region: str,
    partition: str,
    log_group: str,
    secret_arn: str | None,
) -> dict[str, Any]:
    statements: list[dict[str, Any]] = [
        {
            "Sid": "WriteOnlyOwnLogGroup",
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"arn:{partition}:logs:{region}:{account}:log-group:{log_group}:*",
        }
    ]
    if secret_arn is not None:
        statements.append(
            {
                "Sid": "ReadOnlyOwnBasicCredential",
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": secret_arn,
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _ensure_role(
    iam: Any,
    *,
    function_name: str,
    account: str,
    region: str,
    partition: str,
    log_group: str,
    secret_arn: str | None,
) -> str:
    role_name = f"quick-agentcore-{region}-{function_name}-role"
    trust = _trust_policy(account)
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        tags = _tag_dict(iam.list_role_tags(RoleName=role_name).get("Tags", []))
        _assert_owned(tags, f"IAM role {role_name}")
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust, separators=(",", ":")),
        )
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust, separators=(",", ":")),
            Description=f"Least-privilege execution role for {function_name}",
            Tags=[{"Key": key, "Value": value} for key, value in TAGS.items()],
        )["Role"]
        iam.get_waiter("role_exists").wait(RoleName=role_name)

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="BusinessLambdaRuntime",
        PolicyDocument=json.dumps(
            _runtime_policy(
                account=account,
                region=region,
                partition=partition,
                log_group=log_group,
                secret_arn=secret_arn,
            ),
            separators=(",", ":"),
        ),
    )
    return role["Arn"]


def _wait_function(lambda_client: Any, name: str) -> dict[str, Any]:
    lambda_client.get_waiter("function_updated_v2").wait(
        FunctionName=name,
        WaiterConfig={"Delay": 2, "MaxAttempts": 90},
    )
    configuration = lambda_client.get_function_configuration(FunctionName=name)
    if configuration.get("State") != "Active" or configuration.get("LastUpdateStatus") != "Successful":
        raise RuntimeError(f"Lambda {name} did not become ready")
    return configuration


def _ensure_function(
    lambda_client: Any,
    *,
    name: str,
    source: Path,
    description: str,
    role_arn: str,
    secret_arn: str | None,
) -> dict[str, Any]:
    code = _zip_source(source)
    environment = (
        {"Variables": {"ORDER_CREDENTIAL_SECRET_ARN": secret_arn}}
        if secret_arn is not None
        else {"Variables": {}}
    )
    configuration = {
        "FunctionName": name,
        "Runtime": "python3.12",
        "Role": role_arn,
        "Handler": "lambda_function.lambda_handler",
        "Description": description,
        "Timeout": 10,
        "MemorySize": 128,
        "Environment": environment,
        "LoggingConfig": {
            "LogFormat": "JSON",
            "ApplicationLogLevel": "INFO",
            "SystemLogLevel": "WARN",
            "LogGroup": f"/aws/lambda/{name}",
        },
    }
    try:
        existing = lambda_client.get_function(FunctionName=name)
        _assert_owned(existing.get("Tags", {}), f"Lambda {name}")
        lambda_client.update_function_code(
            FunctionName=name,
            ZipFile=code,
            Publish=False,
        )
        _wait_function(lambda_client, name)
        lambda_client.update_function_configuration(**configuration)
    except lambda_client.exceptions.ResourceNotFoundException:
        lambda_client.create_function(
            **configuration,
            Code={"ZipFile": code},
            Publish=False,
            Architectures=["x86_64"],
            Tags=TAGS,
        )
    full = _wait_function(lambda_client, name)

    try:
        lambda_client.get_function_url_config(FunctionName=name)
    except lambda_client.exceptions.ResourceNotFoundException:
        pass
    else:
        raise RuntimeError(f"Lambda {name} unexpectedly has a Function URL")

    return {
        "name": name,
        "arn": full["FunctionArn"],
        "roleArn": full["Role"],
        "runtime": full["Runtime"],
        "handler": full["Handler"],
        "memorySize": full["MemorySize"],
        "timeout": full["Timeout"],
        "codeSha256": full["CodeSha256"],
        "state": full["State"],
        "lastUpdateStatus": full["LastUpdateStatus"],
        "functionUrlPresent": False,
    }


def _invoke(lambda_client: Any, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = lambda_client.invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    raw = response["Payload"].read()
    if response.get("FunctionError"):
        raise RuntimeError(f"Lambda {name} returned FunctionError")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError(f"Lambda {name} returned a non-object payload")
    return result


def _proxy_body(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body")
    if not isinstance(body, str):
        raise TypeError("Proxy response has no JSON body")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise TypeError("Proxy response body is not an object")
    return payload


def _validate_remote(lambda_client: Any, secrets_client: Any, secret_arn: str) -> dict[str, Any]:
    order = _invoke(lambda_client, "order-tool", {"order_id": "ORDER-1001"})
    if order.get("status") != "SUCCESS":
        raise RuntimeError("Native order invocation failed")
    handoff = {
        "order_id": order["order_id"],
        "requirements": order["requirements"],
        "requirements_checksum": order["requirements_checksum"],
    }
    inventory = _invoke(lambda_client, "inventory-tool", handoff)
    shortage = sum(
        item.get("shortage_quantity", 0)
        for item in inventory.get("shortage_items", [])
    )
    if inventory.get("risk") != "SHORTAGE_RISK" or shortage != 2:
        raise RuntimeError("Native inventory invocation returned an unexpected result")

    order_http_event = {
        "version": "2.0",
        "stageVariables": {
            "authorizationMode": "ENTRA_DELEGATED_USER_ROLE",
            "requiredUserRole": "OrdersReader",
            "expectedAuthorizedParty": "validation-gateway",
        },
        "requestContext": {"authorizer": {"jwt": {"claims": {
            "tid": "validation-tenant", "oid": "validation-orders-reader-user",
            "sub": "validation-orders-reader-subject", "azp": "validation-gateway",
            "scp": "Orders.Read", "roles": "[OrdersReader]",
        }}}},
        "body": json.dumps({"order_id": "ORDER-1001"}, separators=(",", ":")),
        "isBase64Encoded": False,
    }
    role_order_success = _invoke(lambda_client, "order-tool", order_http_event)
    order_http_event["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = []
    role_order_denied = _invoke(lambda_client, "order-tool", order_http_event)
    order_http = role_order_success
    if role_order_success.get("statusCode") != 200 or role_order_denied.get("statusCode") != 403:
        raise RuntimeError("OrdersReader role order authorization checks failed")
    if _proxy_body(role_order_denied).get("error_code") != "ORDERS_READER_ROLE_REQUIRED":
        raise RuntimeError("OrdersReader role order denial returned an unexpected error")

    inventory_rest = _invoke(
        lambda_client,
        "inventory-tool",
        {
            "httpMethod": "POST",
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "client_id": "validation-inventory-client",
                        "scope": "inventory/read",
                    }
                }
            },
            "body": json.dumps(handoff, separators=(",", ":")),
            "isBase64Encoded": False,
        },
    )
    if inventory_rest.get("statusCode") != 200:
        raise RuntimeError("REST inventory invocation failed")

    inventory_app_only_event = {
        "version": "2.0",
        "stageVariables": {
            "authorizationMode": "ENTRA_APP_ONLY",
            "expectedTenantId": "validation-tenant",
            "expectedClientId": "validation-inventory-client",
            "requiredAppRole": "InventoryReader",
        },
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "tid": "validation-tenant",
                        "azp": "validation-inventory-client",
                        "roles": ["InventoryReader"],
                    }
                }
            }
        },
        "body": json.dumps(handoff, separators=(",", ":")),
        "isBase64Encoded": False,
    }
    inventory_app_only = _invoke(
        lambda_client, "inventory-tool", inventory_app_only_event
    )
    inventory_app_only_event["requestContext"]["authorizer"]["jwt"]["claims"][
        "azp"
    ] = "wrong-client"
    inventory_app_only_denied = _invoke(
        lambda_client, "inventory-tool", inventory_app_only_event
    )
    if (
        inventory_app_only.get("statusCode") != 200
        or inventory_app_only_denied.get("statusCode") != 403
        or _proxy_body(inventory_app_only_denied).get("error_code")
        != "ENTRA_CLIENT_NOT_ALLOWED"
    ):
        raise RuntimeError("Entra app-only inventory authorization checks failed")

    inventory_http_event = {
        "version": "2.0",
        "stageVariables": {
            "authorizationMode": "ENTRA_DELEGATED_USER_ROLE",
            "requiredUserRole": "InventoryReader",
            "expectedAuthorizedParty": "validation-gateway",
        },
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "tid": "validation-tenant",
                        "oid": "validation-inventory-reader-user",
                        "sub": "validation-inventory-reader-subject",
                        "azp": "validation-gateway",
                        "scp": "Inventory.Read",
                        "roles": ["InventoryReader"],
                    }
                }
            }
        },
        "body": json.dumps(handoff, separators=(",", ":")),
        "isBase64Encoded": False,
    }
    inventory_http = _invoke(lambda_client, "inventory-tool", inventory_http_event)
    inventory_http_event["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = []
    inventory_http_denied = _invoke(lambda_client, "inventory-tool", inventory_http_event)
    if inventory_http.get("statusCode") != 200 or inventory_http_denied.get("statusCode") != 403:
        raise RuntimeError("InventoryReader role inventory authorization checks failed")
    if _proxy_body(inventory_http_denied).get("error_code") != "INVENTORY_READER_ROLE_REQUIRED":
        raise RuntimeError("InventoryReader role inventory denial returned an unexpected error")

    missing_order_auth = _invoke(
        lambda_client,
        "order-tool",
        {"version": "2.0", "body": '{"order_id":"ORDER-1001"}'},
    )
    missing_inventory_auth = _invoke(
        lambda_client,
        "inventory-tool",
        {"version": "2.0", "body": json.dumps(handoff, separators=(",", ":"))},
    )
    if missing_order_auth.get("statusCode") != 401 or missing_inventory_auth.get("statusCode") != 401:
        raise RuntimeError("HTTP authorizer-context negative checks failed")

    # Read only inside this process and never print/persist the credential.
    secret_payload = json.loads(
        secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]
    )
    encoded = secret_payload["base64Credentials"]
    basic_event = {
        "httpMethod": "POST",
        "requestContext": {"requestId": "deployment-validation"},
        "headers": {"Authorization": f"Basic {encoded}"},
        "body": '{"order_id":"ORDER-1001"}',
        "isBase64Encoded": False,
    }
    basic_success = _invoke(lambda_client, "order-basic-auth-tool", basic_event)
    basic_event["headers"]["Authorization"] = "Basic Zm9vOmJhcg=="
    basic_rejected = _invoke(lambda_client, "order-basic-auth-tool", basic_event)
    encoded = ""
    secret_payload = {}
    if basic_success.get("statusCode") != 200 or basic_rejected.get("statusCode") != 401:
        raise RuntimeError("Basic authentication checks failed")

    return {
        "nativeOrderStatus": order["status"],
        "nativeInventoryStatus": inventory["status"],
        "nativeInventoryRisk": inventory["risk"],
        "nativeShortageQuantity": shortage,
        "httpOrderStatusCode": order_http["statusCode"],
        "ordersReaderRoleOrderStatusCode": role_order_success["statusCode"],
        "missingOrdersReaderRoleOrderStatusCode": role_order_denied["statusCode"],
        "restInventoryStatusCode": inventory_rest["statusCode"],
        "appOnlyInventoryStatusCode": inventory_app_only["statusCode"],
        "wrongAppOnlyClientInventoryStatusCode": inventory_app_only_denied["statusCode"],
        "inventoryReaderRoleInventoryStatusCode": inventory_http["statusCode"],
        "missingInventoryReaderRoleInventoryStatusCode": inventory_http_denied["statusCode"],
        "missingOrderAuthorizerStatusCode": missing_order_auth["statusCode"],
        "missingInventoryAuthorizerStatusCode": missing_inventory_auth["statusCode"],
        "basicValidStatusCode": basic_success["statusCode"],
        "basicInvalidStatusCode": basic_rejected["statusCode"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--region",
        help="AWS Region; defaults to the active AWS profile/environment configuration",
    )
    parser.add_argument(
        "--account-id",
        help="Optional 12-digit account guard; the caller account is discovered with STS",
    )
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    region = session.region_name
    if not region:
        raise SystemExit(
            "AWS Region is required; pass --region or configure it in the active AWS profile/environment"
        )
    args.region = region
    caller = session.client("sts").get_caller_identity()
    account = caller["Account"]
    caller_arn = caller.get("Arn", "")
    arn_parts = caller_arn.split(":", 2)
    if len(arn_parts) != 3 or arn_parts[0] != "arn" or not arn_parts[1]:
        raise SystemExit("Could not determine AWS partition from STS caller ARN")
    partition = arn_parts[1]
    if args.account_id and account != args.account_id:
        raise SystemExit(f"Refusing AWS account {account}; expected {args.account_id}")

    iam = session.client("iam")
    logs = session.client("logs")
    lambda_client = session.client("lambda")
    secrets_client = session.client("secretsmanager")

    secret_arn = _ensure_secret(session, account, args.region, partition)
    roles: dict[str, str] = {}
    log_groups: dict[str, str] = {}
    for name in FUNCTIONS:
        log_group = _ensure_log_group(logs, f"/aws/lambda/{name}")
        log_groups[name] = log_group
        roles[name] = _ensure_role(
            iam,
            function_name=name,
            account=account,
            region=args.region,
            partition=partition,
            log_group=log_group,
            secret_arn=secret_arn if name == "order-basic-auth-tool" else None,
        )

    # IAM trust and inline policy propagation is eventually consistent.
    time.sleep(10)

    functions: dict[str, dict[str, Any]] = {}
    for name, definition in FUNCTIONS.items():
        functions[name] = _ensure_function(
            lambda_client,
            name=name,
            source=definition["source"],
            description=definition["description"],
            role_arn=roles[name],
            secret_arn=secret_arn if name == "order-basic-auth-tool" else None,
        )

    validation = _validate_remote(lambda_client, secrets_client, secret_arn)
    state = {
        "schemaVersion": "1.0",
        "purpose": "PREDEPLOYED_BUSINESS_LAMBDAS",
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "accountId": account,
        "region": args.region,
        "partition": partition,
        "functions": functions,
        "logGroups": log_groups,
        "orderBasicSecretArn": secret_arn,
        "validation": validation,
        "secretIncluded": False,
        "tokenIncluded": False,
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
