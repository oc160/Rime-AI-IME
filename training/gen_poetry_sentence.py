# -*- coding: utf-8 -*-
"""gen_poetry_sentence.py — 诗词整句重排样本生成（原型）

用回放引擎（真实 rime.dll）生成部署分布的整句候选：
  上下文 = 上一句（真实打字上屏），拼音 = 下一句全拼，候选 = 引擎 top-5 整句组词
  expected = 真实诗句（在候选内才收录——重排器够得着的样本）
这是词级挖矿在文言域碰壁后的正确姿势：候选由引擎生成，天然是部署分布。
"""
import ctypes, json, os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from replay_eval import RimeReplay, RimeContext
from pypinyin import lazy_pinyin

def load_couplets(limit=200):
    pairs = []
    for fp in glob.glob(r"<项目目录>\corpus\chinese-poetry\全唐诗\poet.tang.*.json")[:60]:
        try:
            poems = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for p in poems:
            for para in p.get("paragraphs", []):
                segs = [s.strip() for s in para.replace("，", "。").split("。") if s.strip()]
                for i in range(0, len(segs) - 1, 2):
                    a, b = segs[i], segs[i + 1]
                    if 4 <= len(b) <= 9 and len(a) == len(b):
                        pairs.append((a, b))
        if len(pairs) >= limit * 3:
            break
    return pairs[:limit * 3]

def to_simplified(text, char_map):
    return "".join(char_map.get(c, c) for c in text)

def main():
    char_map = {}
    for line in open("corpus/TSCharacters.txt", encoding="utf-8"):
        p = line.strip().split("\t")
        if len(p) == 2 and not line.startswith("#"):
            char_map[p[0]] = p[1].split(" ")[0]

    pairs = [(to_simplified(a, char_map), to_simplified(b, char_map))
             for a, b in load_couplets()]
    print(f"对联样本池: {len(pairs)}")

    eng = RimeReplay("poetry", grammar_on=False)
    out, seen = [], set()
    for a, b in pairs:
        if len(out) >= 200:
            break
        py_a = "".join(lazy_pinyin(a))
        py_b = "".join(lazy_pinyin(b))
        if len(py_b) > 30 or not py_b:
            continue
        sid = eng.new_session()
        eng.type_pinyin(sid, py_a)
        eng.press_space(sid)
        eng.pop_commit(sid)
        eng.type_pinyin(sid, py_b)
        ctx = eng.context_of(sid) or {"candidates": []}
        cands = [c for c in ctx["candidates"][:5] if len(c) >= 2]
        eng.api.destroy_session(sid)
        if b in cands and b not in seen:
            seen.add(b)
            out.append({"ctx": a, "pinyin": py_b, "cands": cands, "expected": b,
                        "layer": "poetry_sentence"})
    eng.close()
    with open("data/poetry_sentence_proto.jsonl", "w", encoding="utf-8") as f:
        for d in out:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"产出整句样本: {len(out)}")
    for d in out[:3]:
        print("  ", json.dumps(d, ensure_ascii=False)[:130])

if __name__ == "__main__":
    main()
