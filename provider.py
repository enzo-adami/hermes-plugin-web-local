"""Local web extract — direct httpx fetch + trafilatura readability extraction.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. One
capability advertised:

- ``supports_search()``  -> False (pair with ddgs / searxng / brave-free)
- ``supports_extract()`` -> True  (fetch + readability, no API key)

Unlike the cloud extract providers (Firecrawl, Tavily, Exa, Parallel),
the HTTP request here originates from THIS machine — so the SSRF check
(:func:`tools.url_safety.is_safe_url`) and the website-policy gate
(:func:`tools.website_policy.check_website_access`) are load-bearing and
re-run on EVERY redirect hop, not just on the final URL. Redirects are
followed manually (``follow_redirects=False`` + a bounded hop loop) for
exactly this reason.

Readability extraction uses ``trafilatura`` (Apache-2.0; deps courlan /
htmldate Apache-2.0, jusText BSD-2, lxml BSD, urllib3 MIT — verified
2026-08-16). It is NOT vendored: plugin.yaml declares it for the plugin installer.
Only trafilatura's
in-memory extraction API is used — its own ``fetch_url`` networking is
never called; all HTTP goes through the SSRF-checked httpx path above.

Config keys this provider responds to::

    web:
      extract_backend: "local"     # explicit per-capability
      backend: "local"             # shared fallback (extract only)

Env vars: none — that is the point.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from time import monotonic
from typing import Any, Optional
from urllib.parse import urljoin

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url, create_ssrf_safe_client
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

# Per-URL wait timeout. Cancelling the await does not terminate its worker
# thread; in-flight fetch/extraction may continue until it returns.
_EXTRACT_TIMEOUT_SECS = 60

# The whole batch must return inside the caller's ``web.extract_timeout`` cap
# (tools/web_tools_extract.py), or completed items are lost with it.
_BATCH_MARGIN_SECS = 5.0


def _batch_budget_secs() -> Optional[float]:
    """Batch wall-clock budget: the caller's cap minus a margin; None when uncapped."""
    try:
        from tools.web_tools_extract import _extract_timeout_seconds
    except ImportError:  # Older or reorganised core: assume the documented default cap.
        def _extract_timeout_seconds() -> float:
            return 120.0

    cap = _extract_timeout_seconds()
    if cap <= 0:
        return None
    return cap - min(_BATCH_MARGIN_SECS, cap * 0.1)

# httpx inactivity timeout (connect/read/write/pool). A cooperative deadline
# is also checked between redirect hops and decoded body chunks. It does not
# preempt blocked DNS/I/O or readability extraction.
_FETCH_TIMEOUT_SECS = 30

_MAX_REDIRECTS = 5

# Body size cap — readable articles are far below this; the cap protects
# against feeding a multi-hundred-MB response to lxml on the agent host.
_MAX_CONTENT_BYTES = 10 * 1024 * 1024

# Content types we hand to trafilatura (or return as-is for text/plain).
# Anything else (PDF, images, archives) gets a typed error suggesting the
# browser tool instead.
_HTMLISH_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
)

# Some hosts refuse default library user agents outright; identify as a
# browser engine but keep an honest product token appended.
_USER_AGENT = (
    "HermesAgent-LocalExtract/1.0"
)

def _import_trafilatura() -> Any:
    """Import a PM-prepared dependency; never mutate the runtime environment."""
    import trafilatura
    return trafilatura


_TRAFILATURA_REMEDY = (
    "Install trafilatura into the environment of the RUNNING process — a "
    "dependency installed into another venv has no effect. Run:\n"
    "  python -c \"import sys; print(sys.executable)\"\n"
    "  uv pip install --python <that python> 'trafilatura>=2.0,<3'\n"
    "  (or: <that python> -m pip install 'trafilatura>=2.0,<3')\n"
    "No restart needed: the import happens on the next extract."
)


def _find_spec(name: str) -> Any:
    """Indirection so tests can simulate a missing module without uninstalling it."""
    import importlib.util

    if name in sys.modules:
        # An already-imported module wins: find_spec() raises for a module
        # object without a __spec__ (e.g. a test stub), and a None entry means
        # the import failed before.
        return sys.modules[name]
    return importlib.util.find_spec(name)


def _dependency_problem() -> Optional[str]:
    """Return an actionable diagnostic when trafilatura is missing, else None."""
    if _find_spec("trafilatura") is not None:
        return None
    return (
        "trafilatura is not installed in the Python running this agent "
        f"({sys.executable}). {_TRAFILATURA_REMEDY}"
    )


def _make_client() -> Any:
    """Build the httpx client used by the fetch loop.

    ``follow_redirects=False`` is deliberate — redirects are followed
    manually in :func:`_fetch_readable` so each hop is SSRF- and
    policy-checked before any request is issued.
    """
    import httpx

    return create_ssrf_safe_client(
        timeout=httpx.Timeout(_FETCH_TIMEOUT_SECS),
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "text/plain;q=0.8,*/*;q=0.5"
            ),
        },
    )


def _policy_error_item(url: str, blocked: dict[str, Any]) -> dict[str, Any]:
    """Build the standard blocked-by-policy result entry (firecrawl shape)."""
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "error": blocked["message"],
        "blocked_by_policy": {
            "host": blocked["host"],
            "rule": blocked["rule"],
            "source": blocked["source"],
        },
    }


def _fetch_readable(url: str) -> dict[str, Any]:
    """Fetch *url*, following at most ``_MAX_REDIRECTS`` manually.

    Every hop — including the first — is checked with
    :func:`is_safe_url` and :func:`check_website_access` BEFORE the
    request goes out. The body is streamed with a hard byte cap.

    Returns either::

        {"final_url": str, "content": bytes, "content_type": str,
         "encoding": str | None}

    or an error mapping (``final_url`` + ``error`` [+
    ``blocked_by_policy``]) that :func:`_extract_one` converts into a
    per-URL result entry.
    """
    deadline = monotonic() + _EXTRACT_TIMEOUT_SECS
    current = url
    with _make_client() as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            if monotonic() >= deadline:
                return {"final_url": current, "error": "Fetch deadline exceeded"}
            if not is_safe_url(current):
                logger.info("Blocked local web_extract for unsafe URL: %s", current)
                return {
                    "final_url": current,
                    "error": (
                        "Blocked: URL targets a private or internal "
                        "network address"
                    ),
                }
            blocked = check_website_access(current)
            if blocked:
                logger.info(
                    "Blocked local web_extract for %s by rule %s",
                    blocked["host"],
                    blocked["rule"],
                )
                return {
                    "final_url": current,
                    "error": blocked["message"],
                    "blocked_by_policy": blocked,
                }

            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        return {
                            "final_url": current,
                            "error": (
                                f"HTTP {response.status_code} redirect "
                                "without a Location header"
                            ),
                        }
                    current = urljoin(current, location)
                    continue

                if response.status_code >= 400:
                    return {
                        "final_url": current,
                        "error": f"HTTP {response.status_code} fetching URL",
                    }

                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )

                supported = (
                    not content_type
                    or content_type in _HTMLISH_CONTENT_TYPES
                    or content_type == "text/plain"
                )
                if not supported:
                    return {
                        "final_url": current,
                        "error": (
                            f"Unsupported content type '{content_type}' — "
                            "the local extract backend handles HTML/XML/plain "
                            "text. Try browser_navigate for other formats."
                        ),
                    }

                body = bytearray()
                for chunk in response.iter_bytes():
                    if monotonic() >= deadline:
                        return {"final_url": current, "error": "Fetch deadline exceeded"}
                    body.extend(chunk)
                    if len(body) > _MAX_CONTENT_BYTES:
                        return {
                            "final_url": current,
                            "error": (
                                "Response exceeds the "
                                f"{_MAX_CONTENT_BYTES // (1024 * 1024)}MB "
                                "local extract limit"
                            ),
                        }

                encoding: Optional[str] = None
                try:
                    encoding = response.charset_encoding
                except Exception:  # Header parsing quirk.
                    encoding = None

                return {
                    "final_url": current,
                    "content": bytes(body),
                    "content_type": content_type,
                    "encoding": encoding,
                }

    # Loop exhausted without a non-redirect response.
    return {
        "final_url": current,
        "error": f"Exceeded {_MAX_REDIRECTS} redirects",
    }


def _metadata_dict(trafilatura: Any, content: bytes, final_url: str) -> dict[str, Any]:
    """Extract page metadata into the legacy ``metadata`` mapping shape."""
    metadata: dict[str, Any] = {"sourceURL": final_url, "title": ""}
    try:
        doc = trafilatura.extract_metadata(content, default_url=final_url)
    except Exception:  # Metadata is best-effort.
        return metadata
    if doc is None:
        return metadata
    for field in ("title", "author", "description", "sitename", "date"):
        value = getattr(doc, field, None)
        if value:
            metadata[field] = str(value)
    metadata.setdefault("title", "")
    return metadata


def _extract_one(url: str, format: Optional[str]) -> dict[str, Any]:
    """Fetch + extract a single URL synchronously (runs in a thread).

    Never raises for per-URL conditions the caller should surface as a
    result entry — network errors, policy blocks, unsupported types, and
    empty extractions all come back as ``{"url", ..., "error"}`` items.
    Only programming errors escape.
    """
    import httpx

    try:
        fetched = _fetch_readable(url)
    except httpx.HTTPError as exc:
        return {
            "url": url,
            "title": "",
            "content": "",
            "raw_content": "",
            "error": f"Fetch failed: {exc}",
        }

    final_url = fetched.get("final_url", url)
    if "error" in fetched:
        blocked = fetched.get("blocked_by_policy")
        if blocked:
            return _policy_error_item(final_url, blocked)
        return {
            "url": final_url,
            "title": "",
            "content": "",
            "raw_content": "",
            "error": fetched["error"],
        }

    content: bytes = fetched["content"]
    content_type: str = fetched["content_type"]

    if content_type == "text/plain":
        try:
            text = content.decode(fetched.get("encoding") or "utf-8", errors="replace")
        except LookupError:
            # A malformed charset header must not discard a readable body.
            text = content.decode("utf-8", errors="replace")
        return {
            "url": final_url,
            "title": "",
            "content": text,
            "raw_content": text,
            "metadata": {"sourceURL": final_url, "title": ""},
        }

    try:
        trafilatura = _import_trafilatura()
    except ImportError as exc:
        return {
            "url": final_url,
            "title": "",
            "content": "",
            "raw_content": "",
            "error": _dependency_problem() or (
                f"trafilatura import failed in {sys.executable}: {exc}"
            ),
        }

    output_format = "html" if format == "html" else "markdown"
    try:
        text = trafilatura.extract(
            content,
            url=final_url,
            output_format=output_format,
            include_comments=False,
            include_tables=True,
        )
        if not text:
            # Non-article pages (index/nav-heavy) — fall back to a full
            # text rendering rather than returning nothing.
            text = trafilatura.html2txt(content)
    except Exception as exc:  # lxml/encoding edge cases.
        return {
            "url": final_url,
            "title": "",
            "content": "",
            "raw_content": "",
            "error": f"Extraction failed: {exc}",
        }

    if not text or not text.strip():
        return {
            "url": final_url,
            "title": "",
            "content": "",
            "raw_content": "",
            "error": (
                "No readable content could be extracted from this page — "
                "it may be script-rendered. Try browser_navigate instead."
            ),
        }

    metadata = _metadata_dict(trafilatura, content, final_url)
    return {
        "url": final_url,
        "title": metadata.get("title", ""),
        "content": text,
        "raw_content": text,
        "metadata": metadata,
    }


class LocalWebSearchProvider(WebSearchProvider):
    """Keyless local extract provider (httpx fetch + trafilatura)."""

    @property
    def name(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        return "Local (httpx + trafilatura)"

    def is_available(self) -> bool:
        """Report actual dependency availability, never an intended installation.

        The probe answers "is it installed"; the import answers "does it work".
        A present-but-broken install must not be advertised as available.
        """
        if _dependency_problem() is not None:
            return False
        try:
            _import_trafilatura()
        except ImportError:
            return False
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Extract readable content from one or more URLs locally.

        Async; URLs run fetch + extraction sequentially in background threads,
        each waited at most ``_EXTRACT_TIMEOUT_SECS`` and all within one batch
        budget derived from the caller's ``web.extract_timeout``. This retains
        completed items before the caller's timeout; it does not cancel threads.
        Per-URL failures become
        items with an ``error`` field rather than raising.

        Accepted kwargs (others ignored for forward compat):
          - ``format``: ``"markdown"`` (default) or ``"html"`` — mapped to
            trafilatura's output format; both are readability-reduced.
        """
        from tools.interrupt import is_interrupted as _is_interrupted

        if _is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        # A missing dependency is a packaging problem, not a per-URL one: report
        # it before any network access instead of fetching and failing later.
        problem = _dependency_problem()
        if problem is not None:
            logger.error("Local extract unavailable: %s", problem)
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": problem,
                }
                for url in urls
            ]

        format = kwargs.get("format")
        results: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        budget = _batch_budget_secs()
        deadline = None if budget is None else loop.time() + budget

        for url in urls:
            if _is_interrupted():
                results.append({"url": url, "error": "Interrupted", "title": ""})
                continue

            logger.info("Local extract: %s", url)
            timeout = _EXTRACT_TIMEOUT_SECS
            budget_limited = False
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining < timeout:
                    # The batch deadline ends this wait, not the per-URL cap:
                    # the page never got its full allowance.
                    timeout = remaining
                    budget_limited = True
            # Decide the reason BEFORE waiting: comparing the timeout
            # afterwards mislabels a clamped wait as a per-URL timeout.
            if budget_limited:
                reason = f"Extract batch time budget exhausted ({budget:g}s)"
            else:
                reason = f"Extract timed out after {_EXTRACT_TIMEOUT_SECS}s"
            try:
                if timeout <= 0:
                    raise TimeoutError
                item = await asyncio.wait_for(
                    asyncio.to_thread(_extract_one, url, format),
                    timeout=timeout,
                )
            except TimeoutError:
                logger.warning("Local extract timed out for %s", url)
                item = {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": (
                        f"{reason} — page may be too large or unresponsive. Try "
                        "browser_navigate instead."
                    ),
                }
            except Exception as exc:  # Per-URL isolation.
                logger.warning("Local extract failed for %s: %s", url, exc)
                item = {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": str(exc),
                }
            results.append(item)

        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Local (httpx + trafilatura)",
            "badge": "free · no key · extract only",
            "tag": (
                "Fetches pages from this machine and extracts readable "
                "text with trafilatura — pair with any search provider."
            ),
            "env_vars": [],
        }
