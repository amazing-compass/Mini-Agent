#!/usr/bin/env python3
"""Honestly combine N *full-run* SWE-Bench eval reports into pass@k / avg@k.

Each input MUST be a full-50 run's eval report (every instance re-rolled,
NOT just the failures). Cherry-picking failures and merging wins is invalid;
this script only computes recognized metrics:

  - pass@1 per run   : that run's resolved count (single-shot)
  - avg@k            : mean resolved count across runs (typical score)
  - pass@k           : an instance counts as solved if ANY run solved it
                       (upper-envelope; only honest when ALL tasks were
                       re-rolled every run, and when LABELED as pass@k)
  - per-task stability: solved in how many of the k runs (flakiness)

Usage:
    uv run python passk_report.py REPORT1.json REPORT2.json [REPORT3.json ...]

    # e.g. combine fork runs:
    uv run python passk_report.py \
        mini-agent-fork-rerun-deepseek-v4-pro.fork_rerun_eval.json \
        mini-agent-fork-run2-deepseek-v4-pro.fork_run2_eval.json
"""
from __future__ import annotations

import collections
import json
import sys


def load_resolved(path: str) -> tuple[set[str], int]:
    d = json.load(open(path))
    submitted = d.get("submitted_instances")
    return set(d.get("resolved_ids", [])), submitted


def main(paths: list[str]) -> int:
    if len(paths) < 1:
        print(__doc__)
        return 1

    runs = []
    submitted_counts = set()
    for p in paths:
        resolved, submitted = load_resolved(p)
        runs.append((p, resolved))
        submitted_counts.add(submitted)

    k = len(runs)
    print(f"合并 {k} 次跑 (每次应为完整 {submitted_counts} 题):\n")

    # pass@1 per run
    counts = [len(r) for _, r in runs]
    for (p, r) in runs:
        print(f"  pass@1  [{len(r):>2}]  {p.split('/')[-1]}")
    print()

    if len(submitted_counts) != 1:
        print(f"⚠️ 这些报告的 submitted 题数不一致 {submitted_counts} —— "
              f"不是同一组 50 题就别合并。\n")

    # avg@k and pass@k
    union = set().union(*[r for _, r in runs])
    inter = set(runs[0][1]).intersection(*[r for _, r in runs]) if k > 1 else set(runs[0][1])
    avg = sum(counts) / k
    n = next(iter(submitted_counts)) or 50
    print(f"  avg@{k}  (平均，最该当作'真实分'): {avg:.1f}/{n}  ({100*avg/n:.1f}%)")
    print(f"  pass@{k} (任一次解出就算，需标注): {len(union)}/{n}  ({100*len(union)/n:.1f}%)")
    print(f"  每次都解出的稳定核心            : {len(inter)}/{n}")
    print(f"  噪声区间 (min~max)             : {min(counts)} ~ {max(counts)}")
    print()

    # per-task stability
    freq = collections.Counter()
    for _, r in runs:
        for iid in r:
            freq[iid] += 1
    flaky = sorted([iid for iid, c in freq.items() if 0 < c < k])
    print(f"  抖动题 (有时解出有时不，共 {len(flaky)} 个):")
    for iid in flaky:
        print(f"      {iid:32}  {freq[iid]}/{k} 次解出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
