# -*- coding: utf-8 -*-
"""serve_ime.py — OpenAI 兼容的输入法重排推理服务（transformers + GPU）

llama.cpp 预编译 CUDA 包不含 Turing(sm_75) 内核（T10 不可用），本服务用
transformers 直载 HF 权重顶上 /v1/chat/completions 端点（lua filter 只需要这个协议）。
训练出的合并模型直接 --model 指向其目录即可换脑。

用法:
  HF_HOME=C:/hf_cache python serve_ime.py --port 8081 [--model Qwen/Qwen3.5-0.8B]
"""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

class Engine:
    def __init__(self, model_id):
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, attn_implementation="sdpa").cuda().eval()
        self.lock = threading.Lock()

    def chat(self, messages, max_tokens=8, temperature=0.0):
        with self.lock:
            prompt = self.tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
                chat_template_kwargs={"enable_thinking": False})
            ids = self.tok(prompt, add_special_tokens=False, return_tensors="pt").input_ids.cuda()
            with torch.no_grad():
                out = self.model.generate(
                    input_ids=ids, max_new_tokens=max_tokens,
                    do_sample=temperature > 0, pad_token_id=self.tok.eos_token_id)
            return self.tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()

ENGINE = None

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志（打字路径查询频繁）

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
            t0 = time.perf_counter()
            content = ENGINE.chat(req.get("messages", []),
                                  int(req.get("max_tokens", 8)),
                                  float(req.get("temperature", 0.0)))
            ms = (time.perf_counter() - t0) * 1000
            print(f"{time.strftime('%H:%M:%S')} {ms:6.0f}ms -> [{content}]", flush=True)
            self._json(200, {"choices": [{"message": {"role": "assistant", "content": content}}],
                             "usage": {"total_tokens": 0}})
        except Exception as e:
            self._json(500, {"error": str(e)})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--model", default="Qwen/Qwen3.5-0.8B")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"loading {args.model} …", flush=True)
    global ENGINE
    ENGINE = Engine(args.model)
    print("warmup:", ENGINE.chat([{"role": "user", "content": "hi"}], 4), flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"LISTENING 127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
