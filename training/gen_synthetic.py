# -*- coding: utf-8 -*-
"""gen_synthetic.py — 合成训练数据生成器（教师标注/蒸馏的前端）

原理：用雾凇拼音词库做同音组挖掘，把语料切成 (上下文, 拼音, 候选5, 正确词) 样本。

v2（2026-10-12 训练机修复）：
  1. word→codes 倒排索引取代逐词全组扫描（74万同音组），百万级样本量可用
  2. 切词返回字符偏移，修复上下文切片随跳过字符（标点/生字）漂移的 bug
  3. 多音字编码选择：优先取与 pypinyin 读音一致的编码（pip install pypinyin），
     未安装或无匹配时回退到词频最高的编码

用法:
  python gen_synthetic.py <语料.txt> [-n 100000] [-o out.jsonl]
  语料: utf-8 纯文本，一行一段。无语料时可先跑内置示例。
"""
import argparse, json, os, random, re, sys
from collections import defaultdict

_here = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [os.path.join(_here, "assets", "cn_dicts"),            # 套件内
               os.path.join(os.path.dirname(_here), "assets", "cn_dicts"),  # 项目scripts/
               os.path.join(os.path.dirname(_here), "tools", "rime-ice", "cn_dicts")]
DICT_DIR = next((p for p in _CANDIDATES if os.path.isdir(p)), _CANDIDATES[0])

def _norm_code(code: str) -> str:
    return code.replace(" ", "").replace("'", "").lower()

def load_dicts():
    """groups: code -> [(weight, word)] 同音组（组内按权重降序，池取高频=贴近真实候选）
    words:    词条集合（正向最大匹配用）
    word2codes: word -> [(weight, code)] 倒排索引"""
    groups = defaultdict(list)
    words = set()
    for fn in os.listdir(DICT_DIR):
        if not fn.endswith(".dict.yaml"):
            continue
        started = False
        for line in open(os.path.join(DICT_DIR, fn), encoding="utf-8", errors="ignore"):
            if not started:
                if line.strip() == "...":
                    started = True
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            word, code = parts[0].strip(), parts[1].strip()
            if not word or not code or len(word) > 6 or not re.match(r"^[a-z ']+$", code):
                continue
            if re.match(r"^[\u4e00-\u9fff]+$", word):
                w = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                groups[code].append((w, word))
                words.add(word)
    for code in groups:
        groups[code].sort(reverse=True)
    word2codes = defaultdict(list)
    for code, lst in groups.items():
        for w, word in lst:
            word2codes[word].append((w, code))
    return groups, words, word2codes

def segment(text, words, maxlen=4):
    """正向最大匹配切词；返回 [(start, word)]。
    start 为词首字符在原句中的偏移——上下文切片必须用它，不能用累计词长
    （原版 bug：标点/未匹配字符被跳过后，累计词长 ≠ 真实偏移，ctx 越切越歪）"""
    out, i = [], 0
    while i < len(text):
        hit = None
        for L in range(min(maxlen, len(text) - i), 1, -1):
            seg = text[i:i + L]
            if seg in words:
                hit = seg
                break
        if hit:
            out.append((i, hit))
            i += len(hit)
        else:
            i += 1
    return out

def pick_code(word, word2codes, pinyin_fn):
    """多音字：优先取与 pypinyin 读音一致的编码，否则取词频最高的编码"""
    codes = word2codes.get(word) or []
    if not codes:
        return None
    if pinyin_fn:
        py = pinyin_fn(word)
        for _, code in codes:
            if _norm_code(code) == py:
                return code
    return max(codes, key=lambda t: t[0])[1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="?", help="语料txt（缺省用内置示例）")
    ap.add_argument("-n", type=int, default=100000)
    ap.add_argument("-o", default="synthetic_cases.jsonl")
    ap.add_argument("--ctx", type=int, default=30, help="上下文字符数")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    random.seed(42)

    if args.corpus:
        lines = [l.strip() for l in open(args.corpus, encoding="utf-8", errors="ignore")
                 if len(l.strip()) >= 10]
    else:
        lines = ["今天天气很好，我们明天开会讨论计划。",
                 "水库汛期需要提前泄洪，腾出库容迎接洪峰。",
                 "大坝安全鉴定报告中要对工程缺陷进行详细描述。",
                 "这个问题不能再激化了，双方应该坐下来谈判。",
                 "考古学家正在研究这一历史事件的来龙去脉。"]
        print("(未指定语料，使用内置示例)")

    try:
        from pypinyin import lazy_pinyin
        pinyin_fn = lambda w: "".join(lazy_pinyin(w)).replace("ü", "v")
    except ImportError:
        pinyin_fn = None
        print("警告：未安装 pypinyin，多音字按词频近似（pip install pypinyin 可消除该噪声）",
              file=sys.stderr)

    print("加载词库…", file=sys.stderr)
    groups, words, word2codes = load_dicts()
    print(f"同音组 {len(groups)}，词条 {len(words)}", file=sys.stderr)

    cases, seen = [], set()
    for line in lines:
        if len(cases) >= args.n:
            break
        for start, seg in segment(line, words):
            if len(cases) >= args.n:
                break
            code = pick_code(seg, word2codes, pinyin_fn)
            if not code:
                continue
            lst = groups.get(code) or []
            norm = _norm_code(code)
            if len(lst) < 4 or len(norm) < 4:
                continue
            key = (line[:start], seg)
            if key in seen:
                continue
            seen.add(key)
            pool = [w for _, w in lst if w != seg][:30]
            if len(pool) < 4:
                continue
            cands = random.sample(pool, 4) + [seg]
            random.shuffle(cands)
            cases.append({
                "ctx": line[max(0, start - args.ctx):start],
                "pinyin": norm,
                "cands": cands,
                "expected": seg,
            })

    with open(args.o, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"生成 {len(cases)} 条 → {args.o}", file=sys.stderr)

if __name__ == "__main__":
    main()
