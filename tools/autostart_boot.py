# -*- coding: utf-8 -*-
"""autostart_boot.py — 开机全自动：推理服务 + sidecar + 文件缓存预热 + WeaselServer 预拉起

注册方式（管理员一次）:
  schtasks /create /tn "IME_Autostart" /tr "C:\\Python314\\pythonw.exe F:\\输入法\\autostart_boot.py" /sc onlogon /rl highest /f

实测依据：方案加载仅 ~0.06s（雾凇 build 产物 + 温缓存），卡顿元凶是进程启动与冷盘读。
本脚本把这两件事在登录时做掉，之后首次切小狼毫接近瞬时。
模型路径在 autostart_config.json，换模型只改配置不动任务。
"""
import json, os, subprocess, sys, time, urllib.request

ROOT = r"<项目目录>"
LOG = open(os.path.join(ROOT, "reports", "autostart.log"), "a", encoding="utf-8")
CFG = json.load(open(os.path.join(ROOT, "autostart_config.json"), encoding="utf-8"))

def log(m):
    LOG.write(f"{time.strftime('%m-%d %H:%M:%S')} {m}\n"); LOG.flush()

def llama_up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=2) as r:
            return b"ok" in r.read()
    except Exception:
        return False

def sidecar_up(rpc_dir):
    """ping 文件协议：写一个 ping 请求，1 秒内收到同 id 回应即存活"""
    import tempfile
    req, resp = os.path.join(rpc_dir, "req.json"), os.path.join(rpc_dir, "resp.json")
    try:
        json.dump({"id": "ping", "ctx": "", "pinyin": "ceshi", "cands": ["测试", "侧视"]},
                  open(req, "w", encoding="utf-8"))
        for _ in range(6):
            time.sleep(0.15)
            try:
                r = json.load(open(resp, encoding="utf-8"))
                if r.get("id") == "ping" and time.time() - r.get("ts", 0) < 10:
                    return True   # 新鲜 ping 回应（旧 resp.json 跨重启残留会假阳性）
            except Exception:
                pass
    except Exception:
        pass
    return False

def proc_running(name):
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return name.lower() in out.lower()

def main():
    log("=== 开机自启开始 ===")
    # ① 推理服务（若未运行）
    if not llama_up():
        exe = os.path.join(ROOT, CFG["llama_bin"])
        model = CFG["gguf"] if os.path.isabs(CFG["gguf"]) else os.path.join(ROOT, CFG["gguf"])
        logf = open(os.path.join(ROOT, "reports", "llama_server_live.log"), "w", encoding="utf-8")
        subprocess.Popen([exe, "-m", model, "--port", "8081", "--alias", "main", "-c", "2048",
                          "-ngl", "0", "-t", "6", "--jinja", "--parallel", "1", "--no-webui"],
                         cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
        for _ in range(60):
            time.sleep(1)
            if llama_up():
                break
        log(f"llama-server: {'已启动' if llama_up() else '启动失败(打字将无重排,安全直通)'}")
    else:
        log("llama-server: 已在运行")

    # ② sidecar（ping 探测，未响应则启动）
    rpc_dir = os.path.join(os.environ.get("APPDATA", ""), "Rime", "llm_rpc")
    os.makedirs(rpc_dir, exist_ok=True)
    if not sidecar_up(rpc_dir):
        subprocess.Popen([sys.executable, os.path.join(ROOT, "sidecar.py"),
                          "--log", os.path.join(ROOT, "reports", "sidecar_live.log")],
                         cwd=ROOT,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
        time.sleep(3)
        log(f"sidecar: {'已启动' if sidecar_up(rpc_dir) else '启动失败(打字将无重排,安全直通)'}")
    else:
        log("sidecar: 已在运行")

    # ③ 文件缓存预热（冷盘读是首切卡顿的另一半元凶）
    build = os.path.join(os.environ.get("APPDATA", ""), "Rime", "build")
    n = 0
    for root, _, files in os.walk(build):
        for f in files:
            try:
                with open(os.path.join(root, f), "rb") as fh:
                    while fh.read(1 << 20):
                        pass
                n += 1
            except OSError:
                pass
    log(f"文件缓存预热: {n} 个编译产物")

    # ④ WeaselServer 预拉起（进程与 COM 注册成本前置）
    if not proc_running("WeaselServer.exe"):
        weasel = os.path.join(os.environ["PROGRAMFILES"], "Rime", "weasel-0.17.0", "WeaselServer.exe")
        subprocess.Popen([weasel], creationflags=subprocess.DETACHED_PROCESS)
        log("WeaselServer: 已预拉起")
    else:
        log("WeaselServer: 已在运行")
    log("=== 开机自启完成 ===")

if __name__ == "__main__":
    main()
