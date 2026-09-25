# How to run the Bedrock Quotas & Usage Report

> **Version 0.1.2 — English report and CLI.** Read-only collection has been validated in `us-east-1`. Other accounts and Regions can expose different catalogs, quotas, permissions, and metrics; collection gaps are reported explicitly.

Generate a report of your account's current Amazon Bedrock quotas, reported model availability, inference profiles, and historical usage. Clone the repository, run the collector in your own AWS account, then download **`report.html`** and **`quotas.csv`**.

Choose either CloudShell or your local terminal below. Both produce the same output formats. The HTML interface, CLI messages, and application-generated explanations are always in English.

The collector uses read-only AWS API operations. It does not invoke models, subscribe to models, request quota increases, enable logging, or change resources. It collects service metadata and aggregate metrics, not prompts or model responses.

**Cost:** the collector does not generate inference charges. CloudWatch metric retrieval, including `GetMetricData`, can incur API charges. Start with the Regions you use and a 14-day window. Use `--skip-usage` if you only need inventory and quotas.

**Data handling:** credentials stay in your AWS environment. The report remains in CloudShell until you download it. No report is uploaded or emailed automatically.

## Option A — AWS CloudShell

### 1. Sign in to the account you want to inspect

Open the [AWS Console](https://console.aws.amazon.com/) and select the appropriate account and role. The role needs permission to open CloudShell and to perform the read operations listed below.

### 2. Open CloudShell

Choose the terminal icon in the console navigation bar, or search for **CloudShell**. Wait for the terminal to become available.

The collector can query multiple Regions from one CloudShell session, subject to your account permissions, Region availability, and network connectivity.

### 3. Clone the repository and install dependencies

Run:

```bash
git clone https://github.com/dcabib/bedrock-quota-checker.git
cd bedrock-quota-checker
python3 --version
```

The tested dependencies require **Python 3.10 or newer**. If `python3` is older, use a supported Python interpreter for the commands below or follow the local-terminal option.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 bedrock_access_report.py --version
python3 -c "import boto3; print(boto3.__version__)"
```

CloudShell provides temporary AWS credentials from your console session. The collector uses those credentials without a `--profile` argument.

### 4. Run the report

List the Regions where your applications send Bedrock requests:

```bash
python3 bedrock_access_report.py \
  --regions us-east-1 us-west-2 sa-east-1 \
  --days 14 \
  --output-dir ./reports
```

Replace the example Regions with your own. For cross-Region inference, include the **source Region where the application sends its request**.

Other commands:

```bash
# Use the Region configured in your environment.
python3 bedrock_access_report.py

# Inspect the collection plan before retrieving metric datapoints.
python3 bedrock_access_report.py \
  --regions us-east-1 us-west-2 --days 14 --plan

# Collect a 30-day trend using an appropriate metric resolution.
python3 bedrock_access_report.py \
  --regions us-east-1 us-west-2 --days 30 --period auto

# Collect inventory and quotas without CloudWatch usage queries.
python3 bedrock_access_report.py \
  --regions us-east-1 us-west-2 --skip-usage
```

Runtime depends on the number of Regions, models, metric series, and API retries. The collector prints progress and the absolute output paths. An isolated permission error produces a partial report with an explanation.

### 5. Download and view the results

Each execution creates a new directory and a ZIP under the selected output directory:

```text
reports/bedrock-report_<account-id>_<UTC-timestamp>/
reports/bedrock-report_<account-id>_<UTC-timestamp>.zip
```

To download the two primary files:

1. Find the absolute path printed after **`HTML:`**.
2. In CloudShell, choose **Actions → Download file**, paste that path, and download `report.html`.
3. Repeat with the path after **`QUOTAS CSV:`** to download `quotas.csv`.
4. Open the HTML in your browser and the CSV in your preferred spreadsheet application.

Alternatively, use **Actions → Download file** with the exact **`ZIP:`** path. Extract the archive on your computer and open `report.html`. Keeping the files together preserves the dashboard's links to the CSV and JSON exports.

The HTML report works offline, without credentials, external chart libraries, or an additional installation.

| File | Purpose |
|---|---|
| `report.html` | Interactive report with inventory, quotas, usage charts, and collection issues |
| `report.json` | Complete structured report, including sources, timestamps, and limitations |
| `models.csv` | Catalog and reported model availability |
| `inference_profiles.csv` | System-defined and application inference profiles |
| `provisioned_throughput.csv` | Existing provisioned resources and allocated/desired model units |
| `quotas.csv` | Applied quotas and AWS defaults, kept separate |
| `usage_summary.csv` | Usage totals, interval statistics, and supported quota comparisons |
| `usage_timeseries.csv` | Timestamped metric data with dimensions and resolution |
| `collection_issues.csv` | Missing permissions, unavailable data, and other collection problems |

The default output location is relative to the folder where you run the command; it is not necessarily your CloudShell home directory.

### 6. Share only if needed

Review the report before sharing it. Files can contain your account ID, resource identifiers, quotas, and operational usage.

If your AWS contact needs to review the findings, send the files you choose through your organization's approved channel. Credentials, prompts, and model responses are not needed.

## Option B — Your own terminal

Use Git, Python 3.10+, and your existing AWS credentials. Clone the repository and install its dependencies in a virtual environment:

```bash
git clone https://github.com/dcabib/bedrock-quota-checker.git
cd bedrock-quota-checker
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If your `default` profile is already authenticated:

```bash
python3 bedrock_access_report.py \
  --profile default \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

Use your organization's existing temporary-credential or IAM Identity Center flow. For an already configured AWS CLI SSO profile:

```bash
aws sso login --profile customer-readonly
python3 bedrock_access_report.py \
  --profile customer-readonly \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

If the profile is not configured yet, follow your organization's AWS CLI setup instructions. The collector also supports the standard boto3 credential chain when you omit `--profile`.

Open `report.html` directly from the generated folder. The `quotas.csv` file is in the same folder. No CloudShell download is needed for a local run.

## Permissions

Ask your AWS administrator to review the [collector policy](../permissions/collector-read-only.json) against your organization's requirements. These are the read operations in the policy:

```text
bedrock:ListFoundationModels
bedrock:GetFoundationModelAvailability
bedrock:ListInferenceProfiles
bedrock:ListProvisionedModelThroughputs
servicequotas:ListServiceQuotas
servicequotas:ListAWSDefaultServiceQuotas
cloudwatch:ListMetrics
cloudwatch:GetMetricData
sts:GetCallerIdentity
```

`sts:GetCallerIdentity` does not require an explicit permission grant. CloudShell access requires separate permissions. If using the optional `--all-enabled-regions` mode, the collector also needs `ec2:DescribeRegions`; explicit `--regions` avoids that dependency.

You do not need to grant broad `ReadOnlyAccess`, inference permissions, or AWS Marketplace subscription permissions solely for this report. Existing policies may cover the required operations, but organization policies, permission boundaries, and explicit denies can still restrict access.

The collector does not attach or modify IAM policies.

## How to interpret the report

**Availability is not an invocation test.** The model catalog describes supported models and capabilities. Availability checks report the service's access-related states when supported. Actual application requests can still be restricted by IAM, SCPs, endpoint policies, agreements, or provider prerequisites. The collector does not invoke a model to test access.

**Applied quotas and defaults are different.** An applied quota is the account-specific value returned by the API. An AWS default is shown separately. If the applied value is unavailable, the report says so and does not silently treat the default as your confirmed limit.

**Historical usage is compared with today's quota.** The report captures quotas at collection time. It cannot establish which quota applied throughout the past unless a separate quota history is available.

**Granularity affects peaks.** A 14-day report uses one-minute periods. A 30-day report uses five-minute periods; its per-minute rates are averages within each five-minute interval. Older data is available at coarser resolution. These averages can hide short bursts.

**Token usage is not exact quota occupancy.** Runtime token quotas can account for cache writes, model-specific output-token factors, and upfront token reservations. Estimated utilization is labeled accordingly. Throttling can occur even when an estimated percentage is below 100%.

**Endpoints have separate quotas and metrics.** `bedrock-runtime` and `bedrock-mantle` are shown separately. Mantle input/output token quotas are separate from runtime quotas. Missing or unverified quota-to-metric relationships appear as `N/A`.

**Quota correlations are deliberately limited in this release.** The collector uses compatible Service Quotas usage metadata and two explicit mappings for the US Claude Opus 4.7 and Haiku 4.5 profiles. Percentages describe the observed series, not guaranteed coverage of all traffic sharing a quota.

**No data is not zero usage.** Missing datapoints can reflect inactivity, unavailable metrics, retention, permissions, discovery limits, or a different Region/dimension. The report preserves those limitations.

**Provisioned capacity is separate.** Existing Model Units describe allocated resources. A quota on Model Units describes an allocation limit. Neither is automatically converted into on-demand RPM or TPM.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Python cannot import boto3 | Check the environment and follow the release's dependency instructions. |
| The SDK does not recognize an operation | Update to a release-compatible boto3 version; the collector should identify the unsupported operation. |
| Credentials missing or expired | Refresh your SSO session or reopen CloudShell with the intended role. |
| Region not configured | Supply explicit `--regions` values. |
| `AccessDenied` | Review the exact failed action and Region in `collection_issues.csv` with your administrator. A failed read is not proof that the model itself is inaccessible. |
| Endpoint or connection error | Check Region support, SDK version, account Region status, and network access. The error alone does not establish which is responsible. |
| No metrics returned | Check the source Region, endpoint, model/profile identifier, time range, permissions, and available dimensions. |
| Old model missing from discovery | CloudWatch `ListMetrics` omits metrics inactive for two weeks. Known identifiers may still permit direct history queries within retention. |
| Default quota shown without applied quota | The API may not expose an applied value for that quota. The report should preserve this distinction. |
| Throttles with apparently low utilization | Review estimation limits, reservations, bursts, inference mode, and other service constraints before attributing the cause. |
| Expected model is missing or unavailable | Consult the current model catalog and [model access documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html). Access workflows vary by provider, endpoint, and AWS partition. |

Useful references: [Bedrock runtime metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html), [token quota accounting](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html), [CloudWatch retention and retrieval](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_GetMetricData.html), and [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).
