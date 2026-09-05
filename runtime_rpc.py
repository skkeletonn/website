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


def _render_evidence(args: dict[str, Any]):
    """Render the evidence scanner from state supplied by the client.

    The Roblox observation code remains local; the presentation/state-to-HTML
    function is deliberately server-owned as the first hybrid proof of
    concept. Only a small, validated state map crosses the RPC boundary.
    """
    states = args.get("states")
    if not isinstance(states, dict):
        raise ValueError("states must be an object")

    names = {
        "emf5": "EMF Level 5",
        "orbs": "Ghost Orbs",
        "fingerprints": "Fingerprints",
        "motion": "Paranormal Motion",
        "writing": "Ghostly Writing",
    }
    colors = {
        "gray": "rgb(140, 140, 140)",
        "green": "rgb(95, 235, 120)",
        "red": "rgb(255, 95, 95)",
    }
    lines = [
        '<font color="%s">Evidence will not appear instantly. Exit van for evidence to start appearing.</font>\\n'
        % colors["gray"]
    ]
    for key in ("emf5", "orbs", "fingerprints", "motion", "writing"):
        state = states.get(key)
        name = names[key]
        if state == "searching":
            line = '<font color="%s">(|) %s</font>' % (colors["gray"], name)
        elif state == "found":
            line = '<font color="%s">(✓) %s</font>' % (colors["green"], name)
        elif state == "not_found":
            line = '<font color="%s">(X) %s</font>' % (colors["red"], name)
        elif state == "waiting":
            equipment = ""
            if key == "motion":
                equipment = " - Place Motion Sensor where ghost is likely to walk"
            elif key == "writing":
                equipment = " - Place Book where ghost is likely to walk"
            line = '<font color="%s">( ) %s%s</font>' % (colors["gray"], name, equipment)
        else:
            line = '<font color="%s">( ) %s</font>' % (colors["gray"], name)
        lines.append(line)
    return {"content": "\\n".join(lines)}


def register_operation(name: str, handler: Callable[[dict[str, Any]], Any]) -> None:
    """Register trusted server code under a stable operation name."""
    if not isinstance(name, str) or not name or not name.replace("_", "").isalnum():
        raise ValueError("invalid RPC operation name")
    _OPERATIONS[name] = handler


register_operation("render_evidence", _render_evidence)


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
