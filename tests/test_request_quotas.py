from datetime import datetime, timedelta, timezone
from unittest import TestCase

from bedrock_access_report import analyze, iso, merge_quotas, metric_id, normalize_label, rpm_targets

UTC = timezone.utc
START = datetime(2026, 9, 21, tzinfo=UTC)
PROFILE = "us.writer.palmyra-x5-v1:0"
MODEL = "writer.palmyra-x5-v1:0"


def counter(name, model_id, points, status="ok"):
    definition = {"Namespace": "AWS/Bedrock", "MetricName": name,
                  "Dimensions": [{"Name": "ModelId", "Value": model_id}]}
    return {"id": metric_id("us-east-1", definition), "region": "us-east-1", "metric": definition,
            "stat": "Sum", "period_seconds": 60, "status": status, "points": points, "messages": []}


def minutes(*values):
    return [[iso(START + timedelta(minutes=i)), v] for i, v in enumerate(values)]


def report(metrics, quotas, profile=PROFILE, model=MODEL, on_demand=False):
    return {
        "start": iso(START), "end": iso(START + timedelta(minutes=len(metrics[0]["points"]) or 1)),
        "period_seconds": 60, "regions": ["us-east-1"], "metrics": metrics, "quotas": quotas,
        "inference_profiles": [{
            "region": "us-east-1", "inferenceProfileId": profile, "type": "SYSTEM_DEFINED",
            "status": "ACTIVE", "models": [{"modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{model}"}],
        }],
        "models": [{"region": "us-east-1", "modelId": model, "modelName": "Palmyra X5",
                    "providerName": "Writer",
                    "inferenceTypesSupported": ["ON_DEMAND"] if on_demand else ["INFERENCE_PROFILE"]}],
    }


def quota(name, value, code="L-D655BF6C", **overrides):
    row = merge_quotas([{"QuotaCode": code, "QuotaName": name, "Value": value}], [], "us-east-1", "now")[0]
    row.update(overrides)
    return row


CROSS = "Cross-region model inference requests per minute for Palmyra X5"


class LabelResolutionTests(TestCase):
    def test_label_resolves_through_identifier_or_catalogue_name(self):
        targets = rpm_targets(report([counter("Invocations", PROFILE, [])], []), "us-east-1")
        # A quota label can spell the model as the identifier, the catalogue name,
        # or the provider and name together; all three reach the same profile.
        for label in ("writer.palmyra-x5-v1:0", "Palmyra X5", "Writer Palmyra X5"):
            self.assertEqual(targets["cross"].get(normalize_label(label)), PROFILE, label)
        self.assertEqual(targets["global"], {})

    def test_a_label_two_identities_share_is_dropped_rather_than_guessed(self):
        data = report([counter("Invocations", PROFILE, [])], [])
        twin = dict(data["inference_profiles"][0], inferenceProfileId="us.other.palmyra-x5-v1:0")
        data["inference_profiles"].append(twin)
        self.assertEqual(rpm_targets(data, "us-east-1")["cross"], {})

    def test_on_demand_scope_reaches_a_bare_model_only_when_the_catalogue_allows_it(self):
        profile_only = rpm_targets(report([counter("Invocations", MODEL, [])], []), "us-east-1")
        self.assertEqual(profile_only["on_demand"], {})
        allowed = rpm_targets(report([counter("Invocations", MODEL, [])], [], on_demand=True), "us-east-1")
        self.assertEqual(allowed["on_demand"].get(normalize_label("Palmyra X5")), MODEL)

    def test_a_profile_fanning_out_to_several_models_is_not_a_target(self):
        data = report([counter("Invocations", PROFILE, [])], [])
        data["inference_profiles"][0]["models"].append(
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/writer.palmyra-x4-v1:0"})
        self.assertEqual(rpm_targets(data, "us-east-1")["cross"], {})


class RequestQuotaComparisonTests(TestCase):
    def test_peak_accepted_requests_are_compared_with_the_applied_quota(self):
        data = report([counter("Invocations", PROFILE, minutes(4, 10, 7))], [quota(CROSS, 20)])
        analyze(data)
        comparison = data["quotas"][0]["comparison"]
        self.assertEqual(comparison["status"], "estimated")
        self.assertEqual(comparison["source"], "AWS/Bedrock.Invocations")
        self.assertEqual(comparison["peak_per_minute"], 10)
        self.assertEqual(comparison["peak_percent"], 50)
        self.assertEqual(comparison["model_id"], PROFILE)

    def test_recorded_throttling_travels_with_the_comparison(self):
        data = report([counter("Invocations", PROFILE, minutes(10)),
                       counter("InvocationThrottles", PROFILE, minutes(77))], [quota(CROSS, 10)])
        analyze(data)
        comparison = data["quotas"][0]["comparison"]
        self.assertEqual(comparison["peak_percent"], 100)
        self.assertEqual(comparison["throttles_total"], 77)

    def test_scope_must_agree_with_the_profile_it_resolves_to(self):
        # A regional profile cannot answer for a Global cross-region quota.
        data = report([counter("Invocations", PROFILE, minutes(10))],
                      [quota("Global cross-region model inference requests per minute for Palmyra X5", 20)])
        analyze(data)
        self.assertEqual(data["quotas"][0]["comparison"]["status"], "unmapped")

    def test_an_unresolved_label_stays_unmapped_and_says_why(self):
        data = report([counter("Invocations", PROFILE, minutes(10))],
                      [quota("Cross-region model inference requests per minute for Writer AI Palmyra X5 V1", 10)])
        analyze(data)
        comparison = data["quotas"][0]["comparison"]
        self.assertEqual(comparison["status"], "unmapped")
        self.assertIn("does not resolve", comparison["reason"])
        self.assertNotIn("peak_percent", comparison)

    def test_an_incomplete_query_never_reports_an_apparently_healthy_percentage(self):
        data = report([counter("Invocations", PROFILE, minutes(10), status="partial")], [quota(CROSS, 10)])
        analyze(data)
        comparison = data["quotas"][0]["comparison"]
        self.assertEqual(comparison["status"], "incomplete")
        self.assertNotIn("peak_percent", comparison)

    def test_resource_scope_and_zero_limits_are_rejected(self):
        for overrides in ({"level": "RESOURCE"}, {"global": True}, {"context": {"ContextId": "x"}},
                          {"applied_value": 0}, {"applied_value": None}):
            data = report([counter("Invocations", PROFILE, minutes(10))], [quota(CROSS, 10, **overrides)])
            analyze(data)
            self.assertEqual(data["quotas"][0]["comparison"]["status"], "unmapped", overrides)

    def test_a_token_quota_is_not_answered_by_the_request_rule(self):
        data = report([counter("Invocations", PROFILE, minutes(10))],
                      [quota("Cross-region model inference tokens per minute for Palmyra X5", 10)])
        analyze(data)
        self.assertEqual(data["quotas"][0]["comparison"]["status"], "unmapped")


class ThrottleEvidenceTests(TestCase):
    def evidence(self, accepted, throttles, quotas=()):
        data = report([counter("Invocations", PROFILE, accepted),
                       counter("InvocationThrottles", PROFILE, throttles)], list(quotas))
        analyze(data)
        return data["throttle_evidence"]

    def test_the_accepted_rate_during_throttling_is_reported_as_the_ceiling(self):
        # 10 accepted in each throttled minute, 2 in a minute without throttling:
        # the ceiling is what held while requests were being rejected.
        found = self.evidence(accepted=minutes(2, 10, 10), throttles=minutes(0, 40, 37))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["throttles_total"], 77)
        self.assertEqual(found[0]["throttled_intervals"], 2)
        self.assertEqual(found[0]["accepted_per_minute_while_throttled"], [10])
        self.assertEqual(found[0]["observed_ceiling_per_minute"], 10)
        self.assertEqual(found[0]["model_id"], PROFILE)
        self.assertEqual(found[0]["scope"], "cross")

    def test_quotas_are_offered_only_when_value_and_scope_both_fit(self):
        same_value_right_scope = quota(CROSS, 10, code="L-RIGHT")
        same_value_wrong_scope = quota(
            "Global cross-region model inference requests per minute for Other", 10, code="L-SCOPE")
        other_value = quota("Cross-region model inference requests per minute for Other", 99, code="L-VALUE")
        token_quota = quota("Cross-region model inference tokens per minute for Other", 10, code="L-TOKENS")
        found = self.evidence(minutes(10), minutes(77),
                              [same_value_right_scope, same_value_wrong_scope, other_value, token_quota])
        self.assertEqual([q["quota_code"] for q in found[0]["matching_request_quotas"]], ["L-RIGHT"])
        self.assertEqual(found[0]["matching_request_quota_count"], 1)

    def test_no_throttling_produces_no_evidence(self):
        self.assertEqual(self.evidence(minutes(10), minutes(0)), [])

    def test_evidence_needs_a_complete_accepted_series(self):
        data = report([counter("Invocations", PROFILE, minutes(10), status="partial"),
                       counter("InvocationThrottles", PROFILE, minutes(77))], [])
        analyze(data)
        self.assertEqual(data["throttle_evidence"], [])
