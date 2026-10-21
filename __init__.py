"""Local web extract plugin for Hermes.

Fetches pages directly from this machine with ``httpx`` and reduces them to
readable text with ``trafilatura`` (Apache-2.0). No API key. Extract-only:
pair it with any search provider (ddgs, searxng, ...).

Loaded either as a package (relative import works) or by file path (no parent
package, so fall back to the sibling module).
"""

from __future__ import annotations

import logging

try:
    from .provider import LocalWebSearchProvider, _dependency_problem
except ImportError:  # pragma: no cover - path-load fallback
    from provider import LocalWebSearchProvider, _dependency_problem  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register the local extract provider with the plugin context."""
    problem = _dependency_problem()
    if problem is not None:
        # One error line, not one per URL: this is a packaging problem. The
        # provider still registers, so the message also reaches the tool
        # result; raising here would break discovery for the whole plugin set.
        logger.error("web-local is enabled but not usable: %s", problem)
    ctx.register_web_search_provider(LocalWebSearchProvider())
