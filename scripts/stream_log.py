#!/usr/bin/env python3
"""Passes a command's output through unchanged and sends every line of it to the Xcore log service, so the member sees the build
live in Xcore while it runs on this runner (see docs/AGENT_INTEGRATION.md of the log service: this process is one more producer
for the run). Used through scripts/run_streamed.sh.

Where to send it comes from the environment (XLOG_URL, XLOG_TOKEN, XLOG_ORG, XLOG_TOOL, XLOG_RUN, XLOG_ATTEMPT), which the
"Decrypt Apple Developer credentials" step fills from signing_data.enc. Nothing there: this is plain `cat`.

It must never make the build worse: it never fails, it never stops reading (a closed pipe would kill the command with SIGPIPE),
and a log service that cannot be reached costs a bounded wait at the end, not the build.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STEP = "ipa_runner"  # the Xcore build-log panel keeps the steps that start with "ipa"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Values the member's Apple account put in this process's environment; GitHub masks them in its own log, the log service must too.
SECRET_ENV = (
    "USER_APPLE_ID", "USER_APPLE_APP_SPECIFIC_PASSWORD", "USER_APPLE_TEAM_ID", "USER_APP_STORE_CONNECT_KEY_ID",
    "USER_APP_STORE_CONNECT_ISSUER_ID", "USER_APP_STORE_CONNECT_API_KEY", "USER_MATCH_PASSWORD", "USER_FASTLANE_SESSION",
)
ERROR = re.compile(r"(\berror\b[:\]]|\[!\]|\bfatal\b|\bfailed\b|\*\* BUILD FAILED \*\*|❌)", re.IGNORECASE)
WARNING = re.compile(r"(\bwarning\b[:\]]|\[\?\])", re.IGNORECASE)


def secrets():
    found = []
    for name in SECRET_ENV:
        for line in (os.environ.get(name) or "").splitlines():
            if len(line.strip()) >= 6:
                found.append(line.strip())
    return found


def make_client():
    env = os.environ
    if not all(env.get(k) for k in ("XLOG_URL", "XLOG_TOKEN", "XLOG_ORG", "XLOG_TOOL", "XLOG_RUN", "XLOG_ATTEMPT")):
        return None
    try:
        from xcore_logclient import LogClient

        return LogClient(
            env["XLOG_URL"], env["XLOG_TOKEN"], org=env["XLOG_ORG"], tool=env["XLOG_TOOL"], run=env["XLOG_RUN"],
            attempt=int(env["XLOG_ATTEMPT"]), kind="runner", redact=secrets(),
            on_warn=lambda message: sys.stderr.write(f"[log stream] {message}\n"),
            on_fatal=lambda reason, detail=None: sys.stderr.write(f"[log stream] stopped: {reason}\n"),
        )
    except Exception as exc:  # noqa: BLE001 - logging must never fail the build
        sys.stderr.write(f"[log stream] not started: {exc}\n")
        return None


def level_of(text):
    if ERROR.search(text):
        return "error"
    if WARNING.search(text):
        return "warning"
    return "info"


def main():
    client = make_client()
    out = sys.stdout.buffer
    try:
        for raw in sys.stdin.buffer:
            try:
                out.write(raw)
                out.flush()
            except Exception:  # noqa: BLE001 - keep reading: a closed pipe would kill the command
                pass
            if client is None:
                continue
            try:
                text = ANSI.sub("", raw.decode("utf-8", "replace"))
                # A progress line redraws itself with \r; what is left on the screen is its last part.
                parts = [p for p in text.replace("\r\n", "\n").split("\r") if p.strip()]
                text = (parts[-1] if parts else "").rstrip("\n")
                if text.strip():
                    client.log(text, level=level_of(text), step=STEP)
            except Exception:  # noqa: BLE001
                pass
    finally:
        if client is not None:
            try:
                client.finish(timeout=float(os.environ.get("XLOG_FINISH_TIMEOUT", "20")))
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
