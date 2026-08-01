"""Langfuse tracing wrapper. Degrades to a no-op if Langfuse is unreachable
(e.g. the `langfuse` Compose profile isn't up) rather than crashing the
pipeline - tracing is required for judging credit, not for the pipeline to
function during dev.

Every trace and span carries explicit input/output, not just metadata - a
judge opening the top-level trace should immediately see what went in and
what came out, without having to drill into every child span.

NOTE: written against the Langfuse Python SDK v2 trace()/span() API
(langfuse==2.53.9 in requirements.txt). Verify method signatures against
whatever version actually gets installed - the SDK's tracing API has moved
between major versions.
"""
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
    def __init__(self, lf_trace):
        self._trace = lf_trace

    @property
    def id(self):
        return self._trace.id

    def run_span(self, name, func, input=None):
        span = self._trace.span(name=name, input=_json_safe(input))
        try:
            result = func()
            span.end(output=_json_safe(result))
            return result
        except Exception:
            span.end(level="ERROR", status_message="exception during span")
            raise

    def finish(self, output=None):
        try:
            if output is not None:
                self._trace.update(output=_json_safe(output))
            _langfuse.flush()
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)
        return self._trace.id


def start_trace(name: str, input: dict | None = None, metadata: dict | None = None):
    if _langfuse is None:
        return _NoopTrace()
    lf_trace = _langfuse.trace(name=name, input=_json_safe(input), metadata=_json_safe(metadata or {}))
    return _Trace(lf_trace)
