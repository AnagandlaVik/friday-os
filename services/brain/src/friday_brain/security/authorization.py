import hashlib
import json
from typing import Any

from pydantic_core import to_jsonable_python


def digest_tool_arguments(
    arguments: dict[str, Any],
) -> str:
    """
    Return a deterministic SHA-256 digest for tool arguments.

    Sorting and compact separators ensure semantically identical mappings
    produce the same digest regardless of their original key order.
    """
    normalized = to_jsonable_python(arguments)

    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
