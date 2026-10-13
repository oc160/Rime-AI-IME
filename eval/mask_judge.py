# -*- coding: utf-8 -*-
"""掩码评测判分辅助：自动分出 完全命中 / 复读机，其余待人工判定。

用法:
  python -X utf8 mask_judge.py reports/mask_4b_qwen.txt

复读机定义（自动）: pred 为空，或 pred 出现在 ctx 末尾 12 字内，或 pred 整体是 ctx 的尾部子串。
待判定行会带序号打印，人工对每行给 R(合理)/W(不合理) 组成的串回填：
  python -X utf8 mask_judge.py reports/mask_4b_qwen.txt --verdicts "RRWWR..."
"""
import argparse
import re
import sys


def norm(s: str) -> str:
    return re.sub(r"[\s，。、；：""''！？.,;:!?]", "", s)


def is_echo(ctx: str, pred: str) -> bool:
    if not pred:
        return True
    p = norm(pred)
    tail = norm(ctx)[-12:]
    return len(p) > 0 and p in tail


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--verdicts", default="")
    a = ap.parse_args()

    rows = []
    for line in open(a.path, encoding="utf-8"):
        line = line.rstrip("\n")
        if " | " not in line:
            continue
        ctx, gold, pred = line.rsplit(" | ", 2)
        rows.append((ctx, gold, pred))

    exact, echo, pending = [], [], []
    for i, (ctx, gold, pred) in enumerate(rows):
        if norm(pred) == norm(gold):
            exact.append(i)
        elif is_echo(ctx, pred):
            echo.append(i)
        else:
            pending.append(i)

    print(f"total={len(rows)} exact={len(exact)} ({len(exact)/len(rows):.0%}) "
          f"echo={len(echo)} ({len(echo)/len(rows):.0%}) pending={len(pending)}")

    v = a.verdicts
    if not v:
        print("\n--- 待人工判定 ---")
        for j, i in enumerate(pending):
            print(f"[{j}] {rows[i][0]} ⟹ {rows[i][2]}   (gold:{rows[i][1]})")
        print(f"\n回填: python -X utf8 mask_judge.py {a.path} --verdicts \"{'R'*len(pending)}\"")
        sys.exit(0)

    if len(v) != len(pending):
        sys.exit(f"verdicts 长度 {len(v)} != 待判定数 {len(pending)}")
    reasonable = sum(1 for c in v if c.upper() == "R")
    n = len(rows)
    print(f"人工判定: 合理={reasonable} 不合理={len(pending)-reasonable}")
    print(f"合计: 完全命中={len(exact)} ({len(exact)/n:.0%}) | "
          f"严格合理(命中+人工R)={len(exact)+reasonable} ({(len(exact)+reasonable)/n:.0%}) | "
          f"复读机={len(echo)} ({len(echo)/n:.0%})")


if __name__ == "__main__":
    main()
