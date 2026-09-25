# Security finding remediation

## B107: pagination field default

`Collector.listing()` uses `token_key="nextToken"` to name an AWS pagination response field. The default is a field name, not a password, credential, or pagination token value.

The function declaration carries the narrowly scoped annotation:

```python
# nosec B107 - Pagination field name, not a credential.
```

Other Bandit rules remain enabled. Pagination behavior is unchanged.

## Content Security Policy: inline scripts and styles

Version 0.1.2 removes `unsafe-inline` from the generated HTML policy. The renderer computes SHA-256 hashes from the exact contents of each script and stylesheet after serializing and escaping the report data.

- `script-src` and `style-src` authorize only the generated block hashes.
- The inert JSON data block is hashed for each report. Dynamic data does not require a fixed hash across reports.
- `script-src-attr 'none'` and `style-src-attr 'none'` block inline event-handler and style attributes. Typography and legend colors use the hashed stylesheet.
- `default-src 'none'`, `connect-src 'none'`, `base-uri 'none'`, and `form-action 'none'` remain in place.
- The report remains a single offline HTML file. No external JavaScript, CSS, or web server is required.

The renderer continues escaping `<` in embedded JSON, and the application escapes data before inserting it into HTML. Resource names and other report data are not assumed to be trusted merely because the report is opened through `file://`.

This is a code remediation, not a request to accept the original broad inline policy. A CSP does not authenticate the entire file against an attacker who can rewrite both its content and policy.

## Verification

Run the standard-library regression tests without AWS credentials:

```bash
python -m unittest discover -s tests -v
```

The security tests validate hashes against parsed HTML, preserve Unicode and dynamic report data, exercise attempted script-element termination, and reject inline style and event-handler attributes.

Run Bandit in a development environment where it is installed:

```bash
python -m bandit bedrock_access_report.py
```

Verification for this change:

- All 18 unit tests passed.
- Bandit 1.9.4 reported no findings for the collector, with the reviewed B107 annotation applied.
- Chromium 153 opened a synthetic report through `file://` without CSP violations. Charts, legend colors, tooltips, navigation, filters, mobile layout, and export link targets were checked.
- Browser probes confirmed that unauthorized script and style blocks, event-handler and style attributes, and a network request were blocked. Modifying the application script without updating its hash also prevented execution.

Previously generated HTML files retain their original policy; regenerate them with version 0.1.2 or newer using `--render`. No PCSR acceptance or scanner disposition is implied by this implementation record.
