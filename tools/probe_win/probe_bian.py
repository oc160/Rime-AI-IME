# -*- coding: utf-8 -*-
"""probe_bian.py — 离线复现 custom_phrase 抢占候选 + 调洪库容不出现

复用 replay_eval 的隔离副本机制，完全不碰线上 %APPDATA%\Rime。
用法:
  python probe_bian.py            # 现状（331 领域词全量）
  python probe_bian.py --strip    # 对照组：剥掉领域词，只留原 35 条
"""
import os, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay_eval import RimeReplay, RimeContext, WORK_DIR
import ctypes, shutil, re

PROBES = [
    ("bian",            "想打: 变"),
    ("biandun",         "想打: 边墩"),
    ("diaohongkurong",  "想打: 调洪库容(diao码)"),
    ("tiaohongkurong",  "想打: 调洪库容(tiao码/真实读音)"),
    ("jiaoshen",        "想打: 校审"),
    ("gaocheng",        "想打: 高程"),
]

class StrippedReplay(RimeReplay):
    def _prepare_user_dir(self, grammar_on):
        super()._prepare_user_dir(grammar_on)
        p = os.path.join(self.user_dir, "custom_phrase.txt")
        text = open(p, encoding="utf-8").read()
        # 剥掉领域段：保留文件头注释 + 原 35 条（首个 "# --- 领域词库" 之前的内容）
        cut = text.find("# --- 领域词库")
        if cut > 0:
            open(p, "w", encoding="utf-8", newline="").write(text[:cut])
            print(f"[strip] 剥掉领域词，保留 {text[:cut].count(chr(10)) - 4} 条原短语")


def dump(r: RimeReplay, sid, label, py):
    r.type_pinyin(sid, py)
    ctx = RimeContext()
    ctx.data_size = ctypes.sizeof(RimeContext) - ctypes.sizeof(ctypes.c_int)
    if not r.api.get_context(sid, ctypes.byref(ctx)):
        print(f"{py}({label}): <无上下文>")
        return
    m = ctx.menu
    rows = []
    for i in range(min(m.num_candidates, 9)):
        c = m.candidates[i]
        t = c.text.decode("utf-8", "replace") if c.text else ""
        cm = c.comment.decode("utf-8", "replace") if c.comment else ""
        rows.append(f"{i+1}.{t}{(' ' + cm) if cm else ''}")
    preedit = ctx.composition.preedit.decode("utf-8", "replace") if ctx.composition.preedit else ""
    r.api.free_context(ctypes.byref(ctx))
    print(f"\n▶ {py}  ({label})   preedit={preedit}")
    for row in rows:
        print("   " + row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strip", action="store_true")
    args = ap.parse_args()

    tag = "probe_strip" if args.strip else "probe_live"
    r = (StrippedReplay if args.strip else RimeReplay)(tag, grammar_on=True)
    sid = r.new_session()
    for py, label in PROBES:
        dump(r, sid, label, py)
        r.api.process_key(sid, 0xFF1B, 0)   # Esc 清组合
    r.api.destroy_session(sid)
    r.close()
    print("\n[done]", tag)

if __name__ == "__main__":
    main()
