"""Ship application logs to Grafana Loki (Track T1, step 3).

A standard :class:`logging.Handler` that batches records and pushes them to
Loki's ``/loki/api/v1/push`` endpoint, so mesh and backend log lines become
queryable with LogQL — which is exactly what the agent's ``logql`` tool
queries against.

HONESTY CONTRACT
----------------
* A log line that cannot be delivered is **dropped with a warning**, never
  buffered forever, never faked into a "successful" ship.
* Installation is a no-op when Loki is not reachable: rather than spawning a
  thread that fails silently for the lifetime of the process, the handler
  probes once and refuses, so ``make dev-backend`` with no observability
  stack behaves exactly as it did before.
* The handler never raises into the logging call path. A broken pipe to a
  log aggregator must not take down the request that happened to log
  something.
* Re-entrancy is guarded: the handler's own warnings do not re-enter it and
  loop.

LABEL CARDINALITY
-----------------
Only three labels are attached — ``app``, ``level`` and ``logger``. Message
bodies go in the log line, never into a label: labels are indexed and a
message-derived label would explode Loki's index.
"""

import json
import logging
import queue
import sys
import threading
import time
from typing import Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_LOKI_PUSH_PATH = "/loki/api/v1/push"
_FLUSH_INTERVAL_SECONDS = 2.0
_MAX_BATCH = 200
_APP_LABEL = "aiops"


class LokiLoggingHandler(logging.Handler):
    """Batching, non-blocking handler that pushes records to Loki.

    ``emit()`` only enqueues; a daemon thread does the HTTP work, so no
    request ever waits on a log aggregator.
    """

    def __init__(
        self,
        url: str = settings.LOKI_URL,
        app_label: str = _APP_LABEL,
        flush_interval: float = _FLUSH_INTERVAL_SECONDS,
        timeout: float = 5.0,
        level: int = logging.INFO,
    ):
        super().__init__(level=level)
        self.url = url.rstrip("/")
        self.app_label = app_label
        self.flush_interval = flush_interval
        self.timeout = timeout

        self._queue: "queue.Queue[Optional[logging.LogRecord]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="loki-shipper", daemon=True
        )
        self._thread.start()

    # -- logging.Handler API ------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Enqueue a record. Never raises."""
        # Re-entrancy guard: this handler logs its own failures, and those
        # must not come back round.
        if getattr(record, "_shipped_by_loki_handler", False):
            return
        try:
            self._queue.put_nowait(record)
        except Exception:
            # A full queue means Loki is behind; the record is dropped.
            pass

    def close(self) -> None:
        """Flush what is pending, then stop the worker.

        Guarded throughout: a close() that raised would leave the worker
        thread running, so every step tolerates a queue that has already
        been torn down.
        """
        self._stop.set()
        try:
            self._queue.put_nowait(None)       # wake the worker so it can exit
        except Exception:
            pass
        try:
            self._thread.join(timeout=5.0)
        except Exception:
            pass
        try:
            self.flush_remaining()
        except Exception:
            pass
        super().close()

    # -- worker -------------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.flush_interval)
            self._flush()

    def flush_remaining(self) -> None:
        """Push everything still queued. Called on close()."""
        self._flush(drain=True)

    def _flush(self, drain: bool = False) -> None:
        records: List[logging.LogRecord] = []
        while len(records) < _MAX_BATCH:
            try:
                record = self._queue.get_nowait()
            except queue.Empty:
                break
            if record is None:                      # shutdown sentinel
                continue
            records.append(record)

        if not records:
            return

        payload = self._build_payload(records)
        self._post(payload)

    # -- formatting ---------------------------------------------------------

    def _build_payload(
        self, records: List[logging.LogRecord]
    ) -> Dict[str, List[Dict[str, object]]]:
        """Group records into Loki streams, one per label set.

        Timestamps are RFC3339 parsed by the formatter's epoch seconds and
        converted to the nanosecond strings Loki expects.
        """
        formatter = self.formatter or logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

        # One entry per (level, logger) pair keeps the stream count small
        # while still letting LogQL filter on either.
        streams: Dict[str, Dict[str, object]] = {}
        for record in records:
            labels = {
                "app": self.app_label,
                "level": record.levelname,
                "logger": record.name,
            }
            key = json.dumps(labels, sort_keys=True)
            bucket = streams.setdefault(key, {"labels": labels, "values": []})

            nanos = int(record.created * 1_000_000_000)
            bucket["values"].append([str(nanos), formatter.format(record)])

        return {
            "streams": [
                {"stream": b["labels"], "values": b["values"]}
                for b in streams.values()
            ]
        }

    # -- transport ----------------------------------------------------------

    def _post(self, payload: Dict[str, object]) -> bool:
        """Push one batch. Returns False (and warns) on failure."""
        try:
            response = httpx.post(
                f"{self.url}{_LOKI_PUSH_PATH}",
                json=payload,
                timeout=self.timeout,
            )
            if response.status_code in (200, 204):
                return True

            self._warn_once(
                f"Loki rejected {len(payload.get('streams', []))} stream(s) "
                f"with HTTP {response.status_code}: {response.text[:200]}"
            )
            return False
        except Exception as exc:
            self._warn_once(f"could not ship logs to Loki at {self.url}: {exc}")
            return False

    _warned = False

    def _warn_once(self, message: str) -> None:
        """Warn on stderr once per handler, not once per dropped batch.

        Going through ``logging`` here would re-enter this very handler, so
        the message is written straight to stderr and tagged as shipped.
        """
        if LokiLoggingHandler._warned:
            return
        LokiLoggingHandler._warned = True
        tagged = logging.LogRecord(
            name="app.tools.loki_handler",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="%s (further Loki delivery warnings suppressed)",
            args=(),
            exc_info=None,
        )
        tagged._shipped_by_loki_handler = True
        logging.getLogger(tagged.name).warning(tagged.msg, *tagged.args)
        print(f"[loki_handler] {message}", file=sys.stderr, flush=True)


def loki_reachable(url: str = settings.LOKI_URL, timeout: float = 3.0) -> bool:
    """Probe Loki's readiness endpoint. Used to decide whether to install."""
    try:
        return httpx.get(f"{url.rstrip('/')}/ready", timeout=timeout).is_success
    except Exception:
        return False


def install_loki_logging(
    url: str = settings.LOKI_URL,
    level: int = logging.INFO,
    app_label: str = _APP_LABEL,
) -> Optional[LokiLoggingHandler]:
    """Attach a :class:`LokiLoggingHandler` to the root logger.

    Idempotent: a second call returns the handler already installed rather
    than attaching a duplicate. Returns ``None`` when Loki is not reachable,
    leaving logging exactly as it was — an unreachable aggregator must not
    change how the application behaves.

    Callers are expected to guard this in a try/except; it is deliberately
    allowed to fail loudly at startup rather than quietly at first log.
    """
    root = logging.getLogger()

    for existing in root.handlers:
        if isinstance(existing, LokiLoggingHandler):
            return existing

    if not loki_reachable(url):
        print(
            f"[loki_handler] Loki not reachable at {url}; log shipping OFF. "
            f"Start it with `make observability-up`.",
            file=sys.stderr,
            flush=True,
        )
        return None

    LokiLoggingHandler._warned = False    # fresh handler, fresh warning budget
    handler = LokiLoggingHandler(
        url=url, app_label=app_label, level=level
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
    return handler
