#!/usr/bin/env bash
# Builds the investigator's Lambda bundle (Python 3.12, arm64) into services/investigator/build/:
# the Anthropic SDK (with its AWS signing support, for Claude on Amazon Bedrock), its packages
# (domain, agent, ports, tools, adapters, handlers) and the AI SRE's query tools (services/ai/sre.py).
# boto3 comes with the Lambda runtime. The eval harness (eval/) stays out.
set -euo pipefail
cd "$(dirname "$0")/../services/investigator"
rm -rf build && mkdir build
pip install -q --target build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 -r requirements.txt
cp -r domain agent ports tools adapters handlers build/
cp ../ai/sre.py build/
find build -name __pycache__ -prune -exec rm -rf {} +
echo "built $(du -sh build | cut -f1) in services/investigator/build"
