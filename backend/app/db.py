import threading

import clickhouse_connect

from . import config

# Thread-local, not a module-level singleton: FastAPI runs sync endpoint
# functions in a thread pool, so concurrent requests can land in different
# threads at the same time. clickhouse_connect's client isn't safe to share
# across concurrent queries - a single shared client raised
# "Attempt to execute concurrent queries within the same session" under real
# concurrent load (found by actually running the stack with overlapping
# requests, not by inspection). One client per thread avoids that while
# still reusing a connection across requests handled by the same thread.
_local = threading.local()


def get_ro_client():
    if getattr(_local, "ro_client", None) is None:
        _local.ro_client = clickhouse_connect.get_client(
            host=config.CLICKHOUSE_HOST,
            port=config.CLICKHOUSE_PORT,
            username=config.CLICKHOUSE_READONLY_USER,
            password=config.CLICKHOUSE_READONLY_PASSWORD,
            database=config.CLICKHOUSE_DATABASE,
        )
    return _local.ro_client


def get_admin_client():
    """Deterministic-write path only (see config.py) - never handed to LLM code."""
    if getattr(_local, "admin_client", None) is None:
        _local.admin_client = clickhouse_connect.get_client(
            host=config.CLICKHOUSE_HOST,
            port=config.CLICKHOUSE_PORT,
            username=config.CLICKHOUSE_ADMIN_USER,
            password=config.CLICKHOUSE_ADMIN_PASSWORD,
            database=config.CLICKHOUSE_DATABASE,
        )
    return _local.admin_client
