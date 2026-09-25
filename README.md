# Bedrock Quota Checker — Run in AWS CloudShell

Generate a report of your AWS account's current Amazon Bedrock quotas, reported model availability, inference profiles, and historical usage.

**Follow every command below in the AWS CloudShell terminal.** You will clone the repository, install the dependencies, run the collector, and generate the files there. At the end, download **`report.html`** and **`quotas.csv`** to view the results in your browser or spreadsheet application.

The collector uses read-only AWS API operations. It does not invoke models, change resources, enable logging, or request quota increases. It reads service metadata and aggregate metrics, not prompts or model responses. It does not upload the report automatically.

**Cost:** no inference charges are generated. CloudWatch metric retrieval can incur API charges; start with the Regions you use and the default 14-day window.

## Before you start

- Sign in to the AWS account you want to inspect, using a role that can open CloudShell and perform the operations in the [collector read policy](permissions/collector-read-only.json).
- Make sure the repository is accessible from your CloudShell session. **The current AWS GitLab repository requires Amazon GitLab permission and a Midway-signed SSH identity usable in that session.** AWS console credentials do not authenticate GitLab. See the [GitLab access documentation](https://docs.hub.amazon.dev/docs/gitlab/index.html).
- For external customers, the repository owner must first provide an approved repository URL the customer can access from CloudShell. Substitute that URL in Step 3. The internal GitLab URL below is not a public customer distribution endpoint.

CloudShell includes Git and the AWS CLI. Step 4 installs the supported Python version and dependencies inside CloudShell.

## Step 1 — Open AWS CloudShell

1. Open the [AWS Management Console](https://console.aws.amazon.com/) and sign in to the account you want to report on.
2. Select a Region where CloudShell is available.
3. Choose the **CloudShell terminal icon** in the console navigation bar, or search for **CloudShell** and open it.
4. Wait until the terminal prompt appears. Use the default **Bash** shell for this walkthrough.

Keep this CloudShell terminal open for all the remaining commands. The collector can query multiple Regions from this session.

## Step 2 — Confirm the AWS account

Paste this read-only command into CloudShell:

```bash
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
```

Check that `Account` and `Arn` identify the intended account and role. If they do not, switch to the correct account or role in the AWS console and reopen CloudShell before continuing.

CloudShell supplies temporary AWS credentials from your console session. Use those credentials throughout this guide.

## Step 3 — Clone the repository inside CloudShell

In the same CloudShell terminal, run:

```bash
cd ~
git clone --branch main --single-branch \
  git@ssh.gitlab.aws.dev:daniabib/bedrock-quota-checker.git \
  bedrock-quota-checker
cd ~/bedrock-quota-checker
```

This creates the project directory in your CloudShell home directory and checks out `main`.

The command assumes the GitLab access requirement above has been met. If cloning fails with `Permission denied (publickey)`, resolve repository authentication before continuing. Changing AWS IAM permissions does not grant GitLab access, and this GitLab instance does not support Git cloning over HTTPS.

If the directory already exists from a previous run, use the [repeat-run instructions](#run-the-report-again) instead of cloning over it.

## Step 4 — Install Python and the dependencies inside CloudShell

Run these commands from `~/bedrock-quota-checker`:

```bash
sudo dnf install -y python3.11 python3.11-pip
python3.11 -m venv .venv
source .venv/bin/activate
python --version
python -m pip install -r requirements.txt
```

CloudShell uses Amazon Linux 2023, whose system Python can be 3.9. The collector's tested SDK requires Python 3.10 or newer, so these commands use Python 3.11 in a project virtual environment. Leave the system Python unchanged. The package installation affects the CloudShell environment.

Confirm that the source checksum and installation are valid:

```bash
sha256sum -c bedrock_access_report.py.sha256
python bedrock_access_report.py --version
```

The checksum check should print `bedrock_access_report.py: OK`. After activating `.venv`, use `python` for the following commands.

## Step 5 — Generate the HTML and quota files

Still in the same CloudShell terminal, run:

```bash
python bedrock_access_report.py \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

Replace the example Regions with the ones your applications use. For cross-Region inference, include the **source Region where your application sends its request**.

Wait for the collector to finish. Runtime depends on the number of Regions, metric series, and API retries. The script prints progress, followed by the absolute paths of the generated files:

```text
HTML: /home/cloudshell-user/bedrock-quota-checker/reports/bedrock-report_<account-id>_<timestamp>/report.html
QUOTAS CSV: /home/cloudshell-user/bedrock-quota-checker/reports/bedrock-report_<account-id>_<timestamp>/quotas.csv
JSON: /home/cloudshell-user/bedrock-quota-checker/reports/bedrock-report_<account-id>_<timestamp>/report.json
ZIP: /home/cloudshell-user/bedrock-quota-checker/reports/bedrock-report_<account-id>_<timestamp>.zip
```

These are example paths. **Use the actual paths printed by your run.** Each run creates a separate report directory and ZIP archive. Isolated collection failures are recorded in the report instead of silently becoming zero usage.

## Step 6 — Download the HTML and quota CSV from CloudShell

1. Copy the full path printed after **`HTML:`**.
2. In the CloudShell toolbar, choose **Actions → Download file**.
3. Paste the path into the file-path field and choose **Download**.
4. Repeat with the full path printed after **`QUOTAS CSV:`** to download `quotas.csv`.

To download all outputs in one file, repeat the same procedure with the path printed after **`ZIP:`**.

## Step 7 — Open the results

Open the downloaded **`report.html`** in your browser. It is an offline dashboard with usage charts, current quotas, model availability, filters, and collection-quality details. The interface stays in English, with US English number formatting and UTC dates.

Open **`quotas.csv`** in your preferred spreadsheet application or text editor. It includes current applied quotas, AWS defaults, quota codes, units, scope, and collection timestamps.

If you downloaded the ZIP, extract it first and keep its files together. This preserves the dashboard's links to the CSV and JSON exports. Viewing the report requires no AWS credentials, web server, or additional installation.

Review the outputs before sharing them: they can contain account IDs, resource identifiers, quotas, and operational usage.

## Run the report again

Reopen CloudShell in the same Region and identity used for the original checkout, then run:

```bash
cd ~/bedrock-quota-checker
git pull --ff-only origin main
sudo dnf install -y python3.11 python3.11-pip
source .venv/bin/activate
python -m pip install -r requirements.txt
sha256sum -c bedrock_access_report.py.sha256
python bedrock_access_report.py \
  --regions us-east-1 us-west-2 \
  --days 14 \
  --output-dir ./reports
```

CloudShell home files can persist between sessions, but system packages may need reinstalling. Git authentication must still be valid for `git pull`. If the checkout or virtual environment is missing, repeat Steps 3 and 4. Download the new outputs using Step 6.

## Optional commands in CloudShell

Run these from the project directory with `.venv` activated.

**Inventory and quotas only, without historical usage queries:**

```bash
python bedrock_access_report.py --regions us-east-1 --skip-usage
```

**A 30-day trend:**

```bash
python bedrock_access_report.py --regions us-east-1 --days 30 --period auto
```

A 14-day report uses one-minute intervals. A 30-day report uses five-minute intervals; per-minute rates are averages within those intervals and can hide shorter bursts.

**All available options:**

```bash
python bedrock_access_report.py --help
```

See the [CloudShell customer guide](docs/customer-guide.md) for output details, permissions, interpretation limits, and troubleshooting.

## Troubleshooting

| Problem | What to do in CloudShell |
|---|---|
| `git clone` reports `Permission denied (publickey)` | Confirm GitLab permission and a valid Midway-signed SSH identity in this session. External customers need an approved repository they can access. |
| Clone connection times out | Confirm that the CloudShell environment can reach the repository endpoint. |
| Project directory already exists | Follow “Run the report again”; keep existing reports. |
| Python version or dependency error | Repeat Step 4 and verify that `python --version` reports Python 3.11 after activation. |
| `python3.11: command not found` after reopening CloudShell | Repeat the `sudo dnf install` command before activating `.venv`. |
| AWS credentials are missing or expired | Reopen CloudShell from the intended console account and role, then repeat Step 2. |
| `AccessDenied` during collection | Check the failed action and Region in `collection_issues.csv` against the collector read policy. |
| Download says the file does not exist | Copy the exact absolute path from the completed run, without the `HTML:` or `QUOTAS CSV:` prefix. |
| Usage or a quota comparison shows `N/A` | Read “Collection quality” in the HTML; missing data or an unvalidated mapping is not zero usage. |

Environment references: [CloudShell software and package installation](https://docs.aws.amazon.com/cloudshell/latest/userguide/vm-specs.html), [Python versions in Amazon Linux 2023](https://docs.aws.amazon.com/linux/al2023/ug/python.html).
