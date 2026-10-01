#!/usr/bin/env bash
# Builds the AI SRE Lambda bundle (Python 3.12, arm64) into services/ai/build/: the Anthropic SDK
# (with its AWS signing support, for Claude on Amazon Bedrock) and sre.py.
set -euo pipefail
cd "$(dirname "$0")/../services/ai"
rm -rf build && mkdir build
pip install -q --target build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 -r requirements.txt
cp sre.py build/
echo "built $(du -sh build | cut -f1) in services/ai/build"
