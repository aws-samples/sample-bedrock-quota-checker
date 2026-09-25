# Project conventions

- Write all source code, comments, docstrings, documentation, CLI messages, and report interface text in English.
- Keep the generated HTML interface in English regardless of the browser or operating system locale. Use `en-US` number formatting and UTC dates.
- Preserve AWS resource names, identifiers, raw service responses, and collection timestamps as received. Do not translate customer data.
- When regenerating a saved report, refresh application-owned explanations in English and record the renderer version without changing the original collector version or collection timestamps.
- Keep the README and customer guide as a single AWS CloudShell walkthrough: open CloudShell, clone the repository there, install dependencies there, run the collector there, and download `report.html` and `quotas.csv`. Do not add a separate local-terminal execution workflow.
- The collector must use only its explicit read-only AWS API allowlist. Do not invoke models or modify AWS resources.
- Never commit generated customer reports, credentials, local virtual environments, or tool caches.
- Update `bedrock_access_report.py.sha256` whenever the collector source changes.
