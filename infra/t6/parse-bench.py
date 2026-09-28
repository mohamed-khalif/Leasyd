#!/usr/bin/env python3
"""Compact real raw files locally under a Lambda's limits: time, peak memory, pass/fail.

    python3 infra/t6/parse-bench.py --files 12                 # a compaction chunk (2M records)
    python3 infra/t6/parse-bench.py --files 1                  # one raw file, as the fast lane sees it
    python3 infra/t6/parse-bench.py --files 12 --memory-mb 3008 --threads 2

Input: the 12 raw files of a real 50 GB/h chunk (t6-000 logs, 2026-09-28 hour 11,
190 MB gzip, ~114 MB JSON / ~170k records per file, 2,025,984 records in all),
kept at s3://obs-data-<account>-us-east-1/_bench/chunk-2m/ and downloaded once
to --dir. That chunk failed out of memory on AWS with the attribute-map CASE
shortcut (ac003b8) and compacts with the original expression (7b2f5d7).

Limits mirror the Lambda: DuckDB memory_limit = 60% of the function memory
(as handler.py sets it) and 2 threads (~1.7 vCPU at 3008 MB). Peak RSS is the
whole process; on Lambda the function is killed at its memory size.
Run each case in a fresh process (this script does one case per run).
"""

import argparse
import glob
import os
import resource
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "services", "compaction"))

import compact  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--files", type=int, default=12, help="how many of the 12 raw files to compact together")
    p.add_argument("--memory-mb", type=int, default=3008, help="the Lambda's memory size")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--signal", default="logs")
    p.add_argument("--dir", default=os.path.expanduser("~/.obs-bench/chunk-2m"))
    p.add_argument("--bucket", default=None, help="default obs-data-<account>-us-east-1")
    a = p.parse_args()

    if len(glob.glob(os.path.join(a.dir, "*.json.gz"))) < 12:
        bucket = a.bucket or "obs-data-{}-us-east-1".format(subprocess.check_output(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], text=True).strip())
        os.makedirs(a.dir, exist_ok=True)
        subprocess.check_call(["aws", "s3", "cp", "--only-show-errors", "--recursive",
                               f"s3://{bucket}/_bench/chunk-2m/", a.dir, "--exclude", "*", "--include", "*.json.gz"])
    files = sorted(glob.glob(os.path.join(a.dir, "*.json.gz")))[:a.files]

    connect = compact._connect

    def limited(work_dir, memory_limit):
        con = connect(work_dir, memory_limit)
        con.execute(f"SET threads = {a.threads}")
        return con
    compact._connect = limited

    out = os.path.join(a.dir, "out")
    shutil.rmtree(out, ignore_errors=True)
    limit = f"{int(a.memory_mb * 0.6)}MB"
    t0 = time.time()
    try:
        written = compact.compact(a.signal, files, out, "bench", "2026-09-28", "11", memory_limit=limit,
                                  id_digests={})
        result = f"OK: {len(written)} files, {sum(w['rows'] for w in written)} rows"
    except Exception as e:  # noqa: BLE001  (report, don't crash)
        result = "FAIL: " + str(e).splitlines()[0]
    finally:
        shutil.rmtree(out, ignore_errors=True)
        for f in glob.glob(os.path.join(a.dir, "*.plain*")):   # decompressed copies left by plain_json
            os.remove(f)
    gz = sum(os.path.getsize(f) for f in files) >> 20
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10
    print(f"{len(files)} files ({gz} MB gzip), DuckDB limit {limit}, {a.threads} threads: {result}; "
          f"{time.time() - t0:.1f} s, peak RSS {rss} MB (Lambda {a.memory_mb} MB)")


if __name__ == "__main__":
    main()
