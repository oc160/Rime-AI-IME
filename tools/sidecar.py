# -*- coding: utf-8 -*-
"""sidecar.py — LLM 重排请求泵（v8 异步架构的服务端）

双后端：
  --backend openai : 调 llama-server（OpenAI 协议，带置信度门槛）——②档主力
  --backend local  : 进程内直载 metasequoiaime/chinese-ime-lm 打分器（25MB/4.5MB，纯 CPU）
                     ——①.5 档轻量部署 / 2060 主机数据采集机。门槛 = top1-top2 分数差。
用法:
  python sidecar.py --dir "%APPDATA%/Rime/llm_rpc"                              # openai 后端
  python sidecar.py --backend local --model models/ime-lm/pinyin-ime-reranker-25M/sentence-model.safetensors
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

def call_llm(endpoint, ctx, pinyin, cands, timeout=8.0, conf_threshold=0.0):
    import math
    body = {"model": "main", "stream": False, "temperature": 0.0, "max_tokens": 8,
            "logprobs": True,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": build_user(ctx, pinyin, cands)}],
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    content = (d["choices"][0]["message"].get("content") or "").strip()
    word = clean_word(content.split("、")[0].split("\n")[0])
    if word not in cands:
        return "", None          # 不在候选内 = 无重排（与 lua 端一致）
    # 置信度门槛：首选 token 概率（贪心解码 = top-1 概率）。
    # 低于阈值说明模型自己也拿不准——不动手，保持词库原判（防"把不需要的推到前面"）
    conf = None
    if conf_threshold > 0:
        try:
            lp0 = d["choices"][0]["logprobs"]["content"][0]
            conf = math.exp(lp0["logprob"])
            if conf < conf_threshold:
                return "", conf
        except (KeyError, IndexError, TypeError):
            pass                 # 拿不到置信度信息时保守放行（模型选了候选内的词）
    return word, conf

def atomic_write(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)

def make_local_backend(model_path, conf_threshold):
    """进程内打分器后端：返回 (ctx, pinyin, cands) -> (word, conf)。
    conf = top1-top2 归一分数差（margin），低于阈值不动手（与生成式门槛同构）"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ime_lm_scorer import SentenceScorer
    scorer = SentenceScorer(model_path)

    def call(ctx, pinyin, cands):
        scored = [(scorer.score(ctx, c), c) for c in cands]
        scored.sort(reverse=True)
        top_score, top_word = scored[0]
        margin = top_score - scored[1][0] if len(scored) > 1 else 1.0
        # margin 经验尺度：0.35+ 相当于生成式 conf 0.6（待黄金集校准）
        if margin < conf_threshold:
            return "", margin
        return top_word, margin
    return call

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.environ.get("APPDATA", "."), "Rime", "llm_rpc"))
    ap.add_argument("--backend", choices=["openai", "local"], default="openai")
    ap.add_argument("--model", default=r"<项目目录>\models\ime-lm\pinyin-ime-reranker-25M\sentence-model.safetensors")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8081/v1/chat/completions")
    ap.add_argument("--poll-ms", type=int, default=15)
    ap.add_argument("--conf-threshold", type=float, default=0.6,
                    help="首选token概率低于此值不重排（0=关闭门槛）")
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(args.dir, exist_ok=True)
    if args.backend == "local":
        backend = make_local_backend(args.model, args.conf_threshold)
    else:
        _ep, _ct = args.endpoint, args.conf_threshold
        backend = lambda ctx, py, cands: call_llm(_ep, ctx, py, cands, conf_threshold=_ct)
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
            word, conf = backend(req.get("ctx", ""), req.get("pinyin", ""), req.get("cands", []))
            ms = (time.perf_counter() - t0) * 1000
            atomic_write(resp_path, {"id": req.get("id", ""), "word": word,
                                     "conf": round(conf, 4) if conf is not None else None,
                                     "ts": int(time.time()), "ms": round(ms)})
            n_ok += 1
            tag = "命中" if word else "直通"
            ctag = f" conf={conf:.2f}" if conf is not None else ""
            low = " [低置信拦截]" if word == "" and conf is not None and conf < args.conf_threshold else ""
            log(f"{ms:6.0f}ms [{tag}] {req.get('pinyin','')} → {word or '-'}{ctag}{low}")
        except Exception as e:
            n_err += 1
            log(f"ERR {str(e)[:80]} (ok={n_ok} err={n_err})")
            time.sleep(0.2)

if __name__ == "__main__":
    main()
