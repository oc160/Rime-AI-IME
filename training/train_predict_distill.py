# -*- coding: utf-8 -*-
"""train_predict_distill.py — 0.8B 联想副本蒸馏（打字机 → 训练机交接版）

底座：Qwen3.5-0.8B 基座（非 ime-v1 重排微调——零迁移已实证）
数据：领域+wiki 1:1 词边界样本，防复读过滤后
目标：上屏后候选栏尾部追加联想（方案 A）
验收：eval_domain_v2_boundary.jsonl 300 条，严格合理 ≥50%，复读 ≤20%
提示词 = eval/mask_eval.py 的 PROMPT 逐字一致（铁律 3）
显存优化：左对齐 + logits_to_keep（同 train_sft.py 验证方案，151k 词表必须）
"""
import argparse, json, os, sys, time
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

PROMPT = (
    "下面是一段中文公文/技术文本的片段，结尾处缺少一个词。"
    "请直接输出填入结尾的下一个词，只输出这个词本身，不要解释，不要加标点。\n\n"
    "片段：{ctx}____"
)

class PredictData(Dataset):
    def __init__(self, path, tok, max_len=120):
        self.rows = []
        for l in open(path, encoding="utf-8"):
            d = json.loads(l)
            if 2 <= len(d["next"]) <= 6:
                self.rows.append(d)
        self.tok = tok
        self.max_len = max_len

    def __len__(self): return len(self.rows)

    def __getitem__(self, i):
        d = self.rows[i]
        user = PROMPT.format(ctx=d["ctx"])
        try:
            prompt = self.tok.apply_chat_template(
                [{"role": "user", "content": user}], tokenize=False,
                add_generation_prompt=True,
                chat_template_kwargs={"enable_thinking": False})
            p_ids = self.tok(prompt, add_special_tokens=False)["input_ids"]
            # 词尾追加 im_end：让模型学会「输出一个词就停」（裸词训练不刹车的根因）
            im_end = self.tok.convert_tokens_to_ids("<|im_end|>")
            w_ids = self.tok(d["next"], add_special_tokens=False)["input_ids"] + [im_end]
            room = self.max_len - len(w_ids) - 1
            return {"p_ids": p_ids[-room:] if room > 0 else p_ids[-10:],
                    "w_ids": w_ids}
        except Exception:
            return None

def collate(batch, pad_id):
    """左对齐 + 只算目标词 logits（151k 词表显存优化）"""
    batch = [b for b in batch if b is not None]
    k = max(len(b["w_ids"]) for b in batch) + 1
    m = max(len(b["p_ids"]) + len(b["w_ids"]) for b in batch)
    ids, att, tgt = [], [], []
    for b in batch:
        seq = b["p_ids"] + b["w_ids"]
        pad = m - len(seq)
        ids.append([pad_id] * pad + seq)
        att.append([0] * pad + [1] * len(seq))
        tgt.append(b["w_ids"])
    return torch.tensor(ids), torch.tensor(att), tgt, k

def ce_loss(model, ids, att, tgt, k):
    """只对尾部目标 token 算交叉熵（logits_to_keep=k）"""
    out = model(input_ids=ids, attention_mask=att, logits_to_keep=k)
    logits = out.logits[:, :-1, :]
    B, V = logits.size(0), logits.size(-1)
    maxn = max(len(t) for t in tgt)
    lab = torch.full((B, maxn), -100, dtype=torch.long, device=logits.device)
    for i, t in enumerate(tgt):
        lab[i, maxn - len(t):] = torch.tensor(t, device=logits.device)
    return F.cross_entropy(logits[:, -maxn:, :].reshape(-1, V).float(),
                           lab.reshape(-1), ignore_index=-100)

@torch.no_grad()
def val_gen(model, tok, val_path, dev, n=50):
    model.eval()
    rows = [json.loads(l) for l in open(val_path, encoding="utf-8")][:n]
    exact, prefix2, repeat = 0, 0, 0
    pad_id = tok.pad_token_id or tok.eos_token_id
    for d in rows:
        user = PROMPT.format(ctx=d["ctx"])
        prompt = tok.apply_chat_template([{"role": "user", "content": user}],
                                          tokenize=False, add_generation_prompt=True,
                                          chat_template_kwargs={"enable_thinking": False})
        ids = tok(prompt, add_special_tokens=False, return_tensors="pt").input_ids.to(dev)
        out = model.generate(input_ids=ids, max_new_tokens=8, do_sample=False,
                             pad_token_id=pad_id)
        pred = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
        gold = d["next"]
        exact += int(pred == gold)
        prefix2 += int(pred[:2] == gold[:2] and len(pred) >= 2)
        ctx_tail = d["ctx"][-12:]
        repeat += int(pred and (pred in ctx_tail or (len(pred) >= 2 and pred[:2] in ctx_tail)))
    model.train()
    return exact, prefix2, repeat, len(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="Qwen/Qwen3.5-0.8B")
    ap.add_argument("--train", default="data/predict_mix_train.jsonl")
    ap.add_argument("--val", default="data/predict_mix_val.jsonl")
    ap.add_argument("--out-adapter", default="models/adapter_predict_v1")
    ap.add_argument("--out-merged", default="models/hf/qwen35-08b-predict-v1")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    dev = "cuda"

    tok = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForCausalLM.from_pretrained(
        args.base, dtype=torch.bfloat16, attn_implementation="sdpa",
        device_map=dev, low_cpu_mem_usage=True)
    from peft import LoraConfig, get_peft_model
    lora = LoraConfig(r=32, lora_alpha=64, lora_dropout=0.05,
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj"],
                      task_type="CAUSAL_LM")
    model = get_peft_model(model, lora)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.print_trainable_parameters()

    train_ds = PredictData(args.train, tok)
    if args.limit:
        train_ds.rows = train_ds.rows[:args.limit]
    pad_id = tok.pad_token_id or tok.eos_token_id
    tl = DataLoader(train_ds, batch_size=args.bs, shuffle=True,
                    collate_fn=lambda b: collate(b, pad_id))

    steps = len(tl) * args.epochs
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=args.lr, weight_decay=0.01)
    sched = get_cosine_schedule_with_warmup(opt, max(steps // 33, 10), steps)
    print(f"train {len(train_ds)}, steps {steps}, bs {args.bs}")

    gstep, t0 = 0, time.time()
    for ep in range(args.epochs):
        run, seen = 0.0, 0
        for ids, att, tgt, k in tl:
            loss = ce_loss(model, ids.to(dev), att.to(dev), tgt, k)
            loss.backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            opt.step(); sched.step(); opt.zero_grad()
            gstep += 1; run += loss.item(); seen += 1
            if gstep % 50 == 0:
                print(f"  ep{ep} {gstep}/{steps} loss {run/seen:.4f} "
                      f"{(time.time()-t0)/gstep:.2f}s/step", flush=True)
                run, seen = 0.0, 0
            if gstep % 2000 == 0:
                e, p2, rp, n = val_gen(model, tok, args.val, dev)
                print(f"  ★ val: exact {e}/{n} prefix2 {p2}/{n} repeat {rp}/{n} ({rp/n:.0%})",
                      flush=True)

    os.makedirs(args.out_adapter, exist_ok=True)
    model.save_pretrained(args.out_adapter)
    print("adapter:", args.out_adapter)
    if args.out_merged:
        os.makedirs(args.out_merged, exist_ok=True)
        merged = model.merge_and_unload()
        merged.save_pretrained(args.out_merged, safe_serialization=True)
        tok.save_pretrained(args.out_merged)
        print("merged:", args.out_merged)

if __name__ == "__main__":
    main()
