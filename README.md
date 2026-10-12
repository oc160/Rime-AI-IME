# rime-llm-reranker-win

**Windows 版 Rime 输入法 LLM 候选词重排**：词库出候选，本地大模型根据上下文把最合适的词顶到第一——打字路径零阻塞（异步文件握手架构）。

基于 [FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)（macOS 版，MIT）移植起步，架构已重构为异步 sidecar，并附带完整的任务微调训练管线。

```
① 候选生成层   rime-ice 词库（雾凇拼音）—— 整句组词 + 字词候选 Top5
② 重排层       rime.lua v8 异步 filter
               击键路径只做微秒级文件读写：
               打完词 → 写 req.json 立即返回 → 下一键读 resp.json 应用重排
                    ↕ llm_rpc/ 文件握手
               sidecar 独立进程 → llama-server（OpenAI 协议）本地推理
               ◆ 推理服务挂掉 = 安全直通词库顺序，打字完全无感
③ 模型层       Qwen3.5-0.8B + LoRA 任务微调（训练管线见 training/）
               用户真实选择(USER_PAIR)自动落盘 → 个性化训练数据飞轮
```

> **架构动机（真实事故）**：早期版本把同步模型查询（~300ms）放在击键路径上，隔离测试全绿，真人打字 40 秒后 explorer 挂起——Windows TSF 下应用每次击键都同步等输入法返回，路径上没有任何容忍度。完整取证见 [docs/incident-2026-10-13-t10-typing.md](docs/incident-2026-10-13-t10-typing.md)。

## ⚠️ 安装前必读

1. **必须保留至少两个输入法**（如微软拼音 + 小狼毫）。唯一输入法 = 单点故障，出问题时没有退路（`Win+空格` 随时切换是保命键）。
2. **必须预热**：部署后第一次切到小狼毫会触发方案冷加载（数秒一次性卡顿），先在记事本预热再正式使用。
3. **部署时杀干净 WeaselServer / WeaselDeployer 进程再部署**，多实例会互相改写配置（血泪教训，详见开发笔记）。
4. 本项目在 Windows 10 + Weasel 0.17.0 上开发测试；其他环境未验证。

## 快速开始

前置：[小狼毫 Weasel](https://github.com/rime/weasel) + Python 3.10+ + [llama.cpp](https://github.com/ggml-org/llama.cpp)（llama-server）+ 一个 GGUF 模型（本项目基线用 Qwen3.5-0.8B-Q8_0）。

```bash
# 1. 取得雾凇拼音（含词库，GPL-3.0 项目请自行获取，不入本仓库）
git clone --depth 1 https://github.com/iDvel/rime-ice.git tools/rime-ice

# 2. 启动推理服务（CPU，端口 8081）
tools/llama/llama-server.exe -m models/Qwen3.5-0.8B-Q8_0.gguf \
    --port 8081 --alias main -c 2048 -ngl 0 -t 6 --jinja --no-webui

# 3. 启动 sidecar（默认 RPC 目录 = %APPDATA%/Rime/llm_rpc，与 lua 一致）
python tools/sidecar.py --log sidecar.log

# 4. 安装输入法配置
#    rime/rime.lua + rime_ice.custom.yaml + tools/rime-ice 的方案文件 → %APPDATA%\Rime\
#    default.custom.yaml 的 schema_list 只留 rime_ice
#    然后重新部署（先杀干净 Weasel 进程）

# 5. 预热：记事本 → Win+空格 切小狼毫 → 等一次性冷加载完成 → 打一个词
```

确认工作：`%APPDATA%\Rime\rime_llm.log` 出现 `v8-async started` 和 `RPC_OUT/RPC_IN` 条目；`sidecar.log` 出现推理延迟记录。

## 训练你自己的重排模型

```bash
# 1. 造数据：语料 × 词库同音组挖矿（wiki 语料示例，任意中文纯文本均可）
python training/gen_synthetic.py corpus.txt -n 150000 -o cases.jsonl

# 2. 防泄漏过滤 + 切分（判据与 eval/bench 的 40 条评测集互斥，永不脱钩）
python training/prep_train_data.py cases.jsonl -o data/

# 3. LoRA SFT（提示词与部署逐字一致；含大词表显存优化：左 padding + logits_to_keep）
HF_HOME=./hf_cache python training/train_sft.py \
    --train data/train.jsonl --val data/val.jsonl \
    --out-adapter adapter_v1 --out-merged merged_v1 --epochs 2

# 4. 转 GGUF + 40 条基准验收（基线：裸 0.8B = 50%，2B = 80%）
python tools/llama.cpp/convert_hf_to_gguf.py merged_v1 --outtype q8_0 --outfile ime-v1.gguf
python eval/bench_rerank.py ime-v1.gguf
```

与其他 GPU 任务共用机器？`tools/watcher_train.py` 会等占用方下线后自动跑完整链（训练→转换→验收）。

## 评测

- `eval/bench_rerank.py`：40 条拼音歧义题（语境反转/成语接续/专业词/无上下文），自动起停 llama-server，输出命中率与延迟分位
- `eval/replay_eval.py`：**隔离回放引擎**——ctypes 加载 Weasel 的 rime.dll，在用户目录副本里回放按键流，完全不碰前台。上线前的一切验证都在这里做（本项目的铁律：隔离通过 ≠ 线上安全，但隔离不通过绝不上线）

| 模型 | 40 条命中 | 说明 |
|---|---|---|
| Qwen3.5-2B Q4_K_M（零样本） | 80% | 教师档参考 |
| Qwen3.5-0.8B Q8（零样本） | 50% | 微调起点 |
| 0.8B + LoRA（本管线） | 目标 ≥75% | 训练中 |

## 路线图

- **Phase A**：0.8B 任务微调上线；长句重排（`max_preedit_len` 放宽）让 LLM 裁判整句候选
- **Phase B**：每日滚动个性化（USER_PAIR 飞轮 → 夜间 LoRA → 黄金集回归闸门 → 达标换模型）
- **Phase C（终局）**：30-100M 拼音专用打分器（[Transformers4IME](https://github.com/VisualJoyce/Transformers4IME) PinyinGPT 热启动蒸馏），ONNX 进程内加载——IPC 归零，重排变 1-5ms 算术

## 致谢与许可证

- **[FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)**（MIT）：本项目的前身。核心思路「LLM 裁判 Top5 候选」与提示词格式源自它；`rime.lua` 头部保留其 lineage 声明
- **[iDvel/rime-ice](https://github.com/iDvel/rime-ice)**（GPL-3.0）：雾凇拼音词库与方案。**不入本仓库**，请自行获取
- **[Transformers4IME](https://github.com/VisualJoyce/Transformers4IME) / [RIME-LMDG 万象语法模型](https://github.com/amzxyz/RIME-LMDG)**：Phase C 与语法模型层的参考
- 本仓库代码：MIT（见 [LICENSE](LICENSE)）

## 文档

- [开发笔记](docs/dev-notes.md)——架构演进史（v6 同步 → v7 修补 → v8 异步）、环境坑账本、运维规程
- [事故报告 I：2060 主机四次全桌面冻结](docs/incident-2026-10-12-2060-host.md)
- [事故报告 II：T10 真实打字冻结与异步重构决策](docs/incident-2026-10-13-t10-typing.md)
- [博客：给输入法加大模型做候选重排](docs/blog.md)
