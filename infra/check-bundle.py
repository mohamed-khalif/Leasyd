#!/usr/bin/env python3
"""Fails when a Lambda bundle imports one of our own modules that wasn't copied into it.

  python3 infra/check-bundle.py services/synthetics/build

Our modules are the .py files directly in services/*/ (not tests). A bundle that copies
services/ingest/ingest.py but not the modules ingest.py imports deploys fine and then fails every
invocation with "No module named ..." (2026-10-05: Synthetics was down for every tenant)."""
import ast
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parent.parent
ours = {p.stem for p in root.glob("services/*/*.py") if not p.stem.startswith("test_")}
build = pathlib.Path(sys.argv[1])
have = {p.stem for p in build.glob("*.py")}
missing = set()
for name in list(have):
    if name not in ours:
        continue
    for node in ast.walk(ast.parse((build / f"{name}.py").read_text())):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
               [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
        missing |= {f"{name}.py imports {m}" for m in mods if m.split(".")[0] in ours and m.split(".")[0] not in have}
if missing:
    sys.exit(f"{build}: our modules missing from the bundle:\n  " + "\n  ".join(sorted(missing)))
print(f"{build}: every imported module of ours is in the bundle")
