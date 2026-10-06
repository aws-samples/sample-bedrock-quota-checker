# Bedrock Quota Checker

Generate an **English, offline HTML report** of your AWS account's current Amazon Bedrock quotas, model availability, inference profiles, and historical usage. Export the quota inventory as **`quotas.csv`**, with JSON and additional CSV files for further analysis.

Run the Python collector in **AWS CloudShell** or on your **local computer**. No application deployment or AWS infrastructure setup is required.

The collector uses an explicit allowlist of read-only AWS operations. It does not invoke models, modify resources, enable logging, or request quota increases. It reads service metadata and aggregate metrics, not prompts or model responses. Credentials remain in your environment, and reports are not uploaded automatically.

**Cost:** running the collector does not generate inference charges. CloudWatch metric retrieval can incur API charges. Start with the Regions you use and the default 14-day window, or use `--skip-usage` for inventory and quotas only.

## What you get

| Output | What it contains |
|---|---|
| **`report.html`** | Offline dashboard with charts, filters, current quotas, reported model availability, and collection quality |
| **`quotas.csv`** | Current applied quotas and AWS defaults, stored separately, with quota codes, units, scope, and collection timestamps |
| `report.json` | Complete structured report, including metric series and interpretation limits |
| ZIP archive | All report files together, ready to download or share |

All application-generated interface text, CLI messages, explanations, and documentation are in English. The HTML uses US English number formatting and UTC dates. Names and identifiers returned by AWS are preserved as received.

## Prerequisites

- Git and **Python 3.10 or newer**.
- AWS credentials for the account you want to inspect.
- The read permissions in [permissions/collector-read-only.json](permissions/collector-read-only.json).
- Network access to GitHub, the Python package index, and the AWS APIs for your selected Regions.

The commands below assume a Bash or Zsh terminal. CloudShell provides a browser-based terminal and temporary credentials from your AWS console session. For local execution, use your existing AWS profile or IAM Identity Center session.

## Two ways to run

You can generate the report in either of two ways:

- **Automatic (recommended):** run [`run.sh`](run.sh). It checks Python, creates the virtual environment, installs the dependencies, and generates the report in one step.
- **Manual:** follow the numbered steps below to run each command yourself. Use this if you want full control over each stage or cannot run the script.

Both approaches produce the same outputs.

### Automatic: `run.sh`

After cloning the repository (step 1 below), make the script executable, then run it:

```bash
chmod ugo+x run.sh
./run.sh
```

With no arguments, the script uses the configured AWS Region and defaults to `--days 14 --output-dir ./reports`. If no Region is configured, supply `--regions`. Use `--all-enabled-regions` explicitly for broader discovery; it requires `ec2:DescribeRegions`. Arguments are forwarded directly to the collector and replace the script defaults:

```bash
# Inventory and quotas only
./run.sh --regions us-east-1 --skip-usage

# Specific profile, custom Regions and window
./run.sh --profile customer-readonly --regions us-east-1 us-west-2 --days 30

# Preview the collection plan
./run.sh --regions us-east-1 --days 14 --plan
```

Optional environment overrides: `PYTHON` (interpreter, default `python3`) and `VENV` (virtual environment directory, default `.venv`). For example: `PYTHON=python3.11 ./run.sh`.

On CloudShell you do not need `--profile`; the script inherits your console-session credentials. When the run finishes, the collector prints the absolute paths of the generated `report.html`, `quotas.csv`, JSON, and ZIP — see [step 4](#4-open-or-download-the-html-and-quota-file) to open or download them.

To perform the steps yourself instead, continue with the manual instructions below.

## Manual steps

## 1. Open your terminal and clone the repository

**CloudShell:** sign in to the intended AWS account, open [AWS CloudShell](https://console.aws.amazon.com/cloudshell/), and wait for the terminal to start.

**Local computer:** open your terminal.

Run these commands in either environment:

```bash
git clone https://github.com/aws-samples/sample-bedrock-quota-checker.git
cd sample-bedrock-quota-checker
python3 --version
```

If `python3` is older than 3.10, use a Python 3.10+ interpreter for all Python commands below. If your CloudShell environment does not provide one, use the local-computer option with a supported Python installation.

## 2. Install the dependencies

Create a virtual environment and install the tested SDK version:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Check the collector and SDK:

```bash
python3 bedrock_access_report.py --version
python3 -c "import boto3; print(boto3.__version__)"
```

The collector does not install or upgrade packages during execution.

## 3. Generate the report

### AWS CloudShell

Use the credentials from your console session. List the Regions where your applications send Bedrock requests:

```bash
python3 bedrock_access_report.py \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

You do not need `--profile` in CloudShell. Replace the example Regions with the ones you use. For cross-Region inference, include the **source Region where the application sends the request**.

### Local computer with the default AWS profile

If your `default` profile is already authenticated:

```bash
python3 bedrock_access_report.py \
  --profile default \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

For an existing IAM Identity Center profile, refresh the session and use that profile instead:

```bash
aws sso login --profile customer-readonly
python3 bedrock_access_report.py \
  --profile customer-readonly \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

If you omit `--regions`, the collector uses the Region configured in your environment or profile. If no Region is configured, it asks you to supply one. If you omit `--profile`, boto3 uses its standard credential chain.

The collector prints progress and the **absolute paths** of the generated HTML, quota CSV, JSON, and ZIP. Runtime depends on the selected Regions, metric series, and API retries. Isolated collection failures appear in the report instead of silently becoming zero usage.

## 4. Open or download the HTML and quota file

Each execution creates its own directory and ZIP archive:

```text
reports/
  bedrock-report_<account-id>_<UTC-timestamp>/
    report.html
    quotas.csv
    report.json
    models.csv
    inference_profiles.csv
    provisioned_throughput.csv
    usage_summary.csv
    usage_timeseries.csv
    collection_issues.csv
  bedrock-report_<account-id>_<UTC-timestamp>.zip
```

### Download from CloudShell

1. In the terminal output, find the path printed after **`HTML:`**.
2. Choose **Actions → Download file**, paste that exact path, and download **`report.html`**.
3. Repeat with the path printed after **`QUOTAS CSV:`** to download **`quotas.csv`**.
4. Open `report.html` in your browser. Open `quotas.csv` in Excel, another spreadsheet application, or a text editor.

To download everything at once, use **Actions → Download file** with the **`ZIP:`** path, then extract the archive on your computer. Keeping the files together also preserves the HTML dashboard's links to the CSV and JSON exports.

### Open files generated on your local computer

The files are already on your computer. Open the output directory printed by the collector and double-click **`report.html`**. The dashboard runs offline and requires no AWS credentials or web server. The **`quotas.csv`** file is in the same directory.

Review the files before sharing them: they can contain account IDs, resource identifiers, quotas, and operational usage. Nothing is sent automatically.

## Common commands

### Inventory and quotas only

Generate HTML, CSV, and JSON without CloudWatch usage queries:

```bash
python3 bedrock_access_report.py --regions us-east-1 --skip-usage
```

### Preview the collection plan

Read inventory and metric identities without retrieving usage datapoints:

```bash
python3 bedrock_access_report.py --regions us-east-1 --days 14 --plan
```

The plan estimates the minimum number of daily-window requests, before additional pagination. Diagnostics take priority over tokens and activity, across all selected Regions. The newest daily windows are queried first. Increasing collection budgets can increase CloudWatch API charges.

### Discover several Regions in parallel

The collector inventories and discovers up to 4 Regions concurrently. History retrieval then runs in diagnostic priority order, rotating Regions within each daily window. The global collection budgets (`--max-metrics`, `--max-datapoints`, `--max-metric-requests`) apply to each attempt across all Regions.

```bash
python3 bedrock_access_report.py --all-enabled-regions --region-workers 6
```

Use `--region-workers 1` to serialize inventory/discovery. Increasing worker count does not increase the history budgets.

### Resume an incomplete report

```bash
python3 bedrock_access_report.py \
  --resume ./reports/YOUR_REPORT_DIRECTORY/report.json \
  --max-metric-requests 200 \
  --output-dir ./reports
```

Resume uses AWS read APIs with the original account, Regions, time range and resolution. It preserves the inventory and quota snapshot timestamps, skips completed series/windows, retries incomplete windows and includes deferred metric identities. It writes a **new report directory**, preserving the source report. Each attempt gets a fresh collection budget; earlier issues are retained in `previous_attempts`.

Reports from v0.1.2 have no completed-window checkpoints. Their incomplete series are queried again over the original window, with timestamp deduplication. Resume is rejected if CloudWatch retention no longer supports the saved resolution; collect a new report at a supported resolution instead. Controlled budget stops are resumable; this is not a checkpoint guarantee for a killed process.

`--resume` does not refresh quotas or discover new resources. To update those, start a new collection.

### Include inactive models or administrative API usage

By default, history collection targets metric identities discovered by CloudWatch and explicit `--model-ids`. Runtime diagnostics are expanded before history retrieval, including account-level error/throttle queries in Regions with discovered Runtime metrics.

```bash
python3 bedrock_access_report.py \
  --regions us-east-1 --days 14 \
  --include-inactive-models --include-api-usage
```

`--include-inactive-models` also probes every catalog model/profile for historical activity. `--include-api-usage` includes administrative `AWS/Usage` metrics referenced by Service Quotas. Both options increase workload. `--model-ids` adds explicit Runtime identities and is **not** an exclusive filter.

### View a 30-day trend

```bash
python3 bedrock_access_report.py --regions us-east-1 --days 30 --period auto
```

A 14-day report uses one-minute intervals. A 30-day report uses five-minute intervals; its per-minute rates are averages within those intervals, not reconstructed one-minute peaks. Older data requires coarser resolution.

### Regenerate a saved report without contacting AWS

Replace the example path with a `report.json` file from a previous run:

```bash
python3 bedrock_access_report.py --render ./reports/YOUR_REPORT_DIRECTORY/report.json
```

This regenerates HTML, CSV, and ZIP outputs from the saved snapshot. It refreshes application-generated explanations in English while preserving the original collection timestamps and AWS data. It does not refresh quotas or usage from AWS.

### See all options

```bash
python3 bedrock_access_report.py --help
```

Additional options include explicit `--start` and `--end` timestamps, `--model-ids`, `--region-workers` for parallel discovery, and collection limits through `--max-metrics`, `--max-datapoints`, and `--max-metric-requests`. `--all-enabled-regions` additionally requires `ec2:DescribeRegions`.

## How to interpret the results

- **Catalog is not authorization.** Model listings and reported availability do not prove that your application has effective invocation permissions.
- **Applied quota is not the AWS default.** The report keeps both values. A missing applied value is not replaced with a default or zero.
- **The denominator is today's quota.** Historical usage is compared with the quota collected now, not necessarily the quota that applied at the time.
- **Token utilization is an estimate.** Cache accounting, output-token factors, and upfront `max_tokens` reservations affect quota consumption. A low estimate does not rule out throttling.
- **Missing data is not zero.** Retention, permissions, dimensions, discovery limits, and inactivity can all affect coverage.
- **Endpoints are separate.** `bedrock-runtime` and `bedrock-mantle` have separate metrics and quota allocations.
- **Collection limits are not inference failures.** `request_budget_exhausted`, `datapoint_budget_exhausted` and `series_budget_exhausted` describe local collection limits. Incomplete series never receive a utilization percentage. Review coverage and resume before drawing conclusions.
- **Some comparisons show `N/A`.** Validated Runtime mappings include US Opus 4.7 and Haiku 4.5, US/global Opus 4.8, Opus 5, Opus 5.5, Sonnet 5, GPT-6 Astra, and global Haiku 4.5. Mantle input/output mappings cover GPT-5.4, GPT-5.5, and GPT-5.6 Luna/Terra/Sol. Exact codes, names, scope and model identities must match. Unverified relationships remain unmapped.
- **Daily quotas remain visible.** The report shows all quota types by default and highlights `Cross-Model Max Tokens Per Day`. Its pricing-based accounting cannot be reconstructed from raw token sums.
- **Mantle HTTP 429 needs application evidence.** `InferenceClientErrors` excludes requests rejected before processing. Record HTTP status, error code, timestamp, model, Region, endpoint and request ID in application logs; aggregate token metrics cannot prove absence of throttling.

See the [customer guide](docs/customer-guide.md) for permission details, metric limitations, and troubleshooting.

## Development and verification

See [security finding remediation](docs/security-findings.md) for the CSP design, the pagination-field B107 annotation, and verification details.

Run the local unit tests without credentials or AWS calls:

```bash
python3 -m unittest discover -s tests -v
```

Generated reports, virtual environments, and local tool caches are excluded from Git.
