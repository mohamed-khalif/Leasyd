#!/usr/bin/env bash
# The live demo's visitors (POST /v1/demo/session keeps each email as demo-lead#<email> in obs-tenants)
# as CSV: email, first_visit, last_visit, visits. Newest visit first.
#   AWS_DEFAULT_REGION=us-east-1 infra/demo-leads.sh > demo-leads.csv
set -euo pipefail
: "${AWS_DEFAULT_REGION:=us-east-1}"; export AWS_DEFAULT_REGION
aws dynamodb scan --table-name obs-tenants --filter-expression "begins_with(pk, :p)" \
  --expression-attribute-values '{":p": {"S": "demo-lead#"}}' --output json |
python3 -c '
import csv, json, sys
items = json.load(sys.stdin)["Items"]
rows = sorted(({"email": i["email"]["S"], "first_visit": i["first_visit"]["S"], "last_visit": i["last_visit"]["S"],
               "visits": i["visits"]["N"]} for i in items), key=lambda r: r["last_visit"], reverse=True)
w = csv.DictWriter(sys.stdout, fieldnames=["email", "first_visit", "last_visit", "visits"])
w.writeheader(); w.writerows(rows)'
