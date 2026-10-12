# -*- coding: utf-8 -*-
"""sidecar.py — LLM 重排请求泵（v8 异步架构的服务端）

职责：轮询 llm_rpc/req.json → 查 llama-server（OpenAI 协议）→ 原子写 resp.json。
它由部署方在部署时启动，绝不进击键路径；它死了 lua 只是拿不到结果、直通默认排序，
打字体验完全无感——这就是 v8 与 v7（同步 curl）的本质区别。

用法:
  python sidecar.py --dir "%APPDATA%/Rime/llm_rpc" [--endpoint http://127.0.0.1:8081]
回放仿真时 --dir 指向 iso 副本的 llm_rpc 即可。
"""
import argparse, json, os, sys, time, tempfile
import urllib.request

# 与 rime.lua v8 / train_sft.py / bench_rerank.py 的提示词逐字一致（四处同步）
SYSTEM_PROMPT = ("你是输入法候选词排序助手。根据对话历史和当前输入，从候选词列表中选出最符合语境的词。"
                 "只输出这1个词本身，不要拼音、数字序号或任何解释。")

def build_user(ctx, pinyin, cands):
    c = "、".join(cands)
    if ctx:
        return (f"对话历史：「{ctx}」\n当前输入拼音：「{pinyin}」\n候选词：{c}\n"
                "只输出最符合对话语境的那1个候选词：")
    return f"输入拼音：「{pinyin}」\n候选词：{c}\n只输出最可能的那1个候选词："

def clean_word(w: str) -> str:
    import re
    w = re.sub(r"^[\d①②③%.、。\s]+", "", w or "")
    w = re.sub(r"[\d①②③%.、。,，;；!！\s]+$", "", w)
    return w.strip()

def call_llm(endpoint, ctx, pinyin, cands, timeout=8.0):
    body = {"model": "main", "stream": False, "temperature": 0.0, "max_tokens": 8,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": build_user(ctx, pinyin, cands)}],
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    content = (d["choices"][0]["message"].get("content") or "").strip()
    word = clean_word(content.split("、")[0].split("\n")[0])
    return word if word in cands else ""   # 不在候选内 = 无重排（与 lua 端一致）

def atomic_write(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.environ.get("APPDATA", "."), "Rime", "llm_rpc"))
    ap.add_argument("--endpoint", default="http://127.0.0.1:8081/v1/chat/completions")
    ap.add_argument("--poll-ms", type=int, default=15)
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(args.dir, exist_ok=True)
    req_path = os.path.join(args.dir, "req.json")
    resp_path = os.path.join(args.dir, "resp.json")
    logf = open(args.log, "a", encoding="utf-8") if args.log else None

    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        if logf:
            logf.write(line + "\n"); logf.flush()

    log(f"sidecar up | dir={args.dir} endpoint={args.endpoint}")
    seen_mtime = 0.0
    n_ok = n_err = n_drop = 0
    while True:
        try:
            mt = os.path.getmtime(req_path)
        except OSError:
            os.makedirs(args.dir, exist_ok=True)   # 目录被回放/清理时自重建
            time.sleep(args.poll_ms / 1000)
            continue
        if mt == seen_mtime:
            time.sleep(args.poll_ms / 1000)
            continue
        seen_mtime = mt
        try:
            req = json.load(open(req_path, encoding="utf-8"))
        except Exception:
            n_drop += 1
            continue
        t0 = time.perf_counter()
        try:
            word = call_llm(args.endpoint, req.get("ctx", ""), req.get("pinyin", ""),
                            req.get("cands", []))
            ms = (time.perf_counter() - t0) * 1000
            atomic_write(resp_path, {"id": req.get("id", ""), "word": word,
                                     "ts": int(time.time()), "ms": round(ms)})
            n_ok += 1
            log(f"{ms:6.0f}ms [{'命中' if word else '直通'}] {req.get('pinyin','')} → {word or '-'}")
        except Exception as e:
            n_err += 1
            log(f"ERR {str(e)[:80]} (ok={n_ok} err={n_err})")
            time.sleep(0.2)

if __name__ == "__main__":
    main()
