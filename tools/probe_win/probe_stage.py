# -*- coding: utf-8 -*-
"""probe_stage.py — 验证"领域词入真词库"修复方案（隔离副本，不碰线上）"""
import os, sys, ctypes, shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from replay_eval import RimeReplay
from probe_bian import dump, PROBES

STAGE = os.path.join(BASE, "rime_stage")

class StageReplay(RimeReplay):
    def _prepare_user_dir(self, grammar_on):
        super()._prepare_user_dir(grammar_on)
        for fn in ("custom_domain.dict.yaml", "rime_ice.dict.yaml",
                   "rime_ice.custom.yaml", "custom_phrase.txt"):
            shutil.copy(os.path.join(STAGE, fn), os.path.join(self.user_dir, fn))
        print("[stage] 4 files injected")

r = StageReplay("probe_stage", grammar_on=True)
sid = r.new_session()
for py, label in PROBES:
    dump(r, sid, label, py)
    r.api.process_key(sid, 0xFF1B, 0)
r.api.destroy_session(sid); r.close()
print("[done] probe_stage")
