from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.quality.openapi_contract import (  # noqa: E402
    OpenApiContractError,
    breaking_changes,
    canonical_json,
    current_document,
    load_document,
)


def _document(schema: dict[str, object] | None = None) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "fixture", "version": "1"},
        "paths": {
            "/health": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/items": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "Item": schema
                or {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "maxLength": 20}},
                }
            }
        },
    }


def test_canonical_json_is_stable_unicode_with_one_trailing_newline() -> None:
    value = _document()
    value["info"] = {"version": "1", "title": "fïxture"}

    encoded = canonical_json(value)

    assert "fïxture" in encoded
    assert encoded.endswith("\n")
    assert not encoded.endswith("\n\n")
    assert encoded.index('"title"') < encoded.index('"version"')


def test_breaking_changes_accepts_additive_paths_properties_and_enum_values() -> None:
    previous = _document({"type": "string", "enum": ["a"]})
    current = _document({"type": "string", "enum": ["a", "b"]})
    current["paths"]["/new"] = {"get": {"responses": {"200": {"description": "added"}}}}

    assert breaking_changes(previous, current) == ()


def test_breaking_changes_accepts_optional_body_but_rejects_new_required_body() -> None:
    previous = _document()
    current = json.loads(json.dumps(previous))
    current_operation = current["paths"]["/health"]["get"]
    current_operation["requestBody"] = {
        "required": False,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    assert breaking_changes(previous, current) == ()

    current_operation["requestBody"]["required"] = True
    assert "GET /health added required request body" in breaking_changes(previous, current)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value["paths"].pop("/items"), "removed path /items"),
        (
            lambda value: value["components"]["schemas"]["Item"]["properties"].pop("name"),
            "removed property name",
        ),
        (
            lambda value: value["components"]["schemas"]["Item"].update(
                {"required": ["name", "new"]}
            ),
            "added required properties",
        ),
        (
            lambda value: value["components"]["schemas"]["Item"]["properties"]["name"].update(
                {"maxLength": 10}
            ),
            "tightened maxLength",
        ),
    ],
)
def test_breaking_changes_rejects_conservative_contract_breaks(
    mutation: Callable[[dict[str, Any]], object], expected: str
) -> None:
    previous = _document()
    current = json.loads(json.dumps(previous))
    mutation(current)

    findings = breaking_changes(previous, current)

    assert any(expected in finding for finding in findings)


def test_load_document_rejects_malformed_and_oversized_input(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(OpenApiContractError, match="JSON object"):
        load_document(malformed)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(OpenApiContractError, match="8 MiB"):
        load_document(oversized)


@pytest.mark.parametrize(
    ("schema_update", "expected"),
    [
        ({"maxLength": 10}, "added maxLength"),
        ({"pattern": "^[a-z]+$"}, "changed pattern"),
        ({"enum": ["allowed"]}, "added restrictive enum"),
        ({"format": "uuid"}, "added format"),
        ({"additionalProperties": False}, "forbade additional properties"),
        ({"anyOf": [{"type": "string"}]}, "added restrictive anyOf"),
    ],
)
def test_breaking_changes_rejects_new_schema_restrictions(
    schema_update: dict[str, object], expected: str
) -> None:
    previous = _document({"type": "object"})
    current = json.loads(json.dumps(previous))
    current["components"]["schemas"]["Item"].update(schema_update)

    assert any(expected in finding for finding in breaking_changes(previous, current))


def test_breaking_changes_rejects_security_and_same_size_union_changes() -> None:
    previous = _document({"anyOf": [{"type": "string"}, {"type": "null"}]})
    current = json.loads(json.dumps(previous))
    current["components"]["schemas"]["Item"]["anyOf"] = [
        {"type": "integer"},
        {"type": "null"},
    ]
    current["paths"]["/health"]["get"]["security"] = [{"Bearer": []}]

    findings = breaking_changes(previous, current)

    assert "GET /health changed security requirements" in findings
    assert any("removed anyOf alternative type:string" in finding for finding in findings)


def test_breaking_changes_rejects_root_and_component_security_changes() -> None:
    previous = _document()
    previous["security"] = [{"Bearer": []}]
    previous["components"]["securitySchemes"] = {"Bearer": {"type": "http", "scheme": "bearer"}}
    current = json.loads(json.dumps(previous))
    current["security"] = []
    current["components"]["securitySchemes"]["Bearer"]["scheme"] = "basic"

    findings = breaking_changes(previous, current)

    assert "changed root security requirements" in findings
    assert "changed security scheme Bearer" in findings


def test_breaking_changes_accepts_relaxed_required_and_numeric_bounds() -> None:
    previous = _document(
        {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string", "maxLength": 10}},
        }
    )
    current = _document(
        {
            "type": "object",
            "required": [],
            "properties": {"name": {"type": "string", "maxLength": 20}},
        }
    )

    assert breaking_changes(previous, current) == ()


def test_api_contract_has_an_exact_minimal_public_surface() -> None:
    document = current_document()
    public_operations: set[tuple[str, str]] = set()
    protected_operations: set[tuple[str, str]] = set()
    methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
    for path, item in document["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in item.items():
            if method not in methods:
                continue
            target = (method.upper(), path)
            if operation.get("security"):
                protected_operations.add(target)
            else:
                public_operations.add(target)

    assert public_operations == {
        ("GET", "/api/v1/capabilities"),
        ("GET", "/api/v1/health/live"),
        ("GET", "/api/v1/health/ready"),
        ("GET", "/api/v1/version"),
    }
    assert protected_operations
    for method, path in protected_operations:
        assert document["paths"][path][method.lower()]["security"] == [{"HTTPBearer": []}]
    assert document["components"]["securitySchemes"] == {
        "HTTPBearer": {"scheme": "bearer", "type": "http"}
    }
