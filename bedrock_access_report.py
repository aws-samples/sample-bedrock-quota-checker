#!/usr/bin/env python3
"""Read-only Bedrock inventory, quotas and CloudWatch history. No inference calls."""

import argparse
import copy
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import zipfile
import zlib

VERSION = "0.4.0"
UTC = timezone.utc
# Narrow mappings verified against quota definitions and system profile model IDs.
# Names are guards against changed quota semantics, not fuzzy matching rules.
TPM_RULES_VERSION = "2026-10-06.1"
TPM_RULES = {
    "L-5DB28B7B": (
        "Cross-region model inference tokens per minute for Anthropic Claude Opus 4.7",
        "us.anthropic.claude-opus-4-7", "anthropic.claude-opus-4-7",
    ),
    "L-58BE175A": (
        "Cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
}
# Exact quota codes, names and model identities; never infer a mapping from a
# substring. Runtime profile destinations are also checked in analyze().
for _code, _name, _profile, _model in (
    ("L-A4430697", "Cross-region model inference tokens per minute for Anthropic Claude Opus 5.5", "us.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"),
    ("L-A103A344", "Global cross-region model inference tokens per minute for Anthropic Claude Opus 5.5", "global.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"),
    ("L-99296DCD", "Cross-region model inference tokens per minute for Anthropic Claude Opus 5", "us.anthropic.claude-opus-5", "anthropic.claude-opus-5"),
    ("L-D73B1244", "Global cross-region model inference tokens per minute for Anthropic Claude Opus 5", "global.anthropic.claude-opus-5", "anthropic.claude-opus-5"),
    ("L-DB99DCDB", "Cross-region model inference tokens per minute for Anthropic Claude Opus 4.8", "us.anthropic.claude-opus-4-8", "anthropic.claude-opus-4-8"),
    ("L-4FCE27C7", "Global cross-region model inference tokens per minute for Anthropic Claude Opus 4.8", "global.anthropic.claude-opus-4-8", "anthropic.claude-opus-4-8"),
    ("L-9A11C666", "Global cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5", "global.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("L-D4FBCF4E", "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 5", "us.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"),
    ("L-DD84E5CA", "Global cross-region model inference tokens per minute for Anthropic Claude Sonnet 5", "global.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"),
    ("L-B38B530A", "Global cross-region model inference tokens per minute for GPT-6 Astra", "global.openai.gpt-6-astra", "openai.gpt-6-astra"),
    ("L-53A144CD", "Cross-region model inference tokens per minute for GPT-6 Astra", "us.openai.gpt-6-astra", "openai.gpt-6-astra"),
):
    TPM_RULES[_code] = (_name, _profile, _model)

MANTLE_RULES = {}
for _name, _model, _input, _output in (
    ("GPT-5.6 Luna", "openai.gpt-5.6-luna", "L-31615887", "L-D44E1D4E"),
    ("GPT-5.6 Terra", "openai.gpt-5.6-terra", "L-87594EB7", "L-574688BD"),
    ("GPT-5.6 Sol", "openai.gpt-5.6-sol", "L-137DD3C2", "L-C2E494BA"),
    ("GPT-5.5", "openai.gpt-5.5", "L-2555C05B", "L-8A643E07"),
    ("GPT-5.4", "openai.gpt-5.4", "L-C593EA7C", "L-08036ECB"),
):
    for _code, _direction, _metric in (
        (_input, "Input", "TotalInputTokens"), (_output, "Output", "TotalOutputTokens"),
    ):
        MANTLE_RULES[_code] = (
            f"[bedrock-mantle endpoint] {_direction} tokens per minute for {_name}",
            _model, _metric,
        )

RPM_RULES_VERSION = "2026-10-07.1"
# Requests-per-minute quotas carry no UsageMetric and no Period, so Service
# Quotas cannot link them to a series. The quota name does carry the scope and a
# model label, and Invocations is published per ModelId, so the link is derived
# and then validated: a label must resolve to exactly ONE identity built from
# live inventory. Equality is exact after normalization; a label that two
# identities share is dropped rather than guessed, because a wrong mapping would
# report a confident utilization figure for the wrong model.
RPM_QUOTA_NAME = re.compile(
    r"^(?P<scope>Global cross-region|Cross-region|On-demand) "
    r"model inference requests per minute for (?P<model>.+)$")
RPM_SCOPES = {"Global cross-region": "global", "Cross-region": "cross", "On-demand": "on_demand"}
# Geography prefix of a regional system-defined inference profile, as opposed to
# a bare model id, which is reached on demand.
GEO_PREFIX = re.compile(r"^(us|us-gov|eu|apac|ca|sa|au|jp|kr|in|il|mx)\.")


def normalize_label(text):
    """Compare model labels on letters and digits only; punctuation and spacing
    differ between quota names, profile identifiers and catalogue names."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def rpm_targets(report, region):
    """Normalized model label -> the single ModelId dimension it can mean.

    Labels come only from inventory this run collected: a system-defined
    profile's one destination model for the cross-region scopes, and an
    on-demand catalogue model for the on-demand scope. Nothing is inferred from
    a substring, and an ambiguous label is omitted.
    """
    catalogue = {m["modelId"]: m for m in report.get("models", []) if m.get("region") == region}

    def forms(model_id):
        entry = catalogue.get(model_id) or {}
        name, provider = entry.get("modelName"), entry.get("providerName")
        shapes = {model_id, re.sub(r":\d+$", "", model_id), name, f"{provider} {name}" if provider and name else ""}
        return {normalize_label(s) for s in shapes if s}

    tables = {scope: defaultdict(set) for scope in RPM_SCOPES.values()}
    for profile in report.get("inference_profiles", []):
        if (profile.get("region") != region or profile.get("type") != "SYSTEM_DEFINED"
                or profile.get("status") != "ACTIVE"):
            continue
        destinations = {m["modelArn"].split("/")[-1] for m in profile.get("models") or []}
        if len(destinations) != 1:
            continue
        scope = "global" if profile["inferenceProfileId"].startswith("global.") else "cross"
        for label in forms(next(iter(destinations))):
            tables[scope][label].add(profile["inferenceProfileId"])
    for model_id, entry in catalogue.items():
        if "ON_DEMAND" not in (entry.get("inferenceTypesSupported") or []):
            continue
        for label in forms(model_id):
            tables["on_demand"][label].add(model_id)
    return {scope: {label: next(iter(ids)) for label, ids in table.items() if len(ids) == 1}
            for scope, table in tables.items()}


REPORT_LIMITATIONS = (
    "Quotas are a current snapshot; they do not reconstruct historical limits.",
    "Reported availability does not test the application's effective invocation permissions.",
    "EstimatedTPMQuotaUsage is an estimate; it does not reproduce upfront max_tokens reservations.",
    "Missing metrics are not converted to zero. Statistics use only returned datapoints.",
    "ListMetrics omits series inactive for two weeks; known-ID probes cannot guarantee coverage of deleted resources.",
    "Percentages are calculated only when the quota mapping and units are confirmed.",
    "Totals from series with different dimensions are not added together, to avoid double counting.",
    "Mantle has its own namespace and quotas. Missing series do not prove that there was no usage.",
    "Mantle InferenceClientErrors excludes quota-related HTTP 429 responses rejected before processing. Inspect application HTTP status/error logs.",
    "Cross-Model Max Tokens Per Day uses pricing-based accounting, not a raw sum of token metrics; its utilization is not calculated here.",
    "Budget exhaustion is a local collection limit, not an AWS inference error. Resume incomplete queries before interpreting missing diagnostics.",
    "This collection covers only the listed Regions; global quotas may have additional usage from other source Regions.",
    "Metric names and dimension sets come from ListMetrics; a namespace reported as empty published nothing in this account, which is not proof that the namespace does not exist.",
    "Gauge statistics (Average, Maximum, p99) are not summable. Totals and per-minute rates are reported only for Sum series.",
    "p99 is computed by CloudWatch over each period, so it cannot be re-aggregated across periods into a window-wide p99.",
    "Requests-per-minute quotas carry no UsageMetric. Their link to a series is derived from the quota name's scope and model label, and is reported only when that label resolves to exactly one collected profile or on-demand model.",
    "Invocations counts accepted requests. A request-quota percentage therefore excludes throttled requests, and a peak at the limit alongside throttling means demand exceeded it by an amount this percentage does not show.",
    "An observed request ceiling is measured from the intervals that recorded throttling. Quotas listed beside it match that rate and scope; confirming which one applied needs the application's errors or AWS Support.",
)
RUNTIME_METRICS = (
    "Invocations", "InputTokenCount", "OutputTokenCount", "EstimatedTPMQuotaUsage",
    "CacheReadInputTokenCount", "CacheWriteInputTokenCount", "InvocationThrottles",
    "InvocationClientErrors", "InvocationServerErrors", "OutputImageCount",
    "InvocationLatency",
)
MANTLE_METRICS = ("Inferences", "TotalInputTokens", "TotalOutputTokens", "InferenceClientErrors")
DIAGNOSTIC_METRICS = ("InvocationThrottles", "InvocationClientErrors", "InvocationServerErrors", "InferenceClientErrors")
# Namespaces probed with ListMetrics. A namespace that this account does not
# publish returns an empty list: it costs one call and is logged as empty, so an
# absent namespace is observable instead of assumed. Extend with --namespaces.
BEDROCK_NAMESPACES = (
    "AWS/Bedrock", "AWS/BedrockMantle", "AWS/Bedrock/Guardrails",
    "AWS/Bedrock/KnowledgeBase", "AWS/Bedrock/Agents",
)
# Curated names used to probe identifiers that ListMetrics does not list. Names
# the account does publish are discovered at runtime and added to these sets.
CURATED_METRICS = {"AWS/Bedrock": RUNTIME_METRICS, "AWS/BedrockMantle": MANTLE_METRICS}
# Statistics requested per metric. Counters carry their whole signal in Sum;
# gauges such as latency need a distribution, which Sum cannot express.
# GetMetricData accepts extended statistics (p99) directly in MetricStat.Stat.
COUNTER_STATS = ("Sum",)
GAUGE_STATS = ("Average", "Maximum", "p99")
# Suffixes used ONLY to pick statistics and scheduling priority, never to infer a
# quota mapping or to alter a reported value.
GAUGE_SUFFIXES = ("Latency", "TimeToFirstToken", "TimeToFirstByte")
DIAGNOSTIC_SUFFIXES = ("Errors", "Throttles")


def stats_for(name):
    """Statistics to request for a metric name, most significant first."""
    return GAUGE_STATS if name.endswith(GAUGE_SUFFIXES) else COUNTER_STATS


def is_diagnostic(name):
    return name in DIAGNOSTIC_METRICS or name.endswith(DIAGNOSTIC_SUFFIXES)
# Labels for the priority groups reported in the run log; see metric_priority().
PRIORITY_LABELS = {0: "diagnostics", 1: "discovered usage", 2: "known-id probes", 3: "administrative usage"}
BATCH_SIZE = 50
CHUNK_SECONDS = 86400
ALLOWED_OPERATIONS = {
    "sts": {"get_caller_identity"},
    "bedrock": {
        "list_foundation_models", "get_foundation_model_availability",
        "list_inference_profiles", "list_provisioned_model_throughputs",
    },
    "service-quotas": {"list_service_quotas", "list_aws_default_service_quotas"},
    "cloudwatch": {"list_metrics", "get_metric_data"},
    "ec2": {"describe_regions"},
}


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def serializable(value):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: serializable(v) for k, v in value.items() if k != "ResponseMetadata"}
    if isinstance(value, (tuple, list)):
        return [serializable(v) for v in value]
    return value


# Display priority for Regions in the report and HTML views. us-east-1 is always
# first, then the rest of North America, then Europe, then South America, then
# every other geography. Within each tier Regions are ordered alphabetically.
NORTH_AMERICA_PREFIXES = ("us-", "ca-", "mx-")


def region_sort_key(region):
    if region == "us-east-1":
        return (0, region)
    if region.startswith(NORTH_AMERICA_PREFIXES):
        return (1, region)
    if region.startswith("eu-"):
        return (2, region)
    if region.startswith("sa-"):
        return (3, region)
    return (4, region)


def order_regions(regions):
    """Order Regions by display priority: us-east-1, North America, Europe,
    South America, then others; alphabetical within each tier."""
    return sorted(regions, key=region_sort_key)


def percentile(values, quantile=0.95):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def metric_key(metric, stat="Sum"):
    dims = tuple(sorted((d["Name"], d["Value"]) for d in metric.get("Dimensions", [])))
    return metric["Namespace"], metric["MetricName"], dims, stat


def metric_id(region, metric, stat="Sum"):
    payload = repr((region, metric_key(metric, stat))).encode()
    return f"m{zlib.crc32(payload) & 0xffffffff:08x}{zlib.adler32(payload) & 0xffffffff:08x}"


def metric_priority(item):
    metric = item["metric"]
    if is_diagnostic(metric["MetricName"]):
        return 0
    if metric["Namespace"] == "AWS/Usage":
        return 3
    if item.get("sources") == ["known_model_id"]:
        return 2
    return 1


def covered(item, start, end):
    """Only explicitly completed query ranges establish coverage, not datapoints."""
    cursor = iso(start)
    for left, right in sorted(item.get("completed_windows", [])):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= iso(end):
            return True
    return False


def complete_window(item, start, end):
    merged = []
    for left, right in sorted(item.get("completed_windows", []) + [[iso(start), iso(end)]]):
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    item["completed_windows"] = merged


LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
# Column order for run_log.csv. Scalar columns only, so the file stays greppable
# and loads into a spreadsheet without JSON parsing.
LOG_FIELDS = (
    "attempt", "seq", "timestamp", "level", "phase", "region", "operation",
    "duration_ms", "status", "detail", "batch_size", "window_start", "window_end",
    "pages", "points", "requests_used", "datapoints_used",
)


class RunLog:
    """Thread-safe execution log exported next to the report.

    Every AWS call and every collection decision becomes one structured event.
    `run_log.csv` keeps the complete audit trail at all levels; `run_log.txt`
    keeps the readable narrative from `level` upwards. A resumed run appends to
    the saved history under a new attempt number, so one report carries the
    whole execution history that produced it.
    """

    def __init__(self, events=(), transcript=(), attempt=1, level="INFO", echo=True):
        self.lock = threading.Lock()
        self.events = [dict(event) for event in events]
        self.lines = list(transcript)
        self.attempt = attempt
        self.threshold = LOG_LEVELS.get(level, LOG_LEVELS["INFO"])
        self.echo = echo
        self.counts = Counter(event.get("level", "INFO") for event in self.events)
        self.seq = max((event.get("seq", 0) for event in self.events), default=0)

    @classmethod
    def restore(cls, saved, **options):
        """Continue the log of a saved report under the next attempt number."""
        previous = (saved or {}).get("run_log") or {}
        return cls(
            events=previous.get("events", ()), transcript=previous.get("transcript", ()),
            attempt=previous.get("attempt", 1) + 1, **options,
        )

    def add(self, level, phase, operation, region="", status="", detail="", **fields):
        """Record one event and, from the transcript level upwards, print it."""
        stamp = iso(datetime.now(UTC))
        show = LOG_LEVELS.get(level, 20) >= self.threshold and bool(detail)
        prefix = f"[{region}] " if region else ""
        tag = "" if level == "INFO" else f"{level} "
        # Hold the lock across the write so the transcript, the event list and
        # stdout keep one consistent order while Regions are collected in parallel.
        with self.lock:
            self.seq += 1
            event = {
                "attempt": self.attempt, "seq": self.seq, "timestamp": stamp, "level": level,
                "phase": phase, "region": region, "operation": operation,
                "status": status, "detail": detail,
            }
            event.update({k: v for k, v in fields.items() if v is not None})
            self.events.append(event)
            self.counts[level] += 1
            if show:
                self.lines.append(f"{stamp} {level:<5} {prefix}{detail}")
                if self.echo:
                    print(f"{prefix}{tag}{detail}", flush=True)
        return event

    def snapshot(self):
        return {
            "attempt": self.attempt, "levels": dict(self.counts),
            "events": self.events, "transcript": self.lines,
        }


def log_text(snapshot):
    """Render the transcript, plus a counts footer, as the exported run_log.txt."""
    snapshot = snapshot or {}
    lines = list(snapshot.get("transcript", []))
    counts = snapshot.get("levels", {})
    if counts:
        # The counts cover every attempt recorded in the snapshot, not just the last.
        summary = ", ".join(f"{level}={counts[level]}" for level in LOG_LEVELS if counts.get(level))
        attempts = snapshot.get("attempt", 1)
        lines.append(f"-- totals over {attempts} attempt(s): {summary}")
    return "".join(f"{line}\n" for line in lines)


def window(args, now=None):
    now = now or datetime.now(UTC)

    def parse(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Use ISO 8601 timestamps with a timezone, for example 2026-09-01T00:00:00Z.")
        return result.astimezone(UTC)

    end = parse(args.end) if args.end else now - timedelta(minutes=5)
    start = parse(args.start) if args.start else end - timedelta(days=args.days)
    if not start < end <= now:
        raise ValueError("The start must precede the end, and the end cannot be in the future.")
    age = (now - start).total_seconds() / 86400
    if age > 455:
        raise ValueError("The start is outside the 455-day CloudWatch retention window.")
    minimum = 60 if age < 15 else 300 if age < 63 else 3600
    period = minimum if args.period == "auto" else int(args.period)
    if period < minimum or period % minimum:
        raise ValueError(f"This history requires a period that is a multiple of {minimum} seconds.")
    # Trim edge buckets rather than including activity outside the requested window.
    start = datetime.fromtimestamp(math.ceil(start.timestamp() / period) * period, UTC)
    end = datetime.fromtimestamp(math.floor(end.timestamp() / period) * period, UTC)
    if start >= end:
        raise ValueError("The window is shorter than one complete interval.")
    return start, end, period


def merge_quotas(applied, defaults, region, collected_at):
    defaults_by_code = {q["QuotaCode"]: q for q in defaults}
    seen = {q["QuotaCode"] for q in applied}
    records = [(q, True) for q in applied]
    records += [(q, False) for q in defaults if q["QuotaCode"] not in seen]
    result = []
    for quota, is_applied in records:
        default = defaults_by_code.get(quota["QuotaCode"], {})
        value = quota.get("Value") if is_applied and not quota.get("ErrorReason") else None
        result.append({
            "region": region, "service_code": "bedrock", "quota_code": quota["QuotaCode"],
            "name": quota["QuotaName"], "applied_value": value,
            "default_value": default.get("Value"), "unit": quota.get("Unit", "None"),
            "adjustable": quota.get("Adjustable"), "global": quota.get("GlobalQuota", False),
            "level": quota.get("QuotaAppliedAtLevel", "ACCOUNT"),
            "context": quota.get("QuotaContext", {}), "period": quota.get("Period"),
            "usage_metric": quota.get("UsageMetric"), "error_reason": quota.get("ErrorReason"),
            "collected_at": collected_at, "source": "ListServiceQuotas" if is_applied else "ListAWSDefaultServiceQuotas",
            "description": quota.get("Description", ""),
            "comparison": {"status": "unmapped", "reason": "No confirmed mapping between this quota and a usage series."},
        })
    return result


class Collector:
    def __init__(self, session, args, start, end, period):
        self.session, self.args = session, args
        self.start, self.end, self.period = start, end, period
        self.clients = {}
        self.lock = threading.Lock()
        self.calls = Counter()
        self.log = RunLog(level=getattr(args, "log_level", "INFO"))
        # Regions are collected in parallel; the active phase is per thread.
        self.local = threading.local()
        # Namespaces to probe, and the metric names each Region/namespace pair
        # actually published, so expansion follows the account, not a fixed list.
        self.namespaces = list(dict.fromkeys(
            list(BEDROCK_NAMESPACES) + list(getattr(args, "namespaces", None) or [])))
        self.discovered = {}
        self.report = {
            "schema_version": "1.0", "collector_version": VERSION,
            "mapping_rules_version": TPM_RULES_VERSION,
            "generated_at": iso(datetime.now(UTC)), "profile": args.profile or "credential-chain",
            "start": iso(start), "end": iso(end), "period_seconds": period,
            "regions": [], "models": [], "inference_profiles": [], "provisioned_throughput": [],
            "quotas": [], "metrics": [], "collection_issues": [], "collections": [],
            "query_plan": [], "usage_skipped": args.skip_usage or args.plan,
            "deferred_metrics": [], "metric_inventory": [],
            "probed_namespaces": self.namespaces,
            "limitations": list(REPORT_LIMITATIONS),
        }
        self.metric_calls = 0
        self.returned_points = 0
        self.selected_count = 0

    @property
    def phase(self):
        return getattr(self.local, "phase", "run")

    @contextmanager
    def in_phase(self, phase):
        """Label every event raised by this thread until the block exits."""
        previous = self.phase
        self.local.phase = phase
        try:
            yield
        finally:
            self.local.phase = previous

    def client(self, service, region):
        key = service, region
        # Regions are collected concurrently; guard the lazy cache so two threads
        # do not build duplicate clients for the same service/Region pair.
        with self.lock:
            if key not in self.clients:
                from botocore.config import Config
                self.clients[key] = self.session.client(service, region_name=region, config=Config(
                    connect_timeout=10, read_timeout=30,
                    retries={"mode": "standard", "total_max_attempts":4},
                    max_pool_connections=8,
                ))
            return self.clients[key]

    def reserve_metrics(self, candidates):
        """Reserve an attempt's series budget; retain deferred identities for resume."""
        with self.lock:
            remaining = max(0, self.args.max_metrics - self.selected_count)
            selected = candidates[:remaining]
            self.selected_count += len(selected)
            existing = {m["id"] for m in self.report["metrics"]}
            self.report["metrics"].extend(m for m in selected if m["id"] not in existing)
            deferred = {m["id"]: m for m in self.report.get("deferred_metrics", [])}
            for item in candidates[remaining:]:
                item["status_reason"] = "series_budget_exhausted"
                if item["id"] not in existing:
                    deferred[item["id"]] = item
            for item in selected:
                deferred.pop(item["id"], None)
            self.report["deferred_metrics"] = list(deferred.values())
            return selected

    def budget_reason(self):
        if self.metric_calls >= self.args.max_metric_requests:
            return "request_budget_exhausted"
        if self.returned_points >= self.args.max_datapoints:
            return "datapoint_budget_exhausted"
        return None

    def reserve_metric_request(self):
        """Atomically claim one GetMetricData call against --max-metric-requests.

        Returns False once the shared budget (across all Regions) is exhausted.
        """
        with self.lock:
            if self.metric_calls >= self.args.max_metric_requests or self.returned_points >= self.args.max_datapoints:
                return False
            self.metric_calls += 1
            return True

    def add_returned_points(self, count):
        """Add newly returned datapoints to the shared counter under the lock."""
        with self.lock:
            self.returned_points += count

    def issue(self, region, operation, status, message, resource=""):
        with self.lock:
            self.report["collection_issues"].append({
                "region": region, "operation": operation, "status": status,
                "resource": resource, "message": str(message)[:900],
            })

    def call(self, service, region, operation, **kwargs):
        if operation not in ALLOWED_OPERATIONS.get(service, set()):
            raise ValueError(f"Operation is outside the read-only allowlist: {service}.{operation}")
        client = self.client(service, region)
        label = f"{service}.{operation}"
        if not hasattr(client, operation):
            self.issue(region, operation, "not_supported", "Update boto3: this operation is missing from the installed SDK.")
            self.log.add("ERROR", self.phase, label, region, "not_supported",
                         "Operation missing from the installed boto3 SDK.")
            return None
        with self.lock:
            self.calls[label] += 1
        started = time.perf_counter()
        try:
            response = getattr(client, operation)(**kwargs)
        except Exception as exc:
            elapsed = round((time.perf_counter()-started)*1000, 1)
            response = getattr(exc, "response", {})
            error = response.get("Error", {})
            code = error.get("Code", type(exc).__name__)
            status = "access_denied" if any(x in code.lower() for x in ("accessdenied", "unauthorized")) else "error"
            if code in ("UnknownServiceError", "UnknownEndpointError"):
                status = "not_supported"
            message = f"{code}: {error.get('Message', str(exc))}"
            self.issue(region, operation, status, message, kwargs.get("modelId", ""))
            # One event per AWS call, so the timeline keeps the latency of
            # failures as well as the error code that stopped the collection.
            self.log.add("WARN", self.phase, label, region, status,
                         f"{message} {kwargs.get('modelId', '')}".strip(), duration_ms=elapsed)
            return None
        self.log.add("DEBUG", self.phase, label, region, "ok",
                     str(kwargs.get("modelId", "")), duration_ms=round((time.perf_counter()-started)*1000, 1))
        return response

    def listing(self, service, region, operation, key, token_key="nextToken", **kwargs):  # nosec B107 - Pagination field name, not a credential.
        rows, visited, status = [], set(), "ok"
        started, pages = time.perf_counter(), 0
        for _ in range(1000):
            response = self.call(service, region, operation, **kwargs)
            pages += 1
            if response is None:
                status = "partial" if rows else "error"
                break
            rows.extend(response.get(key, []))
            token = response.get(token_key)
            if not token:
                break
            if token in visited:
                self.issue(region, operation, "partial", "Repeated pagination token; collection stopped.")
                self.log.add("WARN", self.phase, f"{service}.{operation}", region, "partial",
                             "Repeated pagination token; collection stopped.", pages=pages)
                status = "partial"
                break
            visited.add(token)
            kwargs[token_key] = token
        else:
            self.issue(region, operation, "partial", "Page limit reached.")
            self.log.add("WARN", self.phase, f"{service}.{operation}", region, "partial",
                         "Page limit reached after 1000 pages.", pages=pages)
            status = "partial"
        self.report["collections"].append({"region": region, "operation": operation, "status": status, "rows": len(rows)})
        self.log.add("DEBUG" if status == "ok" else "WARN", self.phase, f"{service}.{operation}",
                     region, status, f"{len(rows)} rows over {pages} page(s).",
                     duration_ms=round((time.perf_counter()-started)*1000, 1), pages=pages, points=len(rows))
        return rows

    def inventory(self, region):
        self.log.add("INFO", "inventory", "inventory", region, "started",
                     "Collecting the catalog, profiles, and provisioned capacity...")
        models = self.listing("bedrock", region, "list_foundation_models", "modelSummaries")
        for model in models:
            model["region"] = region
        self.report["models"].extend(models)
        profiles = self.listing("bedrock", region, "list_inference_profiles", "inferenceProfileSummaries", maxResults=100)
        for profile in profiles:
            profile.pop("description", None)
            profile["region"] = region
        self.report["inference_profiles"].extend(profiles)
        provisioned = self.listing("bedrock", region, "list_provisioned_model_throughputs", "provisionedModelSummaries", maxResults=100)
        for resource in provisioned:
            resource["region"] = region
        self.report["provisioned_throughput"].extend(provisioned)
        self.log.add("INFO", "inventory", "inventory", region, "ok",
                     f"{len(models)} models, {len(profiles)} profiles, {len(provisioned)} provisioned resources.")
        # Create the shared client before worker threads; boto3 clients support concurrent calls.
        self.client("bedrock", region)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self.call, "bedrock", region, "get_foundation_model_availability", modelId=m["modelId"]): m
                for m in models
            }
            for i, future in enumerate(as_completed(futures), 1):
                model = futures[future]
                availability = future.result()
                model["availability"] = serializable(availability) if availability is not None else None
                if i % 30 == 0 or i == len(models):
                    self.log.add("INFO", "inventory", "get_foundation_model_availability", region,
                                 "progress", f"Availability checked: {i}/{len(models)}.", points=i)

    def quotas(self, region):
        self.log.add("INFO", "quotas", "quotas", region, "started",
                     "Collecting applied quotas and AWS defaults, with pagination...")
        kwargs = {"ServiceCode": "bedrock", "MaxResults": 100}
        model = self.client("service-quotas", region).meta.service_model.operation_model("ListServiceQuotas")
        if "QuotaAppliedAtLevel" in model.input_shape.members:
            kwargs["QuotaAppliedAtLevel"] = "ALL"
        else:
            self.issue(region, "list_service_quotas", "partial", "This SDK supports only ACCOUNT-level queries; update it to query ALL levels.")
            self.log.add("WARN", "quotas", "list_service_quotas", region, "partial",
                         "This SDK supports only ACCOUNT-level queries; update it to query ALL levels.")
        applied = self.listing("service-quotas", region, "list_service_quotas", "Quotas", "NextToken", **kwargs)
        defaults = self.listing("service-quotas", region, "list_aws_default_service_quotas", "Quotas", "NextToken", ServiceCode="bedrock", MaxResults=100)
        merged = merge_quotas(applied, defaults, region, iso(datetime.now(UTC)))
        self.report["quotas"].extend(merged)
        self.log.add("INFO", "quotas", "quotas", region, "ok",
                     f"{len(merged)} quotas; {sum(q['applied_value'] is not None for q in merged)} with applied values.",
                     points=len(merged))

    def discover(self, region):
        candidates = {}

        def add(metric, source, stat="Sum"):
            metric = {k: metric[k] for k in ("Namespace", "MetricName", "Dimensions")}
            metric["Dimensions"] = sorted(metric["Dimensions"], key=lambda d: (d["Name"], d["Value"]))
            key = metric_key(metric, stat)
            if key not in candidates:
                candidates[key] = {
                    "id": metric_id(region, metric, stat), "region": region, "metric": metric,
                    "stat": stat, "period_seconds": self.period, "sources": [],
                    "points": [], "status": "not_queried", "messages": [],
                }
            if source not in candidates[key]["sources"]:
                candidates[key]["sources"].append(source)

        # Published names and dimension sets, per namespace, as ListMetrics
        # reports them. Nothing is filtered against a hardcoded name list: a
        # metric this account publishes is collected even if this tool predates it.
        names_seen, dimsets_seen = defaultdict(set), defaultdict(dict)
        for namespace in self.namespaces:
            rows = self.listing("cloudwatch", region, "list_metrics", "Metrics", "NextToken", Namespace=namespace)
            for metric in rows:
                names_seen[namespace].add(metric["MetricName"])
                dims = sorted(metric["Dimensions"], key=lambda d: (d["Name"], d["Value"]))
                if dims:
                    dimsets_seen[namespace][json.dumps(dims, sort_keys=True)] = dims
                for stat in stats_for(metric["MetricName"]):
                    add(metric, "ListMetrics", stat)
            unknown = sorted(names_seen[namespace] - set(CURATED_METRICS.get(namespace, ())))
            self.log.add("INFO" if rows else "DEBUG", "discover", "metric_inventory", region,
                         "ok" if rows else "empty_namespace",
                         f"{namespace}: {len(names_seen[namespace])} metric name(s), "
                         f"{len(dimsets_seen[namespace])} dimension set(s)."
                         + (f" Not in the curated list: {', '.join(unknown)}." if unknown else ""),
                         points=len(rows))
            with self.lock:
                self.report["metric_inventory"].append({
                    "region": region, "namespace": namespace,
                    "metric_names": sorted(names_seen[namespace]),
                    "dimension_names": sorted({d["Name"] for dims in dimsets_seen[namespace].values() for d in dims}),
                    "dimension_sets": len(dimsets_seen[namespace]),
                    "not_in_curated_list": unknown,
                })
                self.discovered[(region, namespace)] = set(names_seen[namespace])
        # Fill the name x dimension-set matrix per namespace. Both axes come from
        # ListMetrics, so every probe targets a combination AWS already reported;
        # this is what recovers multi-dimension series such as Model + Project.
        # Prepare these before any history retrieval: discovery identifies useful
        # dimensions even when retrieving their history is expensive.
        for namespace, dimsets in dimsets_seen.items():
            for name in sorted(names_seen[namespace] | set(CURATED_METRICS.get(namespace, ()))):
                for dims in dimsets.values():
                    for stat in stats_for(name):
                        add({"Namespace": namespace, "MetricName": name, "Dimensions": dims},
                            "discovered_id_expansion", stat)
        # Account-level diagnostics include failures without a resolved ModelId.
        if (any(m["metric"]["Namespace"] == "AWS/Bedrock" for m in candidates.values())
                or self.args.model_ids or getattr(self.args, "include_inactive_models", False)):
            for name in set(RUNTIME_METRICS) | names_seen.get("AWS/Bedrock", set()):
                for stat in stats_for(name):
                    add({"Namespace": "AWS/Bedrock", "MetricName": name, "Dimensions": []}, "account_probe", stat)
        for quota in self.report["quotas"]:
            usage = quota.get("usage_metric")
            if quota["region"] != region or not usage:
                continue
            if usage["MetricNamespace"] == "AWS/Usage" and not getattr(self.args, "include_api_usage", False):
                continue
            stat = usage.get("MetricStatisticRecommendation")
            if stat not in ("Sum", "Maximum", "Minimum", "Average", "SampleCount"):
                continue
            metric = {
                "Namespace": usage["MetricNamespace"], "MetricName": usage["MetricName"],
                "Dimensions": [{"Name": k, "Value": v} for k, v in usage.get("MetricDimensions", {}).items()],
            }
            add(metric, "ServiceQuotas.UsageMetric", stat)
            quota["usage_metric_id"] = metric_id(region, metric, stat)
        # Query documented ModelId series even if ListMetrics has not listed them.
        for model_id_value in self.args.model_ids or []:
            for name in set(RUNTIME_METRICS) | names_seen.get("AWS/Bedrock", set()):
                for stat in stats_for(name):
                    add({"Namespace": "AWS/Bedrock", "MetricName": name, "Dimensions": [
                        {"Name": "ModelId", "Value": model_id_value},
                    ]}, "explicit_model_id", stat)
        known_ids = set()
        if getattr(self.args, "include_inactive_models", False):
            known_ids.update(m["modelId"] for m in self.report["models"] if m["region"] == region)
            known_ids.update(p["inferenceProfileId"] for p in self.report["inference_profiles"] if p["region"] == region)
            known_ids.update(p["provisionedModelArn"] for p in self.report["provisioned_throughput"] if p["region"] == region)
        for model_id_value in sorted(known_ids):
            # First probe one activity counter; expand historical IDs only when data exists.
            add({"Namespace": "AWS/Bedrock", "MetricName": "Invocations", "Dimensions": [
                {"Name": "ModelId", "Value": model_id_value},
            ]}, "known_model_id")
        # Reserve only after all Regions have been discovered, in priority order.
        selected = sorted(candidates.values(), key=lambda m: (metric_priority(m), m["id"]))
        discovered = sum("ListMetrics" in m["sources"] for m in selected)
        plan = {
            "region": region, "series": len(selected), "discovered_series": discovered,
            "known_id_probes": sum("known_model_id" in m["sources"] for m in selected),
            "period_seconds": self.period,
            "max_possible_points": len(selected) * int((self.end-self.start).total_seconds()/self.period),
            "minimum_window_requests": sum(
                math.ceil(sum(metric_priority(m) == priority for m in selected)/BATCH_SIZE)
                for priority in range(4)
            ) * math.ceil((self.end-self.start).total_seconds()/CHUNK_SECONDS),
        }
        with self.lock:
            self.report["query_plan"].append(plan)
        counts = Counter(metric_priority(m) for m in selected)
        self.log.add("INFO", "discover", "query_plan", region, "ok",
                     f"Plan: {len(selected)} series ({discovered} discovered), {self.period}s periods; "
                     f"priorities {dict(sorted(counts.items()))}; "
                     f"{plan['minimum_window_requests']} GetMetricData requests minimum against a budget of "
                     f"{self.args.max_metric_requests}.",
                     points=len(selected), requests_used=self.metric_calls)
        if plan["minimum_window_requests"] > self.args.max_metric_requests:
            # The budget cannot cover this plan; say so before the run silently
            # truncates the lowest priorities.
            self.log.add("WARN", "discover", "query_plan", region, "insufficient_budget",
                         f"This Region alone needs {plan['minimum_window_requests']} requests but the run budget is "
                         f"{self.args.max_metric_requests}. Lower priorities will be deferred; raise "
                         f"--max-metric-requests or narrow --regions.")
        return selected

    def expand_observed(self, region):
        # Snapshot the shared metric list under the lock; other Regions may be
        # appending to it concurrently.
        with self.lock:
            snapshot = list(self.report["metrics"])
        existing = {m["id"] for m in snapshot + self.report.get("deferred_metrics", [])}
        candidates = []
        for observed in snapshot:
            if observed["region"] != region or not observed["points"]:
                continue
            dimensions = observed["metric"]["Dimensions"]
            namespace = observed["metric"]["Namespace"]
            # Expand the dimension sets this identifier actually published, of any
            # width, so combinations such as Model + Project are not lost. The
            # account rollup is still excluded: do not fabricate aggregates.
            if not dimensions or namespace not in self.namespaces:
                continue
            names = set(CURATED_METRICS.get(namespace, ())) | self.discovered.get((region, namespace), set())
            for name in sorted(names):
                for stat in stats_for(name):
                    metric = {"Namespace": namespace, "MetricName": name, "Dimensions": dimensions}
                    key = metric_id(region, metric, stat)
                    if key in existing:
                        continue
                    existing.add(key)
                    candidates.append({
                        "id": key, "region": region, "metric": metric, "stat": stat,
                        "period_seconds": self.period, "sources": ["observed_id_expansion"],
                        "points": [], "status": "not_queried", "messages": [],
                    })
        return candidates

    def fetch_metrics(self, region, metrics):
        """Compatibility entry point for one Region; run() schedules all Regions."""
        for item in metrics:
            item.setdefault("region", region)
        self.fetch_scheduled(metrics)

    def fetch_scheduled(self, metrics):
        """Diagnostics first; newest daily window first; rotate Regions per window."""
        groups = defaultdict(list)
        for item in metrics:
            if item.get("status") in ("ok", "no_data"):
                continue
            item["messages"] = []
            item.pop("status_reason", None)
            groups[(metric_priority(item), item["region"])].append(item)
        for priority in sorted({key[0] for key in groups}):
            pending_series = sum(len(groups[key]) for key in groups if key[0] == priority)
            self.log.add("INFO", "history", "priority_group", "", "started",
                         f"Priority {priority} ({PRIORITY_LABELS.get(priority, 'other')}): "
                         f"{pending_series} series, newest daily window first.",
                         points=pending_series, requests_used=self.metric_calls)
            end = self.end
            while end > self.start:
                start = max(self.start, end-timedelta(seconds=CHUNK_SECONDS))
                regional_batches = {}
                for rank, region in sorted(groups, key=lambda k: (k[0], region_sort_key(k[1]))):
                    if rank != priority:
                        continue
                    pending = [m for m in groups[(rank, region)] if not covered(m, start, end)]
                    regional_batches[region] = [pending[i:i+BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
                for index in range(max((len(v) for v in regional_batches.values()), default=0)):
                    for region, batches in regional_batches.items():
                        if index >= len(batches):
                            continue
                        if self.budget_reason():
                            self.mark_budget_stop(metrics)
                            return
                        self.fetch_window(region, batches[index], start, end)
                        if self.budget_reason() and any(m.get("status") not in ("ok", "no_data") for m in metrics):
                            self.mark_budget_stop(metrics)
                            return
                end = start

    def mark_budget_stop(self, metrics):
        reason = self.budget_reason()
        for item in metrics:
            if item.get("status") not in ("ok", "no_data") and not item.get("status_reason"):
                item["status"] = "partial" if item["points"] or item.get("completed_windows") else "not_queried"
                item["status_reason"] = reason
        self.issue("all", "get_metric_data", "partial",
                   f"Local {reason}: {self.metric_calls}/{self.args.max_metric_requests} requests, "
                   f"{self.returned_points}/{self.args.max_datapoints} datapoints. Resume the saved report.")
        stopped = Counter(m["metric"]["MetricName"] for m in metrics if m.get("status_reason") == reason)
        self.log.add("ERROR", "history", "budget_stop", "", reason,
                     f"Collection stopped on the local {reason} with "
                     f"{sum(stopped.values())} series incomplete: "
                     f"{', '.join(f'{name}x{count}' for name, count in stopped.most_common(8)) or 'none'}. "
                     f"Resume the saved report to finish them.",
                     requests_used=self.metric_calls, datapoints_used=self.returned_points)

    def fetch_window(self, region, batch, start, end):
        points = {m["id"]: dict(m["points"]) for m in batch}
        received = set()
        states, messages = {}, defaultdict(list)
        lookup = {m["id"]: m for m in batch}
        query = {
            "StartTime": start, "EndTime": end, "ScanBy": "TimestampDescending",
            "MaxDatapoints": min(100800, self.args.max_datapoints-self.returned_points),
            "MetricDataQueries": [{
                "Id": m["id"], "MetricStat": {"Metric": m["metric"], "Period": self.period, "Stat": m["stat"]},
                "ReturnData": True,
            } for m in batch],
        }
        seen_tokens, failure, attempted = set(), None, False
        pages, started = 0, time.perf_counter()
        names = Counter(m["metric"]["MetricName"] for m in batch)
        label = ", ".join(f"{name}x{count}" for name, count in names.most_common(4))
        while True:
            if not self.reserve_metric_request():
                failure = self.budget_reason()
                break
            attempted = True
            response = self.call("cloudwatch", region, "get_metric_data", **query)
            pages += 1
            if response is None:
                failure = "api_error"
                break
            for message in response.get("Messages", []):
                self.issue(region, "get_metric_data", "partial", message.get("Value", message))
                # Request-scoped message: record which batch it lands on, because
                # it suppresses window completion for every series in the batch.
                self.log.add("WARN", "history", "get_metric_data", region, "service_message",
                             f"{message.get('Code', 'Message')}: {message.get('Value', message)}",
                             batch_size=len(batch), window_start=iso(start), window_end=iso(end), pages=pages)
                failure = "service_message"
            new_points = 0
            for result in response.get("MetricDataResults", []):
                key = result["Id"]
                if key not in lookup:
                    continue
                states[key] = result.get("StatusCode", "Unknown")
                messages[key].extend(result.get("Messages", []))
                for timestamp, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                    if start <= timestamp < end and math.isfinite(value):
                        stamp = iso(timestamp)
                        if (key, stamp) not in received:
                            new_points += 1
                            received.add((key, stamp))
                        points[key][stamp] = value
            self.add_returned_points(new_points)
            token = response.get("NextToken")
            if not token:
                break
            if token in seen_tokens:
                failure = "repeated_pagination_token"
                self.issue(region, "get_metric_data", "partial", "Repeated pagination token.")
                self.log.add("WARN", "history", "get_metric_data", region, failure,
                             "Repeated pagination token; stopped paging this batch.",
                             batch_size=len(batch), window_start=iso(start), window_end=iso(end), pages=pages)
                break
            seen_tokens.add(token)
            query["NextToken"] = token
        # One summary event per batch/window. `MissingResult` counts the query IDs
        # CloudWatch never returned, which is how diagnostic series silently
        # disappear when a batch overflows MaxDatapoints.
        codes = Counter(states.get(m["id"], "MissingResult") for m in batch)
        collected = sum(len(points[m["id"]]) for m in batch)
        level = "INFO"
        if codes.get("MissingResult") or failure:
            level = "ERROR" if not collected else "WARN"
        self.log.add(level, "history", "get_metric_data", region, failure or "complete",
                     f"{len(batch)} series [{label}] over {iso(start)[:16]}Z..{iso(end)[:16]}Z: "
                     f"{collected} points in {pages} page(s); statuses {dict(codes.most_common())}"
                     + (f"; failure={failure}" if failure else ""),
                     duration_ms=round((time.perf_counter()-started)*1000, 1),
                     batch_size=len(batch), window_start=iso(start), window_end=iso(end),
                     pages=pages, points=collected,
                     requests_used=self.metric_calls, datapoints_used=self.returned_points)
        for item in batch:
            key = item["id"]
            item["points"] = [[t, v] for t, v in sorted(points[key].items())]
            item.setdefault("messages", []).extend(messages[key])
            code = states.get(key, "MissingResult")
            if failure is None and code == "Complete" and not messages[key]:
                complete_window(item, start, end)
            elif failure in ("request_budget_exhausted", "datapoint_budget_exhausted"):
                item["status_reason"] = failure
            else:
                item["status_reason"] = failure or code
                if attempted:
                    self.issue(region, "get_metric_data", "partial" if item["points"] else "error",
                               f"{iso(start)} to {iso(end)}: {failure or code}. {messages[key]}", key)
            if covered(item, self.start, self.end):
                item["status"] = "ok" if item["points"] else "no_data"
                item.pop("status_reason", None)
            elif item["points"] or item.get("completed_windows"):
                item["status"] = "partial"
            elif item.get("status_reason") in ("request_budget_exhausted", "datapoint_budget_exhausted"):
                item["status"] = "not_queried"
            else:
                item["status"] = "error"

    def collect_region(self, region):
        """Discover inventory and candidates without spending the history budget."""
        with self.in_phase("inventory"):
            self.inventory(region)
        with self.in_phase("quotas"):
            self.quotas(region)
        if not self.args.skip_usage:
            with self.in_phase("discover"):
                return self.discover(region)
        return []

    def run(self, regions):
        identity = self.call("sts", regions[0], "get_caller_identity")
        if identity is None:
            raise RuntimeError("Could not identify the account. Refresh the profile credentials.")
        self.report["account_id"] = identity["Account"]
        self.report["partition"] = identity["Arn"].split(":")[1]
        self.report["regions"] = regions
        self.log.add("INFO", "start", "collection", "", "started",
                     f"Account {identity['Account']} | profile {self.args.profile or 'credential-chain'} | {', '.join(regions)}")
        self.log.add("INFO", "start", "collection", "", "window",
                     f"UTC window {iso(self.start)} -> {iso(self.end)} | {self.period}s | "
                     f"budgets: {self.args.max_metric_requests} requests, {self.args.max_datapoints:,} datapoints, "
                     f"{self.args.max_metrics} series")
        # Collect Regions concurrently. Global budgets (--max-metrics,
        # --max-datapoints, --max-metric-requests) remain shared across all
        # Regions via atomic reservations, so total cost stays capped.
        candidates = []
        workers = max(1, min(self.args.region_workers, len(regions)))
        if workers == 1 or len(regions) == 1:
            for region in regions:
                candidates.extend(self.collect_region(region))
        else:
            self.log.add("INFO", "start", "collection", "", "parallel",
                         f"Collecting {len(regions)} Regions with up to {workers} in parallel.")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self.collect_region, region): region for region in regions}
                for future in as_completed(futures):
                    region = futures[future]
                    # Surface unexpected failures without aborting the other Regions.
                    try:
                        candidates.extend(future.result())
                    except Exception as exc:
                        self.issue(region, "collect_region", "error", f"Region collection failed: {exc}")
                        self.log.add("ERROR", "collect_region", "collect_region", region, "error",
                                     f"Region collection failed: {type(exc).__name__}: {exc}")
        self.collect_candidates(candidates)
        return self.finish()

    def collect_candidates(self, candidates):
        candidates.sort(key=lambda m: (metric_priority(m), region_sort_key(m["region"]), m["id"]))
        selected = self.reserve_metrics(candidates)
        if len(selected) < len(candidates):
            self.issue("all", "query_plan", "partial",
                       f"Local series budget exhausted: {len(candidates)-len(selected)} series deferred. Resume to collect them.")
            self.log.add("WARN", "history", "query_plan", "", "series_budget_exhausted",
                         f"{len(candidates)-len(selected)} of {len(candidates)} series deferred by --max-metrics "
                         f"({self.args.max_metrics}). Resume to collect them.", points=len(selected))
        if not self.args.skip_usage and not self.args.plan:
            with self.in_phase("history"):
                self.fetch_scheduled(selected)
                # Inactive catalog probes may discover historical identifiers.
                extra = [m for region in self.report["regions"] for m in self.expand_observed(region)]
                if extra:
                    self.log.add("INFO", "history", "expand_observed", "", "ok",
                                 f"{len(extra)} additional series discovered from observed identifiers.",
                                 points=len(extra))
                    extra.sort(key=lambda m: (metric_priority(m), region_sort_key(m["region"]), m["id"]))
                    self.fetch_scheduled(self.reserve_metrics(extra))

    def resume(self, saved):
        """Reuse inventory, retry incomplete windows, and write a new report later."""
        if not saved.get("metrics") and not saved.get("deferred_metrics"):
            raise ValueError("The report has no saved metric identities. Start a new usage collection.")
        regions = saved["regions"]
        # Continue the saved execution history under the next attempt number, so a
        # resumed report still shows what the earlier attempts did.
        self.log = RunLog.restore(saved, level=getattr(self.args, "log_level", "INFO"))
        identity = self.call("sts", regions[0], "get_caller_identity")
        if identity is None or identity["Account"] != saved["account_id"] or identity["Arn"].split(":")[1] != saved["partition"]:
            raise ValueError("Resume requires credentials for the original account and partition.")
        self.report = copy.deepcopy(saved)
        # A report saved before namespace/stat discovery has neither key.
        self.report.setdefault("metric_inventory", [])
        self.report.setdefault("probed_namespaces", self.namespaces)
        for entry in self.report["metric_inventory"]:
            self.discovered[(entry["region"], entry["namespace"])] = set(entry["metric_names"])
        self.report.setdefault("previous_attempts", []).append({
            "completed_at": saved.get("completed_at"),
            "collector_version": saved.get("resume_collector_version", saved["collector_version"]),
            "collection_settings": saved.get("collection_settings"),
            "api_calls": saved.get("api_calls", {}), "collection_issues": saved["collection_issues"],
        })
        self.report["collection_issues"] = [
            i for i in saved["collection_issues"]
            if i["operation"] not in ("get_metric_data", "query_plan", "expand_observed")
        ]
        self.report["profile"] = self.args.profile or "credential-chain"
        self.report["usage_skipped"] = False
        self.report["resumed_at"] = iso(datetime.now(UTC))
        self.report["resume_collector_version"] = VERSION
        candidates = [m for m in self.report["metrics"] if m["status"] not in ("ok", "no_data")]
        candidates.extend(self.report.get("deferred_metrics", []))
        self.log.add("INFO", "start", "resume", "", "started",
                     f"Attempt {self.log.attempt} on account {saved['account_id']}: retrying {len(candidates)} "
                     f"incomplete or deferred series over the saved window {saved['start']} -> {saved['end']}.",
                     points=len(candidates))
        self.collect_candidates(candidates)
        return self.finish()

    def finish(self):
        self.report["completed_at"] = iso(datetime.now(UTC))
        self.report["api_calls"] = dict(self.calls)
        self.report["returned_datapoints"] = sum(len(m["points"]) for m in self.report["metrics"])
        self.report["collection_settings"] = {
            "max_metrics": self.args.max_metrics, "max_metric_requests": self.args.max_metric_requests,
            "max_datapoints": self.args.max_datapoints, "selected_series": self.selected_count,
            "metric_requests_used": self.metric_calls, "datapoints_received": self.returned_points,
            "batch_size": BATCH_SIZE, "window_seconds": CHUNK_SECONDS, "scan_by": "TimestampDescending",
            "include_inactive_models": getattr(self.args, "include_inactive_models", False),
            "include_api_usage": getattr(self.args, "include_api_usage", False),
        }
        statuses = Counter(m["status"] for m in self.report["metrics"])
        self.log.add("INFO", "finish", "collection", "", "ok",
                     f"Collection complete: {len(self.report['collection_issues'])} issues, "
                     f"{self.report['returned_datapoints']:,} datapoints, series {dict(statuses.most_common())}.",
                     requests_used=self.metric_calls, datapoints_used=self.returned_points)
        self.report["run_log"] = self.log.snapshot()
        self.report = serializable(self.report)
        analyze(self.report)
        return self.report


def analyze(report):
    """Statistics are over observed points, never zero-filled or deduplicated across dimensions."""
    report["mapping_rules_version"] = TPM_RULES_VERSION
    # Regenerate application-owned explanations in English when rendering older snapshots.
    # Preserve the original collector version, collection timestamps, and AWS resource data.
    report["limitations"] = list(REPORT_LIMITATIONS)
    report["language"] = "en"
    report["renderer_version"] = VERSION
    # Reports saved before namespace/statistic discovery carry neither key.
    report.setdefault("metric_inventory", [])
    report.setdefault("probed_namespaces", list(BEDROCK_NAMESPACES))
    expected = int((datetime.fromisoformat(report["end"].replace("Z","+00:00")) -
                    datetime.fromisoformat(report["start"].replace("Z","+00:00"))).total_seconds() /
                   report["period_seconds"])
    for metric in report["metrics"]:
        values = [p[1] for p in metric["points"]]
        rate = [v * 60 / metric["period_seconds"] for v in values] if metric["stat"] == "Sum" else []
        metric["summary"] = {
            "total": sum(values) if values and metric["stat"] == "Sum" else None,
            "max": max(values) if values else None, "p95": percentile(values),
            "peak_per_minute": max(rate) if rate else None, "p95_per_minute": percentile(rate),
            "observed_points": len(values), "expected_intervals": expected,
            "missing_intervals": max(0, expected-len(values)),
        }
    by_id = {m["id"]: m for m in report["metrics"]}
    for quota in report["quotas"]:
        quota["comparison"] = {"status":"unmapped", "reason":"No confirmed mapping between this quota and a usage series."}
        if quota["quota_code"] == "L-E3F10727" and quota["name"] == "Cross-Model Max Tokens Per Day":
            quota["comparison"] = {
                "status": "unsupported",
                "reason": "Daily pricing-based quota. Raw token counts and EstimatedTPMQuotaUsage cannot establish its consumption. Obtain quota-specific evidence from AWS Support and application errors.",
            }
            continue
        metric = by_id.get(quota.get("usage_metric_id"))
        if not metric:
            continue
        # Explicit usage metadata provides identity, but NOT necessarily compatible units.
        quota["comparison"] = {"status": "unmapped", "reason": "UsageMetric found; the quota unit and period have not been confirmed."}
        period = quota.get("period") or {}
        duration = period.get("PeriodValue", 0) * {"SECOND":1, "MINUTE":60, "HOUR":3600, "DAY":86400}.get(period.get("PeriodUnit"), 0)
        if (duration == 60 and metric["stat"] == "Sum" and not quota["global"] and
                quota.get("unit") in ("Count", "None") and quota["applied_value"] is not None and
                quota["applied_value"] > 0 and metric["status"] == "ok" and metric["summary"]["peak_per_minute"] is not None):
            quota["comparison"] = {
                "status": "estimated", "source": "ServiceQuotas.UsageMetric",
                "metric_id": metric["id"], "peak_per_minute": metric["summary"]["peak_per_minute"],
                "reason": "Observed usage normalized per minute against the current quota; it may differ from the occupancy used for throttling.",
                "peak_percent": 100 * metric["summary"]["peak_per_minute"] / quota["applied_value"],
            }
    for quota in report["quotas"]:
        rule = TPM_RULES.get(quota["quota_code"])
        if not rule or quota["name"] != rule[0] or quota["level"] != "ACCOUNT" or quota["global"] or quota["context"]:
            continue
        profiles = [p for p in report.get("inference_profiles", []) if
                    p["region"] == quota["region"] and p["inferenceProfileId"] == rule[1]
                    and p["type"] == "SYSTEM_DEFINED" and p.get("models")
                    and all(m["modelArn"].split("/")[-1] == rule[2] for m in p["models"])]
        metric = by_id.get(metric_id(quota["region"], {
            "Namespace":"AWS/Bedrock", "MetricName":"EstimatedTPMQuotaUsage",
            "Dimensions":[{"Name":"ModelId","Value":rule[1]}],
        }))
        if len(profiles) != 1 or not metric or metric["stat"] != "Sum" or not quota["applied_value"] or quota["applied_value"] < 0:
            continue
        if metric["status"] != "ok":
            quota["comparison"] = {"status": "incomplete", "reason": "The model/profile mapping is validated, but the usage query is incomplete or has no datapoints. Resume collection before comparing utilization."}
            continue
        peak = metric["summary"]["peak_per_minute"]
        quota["comparison"] = {
            "status":"estimated", "source":"AWS/Bedrock.EstimatedTPMQuotaUsage",
            "rule_version":TPM_RULES_VERSION, "metric_id":metric["id"], "profile_id":rule[1],
            "peak_per_minute":peak, "peak_percent":100*peak/quota["applied_value"],
            "p95_percent":100*metric["summary"]["p95_per_minute"]/quota["applied_value"],
            "reason":"Peak for this profile's series against the current quota. This estimate excludes upfront reservations and does not guarantee coverage of other profiles sharing the quota.",
        }
    for quota in report["quotas"]:
        rule = MANTLE_RULES.get(quota["quota_code"])
        if not rule or quota["name"] != rule[0] or quota["level"] != "ACCOUNT" or quota["global"] or quota["context"]:
            continue
        metric = by_id.get(metric_id(quota["region"], {
            "Namespace": "AWS/BedrockMantle", "MetricName": rule[2],
            "Dimensions": [{"Name": "Model", "Value": rule[1]}],
        }))
        if not metric or metric["stat"] != "Sum" or not quota["applied_value"] or quota["applied_value"] < 0:
            continue
        if metric["status"] != "ok":
            quota["comparison"] = {"status": "incomplete", "reason": "The Mantle model and token direction are mapped, but the usage query is incomplete or has no datapoints."}
            continue
        peak = metric["summary"]["peak_per_minute"]
        quota["comparison"] = {
            "status": "estimated", "source": f"AWS/BedrockMantle.{rule[2]}",
            "rule_version": TPM_RULES_VERSION, "metric_id": metric["id"],
            "peak_per_minute": peak, "peak_percent": 100*peak/quota["applied_value"],
            "p95_percent": 100*metric["summary"]["p95_per_minute"]/quota["applied_value"],
            "reason": "Observed billable tokens against the matching Mantle input/output quota in this Region. Cached input accounting and upfront reservations can differ. HTTP 429 responses require application logs; low observed usage does not rule out throttling.",
        }
    # Requests per minute against Invocations. Unlike the token quotas there is
    # no estimator metric in between: Invocations is the count the quota limits.
    targets = {}
    for quota in report["quotas"]:
        if quota["comparison"]["status"] != "unmapped":
            continue
        matched = RPM_QUOTA_NAME.match(quota["name"] or "")
        if (not matched or quota["level"] != "ACCOUNT" or quota["global"] or quota["context"]
                or not quota["applied_value"] or quota["applied_value"] <= 0):
            continue
        region = quota["region"]
        if region not in targets:
            targets[region] = rpm_targets(report, region)
        scope = RPM_SCOPES[matched.group("scope")]
        model_id = targets[region][scope].get(normalize_label(matched.group("model")))
        if not model_id:
            quota["comparison"] = {
                "status": "unmapped",
                "reason": "Requests-per-minute quota whose model label does not resolve to exactly one "
                          "collected profile or on-demand model in this Region.",
            }
            continue
        metric = by_id.get(metric_id(region, {
            "Namespace": "AWS/Bedrock", "MetricName": "Invocations",
            "Dimensions": [{"Name": "ModelId", "Value": model_id}],
        }))
        if not metric or metric["stat"] != "Sum":
            continue
        if metric["status"] != "ok":
            quota["comparison"] = {
                "status": "incomplete", "model_id": model_id,
                "reason": "The model label resolves to one identity, but the Invocations query is incomplete or has no datapoints.",
            }
            continue
        peak = metric["summary"]["peak_per_minute"]
        throttles = by_id.get(metric_id(region, {
            "Namespace": "AWS/Bedrock", "MetricName": "InvocationThrottles",
            "Dimensions": [{"Name": "ModelId", "Value": model_id}],
        }))
        quota["comparison"] = {
            "status": "estimated", "source": "AWS/Bedrock.Invocations",
            "rule_version": RPM_RULES_VERSION, "metric_id": metric["id"],
            "model_id": model_id, "scope": scope,
            "peak_per_minute": peak, "peak_percent": 100*peak/quota["applied_value"],
            "p95_percent": 100*metric["summary"]["p95_per_minute"]/quota["applied_value"],
            "throttles_total": (throttles or {}).get("summary", {}).get("total"),
            "reason": "Accepted requests per minute against this quota. Invocations counts accepted requests, "
                      "so rejected ones are not included: a peak at the limit together with throttling means "
                      "demand exceeded it, and the excess is not visible in this percentage.",
        }
    # Where throttling was recorded, the accepted rate during the throttled
    # intervals is a measurement of the ceiling that was in force, and it needs
    # no quota mapping. Reported for every throttled model, including those whose
    # quota label could not be resolved above.
    report["throttle_evidence"] = []
    for metric in report["metrics"]:
        definition = metric.get("metric") or {}
        dimensions = definition.get("Dimensions") or []
        if (definition.get("Namespace") != "AWS/Bedrock" or definition.get("MetricName") != "InvocationThrottles"
                or metric["stat"] != "Sum" or not (metric["summary"]["total"] or 0)
                or len(dimensions) != 1 or dimensions[0]["Name"] != "ModelId"):
            continue
        region, model_id = metric.get("region"), dimensions[0]["Value"]
        accepted = by_id.get(metric_id(region, {
            "Namespace": "AWS/Bedrock", "MetricName": "Invocations", "Dimensions": dimensions,
        }))
        if not accepted or accepted["status"] != "ok":
            continue
        by_stamp = dict(accepted["points"])
        factor = 60 / metric["period_seconds"]
        throttled = [stamp for stamp, value in metric["points"] if value > 0]
        rates = sorted({by_stamp.get(stamp, 0) * factor for stamp in throttled})
        ceiling = max(rates) if rates else None
        # A quota can only apply to this identity if its scope agrees: a regional
        # profile is reached by Cross-region, a global one by Global cross-region,
        # and a bare model id by On-demand. No model label is parsed here.
        scope = ("global" if model_id.startswith("global.")
                 else "cross" if GEO_PREFIX.match(model_id) else "on_demand")
        candidates = []
        for quota in report["quotas"]:
            named = RPM_QUOTA_NAME.match(quota["name"] or "")
            if (quota["region"] != region or not named or quota["applied_value"] != ceiling
                    or RPM_SCOPES[named.group("scope")] != scope):
                continue
            candidates.append({"quota_code": quota["quota_code"], "name": quota["name"],
                               "applied_value": quota["applied_value"]})
        report["throttle_evidence"].append({
            "region": region, "model_id": model_id, "scope": scope,
            "throttles_total": metric["summary"]["total"],
            "throttled_intervals": len(throttled),
            "accepted_peak_per_minute": accepted["summary"]["peak_per_minute"],
            "accepted_per_minute_while_throttled": rates,
            "observed_ceiling_per_minute": ceiling,
            "matching_request_quotas": candidates[:8],
            "matching_request_quota_count": len(candidates),
            "reason": "Accepted requests per minute in the intervals that recorded throttling. A rate that holds "
                      "steady across those intervals is the ceiling that applied, measured rather than mapped. "
                      "Quotas are listed only when their applied value equals that rate and their scope fits this "
                      "identity; they are candidates to confirm, not a mapping.",
        })
    report["throttle_evidence"].sort(key=lambda e: -e["throttles_total"])
    report["diagnostic_coverage"] = []
    for region in report.get("regions", []):
        metrics = [m for m in report["metrics"] if m["region"] == region]
        throttles = [m for m in metrics if m["metric"]["Namespace"] == "AWS/Bedrock" and m["metric"]["MetricName"] == "InvocationThrottles"]
        observed = [m for m in metrics if m["points"]]
        report["diagnostic_coverage"].append({
            "region": region,
            "runtime_throttle_series": len(throttles),
            "runtime_throttle_queries_complete": sum(m["status"] in ("ok", "no_data") for m in throttles),
            "incomplete_series": sum(m["status"] not in ("ok", "no_data") for m in metrics),
            "deferred_series": sum(m["region"] == region for m in report.get("deferred_metrics", [])),
            "mantle_requires_application_errors": any(m["metric"]["Namespace"] == "AWS/BedrockMantle" for m in metrics),
            # What this Region actually published, so an absent signal is
            # distinguishable from a signal this tool did not request.
            "namespaces_with_data": sorted({m["metric"]["Namespace"] for m in observed}),
            "diagnostic_metric_names": sorted({m["metric"]["MetricName"] for m in metrics
                                               if is_diagnostic(m["metric"]["MetricName"])}),
            "gauge_metric_names": sorted({m["metric"]["MetricName"] for m in metrics if m["stat"] != "Sum"}),
            "statistics_collected": sorted({m["stat"] for m in metrics}),
            "multi_dimension_series": sum(len(m["metric"]["Dimensions"]) > 1 for m in metrics),
        })


def safe_csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def csv_text(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: safe_csv_value(row.get(field)) for field in fields})
    return buffer.getvalue()


def atomic_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def export(report, directory, collect_only=False):
    directory.mkdir(parents=True, exist_ok=True)
    atomic_text(directory/"report.json", json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    definitions = [
        ("models", ["region", "modelId", "modelName", "providerName", "inferenceTypesSupported", "modelLifecycle", "availability"]),
        ("inference_profiles", ["region", "inferenceProfileId", "inferenceProfileName", "inferenceProfileArn", "type", "status", "models"]),
        ("provisioned_throughput", ["region", "provisionedModelArn", "provisionedModelName", "status", "modelUnits", "desiredModelUnits", "modelArn", "foundationModelArn"]),
        ("quotas", ["region", "quota_code", "name", "applied_value", "default_value", "unit", "adjustable", "global", "level", "context", "period", "source", "collected_at", "usage_metric", "comparison", "error_reason"]),
        ("collection_issues", ["region", "operation", "status", "resource", "message"]),
        ("metric_inventory", ["region", "namespace", "metric_names", "dimension_names",
                              "dimension_sets", "not_in_curated_list"]),
    ]
    for key, fields in definitions:
        atomic_text(directory/f"{key}.csv", csv_text(report[key], fields))
    # Execution history: the same atomic-write path as the data exports, so both
    # files land in report.zip and survive --render of a saved report.
    run_log = report.get("run_log") or {}
    atomic_text(directory/"run_log.csv", csv_text(run_log.get("events", []), LOG_FIELDS))
    atomic_text(directory/"run_log.txt", log_text(run_log))
    rows = []
    for metric in report["metrics"]:
        rows.append({
            "id": metric["id"], "region": metric["region"], "namespace": metric["metric"]["Namespace"],
            "metric": metric["metric"]["MetricName"], "dimensions": metric["metric"]["Dimensions"],
            "stat": metric["stat"], "period_seconds": metric["period_seconds"], "status": metric["status"],
            "status_reason": metric.get("status_reason"), "completed_windows": metric.get("completed_windows", []),
            **metric.get("summary", {}),
        })
    atomic_text(directory/"usage_summary.csv", csv_text(rows, [
        "id", "region", "namespace", "metric", "dimensions", "stat", "period_seconds", "status", "status_reason", "completed_windows",
        "total", "max", "p95", "peak_per_minute", "p95_per_minute", "observed_points", "expected_intervals", "missing_intervals",
    ]))
    def points():
        for metric in report["metrics"]:
            for timestamp, value in metric["points"]:
                yield {"metric_id": metric["id"], "region": metric["region"], "namespace": metric["metric"]["Namespace"],
                       "metric": metric["metric"]["MetricName"], "dimensions": metric["metric"]["Dimensions"],
                       "timestamp": timestamp, "value": value, "stat": metric["stat"], "period_seconds": metric["period_seconds"]}
    # Stream long timeseries instead of constructing a second copy in memory.
    timeseries = directory/"usage_timeseries.csv"
    temp = timeseries.with_suffix(".csv.tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        fields = ["metric_id", "region", "namespace", "metric", "dimensions", "timestamp", "value", "stat", "period_seconds"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in points():
            writer.writerow({k: safe_csv_value(v) for k, v in row.items()})
    temp.replace(timeseries)
    if not collect_only:
        render(report, directory/"report.html")
    zip_path = directory.with_suffix(".zip")
    with zipfile.ZipFile(zip_path.with_suffix(".zip.tmp"), "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.name.endswith(".tmp"):
                bundle.write(path, arcname=f"{directory.name}/{path.name}")
    zip_path.with_suffix(".zip.tmp").replace(zip_path)
    return zip_path


def render(report, path):
    # Escaping '<' prevents data from closing the inert JSON script element.
    data = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    document = HTML.replace("__REPORT_JSON__", data, 1)

    policy = (
        "default-src 'none'; "
        "script-src 'unsafe-inline'; script-src-attr 'none'; "
        "style-src 'unsafe-inline'; style-src-attr 'none'; "
        "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
    )
    # Replace only the meta placeholder; a data value may contain the same text.
    atomic_text(path, document.replace("__REPORT_CSP__", policy, 1))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--profile", help="AWS profile; uses boto3's credential chain when omitted")
    region = result.add_mutually_exclusive_group()
    region.add_argument("--regions", nargs="+",
                        help="Limit the collection to these Regions (default: all enabled Regions with a Bedrock endpoint)")
    region.add_argument("--all-enabled-regions", action="store_true",
                        help="Scan every enabled Region (the default); pass this to require ec2:DescribeRegions and fail if it is denied")
    time = result.add_mutually_exclusive_group()
    time.add_argument("--days", type=float, default=14, help="History length in days (default: 14)")
    time.add_argument("--start", help="History start in ISO 8601 format with a timezone")
    result.add_argument("--end", help="History end in ISO 8601 format (default: five minutes ago)")
    result.add_argument("--period", default="auto", choices=["auto", "60", "300", "3600"], help="Metric period in seconds; auto respects retention")
    result.add_argument("--model-ids", nargs="+", help="Additional runtime metric IDs to query")
    result.add_argument("--namespaces", nargs="+", metavar="NAMESPACE",
                        help="Extra CloudWatch namespaces to probe in addition to the Bedrock defaults "
                             f"({', '.join(BEDROCK_NAMESPACES)})")
    result.add_argument("--include-inactive-models", action="store_true", help="Also probe the entire catalog for historical activity (higher collection cost)")
    result.add_argument("--include-api-usage", action="store_true", help="Also collect administrative AWS/Usage metrics referenced by quotas")
    result.add_argument("--output-dir", default="./reports", help="Parent directory for generated reports (default: ./reports)")
    result.add_argument("--region-workers", type=int, default=4, help="Parallel inventory/discovery workers; history is scheduled by diagnostic priority (default: 4)")
    result.add_argument("--skip-usage", action="store_true", help="Collect inventory and quotas without CloudWatch metric queries")
    result.add_argument("--plan", action="store_true", help="Read inventory/metric identities without fetching datapoints")
    result.add_argument("--max-metrics", type=int, default=3000, help="Maximum selected series per attempt; deferred identities are saved for resume (default: 3000)")
    result.add_argument("--max-datapoints", type=int, default=2000000, help="Returned datapoint limit, checked between responses (default: 2000000)")
    result.add_argument("--max-metric-requests", type=int, default=600,
                        help="Maximum GetMetricData calls per run (default: 600). Discovery now fills the "
                             "metric x dimension-set matrix, so a run selects more series than before")
    result.add_argument("--log-level", choices=sorted(LOG_LEVELS, key=LOG_LEVELS.get), default="INFO",
                        help="Console and run_log.txt verbosity; run_log.csv always keeps every event (default: INFO)")
    result.add_argument("--collect-only", action="store_true", help="Save data without rendering HTML")
    saved = result.add_mutually_exclusive_group()
    saved.add_argument("--render", metavar="REPORT_JSON", help="Regenerate exports from saved data, without AWS calls")
    saved.add_argument("--resume", metavar="REPORT_JSON", help="Retry incomplete usage windows in the original account; save a new report")
    result.add_argument("--version", action="version", version=VERSION)
    return result


def resolve_regions(session, args, collector):
    """Choose which Regions to collect.

    Default (no flags) and explicit --all-enabled-regions both discover every
    enabled Region that has a Bedrock endpoint, so a capacity report is
    account-wide by default. --regions overrides with an explicit list. If
    ec2:DescribeRegions is denied, the default falls back to the configured
    Region (with a warning); an explicit --all-enabled-regions fails instead.
    """
    if args.regions:
        return list(dict.fromkeys(args.regions))
    # Only a seed Region is needed to place the DescribeRegions call itself.
    seed = session.region_name or "us-east-1"
    response = collector.call("ec2", seed, "describe_regions", AllRegions=False)
    if response is None:
        if args.all_enabled_regions:
            raise RuntimeError("Could not list enabled Regions (needs ec2:DescribeRegions); pass --regions instead.")
        if not session.region_name:
            raise ValueError("Could not list enabled Regions and no Region is configured. "
                             "Pass --regions, or grant ec2:DescribeRegions for the all-Regions default.")
        collector.log.add("WARN", "start", "describe_regions", seed, "fallback",
                          f"ec2:DescribeRegions unavailable; defaulting to the configured Region {session.region_name} only. "
                          f"Pass --regions to choose Regions, or grant ec2:DescribeRegions to scan all.")
        return [session.region_name]
    supported = set(session.get_available_regions("bedrock"))
    regions = [r["RegionName"] for r in response["Regions"] if r["RegionName"] in supported]
    if not regions:
        raise RuntimeError("No enabled Regions match the Bedrock endpoints known to this SDK.")
    return regions


def dump_run_log(snapshot, output_dir):
    """Write only the execution history, for a run that produced no report."""
    directory = Path(output_dir).expanduser().resolve()/(
        f"bedrock-run-log_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}")
    directory.mkdir(parents=True, exist_ok=True)
    atomic_text(directory/"run_log.csv", csv_text(snapshot.get("events", []), LOG_FIELDS))
    atomic_text(directory/"run_log.txt", log_text(snapshot))
    return directory/"run_log.csv"


def main():
    args = parser().parse_args()
    os.umask(0o077)
    if args.render:
        path = Path(args.render).expanduser().resolve()
        report = json.loads(path.read_text())
        if report.get("schema_version") != "1.0":
            raise ValueError("Unsupported report schema.")
        analyze(report)
        archive = export(report, path.parent)
        print(f"HTML: {path.parent/'report.html'}")
        print(f"QUOTAS CSV: {path.parent/'quotas.csv'}")
        print(f"RUN LOG: {path.parent/'run_log.csv'} · {path.parent/'run_log.txt'}")
        print(f"JSON: {path}")
        print(f"ZIP: {archive}")
        return
    if args.skip_usage and args.plan:
        raise ValueError("--plan and --skip-usage cannot be used together.")
    if args.days <= 0 or min(args.max_metrics, args.max_datapoints, args.max_metric_requests) <= 0:
        raise ValueError("Days and collection limits must be positive.")
    saved_report = None
    if args.resume:
        if args.skip_usage or args.plan or args.regions or args.all_enabled_regions or args.start or args.end or args.period != "auto" or args.model_ids or args.days != 14:
            raise ValueError("--resume preserves the original Regions, time window, resolution and metric identities. Do not combine it with collection scope options.")
        saved_report = json.loads(Path(args.resume).expanduser().read_text())
        if saved_report.get("schema_version") != "1.0":
            raise ValueError("Unsupported report schema.")
        resume_args = copy.copy(args)
        resume_args.start, resume_args.end = saved_report["start"], saved_report["end"]
        resume_args.period = str(saved_report["period_seconds"])
        # Reject a resume when retention no longer supports the saved resolution.
        start, end, period = window(resume_args)
    else:
        start, end, period = window(args)
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install boto3 in your Python environment before running the collector.") from exc
    session = boto3.Session(profile_name=args.profile)
    collector = Collector(session, args, start, end, period)
    regions = saved_report["regions"] if saved_report else resolve_regions(session, args, collector)
    # Order Regions by display priority so the HTML Overview and its Region
    # selector lead with us-east-1, then North America, Europe, South America.
    regions = order_regions(regions)
    try:
        report = collector.resume(saved_report) if saved_report else collector.run(regions)
    except BaseException as exc:
        # An aborted or failed run still has a history worth keeping; write the
        # log on its own before the error propagates.
        collector.log.add("ERROR", collector.phase, "collection", "", type(exc).__name__,
                          f"Run did not finish: {type(exc).__name__}: {exc}")
        path = dump_run_log(collector.log.snapshot(), args.output_dir)
        print(f"RUN LOG: {path}", file=sys.stderr)
        raise
    name = f"bedrock-report_{report['account_id']}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    directory = Path(args.output_dir).expanduser().resolve()/name
    directory.mkdir(parents=True, exist_ok=False)
    archive = export(report, directory, args.collect_only)
    if not args.collect_only:
        print(f"HTML: {directory/'report.html'}")
    print(f"QUOTAS CSV: {directory/'quotas.csv'}")
    print(f"RUN LOG: {directory/'run_log.csv'} · {directory/'run_log.txt'}")
    print(f"JSON: {directory/'report.json'}")
    print(f"ZIP: {archive}")
    levels = (report.get("run_log") or {}).get("levels", {})
    print(f"Collection complete: {len(report['collection_issues'])} issues recorded; "
          f"{report['returned_datapoints']:,} datapoints; "
          f"{levels.get('ERROR', 0)} errors and {levels.get('WARN', 0)} warnings logged.")


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="__REPORT_CSP__">
<title>Bedrock · Quotas & usage</title>
<style>
:root{--ink:#132a32;--muted:#607780;--teal:#007e80;--border:#dde7e8;--paper:#f4f7f7;--orange:#c06a18;--blue:#597bea;--brick:#a3333d;--faint:#93a7ad}
*{box-sizing:border-box}body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--paper);color:var(--ink)}
button,input,select{font:inherit}button,a,select{touch-action:manipulation}button{cursor:pointer}a{color:var(--teal)}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #4d99ea;outline-offset:3px}
aside{position:fixed;inset:0 auto 0 0;width:228px;background:#112e37;color:#dbe8eb;padding:32px 22px;display:flex;flex-direction:column}
.brand{font-size:23px;font-weight:750;letter-spacing:-.7px;color:#fff}.brandmark{display:inline-grid;place-items:center;background:#55d1ba;color:#173a3c;border-radius:10px;width:36px;height:36px;margin-right:9px}
.brand small{display:block;font-size:11px;color:#9ab4bd;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;margin:10px 0 36px}
nav{display:grid;gap:7px}nav button{border:0;background:transparent;color:#b9cdd3;text-align:left;padding:12px;border-radius:8px;font-weight:550;display:flex;justify-content:space-between}
nav button:hover{background:#203e47}nav button.active{background:#24515a;color:#fff}nav span{font-size:11px;color:#c2d5d9}
.aside-foot{margin-top:auto;border-top:1px solid #35515a;padding-top:20px;color:#abc0c7;font-size:12px}.aside-foot strong{display:block;color:#fff;margin:5px 0}
main{margin-left:228px;padding:26px 40px 60px;max-width:1800px}.topline{display:flex;justify-content:space-between;gap:16px;align-items:center;border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:26px}
.eyebrow{text-transform:uppercase;letter-spacing:1.8px;font-size:10px;font-weight:750;color:var(--muted)}.tag{display:inline-flex;align-items:center;gap:7px;border:1px solid #c9e5dc;background:#edf9f4;color:#207051;border-radius:20px;padding:5px 11px;font-size:11px;font-weight:650}.tag:before{content:"";width:6px;height:6px;background:#36a780;border-radius:100%}
.downloads{display:flex;gap:10px;flex-wrap:wrap}.downloads a{display:inline-block;text-decoration:none;background:#fff;border:1px solid var(--border);border-radius:7px;padding:8px 13px;font-size:12px;font-weight:650}
.text-xs{font-size:11px}.nowrap{white-space:nowrap}.text-sm{font-size:12px}
.dot[data-color="#007e80"]{background:#007e80}.dot[data-color="#597bea"]{background:#597bea}.dot[data-color="#c98536"]{background:#c98536}
h1{font-size:32px;line-height:1.2;letter-spacing:-1.1px;margin:9px 0 10px}h2{font-size:18px;letter-spacing:-.35px;margin:0 0 5px}h3{font-size:14px;margin:0 0 10px}p{margin:0 0 12px}.muted{color:var(--muted)}.lead{font-size:14px;color:var(--muted);max-width:900px}
.scope{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:23px 0}.scope label{color:var(--muted);font-size:12px}.scope select{min-width:145px}
select,input{border:1px solid #ccdadd;background:#fff;border-radius:7px;padding:9px 12px;color:var(--ink);min-width:0}input{width:300px}.pill{background:#e9eff1;border-radius:6px;font-size:12px;padding:7px 10px;color:#46646e}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:22px 0}.card{border:1px solid var(--border);border-radius:12px;background:#fff;padding:20px 22px;min-width:0}.card label{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted)}.card strong{display:block;font-size:32px;font-weight:650;letter-spacing:-.8px;margin:7px 0}.card small{display:block;color:var(--muted);font-size:11px}
.panel{background:white;border:1px solid var(--border);border-radius:12px;padding:24px;margin:20px 0}.panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:20px;flex-wrap:wrap}.panel-head p{color:var(--muted);font-size:12px;margin:0}.resource-select{max-width:470px;width:100%;font-size:12px}
.note{border-left:3px solid #d0a050;background:#fcf7ec;color:#775820;border-radius:0 7px 7px 0;padding:13px 17px;font-size:12px;margin:18px 0}.info{border-left-color:#64a9af;background:#edf5f5;color:#43646b}
.tabs{display:flex;flex-wrap:wrap;gap:5px;background:var(--paper);border:1px solid var(--border);padding:4px;border-radius:8px}.tabs button{border:0;background:transparent;border-radius:5px;padding:6px 11px;color:var(--muted);font-size:12px}.tabs .active{background:white;box-shadow:0 1px 3px #172a3212;color:var(--ink)}
.chart{height:280px;position:relative}.chart canvas{width:100%;height:100%}.legend{display:flex;flex-wrap:wrap;gap:18px;font-size:11px;color:var(--muted);margin:12px 0}.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}.tooltip{position:absolute;pointer-events:none;background:#112e37;color:#fff;border-radius:7px;padding:10px 13px;font-size:11px;box-shadow:0 4px 15px #0002;z-index:2;max-width:290px;white-space:normal}
.mini-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;padding-top:20px;border-top:1px solid var(--border);margin-top:14px}.mini-grid label{font-size:11px;color:var(--muted)}.mini-grid strong{display:block;font-size:22px;margin:3px 0}.mini-grid small{display:block;color:var(--muted);font-size:10px}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px}th{text-align:left;color:var(--muted);background:#f5f8f8;font-size:10px;letter-spacing:.5px;text-transform:uppercase;padding:12px;font-weight:650;white-space:nowrap}td{padding:13px 12px;border-bottom:1px solid #edf1f2;vertical-align:top}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td strong{font-size:12px}
/* Absence recedes; a recorded rejection or failure is the only coloured number in
   the row, and the row carries a rule on its left edge so a grid of healthy
   identifiers reads as a pattern before any figure is parsed. */
.quiet{color:var(--faint)}
.chips{display:flex;flex-direction:column;gap:5px}
.chip{display:flex;align-items:baseline;gap:6px;white-space:nowrap;font-variant-numeric:tabular-nums}
.chip b{font-weight:680;letter-spacing:-.1px}
.chip em{font-size:10px;color:var(--muted);font-style:normal}
.chip.warn b{color:var(--orange)}.chip.bad b{color:var(--brick)}
tr.flagged td:first-child{box-shadow:inset 3px 0 0 var(--orange)}
tr.flagged.bad td:first-child{box-shadow:inset 3px 0 0 var(--brick)}
/* One column per published counter, so the table scrolls sideways. The
   identifier stays pinned, otherwise a figure eight columns to the right
   belongs to a model whose name has scrolled out of view. */
#usage-table th:first-child,#usage-table td:first-child{position:sticky;left:0;min-width:210px;background:#fff;border-right:1px solid var(--border)}
#usage-table th:first-child{background:#f5f8f8;z-index:2}
#usage-table td:first-child{z-index:1}
#usage-table tbody tr:hover td:first-child{background:#fbfdfd}
#usage-table th.extra{white-space:normal;max-width:150px}td small{display:block;font-size:10px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}tbody tr:hover{background:#fbfdfd}code{font-size:11px;color:#54727b;overflow-wrap:anywhere}
.badge{display:inline-block;padding:3px 7px;border-radius:5px;font-size:10px;background:#eef3f4;color:#57727a;white-space:nowrap}.badge.good{background:#e9f5ed;color:#217348}.badge.warn{background:#fff3df;color:#935d15}.badge.bad{background:#fbece7;color:#a34f36}
.pagebar{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-top:16px;font-size:12px;color:var(--muted)}.pagebar button{background:#fff;border:1px solid var(--border);padding:6px 12px;border-radius:6px;margin-left:5px}.pagebar button:disabled{opacity:.4;cursor:default}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.empty{text-align:center;color:var(--muted);padding:45px 24px}.empty strong{display:block;color:var(--ink);font-size:17px;margin:8px}.quality-list{margin:0;padding-left:18px;color:var(--muted);font-size:12px}.quality-list li{margin:9px 0}.section{display:none}.section.active{display:block}.footer{margin-top:30px;color:#7a9198;font-size:11px;border-top:1px solid var(--border);padding-top:18px}
details summary{cursor:pointer;color:var(--teal);font-size:11px;margin-top:5px}details p{max-width:580px;font-size:11px;color:var(--muted);margin-top:7px}.nowrap{white-space:nowrap}
@media(max-width:1100px){aside{width:190px;padding:25px 15px}main{margin-left:190px;padding:24px}.cards{gap:10px}.card{padding:16px}.card strong{font-size:27px}.mini-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:760px){aside{position:static;width:auto;padding:16px 20px}.brand small,.aside-foot{display:none}.brand{font-size:20px}nav{display:flex;overflow:auto;margin-top:15px;gap:5px}nav button{white-space:nowrap;padding:8px;font-size:11px}nav span{margin-left:6px}main{margin:0;padding:20px 16px}h1{font-size:25px}.topline{flex-wrap:wrap}.cards{grid-template-columns:repeat(2,1fr)}.panel{padding:17px}.scope{gap:8px}.chart{height:250px}.downloads a{padding:7px 9px}.filters input{width:100%}.resource-select{max-width:100%}}
@media print{aside,.downloads,.scope,.filters,.tabs,.pagebar{display:none}main{margin:0;padding:0}.section{display:block;break-inside:avoid}.panel{break-inside:avoid}.topline{display:none}}
</style>
</head>
<body>
<aside>
 <div class="brand"><span class="brandmark">▥</span>Bedrock<small>Quotas & usage</small></div>
 <nav aria-label="Report sections">
  <button class="active" data-tab="overview">Overview <span>01</span></button>
  <button data-tab="quotas">Current quotas <span id="quota-nav"></span></button>
  <button data-tab="models">Models & access <span id="model-nav"></span></button>
  <button data-tab="profiles">Inference profiles <span id="profile-nav"></span></button>
  <button data-tab="quality">Collection quality <span id="issue-nav"></span></button>
  <button data-tab="runlog">Run log <span id="log-nav"></span></button>
 </nav>
 <div class="aside-foot">AWS ACCOUNT<strong id="account-side"></strong><span id="profile-side"></span><br><br>Local report · works offline<br>Credentials are not included</div>
</aside>
<main>
 <div class="topline"><div class="eyebrow">Amazon Bedrock / Capacity report</div><div class="downloads"><a href="report.html" download>↓ Download HTML</a><a href="quotas.csv" download>↓ Quotas CSV</a><a href="report.json" download>↓ Full JSON</a></div></div>
 <div class="tag">Read-only collection</div>
 <h1>Amazon Bedrock quotas and usage</h1>
 <p class="lead">Current account limits and observed usage. Explore models, review capacity, and identify data that needs attention.</p>
 <div class="scope"><label for="region">Region</label><select id="region"></select><span class="pill" id="window"></span><span class="pill" id="resolution"></span></div>
 <section class="section active" id="overview">
  <div class="cards" id="cards"></div>
  <div id="run-notice"></div>
  <div class="panel">
   <div class="panel-head"><div><h2>Usage over time</h2><p>One identifier at a time, preserving the original metric dimensions.</p></div><select id="resource" class="resource-select" aria-label="Model or profile to display"></select></div>
   <div class="panel-head"><div class="tabs" id="chart-tabs"><button data-mode="tokens" class="active">Tokens</button><button data-mode="requests">Requests</button><button data-mode="throttles">Throttles</button><button data-mode="latency">Latency</button><button data-mode="counters">All counters</button></div><span class="muted text-xs" id="chart-unit"></span></div>
   <div class="chart"><canvas id="chart" role="img" aria-label="Observed usage during the reporting period"></canvas><div class="tooltip" id="tooltip" hidden></div></div>
   <div class="legend" id="legend"></div>
   <p class="muted text-xs" id="chart-method"></p>
   <div class="mini-grid" id="resource-stats"></div>
   <div id="capacity-comparison"></div>
  </div>
  <div class="note">Historical usage is compared with today's quotas. Token estimates do not reproduce the <code>max_tokens</code> reservations used for capacity control. A low estimate does not rule out throttling.</div>
  <div class="panel"><div class="panel-head"><div><h2>Series with observed data</h2><p>Volumes by identifier, without adding overlapping aggregates of the same traffic. A rule on the left edge marks a row that recorded a rejection or a failure. Every counter the Region published has its own column; scroll sideways to reach them.</p></div><a href="usage_summary.csv" download class="text-sm">↓ Usage CSV</a></div><div class="table-wrap" id="usage-table"></div><p class="muted text-xs" id="usage-legend"></p><div class="pagebar" id="usage-page"></div></div>
 </section>
 <section class="section" id="quotas">
  <div class="panel"><div class="panel-head"><div><h2>Current quotas</h2><p>Applied values and AWS defaults are kept separate.</p></div></div>
   <div class="filters"><input id="quota-search" placeholder="Search by model, name, or quota code…" aria-label="Search quotas"><select id="quota-kind" aria-label="Quota type"><option value="" selected>All types</option><option value="tokens">Tokens per minute</option><option value="daily">Daily token quotas</option><option value="requests">Requests per minute</option><option value="batch">Batch inference</option><option value="provisioned">Provisioned Throughput</option></select><select id="quota-adjustable" aria-label="Adjustable quotas"><option value="">All quotas</option><option value="yes">Adjustable</option><option value="no">Not adjustable</option></select></div>
   <div class="note info">A quota defines a limit, not guaranteed available capacity. A percentage appears only when the quota, metric, and units have a confirmed mapping.</div>
   <div class="table-wrap" id="quota-table"></div><div class="pagebar" id="quota-page"></div>
  </div>
 </section>
 <section class="section" id="models">
  <div class="panel"><div class="panel-head"><div><h2>Catalog and availability</h2><p>Availability reported by the API, without running inference.</p></div><input id="model-search" placeholder="Search by provider or model…" aria-label="Search models"></div>
   <div class="note info">A catalog entry does not prove your application can invoke the model. IAM, SCPs, endpoint policies, and provider prerequisites can still restrict access.</div>
   <div class="table-wrap" id="model-table"></div><div class="pagebar" id="model-page"></div>
  </div>
 </section>
 <section class="section" id="profiles">
  <div class="panel"><div class="panel-head"><div><h2>Inference profiles</h2><p>Source Region, type, and reported destinations. Profiles do not represent independent quotas.</p></div><input id="profile-search" placeholder="Search profiles…" aria-label="Search profiles"></div><div class="table-wrap" id="profile-table"></div><div class="pagebar" id="profile-page"></div></div>
  <div class="panel"><h2>Provisioned Throughput</h2><p class="muted text-sm">Existing resources and allocated units, separate from allocation quotas.</p><div class="table-wrap" id="provisioned-table"></div></div>
 </section>
 <section class="section" id="quality">
  <div class="panel"><h2>Collection coverage</h2><div id="quality-summary"></div><div class="table-wrap" id="quality-table"></div><div class="pagebar" id="quality-page"></div></div>
  <div class="panel"><h2>Interpretation limits</h2><ul class="quality-list" id="limitations"></ul></div>
  <div class="panel"><h2>Operations performed</h2><p class="muted text-sm">Logical collector calls; internal SDK retries may generate additional requests.</p><div class="table-wrap" id="calls-table"></div></div>
 </section>
 <section class="section" id="runlog">
  <div class="panel"><h2>Execution history</h2><p class="muted text-sm">Every AWS call and collection decision, in order, across all attempts of this report. Also exported as <code>run_log.csv</code> and <code>run_log.txt</code>.</p>
   <div id="log-summary"></div>
   <div class="filters"><input id="log-search" type="search" placeholder="Filter by operation, Region, status or detail" aria-label="Filter the run log"><select id="log-level" aria-label="Minimum level"><option value="">All levels</option><option>DEBUG</option><option selected>INFO</option><option>WARN</option><option>ERROR</option></select><select id="log-phase" aria-label="Phase"><option value="">All phases</option></select></div>
   <div class="table-wrap" id="log-table"></div><div class="pagebar" id="log-page"></div>
  </div>
 </section>
 <div class="footer" id="footer"></div>
</main>
<script id="report-data" type="application/json">__REPORT_JSON__</script>
<script>
"use strict";
const D=JSON.parse(document.getElementById("report-data").textContent);
const $=id=>document.getElementById(id);
const h=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=v=>v==null?"N/A":new Intl.NumberFormat("en-US",{maximumFractionDigits:2}).format(v);
const compact=v=>new Intl.NumberFormat("en-US",{notation:"compact",maximumFractionDigits:1}).format(v);
const when=v=>new Date(v).toLocaleString("en-US",{timeZone:"UTC",dateStyle:"short",timeStyle:"short"})+" UTC";
const day=v=>new Date(v).toLocaleDateString("en-US",{timeZone:"UTC",day:"2-digit",month:"short"});
const badge=(text,kind="")=>`<span class="badge ${h(kind)}">${h(text)}</span>`;
// A head is either a label or {t:label, c:class, title:explanation}.
const table=(heads,rows)=>`<table><thead><tr>${heads.map(x=>{const o=typeof x==="string"?{t:x}:x;
 return `<th${o.c?` class="${h(o.c)}"`:""}${o.title?` title="${h(o.title)}"`:""}>${h(o.t)}</th>`}).join("")}</tr></thead><tbody>${rows.join("")||`<tr><td colspan="${heads.length}" class="empty">No results for these filters.</td></tr>`}</tbody></table>`;
const state={tab:"overview",region:D.regions[0],mode:"tokens",resource:"",pages:{}};
// Mirrors GAUGE_SUFFIXES in the collector: names whose statistics are not sums.
const GAUGE_SUFFIXES=["Latency","TimeToFirstToken","TimeToFirstByte"];
const PALETTE=["#007e80","#597bea","#c06a18","#8a5cb8","#2f8f5b","#b03a4a","#4a7c93","#9a7d1e"];
// Short endpoint label per namespace, so a namespace added later is not mislabelled.
const endpointLabel=ns=>ns==="AWS/Bedrock"?"runtime":ns==="AWS/BedrockMantle"?"mantle":ns.replace(/^AWS\/Bedrock\/?/,"").toLowerCase()||"bedrock";
const scoped=rows=>rows.filter(r=>r.region===state.region);
const sumMetric=(g,name)=>g?.metrics.find(m=>m.metric.MetricName===name&&m.stat==="Sum");
const total=(g,name)=>sumMetric(g,name)?.summary.total??null;
// Gauge series are not summable: report the observed maximum of the statistic.
const statMetric=(g,name,stat)=>g?.metrics.find(m=>m.metric.MetricName===name&&m.stat===stat);
// Prefer a gauge series that returned datapoints: a group can hold several gauge
// names (InvocationLatency, TimeToFirstToken) where only some were published.
const gaugeMetrics=(g,stat)=>(g?.metrics||[]).filter(m=>m.stat===stat&&GAUGE_SUFFIXES.some(s=>m.metric.MetricName.endsWith(s)));
const gaugeMetric=(g,stat)=>gaugeMetrics(g,stat).find(m=>m.points.length)||gaugeMetrics(g,stat)[0];
// Three states, never collapsed into one another: a figure, a series that
// returned nothing, and a counter the namespace never published. None is a zero.
// Both are one glyph: across sixteen columns a repeated phrase outweighs the
// figures it sits beside. The table carries a legend, and each cell its title.
const NO_DATA='<span class="quiet" title="Series queried; no datapoints returned. This is not a zero.">&#8211;</span>';
const NOT_PUBLISHED='<span class="quiet" title="This namespace did not publish this counter. See metric_inventory.csv.">&#8709;</span>';
const ABSENCE_LEGEND='<span class="quiet">&#8211;</span> queried, no datapoints returned &nbsp;·&nbsp; '
 +'<span class="quiet">&#8709;</span> counter not published by this namespace &nbsp;·&nbsp; neither is a zero';
// Durations read as time, not as six-digit millisecond counts.
const dur=ms=>ms==null?null:ms<1000?`${fmt(Math.round(ms))} ms`
 :ms<120000?`${fmt(Number((ms/1000).toFixed(1)))} s`:`${fmt(Number((ms/60000).toFixed(1)))} min`;
// Any discovered error or throttle counter for this group, by suffix.
const errorMetrics=(g,suffix)=>(g?.metrics||[]).filter(m=>m.stat==="Sum"&&m.metric.MetricName.endsWith(suffix));
const ERROR_SUFFIXES=["Throttles","ClientErrors","ServerErrors"];
// Rejections and failures share one column: in a healthy account almost every
// row is empty, so three near-empty columns cost width and hide the one that is not.
const TROUBLE=[["Throttles","throttled","warn"],["ClientErrors","client errors","bad"],["ServerErrors","server errors","bad"]];
const troubleFound=g=>TROUBLE.flatMap(([suffix,label,kind])=>errorMetrics(g,suffix)
 .filter(m=>(m.summary.total??0)>0)
 .map(m=>({label,kind,value:m.summary.total,name:m.metric.MetricName})));
const troubleCell=g=>{
 const hits=troubleFound(g);
 if(hits.length)return `<div class="chips">${hits.map(x=>
  `<span class="chip ${x.kind}" title="${h(x.name)}"><b>${fmt(x.value)}</b><em>${h(x.label)}</em></span>`).join("")}</div>`;
 return TROUBLE.some(([suffix])=>errorMetrics(g,suffix).length)?NO_DATA:NOT_PUBLISHED;
};
const troubleKind=g=>{const hits=troubleFound(g);
 return hits.some(x=>x.kind==="bad")?"flagged bad":hits.length?"flagged":""};
// The dimension note earns its line only when it says something the bold
// identifier above it does not. A model id needs no label; a value like
// "default" does, or the row says nothing about which dimension it describes.
const idNote=g=>!g.dims.length?"Aggregate series, without dimensions"
 :g.extra||(["ModelId","Model"].includes(g.idName)?"":g.idName);
const latencyCell=g=>{const m=gaugeMetric(g,"p99");
 if(!m)return NOT_PUBLISHED;
 if(m.summary.max==null)return NO_DATA;
 return `<span title="${h(m.metric.MetricName)} p99 · highest value of any single period">${h(dur(m.summary.max))}</span>`};
// Names already carried by the Requests, Input tokens and Output tokens columns
// of either endpoint. Excluding both endpoints' names keeps a runtime row from
// growing an empty TotalInputTokens column, and a Mantle row an InputTokenCount one.
const CORE_NAMES=["Invocations","Inferences","InputTokenCount","OutputTokenCount","TotalInputTokens","TotalOutputTokens"];
const isExtra=name=>!CORE_NAMES.includes(name)&&!ERROR_SUFFIXES.some(s=>name.endsWith(s));
const otherCounters=g=>(g?.metrics||[]).filter(m=>m.stat==="Sum"&&m.points.length&&isExtra(m.metric.MetricName));
// Every counter the Region published gets its own column, so a figure can be
// compared down the column instead of being stacked inside one cell. Derived
// from all groups in the Region, not the current page, so columns stay put.
const extraNames=()=>[...new Set(groups.flatMap(g=>otherCounters(g).map(m=>m.metric.MetricName)))].sort();
// Header label from a metric name; the exact name stays in the header title.
const metricLabel=name=>name.replace(/TokenCount$/,"Tokens")
 .replace(/([A-Z]+)([A-Z][a-z])/g,"$1 $2").replace(/([a-z0-9])([A-Z])/g,"$1 $2")
 .replace(/Cloud Watch/g,"CloudWatch");
const totalCell=(g,name)=>{const m=sumMetric(g,name);
 if(!m)return NOT_PUBLISHED;
 const flag=["ok","no_data"].includes(m.status)?"":"<small>Incomplete query</small>";
 return (m.summary.total==null?NO_DATA:fmt(m.summary.total))+flag};
let groups=[],chartData=null;

function pagination(name,rows,draw,container,pager,size=15){
 const count=Math.max(1,Math.ceil(rows.length/size)),page=Math.min(state.pages[name]||0,count-1);state.pages[name]=page;
 $(container).innerHTML=draw(rows.slice(page*size,(page+1)*size));
 $(pager).innerHTML=`<span>${fmt(rows.length)} results · page ${page+1} of ${count}</span><span><button data-prev ${page===0?"disabled":""} aria-label="Previous page">←</button><button data-next ${page+1===count?"disabled":""} aria-label="Next page">→</button></span>`;
 $(pager).querySelector("[data-prev]").onclick=()=>{state.pages[name]=page-1;pagination(name,rows,draw,container,pager,size)};
 $(pager).querySelector("[data-next]").onclick=()=>{state.pages[name]=page+1;pagination(name,rows,draw,container,pager,size)};
}
function buildGroups(){
 const map=new Map();
 // Group every probed namespace, not just the two original ones, and keep gauge
 // statistics in the group so latency is reachable alongside the counters.
 for(const m of scoped(D.metrics).filter(m=>m.metric.Namespace!=="AWS/Usage")){
  const dims=m.metric.Dimensions,key=m.region+"|"+m.metric.Namespace+"|"+JSON.stringify(dims);
  if(!map.has(key)){
   // The identifier is the model dimension when present, otherwise the first
   // dimension of the set. Whichever it is, do not repeat it in `extra`.
   const idDim=dims.find(d=>d.Name==="ModelId"||d.Name==="Model")||dims[0];
   map.set(key,{key,id:idDim?.Value||"Account aggregate",idName:idDim?.Name||"",
    namespace:m.metric.Namespace,dims,metrics:[],
    endpoint:endpointLabel(m.metric.Namespace),
    extra:dims.filter(d=>d!==idDim).map(d=>d.Name+"="+d.Value).join(", ")});
  }map.get(key).metrics.push(m);
 }
 groups=[...map.values()].filter(g=>g.metrics.some(m=>m.points.length)).sort((a,b)=>((total(b,"Invocations")??total(b,"Inferences")??0)-(total(a,"Invocations")??total(a,"Inferences")??0))||a.id.localeCompare(b.id));
 if(!groups.some(g=>g.key===state.resource))state.resource=groups[0]?.key||"";
 $("resource").innerHTML=groups.length?groups.map(g=>`<option value="${h(g.key)}">${h(g.id)}${g.extra?" · "+h(g.extra):""} · ${h(g.endpoint)}</option>`).join(""):'<option>No series returned datapoints</option>';
 $("resource").value=state.resource;
}
function renderOverview(){
 const quotas=scoped(D.quotas),models=scoped(D.models),metrics=scoped(D.metrics),observed=metrics.filter(m=>m.points.length);
 const available=models.filter(m=>m.availability?.authorizationStatus==="AUTHORIZED"&&m.availability?.regionAvailability==="AVAILABLE"&&m.availability?.entitlementAvailability==="AVAILABLE"&&m.availability?.agreementAvailability?.status==="AVAILABLE").length;
 const cards=[
 ["Applied quotas",quotas.filter(q=>q.applied_value!==null).length,`${quotas.filter(q=>q.applied_value===null).length} without a confirmed applied value`],
 ["Catalog models",models.length,`${available} with availability confirmed by all reported states`],
 ["Series with data",observed.length,`${fmt(metrics.length)} series selected for querying`],
 ["Observed identifiers",new Set(groups.filter(g=>g.dims.some(d=>d.Name==="ModelId"||d.Name==="Model")).map(g=>g.id)).size,"Models or profiles; future access is not implied"],
 ];
 $("cards").innerHTML=cards.map(([label,value,sub])=>`<div class="card"><label>${h(label)}</label><strong>${fmt(value)}</strong><small>${h(sub)}</small></div>`).join("");
 const issues=D.collection_issues.filter(i=>i.region===state.region||i.region==="all"),coverage=D.diagnostic_coverage?.find(c=>c.region===state.region);
 const unavailable=metrics.filter(m=>["partial","error","not_queried"].includes(m.status)).length+(coverage?.deferred_series||0);
 let notices=D.usage_skipped?'<div class="note">This run did not retrieve usage datapoints. Run without --skip-usage or --plan to include history.</div>':issues.length||unavailable?`<div class="note"><strong>Quota diagnosis is incomplete.</strong> ${fmt(issues.length)} collection issues and ${fmt(unavailable)} incomplete or deferred series. Local collection limits are not inference failures. Resume the saved report to complete history.</div>`:"";
 if(coverage&&!D.usage_skipped)notices+=`<div class="note info">Runtime throttling queries completed: ${fmt(coverage.runtime_throttle_queries_complete)} / ${fmt(coverage.runtime_throttle_series)}. Missing datapoints do not establish zero throttling.${coverage.mantle_requires_application_errors?" Mantle HTTP 429 responses require application logs; InferenceClientErrors excludes requests rejected before processing.":""}</div>`;
 const daily=quotas.find(q=>q.quota_code==="L-E3F10727"&&q.name==="Cross-Model Max Tokens Per Day");
 if(daily)notices+=`<div class="note info"><strong>Daily cross-model quota: ${fmt(daily.applied_value)}.</strong> AWS default: ${fmt(daily.default_value)}. Utilization is unavailable: this quota uses pricing-based accounting, not a raw sum of token metrics.</div>`;
 // Throttling was recorded: lead with the rate that was in force when it happened.
 for(const e of (D.throttle_evidence||[]).filter(e=>e.region===state.region)){
  const rates=e.accepted_per_minute_while_throttled||[];
  const steady=rates.length===1?`held steady at ${fmt(rates[0])}`:`ranged ${fmt(Math.min(...rates))} to ${fmt(Math.max(...rates))}`;
  const list=(e.matching_request_quotas||[]).map(q=>`<code>${h(q.quota_code)}</code> ${h(q.name)}`).join("<br>");
  notices+=`<div class="note"><strong>${fmt(e.throttles_total)} request${e.throttles_total===1?"":"s"} throttled on ${h(e.model_id)}.</strong> `
   +`Across the ${fmt(e.throttled_intervals)} interval${e.throttled_intervals===1?"":"s"} that recorded throttling, accepted requests ${h(steady)} per minute. `
   +`That rate is the ceiling that applied, measured rather than mapped.`
   +(list?`<br><span>Requests-per-minute quotas in this Region whose applied value equals it, and whose scope fits this identity — candidates to confirm, not a mapping:</span><br>${list}`
         :`<br><span>No requests-per-minute quota in this Region has an applied value equal to that rate, so the limit reached was a different one.</span>`)
   +`</div>`;
 }
 $("run-notice").innerHTML=notices;
 const extras=extraNames();
 const renderRows=items=>table([
  "Identifier","Endpoint",
  {t:"Requests",c:"num",title:"Accepted requests. Rejected ones are counted under Rejected or failed."},
  {t:"Input tokens",c:"num"},{t:"Output tokens",c:"num"},
  {t:"Latency p99",c:"num",title:"Highest p99 of any single period. Per-period statistics cannot be re-aggregated."},
  "Rejected or failed",
  ...extras.map(name=>({t:metricLabel(name),c:"num extra",title:name})),
 ],items.map(g=>{
  const mantle=g.namespace==="AWS/BedrockMantle";
  // Error and throttle counters are matched by suffix, so a counter this
  // namespace publishes appears even if it is not in the curated list.
  return `<tr class="${h(troubleKind(g))}">`
   +`<td><strong>${h(g.id)}</strong>${idNote(g)?`<small>${h(idNote(g))}</small>`:""}</td>`
   +`<td>${badge(g.endpoint)}</td>`
   +`<td class="num">${totalCell(g,mantle?"Inferences":"Invocations")}</td>`
   +`<td class="num">${totalCell(g,mantle?"TotalInputTokens":"InputTokenCount")}</td>`
   +`<td class="num">${totalCell(g,mantle?"TotalOutputTokens":"OutputTokenCount")}</td>`
   +`<td class="num">${latencyCell(g)}</td>`
   +`<td>${troubleCell(g)}</td>`
   +extras.map(name=>`<td class="num">${totalCell(g,name)}</td>`).join("")
   +`</tr>`;
 }));
 $("usage-legend").innerHTML=ABSENCE_LEGEND;
 pagination("usage",groups,renderRows,"usage-table","usage-page");
 renderChart();
}
function renderChart(){
 const g=groups.find(g=>g.key===state.resource),mantle=g?.namespace==="AWS/BedrockMantle";
 const definitions=state.mode==="tokens"?[
  [mantle?"TotalInputTokens":"InputTokenCount","Input","#007e80"],
  [mantle?"TotalOutputTokens":"OutputTokenCount","Output","#597bea"],
  ...(mantle?[]:[["EstimatedTPMQuotaUsage","Estimated TPM","#c98536"]]),
 ]:state.mode==="requests"?[[mantle?"Inferences":"Invocations",mantle?"Completed inferences":"Accepted requests","#007e80"]]
 // One gauge name for all three statistics, so the lines describe the same metric.
 :state.mode==="latency"?(name=>[[name,"p99","#c06a18","p99"],[name,"Average","#597bea","Average"],[name,"Maximum","#b03a4a","Maximum"]])(gaugeMetric(g,"p99")?.metric.MetricName)
 // Every Sum series in the group, so a namespace with no dedicated view is still plottable.
 :state.mode==="counters"?(g?.metrics||[]).filter(m=>m.stat==="Sum"&&m.points.length).map((m,i)=>[m.metric.MetricName,m.metric.MetricName,PALETTE[i%PALETTE.length]])
 :[["InvocationThrottles","Throttles","#c98536"]];
 // A gauge is plotted as-is; only Sum counters are converted to a per-minute rate.
 const gauge=state.mode==="latency";
 const pick=(name,stat)=>name?(stat?statMetric(g,name,stat):sumMetric(g,name)):null;
 const series=definitions.map(([name,label,color,stat])=>({name,label,color,stat,metric:pick(name,stat)})).filter(s=>s.metric?.points.length);
 $("chart-unit").textContent=gauge?"milliseconds (per-period statistic)":state.mode==="tokens"?"tokens / min":state.mode==="requests"?"requests / min":state.mode==="counters"?"metric units / min · scales differ per series":"throttles / min";
 $("legend").innerHTML=definitions.map(([name,label,color,stat])=>`<span><i class="dot" data-color="${h(color)}"></i>${h(label)}${!pick(name,stat)?.points.length?" · no data":""}</span>`).join("");
 const inv=total(g,mantle?"Inferences":"Invocations"),input=total(g,mantle?"TotalInputTokens":"InputTokenCount"),output=total(g,mantle?"TotalOutputTokens":"OutputTokenCount");
 const stats=[[mantle?"Completed inferences":"Accepted requests",inv,mantle?"Total of returned datapoints":"Includes requests that later fail"],["Input tokens",input,mantle?"Billable tokens":"InputTokenCount metric; cache reported separately"],["Output tokens",output,"Observed volume, without a quota multiplier"],["Throttles",mantle?null:total(g,"InvocationThrottles"),mantle?"Track HTTP 429 in application logs":"Includes the effects of client retries"],["Latency p99",gaugeMetric(g,"p99")?.summary.max??null,"Highest per-period p99, in milliseconds"]];
 $("resource-stats").innerHTML=stats.map(([label,value,sub])=>`<div><label>${h(label)}</label><strong>${fmt(value)}</strong><small>${h(sub)}</small></div>`).join("");
 // A model can have both a token and a request quota mapped; show each with its
 // own unit rather than picking whichever came first.
 const mapped=scoped(D.quotas).filter(q=>q.comparison?.metric_id&&g?.metrics.some(m=>m.id===q.comparison.metric_id));
 $("capacity-comparison").innerHTML=mapped.map(q=>{
  const unit=q.comparison.source==="AWS/Bedrock.Invocations"?"requests/min":"tokens/min";
  return `<div class="note info"><strong>Mapped quota: ${fmt(q.applied_value)} ${h(unit)}.</strong> Observed peak for this series: ${fmt(q.comparison.peak_per_minute)} ${h(unit)}, equivalent to <strong>${fmt(q.comparison.peak_percent)}%</strong> of the current quota.<br><span>${h(q.comparison.reason)}</span><br><code>${h(q.quota_code)}</code> · ${h(q.name)}</div>`;
 }).join("");
 const canvas=$("chart"),rect=canvas.getBoundingClientRect();if(!rect.width)return;
 const ratio=window.devicePixelRatio||1;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;
 const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);const w=rect.width,ht=rect.height,L=55,R=16,T=20,B=38,pw=w-L-R,ph=ht-T-B;
 ctx.clearRect(0,0,w,ht);ctx.font="11px -apple-system, sans-serif";
 if(!series.length){ctx.fillStyle="#607780";ctx.textAlign="center";ctx.fillText("No datapoints are available for this selection.",w/2,ht/2);$("chart-method").textContent="Missing data is not interpreted as zero.";chartData=null;return;}
 const start=Date.parse(D.start),end=Date.parse(D.end),bins=Math.min(168,Math.max(24,Math.floor(pw/5))),step=(end-start)/bins;
 let max=0;
 for(const s of series){s.values=Array(bins).fill(null);for(const [t,v] of s.metric.points){const i=Math.floor((Date.parse(t)-start)/step);if(i>=0&&i<bins){const value=gauge?v:v*60/D.period_seconds;s.values[i]=s.values[i]===null?value:Math.max(value,s.values[i]);max=Math.max(max,value)}}}
 const ymax=max>0?max*1.14:1;
 for(let i=0;i<=4;i++){const y=T+ph-i*ph/4;ctx.strokeStyle="#e7eff0";ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();ctx.fillStyle="#718890";ctx.textAlign="right";ctx.fillText(compact(ymax*i/4),L-9,y+4);}
 const width=pw/bins;
 for(let si=0;si<series.length;si++){const s=series[si];ctx.fillStyle=s.color;ctx.globalAlpha=.82;for(let i=0;i<bins;i++){if(s.values[i]===null)continue;const x=L+i*width+si*width/series.length+.5,height=Math.max(1,s.values[i]/ymax*ph);ctx.fillRect(x,T+ph-height,Math.max(.8,width/series.length-1),height)}}ctx.globalAlpha=1;
 ctx.fillStyle="#718890";for(let i=0;i<=4;i++){ctx.textAlign=i===0?"left":i===4?"right":"center";ctx.fillText(day(start+(end-start)*i/4),L+pw*i/4,ht-10);}
 const resolution=gauge?`the highest per-period value in each bucket; a per-period statistic cannot be re-aggregated into a window-wide one`:D.period_seconds===60?"1-minute peaks":`maximum per-minute averages within ${D.period_seconds/60}-minute intervals`;
 $("chart-method").textContent=`Displayed in ${bins} buckets: ${resolution}. ${series.some(s=>s.metric.status!=="ok")?"Incomplete query: these totals and peaks cover only returned data. ":""}Gaps remain missing; no interpolation is applied. Returned data is available in the CSV.`;
 chartData={series,start,step,bins,L,T,pw,ph,width,w,unit:gauge?" ms":" / min"};
 canvas.setAttribute("aria-label",`${state.mode}, ${g?.id||""}, from ${day(start)} to ${day(end)}. Observed maximum: ${fmt(max)}${gauge?" milliseconds":" per minute"}.`);
}
function renderQuotas(){
 const search=$("quota-search").value.toLowerCase(),kind=$("quota-kind").value,adjust=$("quota-adjustable").value;
 const rows=scoped(D.quotas).filter(q=>{
  const n=q.name.toLowerCase();return(!search||(n+" "+q.quota_code.toLowerCase()).includes(search))&&(!adjust||(q.adjustable===(adjust==="yes")))&&
  (!kind||(kind==="tokens"&&/tokens.*per minute/.test(n))||(kind==="daily"&&/tokens.*per day/.test(n))||(kind==="requests"&&/requests.*per minute/.test(n))||(kind==="batch"&&n.includes("batch"))||(kind==="provisioned"&&/provisioned|model units/.test(n)));
 }).sort((a,b)=>a.name.localeCompare(b.name));
 const draw=items=>table(["Quota","Applied","AWS default","Scope","Adjustable","Peak / current quota"],items.map(q=>`<tr>
 <td><strong>${h(q.name)}</strong><small>${h(q.quota_code)} · unit ${h(q.unit)}</small><details><summary>Source and interpretation</summary><p>${h(q.description)}<br>${h(q.source)} · ${h(when(q.collected_at))}<br>${h(q.comparison?.reason)}${q.context?.ContextId?"<br>Context: "+h(q.context.ContextId):""}</p></details></td>
 <td class="nowrap">${fmt(q.applied_value)}${q.applied_value==null?"<small>Not confirmed</small>":""}</td><td>${fmt(q.default_value)}</td><td>${badge(q.global?"Global":q.level==="RESOURCE"?"Resource":"Account / Region")}</td><td>${badge(q.adjustable?"Yes":"No",q.adjustable?"good":"")}</td><td>${q.comparison?.peak_percent!=null?fmt(q.comparison.peak_percent)+"%<small>Estimate</small>":"N/A<small>No validated comparison</small>"}</td></tr>`));
 pagination("quota",rows,draw,"quota-table","quota-page",20);
}
function renderModels(){
 const search=$("model-search").value.toLowerCase(),rows=scoped(D.models).filter(m=>(m.modelId+" "+m.modelName+" "+m.providerName).toLowerCase().includes(search)).sort((a,b)=>a.providerName.localeCompare(b.providerName)||a.modelName.localeCompare(b.modelName));
 const draw=items=>table(["Model","Provider","Inference types","Reported availability"],items.map(m=>{
  const a=m.availability,all=a&&a.authorizationStatus==="AUTHORIZED"&&a.regionAvailability==="AVAILABLE"&&a.entitlementAvailability==="AVAILABLE"&&a.agreementAvailability?.status==="AVAILABLE";
  return `<tr><td><strong>${h(m.modelName)}</strong><small>${h(m.modelId)}</small>${badge(m.modelLifecycle?.status||"Unknown")}</td><td>${h(m.providerName)}</td><td>${(m.inferenceTypesSupported||[]).map(t=>badge(t)).join(" ")}</td><td>${badge(all?"Reported available":a?"See status":"Not checked",all?"good":"warn")}<details><summary>API status</summary><p>Authorization: ${h(a?.authorizationStatus||"N/A")}<br>Region: ${h(a?.regionAvailability||"N/A")}<br>Entitlement: ${h(a?.entitlementAvailability||"N/A")}<br>Agreement: ${h(a?.agreementAvailability?.status||"N/A")}</p></details></td></tr>`;
 }));
 pagination("model",rows,draw,"model-table","model-page",20);
}
function renderProfiles(){
 const search=$("profile-search").value.toLowerCase(),rows=scoped(D.inference_profiles).filter(p=>(p.inferenceProfileId+" "+p.inferenceProfileName).toLowerCase().includes(search));
 const draw=items=>table(["Profile","Type","Status","Destination Regions"],items.map(p=>`<tr><td><strong>${h(p.inferenceProfileName)}</strong><small>${h(p.inferenceProfileId)}</small></td><td>${badge(p.type)}</td><td>${badge(p.status,p.status==="ACTIVE"?"good":"warn")}</td><td>${h([...new Set((p.models||[]).map(m=>m.modelArn.split(":")[3]||"global / unspecified"))].join(", "))}</td></tr>`));
 pagination("profile",rows,draw,"profile-table","profile-page",20);
 const resources=scoped(D.provisioned_throughput);
 const listing=D.collections.find(c=>c.region===state.region&&c.operation==="list_provisioned_model_throughputs");
 $("provisioned-table").innerHTML=resources.length?table(["Resource","Status","Current units","Desired units"],resources.map(p=>`<tr><td>${h(p.provisionedModelName)}<small>${h(p.provisionedModelArn)}</small></td><td>${h(p.status)}</td><td>${fmt(p.modelUnits)}</td><td>${fmt(p.desiredModelUnits)}</td></tr>`)):`<div class="empty">${listing?.status==="ok"?"No provisioned resources found in this Region.":"Could not confirm the provisioned resource inventory."}</div>`;
}
function renderQuality(){
 const metrics=scoped(D.metrics),counts={};for(const m of metrics)counts[m.status]=(counts[m.status]||0)+1;
 $("quality-summary").innerHTML=`<div class="cards">${[["With data",counts.ok||0],["No datapoints",counts.no_data||0],["Partial / error",(counts.partial||0)+(counts.error||0)],["Not queried",counts.not_queried||0]].map(([label,value])=>`<div class="card"><label>${h(label)}</label><strong>${fmt(value)}</strong></div>`).join("")}</div><p class="muted text-sm">${fmt(metrics.reduce((n,m)=>n+m.points.length,0))} datapoints returned in this Region. “No datapoints” does not mean zero usage.</p>`;
 const rows=D.collection_issues.filter(i=>i.region===state.region||i.region==="all");
 const budget=D.collection_settings;
 if(budget)$("quality-summary").innerHTML+=`<p>Latest attempt: ${fmt(budget.metric_requests_used)} / ${fmt(budget.max_metric_requests)} metric requests; ${fmt(budget.datapoints_received)} / ${fmt(budget.max_datapoints)} datapoints; ${fmt(budget.selected_series)} / ${fmt(budget.max_metrics)} selected series. Deferred identities: ${fmt(scoped(D.deferred_metrics||[]).length)} in this Region. Completed time windows are retained for resume.</p>`;
 pagination("quality",rows,items=>table(["Operation","Status","Resource / detail"],items.map(i=>`<tr><td><code>${h(i.operation)}</code></td><td>${badge(i.status,"warn")}</td><td>${h(i.message)}<small>${h(i.resource)}</small></td></tr>`)),"quality-table","quality-page",15);
 $("limitations").innerHTML=D.limitations.map(x=>`<li>${h(x)}</li>`).join("");
 $("calls-table").innerHTML=table(["Operation","Calls"],Object.entries(D.api_calls||{}).map(([op,n])=>`<tr><td><code>${h(op)}</code></td><td>${fmt(n)}</td></tr>`));
}
const LOG_RANK={DEBUG:10,INFO:20,WARN:30,ERROR:40};
// Log rows need second precision and no line breaks; `when` is for prose elsewhere.
const clock=v=>new Date(v).toISOString().replace("T"," ").slice(5,19)+"Z";
function renderRunLog(){
 const events=(D.run_log||{}).events||[];
 const counts=(D.run_log||{}).levels||{};
 const attempts=new Set(events.map(e=>e.attempt||1)).size;
 $("log-summary").innerHTML=events.length
  ?`<div class="cards">${[["Events",events.length],["Attempts",attempts],["Warnings",counts.WARN||0],["Errors",counts.ERROR||0]].map(([label,value])=>`<div class="card"><label>${h(label)}</label><strong>${fmt(value)}</strong></div>`).join("")}</div>`
  :'<div class="empty">This report was produced before run logging, or by --render of such a report.<strong>Collect again to capture the execution history.</strong></div>';
 const phases=[...new Set(events.map(e=>e.phase).filter(Boolean))];
 const chosen=$("log-phase").value;
 $("log-phase").innerHTML='<option value="">All phases</option>'+phases.map(p=>`<option${p===chosen?" selected":""}>${h(p)}</option>`).join("");
 const search=$("log-search").value.toLowerCase(),floor=LOG_RANK[$("log-level").value]||0,phase=$("log-phase").value;
 const rows=events.filter(e=>(LOG_RANK[e.level]||20)>=floor&&(!phase||e.phase===phase)
  &&(!search||[e.operation,e.region,e.status,e.detail,e.phase].join(" ").toLowerCase().includes(search)));
 const draw=items=>table(["#","Time","Level","Phase / Region","Operation","Detail","Cost"],items.map(e=>{
  const kind=e.level==="ERROR"?"bad":e.level==="WARN"?"warn":"";
  const cost=[e.duration_ms!=null?`${fmt(e.duration_ms)} ms`:"",e.pages?`${fmt(e.pages)} page(s)`:"",
   e.points!=null?`${fmt(e.points)} pts`:"",e.requests_used!=null?`req ${fmt(e.requests_used)}`:""].filter(Boolean).join(" · ");
  return `<tr><td class="nowrap"><code>${h(e.attempt||1)}.${h(e.seq)}</code></td><td class="nowrap text-xs">${h(clock(e.timestamp))}</td><td>${badge(e.level,kind)}</td>`
   +`<td class="nowrap">${h(e.phase)}<small>${h(e.region||"all Regions")}</small></td><td class="nowrap"><code>${h(e.operation)}</code><small>${h(e.status)}</small></td>`
   +`<td>${h(e.detail)}</td><td class="text-xs nowrap">${h(cost)}</td></tr>`;
 }));
 pagination("runlog",rows,draw,"log-table","log-page",25);
}
function refresh(){
 buildGroups();renderOverview();renderQuotas();renderModels();renderProfiles();renderQuality();renderRunLog();
 $("quota-nav").textContent=fmt(scoped(D.quotas).length);$("model-nav").textContent=fmt(scoped(D.models).length);$("profile-nav").textContent=fmt(scoped(D.inference_profiles).length);$("issue-nav").textContent=fmt(scoped(D.collection_issues).length);
 $("log-nav").textContent=fmt(((D.run_log||{}).events||[]).length);
}
$("account-side").textContent=D.account_id;$("profile-side").textContent="Profile "+D.profile;
$("region").innerHTML=D.regions.map(r=>`<option>${h(r)}</option>`).join("");
$("window").textContent=`${day(D.start)} — ${day(D.end)} · ${Math.round((Date.parse(D.end)-Date.parse(D.start))/86400000)} days`;
$("resolution").textContent=`Resolution ${D.period_seconds/60} min · UTC`;
$("footer").textContent=`Account ${D.account_id} · collected ${when(D.completed_at||D.generated_at)} · collector v${D.collector_version} · report v${D.renderer_version||D.collector_version} · local data; this report makes no network requests.`;
$("region").onchange=()=>{state.region=$("region").value;state.pages={};refresh()};
$("resource").onchange=()=>{state.resource=$("resource").value;renderChart()};
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{state.tab=b.dataset.tab;document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("active",x===b));document.querySelectorAll(".section").forEach(x=>x.classList.toggle("active",x.id===state.tab));if(state.tab==="overview")renderChart();window.scrollTo({top:0,behavior:"smooth"})});
document.querySelectorAll("#chart-tabs button").forEach(b=>b.onclick=()=>{state.mode=b.dataset.mode;document.querySelectorAll("#chart-tabs button").forEach(x=>x.classList.toggle("active",x===b));renderChart()});
for(const [ids,key,fn] of [[["quota-search","quota-kind","quota-adjustable"],"quota",renderQuotas],[["model-search"],"model",renderModels],[["profile-search"],"profile",renderProfiles],[["log-search","log-level","log-phase"],"runlog",renderRunLog]])for(const id of ids)$(id).addEventListener("input",()=>{state.pages[key]=0;fn()});
$("chart").onmousemove=event=>{if(!chartData)return;const c=chartData,rect=$("chart").getBoundingClientRect(),x=event.clientX-rect.left,i=Math.floor((x-c.L)/c.width),tip=$("tooltip");if(i<0||i>=c.bins){tip.hidden=true;return}tip.innerHTML=`<strong>${h(when(c.start+i*c.step))}</strong><br>`+c.series.map(s=>`${h(s.label)}: ${s.values[i]===null?"no data":fmt(s.values[i])+c.unit}`).join("<br>");tip.hidden=false;tip.style.left=Math.min(Math.max(0,x+10),Math.max(0,c.w-290))+"px";tip.style.top="15px"};
$("chart").onmouseleave=()=>{$("tooltip").hidden=true};
let resizeTimer;window.addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(renderChart,100)});
refresh();
</script>
</body></html>"""


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Execution interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
