import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest

from bedrock_access_report import render


class ReportMarkup(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=False)
        self.policy = {}
        self.blocks = []
        self.attributes = []
        self.active = None
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        self.attributes.append((tag, attributes))
        if tag == "meta" and attributes.get("http-equiv") == "Content-Security-Policy":
            self.policy = {
                parts[0]: parts[1:]
                for directive in attributes["content"].split(";")
                if (parts := directive.split())
            }
        if tag in ("script", "style"):
            self.active = {"tag": tag, "attributes": attributes, "text": ""}

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"] += data

    def handle_endtag(self, tag):
        if self.active is not None and tag == self.active["tag"]:
            self.blocks.append(self.active)
            self.active = None


class ReportSecurityTests(unittest.TestCase):
    def rendered(self, report):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            render(report, path)
            return path.read_text(encoding="utf-8")

    def test_csp_authorizes_only_the_emitted_script_and_style_hashes(self):
        markup = ReportMarkup(self.rendered({"account_id": "test-account"}))
        for tag, expected_count in [("script", 2), ("style", 1)]:
            blocks = [block for block in markup.blocks if block["tag"] == tag]
            self.assertEqual(len(blocks), expected_count)
            expected = [
                "'sha256-" + base64.b64encode(
                    hashlib.sha256(block["text"].encode("utf-8")).digest()
                ).decode("ascii") + "'"
                for block in blocks
            ]
            self.assertCountEqual(markup.policy[f"{tag}-src"], expected)
            self.assertEqual(markup.policy[f"{tag}-src-attr"], ["'none'"])
        for directive in ("default-src", "connect-src", "base-uri", "form-action"):
            self.assertEqual(markup.policy[directive], ["'none'"])
        self.assertEqual(markup.policy["img-src"], ["data:"])

    def test_report_data_is_inert_and_dynamic_hashes_preserve_exact_values(self):
        first = {"name": "Plain model name"}
        second = {
            "name": '</script><script>globalThis.reportInjected = true</script>',
            "unicode": "Model \u00e9 \u65e5\u672c\u8a9e \u2028 \u2029",
            "whitespace": "\r\n\t ",
            "placeholders": "__REPORT_CSP__ __REPORT_JSON__",
            "markup": '<img src=x onerror="globalThis.reportInjected=true">',
        }
        pages = [ReportMarkup(self.rendered(report)) for report in (first, second)]
        for report, page in zip((first, second), pages):
            self.assertEqual(len(page.blocks), 3)
            data = next(block for block in page.blocks if block["attributes"].get("id") == "report-data")
            self.assertEqual(data["attributes"]["type"], "application/json")
            self.assertEqual(json.loads(data["text"]), report)
            self.assertNotIn("<", data["text"])
        self.assertEqual(pages[0].policy["style-src"], pages[1].policy["style-src"])
        # Only the data block changes; the trusted application script stays fixed.
        self.assertEqual(len(set(pages[0].policy["script-src"]) & set(pages[1].policy["script-src"])), 1)
        self.assertEqual(pages[0].blocks[-1]["text"], pages[1].blocks[-1]["text"])

    def test_markup_and_generated_templates_do_not_use_inline_style_attributes(self):
        document = self.rendered({})
        markup = ReportMarkup(document)
        for tag, attributes in markup.attributes:
            self.assertNotIn("style", attributes, tag)
            self.assertFalse(any(name.startswith("on") for name in attributes), tag)
        # Also cover HTML snippets embedded in the application's JavaScript.
        self.assertIsNone(re.search(r"\bstyle\s*=", document, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
