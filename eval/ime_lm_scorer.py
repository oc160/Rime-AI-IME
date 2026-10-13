# -*- coding: utf-8 -*-
"""ime_lm_scorer.py — metasequoiaime/chinese-ime-lm 打分器的 Python/torch 移植

严格按 docs/format.md 实现：int8 行量化反解、Pre-LN、融合QKV因果注意力、
erf-GELU、绑定嵌入；打分 = log P(候选|上下文) 按字数归一，首字由前缀末位预测。
用法:
  python ime_lm_scorer.py bench        # 40 条歧义 bench
  python ime_lm_scorer.py golden       # 300 条黄金集（分层报分）
"""
import json, os, sys, time
import torch
import torch.nn.functional as F
from safetensors.torch import load_file

PAD, UNK, BOS = 0, 1, 2

class SentenceScorer:
    def __init__(self, path):
        raw = load_file(path)
        import struct
        with open(path, "rb") as f:                      # 读 header 拿元数据
            n = struct.unpack("<Q", f.read(8))[0]
            header = json.loads(f.read(n))
        meta = header["__metadata__"]
        assert meta["format"] == "chinese-ime-lm"
        cfg = json.loads(meta["config"])
        vocab = json.loads(meta["vocab"])
        self.ctx_len = cfg["context"]
        self.n_embd, self.n_head = cfg["n_embd"], cfg["n_head"]
        self.char2id = {t: i for i, t in enumerate(vocab) if len(t) == 1}
        self.w = {}
        for k, v in raw.items():
            if k.endswith(".scale"):
                continue
            if v.dtype == torch.int8:                    # 行对称量化：int8 × scale[行]
                scale = raw[k + ".scale"].float()
                self.w[k] = v.float() * scale.unsqueeze(1)
            else:
                self.w[k] = v.float()
        self.n_layer = sum(1 for k in self.w if k.endswith(".ln1.weight"))

    def encode(self, text):
        return [self.char2id.get(c, UNK) for c in text]

    def forward(self, ids):                              # ids: [T]
        T = len(ids)
        x = self.w["tok.weight"][ids] + self.w["pos.weight"][:T]
        mask = ~torch.triu(torch.ones(T, T, dtype=torch.bool), diagonal=1)  # SDPA: True=可参与
        for i in range(self.n_layer):
            b = f"blocks.{i}."
            y = F.layer_norm(x, (self.n_embd,), self.w[b+"ln1.weight"], self.w[b+"ln1.bias"], 1e-5)
            qkv = y @ self.w[b+"qkv.weight"].T + self.w[b+"qkv.bias"]
            q, k, v = qkv.split(self.n_embd, dim=-1)
            H = self.n_head
            q = q.view(T, H, -1).transpose(0, 1)
            k = k.view(T, H, -1).transpose(0, 1)
            v = v.view(T, H, -1).transpose(0, 1)
            att = F.scaled_dot_product_attention(q, k, v, attn_mask=mask, scale=None)
            att = att.transpose(0, 1).reshape(T, self.n_embd)
            x = x + att @ self.w[b+"proj.weight"].T + self.w[b+"proj.bias"]
            y = F.layer_norm(x, (self.n_embd,), self.w[b+"ln2.weight"], self.w[b+"ln2.bias"], 1e-5)
            y = F.gelu(y) @ self.w[b+"fc.weight"].T          # fc/out 无 bias（格式表）
            x = x + y @ self.w[b+"out.weight"].T
        x = F.layer_norm(x, (self.n_embd,), self.w["ln_f.weight"], self.w["ln_f.bias"], 1e-5)
        return x @ self.w["tok.weight"].T                # [T, vocab]

    def score(self, ctx, cand):
        """log P(cand | ctx) 按字数归一；首字由前缀末位隐状态预测"""
        cand_ids = self.encode(cand)
        n = len(cand_ids)
        room = self.ctx_len - n
        prefix = [BOS] + self.encode(ctx)
        prefix = prefix[-room:] if room > 0 else [BOS]
        ids = torch.tensor(prefix + cand_ids)
        logits = self.forward(ids)
        lp = F.log_softmax(logits, dim=-1)
        total = 0.0
        for j in range(n):
            pos = len(prefix) - 1 + j                    # 位置 pos 预测 cand[j]
            total += lp[pos, cand_ids[j]].item()
        return total / n

    def best(self, ctx, cands):
        scores = [self.score(ctx, c) for c in cands]
        return max(range(len(cands)), key=lambda i: scores[i]), scores

def run_bench(model_path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bench_rerank import CASES
    m = SentenceScorer(model_path)
    hits, t0 = 0, time.perf_counter()
    lat = []
    for ctx, py, cands, exp in CASES:
        t1 = time.perf_counter()
        idx, _ = m.best(ctx, cands)
        lat.append((time.perf_counter() - t1) * 1000)
        hits += int(cands[idx] == exp)
    n = len(CASES)
    lat.sort()
    print(f"{os.path.basename(os.path.dirname(model_path))}: {hits}/{n} ({hits/n:.0%})  "
          f"每题 {sum(lat)/len(lat):.0f}ms (p95 {lat[int(n*0.95)]:.0f}ms)")
    return hits / n

def run_golden(model_path):
    m = SentenceScorer(model_path)
    rows = [json.loads(l) for l in open("data/golden_v1.jsonl", encoding="utf-8")]
    by_layer = {}
    for d in rows:
        idx, _ = m.best(d["ctx"], d["cands"])
        ok = int(d["cands"][idx] == d["expected"])
        by_layer.setdefault(d["layer"], [0, 0])
        by_layer[d["layer"]][0] += ok
        by_layer[d["layer"]][1] += 1
    total = [sum(v[0] for v in by_layer.values()), sum(v[1] for v in by_layer.values())]
    for layer, (h, n) in by_layer.items():
        print(f"  {layer:18s} {h}/{n} ({h/n:.0%})")
    print(f"  {'黄金集总分':16s} {total[0]}/{total[1]} ({total[0]/total[1]:.0%})")

if __name__ == "__main__":
    base = r"<项目目录>\models\ime-lm"
    target = sys.argv[2] if len(sys.argv) > 2 else "pinyin-ime-reranker-25M"
    mp = os.path.join(base, target, "sentence-model.safetensors")
    if sys.argv[1] == "bench":
        run_bench(mp)
    elif sys.argv[1] == "golden":
        run_golden(mp)
