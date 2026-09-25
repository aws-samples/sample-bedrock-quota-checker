#!/usr/bin/env bash
#
# run.sh — Automated setup and report generation for Bedrock Quota Checker.
#
# This script performs the full manual process for you:
#   1. Verifies Python 3.10+ is available.
#   2. Creates a local virtual environment (.venv).
#   3. Installs the tested dependencies from requirements.txt.
#   4. Runs the collector to generate the offline HTML report and CSV/JSON exports.
#
# Usage:
#   ./run.sh [collector arguments...]
#
# Any arguments you pass are forwarded to bedrock_access_report.py. If you pass
# none, sensible defaults are used (see DEFAULT_ARGS below).
#
# Examples:
#   ./run.sh                                          # defaults: us-east-1 us-west-2, 14 days
#   ./run.sh --regions us-east-1 --skip-usage         # inventory and quotas only
#   ./run.sh --profile customer-readonly --regions us-east-1 --days 30
#
# Environment overrides:
#   PYTHON   Python interpreter to use (default: python3)
#   VENV     Virtual environment directory (default: .venv)

set -euo pipefail

# Resolve the directory this script lives in, so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv}"
COLLECTOR="bedrock_access_report.py"

# Default collector arguments, used only when none are provided on the command line.
# --all-enabled-regions discovers every enabled Region (requires ec2:DescribeRegions).
DEFAULT_ARGS=(--all-enabled-regions --days 14 --output-dir ./reports)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Verify Python 3.10+.
log "Checking for Python 3.10 or newer"
command -v "$PYTHON" >/dev/null 2>&1 || die "'$PYTHON' not found. Install Python 3.10+ or set PYTHON=/path/to/python3."
"$PYTHON" - <<'PY' || die "Python 3.10 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
log "Using $("$PYTHON" --version 2>&1)"

# 2. Create the virtual environment if it does not exist.
if [ ! -d "$VENV" ]; then
  log "Creating virtual environment in $VENV"
  "$PYTHON" -m venv "$VENV"
else
  log "Reusing existing virtual environment in $VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 3. Install dependencies.
log "Installing dependencies from requirements.txt"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt

log "Collector version: $(python3 "$COLLECTOR" --version 2>&1)"
log "boto3 version: $(python3 -c 'import boto3; print(boto3.__version__)')"

# 4. Generate the report.
if [ "$#" -gt 0 ]; then
  ARGS=("$@")
else
  ARGS=("${DEFAULT_ARGS[@]}")
  warn "No arguments provided; using defaults: ${DEFAULT_ARGS[*]}"
fi

log "Generating report: python3 $COLLECTOR ${ARGS[*]}"
# Capture the collector output while still streaming it live, so we can echo the
# exact generated file paths back to the user at the end.
OUTPUT_LOG="$(mktemp)"
trap 'rm -f "$OUTPUT_LOG"' EXIT
python3 "$COLLECTOR" "${ARGS[@]}" 2>&1 | tee "$OUTPUT_LOG"

# Extract the absolute paths the collector printed (lines like "ZIP: /path/file.zip").
HTML_PATH="$(grep -m1 '^HTML: '        "$OUTPUT_LOG" | sed 's/^HTML: //')"
CSV_PATH="$(grep -m1 '^QUOTAS CSV: '   "$OUTPUT_LOG" | sed 's/^QUOTAS CSV: //')"
ZIP_PATH="$(grep -m1 '^ZIP: '          "$OUTPUT_LOG" | sed 's/^ZIP: //')"

# Download instructions for AWS CloudShell.
echo
echo "============================================================"
echo " NEXT STEP — DOWNLOAD YOUR REPORT (AWS CloudShell)"
echo "============================================================"
echo "Your generated files:"
echo "  ZIP (everything): ${ZIP_PATH:-see the \"ZIP:\" line above}"
echo "  HTML report:      ${HTML_PATH:-see the \"HTML:\" line above}"
echo "  Quotas CSV:       ${CSV_PATH:-see the \"QUOTAS CSV:\" line above}"
echo
echo "To download them in CloudShell:"
echo "  1. In the top-right corner of the CloudShell window, click \"Actions\"."
echo "  2. Choose \"Download file\"."
echo "  3. Paste one of the exact paths above (the ZIP downloads everything at once)."
echo "  4. Click \"Download\"."
echo
echo "Then open report.html in your browser. It runs offline and needs no AWS credentials or web server."
echo
echo "(On your local computer the files are already on disk at the paths shown above — just open the output directory.)"
echo "============================================================"
