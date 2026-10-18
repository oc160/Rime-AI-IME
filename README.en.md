# rime-llm-reranker

[简体中文](README.md) | **English**

**LLM candidate reranking + next-word prediction for the Rime input method, on two platforms**: the dictionary proposes candidates, a local model reads the context and pushes the best word to the top — with zero blocking on the typing path.

| Platform | Approach | Docs |
|---|---|---|
| **Windows** (Weasel + rime-ice) | rime.lua v8 async architecture (sidecar file handshake) | this README |
| **Android** (Trime v3.3.12) | in-process C++ inference (4M model, libime.so), T9 9-key supported | [`android/BUILD.md`](android/BUILD.md) |

Both platforms share the same reranking core (lazy candidate enumeration, async query/pickup, confidence gating, training-data flywheel); fixes are ported both ways.

Started as a port of [FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker) (macOS, MIT), then rearchitected around an async sidecar, plus a complete task-specific fine-tuning pipeline.

> **Status**: v1 stable. Daily-driven on real hardware on both platforms; the zero-blocking keystroke path is verified across multiple machines. If the inference service dies, candidates silently fall back to native dictionary order.

```
① Candidate layer   rime-ice dictionary — sentence composition + top-5 word candidates
② Rerank layer      rime.lua v8 async filter
                    The keystroke path only does microsecond file I/O:
                    word committed → write req.json, return immediately
                    → next keystroke reads resp.json and applies the rerank
                         ↕ llm_rpc/ file handshake
                    sidecar process → llama-server (OpenAI protocol), local inference
                    ◆ inference server dead = safe passthrough to dictionary order
③ Model layer       Qwen3.5-0.8B + LoRA task fine-tune (pipeline in training/)
                    Real user selections (USER_PAIR) logged automatically
                    → personalization training-data flywheel
```

> **Why this architecture**: under Windows TSF the host app synchronously waits for the IME on every keystroke — the typing path has zero latency tolerance. All model queries therefore live off the keystroke path (async file handshake / background threads). This is the project's first design principle.

## Install notes

1. The first switch to Weasel after a deploy triggers a one-off cold load (a few seconds); warm it up in Notepad first.
2. Close any running WeaselServer / WeaselDeployer before deploying (multiple instances rewrite each other's config).
3. Primary environments: Windows 10/11 + Weasel 0.17.x, Android + Trime 3.3.12. If the inference service dies, typing silently falls back — no manual intervention needed.

## Quick start

Prerequisites: [Weasel (小狼毫)](https://github.com/rime/weasel) + Python 3.10+ + [llama.cpp](https://github.com/ggml-org/llama.cpp) (llama-server) + a GGUF model (this project's baseline: Qwen3.5-0.8B-Q8_0).

```bash
# 1. Fetch rime-ice (dictionary included; GPL-3.0 project — obtain it yourself, not bundled here)
git clone --depth 1 https://github.com/iDvel/rime-ice.git tools/rime-ice

# 2. Start the inference server (CPU, port 8081)
tools/llama/llama-server.exe -m models/Qwen3.5-0.8B-Q8_0.gguf \
    --port 8081 --alias main -c 2048 -ngl 0 -t 6 --jinja --no-webui

# 3. Start the sidecar (default RPC dir = %APPDATA%/Rime/llm_rpc, matching the lua filter)
python tools/sidecar.py --log sidecar.log

# 4. Install the IME config
#    rime/rime.lua + rime_ice.custom.yaml + schema files from tools/rime-ice → %APPDATA%\Rime\
#    keep only rime_ice in default.custom.yaml's schema_list
#    then redeploy (kill all Weasel processes first)

# 5. Prewarm: Notepad → Win+Space to Weasel → wait out the one-off cold load → type a word
```

Sanity check: `%APPDATA%\Rime\rime_llm.log` shows `v8-async started` and `RPC_OUT/RPC_IN` entries; `sidecar.log` shows inference latencies.

## Train your own reranking model

```bash
# 1. Generate data: corpus × dictionary homophone-group mining (any Chinese plain text works)
python training/gen_synthetic.py corpus.txt -n 150000 -o cases.jsonl

# 2. Leak-proof filtering + split (kept disjoint from the 40-case eval set in eval/, never decoupled)
python training/prep_train_data.py cases.jsonl -o data/

# 3. LoRA SFT (prompts byte-identical to deployment; large-vocab VRAM optimizations: left padding + logits_to_keep)
HF_HOME=./hf_cache python training/train_sft.py \
    --train data/train.jsonl --val data/val.jsonl \
    --out-adapter adapter_v1 --out-merged merged_v1 --epochs 2

# 4. Convert to GGUF into models/ + 40-case benchmark gate
#    (bench resolves relative names against models/; baselines: bare 0.8B = 50%, 2B = 80%)
python tools/llama.cpp/convert_hf_to_gguf.py merged_v1 --outtype q8_0 --outfile models/ime-v1.gguf
python eval/bench_rerank.py ime-v1.gguf
```

Sharing the machine with other GPU jobs? `tools/watcher_train.py` waits for them to go idle, then runs the whole chain (train → convert → benchmark) automatically.

## Evaluation

- `eval/bench_rerank.py`: 40 pinyin-ambiguity cases (context flips / idiom continuations / domain words / no context), auto-starts and stops llama-server, reports hit rate and latency percentiles
- `eval/replay_eval.py`: **isolated replay engine** — loads Weasel's rime.dll via ctypes and replays keystroke streams inside a copy of the user directory, never touching your foreground session. All pre-release validation happens here (project rule: isolated-pass ≠ production-safe, but isolated-fail means never ship)

| Model | 40-case hit rate | Notes |
|---|---|---|
| Qwen3.5-2B Q4_K_M (zero-shot) | 80% | teacher-tier reference |
| Qwen3.5-0.8B Q8 (zero-shot) | 50% | fine-tuning starting point |
| 0.8B + LoRA (this pipeline) | target ≥75% | training in progress |

## Roadmap

- **Phase A**: ship the 0.8B task fine-tune; long-sentence reranking (relaxing `max_preedit_len`) so the LLM judges whole-sentence candidates
- **Phase B**: rolling daily personalization (USER_PAIR flywheel → nightly LoRA → golden-set regression gate → swap the model when it passes)
- **Phase C (endgame)**: a 30-100M pinyin-specific scorer (warm-started from [Transformers4IME](https://github.com/VisualJoyce/Transformers4IME) PinyinGPT, then distilled), loaded in-process via ONNX — IPC eliminated, reranking becomes 1-5 ms of arithmetic

## Credits & license

- **[FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)** (MIT): the predecessor of this project. The core idea "an LLM judges the top-5 candidates" and the prompt format come from it; `rime.lua` keeps the lineage note in its header
- **[iDvel/rime-ice](https://github.com/iDvel/rime-ice)** (GPL-3.0): the Mist pinyin dictionary and schema. **Not bundled in this repo** — obtain it yourself
- **[Transformers4IME](https://github.com/VisualJoyce/Transformers4IME) / [RIME-LMDG](https://github.com/amzxyz/RIME-LMDG)**: references for Phase C and the grammar-model layer
- Code in this repo: MIT (see [LICENSE](LICENSE))

## Documentation (Chinese)

- [Incident report I: four full-desktop freezes on the 2060 host](docs/incident-2026-10-12-2060-host.md)
- [Incident report II: the T10 real-typing freeze and the async-rewrite decision](docs/incident-2026-10-13-t10-typing.md)
