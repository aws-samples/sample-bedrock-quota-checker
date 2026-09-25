import argparse
import json
from datetime import datetime, timedelta, timezone
import unittest

from bedrock_access_report import Collector, analyze, merge_quotas, metric_id, metric_key, safe_csv_value, window

UTC = timezone.utc


def args(**updates):
    values = dict(profile="default", days=14, start=None, end=None, period="auto",
                  skip_usage=False, plan=False, max_metric_requests=200,
                  max_datapoints=2000000, max_metrics=3000, model_ids=None)
    return argparse.Namespace(**dict(values, **updates))


class CollectorTests(unittest.TestCase):
    def test_default_is_not_substituted_for_unknown_applied_and_zero_is_preserved(self):
        defaults = [{"QuotaCode":"Q1","QuotaName":"One","Value":500},
                    {"QuotaCode":"Q2","QuotaName":"Two","Value":100}]
        applied = [{"QuotaCode":"Q1","QuotaName":"One","Value":0}]
        result = {q["quota_code"]:q for q in merge_quotas(applied, defaults, "us-east-1", "now")}
        self.assertEqual(result["Q1"]["applied_value"], 0)
        self.assertEqual(result["Q1"]["default_value"], 500)
        self.assertIsNone(result["Q2"]["applied_value"])
        self.assertEqual(result["Q2"]["default_value"], 100)

    def test_resource_contexts_are_preserved(self):
        rows = [{"QuotaCode":"Q","QuotaName":"Limit","Value":v,"QuotaAppliedAtLevel":"RESOURCE",
                 "QuotaContext":{"ContextId":c}} for c,v in [("A",10),("B",20)]]
        result = merge_quotas(rows, [], "us-east-1", "now")
        self.assertEqual([(q["context"]["ContextId"],q["applied_value"]) for q in result], [("A",10),("B",20)])

    def test_retention_and_explicit_incompatible_resolution(self):
        now = datetime(2026,9,25,12,tzinfo=UTC)
        self.assertEqual(window(args(days=14), now)[2],60)
        self.assertEqual(window(args(days=30), now)[2],300)
        self.assertEqual(window(args(days=90), now)[2],3600)
        with self.assertRaises(ValueError):
            window(args(days=30,period="60"), now)

    def test_dimensions_are_canonical_but_rollups_are_distinct(self):
        m={"Namespace":"AWS/Bedrock","MetricName":"Invocations",
           "Dimensions":[{"Name":"ModelId","Value":"x"},{"Name":"A","Value":"b"}]}
        reverse=dict(m,Dimensions=list(reversed(m["Dimensions"])))
        self.assertEqual(metric_key(m), metric_key(reverse))
        self.assertNotEqual(metric_key(m), metric_key(dict(m,Dimensions=m["Dimensions"][:1])))

    def test_partial_intermediate_page_then_complete_is_success(self):
        start=datetime(2026,9,1,tzinfo=UTC)
        c=Collector(None,args(),start,start+timedelta(minutes=5),60)
        metric={"Namespace":"AWS/Bedrock","MetricName":"Invocations","Dimensions":[{"Name":"ModelId","Value":"x"}]}
        key=metric_id("us-east-1",metric)
        item={"id":key,"metric":metric,"stat":"Sum","points":[],"messages":[],"status":"not_queried"}
        responses=iter([
            {"NextToken":"page2","MetricDataResults":[{"Id":key,"StatusCode":"PartialData","Timestamps":[start],"Values":[5]}]},
            {"MetricDataResults":[{"Id":key,"StatusCode":"Complete","Timestamps":[start,start+timedelta(minutes=1)],"Values":[5,7]}]},
        ])
        c.call=lambda *a,**kw:next(responses)
        c.fetch_metrics("us-east-1",[item])
        self.assertEqual(item["status"],"ok")
        self.assertEqual([p[1] for p in item["points"]],[5,7])
        self.assertEqual(c.report["collection_issues"],[])
        self.assertEqual(c.returned_points,2)

    def test_access_denied_retains_partial_listing(self):
        c=Collector(None,args(),datetime.now(UTC),datetime.now(UTC),60)
        responses=iter([{"Rows":[1],"NextToken":"next"},None])
        c.call=lambda *a,**kw:next(responses)
        self.assertEqual(c.listing("cloudwatch","us-east-1","list_metrics","Rows","NextToken"),[1])
        self.assertEqual(c.report["collections"][-1]["status"],"partial")

    def test_final_partial_and_repeated_tokens_do_not_claim_success(self):
        start=datetime(2026,9,1,tzinfo=UTC)
        c=Collector(None,args(),start,start+timedelta(minutes=5),60)
        metric={"Namespace":"AWS/Bedrock","MetricName":"Invocations","Dimensions":[]}
        item={"id":"m1","metric":metric,"stat":"Sum","points":[],"messages":[]}
        response={"NextToken":"same","MetricDataResults":[{"Id":"m1","StatusCode":"PartialData","Timestamps":[start],"Values":[1]}]}
        c.call=lambda *a,**kw:response
        c.fetch_metrics("us-east-1",[item])
        self.assertEqual(item["status"],"partial")
        self.assertTrue(c.report["collection_issues"])

    def test_sparse_5_minute_data_is_not_zero_filled_or_a_one_minute_peak(self):
        metric={"id":"m1","points":[["2026-09-01T00:00:00Z",100],["2026-09-01T00:10:00Z",200]],
                "stat":"Sum","period_seconds":300}
        report={"start":"2026-09-01T00:00:00Z","end":"2026-09-01T00:15:00Z",
                "period_seconds":300,"metrics":[metric],"quotas":[]}
        analyze(report)
        self.assertEqual(metric["summary"]["total"],300)
        self.assertEqual(metric["summary"]["peak_per_minute"],40)
        self.assertEqual(metric["summary"]["missing_intervals"],1)
        self.assertEqual(metric["summary"]["p95_per_minute"],39)

    def test_mutations_are_rejected_before_creating_any_client(self):
        c=Collector(None,args(),datetime.now(UTC),datetime.now(UTC),60)
        with self.assertRaises(ValueError):
            c.call("bedrock","us-east-1","create_inference_profile")

    def test_csv_formula_payload_is_neutralized(self):
        self.assertEqual(safe_csv_value('=HYPERLINK("bad")'),"'=HYPERLINK(\"bad\")")
        self.assertEqual(safe_csv_value("  +cmd"),"'  +cmd")
        self.assertEqual(safe_csv_value(0),0)
        self.assertEqual(safe_csv_value(None),"")

    def test_quota_comparison_requires_matching_profile_and_rejects_changed_semantics(self):
        model="anthropic.claude-opus-4-7"
        definition={"Namespace":"AWS/Bedrock","MetricName":"EstimatedTPMQuotaUsage",
                    "Dimensions":[{"Name":"ModelId","Value":"us."+model}]}
        metric={"id":metric_id("us-east-1",definition),"metric":definition,"stat":"Sum",
                "period_seconds":60,"status":"ok","points":[["2026-09-01T00:00:00Z",150000]]}
        quota=merge_quotas([{"QuotaCode":"L-5DB28B7B",
                            "QuotaName":"Cross-region model inference tokens per minute for Anthropic Claude Opus 4.7",
                            "Value":30000000}],[],"us-east-1","now")[0]
        report={"start":"2026-09-01T00:00:00Z","end":"2026-09-01T00:02:00Z",
                "period_seconds":60,"metrics":[metric],"quotas":[quota],
                "inference_profiles":[{"region":"us-east-1","inferenceProfileId":"us."+model,
                    "type":"SYSTEM_DEFINED","models":[{"modelArn":"arn:aws:bedrock:us-east-1::foundation-model/"+model}]}]}
        analyze(report)
        self.assertEqual(quota["comparison"]["peak_percent"],0.5)
        quota["name"]+=" 1M Context Length"
        analyze(report)
        self.assertEqual(quota["comparison"]["status"],"unmapped")
        self.assertNotIn("peak_percent",quota["comparison"])

    def test_only_observed_model_dimensions_expand_to_other_metrics(self):
        start=datetime(2026,9,1,tzinfo=UTC)
        c=Collector(None,args(),start,start+timedelta(minutes=5),60)
        for dims,points in [([{"Name":"ModelId","Value":"active"}],[["2026-09-01T00:00:00Z",1]]),
                            ([{"Name":"ModelId","Value":"inactive"}],[]),
                            ([],[["2026-09-01T00:00:00Z",1]])]:
            metric={"Namespace":"AWS/Bedrock","MetricName":"Invocations","Dimensions":dims}
            c.report["metrics"].append({"id":metric_id("us-east-1",metric),"region":"us-east-1",
                                       "metric":metric,"points":points})
        extra=c.expand_observed("us-east-1")
        self.assertTrue(extra)
        self.assertTrue(all(m["metric"]["Dimensions"]==[{"Name":"ModelId","Value":"active"}] for m in extra))


if __name__ == "__main__":
    unittest.main()
