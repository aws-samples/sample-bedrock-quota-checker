# Security finding remediation

## B107: pagination field default

`Collector.listing()` uses `token_key="nextToken"` to name an AWS pagination response field. The default is a field name, not a password, credential, or pagination token value.

The function declaration carries the narrowly scoped annotation:

```python
# nosec B107 - Pagination field name, not a credential.
```

Other Bandit rules remain enabled. Pagination behavior is unchanged.

## Content Security Policy: inline scripts and styles

The generated report is a single self-contained HTML file opened over `file://`, with no web server, no network endpoints, and no user-controlled input reaching the DOM as markup. Its Content Security Policy keeps `script-src 'unsafe-inline'` and `style-src 'unsafe-inline'` because the inline application script and stylesheet are inherent to a self-contained dashboard.

- The report data is an inert `application/json` block, not executable script.
- `script-src-attr 'none'` and `style-src-attr 'none'` block inline event-handler and style attributes.
- `default-src 'none'`, `connect-src 'none'`, `base-uri 'none'`, and `form-action 'none'` remain in place, so there is no remote XSS surface.
- The report remains a single offline HTML file. No external JavaScript, CSS, or web server is required.

The renderer continues escaping `<` in embedded JSON, and the application escapes data before inserting it into HTML. Resource names and other report data are not assumed to be trusted merely because the report is opened through `file://`.

This inline policy is accepted by design for an offline, single-file report; the remaining directives keep the policy otherwise restrictive.

## Verification

Run the standard-library regression tests without AWS credentials:

```bash
python -m unittest discover -s tests -v
```

The security tests validate the generated policy against parsed HTML, preserve Unicode and dynamic report data, exercise attempted script-element termination, and reject inline style and event-handler attributes.

Run Bandit in a development environment where it is installed:

```bash
python -m bandit bedrock_access_report.py
```

No PCSR acceptance or scanner disposition is implied by this implementation record.
