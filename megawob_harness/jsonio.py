"""JSON answers from model text, prompt templates and atomic JSON writes."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import jsonschema

PROMPTS = Path(__file__).resolve().parent / "prompts"


def parse_json(text: str, schema: dict) -> dict:
    """The JSON object in `text` (from its first `{` to its last `}`) if it validates against `schema`.

    Raises ValueError otherwise. Models are asked for a bare object; this also accepts one wrapped in prose or a
    code fence.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in {text[-200:]!r}")
    try:
        out = json.loads(text[start:end + 1])
        jsonschema.validate(out, schema)
    except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
        raise ValueError(f"{type(exc).__name__}: {str(exc)[:200]}") from None
    return out


def schema_prompt(prompt: str, schema: dict) -> str:
    """`prompt` plus the instruction to answer with one object of `schema`.

    The schema goes in the prompt rather than in a provider's structured-output option: some reasoning models stall
    after their reasoning item when given a strict output schema, and local validation with a retry is as strict.
    """
    return (prompt.rstrip() + "\n\nReply with only one JSON object (no code fence, no other text) that validates "
            "against this JSON Schema:\n" + json.dumps(schema))


def fill(text: str, **values) -> str:
    """Fill `{name}` placeholders in one pass. Unlike str.format, other braces stay as they are, so briefs can quote
    code and JSON, and a value is never filled in again."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def prompt(name: str, **values) -> str:
    """prompts/<name>, filled."""
    return fill((PROMPTS / name).read_text(), **values)


def write_json(path: Path, obj) -> None:
    """Write `obj` as indented JSON, atomically (a reader never sees a half-written file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1, default=str)
        fh.write("\n")
    os.replace(tmp, path)
