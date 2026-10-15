# -*- coding: utf-8 -*-
"""deploy_live.py — 对线上 %APPDATA%\Rime 跑全量维护（重编译词表）

WeaselDeployer /deploy 是快速通道，dict 源码变更不触发重编译；
此处走 librime start_maintenance(full_check=1)，与托盘"重新部署"等价。
"""
import os, sys, ctypes, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay_eval import (WEASEL_DIR, SHARED_DATA, RimeTraits, RimeApi)

class MiniDeploy:
    def __init__(self):
        os.add_dll_directory(WEASEL_DIR)
        self.dll = ctypes.WinDLL(os.path.join(WEASEL_DIR, "rime.dll"))
        self.dll.rime_get_api.restype = ctypes.c_void_p
        api_ptr = self.dll.rime_get_api()
        self.api = ctypes.cast(api_ptr, ctypes.POINTER(RimeApi)).contents
        t = RimeTraits()
        t.data_size = ctypes.sizeof(RimeTraits) - ctypes.sizeof(ctypes.c_int)
        t.shared_data_dir = SHARED_DATA.encode()
        t.user_data_dir = os.path.join(os.environ["APPDATA"], "Rime").encode()
        t.distribution_name = b"Weasel"
        t.distribution_code_name = b"weasel-deploy-cli"
        t.distribution_version = b"0.1"
        t.app_name = b"rime.deploy-cli"
        t.min_log_level = 3
        _logdir = os.path.join(os.environ.get("TEMP", "C:\Temp"), "rime_replay")
        os.makedirs(_logdir, exist_ok=True)
        t.log_dir = _logdir.encode("ascii", "replace")
        self._mods = (ctypes.c_char_p * 5)(b"default", b"octagram", b"lua", b"predict", None)
        t.modules = ctypes.cast(self._mods, ctypes.POINTER(ctypes.c_char_p))
        self.api.setup(ctypes.byref(t))
        self.api.initialize(ctypes.byref(t))
        self.api.start_maintenance(1)   # 1 = full_check
        print("[deploy] maintenance(full_check=1) started", flush=True)
        t0 = time.time()
        while True:
            if not self.api.is_maintenance_mode():
                print(f"[deploy] maintenance finished in {time.time()-t0:.1f}s", flush=True)
                break
            if time.time() - t0 > 600:
                print("[deploy] TIMEOUT 600s", flush=True); sys.exit(2)
            time.sleep(2)
        self.api.join_maintenance_thread()
        self.api.finalize()

if __name__ == "__main__":
    MiniDeploy()
    print("[deploy] done")
