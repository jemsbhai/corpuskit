"""Generate and conservatively compare the committed CorpusKit OpenAPI contract."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


class OpenApiContractError(ValueError):
    """Raised when an OpenAPI document is missing, malformed, stale, or incompatible."""


def current_document() -> dict[str, Any]:
    """Build the deterministic test-profile contract without opening external resources."""

    from corpuskit.api.app import create_app
    from corpuskit.config import Settings

    application = create_app(Settings(environment="test", _env_file=None))
    value = application.openapi()
    _validate_document(value)
    return value


def canonical_json(value: Mapping[str, Any]) -> str:
    """Encode a contract with stable key ordering and one trailing newline."""

    _validate_document(value)
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_document(path: Path) -> dict[str, Any]:
    """Load one bounded local JSON object and validate the OpenAPI envelope."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OpenApiContractError(f"OpenAPI contract is not readable: {path}") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise OpenApiContractError("OpenAPI contract exceeds the 8 MiB policy limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenApiContractError("OpenAPI contract is not valid UTF-8 JSON") from exc
    _validate_document(value)
    return cast(dict[str, Any], value)


def breaking_changes(previous: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, ...]:
    """Return conservative backward-incompatibilities between two OpenAPI documents."""

    _validate_document(previous)
    _validate_document(current)
    findings: list[str] = []
    if previous.get("security") != current.get("security") and (
        "security" in previous or "security" in current
    ):
        findings.append("changed root security requirements")
    old_paths = _mapping(previous.get("paths"), "paths")
    new_paths = _mapping(current.get("paths"), "paths")
    for path, old_path_value in sorted(old_paths.items()):
        if path not in new_paths:
            findings.append(f"removed path {path}")
            continue
        old_path = _mapping(old_path_value, f"paths.{path}")
        new_path = _mapping(new_paths[path], f"paths.{path}")
        for method in sorted(HTTP_METHODS.intersection(old_path)):
            if method not in new_path:
                findings.append(f"removed operation {method.upper()} {path}")
                continue
            _compare_operation(
                path,
                method,
                old_path,
                new_path,
                findings,
            )

    old_schemas = _component_schemas(previous)
    new_schemas = _component_schemas(current)
    for name, old_schema in sorted(old_schemas.items()):
        location = f"components.schemas.{name}"
        if name not in new_schemas:
            findings.append(f"removed schema {name}")
            continue
        _compare_schema(old_schema, new_schemas[name], location, findings)
    old_security_schemes = _component_security_schemes(previous)
    new_security_schemes = _component_security_schemes(current)
    for name, old_scheme in sorted(old_security_schemes.items()):
        if name not in new_security_schemes:
            findings.append(f"removed security scheme {name}")
        elif old_scheme != new_security_schemes[name]:
            findings.append(f"changed security scheme {name}")
    return tuple(dict.fromkeys(findings))


def _validate_document(value: object) -> None:
    if not isinstance(value, dict):
        raise OpenApiContractError("OpenAPI contract must be a JSON object")
    version = value.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        raise OpenApiContractError("OpenAPI contract must declare an OpenAPI 3.x version")
    paths = value.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise OpenApiContractError("OpenAPI contract must contain at least one path")


def _mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise OpenApiContractError(f"{location} must be an object")
    return value


def _component_schemas(document: Mapping[str, Any]) -> Mapping[str, Any]:
    components = document.get("components", {})
    if not isinstance(components, dict):
        raise OpenApiContractError("components must be an object")
    schemas = components.get("schemas", {})
    return _mapping(schemas, "components.schemas")


def _component_security_schemes(document: Mapping[str, Any]) -> Mapping[str, Any]:
    components = document.get("components", {})
    if not isinstance(components, dict):
        raise OpenApiContractError("components must be an object")
    schemes = components.get("securitySchemes", {})
    return _mapping(schemes, "components.securitySchemes")


def _compare_operation(
    path: str,
    method: str,
    old_path: Mapping[str, Any],
    new_path: Mapping[str, Any],
    findings: list[str],
) -> None:
    location = f"{method.upper()} {path}"
    old_operation = _mapping(old_path[method], location)
    new_operation = _mapping(new_path[method], location)
    if old_operation.get("security") != new_operation.get("security") and (
        "security" in old_operation or "security" in new_operation
    ):
        findings.append(f"{location} changed security requirements")
    old_parameters = _parameters(old_path, old_operation, location)
    new_parameters = _parameters(new_path, new_operation, location)
    for identity, old_parameter in old_parameters.items():
        if identity not in new_parameters:
            findings.append(f"{location} removed parameter {identity[0]}:{identity[1]}")
            continue
        _compare_schema(
            old_parameter.get("schema", {}),
            new_parameters[identity].get("schema", {}),
            f"{location} parameter {identity[0]}:{identity[1]}",
            findings,
        )
        if old_parameter.get("required") is not True and (
            new_parameters[identity].get("required") is True
        ):
            findings.append(f"{location} made parameter {identity[0]}:{identity[1]} required")
    for identity, new_parameter in new_parameters.items():
        if identity not in old_parameters and new_parameter.get("required") is True:
            findings.append(f"{location} added required parameter {identity[0]}:{identity[1]}")

    old_request = old_operation.get("requestBody")
    new_request = new_operation.get("requestBody")
    if old_request is None and isinstance(new_request, dict):
        if new_request.get("required") is True:
            findings.append(f"{location} added required request body")
    elif old_request is not None and new_request is None:
        findings.append(f"{location} removed request body contract")
    elif isinstance(old_request, dict) and isinstance(new_request, dict):
        if old_request.get("required") is not True and new_request.get("required") is True:
            findings.append(f"{location} made request body required")
        _compare_content(old_request, new_request, f"{location} request", findings)
    elif old_request is not None or new_request is not None:
        raise OpenApiContractError(f"{location} requestBody must be an object")

    old_responses = _mapping(old_operation.get("responses", {}), f"{location} responses")
    new_responses = _mapping(new_operation.get("responses", {}), f"{location} responses")
    for status, old_response in old_responses.items():
        if status not in new_responses:
            findings.append(f"{location} removed response {status}")
            continue
        if isinstance(old_response, dict) and isinstance(new_responses[status], dict):
            _compare_content(
                old_response,
                new_responses[status],
                f"{location} response {status}",
                findings,
            )


def _parameters(
    path_item: Mapping[str, Any], operation: Mapping[str, Any], location: str
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for owner, values in (
        ("path", path_item.get("parameters", [])),
        ("operation", operation.get("parameters", [])),
    ):
        if not isinstance(values, list):
            raise OpenApiContractError(f"{location} {owner} parameters must be an array")
        for value in values:
            parameter = _mapping(value, f"{location} parameter")
            name = parameter.get("name")
            position = parameter.get("in")
            if not isinstance(name, str) or not isinstance(position, str):
                raise OpenApiContractError(f"{location} parameter requires string name/in")
            result[(position, name)] = parameter
    return result


def _compare_content(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    location: str,
    findings: list[str],
) -> None:
    old_content = _mapping(previous.get("content", {}), f"{location}.content")
    new_content = _mapping(current.get("content", {}), f"{location}.content")
    for media_type, old_media in old_content.items():
        if media_type not in new_content:
            findings.append(f"{location} removed media type {media_type}")
            continue
        old_media_value = _mapping(old_media, f"{location}.{media_type}")
        new_media_value = _mapping(new_content[media_type], f"{location}.{media_type}")
        _compare_schema(
            old_media_value.get("schema", {}),
            new_media_value.get("schema", {}),
            f"{location} {media_type}",
            findings,
        )


def _compare_schema(previous: object, current: object, location: str, findings: list[str]) -> None:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        if previous != current:
            findings.append(f"{location} changed schema shape")
        return
    old_ref = previous.get("$ref")
    new_ref = current.get("$ref")
    if old_ref != new_ref and (old_ref is not None or new_ref is not None):
        findings.append(f"{location} changed schema reference")
        return
    old_type = previous.get("type")
    new_type = current.get("type")
    if old_type != new_type and old_type is not None:
        findings.append(f"{location} changed type from {old_type!r} to {new_type!r}")
    for key in ("format", "const"):
        if key in previous and previous.get(key) != current.get(key):
            findings.append(f"{location} changed {key}")
        elif key not in previous and key in current:
            findings.append(f"{location} added {key}")
    old_enum = previous.get("enum")
    new_enum = current.get("enum")
    if old_enum is None and isinstance(new_enum, list):
        findings.append(f"{location} added restrictive enum")
    elif isinstance(old_enum, list) and isinstance(new_enum, list):
        removed = [item for item in old_enum if item not in new_enum]
        if removed:
            findings.append(f"{location} narrowed enum by removing {removed!r}")
    _compare_limits(previous, current, location, findings)

    old_required = previous.get("required", [])
    new_required = current.get("required", [])
    if isinstance(old_required, list) and isinstance(new_required, list):
        added_required = sorted(set(new_required).difference(old_required))
        if added_required:
            findings.append(f"{location} added required properties {added_required!r}")
    old_properties = previous.get("properties", {})
    new_properties = current.get("properties", {})
    if isinstance(old_properties, dict) and isinstance(new_properties, dict):
        for name, old_property in old_properties.items():
            if name not in new_properties:
                findings.append(f"{location} removed property {name}")
                continue
            _compare_schema(old_property, new_properties[name], f"{location}.{name}", findings)
    if "items" in previous and "items" in current:
        _compare_schema(previous["items"], current["items"], f"{location}.items", findings)
    _compare_alternatives(previous, current, "anyOf", location, findings)
    _compare_alternatives(previous, current, "oneOf", location, findings)

    old_additional = previous.get("additionalProperties", True)
    new_additional = current.get("additionalProperties", True)
    if old_additional is not False and new_additional is False:
        findings.append(f"{location} forbade additional properties")
    elif not isinstance(old_additional, dict) and isinstance(new_additional, dict):
        findings.append(f"{location} constrained additional properties")
    elif isinstance(old_additional, dict) and isinstance(new_additional, dict):
        _compare_schema(
            old_additional,
            new_additional,
            f"{location}.additionalProperties",
            findings,
        )


def _compare_alternatives(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    keyword: str,
    location: str,
    findings: list[str],
) -> None:
    old_alternatives = previous.get(keyword)
    new_alternatives = current.get(keyword)
    if old_alternatives is None:
        if isinstance(new_alternatives, list):
            findings.append(f"{location} added restrictive {keyword}")
        return
    if not isinstance(old_alternatives, list) or not isinstance(new_alternatives, list):
        findings.append(f"{location} changed {keyword} alternatives")
        return
    available = list(enumerate(new_alternatives))
    for index, old_alternative in enumerate(old_alternatives):
        identity = _alternative_identity(old_alternative)
        match = next(
            (
                (position, candidate)
                for position, candidate in available
                if _alternative_identity(candidate) == identity
            ),
            None,
        )
        if match is None:
            findings.append(f"{location} removed {keyword} alternative {identity}")
            continue
        available.remove(match)
        _compare_schema(
            old_alternative,
            match[1],
            f"{location}.{keyword}[{index}]",
            findings,
        )


def _alternative_identity(value: object) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    reference = value.get("$ref")
    if isinstance(reference, str):
        return f"ref:{reference}"
    schema_type = value.get("type")
    if isinstance(schema_type, str):
        return f"type:{schema_type}"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _compare_limits(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    location: str,
    findings: list[str],
) -> None:
    tighter_high = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
    tighter_low = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
    for key in tighter_high:
        old = previous.get(key)
        new = current.get(key)
        if old is None and isinstance(new, (int, float)):
            findings.append(f"{location} added {key} {new}")
        elif isinstance(old, (int, float)) and isinstance(new, (int, float)) and new < old:
            findings.append(f"{location} tightened {key} from {old} to {new}")
    for key in tighter_low:
        old = previous.get(key)
        new = current.get(key)
        if old is None and isinstance(new, (int, float)):
            findings.append(f"{location} added {key} {new}")
        elif isinstance(old, (int, float)) and isinstance(new, (int, float)) and new > old:
            findings.append(f"{location} tightened {key} from {old} to {new}")
    if previous.get("pattern") != current.get("pattern") and (
        "pattern" in previous or "pattern" in current
    ):
        findings.append(f"{location} changed pattern")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write = subparsers.add_parser("write", help="write the current canonical contract")
    write.add_argument("output", type=Path)
    check = subparsers.add_parser("check", help="require the snapshot to match the application")
    check.add_argument("snapshot", type=Path)
    compare = subparsers.add_parser("compare", help="reject conservative breaking changes")
    compare.add_argument("previous", type=Path)
    compare.add_argument("current", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "write":
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(canonical_json(current_document()), encoding="utf-8")
        return 0
    if arguments.command == "check":
        expected = canonical_json(load_document(arguments.snapshot))
        actual = canonical_json(current_document())
        if expected != actual:
            sys.stderr.write("Committed OpenAPI contract is stale; regenerate it explicitly.\n")
            return 1
        return 0
    findings = breaking_changes(
        load_document(arguments.previous),
        load_document(arguments.current),
    )
    if findings:
        sys.stderr.write("Backward-incompatible OpenAPI changes:\n")
        sys.stderr.write("".join(f"- {finding}\n" for finding in findings))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CI entrypoint
    raise SystemExit(main())
