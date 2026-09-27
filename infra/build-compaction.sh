#!/usr/bin/env bash
# Builds the compaction Lambda bundle (Python 3.12, arm64) into
# services/compaction/build/. `aws cloudformation package` zips and uploads it.
set -euo pipefail
cd "$(dirname "$0")/../services/compaction"
rm -rf build && mkdir build
pip install -q --target build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 -r requirements.txt
cp handler.py compact.py bloom.py lookup.py layout.py build/
echo "built $(du -sh build | cut -f1) in services/compaction/build"
