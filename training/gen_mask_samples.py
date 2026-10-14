# -*- coding: utf-8 -*-
"""词边界掩码采样器：从语料生成「前缀→下一词」评测/训练样本。

与旧版（随机字符位切）的区别：切位对齐词典词边界，gold 是完整词——
完全命中率因此变得有意义（旧版 2% 是切位任意性压出来的下限）。

词典：rime-ice cn_dicts（或任意同格式 yaml），或本目录 assets/wordlist.txt（纯词表，每行一词）。

用法:
  python -X utf8 gen_mask_samples.py --corpus wiki_sentences.txt -n 5000 --seed 42 \
      [--dict-dir /path/to/rime-ice/cn_dicts] [--wordlist assets/wordlist.txt]
输出: <corpus>.mask_samples.jsonl  每行 {"ctx","gold","pos"}
"""
import argparse
import json
import random
import re
from pathlib import Path


def load_words(dict_dir: str | None, wordlist: str | None) -> set[str]:
    words: set[str] = set()
    if wordlist and Path(wordlist).is_file():
        for line in open(wordlist, encoding="utf-8"):
            w = line.strip().split("\t")[0]
            if w:
                words.add(w)
        return words
    for p in sorted(Path(dict_dir).glob("*.dict.yaml")):
        for line in open(p, encoding="utf-8"):
            if line.startswith("#") or "\t" not in line:
                continue
            w = line.split("\t")[0].strip()
            # 过滤英文/单字母/超长词条
            if w and not re.fullmatch(r"[A-Za-z0-9%.]+", w) and len(w) <= 6:
                words.add(w)
    return words


def max_match(s: str, words: set[str], maxlen: int = 6) -> list[tuple[int, str]]:
    """最大正向匹配，返回 [(起始位置, 词)]；未匹配单字也成词。"""
    out, i = [], 0
    while i < len(s):
        hit = ""
        for l in range(min(maxlen, len(s) - i), 1, -1):
            if s[i : i + l] in words:
                hit = s[i : i + l]
                break
        if hit:
            out.append((i, hit))
            i += len(hit)
        else:
            out.append((i, s[i]))
            i += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="每行一句的语料文件")
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-ctx", type=int, default=12, help="前缀最短字符数")
    ap.add_argument("--dict-dir", default=None)
    ap.add_argument("--wordlist", default=None)
    a = ap.parse_args()

    words = load_words(a.dict_dir, a.wordlist)
    if not words:
        raise SystemExit("需要 --dict-dir 或 --wordlist 提供分词词典")
    print(f"词典词数: {len(words)}")

    rng = random.Random(a.seed)
    sents = [l.strip() for l in open(a.corpus, encoding="utf-8") if len(l.strip()) >= a.min_ctx + 2]
    rng.shuffle(sents)

    out_path = a.corpus.rsplit(".", 1)[0] + ".mask_samples.jsonl"
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for s in sents:
            if n >= a.n:
                break
            toks = max_match(s, words)
            if len(toks) < 3:
                continue
            # 随机选一个词位（跳过句首），要求前缀够长、目标词为中文
            idxs = [i for i, (p, w) in enumerate(toks[1:], start=1)
                    if toks[i - 1][0] >= a.min_ctx and re.fullmatch(r"[\u4e00-\u9fff]+", w)]
            if not idxs:
                continue
            i = rng.choice(idxs)
            pos, gold = toks[i]
            f.write(json.dumps({"ctx": s[:pos], "gold": gold, "pos": pos}, ensure_ascii=False) + "\n")
            n += 1
    print(f"DONE {n} samples -> {out_path}")


if __name__ == "__main__":
    main()
