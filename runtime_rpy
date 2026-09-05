"""Small, allowlisted server-side operation dispatcher.

This intentionally never accepts Lua source or arbitrary code from a client.
Future sensitive pure operations are registered here as named handlers and are
called in batches by the client-side runtime.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from runtime_bundles import authorize_runtime_bundle

logger = logging.getLogger(__name__)

_MAX_BATCH_OPERATIONS = 32
_OPERATIONS: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register_operation(name: str, handler: Callable[[dict[str, Any]], Any]) -> None:
    """Register trusted server code under a stable operation name."""
    if not isinstance(name, str) or not name or not name.replace("_", "").isalnum():
        raise ValueError("invalid RPC operation name")
    _OPERATIONS[name] = handler


def enabled_operations() -> list[str]:
    return sorted(_OPERATIONS)


def handle_batch(artifact_id: str, access_token: str, operations):
    """Authorize and execute a bounded batch of allowlisted operations."""
    if not authorize_runtime_bundle(artifact_id, access_token):
        return {"ok": False, "error": "Unauthorized or expired artifact"}, 403
    if not isinstance(operations, list) or not operations:
        return {"ok": False, "error": "operations must be a non-empty array"}, 400
    if len(operations) > _MAX_BATCH_OPERATIONS:
        return {"ok": False, "error": "Too many operations in one batch"}, 400

    results = []
    for item in operations:
        if not isinstance(item, dict):
            return {"ok": False, "error": "Each operation must be an object"}, 400
        name = item.get("name")
        args = item.get("args", {})
        handler = _OPERATIONS.get(name)
        if handler is None:
            return {"ok": False, "error": f"Unknown operation: {name}"}, 400
        if not isinstance(args, dict):
            return {"ok": False, "error": "Operation args must be an object"}, 400
        try:
            results.append({"name": name, "ok": True, "result": handler(args)})
        except Exception:
            logger.exception("Runtime RPC operation failed: %s", name)
            return {"ok": False, "error": f"Operation failed: {name}"}, 500

    return {"ok": True, "results": results}, 200
