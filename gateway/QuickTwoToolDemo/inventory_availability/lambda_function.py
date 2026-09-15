"""Read-only Demo inventory service exposed as an AgentCore Gateway Lambda tool."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_ORDER_ID_RE = re.compile(r"^ORDER-[A-Z0-9-]{1,32}$")
_CODE_RE = re.compile(r"^[A-Z0-9-]{1,32}$")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_INVENTORY_ROLE = "InventoryReader"

# Fixed, non-customer Demo inventory. SKU-ERROR intentionally simulates a source failure.
_INVENTORY: dict[tuple[str, str], int] = {
    ("SKU-RED", "WH-A"): 3,
    ("SKU-BLUE", "WH-A"): 10,
    ("SKU-GREEN", "WH-B"): 1,
    ("SKU-ZERO", "WH-C"): 0,
}
_FAILURE_KEYS = {("SKU-ERROR", "WH-A")}


def _checksum(order_id: str, requirements: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"order_id": order_id, "requirements": requirements},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(error_code: str, order_id: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "tool": "check_inventory_availability",
        "status": "INVALID_INPUT",
        "risk": "UNABLE_TO_FULLY_ASSESS",
        "error_code": error_code,
        "read_only": True,
    }
    if isinstance(order_id, str):
        result["order_id"] = order_id
    return result


def _validate_requirements(value: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        return None, "INVALID_REQUIREMENTS_COUNT"

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "sku",
            "warehouse_id",
            "required_quantity",
        }:
            return None, "INVALID_REQUIREMENT_FIELDS"
        sku = item.get("sku")
        warehouse_id = item.get("warehouse_id")
        quantity = item.get("required_quantity")
        if (
            not isinstance(sku, str)
            or _CODE_RE.fullmatch(sku) is None
            or not isinstance(warehouse_id, str)
            or _CODE_RE.fullmatch(warehouse_id) is None
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or quantity > 1_000_000
        ):
            return None, "INVALID_REQUIREMENT_VALUE"
        key = (sku, warehouse_id)
        if key in seen:
            return None, "DUPLICATE_REQUIREMENT_KEY"
        seen.add(key)
        normalized.append(
            {
                "sku": sku,
                "warehouse_id": warehouse_id,
                "required_quantity": quantity,
            }
        )

    normalized.sort(key=lambda item: (item["sku"], item["warehouse_id"]))
    return normalized, None


def _execute(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {
        "order_id",
        "requirements",
        "requirements_checksum",
    }:
        return _invalid("INVALID_REQUEST_FIELDS")

    order_id = request.get("order_id")
    if not isinstance(order_id, str) or _ORDER_ID_RE.fullmatch(order_id) is None:
        return _invalid("INVALID_ORDER_ID")

    requirements, error = _validate_requirements(request.get("requirements"))
    if error is not None or requirements is None:
        return _invalid(error or "INVALID_REQUIREMENTS", order_id)

    supplied_checksum = request.get("requirements_checksum")
    if (
        not isinstance(supplied_checksum, str)
        or _CHECKSUM_RE.fullmatch(supplied_checksum) is None
        or supplied_checksum != _checksum(order_id, requirements)
    ):
        return _invalid("REQUIREMENTS_CHECKSUM_MISMATCH", order_id)

    items: list[dict[str, Any]] = []
    shortage_items: list[dict[str, Any]] = []
    unverified_items: list[dict[str, Any]] = []

    for requirement in requirements:
        key = (requirement["sku"], requirement["warehouse_id"])
        if key in _FAILURE_KEYS or key not in _INVENTORY:
            unverified_items.append(
                {
                    **requirement,
                    "error_code": "INVENTORY_SOURCE_UNAVAILABLE"
                    if key in _FAILURE_KEYS
                    else "INVENTORY_KEY_NOT_FOUND",
                }
            )
            continue

        available_quantity = _INVENTORY[key]
        shortage_quantity = max(
            requirement["required_quantity"] - available_quantity,
            0,
        )
        item = {
            **requirement,
            "available_quantity": available_quantity,
            "shortage_quantity": shortage_quantity,
        }
        items.append(item)
        if shortage_quantity > 0:
            shortage_items.append(item)

    if unverified_items:
        status = "PARTIAL"
        risk = "UNABLE_TO_FULLY_ASSESS"
    elif shortage_items:
        status = "SUCCESS"
        risk = "SHORTAGE_RISK"
    else:
        status = "SUCCESS"
        risk = "NO_SHORTAGE_FOUND"

    return {
        "schema_version": "1.0",
        "tool": "check_inventory_availability",
        "status": status,
        "order_id": order_id,
        "risk": risk,
        "items": items,
        "shortage_items": shortage_items,
        "unverified_items": unverified_items,
        "read_only": True,
    }


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def _trusted_authorizer_claims(
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    request_context = event.get("requestContext")
    authorizer = (
        request_context.get("authorizer")
        if isinstance(request_context, dict)
        else None
    )
    if not isinstance(authorizer, dict):
        return None, None

    # API Gateway REST API with a Cognito user-pool authorizer.
    claims = authorizer.get("claims")
    if isinstance(claims, dict):
        return claims, "COGNITO_REST"

    # API Gateway HTTP API payload 2.0 with a JWT authorizer.
    jwt = authorizer.get("jwt")
    claims = jwt.get("claims") if isinstance(jwt, dict) else None
    if isinstance(claims, dict):
        return claims, "JWT_HTTP"
    return None, None


def _claim_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {item for item in value if isinstance(item, str) and item}
    if not isinstance(value, str) or not value.strip():
        return set()
    stripped = value.strip()
    if stripped.startswith("["):
        if not stripped.endswith("]"):
            return set()
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            # API Gateway HTTP JWT authorizers stringify array claims as
            # bracketed values such as ``[InventoryReader]`` rather than JSON.
            inner = stripped[1:-1].strip()
            if not inner:
                return set()
            return {
                item.strip().strip("'\"")
                for item in inner.split(",")
                if item.strip().strip("'\"")
            }
        if isinstance(decoded, list):
            return {item for item in decoded if isinstance(item, str) and item}
        return set()
    return set(stripped.split())


def _authorization_error(
    event: dict[str, Any], claims: dict[str, Any], authorizer_type: str
) -> tuple[int, str] | None:
    stage = event.get("stageVariables")
    if not isinstance(stage, dict):
        # Preserve legacy delegated HTTP integrations whose route scope is their
        # only business authorization control. App-only tokens carry no scp, so
        # they must never pass without a trusted stage contract.
        delegated_scopes = _claim_values(claims.get("scp") or claims.get("scope"))
        if authorizer_type == "JWT_HTTP" and not delegated_scopes:
            return 500, "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID"
        return None
    if authorizer_type != "JWT_HTTP":
        return 401, "ENTRA_JWT_AUTHORIZER_REQUIRED"

    mode = stage.get("authorizationMode")
    if mode == "ENTRA_APP_ONLY":
        expected_tenant = stage.get("expectedTenantId")
        expected_client = stage.get("expectedClientId")
        required_role = stage.get("requiredAppRole")
        if (
            not all(isinstance(value, str) and value for value in (expected_tenant, expected_client, required_role))
            or required_role != _REQUIRED_INVENTORY_ROLE
        ):
            return 500, "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID"
        if claims.get("tid") != expected_tenant:
            return 403, "ENTRA_TENANT_NOT_ALLOWED"
        if (claims.get("azp") or claims.get("appid")) != expected_client:
            return 403, "ENTRA_CLIENT_NOT_ALLOWED"
    elif mode == "ENTRA_DELEGATED_USER_ROLE":
        required_role = stage.get("requiredUserRole")
        expected_authorized_party = stage.get("expectedAuthorizedParty")
        if (
            required_role != _REQUIRED_INVENTORY_ROLE
            or not isinstance(expected_authorized_party, str)
            or not expected_authorized_party
        ):
            return 500, "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID"
        authorized_party = claims.get("azp") or claims.get("appid")
        delegated_identity = (
            claims.get("tid"), claims.get("oid"), claims.get("sub"),
            authorized_party,
        )
        if not all(isinstance(value, str) and value for value in delegated_identity):
            return 401, "INVALID_DELEGATED_USER_CLAIMS"
        if expected_authorized_party is not None and authorized_party != expected_authorized_party:
            return 403, "ENTRA_CLIENT_NOT_ALLOWED"
    else:
        return 500, "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID"

    if required_role not in _claim_values(claims.get("roles")):
        return 403, "INVENTORY_READER_ROLE_REQUIRED"
    return None


def _trusted_caller_context(
    claims: dict[str, Any], authorizer_type: str
) -> dict[str, Any]:
    scopes = claims.get("scp") or claims.get("scope") or ""
    return {
        "authorizer_type": authorizer_type,
        "tenant_id": claims.get("tid"),
        "user_object_id": claims.get("oid"),
        "subject": claims.get("sub"),
        "client_id": claims.get("client_id") or claims.get("azp") or claims.get("appid"),
        "scopes": sorted(scopes.split()) if isinstance(scopes, str) else [],
        "roles": sorted(_claim_values(claims.get("roles"))),
    }


def _is_http_event(event: dict[str, Any]) -> bool:
    return any(
        key in event
        for key in ("requestContext", "body", "version", "routeKey", "httpMethod")
    )


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return _invalid("INVALID_REQUEST_FIELDS")

    if not _is_http_event(event):
        return _execute(event)

    claims, authorizer_type = _trusted_authorizer_claims(event)
    if claims is None or authorizer_type is None:
        return _response(
            401,
            {
                "schema_version": "1.0",
                "service": "inventory_tool",
                "status": "UNAUTHENTICATED",
                "error_code": "MISSING_TRUSTED_AUTHORIZER_CLAIMS",
                "read_only": True,
            },
        )
    authorization_error = _authorization_error(event, claims, authorizer_type)
    if authorization_error is not None:
        status_code, error_code = authorization_error
        return _response(
            status_code,
            {
                "schema_version": "1.0",
                "service": "inventory_tool",
                "status": "UNAUTHENTICATED" if status_code == 401 else "FORBIDDEN" if status_code == 403 else "ERROR",
                "error_code": error_code,
                "read_only": True,
            },
        )
    if event.get("isBase64Encoded") is True:
        return _response(400, _invalid("BASE64_BODY_NOT_SUPPORTED"))

    body = event.get("body")
    if not isinstance(body, str):
        return _response(400, _invalid("JSON_BODY_REQUIRED"))
    try:
        request = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return _response(400, _invalid("INVALID_JSON_BODY"))

    result = _execute(request)
    result["trusted_caller_context"] = _trusted_caller_context(
        claims, authorizer_type
    )
    return _response(400 if result["status"] == "INVALID_INPUT" else 200, result)
