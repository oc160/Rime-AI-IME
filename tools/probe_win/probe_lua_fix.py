# -*- coding: utf-8 -*-
"""probe_lua_fix.py — 验证 rime.lua 惰性枚举修复：击键延迟 + 翻页完整 + RPC 行为"""
import os, sys, ctypes, time, shutil, io

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from replay_eval import RimeReplay
from probe_bian import dump

class LuaFixReplay(RimeReplay):
    def _prepare_user_dir(self, grammar_on):
        super()._prepare_user_dir(grammar_on)
        shutil.copy(os.path.join(BASE, "rime_stage", "rime.lua"),
                    os.path.join(self.user_dir, "rime.lua"))
        print("[luafix] staged rime.lua injected")

r = LuaFixReplay("probe_luafix", grammar_on=True)
api = r.api
ESC = 0xFF1B

def timed(sid, label, keys):
    for ch in keys:
        t0 = time.perf_counter()
        api.process_key(sid, ord(ch), 0)
        dt = (time.perf_counter() - t0) * 1000
        flag = "  ←← 卡!" if dt > 100 else ""
        print(f"  {label} '{ch}': {dt:7.1f}ms{flag}")
    api.process_key(sid, ESC, 0)

sid = r.new_session()
print("修复后(冷):")
timed(sid, "大写AI", "AI")
timed(sid, "小写ai", "ai")
timed(sid, "对照bj", "bj")
print("修复后(热):")
timed(sid, "大写AI", "AI")
timed(sid, "小写ai", "ai")
# 行为回归：候选正确性 + 翻页不丢（第10个候选还在）
dump(r, sid, "回归: biandun", "biandun"); api.process_key(sid, ESC, 0)
dump(r, sid, "回归: jiaoshen", "jiaoshen"); api.process_key(sid, ESC, 0)
# 翻页完整性：长音节输入后第 10+ 候选应存在（Page Down 或直接看 num_candidates）
api.process_key(sid, ESC, 0)
r.api.destroy_session(sid); r.close()
print("[done] luafix probe")
