# -*- coding: utf-8 -*-
"""prep_train_data.py — 合成样本 → 训练集：防泄漏过滤 + train/val 切分

泄漏判据（直接 import bench_rerank.CASES，与评测同一数据源，永不脱钩）：
  1. (pinyin, expected) 与任何 bench 用例相同 → 剔除（评测歧义对不许进训练集）
  2. 候选词集合与 bench 用例完全相同 → 剔除（防排序信息泄漏）
用法:
  python prep_train_data.py synthetic_cases.jsonl -o data/ [--val-ratio 0.02]
"""
import argparse, json, os, random, sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "eval"))   # bench_rerank 在 eval/ 目录
from bench_rerank import CASES

BENCH_KEYS = {(py, exp) for _, py, _, exp in CASES}
BENCH_CAND_SETS = {frozenset(c) for _, _, c, _ in CASES}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", default="data")
    ap.add_argument("--val-ratio", type=float, default=0.02)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    kept, leak, total = [], 0, 0
    seen = set()
    for line in open(args.input, encoding="utf-8"):
        total += 1
        d = json.loads(line)
        if (d["pinyin"], d["expected"]) in BENCH_KEYS or \
           frozenset(d["cands"]) in BENCH_CAND_SETS:
            leak += 1
            continue
        key = (d["ctx"], d["pinyin"], d["expected"])  # 跨段落去重
        if key in seen:
            continue
        seen.add(key)
        kept.append(d)

    # 部署对齐增强：随机 5% 丢弃上下文（线上 lua 支持无历史查询，bench 有无上下文用例）
    rng = random.Random(777)
    aug = []
    for d in kept:
        if d["ctx"] and rng.random() < 0.05:
            aug.append({**d, "ctx": ""})
    kept.extend(aug)

    rng = random.Random(123)
    rng.shuffle(kept)
    n_val = max(int(len(kept) * args.val_ratio), 1)
    os.makedirs(args.o, exist_ok=True)
    with open(os.path.join(args.o, "val.jsonl"), "w", encoding="utf-8") as f:
        for d in kept[:n_val]:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with open(os.path.join(args.o, "train.jsonl"), "w", encoding="utf-8") as f:
        for d in kept[n_val:]:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"总数 {total}，泄漏剔除 {leak}，去重后 {len(kept)}，"
          f"train {len(kept)-n_val} / val {n_val} → {args.o}/")

if __name__ == "__main__":
    main()
