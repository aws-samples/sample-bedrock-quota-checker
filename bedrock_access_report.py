#!/usr/bin/env python3
"""Read-only Bedrock inventory, quotas and CloudWatch history. No inference calls."""

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import zipfile

VERSION = "0.1.1"
UTC = timezone.utc
# Narrow mappings verified against quota definitions and system profile model IDs.
# Names are guards against changed quota semantics, not fuzzy matching rules.
TPM_RULES_VERSION = "2026-09-25.1"
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
REPORT_LIMITATIONS = (
    "Quotas are a current snapshot; they do not reconstruct historical limits.",
    "Reported availability does not test the application's effective invocation permissions.",
    "EstimatedTPMQuotaUsage is an estimate; it does not reproduce upfront max_tokens reservations.",
    "Missing metrics are not converted to zero. Statistics use only returned datapoints.",
    "ListMetrics omits series inactive for two weeks; known-ID probes cannot guarantee coverage of deleted resources.",
    "Percentages are calculated only when the quota mapping and units are confirmed.",
    "Totals from series with different dimensions are not added together, to avoid double counting.",
    "Mantle has its own namespace and quotas. Missing series do not prove that there was no usage.",
    "This collection covers only the listed Regions; global quotas may have additional usage from other source Regions.",
)
RUNTIME_METRICS = (
    "Invocations", "InputTokenCount", "OutputTokenCount", "EstimatedTPMQuotaUsage",
    "CacheReadInputTokenCount", "CacheWriteInputTokenCount", "InvocationThrottles",
    "InvocationClientErrors", "InvocationServerErrors", "OutputImageCount",
)
MANTLE_METRICS = ("Inferences", "TotalInputTokens", "TotalOutputTokens", "InferenceClientErrors")
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
    return "m" + hashlib.sha256(repr((region, metric_key(metric, stat))).encode()).hexdigest()[:24]


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
        self.report = {
            "schema_version": "1.0", "collector_version": VERSION,
            "mapping_rules_version": TPM_RULES_VERSION,
            "generated_at": iso(datetime.now(UTC)), "profile": args.profile or "credential-chain",
            "start": iso(start), "end": iso(end), "period_seconds": period,
            "regions": [], "models": [], "inference_profiles": [], "provisioned_throughput": [],
            "quotas": [], "metrics": [], "collection_issues": [], "collections": [],
            "query_plan": [], "usage_skipped": args.skip_usage or args.plan,
            "limitations": list(REPORT_LIMITATIONS),
        }
        self.metric_calls = 0
        self.returned_points = 0

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
        """Atomically claim series against the global --max-metrics budget.

        Truncates `candidates` to the remaining budget, appends the survivors to
        self.report["metrics"] under the lock, and returns them. Doing the check
        and the append together prevents concurrent Regions from jointly exceeding
        the cap.
        """
        with self.lock:
            remaining = max(0, self.args.max_metrics - len(self.report["metrics"]))
            selected = candidates[:remaining]
            self.report["metrics"].extend(selected)
            return selected

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
        if not hasattr(client, operation):
            self.issue(region, operation, "not_supported", "Update boto3: this operation is missing from the installed SDK.")
            return None
        with self.lock:
            self.calls[f"{service}.{operation}"] += 1
        try:
            return getattr(client, operation)(**kwargs)
        except Exception as exc:
            response = getattr(exc, "response", {})
            error = response.get("Error", {})
            code = error.get("Code", type(exc).__name__)
            status = "access_denied" if any(x in code.lower() for x in ("accessdenied", "unauthorized")) else "error"
            if code in ("UnknownServiceError", "UnknownEndpointError"):
                status = "not_supported"
            self.issue(region, operation, status, f"{code}: {error.get('Message', str(exc))}", kwargs.get("modelId", ""))
            return None

    def listing(self, service, region, operation, key, token_key="nextToken", **kwargs):
        rows, visited, status = [], set(), "ok"
        for _ in range(1000):
            response = self.call(service, region, operation, **kwargs)
            if response is None:
                status = "partial" if rows else "error"
                break
            rows.extend(response.get(key, []))
            token = response.get(token_key)
            if not token:
                break
            if token in visited:
                self.issue(region, operation, "partial", "Repeated pagination token; collection stopped.")
                status = "partial"
                break
            visited.add(token)
            kwargs[token_key] = token
        else:
            self.issue(region, operation, "partial", "Page limit reached.")
            status = "partial"
        self.report["collections"].append({"region": region, "operation": operation, "status": status, "rows": len(rows)})
        return rows

    def inventory(self, region):
        print(f"[{region}] Collecting the catalog, profiles, and provisioned capacity...", flush=True)
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
        print(f"[{region}] {len(models)} models, {len(profiles)} profiles, {len(provisioned)} provisioned resources.", flush=True)
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
                    print(f"[{region}] Availability checked: {i}/{len(models)}.", flush=True)

    def quotas(self, region):
        print(f"[{region}] Collecting applied quotas and AWS defaults, with pagination...", flush=True)
        kwargs = {"ServiceCode": "bedrock", "MaxResults": 100}
        model = self.client("service-quotas", region).meta.service_model.operation_model("ListServiceQuotas")
        if "QuotaAppliedAtLevel" in model.input_shape.members:
            kwargs["QuotaAppliedAtLevel"] = "ALL"
        else:
            self.issue(region, "list_service_quotas", "partial", "This SDK supports only ACCOUNT-level queries; update it to query ALL levels.")
        applied = self.listing("service-quotas", region, "list_service_quotas", "Quotas", "NextToken", **kwargs)
        defaults = self.listing("service-quotas", region, "list_aws_default_service_quotas", "Quotas", "NextToken", ServiceCode="bedrock", MaxResults=100)
        merged = merge_quotas(applied, defaults, region, iso(datetime.now(UTC)))
        self.report["quotas"].extend(merged)
        print(f"[{region}] {len(merged)} quotas; {sum(q['applied_value'] is not None for q in merged)} with applied values.", flush=True)

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

        for namespace, names in (("AWS/Bedrock", RUNTIME_METRICS), ("AWS/BedrockMantle", MANTLE_METRICS)):
            rows = self.listing("cloudwatch", region, "list_metrics", "Metrics", "NextToken", Namespace=namespace)
            for metric in rows:
                if metric["MetricName"] in names:
                    add(metric, "ListMetrics")
        for quota in self.report["quotas"]:
            usage = quota.get("usage_metric")
            if quota["region"] != region or not usage:
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
        known_ids = set(self.args.model_ids or [])
        known_ids.update(m["modelId"] for m in self.report["models"] if m["region"] == region)
        known_ids.update(p["inferenceProfileId"] for p in self.report["inference_profiles"] if p["region"] == region)
        known_ids.update(p["provisionedModelArn"] for p in self.report["provisioned_throughput"] if p["region"] == region)
        for model_id_value in sorted(known_ids):
            # First probe one activity counter; expand historical IDs only when data exists.
            add({"Namespace": "AWS/Bedrock", "MetricName": "Invocations", "Dimensions": [
                {"Name": "ModelId", "Value": model_id_value},
            ]}, "known_model_id")
        # Reserve series against the shared --max-metrics budget and append them
        # atomically, so parallel Regions cannot jointly exceed the global cap.
        all_candidates = list(candidates.values())
        selected = self.reserve_metrics(all_candidates)
        if len(selected) != len(all_candidates):
            self.issue(region, "query_plan", "partial", f"Global limit of {self.args.max_metrics} series reached: {len(all_candidates)-len(selected)} series were not queried.")
        discovered = sum("ListMetrics" in m["sources"] for m in selected)
        plan = {
            "region": region, "series": len(selected), "discovered_series": discovered,
            "known_id_probes": sum("known_model_id" in m["sources"] for m in selected),
            "period_seconds": self.period,
            "max_possible_points": len(selected) * int((self.end-self.start).total_seconds()/self.period),
            "initial_get_metric_data_requests": math.ceil(len(selected)/100),
        }
        with self.lock:
            self.report["query_plan"].append(plan)
        print(f"[{region}] Plan: {len(selected)} series ({discovered} discovered), {self.period}s periods; limit of {self.args.max_datapoints:,} returned datapoints.", flush=True)
        return selected

    def expand_observed(self, region):
        # Snapshot the shared metric list under the lock; other Regions may be
        # appending to it concurrently.
        with self.lock:
            snapshot = list(self.report["metrics"])
        existing = {m["id"] for m in snapshot}
        candidates = []
        for observed in snapshot:
            if observed["region"] != region or not observed["points"]:
                continue
            dimensions = observed["metric"]["Dimensions"]
            namespace = observed["metric"]["Namespace"]
            # Probe only documented single-model dimensions; do not fabricate rollups.
            if len(dimensions) != 1:
                continue
            if namespace == "AWS/Bedrock" and dimensions[0]["Name"] == "ModelId":
                names = RUNTIME_METRICS
            elif namespace == "AWS/BedrockMantle" and dimensions[0]["Name"] == "Model":
                names = MANTLE_METRICS
            else:
                continue
            for name in names:
                metric = {"Namespace": namespace, "MetricName": name, "Dimensions": dimensions}
                key = metric_id(region, metric)
                if key in existing:
                    continue
                existing.add(key)
                candidates.append({
                    "id": key, "region": region, "metric": metric, "stat": "Sum",
                    "period_seconds": self.period, "sources": ["observed_id_expansion"],
                    "points": [], "status": "not_queried", "messages": [],
                })
        # Claim the candidates against the shared budget atomically.
        additional = self.reserve_metrics(candidates)
        if len(additional) != len(candidates):
            self.issue(region, "expand_observed", "partial", "Series limit reached while expanding identifiers with observed usage.")
        if additional:
            print(f"[{region}] Querying {len(additional)} additional series for IDs with observed usage.", flush=True)
        return additional

    def fetch_metrics(self, region, metrics):
        for offset in range(0, len(metrics), 100):
            batch = metrics[offset:offset+100]
            points = {m["id"]: {} for m in batch}
            states = {}
            lookup = {m["id"]: m for m in batch}
            query = {
                "StartTime": self.start, "EndTime": self.end, "ScanBy": "TimestampAscending",
                "MaxDatapoints": 100800,
                "MetricDataQueries": [{
                    "Id": m["id"], "MetricStat": {"Metric": m["metric"], "Period": self.period, "Stat": m["stat"]},
                    "ReturnData": True,
                } for m in batch],
            }
            seen_tokens, complete = set(), True
            while True:
                # Atomically claim one request against the shared call/datapoint
                # budgets so parallel Regions cannot jointly exceed them.
                if not self.reserve_metric_request():
                    self.issue(region, "get_metric_data", "partial", "Query or datapoint limit reached; results are partial.")
                    complete = False
                    break
                response = self.call("cloudwatch", region, "get_metric_data", **query)
                if response is None:
                    complete = False
                    break
                for message in response.get("Messages", []):
                    self.issue(region, "get_metric_data", "partial", message.get("Value", message))
                    complete = False
                new_points = 0
                for result in response.get("MetricDataResults", []):
                    key = result["Id"]
                    if key not in lookup:
                        continue
                    # PartialData is expected on intermediate pages. The final state wins.
                    states[key] = result.get("StatusCode", "Unknown")
                    lookup[key]["messages"].extend(result.get("Messages", []))
                    for timestamp, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                        if self.start <= timestamp < self.end and math.isfinite(value):
                            stamp = iso(timestamp)
                            if stamp not in points[key]:
                                new_points += 1
                            points[key][stamp] = value
                # Flush this response's datapoints to the shared counter so the
                # cross-Region budget check stays accurate.
                if new_points:
                    self.add_returned_points(new_points)
                token = response.get("NextToken")
                if not token:
                    break
                if token in seen_tokens:
                    self.issue(region, "get_metric_data", "partial", "Repeated pagination token.")
                    complete = False
                    break
                seen_tokens.add(token)
                query["NextToken"] = token
            for metric in batch:
                key = metric["id"]
                metric["points"] = [[stamp, value] for stamp, value in sorted(points[key].items())]
                code = states.get(key, "MissingResult")
                if not complete or code != "Complete" or metric["messages"]:
                    metric["status"] = "partial" if metric["points"] else "error"
                    if code != "Complete" or metric["messages"]:
                        self.issue(region, "get_metric_data", metric["status"], f"Final status: {code}. {metric['messages']}", key)
                else:
                    metric["status"] = "ok" if metric["points"] else "no_data"
            if self.metric_calls >= self.args.max_metric_requests or self.returned_points >= self.args.max_datapoints:
                if offset+len(batch) < len(metrics):
                    self.issue(region, "get_metric_data", "partial", f"Query/datapoint limit reached; {len(metrics)-offset-len(batch)} series were not queried.")
                break
            if offset % 500 == 0 or offset+100 >= len(metrics):
                print(f"[{region}] History: {min(offset+100,len(metrics))}/{len(metrics)} series, {self.returned_points:,} datapoints.", flush=True)

    def collect_region(self, region):
        """Collect everything for a single Region. Safe to run concurrently:
        shared budgets and counters are guarded by self.lock, and the report
        lists are only appended to (atomic under the GIL)."""
        self.inventory(region)
        self.quotas(region)
        if not self.args.skip_usage:
            metrics = self.discover(region)
            if not self.args.plan:
                self.fetch_metrics(region, metrics)
                extra = self.expand_observed(region)
                if extra:
                    self.fetch_metrics(region, extra)

    def run(self, regions):
        identity = self.call("sts", regions[0], "get_caller_identity")
        if identity is None:
            raise RuntimeError("Could not identify the account. Refresh the profile credentials.")
        self.report["account_id"] = identity["Account"]
        self.report["partition"] = identity["Arn"].split(":")[1]
        self.report["regions"] = regions
        print(f"Account {identity['Account']} | profile {self.args.profile or 'credential-chain'} | {', '.join(regions)}", flush=True)
        print(f"UTC window {iso(self.start)} → {iso(self.end)} | {self.period}s", flush=True)
        # Collect Regions concurrently. Global budgets (--max-metrics,
        # --max-datapoints, --max-metric-requests) remain shared across all
        # Regions via atomic reservations, so total cost stays capped.
        workers = max(1, min(self.args.region_workers, len(regions)))
        if workers == 1 or len(regions) == 1:
            for region in regions:
                self.collect_region(region)
        else:
            print(f"Collecting {len(regions)} Regions with up to {workers} in parallel.", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self.collect_region, region): region for region in regions}
                for future in as_completed(futures):
                    region = futures[future]
                    # Surface unexpected failures without aborting the other Regions.
                    try:
                        future.result()
                    except Exception as exc:
                        self.issue(region, "collect_region", "error", f"Region collection failed: {exc}")
                        print(f"[{region}] Collection failed: {exc}", flush=True)
        self.report["completed_at"] = iso(datetime.now(UTC))
        self.report["api_calls"] = dict(self.calls)
        self.report["returned_datapoints"] = self.returned_points
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
        metric = by_id.get(quota.get("usage_metric_id"))
        if not metric:
            continue
        # Explicit usage metadata provides identity, but NOT necessarily compatible units.
        quota["comparison"] = {"status": "unmapped", "reason": "UsageMetric found; the quota unit and period have not been confirmed."}
        period = quota.get("period") or {}
        duration = period.get("PeriodValue", 0) * {"SECOND":1, "MINUTE":60, "HOUR":3600, "DAY":86400}.get(period.get("PeriodUnit"), 0)
        if (duration == 60 and metric["stat"] == "Sum" and not quota["global"] and
                quota.get("unit") in ("Count", "None") and quota["applied_value"] is not None and
                quota["applied_value"] > 0 and metric["status"] == "ok"):
            quota["comparison"] = {
                "status": "estimated", "source": "ServiceQuotas.UsageMetric",
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
        if len(profiles) != 1 or not metric or metric["status"] != "ok" or not quota["applied_value"] or quota["applied_value"] < 0:
            continue
        peak = metric["summary"]["peak_per_minute"]
        quota["comparison"] = {
            "status":"estimated", "source":"AWS/Bedrock.EstimatedTPMQuotaUsage",
            "rule_version":TPM_RULES_VERSION, "metric_id":metric["id"], "profile_id":rule[1],
            "peak_per_minute":peak, "peak_percent":100*peak/quota["applied_value"],
            "p95_percent":100*metric["summary"]["p95_per_minute"]/quota["applied_value"],
            "reason":"Peak for this profile's series against the current quota. This estimate excludes upfront reservations and does not guarantee coverage of other profiles sharing the quota.",
        }


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
    ]
    for key, fields in definitions:
        atomic_text(directory/f"{key}.csv", csv_text(report[key], fields))
    rows = []
    for metric in report["metrics"]:
        rows.append({
            "id": metric["id"], "region": metric["region"], "namespace": metric["metric"]["Namespace"],
            "metric": metric["metric"]["MetricName"], "dimensions": metric["metric"]["Dimensions"],
            "stat": metric["stat"], "period_seconds": metric["period_seconds"], "status": metric["status"],
            **metric.get("summary", {}),
        })
    atomic_text(directory/"usage_summary.csv", csv_text(rows, [
        "id", "region", "namespace", "metric", "dimensions", "stat", "period_seconds", "status",
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
    atomic_text(path, HTML.replace("__REPORT_JSON__", data))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--profile", help="AWS profile; uses boto3's credential chain when omitted")
    region = result.add_mutually_exclusive_group()
    region.add_argument("--regions", nargs="+", help="AWS source Regions to query (default: configured Region)")
    region.add_argument("--all-enabled-regions", action="store_true", help="Discover enabled Regions; requires ec2:DescribeRegions")
    time = result.add_mutually_exclusive_group()
    time.add_argument("--days", type=float, default=14, help="History length in days (default: 14)")
    time.add_argument("--start", help="History start in ISO 8601 format with a timezone")
    result.add_argument("--end", help="History end in ISO 8601 format (default: five minutes ago)")
    result.add_argument("--period", default="auto", choices=["auto", "60", "300", "3600"], help="Metric period in seconds; auto respects retention")
    result.add_argument("--model-ids", nargs="+", help="Additional runtime metric IDs to query")
    result.add_argument("--output-dir", default="./reports", help="Parent directory for generated reports (default: ./reports)")
    result.add_argument("--region-workers", type=int, default=4, help="Regions to collect in parallel; global budgets stay shared (default: 4, use 1 to serialize)")
    result.add_argument("--skip-usage", action="store_true", help="Collect inventory and quotas without CloudWatch metric queries")
    result.add_argument("--plan", action="store_true", help="Read inventory/metric identities without fetching datapoints")
    result.add_argument("--max-metrics", type=int, default=3000, help="Maximum selected series per run (default: 3000)")
    result.add_argument("--max-datapoints", type=int, default=2000000, help="Returned datapoint limit, checked between responses (default: 2000000)")
    result.add_argument("--max-metric-requests", type=int, default=200, help="Maximum GetMetricData calls per run (default: 200)")
    result.add_argument("--collect-only", action="store_true", help="Save data without rendering HTML")
    result.add_argument("--render", metavar="REPORT_JSON", help="Regenerate exports from saved data, without AWS calls")
    result.add_argument("--version", action="version", version=VERSION)
    return result


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
        print(f"JSON: {path}")
        print(f"ZIP: {archive}")
        return
    if args.skip_usage and args.plan:
        raise ValueError("--plan and --skip-usage cannot be used together.")
    if args.days <= 0 or min(args.max_metrics, args.max_datapoints, args.max_metric_requests) <= 0:
        raise ValueError("Days and collection limits must be positive.")
    start, end, period = window(args)
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install boto3 in your Python environment before running the collector.") from exc
    session = boto3.Session(profile_name=args.profile)
    regions = list(dict.fromkeys(args.regions or [session.region_name]))
    if not regions[0]:
        raise ValueError("No Region configured. Provide --regions or configure a Region in the profile.")
    collector = Collector(session, args, start, end, period)
    if args.all_enabled_regions:
        response = collector.call("ec2", regions[0], "describe_regions", AllRegions=False)
        if response is None:
            raise RuntimeError("Could not list enabled Regions; use --regions.")
        supported = set(session.get_available_regions("bedrock"))
        regions = [r["RegionName"] for r in response["Regions"] if r["RegionName"] in supported]
        if not regions:
            raise RuntimeError("No enabled Regions match the Bedrock endpoints known to this SDK.")
    # Order Regions by display priority so the HTML Overview and its Region
    # selector lead with us-east-1, then North America, Europe, South America.
    regions = order_regions(regions)
    report = collector.run(regions)
    name = f"bedrock-report_{report['account_id']}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    directory = Path(args.output_dir).expanduser().resolve()/name
    directory.mkdir(parents=True, exist_ok=False)
    archive = export(report, directory, args.collect_only)
    if not args.collect_only:
        print(f"HTML: {directory/'report.html'}")
    print(f"QUOTAS CSV: {directory/'quotas.csv'}")
    print(f"JSON: {directory/'report.json'}")
    print(f"ZIP: {archive}")
    print(f"Collection complete: {len(report['collection_issues'])} issues recorded; {report['returned_datapoints']:,} datapoints.")


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>Bedrock · Quotas & usage</title>
<style>
:root{--ink:#132a32;--muted:#607780;--teal:#007e80;--border:#dde7e8;--paper:#f4f7f7;--orange:#c06a18;--blue:#597bea}
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
h1{font-size:32px;line-height:1.2;letter-spacing:-1.1px;margin:9px 0 10px}h2{font-size:18px;letter-spacing:-.35px;margin:0 0 5px}h3{font-size:14px;margin:0 0 10px}p{margin:0 0 12px}.muted{color:var(--muted)}.lead{font-size:14px;color:var(--muted);max-width:900px}
.scope{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:23px 0}.scope label{color:var(--muted);font-size:12px}.scope select{min-width:145px}
select,input{border:1px solid #ccdadd;background:#fff;border-radius:7px;padding:9px 12px;color:var(--ink);min-width:0}input{width:300px}.pill{background:#e9eff1;border-radius:6px;font-size:12px;padding:7px 10px;color:#46646e}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:22px 0}.card{border:1px solid var(--border);border-radius:12px;background:#fff;padding:20px 22px;min-width:0}.card label{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted)}.card strong{display:block;font-size:32px;font-weight:650;letter-spacing:-.8px;margin:7px 0}.card small{display:block;color:var(--muted);font-size:11px}
.panel{background:white;border:1px solid var(--border);border-radius:12px;padding:24px;margin:20px 0}.panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:20px;flex-wrap:wrap}.panel-head p{color:var(--muted);font-size:12px;margin:0}.resource-select{max-width:470px;width:100%;font-size:12px}
.note{border-left:3px solid #d0a050;background:#fcf7ec;color:#775820;border-radius:0 7px 7px 0;padding:13px 17px;font-size:12px;margin:18px 0}.info{border-left-color:#64a9af;background:#edf5f5;color:#43646b}
.tabs{display:flex;gap:5px;background:var(--paper);border:1px solid var(--border);padding:4px;border-radius:8px}.tabs button{border:0;background:transparent;border-radius:5px;padding:6px 11px;color:var(--muted);font-size:12px}.tabs .active{background:white;box-shadow:0 1px 3px #172a3212;color:var(--ink)}
.chart{height:280px;position:relative}.chart canvas{width:100%;height:100%}.legend{display:flex;flex-wrap:wrap;gap:18px;font-size:11px;color:var(--muted);margin:12px 0}.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}.tooltip{position:absolute;pointer-events:none;background:#112e37;color:#fff;border-radius:7px;padding:10px 13px;font-size:11px;box-shadow:0 4px 15px #0002;z-index:2;max-width:290px;white-space:normal}
.mini-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;padding-top:20px;border-top:1px solid var(--border);margin-top:14px}.mini-grid label{font-size:11px;color:var(--muted)}.mini-grid strong{display:block;font-size:22px;margin:3px 0}.mini-grid small{display:block;color:var(--muted);font-size:10px}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px}th{text-align:left;color:var(--muted);background:#f5f8f8;font-size:10px;letter-spacing:.5px;text-transform:uppercase;padding:12px;font-weight:650;white-space:nowrap}td{padding:13px 12px;border-bottom:1px solid #edf1f2;vertical-align:top}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}td strong{font-size:12px}td small{display:block;font-size:10px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}tbody tr:hover{background:#fbfdfd}code{font-size:11px;color:#54727b;overflow-wrap:anywhere}
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
   <div class="panel-head"><div class="tabs" id="chart-tabs"><button data-mode="tokens" class="active">Tokens</button><button data-mode="requests">Requests</button><button data-mode="throttles">Throttles</button></div><span class="muted" id="chart-unit" style="font-size:11px"></span></div>
   <div class="chart"><canvas id="chart" role="img" aria-label="Observed usage during the reporting period"></canvas><div class="tooltip" id="tooltip" hidden></div></div>
   <div class="legend" id="legend"></div>
   <p class="muted" id="chart-method" style="font-size:11px"></p>
   <div class="mini-grid" id="resource-stats"></div>
   <div id="capacity-comparison"></div>
  </div>
  <div class="note">Historical usage is compared with today's quotas. Token estimates do not reproduce the <code>max_tokens</code> reservations used for capacity control. A low estimate does not rule out throttling.</div>
  <div class="panel"><div class="panel-head"><div><h2>Series with observed data</h2><p>Volumes by identifier, without adding overlapping aggregates of the same traffic.</p></div><a href="usage_summary.csv" download style="font-size:12px">↓ Usage CSV</a></div><div class="table-wrap" id="usage-table"></div><div class="pagebar" id="usage-page"></div></div>
 </section>
 <section class="section" id="quotas">
  <div class="panel"><div class="panel-head"><div><h2>Current quotas</h2><p>Applied values and AWS defaults are kept separate.</p></div></div>
   <div class="filters"><input id="quota-search" placeholder="Search by model, name, or quota code…" aria-label="Search quotas"><select id="quota-kind" aria-label="Quota type"><option value="">All types</option><option value="tokens" selected>Tokens per minute</option><option value="requests">Requests per minute</option><option value="batch">Batch inference</option><option value="provisioned">Provisioned Throughput</option></select><select id="quota-adjustable" aria-label="Adjustable quotas"><option value="">All quotas</option><option value="yes">Adjustable</option><option value="no">Not adjustable</option></select></div>
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
  <div class="panel"><h2>Provisioned Throughput</h2><p class="muted" style="font-size:12px">Existing resources and allocated units, separate from allocation quotas.</p><div class="table-wrap" id="provisioned-table"></div></div>
 </section>
 <section class="section" id="quality">
  <div class="panel"><h2>Collection coverage</h2><div id="quality-summary"></div><div class="table-wrap" id="quality-table"></div><div class="pagebar" id="quality-page"></div></div>
  <div class="panel"><h2>Interpretation limits</h2><ul class="quality-list" id="limitations"></ul></div>
  <div class="panel"><h2>Operations performed</h2><p class="muted" style="font-size:12px">Logical collector calls; internal SDK retries may generate additional requests.</p><div class="table-wrap" id="calls-table"></div></div>
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
const table=(heads,rows)=>`<table><thead><tr>${heads.map((x,i)=>`<th>${h(x)}</th>`).join("")}</tr></thead><tbody>${rows.join("")||`<tr><td colspan="${heads.length}" class="empty">No results for these filters.</td></tr>`}</tbody></table>`;
const state={tab:"overview",region:D.regions[0],mode:"tokens",resource:"",pages:{}};
const scoped=rows=>rows.filter(r=>r.region===state.region);
const sumMetric=(g,name)=>g?.metrics.find(m=>m.metric.MetricName===name&&m.stat==="Sum");
const total=(g,name)=>sumMetric(g,name)?.summary.total??null;
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
 for(const m of scoped(D.metrics).filter(m=>m.points.length&&m.stat==="Sum"&&["AWS/Bedrock","AWS/BedrockMantle"].includes(m.metric.Namespace))){
  const dims=m.metric.Dimensions,key=m.region+"|"+m.metric.Namespace+"|"+JSON.stringify(dims);
  if(!map.has(key)){const identifier=dims.find(d=>d.Name==="ModelId"||d.Name==="Model")?.Value||"Account aggregate";
   map.set(key,{key,id:identifier,namespace:m.metric.Namespace,dims,metrics:[],extra:dims.filter(d=>d.Name!=="ModelId"&&d.Name!=="Model").map(d=>d.Name+"="+d.Value).join(", ")});
  }map.get(key).metrics.push(m);
 }
 groups=[...map.values()].sort((a,b)=>((total(b,"Invocations")??total(b,"Inferences")??0)-(total(a,"Invocations")??total(a,"Inferences")??0))||a.id.localeCompare(b.id));
 if(!groups.some(g=>g.key===state.resource))state.resource=groups[0]?.key||"";
 $("resource").innerHTML=groups.length?groups.map(g=>`<option value="${h(g.key)}">${h(g.id)}${g.extra?" · "+h(g.extra):""} · ${g.namespace==="AWS/BedrockMantle"?"mantle":"runtime"}</option>`).join(""):'<option>No series returned datapoints</option>';
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
 const issues=scoped(D.collection_issues);
 const unavailable=metrics.filter(m=>["partial","error","not_queried"].includes(m.status)).length;
 $("run-notice").innerHTML=D.usage_skipped?'<div class="note">This run did not retrieve usage datapoints. Run without --skip-usage or --plan to include history.</div>':issues.length||unavailable?`<div class="note">${fmt(issues.length)} issues recorded and ${fmt(unavailable)} series with incomplete queries. See “Collection quality” for coverage details.</div>`:"";
 const renderRows=items=>table(["Identifier","Endpoint","Requests","Input tokens","Output tokens","Throttles"],items.map(g=>{
  const mantle=g.namespace==="AWS/BedrockMantle";
  return `<tr><td><strong>${h(g.id)}</strong><small>${h(g.extra||(g.dims.length?"Model dimension":"Aggregate series, without dimensions"))}</small></td><td>${badge(mantle?"mantle":"runtime")}</td><td>${fmt(total(g,mantle?"Inferences":"Invocations"))}</td><td>${fmt(total(g,mantle?"TotalInputTokens":"InputTokenCount"))}</td><td>${fmt(total(g,mantle?"TotalOutputTokens":"OutputTokenCount"))}</td><td>${mantle?"Not published":fmt(total(g,"InvocationThrottles"))}</td></tr>`;
 }));
 pagination("usage",groups,renderRows,"usage-table","usage-page");
 renderChart();
}
function renderChart(){
 const g=groups.find(g=>g.key===state.resource),mantle=g?.namespace==="AWS/BedrockMantle";
 const definitions=state.mode==="tokens"?[
  [mantle?"TotalInputTokens":"InputTokenCount","Input","#007e80"],
  [mantle?"TotalOutputTokens":"OutputTokenCount","Output","#597bea"],
  ...(mantle?[]:[["EstimatedTPMQuotaUsage","Estimated TPM","#c98536"]]),
 ]:state.mode==="requests"?[[mantle?"Inferences":"Invocations",mantle?"Completed inferences":"Successful requests","#007e80"]]:[["InvocationThrottles","Throttles","#c98536"]];
 const series=definitions.map(([name,label,color])=>({name,label,color,metric:sumMetric(g,name)})).filter(s=>s.metric?.points.length);
 $("chart-unit").textContent=state.mode==="tokens"?"tokens / min":state.mode==="requests"?"requests / min":"throttles / min";
 $("legend").innerHTML=definitions.map(([name,label,color])=>`<span><i class="dot" style="background:${color}"></i>${h(label)}${!sumMetric(g,name)?.points.length?" · no data":""}</span>`).join("");
 const inv=total(g,mantle?"Inferences":"Invocations"),input=total(g,mantle?"TotalInputTokens":"InputTokenCount"),output=total(g,mantle?"TotalOutputTokens":"OutputTokenCount");
 const stats=[[mantle?"Completed inferences":"Successful requests",inv,"Total of returned datapoints"],["Input tokens",input,mantle?"Billable tokens":"InputTokenCount metric; cache reported separately"],["Output tokens",output,"Observed volume, without a quota multiplier"],["Throttles",mantle?null:total(g,"InvocationThrottles"),mantle?"Metric not published in this namespace":"Includes the effects of client retries"]];
 $("resource-stats").innerHTML=stats.map(([label,value,sub])=>`<div><label>${h(label)}</label><strong>${fmt(value)}</strong><small>${h(sub)}</small></div>`).join("");
 const q=scoped(D.quotas).find(q=>q.comparison?.metric_id&&g?.metrics.some(m=>m.id===q.comparison.metric_id));
 $("capacity-comparison").innerHTML=q?`<div class="note info"><strong>Mapped quota: ${fmt(q.applied_value)} tokens/min.</strong> Observed peak for this series: ${fmt(q.comparison.peak_per_minute)} tokens/min, equivalent to <strong>${fmt(q.comparison.peak_percent)}%</strong> of the current quota.<br><span>${h(q.comparison.reason)}</span><br><code>${h(q.quota_code)}</code> · ${h(q.name)}</div>`:"";
 const canvas=$("chart"),rect=canvas.getBoundingClientRect();if(!rect.width)return;
 const ratio=window.devicePixelRatio||1;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;
 const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);const w=rect.width,ht=rect.height,L=55,R=16,T=20,B=38,pw=w-L-R,ph=ht-T-B;
 ctx.clearRect(0,0,w,ht);ctx.font="11px -apple-system, sans-serif";
 if(!series.length){ctx.fillStyle="#607780";ctx.textAlign="center";ctx.fillText("No datapoints are available for this selection.",w/2,ht/2);$("chart-method").textContent="Missing data is not interpreted as zero.";chartData=null;return;}
 const start=Date.parse(D.start),end=Date.parse(D.end),bins=Math.min(168,Math.max(24,Math.floor(pw/5))),step=(end-start)/bins;
 let max=0;
 for(const s of series){s.values=Array(bins).fill(null);for(const [t,v] of s.metric.points){const i=Math.floor((Date.parse(t)-start)/step);if(i>=0&&i<bins){const value=v*60/D.period_seconds;s.values[i]=s.values[i]===null?value:Math.max(value,s.values[i]);max=Math.max(max,value)}}}
 const ymax=max>0?max*1.14:1;
 for(let i=0;i<=4;i++){const y=T+ph-i*ph/4;ctx.strokeStyle="#e7eff0";ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();ctx.fillStyle="#718890";ctx.textAlign="right";ctx.fillText(compact(ymax*i/4),L-9,y+4);}
 const width=pw/bins;
 for(let si=0;si<series.length;si++){const s=series[si];ctx.fillStyle=s.color;ctx.globalAlpha=.82;for(let i=0;i<bins;i++){if(s.values[i]===null)continue;const x=L+i*width+si*width/series.length+.5,height=Math.max(1,s.values[i]/ymax*ph);ctx.fillRect(x,T+ph-height,Math.max(.8,width/series.length-1),height)}}ctx.globalAlpha=1;
 ctx.fillStyle="#718890";for(let i=0;i<=4;i++){ctx.textAlign=i===0?"left":i===4?"right":"center";ctx.fillText(day(start+(end-start)*i/4),L+pw*i/4,ht-10);}
 const resolution=D.period_seconds===60?"1-minute peaks":`maximum per-minute averages within ${D.period_seconds/60}-minute intervals`;
 $("chart-method").textContent=`Displayed in ${bins} buckets: ${resolution}. Gaps remain missing; no interpolation is applied. Full data is available in the CSV.`;
 chartData={series,start,step,bins,L,T,pw,ph,width,w};
 canvas.setAttribute("aria-label",`${state.mode}, ${g?.id||""}, from ${day(start)} to ${day(end)}. Observed maximum: ${fmt(max)} per minute.`);
}
function renderQuotas(){
 const search=$("quota-search").value.toLowerCase(),kind=$("quota-kind").value,adjust=$("quota-adjustable").value;
 const rows=scoped(D.quotas).filter(q=>{
  const n=q.name.toLowerCase();return(!search||(n+" "+q.quota_code.toLowerCase()).includes(search))&&(!adjust||(q.adjustable===(adjust==="yes")))&&
  (!kind||(kind==="tokens"&&/tokens.*per minute/.test(n))||(kind==="requests"&&/requests.*per minute/.test(n))||(kind==="batch"&&n.includes("batch"))||(kind==="provisioned"&&/provisioned|model units/.test(n)));
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
 $("quality-summary").innerHTML=`<div class="cards">${[["With data",counts.ok||0],["No datapoints",counts.no_data||0],["Partial / error",(counts.partial||0)+(counts.error||0)],["Not queried",counts.not_queried||0]].map(([label,value])=>`<div class="card"><label>${h(label)}</label><strong>${fmt(value)}</strong></div>`).join("")}</div><p class="muted" style="font-size:12px">${fmt(metrics.reduce((n,m)=>n+m.points.length,0))} datapoints returned in this Region. “No datapoints” does not mean zero usage.</p>`;
 const rows=scoped(D.collection_issues);
 pagination("quality",rows,items=>table(["Operation","Status","Resource / detail"],items.map(i=>`<tr><td><code>${h(i.operation)}</code></td><td>${badge(i.status,"warn")}</td><td>${h(i.message)}<small>${h(i.resource)}</small></td></tr>`)),"quality-table","quality-page",15);
 $("limitations").innerHTML=D.limitations.map(x=>`<li>${h(x)}</li>`).join("");
 $("calls-table").innerHTML=table(["Operation","Calls"],Object.entries(D.api_calls||{}).map(([op,n])=>`<tr><td><code>${h(op)}</code></td><td>${fmt(n)}</td></tr>`));
}
function refresh(){
 buildGroups();renderOverview();renderQuotas();renderModels();renderProfiles();renderQuality();
 $("quota-nav").textContent=fmt(scoped(D.quotas).length);$("model-nav").textContent=fmt(scoped(D.models).length);$("profile-nav").textContent=fmt(scoped(D.inference_profiles).length);$("issue-nav").textContent=fmt(scoped(D.collection_issues).length);
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
for(const [ids,key,fn] of [[["quota-search","quota-kind","quota-adjustable"],"quota",renderQuotas],[["model-search"],"model",renderModels],[["profile-search"],"profile",renderProfiles]])for(const id of ids)$(id).addEventListener("input",()=>{state.pages[key]=0;fn()});
$("chart").onmousemove=event=>{if(!chartData)return;const c=chartData,rect=$("chart").getBoundingClientRect(),x=event.clientX-rect.left,i=Math.floor((x-c.L)/c.width),tip=$("tooltip");if(i<0||i>=c.bins){tip.hidden=true;return}tip.innerHTML=`<strong>${h(when(c.start+i*c.step))}</strong><br>`+c.series.map(s=>`${h(s.label)}: ${s.values[i]===null?"no data":fmt(s.values[i])+" / min"}`).join("<br>");tip.hidden=false;tip.style.left=Math.min(Math.max(0,x+10),Math.max(0,c.w-290))+"px";tip.style.top="15px"};
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
