# -*- coding: utf-8 -*-
"""label_teacher.py — v1 教师批量标注：蒸馏学生训练数据的燃料

原理：合并后的 v1 模型在 GPU 上批量推理（batch 64），对 30 万同音组样本给出选择；
与语料金标准分歧的样本单独标记（学生训练时按需过滤——TRAINING.md 的噪声控制纪律）。
输出: data/distill_v1.jsonl  每行 {ctx,pinyin,cands,expected,teacher,agree}
"""
import argparse, json, os, sys, time
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_sft import SYSTEM_PROMPT, build_user, render

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/hf/qwen35-08b-ime-v1")
    ap.add_argument("--pool", default="corpus/distill_pool.jsonl")
    ap.add_argument("--out", default="data/distill_v1.jsonl")
    ap.add_argument("--bs", type=int, default=64)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    dev = "cuda"

    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=torch.bfloat16, attn_implementation="sdpa",
        device_map=dev, low_cpu_mem_usage=True).eval()
    pad_id = tok.pad_token_id or tok.eos_token_id

    rows = [json.loads(l) for l in open(args.pool, encoding="utf-8")]
    t0, n_ok, n_dis = time.perf_counter(), 0, 0
    with open(args.out, "w", encoding="utf-8") as f:
        for i in range(0, len(rows), args.bs):
            chunk = rows[i:i + args.bs]
            prompts = [render(tok, [{"role": "system", "content": SYSTEM_PROMPT},
                                     {"role": "user", "content": build_user(
                                         r["ctx"], r["pinyin"], r["cands"])}],
                              add_generation_prompt=True) for r in chunk]
            enc = [tok(p, add_special_tokens=False).input_ids for p in prompts]
            m = max(len(e) for e in enc)
            ids = torch.tensor([[pad_id] * (m - len(e)) + e for e in enc], device=dev)
            att = torch.tensor([[0] * (m - len(e)) + [1] * len(e) for e in enc], device=dev)
            with torch.no_grad():
                out = model.generate(input_ids=ids, attention_mask=att, max_new_tokens=6,
                                     do_sample=False, pad_token_id=pad_id)
            for r, o in zip(chunk, out):
                text = tok.decode(o[m:], skip_special_tokens=True).strip()
                w = text.split("、")[0].split("\n")[0].strip()
                agree = int(w == r["expected"])
                n_ok += agree; n_dis += (1 - agree)
                f.write(json.dumps({**r, "teacher": w, "agree": agree},
                                   ensure_ascii=False) + "\n")
            if (i // args.bs) % 50 == 0:
                done = i + len(chunk)
                rate = done / (time.perf_counter() - t0)
                eta = (len(rows) - done) / max(rate, 1) / 60
                print(f"  {done}/{len(rows)}  {rate:.0f}/s  ETA {eta:.0f}min  "
                      f"一致率 {n_ok/max(n_ok+n_dis,1):.1%}", flush=True)
    print(f"完成：{len(rows)} 条，一致 {n_ok}（{n_ok/len(rows):.1%}），分歧 {n_dis}")

if __name__ == "__main__":
    main()
