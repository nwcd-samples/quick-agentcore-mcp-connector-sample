"""Legacy order service with HTTP Basic authentication inside the business Lambda.

This intentionally models a legacy application where authentication and business
logic share one process. API Gateway performs no authorizer step for this route.
Credentials live in Secrets Manager, are cached briefly, compared in constant time,
and are never logged or returned.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict
from typing import Any

import boto3

_ORDER_ID_RE = re.compile(r"^ORDER-[A-Z0-9-]{1,32}$")
_DEFAULT_CACHE_TTL_SECONDS = 60
_CLIENT: Any = None
_CACHE: dict[str, Any] = {"credential": None, "expires": 0.0}

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
        "lines": [{"sku": "SKU-ZERO", "warehouse_id": "WH-C", "quantity": 6}],
    },
    "ORDER-1004": {
        "order_status": "CONFIRMED",
        "lines": [
            {"sku": "SKU-BLUE", "warehouse_id": "WH-A", "quantity": 1},
            {"sku": "SKU-ERROR", "warehouse_id": "WH-A", "quantity": 2},
        ],
    },
}


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def _auth_error(status_code: int, code: str) -> dict[str, Any]:
    return _response(status_code, {
        "schema_version": "1.0",
        "service": "legacy_order_service",
        "status": "UNAUTHENTICATED" if status_code == 401 else "SERVICE_CONFIGURATION_ERROR",
        "error_code": code,
        "read_only": True,
    })


def _secret_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = boto3.client("secretsmanager")
    return _CLIENT


def _cache_ttl_seconds() -> float:
    raw = os.environ.get("CREDENTIAL_CACHE_TTL_SECONDS", str(_DEFAULT_CACHE_TTL_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        return float(_DEFAULT_CACHE_TTL_SECONDS)
    return value if value >= 0 else float(_DEFAULT_CACHE_TTL_SECONDS)


def _credential() -> dict[str, str]:
    secret_id = os.environ.get("ORDER_CREDENTIAL_SECRET_ARN")
    if not secret_id:
        raise RuntimeError("ORDER_CREDENTIAL_SECRET_ARN is not configured")
    now = time.monotonic()
    cached = _CACHE.get("credential")
    if isinstance(cached, dict) and now < _CACHE.get("expires", 0.0):
        return cached

    response = _secret_client().get_secret_value(SecretId=secret_id)
    payload = json.loads(response["SecretString"])
    if not isinstance(payload, dict):
        raise TypeError("Order Basic credential secret is malformed")

    # AgentCore injects `Basic <base64Credentials>` and this Lambda validates
    # that exact opaque payload without exposing it.
    if set(payload) != {"base64Credentials"}:
        raise RuntimeError("Order Basic credential secret is malformed")
    encoded = payload.get("base64Credentials")
    if not isinstance(encoded, str) or _decode_basic(f"Basic {encoded}") is None:
        raise RuntimeError("Order Basic credential secret is malformed")
    value = {"base64Credentials": encoded}

    _CACHE["credential"] = value
    _CACHE["expires"] = now + _cache_ttl_seconds()
    return value


def _header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted and isinstance(value, str):
            return value
    return ""


def _decode_basic(value: str) -> tuple[str, str] | None:
    prefix = "basic "
    if len(value) <= len(prefix) or value[:len(prefix)].lower() != prefix:
        return None
    try:
        decoded = base64.b64decode(value[len(prefix):].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator or not username or not password:
        return None
    return username, password


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _authenticated(event: dict[str, Any]) -> bool:
    header = _header(event, "Authorization")
    supplied = _decode_basic(header)
    if supplied is None:
        return False
    credential = _credential()
    supplied_payload = header[len("Basic "):].strip()
    return hmac.compare_digest(
        _digest(supplied_payload),
        _digest(credential["base64Credentials"]),
    )


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
            "schema_version": "1.0", "tool": "get_order_requirements",
            "status": "ORDER_NOT_FOUND", "order_id": order_id,
            "requirements": [], "read_only": True,
        }
    totals: defaultdict[tuple[str, str], int] = defaultdict(int)
    for line in order["lines"]:
        quantity = line["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return {
                "schema_version": "1.0", "tool": "get_order_requirements",
                "status": "INVALID_ORDER_DATA", "order_id": order_id,
                "error_code": "NON_POSITIVE_OR_NON_INTEGER_QUANTITY", "read_only": True,
            }
        totals[(line["sku"], line["warehouse_id"])] += quantity
    requirements = [
        {"sku": sku, "warehouse_id": warehouse_id, "required_quantity": totals[(sku, warehouse_id)]}
        for sku, warehouse_id in sorted(totals)
    ]
    return {
        "schema_version": "1.0", "tool": "get_order_requirements", "status": "SUCCESS",
        "order_id": order_id, "order_status": order["order_status"],
        "requirements": requirements,
        "requirements_checksum": _checksum(order_id, requirements),
        "read_only": True, "next_tool": "check_inventory_availability",
    }


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return _auth_error(401, "BASIC_AUTHENTICATION_REQUIRED")
    try:
        if not _authenticated(event):
            return _auth_error(401, "BASIC_AUTHENTICATION_REQUIRED")
    except (KeyError, TypeError, json.JSONDecodeError, RuntimeError):
        return _auth_error(500, "AUTH_CONFIGURATION_INVALID")
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
    return _response(400 if result["status"] == "INVALID_INPUT" else 200, result)
