# AWS CloudShell customer guide

Use the [README walkthrough](../README.md) to perform the complete workflow in **AWS CloudShell**:

1. Sign in to the intended AWS account and open CloudShell.
2. Confirm the account and role with `aws sts get-caller-identity`.
3. Run `git clone` in CloudShell and enter the project directory.
4. Install Python 3.11, create `.venv`, and install `requirements.txt` in CloudShell.
5. Run the collector in CloudShell to generate the reports.
6. Download `report.html` and `quotas.csv` using **Actions → Download file**, or download the ZIP containing all outputs.
7. Open the downloaded files to inspect the results.

Every shell command in this guide belongs in CloudShell, in `~/bedrock-quota-checker` with `.venv` activated. The HTML interface, CLI messages, and application-generated explanations are in English.

**Repository access:** the current AWS GitLab repository requires GitLab permission and a Midway-signed SSH identity usable in the CloudShell session. Console AWS credentials do not grant repository access, and this GitLab instance does not allow Git operations over HTTPS. External customers need an approved repository they can access; use that URL in the README's clone command. See the [GitLab access documentation](https://docs.hub.amazon.dev/docs/gitlab/index.html).

**Read-only collection:** the collector does not invoke models, subscribe to models, request quota increases, enable logging, or change AWS resources. It reads service metadata and aggregate metrics, not prompts or model responses. It generates no inference charges, but CloudWatch metric retrieval can incur API charges.

**Data handling:** credentials stay in the CloudShell environment. Reports remain there until you download them. No report is uploaded or emailed automatically. Review the contents before sharing them through your organization's approved channel.

## Files generated in CloudShell

Each execution creates a separate directory and ZIP under the selected output directory:

```text
reports/bedrock-report_<account-id>_<UTC-timestamp>/
reports/bedrock-report_<account-id>_<UTC-timestamp>.zip
```

Use the actual absolute paths printed after `HTML:`, `QUOTAS CSV:`, and `ZIP:` with **Actions → Download file**. The default output location is relative to the project directory where the command runs.

| File | Purpose |
|---|---|
| `report.html` | Offline dashboard with inventory, quotas, usage charts, and collection issues |
| `quotas.csv` | Applied quotas and AWS defaults, kept separate |
| `report.json` | Complete structured report, including sources, timestamps, and limitations |
| `models.csv` | Catalog and reported model availability |
| `inference_profiles.csv` | System-defined and application inference profiles |
| `provisioned_throughput.csv` | Existing provisioned resources and allocated/desired model units |
| `usage_summary.csv` | Usage totals, interval statistics, and supported quota comparisons |
| `usage_timeseries.csv` | Timestamped metric data with dimensions and resolution |
| `collection_issues.csv` | Missing permissions, unavailable data, and other collection problems |

The HTML works offline without credentials or external chart libraries. Extract the ZIP and keep the files together to preserve the dashboard's CSV and JSON download links.

## Additional commands in CloudShell

Read the inventory and metric identities before retrieving usage datapoints:

```bash
python bedrock_access_report.py --regions us-east-1 us-west-2 --days 14 --plan
```

This is a read-only discovery run. It estimates the initial metric query workload; identifiers with observed activity can require additional queries during a full run.

Regenerate files from a saved JSON report without contacting AWS. Replace the example directory below with the directory from a previous run:

```bash
python bedrock_access_report.py --render ./reports/YOUR_REPORT_DIRECTORY/report.json
```

This refreshes application-generated explanations in English while preserving the original collection timestamps and AWS data. Download the regenerated files using the paths printed by this command. It does not retrieve fresh quotas or usage.

Use `python bedrock_access_report.py --help` for all options, including explicit timestamps, known model identifiers, and collection limits.

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

**Quota correlations are deliberately limited in v0.1.1.** The collector uses compatible Service Quotas usage metadata and two explicit mappings for the US Claude Opus 4.7 and Haiku 4.5 profiles. Percentages describe the observed series, not guaranteed coverage of all traffic sharing a quota.

**No data is not zero usage.** Missing datapoints can reflect inactivity, unavailable metrics, retention, permissions, discovery limits, or a different Region/dimension. The report preserves those limitations.

**Provisioned capacity is separate.** Existing Model Units describe allocated resources. A quota on Model Units describes an allocation limit. Neither is automatically converted into on-demand RPM or TPM.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Python cannot import boto3 | Repeat README Step 4 in CloudShell and activate `.venv` before running the collector. |
| The SDK does not recognize an operation | Activate `.venv` and run `python -m pip install -r requirements.txt` from the CloudShell project directory. |
| Credentials missing or expired | Reopen CloudShell from the intended AWS console account and role, then run `aws sts get-caller-identity`. |
| Region not configured | Supply explicit `--regions` values. |
| `AccessDenied` | Review the exact failed action and Region in `collection_issues.csv` with your administrator. A failed read is not proof that the model itself is inaccessible. |
| Endpoint or connection error | Check Region support, SDK version, account Region status, and network access. The error alone does not establish which is responsible. |
| No metrics returned | Check the source Region, endpoint, model/profile identifier, time range, permissions, and available dimensions. |
| Old model missing from discovery | CloudWatch `ListMetrics` omits metrics inactive for two weeks. Known identifiers may still permit direct history queries within retention. |
| Default quota shown without applied quota | The API may not expose an applied value for that quota. The report should preserve this distinction. |
| Throttles with apparently low utilization | Review estimation limits, reservations, bursts, inference mode, and other service constraints before attributing the cause. |
| Expected model is missing or unavailable | Consult the current model catalog and [model access documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html). Access workflows vary by provider, endpoint, and AWS partition. |

Useful references: [Bedrock runtime metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html), [token quota accounting](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html), [CloudWatch retention and retrieval](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_GetMetricData.html), and [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).
