#!/usr/bin/env bash
# Builds the investigator's Lambda bundle (Python 3.12, arm64) into services/investigator/build/:
# its packages (domain, agent, ports, tools, adapters, handlers). boto3 comes with the Lambda runtime.
set -euo pipefail
cd "$(dirname "$0")/../services/investigator"
rm -rf build && mkdir build
cp -r domain agent ports tools adapters handlers build/
find build -name __pycache__ -prune -exec rm -rf {} +
echo "built $(du -sh build | cut -f1) in services/investigator/build"
