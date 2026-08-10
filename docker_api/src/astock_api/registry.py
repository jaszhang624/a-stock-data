"""Function registry - whitelist of callable upstream functions."""
from typing import Dict, Any, Callable

# Registry: function_name → metadata
FUNCTIONS: Dict[str, Dict[str, Any]] = {}


def register(
    name: str,
    category: str,
    source: str,
    description: str,
    *,
    func: Callable[..., Any] | None = None,
    public: bool = True,
    requires_key: bool = False,
    rate_limit: str = "",
):
    """Register an upstream function in the whitelist.

    If public=True, func MUST be callable (fail-fast).
    """
    if public and not callable(func):
        raise RuntimeError(
            f"Public registry function '{name}' has no callable binding. "
            f"Pass func=<callable> or set public=False."
        )

    FUNCTIONS[name] = {
        "name": name,
        "function": func,
        "category": category,
        "source": source,
        "description": description,
        "public": public,
        "requires_key": requires_key,
        "rate_limit": rate_limit,
    }


def get_function(name: str):
    """Get a publicly callable function by name. Returns None if not found or not public."""
    entry = FUNCTIONS.get(name)
    if not entry:
        return None
    if not entry.get("public", False):
        return None
    func = entry.get("function")
    if not callable(func):
        return None
    return func


def list_functions():
    """List all PUBLIC registered functions (without the function objects)."""
    result = []
    for name, entry in FUNCTIONS.items():
        if not entry.get("public", False):
            continue
        result.append({
            "name": name,
            "category": entry["category"],
            "source": entry["source"],
            "description": entry["description"],
            "requires_key": entry.get("requires_key", False),
            "rate_limit": entry.get("rate_limit", ""),
        })
    return result


def is_available(name: str) -> bool:
    """Check if a function is available (registered, public, callable, and dependencies met)."""
    entry = FUNCTIONS.get(name)
    if not entry:
        return False
    if not entry.get("public", False):
        return False
    if not callable(entry.get("function")):
        return False
    if entry.get("requires_key"):
        from astock_api.config import IWENCAI_API_KEY
        if not IWENCAI_API_KEY:
            return False
    return True


def validate_registry():
    """Validate registry at startup. Raises if public entries lack callable bindings."""
    total = len(FUNCTIONS)
    public_entries = [n for n, e in FUNCTIONS.items() if e.get("public")]
    internal_entries = [n for n, e in FUNCTIONS.items() if not e.get("public")]
    unbound_public = [n for n, e in FUNCTIONS.items() if e.get("public") and not callable(e.get("function"))]

    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Registry validation: {total} total, {len(public_entries)} public, "
                f"{len(internal_entries)} internal, {len(unbound_public)} unbound public")

    if unbound_public:
        raise RuntimeError(f"Public registry entries without callable binding: {unbound_public}")

    return total, len(public_entries), len(internal_entries)
