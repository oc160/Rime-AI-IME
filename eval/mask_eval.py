# -*- coding: utf-8 -*-
"""显示型联想评测：给定公文前缀，让模型填下一个词。

与被测模型无关（OpenAI 兼容接口），统一提示词、贪心解码、关闭思维链。
用法:
  python -X utf8 mask_eval.py --port 8083 --tag 4b_qwen
输出: reports/mask_<tag>.txt  每行 "ctx | gold | pred"
      reports/mask_<tag>.time.txt  每条耗时(ms)
"""
import argparse
import json
import time
import urllib.request

PROMPT = (
    "下面是一段中文公文/技术文本的片段，结尾处缺少一个词。"
    "请直接输出填入结尾的下一个词，只输出这个词本身，不要解释，不要加标点。\n\n"
    "片段：{ctx}____"
)


def ask(port: int, ctx: str) -> tuple[str, float]:
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
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8"))
    ms = (time.time() - t0) * 1000
    pred = d["choices"][0]["message"]["content"].strip()
    # 去掉首尾引号/空白与常见包裹
    for w in ("「", "」", "“", "”", '"', "'"):
        pred = pred.replace(w, "")
    pred = pred.strip().splitlines()[0].strip() if pred.strip() else ""
    return pred, ms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--samples", default="reports/mask_domain_samples.jsonl")
    a = ap.parse_args()

    samples = [json.loads(l) for l in open(a.samples, encoding="utf-8") if l.strip()]
    out = open(f"reports/mask_{a.tag}.txt", "w", encoding="utf-8")
    tout = open(f"reports/mask_{a.tag}.time.txt", "w", encoding="utf-8")
    t_all = time.time()
    for i, s in enumerate(samples):
        try:
            pred, ms = ask(a.port, s["ctx"])
        except Exception as e:  # noqa: BLE001
            pred, ms = f"<ERR {e}>", -1
        out.write(f"{s['ctx']} | {s['gold']} | {pred}\n")
        out.flush()
        tout.write(f"{ms:.0f}\n")
        tout.flush()
        if (i + 1) % 20 == 0:
            print(f"[{i+1}/{len(samples)}] elapsed {time.time()-t_all:.0f}s", flush=True)
    print(f"DONE {len(samples)} in {time.time()-t_all:.0f}s -> reports/mask_{a.tag}.txt")


if __name__ == "__main__":
    main()
