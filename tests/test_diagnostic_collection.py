import copy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock

from bedrock_access_report import (
    Collector, MANTLE_RULES, TPM_RULES, analyze, iso, merge_quotas,
    metric_id, parser,
)


START = datetime(2026, 9, 21, tzinfo=timezone.utc)


def collector(days=1, **options):
    args = parser().parse_args([])
    for name, value in options.items():
        setattr(args, name, value)
    return Collector(None, args, START, START + timedelta(days=days), 60)


def series(name="InvocationThrottles", region="us-east-1", model=None, namespace="AWS/Bedrock"):
    dims = [] if model is None else [{"Name": "Model" if namespace == "AWS/BedrockMantle" else "ModelId", "Value": model}]
    metric = {"Namespace": namespace, "MetricName": name, "Dimensions": dims}
    return {
        "id": metric_id(region, metric), "region": region, "metric": metric,
        "stat": "Sum", "period_seconds": 60, "sources": ["ListMetrics"],
        "status": "not_queried", "messages": [], "points": [],
    }


def complete_response(*args, **query):
    return {"MetricDataResults": [
        {"Id": m["Id"], "StatusCode": "Complete",
         "Timestamps": [query["StartTime"]], "Values": [3]}
        for m in query["MetricDataQueries"]
    ]}


def snapshot(metrics, days=1):
    c = collector(days)
    c.report.update({
        "account_id": "test-account", "partition": "aws", "regions": ["us-east-1"],
        "metrics": metrics, "collector_version": "0.1.2",
    })
    return c.report


def resume_call(*args, **query):
    if args[0] == "sts":
        return {"Account": "test-account", "Arn": "arn:aws:iam::test-account:role/test"}
    return complete_response(*args, **query)


class DiagnosticCollectionTests(TestCase):
    def test_exhausted_budget_does_not_report_an_aws_error(self):
        c = collector()
        c.metric_calls = c.args.max_metric_requests
        item = series()
        c.call = Mock(side_effect=AssertionError("No AWS request permitted"))
        c.fetch_metrics("us-east-1", [item])
        c.call.assert_not_called()
        self.assertEqual(item["status"], "not_queried")
        self.assertEqual(item["status_reason"], "request_budget_exhausted")
        self.assertFalse(any("MissingResult" in i["message"] for i in c.report["collection_issues"]))

    def test_budget_stop_between_pages_preserves_data_without_claiming_coverage(self):
        c = collector(max_metric_requests=1)
        item = series()
        c.call = Mock(return_value={
            "NextToken": "more", "MetricDataResults": [
                {"Id": item["id"], "StatusCode": "PartialData", "Timestamps": [START], "Values": [7]},
            ],
        })
        c.fetch_metrics("us-east-1", [item])
        self.assertEqual(item["status"], "partial")
        self.assertEqual(item["status_reason"], "request_budget_exhausted")
        self.assertEqual(item["points"], [[iso(START), 7]])
        self.assertFalse(item.get("completed_windows"))

    def test_missing_result_after_a_real_request_is_still_an_error(self):
        c = collector()
        c.call = Mock(return_value={})
        item = series()
        c.fetch_metrics("us-east-1", [item])
        c.call.assert_called_once()
        self.assertEqual(item["status"], "error")
        self.assertEqual(item["status_reason"], "MissingResult")

    def test_diagnostics_precede_tokens_and_newest_window_is_first(self):
        c = collector(days=3, max_metric_requests=1)
        tokens = series("InputTokenCount")
        throttles = series()
        c.call = Mock(side_effect=complete_response)
        c.fetch_scheduled([tokens, throttles])
        query = c.call.call_args.kwargs
        self.assertEqual(query["StartTime"], START + timedelta(days=2))
        self.assertEqual(query["ScanBy"], "TimestampDescending")
        self.assertEqual([m["Id"] for m in query["MetricDataQueries"]], [throttles["id"]])
        self.assertEqual(tokens["status"], "not_queried")
        self.assertEqual(throttles["status"], "partial")

    def test_regions_rotate_before_older_windows(self):
        c = collector(days=2, max_metric_requests=2)
        c.call = Mock(side_effect=complete_response)
        c.fetch_scheduled([series(region="us-west-2"), series()])
        self.assertEqual([call.args[1] for call in c.call.call_args_list], ["us-east-1", "us-west-2"])
        self.assertTrue(all(call.kwargs["StartTime"] == START+timedelta(days=1) for call in c.call.call_args_list))

    def test_discovery_prepares_throttles_before_fetching_history(self):
        c = collector()
        active = series("Invocations", model="active")["metric"]
        c.listing = Mock(side_effect=lambda *a, **kw: [active] if kw["Namespace"] == "AWS/Bedrock" else [])
        c.report["models"] = [{"region": "us-east-1", "modelId": "inactive"}]
        c.call = Mock(side_effect=AssertionError("Discovery must not retrieve history"))
        candidates = c.discover("us-east-1")
        self.assertEqual(candidates[0]["metric"]["MetricName"] in ("InvocationThrottles", "InvocationClientErrors", "InvocationServerErrors"), True)
        self.assertTrue(any(m["metric"]["MetricName"] == "InvocationThrottles" and m["metric"]["Dimensions"] == active["Dimensions"] for m in candidates))
        self.assertTrue(any(m["metric"]["MetricName"] == "InvocationThrottles" and not m["metric"]["Dimensions"] for m in candidates))
        self.assertFalse(any(d["Value"] == "inactive" for m in candidates for d in m["metric"]["Dimensions"]))

    def test_inactive_catalog_and_administrative_usage_are_opt_in(self):
        c = collector()
        c.listing = Mock(return_value=[])
        c.report["models"] = [{"region": "us-east-1", "modelId": "inactive"}]
        c.report["quotas"] = [{
            "region": "us-east-1", "usage_metric": {
                "MetricNamespace": "AWS/Usage", "MetricName": "CallCount",
                "MetricStatisticRecommendation": "Sum", "MetricDimensions": {"Resource": "ListFlows"},
            },
        }]
        self.assertEqual(c.discover("us-east-1"), [])
        c.args.include_api_usage = c.args.include_inactive_models = True
        candidates = c.discover("us-east-1")
        self.assertTrue(any(m["metric"]["Namespace"] == "AWS/Usage" for m in candidates))
        self.assertTrue(any(m["sources"] == ["known_model_id"] for m in candidates))

    def test_completed_windows_resume_without_requerying_or_losing_points(self):
        first = collector(days=2, max_metric_requests=1)
        item = series()
        first.report = snapshot([item], days=2)
        first.call = Mock(side_effect=complete_response)
        first.fetch_scheduled([item])
        saved = first.finish()
        original = copy.deepcopy(saved)
        second = collector(days=2)
        second.call = Mock(side_effect=resume_call)
        result = second.resume(saved)
        metric_queries = [x for x in second.call.call_args_list if x.args[0] == "cloudwatch"]
        self.assertEqual(len(metric_queries), 1)
        self.assertEqual(metric_queries[0].kwargs["EndTime"], START+timedelta(days=1))
        self.assertEqual(result["metrics"][0]["status"], "ok")
        self.assertEqual(result["metrics"][0]["points"], [[iso(START), 3], [iso(START+timedelta(days=1)), 3]])
        self.assertEqual(result["metrics"][0]["completed_windows"], [[iso(START), iso(START+timedelta(days=2))]])
        self.assertEqual(result["collector_version"], "0.1.2")
        self.assertEqual(result["generated_at"], saved["generated_at"])
        self.assertEqual(saved, original)

    def test_legacy_partial_series_retries_full_window_and_deduplicates(self):
        item = series()
        item.update(status="partial", points=[[iso(START), 3]])
        c = collector()
        c.call = Mock(side_effect=resume_call)
        result = c.resume(snapshot([item]))
        self.assertEqual(result["metrics"][0]["points"], [[iso(START), 3]])
        self.assertEqual(result["metrics"][0]["status"], "ok")
        self.assertEqual(result["collection_settings"]["datapoints_received"], 1)

    def test_resume_checks_account_before_querying_metrics(self):
        c = collector()
        c.call = Mock(return_value={"Account": "wrong", "Arn": "arn:aws:iam::wrong:role/test"})
        with self.assertRaisesRegex(ValueError, "original account"):
            c.resume(snapshot([series()]))
        self.assertEqual(c.call.call_count, 1)

    def test_inventory_only_report_requires_a_new_collection(self):
        c = collector()
        c.call = Mock(side_effect=AssertionError("No AWS request expected"))
        with self.assertRaisesRegex(ValueError, "no saved metric identities"):
            c.resume(snapshot([]))
        c.call.assert_not_called()

    def test_run_reserves_diagnostics_across_regions_before_other_series(self):
        c = collector(max_metrics=1, region_workers=1)
        c.call = Mock(side_effect=resume_call)
        c.collect_region = Mock(side_effect=[
            [series("InputTokenCount")],
            [series(region="us-west-2")],
        ])
        report = c.run(["us-east-1", "us-west-2"])
        self.assertEqual(report["metrics"][0]["region"], "us-west-2")
        self.assertEqual(report["metrics"][0]["metric"]["MetricName"], "InvocationThrottles")
        self.assertEqual(len(report["deferred_metrics"]), 1)
        self.assertEqual(report["collection_settings"]["selected_series"], 1)

    def test_series_budget_defers_identities_and_resume_promotes_them(self):
        c = collector(max_metrics=1)
        c.report = snapshot([])
        c.call = Mock(side_effect=complete_response)
        c.collect_candidates([series("InputTokenCount"), series()])
        saved = c.finish()
        self.assertEqual(saved["metrics"][0]["metric"]["MetricName"], "InvocationThrottles")
        self.assertEqual(len(saved["deferred_metrics"]), 1)
        resumed = collector(max_metrics=1)
        resumed.call = Mock(side_effect=resume_call)
        result = resumed.resume(saved)
        self.assertEqual(len(result["metrics"]), 2)
        self.assertEqual(result["deferred_metrics"], [])
        self.assertTrue(all(m["status"] == "ok" for m in result["metrics"]))

    def test_api_failure_does_not_become_complete_when_another_window_succeeds(self):
        c = collector(days=2)
        responses = iter([None, complete_response(MetricDataQueries=[{"Id": series()["id"]}], StartTime=START)])
        c.call = Mock(side_effect=lambda *a, **kw: next(responses))
        item = series()
        c.fetch_scheduled([item])
        self.assertEqual(item["status"], "partial")
        self.assertEqual(item["status_reason"], "api_error")
        self.assertEqual(item["completed_windows"], [[iso(START), iso(START+timedelta(days=1))]])

    def test_datapoint_budget_is_reported_separately_and_caps_request_size(self):
        c = collector(days=2, max_datapoints=1)
        c.call = Mock(side_effect=complete_response)
        item = series()
        c.fetch_scheduled([item])
        self.assertEqual(c.call.call_args.kwargs["MaxDatapoints"], 1)
        self.assertEqual(item["status_reason"], "datapoint_budget_exhausted")


class ExpandedMappingTests(TestCase):
    def mantle_report(self, status="ok"):
        code = "L-31615887"
        name, model, metric_name = MANTLE_RULES[code]
        item = series(metric_name, model=model, namespace="AWS/BedrockMantle")
        item.update(status=status, points=[[iso(START), 15_000_000]])
        report = snapshot([item])
        report["quotas"] = merge_quotas([{"QuotaCode": code, "QuotaName": name, "Value": 60_000_000}], [], "us-east-1", "snapshot-time")
        return report

    def test_mantle_input_mapping_uses_only_matching_endpoint_model_and_direction(self):
        report = self.mantle_report()
        output = series("TotalOutputTokens", model="openai.gpt-5.6-luna", namespace="AWS/BedrockMantle")
        output.update(status="ok", points=[[iso(START), 999_000_000]])
        report["metrics"].append(output)
        analyze(report)
        self.assertEqual(report["quotas"][0]["comparison"]["peak_percent"], 25)
        self.assertEqual(report["quotas"][0]["collected_at"], "snapshot-time")
        self.assertTrue(report["diagnostic_coverage"][0]["mantle_requires_application_errors"])

    def test_partial_mapping_never_reports_an_apparently_healthy_percentage(self):
        report = self.mantle_report("partial")
        analyze(report)
        self.assertEqual(report["quotas"][0]["comparison"]["status"], "incomplete")
        self.assertNotIn("peak_percent", report["quotas"][0]["comparison"])

    def test_mantle_rejects_changed_quota_semantics_or_resource_scope(self):
        for field, value in (("name", "Different quota"), ("level", "RESOURCE"), ("global", True), ("context", {"ContextId": "resource"})):
            report = self.mantle_report()
            report["quotas"][0][field] = value
            analyze(report)
            self.assertEqual(report["quotas"][0]["comparison"]["status"], "unmapped")

    def test_daily_pricing_quota_is_not_compared_with_raw_token_counts(self):
        report = self.mantle_report()
        quota = report["quotas"][0]
        quota.update(quota_code="L-E3F10727", name="Cross-Model Max Tokens Per Day",
                     usage_metric_id=report["metrics"][0]["id"], period={"PeriodValue": 1, "PeriodUnit": "MINUTE"})
        analyze(report)
        self.assertEqual(quota["comparison"]["status"], "unsupported")
        self.assertNotIn("peak_percent", quota["comparison"])

    def test_global_runtime_mapping_validates_profile_destinations(self):
        code = "L-A103A344"
        name, profile, model = TPM_RULES[code]
        item = series("EstimatedTPMQuotaUsage", model=profile)
        item.update(status="ok", points=[[iso(START), 3_000_000]])
        report = snapshot([item])
        report["quotas"] = merge_quotas([{"QuotaCode": code, "QuotaName": name, "Value": 30_000_000}], [], "us-east-1", "now")
        report["inference_profiles"] = [{
            "region": "us-east-1", "inferenceProfileId": profile, "type": "SYSTEM_DEFINED",
            "models": [{"modelArn": "arn:aws:bedrock:::foundation-model/"+model}],
        }]
        analyze(report)
        self.assertEqual(report["quotas"][0]["comparison"]["peak_percent"], 10)
        report["inference_profiles"][0]["models"][0]["modelArn"] += "-other"
        analyze(report)
        self.assertEqual(report["quotas"][0]["comparison"]["status"], "unmapped")
