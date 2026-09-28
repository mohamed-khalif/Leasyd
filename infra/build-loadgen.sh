#!/usr/bin/env bash
# Builds the load-generator Lambda bundle (Python 3.12, arm64) into services/loadgen/build/.
set -euo pipefail
cd "$(dirname "$0")/../services/loadgen"
rm -rf build && mkdir build
pip install -q --target build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 -r requirements.txt
cp loadgen.py build/
echo "built $(du -sh build | cut -f1) in services/loadgen/build"
