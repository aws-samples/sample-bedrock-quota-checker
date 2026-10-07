from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock

from bedrock_access_report import (
    BEDROCK_NAMESPACES, Collector, analyze, is_diagnostic, iso, metric_id,
    metric_priority, parser, stats_for,
)

UTC = timezone.utc
START = datetime(2026, 9, 21, tzinfo=UTC)


def collector(days=1, **options):
    args = parser().parse_args([])
    for name, value in options.items():
        setattr(args, name, value)
    return Collector(None, args, START, START + timedelta(days=days), 60)


def published(namespace, name, dims):
    return {"Namespace": namespace, "MetricName": name, "Dimensions": dims}


def listing_for(catalog):
    """Mock ListMetrics: one list of metric definitions per namespace."""
    return Mock(side_effect=lambda *a, **kw: list(catalog.get(kw["Namespace"], [])))


def names(candidates, namespace=None, stat=None):
    return {m["metric"]["MetricName"] for m in candidates
            if (namespace is None or m["metric"]["Namespace"] == namespace)
            and (stat is None or m["stat"] == stat)}


def inventory(collector_, namespace):
    return next(e for e in collector_.report["metric_inventory"] if e["namespace"] == namespace)


MODEL = [{"Name": "ModelId", "Value": "us.anthropic.claude-opus-5-5"}]
MANTLE = [{"Name": "Model", "Value": "openai.gpt-5.6-luna"}, {"Name": "Project", "Value": "default"}]


class StatisticSelectionTests(TestCase):
    def test_counters_use_sum_and_gauges_use_a_distribution(self):
        self.assertEqual(stats_for("Invocations"), ("Sum",))
        self.assertEqual(stats_for("InputTokenCount"), ("Sum",))
        # A gauge carries no meaningful sum: request the distribution instead.
        self.assertEqual(stats_for("InvocationLatency"), ("Average", "Maximum", "p99"))
        self.assertEqual(stats_for("TimeToFirstToken"), ("Average", "Maximum", "p99"))

    def test_error_and_throttle_counters_keep_diagnostic_priority(self):
        # Names the curated list never mentioned must still be scheduled first.
        for name in ("InferenceServerErrors", "InferenceThrottles", "InvocationThrottles"):
            self.assertTrue(is_diagnostic(name), name)
            item = {"metric": published("AWS/BedrockMantle", name, MANTLE), "sources": ["ListMetrics"]}
            self.assertEqual(metric_priority(item), 0, name)
        latency = {"metric": published("AWS/Bedrock", "InvocationLatency", MODEL), "sources": ["ListMetrics"]}
        self.assertNotEqual(metric_priority(latency), 0)

    def test_gauge_series_are_not_summed_or_converted_to_a_rate(self):
        metric = {"id": "m1", "stat": "p99", "period_seconds": 60,
                  "points": [[iso(START), 1200], [iso(START + timedelta(minutes=1)), 3400]]}
        counter = {"id": "m2", "stat": "Sum", "period_seconds": 60,
                   "points": [[iso(START), 60], [iso(START + timedelta(minutes=1)), 120]]}
        report = {"start": iso(START), "end": iso(START + timedelta(minutes=2)),
                  "period_seconds": 60, "metrics": [metric, counter], "quotas": [], "regions": []}
        analyze(report)
        # A p99 of 1200 ms and one of 3400 ms do not add up to 4600 of anything.
        self.assertIsNone(metric["summary"]["total"])
        self.assertIsNone(metric["summary"]["peak_per_minute"])
        self.assertEqual(metric["summary"]["max"], 3400)
        self.assertEqual(counter["summary"]["total"], 180)
        self.assertEqual(counter["summary"]["peak_per_minute"], 120)


class NamespaceDiscoveryTests(TestCase):
    def test_metric_names_outside_the_curated_list_are_collected_and_reported(self):
        c = collector()
        c.listing = listing_for({"AWS/BedrockMantle": [
            published("AWS/BedrockMantle", "Inferences", MANTLE),
            published("AWS/BedrockMantle", "InferenceServerErrors", MANTLE),
        ]})
        candidates = c.discover("us-east-1")
        self.assertIn("InferenceServerErrors", names(candidates, "AWS/BedrockMantle"))
        entry = inventory(c, "AWS/BedrockMantle")
        self.assertEqual(entry["not_in_curated_list"], ["InferenceServerErrors"])
        self.assertEqual(entry["dimension_names"], ["Model", "Project"])

    def test_multi_dimension_sets_are_expanded_to_the_other_published_names(self):
        c = collector()
        c.listing = listing_for({"AWS/BedrockMantle": [
            published("AWS/BedrockMantle", "Inferences", MANTLE),
            published("AWS/BedrockMantle", "InferenceServerErrors", MANTLE),
        ]})
        candidates = c.discover("us-east-1")
        at_multi = [m for m in candidates if m["metric"]["Dimensions"] == MANTLE]
        # Both published names exist at the Model + Project dimension set, and the
        # curated token metrics are probed there too.
        self.assertTrue({"Inferences", "InferenceServerErrors", "TotalInputTokens"}
                        <= {m["metric"]["MetricName"] for m in at_multi})
        self.assertTrue(all(len(m["metric"]["Dimensions"]) == 2 for m in at_multi))

    def test_gauge_statistics_are_requested_for_a_discovered_latency_metric(self):
        c = collector()
        c.listing = listing_for({"AWS/Bedrock/Guardrails": [
            published("AWS/Bedrock/Guardrails", "InvocationLatency", MODEL),
        ]})
        candidates = c.discover("us-east-1")
        latency = [m for m in candidates if m["metric"]["MetricName"] == "InvocationLatency"]
        self.assertEqual({m["stat"] for m in latency}, {"Average", "Maximum", "p99"})
        self.assertFalse(any(m["stat"] == "Sum" for m in latency))
        # Distinct statistics are distinct identities, never one overwriting another.
        self.assertEqual(len({m["id"] for m in latency}), 3)

    def test_every_bedrock_namespace_is_probed_and_an_empty_one_is_logged(self):
        c = collector()
        c.listing = listing_for({})
        c.discover("us-east-1")
        probed = [call.kwargs["Namespace"] for call in c.listing.call_args_list]
        self.assertEqual(probed, list(BEDROCK_NAMESPACES))
        self.assertEqual([e["namespace"] for e in c.report["metric_inventory"]], list(BEDROCK_NAMESPACES))
        # An absent namespace is recorded as empty, not silently assumed away.
        self.assertTrue(all(e["metric_names"] == [] for e in c.report["metric_inventory"]))
        self.assertTrue(any(e["status"] == "empty_namespace" for e in c.log.events))

    def test_extra_namespaces_from_the_flag_are_probed_without_duplicating_defaults(self):
        c = collector(namespaces=["AWS/Bedrock", "Custom/Namespace"])
        c.listing = listing_for({})
        c.discover("us-east-1")
        self.assertEqual(c.namespaces, list(BEDROCK_NAMESPACES) + ["Custom/Namespace"])
        self.assertIn("Custom/Namespace", [e["namespace"] for e in c.report["metric_inventory"]])

    def test_discovery_does_not_retrieve_history(self):
        c = collector()
        c.listing = listing_for({"AWS/Bedrock": [published("AWS/Bedrock", "Invocations", MODEL)]})
        c.call = Mock(side_effect=AssertionError("Discovery must not retrieve history"))
        self.assertTrue(c.discover("us-east-1"))
        c.call.assert_not_called()


class ObservedExpansionTests(TestCase):
    def observed(self, c, namespace, name, dims, points):
        metric = published(namespace, name, dims)
        c.report["metrics"].append({"id": metric_id("us-east-1", metric), "region": "us-east-1",
                                    "metric": metric, "stat": "Sum", "points": points})

    def test_multi_dimension_identifiers_expand_but_the_rollup_is_not_fabricated(self):
        c = collector()
        self.observed(c, "AWS/BedrockMantle", "Inferences", MANTLE, [[iso(START), 1]])
        self.observed(c, "AWS/Bedrock", "Invocations", [], [[iso(START), 1]])
        extra = c.expand_observed("us-east-1")
        self.assertTrue(extra)
        # The Model + Project set expands; the dimensionless rollup never does.
        self.assertTrue(all(m["metric"]["Dimensions"] == MANTLE for m in extra))

    def test_expansion_follows_the_names_the_namespace_published(self):
        c = collector()
        c.discovered[("us-east-1", "AWS/BedrockMantle")] = {"Inferences", "InferenceServerErrors"}
        self.observed(c, "AWS/BedrockMantle", "Inferences", MANTLE, [[iso(START), 1]])
        extra = c.expand_observed("us-east-1")
        self.assertIn("InferenceServerErrors", {m["metric"]["MetricName"] for m in extra})

    def test_expansion_requests_gauge_statistics_and_skips_inactive_identifiers(self):
        c = collector()
        c.discovered[("us-east-1", "AWS/Bedrock")] = {"InvocationLatency"}
        self.observed(c, "AWS/Bedrock", "Invocations", MODEL, [[iso(START), 1]])
        self.observed(c, "AWS/Bedrock", "Invocations",
                      [{"Name": "ModelId", "Value": "inactive"}], [])
        extra = c.expand_observed("us-east-1")
        latency = [m for m in extra if m["metric"]["MetricName"] == "InvocationLatency"]
        self.assertEqual({m["stat"] for m in latency}, {"Average", "Maximum", "p99"})
        self.assertFalse(any(d["Value"] == "inactive"
                             for m in extra for d in m["metric"]["Dimensions"]))


class ResumeDiscoveryTests(TestCase):
    def test_resume_restores_the_discovered_names_from_the_saved_inventory(self):
        c = collector()
        saved = collector().report
        saved.update(account_id="acct", partition="aws", regions=["us-east-1"],
                     collector_version="0.3.0", collection_issues=[],
                     metric_inventory=[{"region": "us-east-1", "namespace": "AWS/BedrockMantle",
                                        "metric_names": ["Inferences", "InferenceServerErrors"],
                                        "dimension_names": ["Model"], "dimension_sets": 1,
                                        "not_in_curated_list": ["InferenceServerErrors"]}],
                     metrics=[{"id": "m1", "region": "us-east-1", "status": "ok", "points": [],
                               "metric": published("AWS/BedrockMantle", "Inferences", MANTLE),
                               "stat": "Sum"}])
        c.call = Mock(return_value={"Account": "acct", "Arn": "arn:aws:iam::acct:role/test"})
        c.resume(saved)
        self.assertEqual(c.discovered[("us-east-1", "AWS/BedrockMantle")],
                         {"Inferences", "InferenceServerErrors"})

    def test_a_report_saved_before_discovery_still_renders(self):
        report = {"start": iso(START), "end": iso(START + timedelta(minutes=2)),
                  "period_seconds": 60, "metrics": [], "quotas": [], "regions": ["us-east-1"]}
        analyze(report)
        self.assertEqual(report["metric_inventory"], [])
        self.assertEqual(report["probed_namespaces"], list(BEDROCK_NAMESPACES))
