# -*- coding: utf-8 -*-
"""pred_sidecar.py — AI联想 v2 副驾：文件握手 → llama-server(predv2) → 词键结果缓存

与 rime.lua 的握手约定（目录 = --watch-dir，默认 %APPDATA%/Rime）：
  pred_req.txt   lua 原子覆盖写，每行 "词<TAB>上下文"（同词不重复请求）
  pred_resp.txt  本进程追加，每行 "词<TAB>模型下一词<TAB>unix秒"，裁剪保留末400行
  lua 在挂码时整读 pred_resp.txt 建 词→下一词 映射；查不到就走查表兜底。

模型/配方不可改（与 2026-10-14 验收同源）：
  models/qwen35-08b-predict-v2-Q8_0.gguf，PROMPT/temperature=0/max_tokens=32/
  enable_thinking=False 原样照抄 prediction_kit/mask_eval.py。
复读过滤规则照抄 prediction_kit/HANDOFF.md §4。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_EXE = os.path.join(BASE, "tools", "llama", "llama-server.exe")
MODEL = os.path.join(BASE, "models", "qwen35-08b-predict-v2-Q8_0.gguf")
LOG_PATH = os.path.join(BASE, "reports", "pred_sidecar_v2.log")
SRV_LOG = os.path.join(BASE, "reports", "srv_predv2_live.log")

PROMPT = (
    "下面是一段中文公文/技术文本的片段，结尾处缺少一个词。"
    "请直接输出填入结尾的下一个词，只输出这个词本身，不要解释，不要加标点。\n\n"
    "片段：{ctx}____"
)

def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass

def server_health(port, timeout=2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def spawn_server(port):
    if not os.path.exists(SERVER_EXE):
        log(f"FATAL llama-server not found: {SERVER_EXE}")
        return None
    log(f"spawning llama-server port={port} (CPU-only, 同 eval 参数: -c 2048 -t 6 --parallel 4)")
    try:
        lf = open(SRV_LOG, "a", encoding="utf-8")
    except OSError:
        lf = subprocess.DEVNULL
    # BELOW_NORMAL_PRIORITY_CLASS：打字/桌面永远优先，推理只吃剩余算力——
    # 词边界恢复打字的瞬间不再撞上推理窗口（黏滞感的根源）
    p = subprocess.Popen(
        [SERVER_EXE, "-m", MODEL, "--port", str(port), "-c", "2048",
         "-t", "4", "--parallel", "4", "--jinja", "--no-webui", "-ngl", "0"],
        stdout=lf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0),
    )
    return p

def ensure_server(port, proc_holder):
    if server_health(port):
        return
    if proc_holder.get("proc") and proc_holder["proc"].poll() is None:
        proc_holder["proc"].kill()
        time.sleep(1)
    p = spawn_server(port)
    proc_holder["proc"] = p
    t0 = time.time()
    while time.time() - t0 < 120:
        if server_health(port):
            log(f"llama-server ready in {time.time()-t0:.0f}s pid={p.pid if p else '?'}")
            return
        if p and p.poll() is not None:
            log("llama-server exited early, see srv_predv2_live.log")
            return
        time.sleep(1)
    log("llama-server health timeout")

def ask_model(port, ctx, timeout=8.0):
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": PROMPT.format(ctx=ctx)}],
        "temperature": 0,
        "max_tokens": 32,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    pred = d["choices"][0]["message"]["content"].strip()
    for w in ("「", "」", "\u201c", "\u201d", '"', "'"):
        pred = pred.replace(w, "")
    pred = pred.strip().splitlines()[0].strip() if pred.strip() else ""
    return pred, (time.time() - t0) * 1000

def is_cjk(s):
    return all('\u4e00' <= ch <= '\u9fff' for ch in s)

def repeat_char(s):
    return len(s) >= 3 and len(set(s)) == 1

def clean_pred(pred, ctx):
    """复读/合法性过滤：返回 None 表示丢弃（规则照抄 HANDOFF §4）"""
    if not pred:
        return None
    pred = pred.strip().strip("。，、；：""''")
    if not (1 <= len(pred) <= 8):
        return None
    if not is_cjk(pred):
        return None
    tail = ctx[-12:]
    if pred in tail:                      # 输出 ⊆ 上文尾部
        return None
    if len(pred) >= 4 and tail.endswith(pred):   # 上文长后缀
        return None
    if repeat_char(pred):                 # 单字重复≥3
        return None
    return pred

def append_result(path, word, pred):
    lines = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln]
    except OSError:
        pass
    lines.append(f"{word}\t{pred}\t{int(time.time())}")
    lines = lines[-400:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-dir", default=os.path.join(os.environ.get("APPDATA", ""), "Rime"))
    ap.add_argument("--port", type=int, default=8084)
    ap.add_argument("--interval", type=float, default=0.15)
    a = ap.parse_args()

    req_path = os.path.join(a.watch_dir, "pred_req.txt")
    resp_path = os.path.join(a.watch_dir, "pred_resp.txt")
    os.makedirs(a.watch_dir, exist_ok=True)

    log(f"=== pred_sidecar start watch={a.watch_dir} port={a.port} pid={os.getpid()} ===")
    proc_holder = {}
    ensure_server(a.port, proc_holder)
    try:  # 预热：首个真实查询不再吃首推理冷启动
        ask_model(a.port, "预热", timeout=90)
        log("warmup done")
    except Exception as e:
        log(f"warmup fail (忽略): {e}")

    served = {}          # word -> ts（600s 内不重复问）
    last_mtime = -1
    fails = 0
    while True:
        try:
            if not os.path.exists(req_path):
                time.sleep(a.interval)
                continue
            mt = os.path.getmtime(req_path)
            if mt != last_mtime:
                last_mtime = mt
                try:
                    with open(req_path, "r", encoding="utf-8") as f:
                        rows = [ln for ln in f.read().splitlines() if "\t" in ln]
                except OSError:
                    rows = []
                for row in rows[:1]:   # 每圈1条：推理错峰，最新请求下一圈自然接管
                    word, _, ctx = row.partition("\t")
                    if not word or not ctx:
                        continue
                    if time.time() - served.get(word, 0) < 600:
                        continue
                    served[word] = time.time()
                    try:
                        pred, ms = ask_model(a.port, ctx)
                        fails = 0
                        cp = clean_pred(pred, ctx)
                        if cp:
                            append_result(resp_path, word, cp)
                            log(f"OK [{word}] -> {cp} ({ms:.0f}ms)")
                        else:
                            log(f"DROP [{word}] -> {pred!r}")
                    except Exception as e:
                        fails += 1
                        log(f"ASK-FAIL [{word}] {type(e).__name__}: {e} (fails={fails})")
                        if fails >= 2:
                            ensure_server(a.port, proc_holder)
                            fails = 0
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"loop-err {type(e).__name__}: {e}")
        time.sleep(a.interval)

if __name__ == "__main__":
    main()
