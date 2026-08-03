"""Langfuse tracing wrapper - degrades to a no-op if Langfuse is unreachable
rather than crashing the pipeline.

Written against SDK v3's start_span()/update_trace() interface (OTel-based).
v2's trace()/span() API (previously used here) generates UUID-format trace
IDs; v3 generates 32-char hex OTel trace IDs. Traces created with the old v2
SDK against this project's Langfuse Cloud instance (server v4.2.0) were
retrievable via the legacy public REST API but never appeared in the v4 web
UI - a real incompatibility between SDK major versions and the server's
newer ClickHouse-backed read path, not a URL-format or caching issue (both
were independently ruled out first). v3's get_trace_url() also builds the
correct project-scoped URL directly, replacing manual construction."""
import json
import logging

from . import config

logger = logging.getLogger("tracing")

_langfuse = None
if config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY:
    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_HOST,
        )
    except Exception:
        logger.warning("Langfuse client unavailable, tracing disabled", exc_info=True)
        _langfuse = None


def _json_safe(value):
    if value is None:
        return None
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


class _NoopTrace:
    id = None

    def run_span(self, name, func, input=None):
        return func()

    def finish(self, output=None):
        return None


class _Trace:
    def __init__(self, root_span):
        self._span = root_span

    @property
    def id(self):
        return self._span.trace_id

    def run_span(self, name, func, input=None):
        child = self._span.start_span(name=name, input=_json_safe(input))
        try:
            result = func()
            child.update(output=_json_safe(result))
            return result
        except Exception:
            child.update(level="ERROR", status_message="exception during span")
            raise
        finally:
            child.end()

    def finish(self, output=None):
        try:
            if output is not None:
                self._span.update_trace(output=_json_safe(output))
            self._span.end()
            _langfuse.flush()
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)
        return self._span.trace_id


def start_trace(name: str, input: dict | None = None, metadata: dict | None = None):
    if _langfuse is None:
        return _NoopTrace()
    root_span = _langfuse.start_span(
        name=name, input=_json_safe(input), metadata=_json_safe(metadata or {})
    )
    return _Trace(root_span)


def get_trace_url(trace_id: str | None) -> str | None:
    if _langfuse is None or not trace_id:
        return None
    try:
        return _langfuse.get_trace_url(trace_id=trace_id)
    except Exception:
        logger.warning("get_trace_url failed", exc_info=True)
        return None
