"""
The producer side of the Xcore log service, for a Python agent (BackendX, FrontendX, anything with a worker).

    from xcore_logclient import LogClient
    logs = LogClient(url, token, org=org, tool="backendx", run=run_id, attempt=attempt)
    logs.log("generating the plan", step="plan")
    ...
    logs.finish()          # before the process exits, and before you report the run's result

Same contract as the Node client (clients/node/logClient.js), line for line. What an agent has to know:

  * `token` is the WRITE token Xcore gave the agent for this run. `org`, `tool`, `run` and `attempt` are the agent's OWN record of
    which run this is (from the job it is executing), NOT read from the token. The service checks that they agree with the token and
    refuses the whole batch if they do not: that is what stops one job's lines going into another job's log.
  * log() never raises, never blocks and never fails the job. A log line is not worth a job. If the service cannot be reached the
    lines wait in a bounded queue; when it is full the OLDEST are dropped and one marker line says how many.
  * One batch is in flight at a time and a failed batch is resent UNCHANGED (same batch number), so a retry after a lost answer is
    recognised by the service and not stored twice. Order is the order of log() calls.
  * finish() sends a manifest (lines emitted, sent, dropped, and a rolling hash of those sent). The service compares it with what it
    stored, and the run's `end` event says whether every line arrived. Call it before reporting the run's result: the sandbox is
    usually destroyed soon after, and anything still queued then is gone.
  * On a permanent refusal (an older attempt after a resume, a sealed run, a token for another run, an invalid token) the client
    stops, drops what it holds and calls on_fatal(reason) once. It does not retry: retrying cannot help.
  * Values in `redact`, and the token itself, are replaced by [redacted] in every line before it is queued.

Standard library only (threading, http.client). Safe to use from many threads; one LogClient per run per process.
"""
import gzip
import hashlib
import http.client
import json
import random
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

__all__ = ["LogClient"]

_FATAL_409 = {"attempt_fenced", "sealed", "attempt_not_open", "run_mismatch"}


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _clean(value) -> str:
    """Any value to text the service can store: never raises, and valid UTF-8 (a lone surrogate would break the JSON)."""
    try:
        text = value if isinstance(value, str) else (repr(value) if not isinstance(value, BaseException) else f"{type(value).__name__}: {value}")
    except Exception:  # an object whose repr raises
        text = "[unprintable]"
    return text.encode("utf-8", "replace").decode("utf-8")


class LogClient:
    def __init__(self, url, token, org, tool, run, attempt, *, kind="agent", producer=None, redact=(),
                 on_fatal=None, on_warn=None, max_queue_lines=20000, max_batch_lines=500, max_batch_bytes=256 * 1024,
                 flush_interval=0.25, request_timeout=15.0, backoff_base=0.25, backoff_max=10.0, gzip_over_bytes=1024):
        for name, v in (("url", url), ("token", token), ("org", org), ("tool", tool), ("run", run)):
            if not isinstance(v, str) or not v:
                raise ValueError(f"LogClient: {name} is required")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("LogClient: attempt must be a positive integer")

        self._url = urlsplit(url.rstrip("/"))
        self._base_path = self._url.path
        self._token = token
        self._org, self._tool, self._run_id, self._attempt = org, tool, run, attempt   # NOT `_run`: that is the worker thread's method
        self.producer = producer or f"{kind}-{secrets.token_hex(4)}"
        self._secrets = sorted({s for s in (token, *redact) if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)
        self._on_fatal = on_fatal or (lambda reason, detail=None: None)
        self._on_warn = on_warn or (lambda message: None)
        self._o = dict(max_queue_lines=max_queue_lines, max_batch_lines=max_batch_lines, max_batch_bytes=max_batch_bytes,
                       flush_interval=flush_interval, request_timeout=request_timeout, backoff_base=backoff_base,
                       backoff_max=backoff_max, gzip_over_bytes=gzip_over_bytes)

        self._cv = threading.Condition()
        self._queue = deque()
        self._inflight = None           # {"n", "lines", "manifest"}, kept until acknowledged and resent UNCHANGED
        self._last_batch = 0
        self._chain = ""
        self._emitted = self._sent = self._dropped = self._retries = self._batches_sent = 0
        self._dead = None
        self._finishing = self._finished = self._closed = False
        self._flush_requested = False
        self._last_error = None
        self._thread = None

    # ── what the agent calls ─────────────────────────────────────────────────────

    def log(self, text, level="info", step=""):
        """Queue one line. Never raises, never blocks."""
        try:
            with self._cv:
                if self._dead or self._closed or self._finishing or self._finished:
                    return
                t = _clean(text)
                for s in self._secrets:
                    if s in t:
                        t = t.replace(s, "[redacted]")
                self._emitted += 1
                self._queue.append({"text": t, "level": _clean(level) if level else "info", "step": _clean(step) if step else "", "ts": int(time.time() * 1000)})
                if len(self._queue) > self._o["max_queue_lines"]:
                    self._overflow()
                self._ensure_thread()
                self._cv.notify_all()
        except Exception:  # noqa: BLE001 - logging must never raise into the job
            pass

    def flush(self, timeout=10.0):
        """Wait until everything logged so far is acknowledged, or `timeout` seconds. Never raises."""
        deadline = time.monotonic() + timeout
        with self._cv:
            if self._dead or self._closed:
                return {"complete": False, "pending": self._pending(), "dead": self._dead}
            self._flush_requested = True
            self._ensure_thread()
            self._cv.notify_all()
            while (self._inflight is not None or self._queue) and not self._dead and not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(timeout=min(left, 0.2))
            self._flush_requested = False
            return {"complete": self._pending() == 0 and not self._dead, "pending": self._pending(), "dead": self._dead}

    def finish(self, timeout=10.0):
        """
        Send what is left and the manifest, and stop. Call it before you report the run's result. Never raises.
        On a timeout the lines still waiting are counted as dropped and the manifest is NOT sent, so the service reports this
        producer as vanished, which is the truth; everything it did receive is still there.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            if self._finished:
                return self._result(True)
            if self._dead:
                return self._result(False, self._dead)
            self._finishing = True
            self._ensure_thread()
            self._cv.notify_all()
            while not self._finished and not self._dead:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(timeout=min(left, 0.2))
            if self._finished:
                return self._result(True)
            self._dropped += self._pending()
            self._closed = True
            self._cv.notify_all()
            return self._result(False, self._dead or "timeout")

    def close(self):
        """Stop at once, as a killed process would: nothing more is sent, no manifest."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def stats(self):
        with self._cv:
            return {"producer": self.producer, "emitted": self._emitted, "sent": self._sent, "dropped": self._dropped,
                    "retries": self._retries, "batches_sent": self._batches_sent, "pending": self._pending(),
                    "dead": self._dead, "last_error": self._last_error, "finished": self._finished}

    # ── internals ────────────────────────────────────────────────────────────────

    def _pending(self):
        return len(self._queue) + (len(self._inflight["lines"]) if self._inflight else 0)

    def _result(self, ok, reason=None):
        return {"ok": ok, "reason": reason, "emitted": self._emitted, "sent": self._sent, "dropped": self._dropped, "pending": self._pending()}

    def _ensure_thread(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="xcore-logclient", daemon=True)
            self._thread.start()

    @staticmethod
    def _marker_text(n):
        return f"[log-client] {n} log line{' was' if n == 1 else 's were'} dropped here: the log queue was full because the log service could not be reached"

    def _overflow(self):
        """Queue full: drop the OLDEST lines. One marker stays at the front and only grows (it is never itself counted as dropped)."""
        excess = len(self._queue) - self._o["max_queue_lines"]
        if excess <= 0:
            return
        has_marker = bool(self._queue and self._queue[0].get("marker"))
        items = list(self._queue)
        start = 1 if has_marker else 0
        removed = items[start:start + excess]
        del items[start:start + excess]
        self._dropped += len(removed)
        lost = sum(l["count"] if l.get("marker") else 1 for l in removed)
        if has_marker:
            items[0]["count"] += lost
            items[0]["text"] = self._marker_text(items[0]["count"])
        else:
            items.insert(0, {"marker": True, "count": lost, "text": self._marker_text(lost), "level": "warn", "step": "log-client", "ts": int(time.time() * 1000)})
            self._emitted += 1
        self._queue = deque(items)

    def _fatal(self, reason, detail=None):
        if self._dead:
            return
        self._dead = reason
        self._last_error = f"{reason}{': ' + str(detail) if detail else ''}"
        self._dropped += len(self._queue) + (len(self._inflight["lines"]) if self._inflight else 0)
        self._queue.clear()
        self._inflight = None
        self._cv.notify_all()
        try:
            self._on_warn(f"log client stopped for good ({self._last_error})")
            self._on_fatal(reason, detail)
        except Exception:  # noqa: BLE001
            pass

    def _pause(self, seconds):
        """
        Wait `seconds` before a retry, ignoring the wake-ups every log() call sends. Without this a busy logger cut the backoff short
        on every line and the client hammered a service that was already struggling. Only close, a fatal error or the deadline end it.
        """
        deadline = time.monotonic() + seconds
        while not (self._closed or self._dead):
            left = deadline - time.monotonic()
            if left <= 0:
                return
            self._cv.wait(timeout=left)

    def _build_batch(self):
        lines = []
        size = 0
        while self._queue and len(lines) < self._o["max_batch_lines"]:
            l = self._queue[0]
            s = len(l["text"].encode("utf-8")) + 64
            if lines and size + s > self._o["max_batch_bytes"]:
                break
            lines.append(self._queue.popleft())
            size += s
        manifest = None
        if self._finishing and not self._queue:
            h = self._chain
            for l in lines:
                h = _sha1(h + _sha1(l["text"]))
            manifest = {"sent": self._sent + len(lines), "emitted": self._emitted, "dropped": self._dropped, "hash": h}
        self._last_batch += 1
        return {"n": self._last_batch, "lines": lines, "manifest": manifest}

    def _body(self, b):
        payload = {"v": 1, "org": self._org, "tool": self._tool, "run": self._run_id, "attempt": self._attempt,
                   "producer": self.producer, "batch": b["n"],
                   "lines": [{"text": l["text"], "level": l["level"], "step": l["step"], "ts": l["ts"]} for l in b["lines"]]}
        if b["manifest"]:
            payload["manifest"] = b["manifest"]
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _send(self, b):
        """-> (kind, extra): kind is ok | retry | dropped | fatal."""
        body = self._body(b)
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        if len(body) > self._o["gzip_over_bytes"]:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        cls = http.client.HTTPSConnection if self._url.scheme == "https" else http.client.HTTPConnection
        conn = cls(self._url.hostname, self._url.port, timeout=self._o["request_timeout"])
        try:
            conn.request("POST", f"{self._base_path}/v1/ingest", body, headers)
            resp = conn.getresponse()
            raw = resp.read()
            status, retry_after = resp.status, resp.getheader("Retry-After")
        except (OSError, http.client.HTTPException) as e:
            self._last_error = f"network: {e}"
            return "retry", {}
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = None
        if status == 200 and isinstance(data, dict) and data.get("ok"):
            return "ok", {}
        code = data.get("code") if isinstance(data, dict) else None
        self._last_error = f"HTTP {status}{' ' + code if code else ''}"
        if status == 429:
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = 1.0
            return "retry", {"retry_after": wait}
        if status >= 500:
            return "retry", {}
        if status == 409 and code in _FATAL_409:
            return "fatal", {"reason": code}
        if status in (401, 403):
            return "fatal", {"reason": "unauthorized" if status == 401 else "forbidden", "detail": code}
        return "dropped", {"detail": f"{status} {code or ''}".strip()}

    def _run(self):
        attempt_no = 0
        while True:
            with self._cv:
                while True:
                    if self._closed or self._dead:
                        self._cv.notify_all()
                        return
                    if self._inflight is not None or self._queue or (self._finishing and not self._finished):
                        break
                    self._cv.wait()
                # let a few more lines accumulate into the batch, unless someone is waiting on us
                if self._inflight is None and not self._flush_requested and not self._finishing and len(self._queue) < self._o["max_batch_lines"]:
                    self._cv.wait(timeout=self._o["flush_interval"])
                    if self._closed or self._dead:
                        continue
                if self._inflight is None:
                    self._inflight = self._build_batch()
                batch = self._inflight

            kind, extra = self._send(batch)

            with self._cv:
                if self._closed or self._dead:
                    continue
                if kind == "ok":
                    for l in batch["lines"]:
                        self._chain = _sha1(self._chain + _sha1(l["text"]))
                    self._sent += len(batch["lines"])
                    self._batches_sent += 1
                    if batch["manifest"]:
                        self._finished = True
                    self._inflight = None
                    attempt_no = 0
                    self._cv.notify_all()
                elif kind == "retry":
                    self._retries += 1
                    attempt_no += 1
                    backoff = min(self._o["backoff_max"], self._o["backoff_base"] * (2 ** min(attempt_no, 10)))
                    wait = max(extra.get("retry_after", 0.0), backoff * (0.8 + random.random() * 0.4))
                    if attempt_no == 1 or attempt_no % 10 == 0:
                        try:
                            self._on_warn(f"log service unavailable ({self._last_error}); retrying batch {batch['n']}")
                        except Exception:  # noqa: BLE001
                            pass
                    self._pause(wait)
                elif kind == "dropped":
                    # The service will never accept this batch as it is: drop it and say so, rather than loop on it forever.
                    self._dropped += len(batch["lines"])
                    lost = sum(l["count"] if l.get("marker") else 1 for l in batch["lines"])
                    had_manifest = bool(batch["manifest"])
                    self._inflight = None
                    self._queue.appendleft({"marker": True, "count": lost, "level": "warn", "step": "log-client", "ts": int(time.time() * 1000),
                                            "text": f"[log-client] {lost} log line{'' if lost == 1 else 's'} could not be delivered (the log service refused them: {extra.get('detail')})"})
                    self._emitted += 1
                    if had_manifest:
                        self._finishing = True
                else:
                    self._fatal(extra.get("reason"), extra.get("detail"))
