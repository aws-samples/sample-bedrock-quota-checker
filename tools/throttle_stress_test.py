#!/usr/bin/env python3
"""Deliberately provoke Bedrock throttling to verify that the quota checker's
`InvocationThrottles` series is populated.

This is a TEST HARNESS, not part of the collector. It makes **real, billed**
`bedrock-runtime` InvokeModel/Converse calls against your own account and is
designed to drive the model's request-per-minute quota to the point where
Bedrock returns `ThrottlingException` (HTTP 429). Those 429s are exactly what
show up later as the CloudWatch metric `AWS/Bedrock InvocationThrottles`.

Why requests, not tokens
------------------------
Throttling can trip on either the requests-per-minute (RPM) or the
tokens-per-minute (TPM) quota. Saturating RPM is far cheaper: each probe sends
one trivial prompt with ``max_tokens=1``, so you pay for a handful of tokens per
call while still consuming one unit of the RPM budget. Burning enough tokens to
trip TPM would cost orders of magnitude more. This script therefore defaults to
the RPM strategy — short prompts, high concurrency.

Safety
------
* Dry-run by default. Nothing is sent until you pass ``--run``.
* A hard ceiling on total requests (``--max-requests``) that you cannot exceed.
* A printed cost estimate and an interactive confirmation before any traffic.
* Read-only toward account configuration: it never creates, changes, or deletes
  any AWS resource or quota. It only invokes a model, which is a billed data-plane
  call — this is NOT an infrastructure change, but it DOES cost money and generate
  load, so run it only against an account where you are authorized to do so.

It stops as soon as it has observed enough throttles (``--stop-after-throttles``)
so you do not generate more load than needed to prove the point.

Usage
-----
    # See the plan and cost estimate without sending anything:
    python3 tools/throttle_stress_test.py --region us-east-1 \
        --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0

    # Actually run it (will prompt for confirmation):
    python3 tools/throttle_stress_test.py --region us-east-1 \
        --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
        --run --concurrency 60 --max-requests 1500

Pick the SAME inference profile ID your workload uses (for example
``us.anthropic.claude-opus-5-5``) so the throttles land on the quota you care
about. Run the quota checker ~10-15 minutes afterwards (CloudWatch is not
instant) and open the Run log / usage for `InvocationThrottles` in that Region.
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


# Rough per-call token footprint of an RPM probe: a tiny system+user prompt and
# a single output token. Only used for the pre-flight cost estimate, which is
# intentionally an over-estimate so the printed number is never a surprise.
PROBE_INPUT_TOKENS = 20
PROBE_OUTPUT_TOKENS = 1


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Tally:
    """Thread-safe counters shared across the worker pool."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent = 0
        self.ok = 0
        self.throttled = 0
        self.other_errors = 0
        self.first_throttle_at = None
        self.error_samples = {}
        self.latencies_ms = []

    def record(self, outcome, detail="", latency_ms=None):
        with self.lock:
            self.sent += 1
            if latency_ms is not None:
                self.latencies_ms.append(latency_ms)
            if outcome == "ok":
                self.ok += 1
            elif outcome == "throttled":
                self.throttled += 1
                if self.first_throttle_at is None:
                    self.first_throttle_at = iso_now()
            else:
                self.other_errors += 1
                if detail and detail not in self.error_samples:
                    self.error_samples[detail] = self.sent
            return self.throttled


def invoke_once(client, model_id, tally, stop_event):
    """Send one minimal Converse request and classify the result."""
    if stop_event.is_set():
        return
    started = time.perf_counter()
    try:
        # maxTokens=1 keeps each probe trivially cheap. No sampling params: the
        # Opus 5 / Sonnet 5 / Fable 5 family rejects temperature/top_p with a 400,
        # and omitting them is valid for every model.
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        tally.record("ok", latency_ms=(time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - we classify by error code below.
        latency_ms = (time.perf_counter() - started) * 1000
        code = ""
        response = getattr(exc, "response", {}) or {}
        code = response.get("Error", {}).get("Code", type(exc).__name__)
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("ThrottlingException", "TooManyRequestsException") or status == 429:
            tally.record("throttled", latency_ms=latency_ms)
        else:
            tally.record("other", detail=f"{code}: {str(exc)[:120]}", latency_ms=latency_ms)


def run(args):
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    # Disable botocore's own retries: a retry on a 429 would hide throttles from
    # our tally and keep hammering the quota. We want to SEE the first 429.
    client = session.client(
        "bedrock-runtime", region_name=args.region,
        config=Config(retries={"mode": "standard", "total_max_attempts": 1},
                      connect_timeout=10, read_timeout=30, max_pool_connections=args.concurrency + 4),
    )

    identity = session.client("sts", region_name=args.region).get_caller_identity()
    est_input = PROBE_INPUT_TOKENS * args.max_requests
    est_output = PROBE_OUTPUT_TOKENS * args.max_requests

    print("=" * 72)
    print("Bedrock throttle stress test")
    print("=" * 72)
    print(f"  account        : {identity['Account']}")
    print(f"  region         : {args.region}")
    print(f"  model/profile  : {args.model_id}")
    print(f"  strategy       : saturate requests-per-minute (max_tokens=1 probes)")
    print(f"  concurrency    : {args.concurrency} in-flight requests")
    print(f"  ceiling        : up to {args.max_requests:,} total requests")
    print(f"  stop condition : {args.stop_after_throttles} throttles OR the ceiling, whichever first")
    print(f"  cost ceiling   : <= ~{est_input:,} input + ~{est_output:,} output tokens if it runs to the ceiling")
    print(f"                   (a short prompt per call; throttled calls are billed little to nothing)")
    print("=" * 72)

    if not args.run:
        print("\nDRY RUN — nothing was sent. Re-run with --run to execute.")
        return 0

    if not args.yes:
        print("\nThis will send REAL, billed requests and deliberately trigger 429s.")
        answer = input("Type 'throttle' to proceed: ").strip()
        if answer != "throttle":
            print("Aborted; no requests sent.")
            return 1

    tally = Tally()
    stop_event = threading.Event()
    started = time.perf_counter()
    print(f"\n[{iso_now()}] starting; press Ctrl-C to stop early.\n", flush=True)

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = set()
            dispatched = 0
            while dispatched < args.max_requests and not stop_event.is_set():
                # Keep the pool full without queueing the entire ceiling at once.
                while len(futures) < args.concurrency and dispatched < args.max_requests:
                    futures.add(executor.submit(invoke_once, client, args.model_id, tally, stop_event))
                    dispatched += 1
                done = {f for f in futures if f.done()}
                futures -= done
                with tally.lock:
                    throttled = tally.throttled
                    sent = tally.sent
                if throttled >= args.stop_after_throttles:
                    stop_event.set()
                    break
                if sent and sent % args.concurrency == 0:
                    print(f"[{iso_now()}] sent={sent:,} ok={tally.ok:,} "
                          f"throttled={throttled:,} other={tally.other_errors:,}", flush=True)
                time.sleep(0.01)
            # Drain whatever is still in flight.
            for _ in as_completed(futures):
                pass
    except KeyboardInterrupt:
        stop_event.set()
        print(f"\n[{iso_now()}] interrupted by user; draining in-flight requests...", flush=True)

    elapsed = time.perf_counter() - started
    summarize(args, tally, elapsed)
    return 0 if tally.throttled else 2


def summarize(args, tally, elapsed):
    rate = (100 * tally.throttled / tally.sent) if tally.sent else 0
    p50 = sorted(tally.latencies_ms)[len(tally.latencies_ms) // 2] if tally.latencies_ms else 0
    print("\n" + "=" * 72)
    print("RESULT")
    print("=" * 72)
    print(f"  duration           : {elapsed:.1f}s")
    print(f"  requests sent      : {tally.sent:,}")
    print(f"  succeeded (200)    : {tally.ok:,}")
    print(f"  THROTTLED (429)    : {tally.throttled:,}  ({rate:.1f}% of sent)")
    print(f"  other errors       : {tally.other_errors:,}")
    print(f"  median latency     : {p50:.0f} ms")
    print(f"  first throttle at  : {tally.first_throttle_at or 'never'}")
    if tally.error_samples:
        print("  non-throttle error samples:")
        for detail, index in list(tally.error_samples.items())[:5]:
            print(f"    - (after {index} sent) {detail}")
    print("=" * 72)
    if tally.throttled:
        print("\n✅ Throttling was triggered. The matching CloudWatch metric is")
        print(f"   AWS/Bedrock InvocationThrottles, dimension ModelId={args.model_id!r},")
        print(f"   in {args.region}. CloudWatch lags ~5-15 min; wait, then run the")
        print("   collector and look for InvocationThrottles > 0 in the Run log / usage:")
        print(f"\n     python3 bedrock_access_report.py --regions {args.region} \\")
        print(f"         --model-ids {args.model_id} --max-metric-requests 1200\n")
    else:
        print("\n⚠️  No throttles observed. The RPM quota for this model may be higher")
        print("   than the load generated. Raise --concurrency and --max-requests, or")
        print("   target a model/profile with a lower requests-per-minute quota.")
        print("   Check applied RPM quotas in the collector's quotas.csv for this Region.")


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", required=True, help="AWS Region to target, e.g. us-east-1")
    p.add_argument("--model-id", required=True,
                   help="Inference profile / model ID to invoke, e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0. "
                        "Use the SAME profile your workload uses so throttles land on the quota you care about.")
    p.add_argument("--profile", help="AWS profile; uses the default credential chain when omitted")
    p.add_argument("--run", action="store_true",
                   help="Actually send requests. Without this flag the script only prints the plan (dry run).")
    p.add_argument("--yes", action="store_true", help="Skip the interactive confirmation (for non-interactive runs)")
    p.add_argument("--concurrency", type=int, default=50, help="In-flight requests (default: 50)")
    p.add_argument("--max-requests", type=int, default=1000,
                   help="Hard ceiling on total requests sent (default: 1000)")
    p.add_argument("--stop-after-throttles", type=int, default=25,
                   help="Stop once this many 429s have been observed (default: 25)")
    return p


def main():
    args = parser().parse_args()
    if args.concurrency <= 0 or args.max_requests <= 0 or args.stop_after_throttles <= 0:
        raise SystemExit("concurrency, max-requests and stop-after-throttles must be positive.")
    if args.concurrency > args.max_requests:
        args.concurrency = args.max_requests
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
