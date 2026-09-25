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
- Authorized AWS GitLab access with SSH authentication, plus network access to the Python package index and the AWS APIs for your selected Regions.

The commands below assume a Bash or Zsh terminal. CloudShell provides a browser-based terminal and temporary credentials from your AWS console session. For local execution, use your existing AWS profile or IAM Identity Center session.

This repository is hosted on AWS GitLab. AWS console credentials do not grant access to the repository. External customers need an approved source archive or a repository they can access. If cloning from CloudShell is unavailable, upload the source archive through **Actions → Upload file**, extract it, and run the installation commands from the extracted project directory.

## 1. Open your terminal and clone the repository

**CloudShell:** sign in to the intended AWS account, open [AWS CloudShell](https://console.aws.amazon.com/cloudshell/), and wait for the terminal to start.

**Local computer:** open your terminal.

Run these commands in either environment:

```bash
git clone git@ssh.gitlab.aws.dev:daniabib/bedrock-quota-checker.git
cd bedrock-quota-checker
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

The plan estimates the initial query workload. Identifiers with observed activity can require additional metric queries during a full run.

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

Additional options include explicit `--start` and `--end` timestamps, `--model-ids`, and collection limits through `--max-metrics`, `--max-datapoints`, and `--max-metric-requests`. `--all-enabled-regions` additionally requires `ec2:DescribeRegions`.

## How to interpret the results

- **Catalog is not authorization.** Model listings and reported availability do not prove that your application has effective invocation permissions.
- **Applied quota is not the AWS default.** The report keeps both values. A missing applied value is not replaced with a default or zero.
- **The denominator is today's quota.** Historical usage is compared with the quota collected now, not necessarily the quota that applied at the time.
- **Token utilization is an estimate.** Cache accounting, output-token factors, and upfront `max_tokens` reservations affect quota consumption. A low estimate does not rule out throttling.
- **Missing data is not zero.** Retention, permissions, dimensions, discovery limits, and inactivity can all affect coverage.
- **Endpoints are separate.** `bedrock-runtime` and `bedrock-mantle` have separate metrics and quota allocations.
- **Some comparisons show `N/A`.** This version uses compatible Service Quotas usage metadata and two explicit mappings for US Claude Opus 4.7 and Haiku 4.5 profiles. Other unverified relationships remain unmapped. Even a mapped percentage describes the observed series, not guaranteed coverage of all traffic sharing the quota.

See the [customer guide](docs/customer-guide.md) for permission details, metric limitations, and troubleshooting.

## Development and verification

Run the local unit tests without credentials or AWS calls:

```bash
python3 -m unittest discover -s tests -v
```

On CloudShell/Linux, verify the script checksum with:

```bash
sha256sum -c bedrock_access_report.py.sha256
```

On macOS, use `shasum -a 256 -c bedrock_access_report.py.sha256`.

Generated reports, virtual environments, and local tool caches are excluded from Git. Changes to the collector require updating its SHA-256 file before distribution.
