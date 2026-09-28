#!/usr/bin/env bash
# The canary needs only the Lambda runtime (boto3 included): copy its module.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf services/canary/build && mkdir -p services/canary/build
cp services/canary/canary.py services/canary/build/
