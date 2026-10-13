# -*- coding: utf-8 -*-
"""train_sft.py — Qwen3.5-0.8B 任务微调：候选词 5选1（single 模式）

提示词与 rime.lua / bench_rerank.py 的 single 模式逐字一致，训练即部署格式。
LoRA 微调（T10 16GB 富余），目标输出 = 正确词 + 模板收尾。

用法:
  HF_HOME=C:/hf_cache python train_sft.py --train data/train.jsonl --val data/val.jsonl \
      --out-adapter models/adapter_v1 --out-merged C:/ime_models/qwen35-08b-ime-v1
"""
import argparse, json, math, os, random, sys, time
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup
from peft import LoraConfig, get_peft_model

# 与 rime/rime_ice.custom.yaml 的 system_prompt 逐字一致（改动须三处同步）
SYSTEM_PROMPT = ("你是输入法候选词排序助手。根据对话历史和当前输入，从候选词列表中选出最符合语境的词。"
                 "只输出这1个词本身，不要拼音、数字序号或任何解释。")

def build_user(ctx, pinyin, cands):
    c = "、".join(cands)
    if ctx:
        return (f"对话历史：「{ctx}」\n当前输入拼音：「{pinyin}」\n候选词：{c}\n"
                "只输出最符合对话语境的那1个候选词：")
    return f"输入拼音：「{pinyin}」\n候选词：{c}\n只输出最可能的那1个候选词："

def render(tok, msgs, **kw):
    # transformers 各版本对模板 kwargs 的传递方式不同，两种都试
    for kwargs in (dict(kw, chat_template_kwargs={"enable_thinking": False}), kw):
        try:
            return tok.apply_chat_template(msgs, tokenize=False, **kwargs)
        except TypeError:
            continue
    raise RuntimeError("chat template 不接受 enable_thinking 参数，需人工检查模板")

class SFTData(Dataset):
    def __init__(self, path, tok, max_len=256):
        self.rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        self.tok, self.max_len = tok, max_len
        self.skip = 0
    def __len__(self): return len(self.rows)
    def __getitem__(self, i):
        r = self.rows[i]
        msgs = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user(r["ctx"], r["pinyin"], r["cands"])}]
        prompt = render(self.tok, msgs, add_generation_prompt=True)
        full = render(self.tok, msgs + [{"role": "assistant", "content": r["expected"]}])
        p_ids = self.tok(prompt, add_special_tokens=False)["input_ids"]
        f_ids = self.tok(full, add_special_tokens=False)["input_ids"]
        if f_ids[:len(p_ids)] != p_ids:
            self.skip += 1
            return None   # 模板前缀不一致：丢弃（实测发生率为 0）
        f_ids = f_ids[:self.max_len]
        return {"input_ids": f_ids, "n_target": len(f_ids) - len(p_ids)}

def collate(batch, pad_id):
    """左对齐填充（目标词统一在序列尾部）。
    大词表(15万)下若对全序列算 logits，fp32 一步就 2GB；目标只占尾部几个位置，
    配合 forward 的 logits_to_keep 只算尾部 k 个位置的 logits，显存省 90%"""
    batch = [b for b in batch if b is not None]
    k = max(b["n_target"] for b in batch) + 1          # logits_to_keep
    m = max(len(b["input_ids"]) for b in batch)
    ids, att, tgt = [], [], []
    for b in batch:
        n, L = b["n_target"], len(b["input_ids"])
        pad = m - L
        ids.append([pad_id] * pad + b["input_ids"])
        att.append([0] * pad + [1] * L)
        tgt.append(b["input_ids"][L - n:])             # 目标 token 本身
    return torch.tensor(ids), torch.tensor(att), tgt, k

def ce_loss(model, ids, att, tgt, k):
    """只对尾部目标 token 算交叉熵：logits_to_keep=k 取最后 k 行，
    其中第 j 行预测切片第 j+1 个 token，各样本目标占切片尾部 n 个 → 取尾部 maxn 行对齐"""
    out = model(input_ids=ids, attention_mask=att, logits_to_keep=k)
    logits = out.logits[:, :-1, :]                     # 去掉最后一行（无下一 token 可预测）
    B, V = logits.size(0), logits.size(-1)
    maxn = max(len(t) for t in tgt)
    lab = torch.full((B, maxn), -100, dtype=torch.long, device=logits.device)
    for i, t in enumerate(tgt):
        lab[i, maxn - len(t):] = torch.tensor(t, device=logits.device)
    return F.cross_entropy(logits[:, -maxn:, :].reshape(-1, V).float(), lab.reshape(-1),
                           ignore_index=-100)

@torch.no_grad()
def val_loss(model, loader, dev):
    model.eval()
    tot, n = 0.0, 0
    for ids, att, tgt, k in loader:
        loss = ce_loss(model, ids.to(dev), att.to(dev), tgt, k)
        tot += loss.item() * ids.size(0)
        n += ids.size(0)
    model.train()
    return tot / max(n, 1)

@torch.no_grad()
def val_exact(model, tok, rows, dev, bs=32, n=300):
    """留出集首词精确命中（与部署指标同构的粗估）"""
    model.eval()
    hits = 0
    rows = rows[:n]
    for i in range(0, len(rows), bs):
        chunk = rows[i:i + bs]
        prompts = [render(tok, [{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": build_user(r["ctx"], r["pinyin"], r["cands"])}],
                          add_generation_prompt=True) for r in chunk]
        enc = [tok(p, add_special_tokens=False, return_tensors="pt").input_ids[0] for p in prompts]
        m = max(len(e) for e in enc)
        padv = tok.pad_token_id or tok.eos_token_id
        ids = torch.stack([torch.cat([torch.full((m - len(e),), padv, dtype=torch.long), e]) for e in enc])   # 左对齐
        att = torch.stack([torch.cat([torch.zeros(m - len(e), dtype=torch.long), torch.ones(len(e), dtype=torch.long)]) for e in enc])
        out = model.generate(input_ids=ids.to(dev), attention_mask=att.to(dev),
                             max_new_tokens=8, do_sample=False,
                             pad_token_id=tok.pad_token_id or tok.eos_token_id)
        for r, o in zip(chunk, out):
            text = tok.decode(o[m:], skip_special_tokens=True).strip()  # 左对齐：跳过整个填充后的提示词长度 m
            hits += int(text == r["expected"])
    model.train()
    return hits, len(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="Qwen/Qwen3.5-0.8B")
    ap.add_argument("--train", default="data/train.jsonl")
    ap.add_argument("--val", default="data/val.jsonl")
    ap.add_argument("--out-adapter", default="models/adapter_v1")
    ap.add_argument("--out-merged", default="")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--grad-ckpt", type=int, default=0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0, help="限制训练样本数（调试用）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device: {dev}, base: {args.base}")

    tok = AutoTokenizer.from_pretrained(args.base)
    # 低内存流式加载：本机仅 16GB RAM 且与其他 GPU 负载方共用（空闲常 <4GB），
    # 常规加载会先在 CPU 物化全量权重再拷 GPU；device_map+low_cpu_mem_usage
    # 按分片直传 GPU，CPU 峰值只有几百 MB
    model = AutoModelForCausalLM.from_pretrained(
        args.base, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
        device_map="cuda:0", low_cpu_mem_usage=True)
    lora = LoraConfig(r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05,
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj"],
                      task_type="CAUSAL_LM")
    model = get_peft_model(model, lora).to(dev)
    # 梯度检查点：16GB 卡与其他 GPU 负载方共用，激活内存必须压（bs16+ckpt 峰值~3GB）
    if args.grad_ckpt:
        model.enable_input_require_grads()
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.print_trainable_parameters()

    train_ds = SFTData(args.train, tok)
    val_ds = SFTData(args.val, tok)
    if args.limit:
        train_ds.rows = train_ds.rows[:args.limit]
    pad_id = tok.pad_token_id or tok.eos_token_id
    tl = DataLoader(train_ds, batch_size=args.bs, shuffle=True,
                    collate_fn=lambda b: collate(b, pad_id), num_workers=0)
    vl = DataLoader(val_ds, batch_size=args.bs, shuffle=False,
                    collate_fn=lambda b: collate(b, pad_id), num_workers=0)

    steps = len(tl) * args.epochs
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=args.lr, weight_decay=0.01)
    sched = get_cosine_schedule_with_warmup(opt, max(steps // 33, 10), steps)

    print(f"train {len(train_ds)} / val {len(val_ds)}, epochs {args.epochs}, "
          f"steps/epoch {len(tl)}, total {steps}")
    model.train()
    gstep, t0 = 0, time.time()
    best_val = -1.0   # 防退化：只升不降的最佳适配器单独存档（末轮过拟合时保住最优解）
    for ep in range(args.epochs):
        run, seen = 0.0, 0
        for ids, att, tgt, k in tl:
            loss = ce_loss(model, ids.to(dev), att.to(dev), tgt, k)
            loss.backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            opt.step(); sched.step(); opt.zero_grad()
            gstep += 1; run += loss.item(); seen += 1
            if gstep % 25 == 0:
                el = time.time() - t0
                print(f"  ep{ep} step {gstep}/{steps} loss {run/seen:.4f} "
                      f"lr {sched.get_last_lr()[0]:.2e} {el/gstep:.2f}s/step", flush=True)
                run, seen = 0.0, 0
            if gstep % 500 == 0 and gstep < steps:
                os.makedirs(args.out_adapter, exist_ok=True)
                model.save_pretrained(args.out_adapter)  # 覆盖式存最新，中断也能拿去转换验收
                print(f"  checkpoint 已存: {args.out_adapter} (step {gstep})", flush=True)
        vl_loss = val_loss(model, vl, dev)
        hits, n = val_exact(model, tok, val_ds.rows, dev)
        print(f"== epoch {ep}: val_loss {vl_loss:.4f}, val首词命中 {hits}/{n} ({hits/n:.0%})", flush=True)
        if hits / max(n, 1) > best_val:
            best_val = hits / max(n, 1)
            os.makedirs(args.out_adapter + "-best", exist_ok=True)
            model.save_pretrained(args.out_adapter + "-best")
            print(f"   ★ 新最佳适配器已存 ({best_val:.1%}) → {args.out_adapter}-best", flush=True)

    if train_ds.skip:
        print(f"注意：{train_ds.skip} 条样本模板前缀不一致，退化为整句训练")
    os.makedirs(args.out_adapter, exist_ok=True)
    model.save_pretrained(args.out_adapter)  # 只存 LoRA
    print("adapter 已保存:", args.out_adapter)

    if args.out_merged:
        os.makedirs(args.out_merged, exist_ok=True)
        merged = model.merge_and_unload()
        merged.save_pretrained(args.out_merged, safe_serialization=True)
        tok.save_pretrained(args.out_merged)
        print("合并模型已保存:", args.out_merged)

if __name__ == "__main__":
    main()
