#!/usr/bin/env python3
"""Stream a real project's logs (and optionally metrics) into OpsMemory AI.

This is the answer to "how do I connect my project?" — run it next to anything that writes a log
file, point it at that file, and the platform's existing detection, investigation, verification
and memory pipeline starts operating on that project. No changes to your project, no agent to
install, no infrastructure to stand up.

    python -m app.connectors.tail_logs \
        --service my-api --environment production --project my-project \
        --log-file ./logs/app.log

Only the standard library is used, on purpose: this file is meant to be copied into someone
else's repository, where this project's dependencies are not available.

Useful flags:

    --log-file PATH      the log to follow (repeatable, up to a few files)
    --metrics-file PATH  a JSONL feed of {"name": ..., "value": ..., "ts": ...} to forward
    --from-start         send existing content instead of only new lines
    --demo               generate synthetic lines, for verifying a connection end to end
    --dry-run            parse and print what would be sent, without sending it
    --once               drain what is currently readable, then exit (handy in cron)

Nothing is lost on a transient outage: a batch that fails to send is retried with backoff, and
the connector only advances past the bytes it has successfully delivered.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

DEFAULT_URL = "http://127.0.0.1:8000"

# Detected from the text when the shipper does not say. Order matters: the most severe wins.
_LEVEL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("FATAL", ("fatal", "critical", "panic", "crash")),
    ("ERROR", ("error", "exception", "traceback", "failed", "failure", "err ")),
    ("WARN", ("warn", "warning", "timeout", "retry", "degraded", "slow", "deprecat")),
    ("DEBUG", ("debug", "verbose", "trace ")),
)

# A tiny pool so --demo produces something recognisable rather than noise.
_DEMO_LINES: tuple[tuple[str, str], ...] = (
    ("INFO", "GET /v1/orders 200 duration=42ms"),
    ("INFO", "GET /v1/profile 200 duration=31ms"),
    ("WARN", "connection pool utilisation high active=44 max=50"),
    ("ERROR", "connection pool exhausted: cannot acquire connection within 5000ms active=50 max=50"),
    ("WARN", "cache served stale entry namespace=v7"),
    ("ERROR", "upstream call timed out after 5000ms host=payments-db-primary.internal"),
)


def detect_level(text: str) -> str:
    """Best-effort severity from a raw line.

    The server normalises and re-classifies anyway, so this only has to be roughly right — but
    sending it means a connector's own vocabulary is preserved rather than guessed at twice.
    """
    lowered = text.lower()
    for level, markers in _LEVEL_MARKERS:
        if any(marker in lowered for marker in markers):
            return level
    return "INFO"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------------------
@dataclass
class Client:
    base_url: str
    token: str = ""
    timeout: float = 15.0
    dry_run: bool = False
    verbose: bool = True
    sent_batches: int = 0
    sent_rows: int = 0

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["x-ingest-token"] = self.token
        return headers

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        if self.dry_run:
            if self.verbose:
                print(f"[dry-run] POST {path}  {json.dumps(payload)[:400]}")
            return {"accepted": len(payload.get("lines") or payload.get("samples") or []), "dry_run": True}

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:  # a 4xx is a bug in the connector, not a blip
            detail = exc.read().decode("utf-8", "replace")[:400]
            if 400 <= exc.code < 500:
                raise ConnectorError(f"HTTP {exc.code} from {path}: {detail}") from exc
            raise RetryableError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RetryableError(f"could not reach {url}: {exc}") from exc


class ConnectorError(RuntimeError):
    """A permanent failure. Retrying will not help."""


class RetryableError(RuntimeError):
    """A transient failure. Keep the batch and try again."""


def send_with_retry(client: Client, path: str, payload: dict[str, Any], *, attempts: int = 6) -> dict[str, Any]:
    """Deliver a batch, backing off on transient failures.

    A hackathon venue's wifi, a laptop going to sleep and a backend restart are all normal, and
    none of them should cost you the log lines that describe the incident.
    """
    delay = 1.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.post(path, payload)
        except RetryableError as exc:
            last = exc
            if attempt == attempts:
                break
            sys.stderr.write(f"  ! send failed ({exc}); retrying in {delay:.0f}s\n")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise ConnectorError(f"giving up after {attempts} attempts: {last}")


# ---------------------------------------------------------------------------------------
# tailing
# ---------------------------------------------------------------------------------------
@dataclass
class Tailer:
    """Follows a file, tolerating rotation and truncation.

    Reads in binary and tracks a **byte** cursor. Text mode would make ``seek`` ambiguous for
    anything but ASCII, and this file is meant to follow logs from arbitrary services, which is
    exactly where multi-byte characters turn up.
    """

    path: str
    from_start: bool = False
    position: int = 0
    _identity: tuple[int, int] | None = None

    def prime(self) -> None:
        if not os.path.exists(self.path):
            self.position = 0
            self._identity = None
            return
        stat = os.stat(self.path)
        self._identity = (stat.st_dev, stat.st_ino)
        self.position = 0 if self.from_start else stat.st_size

    def read_new_lines(self) -> list[str]:
        if not os.path.exists(self.path):
            return []
        stat = os.stat(self.path)
        identity = (stat.st_dev, stat.st_ino)

        # Rotation comes in two shapes, and only one of them is detectable by size alone.
        # A *rename*-based rotation (mv app.log app.log.1; create a new app.log) can leave the new
        # file larger than our cursor, so a size check would silently skip its first lines - which
        # are exactly the lines written during the incident that caused the rotation. Tracking the
        # file identity catches that case; the size check catches in-place truncation.
        if self._identity is not None and identity != self._identity:
            self.position = 0
        elif stat.st_size < self.position:
            self.position = 0
        self._identity = identity

        size = stat.st_size
        # Compare against the cursor, not the previous size: with --from-start the cursor begins
        # behind the end of an unchanged file, and a size comparison would deliver nothing at all.
        if size <= self.position:
            return []

        with open(self.path, "rb") as handle:
            handle.seek(self.position)
            data = handle.read()

        lines: list[str] = []
        consumed = 0
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                # A partial write: leave the cursor before it so it is read again once whole.
                break
            consumed += len(raw)
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if text.strip():
                lines.append(text)
        self.position += consumed
        return lines


@dataclass
class Batcher:
    """Accumulates lines and flushes on size or time, whichever comes first."""

    max_lines: int = 200
    flush_seconds: float = 5.0
    pending: list[dict[str, Any]] = field(default_factory=list)
    last_flush: float = field(default_factory=time.monotonic)

    def add(self, line: str) -> None:
        self.pending.append({"message": line, "level": detect_level(line), "ts": now_iso()})

    def due(self) -> bool:
        if len(self.pending) >= self.max_lines:
            return True
        if not self.pending:
            return False
        return (time.monotonic() - self.last_flush) >= self.flush_seconds

    def take(self) -> list[dict[str, Any]]:
        batch, self.pending = self.pending, []
        self.last_flush = time.monotonic()
        return batch


# ---------------------------------------------------------------------------------------
# metrics sidecar
# ---------------------------------------------------------------------------------------
def read_metrics_file(path: str, cursor: dict[str, int]) -> list[dict[str, Any]]:
    """Read new JSONL metric samples. Self-describing lines only — nothing is inferred."""
    if not os.path.exists(path):
        return []
    size = os.path.getsize(path)
    position = cursor.get(path, 0)
    if size < position:
        position = 0
    if size == position:
        return []
    samples: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        handle.seek(position)
        for line in handle:
            if not line.endswith("\n"):
                break
            cursor[path] = position + len(line.encode("utf-8"))
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "name" in payload and "value" in payload:
                samples.append(payload)
    cursor[path] = size
    return samples


# ---------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tail_logs",
        description="Stream a real project's logs and metrics into OpsMemory AI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m app.connectors.tail_logs --service my-api --log-file ./app.log\n"
            "  python -m app.connectors.tail_logs --service my-api --log-file ./app.log --once\n"
            "  python -m app.connectors.tail_logs --service my-api --demo\n"
        ),
    )
    parser.add_argument("--url", default=os.environ.get("OPSMEMORY_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("OPSMEMORY_INGEST_TOKEN", ""))
    parser.add_argument("--service", required=True, help="the service name to report as")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--project", default=None, help="defaults to the service name")
    parser.add_argument("--log-file", action="append", default=[], help="repeatable")
    parser.add_argument("--metrics-file", action="append", default=[], help="JSONL metric feed")
    parser.add_argument("--from-start", action="store_true", help="send existing content too")
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--flush-seconds", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval")
    parser.add_argument("--once", action="store_true", help="drain and exit")
    parser.add_argument("--demo", action="store_true", help="generate synthetic lines")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _register(client: Client, args: argparse.Namespace) -> None:
    client.post(
        "/api/ingest/register",
        {
            "service": args.service,
            "environment": args.environment,
            "project": args.project,
            "kind": "api",
        },
    )


def _flush(client: Client, args: argparse.Namespace, batch: list[dict[str, Any]]) -> None:
    if not batch:
        return
    result = send_with_retry(
        client,
        "/api/ingest/logs",
        {
            "service": args.service,
            "environment": args.environment,
            "project": args.project,
            "lines": batch,
        },
    )
    client.sent_batches += 1
    client.sent_rows += len(batch)
    if not args.quiet:
        accepted = result.get("accepted", len(batch))
        rejected = result.get("rejected", 0)
        suffix = f", {rejected} rejected" if rejected else ""
        print(f"  → accepted {accepted} log line(s){suffix}  (total {client.sent_rows})")
        for issue in (result.get("issues") or [])[:3]:
            print(f"    ! {issue}")


def _flush_metrics(client: Client, args: argparse.Namespace, samples: list[dict[str, Any]]) -> None:
    if not samples:
        return
    result = send_with_retry(
        client,
        "/api/ingest/metrics",
        {
            "service": args.service,
            "environment": args.environment,
            "project": args.project,
            "samples": samples,
        },
    )
    client.sent_batches += 1
    client.sent_rows += len(samples)
    if not args.quiet:
        print(f"  → accepted {result.get('accepted', len(samples))} metric sample(s)")
        for issue in (result.get("issues") or [])[:3]:
            print(f"    ! {issue}")
        if result.get("unwatched_metrics"):
            print(f"    ! stored but not watched by detection: {', '.join(result['unwatched_metrics'])}")


def run(args: argparse.Namespace) -> int:
    client = Client(
        base_url=args.url,
        token=args.token,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )

    if not args.demo and not args.log_file and not args.metrics_file:
        print(
            "Nothing to read. Pass --log-file, --metrics-file, or --demo.\n"
            "  e.g.  python -m app.connectors.tail_logs --service my-api --log-file ./app.log",
            file=sys.stderr,
        )
        return 2

    print(f"OpsMemory AI connector → {args.url}")
    print(f"  service={args.service}  environment={args.environment}  project={args.project or args.service}")
    try:
        _register(client, args)
        print("  registered (or already known)")
    except ConnectorError as exc:
        print(f"  could not register: {exc}", file=sys.stderr)
        return 1

    tailers = [Tailer(path=path, from_start=args.from_start) for path in args.log_file]
    for tailer in tailers:
        tailer.prime()
        print(f"  following {tailer.path} (from {'start' if args.from_start else 'end'})")
    for path in args.metrics_file:
        print(f"  forwarding metrics from {path}")

    batch = Batcher(max_lines=args.batch, flush_seconds=args.flush_seconds)
    metric_cursor: dict[str, int] = {}

    print("streaming — press Ctrl-C to stop\n")
    try:
        while True:
            for tailer in tailers:
                for line in tailer.read_new_lines():
                    batch.add(line)

            if args.demo:
                for _ in range(random.randint(1, 3)):
                    level, text = random.choice(_DEMO_LINES)
                    batch.add(f"{level} {text}")

            if batch.due() or (args.once and batch.pending):
                _flush(client, args, batch.take())

            metric_samples: list[dict[str, Any]] = []
            for path in args.metrics_file:
                metric_samples.extend(read_metrics_file(path, metric_cursor))
            if metric_samples:
                _flush_metrics(client, args, metric_samples)

            if args.once:
                # One more pass so a file being written to right now is fully drained.
                for tailer in tailers:
                    for line in tailer.read_new_lines():
                        batch.add(line)
                _flush(client, args, batch.take())
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopping…")
        _flush(client, args, batch.take())
    except ConnectorError as exc:
        print(f"\nfailed: {exc}", file=sys.stderr)
        return 1

    print(f"\nsent {client.sent_rows} row(s) in {client.sent_batches} batch(es)")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    return run(build_parser().parse_args(list(argv) if argv is not None else None))


if __name__ == "__main__":
    raise SystemExit(main())
