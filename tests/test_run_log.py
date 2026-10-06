import csv
import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock

from bedrock_access_report import (
    LOG_FIELDS, RunLog, dump_run_log, export, log_text, render,
)

from test_diagnostic_collection import collector, series, snapshot


def events(report_or_log):
    if isinstance(report_or_log, RunLog):
        return report_or_log.snapshot()["events"]
    return (report_or_log.get("run_log") or {}).get("events", [])


def find(collection, **match):
    return [e for e in collection if all(e.get(k) == v for k, v in match.items())]


def quiet(**options):
    """A collector whose log does not print, so tests keep a clean output."""
    result = collector(**options)
    result.log = RunLog(echo=False, level=options.get("log_level", "INFO"))
    return result


def window_response(*args, **query):
    return {"MetricDataResults": [
        {"Id": m["Id"], "StatusCode": "Complete",
         "Timestamps": [query["StartTime"]], "Values": [5.0]}
        for m in query["MetricDataQueries"]
    ]}


class RunLogTests(TestCase):
    def test_levels_below_the_threshold_stay_in_the_csv_but_not_the_transcript(self):
        log = RunLog(echo=False, level="INFO")
        log.add("DEBUG", "inventory", "bedrock.list_foundation_models", "us-east-1", "ok", "131 models")
        log.add("WARN", "history", "cloudwatch.get_metric_data", "us-east-1", "service_message", "Query limit")
        snap = log.snapshot()
        self.assertEqual([e["level"] for e in snap["events"]], ["DEBUG", "WARN"])
        self.assertEqual(len(snap["transcript"]), 1)
        self.assertIn("Query limit", snap["transcript"][0])
        self.assertNotIn("131 models", "\n".join(snap["transcript"]))

    def test_sequence_and_attempt_are_stable_and_counts_are_tracked(self):
        log = RunLog(echo=False)
        for index in range(3):
            log.add("INFO", "history", "op", detail=f"event {index}")
        snap = log.snapshot()
        self.assertEqual([e["seq"] for e in snap["events"]], [1, 2, 3])
        self.assertEqual({e["attempt"] for e in snap["events"]}, {1})
        self.assertEqual(snap["levels"], {"INFO": 3})

    def test_every_event_field_is_declared_as_a_csv_column(self):
        log = RunLog(echo=False)
        log.add("WARN", "history", "cloudwatch.get_metric_data", "us-east-1", "service_message", "detail",
                duration_ms=12.5, batch_size=50, window_start="a", window_end="b",
                pages=3, points=99, requests_used=7, datapoints_used=1234)
        for event in log.snapshot()["events"]:
            self.assertEqual(set(event) - set(LOG_FIELDS), set())

    def test_optional_fields_are_omitted_when_none(self):
        log = RunLog(echo=False)
        log.add("INFO", "start", "op", detail="x", duration_ms=None, pages=4)
        event = log.snapshot()["events"][0]
        self.assertNotIn("duration_ms", event)
        self.assertEqual(event["pages"], 4)

    def test_restore_continues_the_saved_history_under_the_next_attempt(self):
        first = RunLog(echo=False)
        first.add("INFO", "start", "collection", detail="attempt one")
        saved = {"run_log": first.snapshot()}
        second = RunLog.restore(saved, echo=False)
        second.add("INFO", "start", "resume", detail="attempt two")
        snap = second.snapshot()
        self.assertEqual(snap["attempt"], 2)
        self.assertEqual([e["attempt"] for e in snap["events"]], [1, 2])
        self.assertEqual([e["seq"] for e in snap["events"]], [1, 2])
        self.assertEqual(len(snap["transcript"]), 2)

    def test_restore_of_a_report_without_a_log_starts_at_attempt_two(self):
        self.assertEqual(RunLog.restore({}, echo=False).attempt, 2)
        self.assertEqual(RunLog.restore(None, echo=False).attempt, 2)

    def test_transcript_text_ends_with_a_level_summary(self):
        log = RunLog(echo=False)
        log.add("ERROR", "history", "op", detail="broke")
        text = log_text(log.snapshot())
        self.assertTrue(text.endswith("\n"))
        self.assertIn("ERROR=1", text.splitlines()[-1])
        self.assertIn("1 attempt(s)", text.splitlines()[-1])

    def test_the_level_summary_covers_every_attempt_in_the_snapshot(self):
        first = RunLog(echo=False)
        first.add("ERROR", "history", "op", detail="attempt one failed")
        second = RunLog.restore({"run_log": first.snapshot()}, echo=False)
        second.add("INFO", "finish", "op", detail="attempt two finished")
        footer = log_text(second.snapshot()).splitlines()[-1]
        self.assertIn("2 attempt(s)", footer)
        self.assertIn("INFO=1", footer)
        self.assertIn("ERROR=1", footer)

    def test_transcript_text_tolerates_a_report_without_a_log(self):
        self.assertEqual(log_text(None), "")
        self.assertEqual(log_text({}), "")


class InstrumentationTests(TestCase):
    def test_every_aws_call_is_logged_with_its_duration(self):
        c = quiet()
        c.client = Mock(return_value=Mock(get_caller_identity=Mock(
            return_value={"Account": "1", "Arn": "arn:aws:iam::1:role/r"})))
        c.call("sts", "us-east-1", "get_caller_identity")
        logged = find(events(c.log), operation="sts.get_caller_identity")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["status"], "ok")
        self.assertEqual(logged[0]["level"], "DEBUG")
        self.assertGreaterEqual(logged[0]["duration_ms"], 0)

    def test_a_failed_call_is_logged_once_with_the_error_code(self):
        c = quiet()
        failure = Exception("boom")
        failure.response = {"Error": {"Code": "AccessDeniedException", "Message": "no"}}
        c.client = Mock(return_value=Mock(get_caller_identity=Mock(side_effect=failure)))
        self.assertIsNone(c.call("sts", "us-east-1", "get_caller_identity"))
        logged = find(events(c.log), operation="sts.get_caller_identity")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["level"], "WARN")
        self.assertEqual(logged[0]["status"], "access_denied")
        self.assertIn("AccessDeniedException", logged[0]["detail"])
        # The canonical issue list is unchanged by logging.
        self.assertEqual(len(c.report["collection_issues"]), 1)

    def test_phase_is_recorded_per_thread_and_restored_on_exit(self):
        c = quiet()
        self.assertEqual(c.phase, "run")
        with c.in_phase("inventory"):
            self.assertEqual(c.phase, "inventory")
            with c.in_phase("history"):
                self.assertEqual(c.phase, "history")
            self.assertEqual(c.phase, "inventory")
        self.assertEqual(c.phase, "run")

    def test_a_request_scoped_message_names_the_batch_it_poisoned(self):
        batch = [series("InvocationThrottles", model=f"m{i}") for i in range(3)]

        def response(*args, **query):
            return {
                "Messages": [{"Code": "MaxMetricsExceeded", "Value": "Query limit reached"}],
                "MetricDataResults": [
                    {"Id": m["Id"], "StatusCode": "Complete",
                     "Timestamps": [query["StartTime"]], "Values": [1.0]}
                    for m in query["MetricDataQueries"]
                ],
            }

        c = quiet()
        c.call = Mock(side_effect=response)
        c.fetch_scheduled(batch)
        message = find(events(c.log), status="service_message")
        self.assertTrue(message)
        self.assertEqual(message[0]["batch_size"], 3)
        self.assertIn("MaxMetricsExceeded", message[0]["detail"])
        # The batch summary keeps the statuses CloudWatch actually returned, so a
        # poisoned batch is distinguishable from series that really failed.
        summary = [e for e in events(c.log) if e["operation"] == "get_metric_data" and e.get("points") is not None]
        self.assertTrue(summary)
        self.assertIn("Complete", summary[-1]["detail"])
        self.assertEqual(summary[-1]["level"], "WARN")

    def test_missing_results_are_counted_in_the_batch_summary(self):
        batch = [series("InvocationThrottles", model=f"m{i}") for i in range(3)]

        def partial(*args, **query):
            # CloudWatch omits the tail of the batch, exactly how diagnostic
            # series disappear when a request overflows MaxDatapoints.
            return {"MetricDataResults": [
                {"Id": query["MetricDataQueries"][0]["Id"], "StatusCode": "Complete",
                 "Timestamps": [query["StartTime"]], "Values": [2.0]},
            ]}

        c = quiet()
        c.call = Mock(side_effect=partial)
        c.fetch_scheduled(batch)
        summary = [e for e in events(c.log) if e["operation"] == "get_metric_data" and e.get("points") is not None]
        self.assertIn("MissingResult", summary[-1]["detail"])
        self.assertEqual(summary[-1]["level"], "WARN")
        self.assertEqual([m["status"] for m in batch].count("error"), 2)

    def test_budget_stop_is_logged_as_an_error_naming_the_affected_metrics(self):
        batch = [series("InvocationThrottles", model="m0"), series("Invocations", model="m0")]
        c = quiet(max_metric_requests=1)
        c.call = Mock(side_effect=window_response)
        c.fetch_scheduled(batch)
        stop = find(events(c.log), operation="budget_stop")
        self.assertTrue(stop)
        self.assertEqual(stop[0]["level"], "ERROR")
        self.assertEqual(stop[0]["status"], "request_budget_exhausted")
        self.assertEqual(stop[0]["requests_used"], 1)

    def test_priority_groups_are_announced_before_their_windows(self):
        batch = [series("InvocationThrottles", model="m0"), series("Invocations", model="m0")]
        c = quiet()
        c.call = Mock(side_effect=window_response)
        c.fetch_scheduled(batch)
        groups = find(events(c.log), operation="priority_group")
        self.assertEqual(len(groups), 2)
        self.assertIn("diagnostics", groups[0]["detail"])
        self.assertLess(groups[0]["seq"], groups[1]["seq"])

    def test_finish_attaches_the_log_to_the_report(self):
        c = quiet()
        c.report["metrics"] = []
        c.report["account_id"], c.report["partition"] = "1", "aws"
        report = c.finish()
        self.assertIn("run_log", report)
        self.assertTrue(find(events(report), operation="collection", phase="finish"))


class LogExportTests(TestCase):
    def report_with_log(self):
        metric = series("InvocationThrottles", model="m0")
        metric["status"] = "error"
        report = snapshot([metric])
        log = RunLog(echo=False)
        log.add("WARN", "history", "cloudwatch.get_metric_data", "us-east-1", "service_message",
                "Query limit reached", batch_size=50, pages=2, points=0, duration_ms=18.0)
        log.add("DEBUG", "inventory", "bedrock.list_foundation_models", "us-east-1", "ok", "131 rows")
        report["run_log"] = log.snapshot()
        report.update({"api_calls": {}, "returned_datapoints": 0, "collection_settings": {}})
        return report

    def exported(self, report):
        directory = Path(tempfile.mkdtemp())
        export(report, directory, collect_only=True)
        return directory

    def test_csv_and_transcript_are_written_next_to_the_data_exports(self):
        directory = self.exported(self.report_with_log())
        rows = list(csv.DictReader(io.StringIO((directory/"run_log.csv").read_text())))
        self.assertEqual(list(rows[0]), list(LOG_FIELDS))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "service_message")
        self.assertEqual(rows[0]["batch_size"], "50")
        # The DEBUG row is in the CSV but not in the readable transcript.
        self.assertEqual(rows[1]["level"], "DEBUG")
        transcript = (directory/"run_log.txt").read_text()
        self.assertIn("Query limit reached", transcript)
        self.assertNotIn("131 rows", transcript)

    def test_both_log_files_are_bundled_in_the_zip(self):
        directory = self.exported(self.report_with_log())
        archive = directory.with_suffix(".zip")
        import zipfile
        with zipfile.ZipFile(archive) as bundle:
            names = {Path(n).name for n in bundle.namelist()}
        self.assertLessEqual({"run_log.csv", "run_log.txt"}, names)

    def test_export_of_a_report_without_a_log_still_writes_empty_files(self):
        report = self.report_with_log()
        report.pop("run_log")
        directory = self.exported(report)
        self.assertEqual((directory/"run_log.txt").read_text(), "")
        rows = list(csv.DictReader(io.StringIO((directory/"run_log.csv").read_text())))
        self.assertEqual(rows, [])

    def test_dump_writes_the_history_of_a_run_that_produced_no_report(self):
        log = RunLog(echo=False)
        log.add("ERROR", "history", "collection", detail="Run did not finish: KeyboardInterrupt")
        with tempfile.TemporaryDirectory() as parent:
            path = dump_run_log(log.snapshot(), parent)
            self.assertTrue(path.exists())
            self.assertIn("KeyboardInterrupt", (path.parent/"run_log.txt").read_text())

    def test_the_log_survives_a_json_round_trip_for_render(self):
        report = self.report_with_log()
        restored = json.loads(json.dumps(report))
        directory = self.exported(restored)
        rows = list(csv.DictReader(io.StringIO((directory/"run_log.csv").read_text())))
        self.assertEqual(len(rows), 2)

    def test_the_html_renders_a_run_log_section_without_new_script_blocks(self):
        document = io.StringIO()
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent)/"report.html"
            render(self.report_with_log(), path)
            document = path.read_text()
        self.assertIn('data-tab="runlog"', document)
        self.assertIn('id="log-table"', document)
        self.assertEqual(document.count("<script"), 2)
        self.assertEqual(document.count("<style"), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
