#!/usr/bin/env bash
# Builds the tenant-admin and account-API Lambda bundle into services/tenants/build/
# (boto3 comes with the Lambda runtime; no other dependencies).
set -euo pipefail
cd "$(dirname "$0")/../services/tenants"
rm -rf build && mkdir build
cp admin.py account.py emails.py build/
echo "built services/tenants/build"
