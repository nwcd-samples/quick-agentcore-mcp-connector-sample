"""Read-only order tool for native and role-protected HTTP API events.

Native AgentCore Lambda targets use the direct business path. For Scenario 3,
API Gateway validates the Entra OBO token and ``Orders.Read`` route scope; this
Lambda then requires trusted JWT authorizer claims and the ``OrdersReader``
user app role. Caller-controlled headers, query parameters and request bodies
never provide authorization data.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

_ORDER_ID_RE = re.compile(r"^ORDER-[A-Z0-9-]{1,32}$")
_AUTHORIZATION_MODE = "ENTRA_DELEGATED_USER_ROLE"
_REQUIRED_USER_ROLE = "OrdersReader"

# Fixed, non-customer Demo records. Repeated lines intentionally exercise aggregation.
_ORDERS: dict[str, dict[str, Any]] = {
    "ORDER-1001": {
        "order_status": "CONFIRMED",
        "lines": [
            {"sku": "SKU-RED", "warehouse_id": "WH-A", "quantity": 3},
            {"sku": "SKU-RED", "warehouse_id": "WH-A", "quantity": 2},
            {"sku": "SKU-BLUE", "warehouse_id": "WH-A", "quantity": 4},
            {"sku": "SKU-GREEN", "warehouse_id": "WH-B", "quantity": 1},
        ],
    },
    "ORDER-1002": {
        "order_status": "CONFIRMED",
        "lines": [
            {"sku": "SKU-BLUE", "warehouse_id": "WH-A", "quantity": 2},
            {"sku": "SKU-GREEN", "warehouse_id": "WH-B", "quantity": 1},
        ],
    },
    "ORDER-1003": {
        "order_status": "CONFIRMED",
        "lines": [
            {"sku": "SKU-ZERO", "warehouse_id": "WH-C", "quantity": 6},
        ],
    },
    "ORDER-1004": {
        "order_status": "CONFIRMED",
        "lines": [
            {"sku": "SKU-BLUE", "warehouse_id": "WH-A", "quantity": 1},
            {"sku": "SKU-ERROR", "warehouse_id": "WH-A", "quantity": 2},
        ],
    },
}


def _checksum(order_id: str, requirements: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"order_id": order_id, "requirements": requirements},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(error_code: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "tool": "get_order_requirements",
        "status": "INVALID_INPUT",
        "error_code": error_code,
        "read_only": True,
    }


def _execute(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {"order_id"}:
        return _invalid("INVALID_REQUEST_FIELDS")

    order_id = request.get("order_id")
    if not isinstance(order_id, str) or _ORDER_ID_RE.fullmatch(order_id) is None:
        return _invalid("INVALID_ORDER_ID")

    order = _ORDERS.get(order_id)
    if order is None:
        return {
            "schema_version": "1.0",
            "tool": "get_order_requirements",
            "status": "ORDER_NOT_FOUND",
            "order_id": order_id,
            "requirements": [],
            "read_only": True,
        }

    totals: defaultdict[tuple[str, str], int] = defaultdict(int)
    for line in order["lines"]:
        quantity = line["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return {
                "schema_version": "1.0",
                "tool": "get_order_requirements",
                "status": "INVALID_ORDER_DATA",
                "order_id": order_id,
                "error_code": "NON_POSITIVE_OR_NON_INTEGER_QUANTITY",
                "read_only": True,
            }
        totals[(line["sku"], line["warehouse_id"])] += quantity

    requirements = [
        {
            "sku": sku,
            "warehouse_id": warehouse_id,
            "required_quantity": totals[(sku, warehouse_id)],
        }
        for sku, warehouse_id in sorted(totals)
    ]

    return {
        "schema_version": "1.0",
        "tool": "get_order_requirements",
        "status": "SUCCESS",
        "order_id": order_id,
        "order_status": order["order_status"],
        "requirements": requirements,
        "requirements_checksum": _checksum(order_id, requirements),
        "read_only": True,
        "next_tool": "check_inventory_availability",
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
            # bracketed values such as ``[OrdersReader]`` rather than JSON.
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


def _auth_error(status_code: int, error_code: str) -> dict[str, Any]:
    return _response(
        status_code,
        {
            "schema_version": "1.0",
            "tool": "get_order_requirements",
            "status": "AUTHORIZATION_FAILED",
            "error_code": error_code,
            "read_only": True,
        },
    )


def _authorize_http(
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    request_context = event.get("requestContext")
    authorizer = (
        request_context.get("authorizer")
        if isinstance(request_context, dict)
        else None
    )
    jwt = authorizer.get("jwt") if isinstance(authorizer, dict) else None
    claims = jwt.get("claims") if isinstance(jwt, dict) else None
    if not isinstance(claims, dict):
        return None, _auth_error(401, "MISSING_TRUSTED_AUTHORIZER_CLAIMS")

    stage = event.get("stageVariables")
    expected_authorized_party = (
        stage.get("expectedAuthorizedParty") if isinstance(stage, dict) else None
    )
    if (
        not isinstance(stage, dict)
        or stage.get("authorizationMode") != _AUTHORIZATION_MODE
        or stage.get("requiredUserRole") != _REQUIRED_USER_ROLE
        or not isinstance(expected_authorized_party, str)
        or not expected_authorized_party
    ):
        return None, _auth_error(500, "ENTRA_AUTHORIZATION_CONFIGURATION_INVALID")

    tenant = claims.get("tid")
    object_id = claims.get("oid")
    subject = claims.get("sub")
    authorized_party = claims.get("azp") or claims.get("appid")
    if not all(
        isinstance(value, str) and value
        for value in (tenant, object_id, subject, authorized_party)
    ):
        return None, _auth_error(401, "INVALID_DELEGATED_USER_CLAIMS")
    if expected_authorized_party is not None and authorized_party != expected_authorized_party:
        return None, _auth_error(403, "ENTRA_CLIENT_NOT_ALLOWED")

    roles = _claim_values(claims.get("roles"))
    if _REQUIRED_USER_ROLE not in roles:
        return None, _auth_error(403, "ORDERS_READER_ROLE_REQUIRED")

    scopes = claims.get("scp") or claims.get("scope") or ""
    return {
        "tenant_id": tenant,
        "user_object_id": object_id,
        "subject": subject,
        "authorized_party": authorized_party,
        "scopes": sorted(scopes.split()) if isinstance(scopes, str) else [],
        "roles": sorted(roles),
    }, None


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

    identity, authorization_error = _authorize_http(event)
    if authorization_error is not None or identity is None:
        return authorization_error or _auth_error(
            401, "MISSING_TRUSTED_AUTHORIZER_CLAIMS"
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
    result["delegated_user_context"] = identity
    return _response(400 if result["status"] == "INVALID_INPUT" else 200, result)
