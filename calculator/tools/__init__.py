"""Trusted tools bundled in the image, never executable code supplied by users."""
import importlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from errors import Rejected


def discover():
    registry = {}
    for path in sorted(Path(__file__).parent.glob("*/tool.json")):
        tool_id = path.parent.name
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", tool_id):
            raise ValueError(f"Invalid bundled tool ID: {tool_id}")
        spec = json.loads(path.read_text())
        if spec["execution"] not in ("inline", "job"):
            raise ValueError("Tool execution must be inline or job")
        Draft202012Validator.check_schema(spec["inputs"])
        # External schema references are not supported (no network resolution).
        if any(f'"{key}"' in json.dumps(spec["inputs"]) for key in ("$ref", "$dynamicRef", "$recursiveRef")):
            raise ValueError("Tool input schemas must be self-contained")
        registry[tool_id] = {**spec, "id": tool_id}
    return registry


REGISTRY = discover()


def get(tool_id, inputs=None):
    if tool_id not in REGISTRY:
        raise Rejected("Tool not found", 404)
    spec = REGISTRY[tool_id]
    if inputs is not None:
        try:
            json.dumps(inputs, allow_nan=False)
            Draft202012Validator(spec["inputs"]).validate(inputs)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise Rejected("Invalid tool inputs", 422) from None
    return spec


def execute(tool_id, inputs, context):
    get(tool_id, inputs)
    # The path comes only from the validated registry, not a client import path.
    return importlib.import_module(f"tools.{tool_id}.tool").run(inputs, context)
