# rime-llm-reranker-win

**简体中文** | [English](README.en.md)

**Windows 版 Rime 输入法 LLM 候选词重排 + 联想**：词库出候选，本地模型根据上下文把最合适的词顶到第一候选；上屏后预测下一个词作为联想提示——打字路径零阻塞（异步文件握手架构）。

基于 [FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)（macOS 版，MIT）移植起步，架构已重构为异步 sidecar，并附带完整的任务微调训练管线（重排 + 联想两条线）。

> [下载预训练模型](https://github.com/oc160/rime-llm-reranker-win/releases/tag/v0.1.0-model)（重排 0.8B / 25M 学生 / 联想 0.8B，共三个 GGUF）

> ⚠️ **实验性项目**：早期同步版本曾造成系统级冻结（见[事故报告](docs/incident-2026-10-13-t10-typing.md)）；现行 v8 异步架构已消除该风险（多机实测零冻结），但仍建议保留第二个输入法作为退路。方案选单（F4/Ctrl+`）有「AI关/AI开」开关。

## 架构

```
① 候选生成层   rime-ice 词库（雾凇拼音）—— 整句组词 + 字词候选 Top5

② 重排层       rime.lua v8 异步 filter（击键路径零阻塞）
               打完词 → 写 req.json 立即返回 → 下一键读 resp.json 应用重排
                    ↕ llm_rpc/ 文件握手
               sidecar 独立进程（双后端）：
               ├── llama-server（OpenAI 协议，0.8B 重排模型）
               └── 本地 25M 打分器（进程内 nn.Module，无需 llama-server）
               ◆ 推理服务挂掉 = 安全直通词库顺序，打字完全无感
               ◆ 置信度门槛：低置信不干预词库（防误推）

③ 联想层       上屏后 sidecar 异步预测下一个词
               0.8B 联想专用模型（im_end 终止训练，复读率 0%）
               候选栏尾部追加联想词（方案 A 交互）

④ 数据飞轮     用户真实选择(USER_PAIR)自动落盘
               回删行为自动生成 retract 墓碑 → 训练侧剔除坏样本
               个性化训练数据持续积累 → 夜间 LoRA → 黄金集回归闸门 → 达标换模型

⑤ 自启链       开机自动：推理服务 + sidecar + 文件缓存预热 + WeaselServer 预拉起
```

> **架构动机（真实事故）**：早期版本把同步模型查询（~300ms）放在击键路径上，隔离测试全绿，真人打字 40 秒后 explorer 挂起——Windows TSF 下应用每次击键都同步等输入法返回。完整取证见 [docs/incident-2026-10-13-t10-typing.md](docs/incident-2026-10-13-t10-typing.md)。

## 安全使用须知

1. 保留至少两个输入法（如微软拼音 + 小狼毫），`Win+空格` 随时切换。
2. 部署后第一次切到小狼毫会有一次性冷加载（数秒），先在记事本预热。
3. 部署前杀干净 WeaselServer / WeaselDeployer 进程（多实例会互相改写配置）。
4. 在 Windows 10 + Weasel 0.17.0 上开发测试；其他环境未验证。

## 快速开始

前置：[小狼毫 Weasel](https://github.com/rime/weasel) + Python 3.10+ + [llama.cpp](https://github.com/ggml-org/llama.cpp)（llama-server）+ [Release 里的 GGUF 模型](https://github.com/oc160/rime-llm-reranker-win/releases/tag/v0.1.0-model)。

```bash
# 1. 取得雾凇拼音（GPL-3.0 项目请自行获取）
git clone --depth 1 https://github.com/iDvel/rime-ice.git tools/rime-ice

# 2. 启动推理服务（CPU，端口 8081）
tools/llama/llama-server.exe -m models/qwen35-08b-ime-v1-Q8_0.gguf \
    --port 8081 --alias main -c 2048 -ngl 0 -t 6 --jinja --no-webui

# 3. 启动 sidecar（置信度门槛 0.6，低于此值不干预词库）
python tools/sidecar.py --conf-threshold 0.6 --log sidecar.log

# 4. 安装输入法配置
#    rime/rime.lua + rime_ice.custom.yaml + tools/rime-ice 方案文件 → %APPDATA%\Rime\
#    default.custom.yaml 的 schema_list 只留 rime_ice → 杀干净 Weasel 进程后重新部署

# 5. 预热：记事本 → Win+空格 切小狼毫 → 等一次性冷加载 → 打一个词
```

确认工作：`%APPDATA%\Rime\rime_llm.log` 出现 `v8-async started` 和 `RPC_OUT/RPC_IN`；`sidecar.log` 出现推理延迟与置信度记录。

**不需要 llama-server？** sidecar 支持本地 25M 打分器直载（无外部依赖，~73ms/题 CPU）：
```bash
python tools/sidecar.py --backend local \
    --model models/ime-lm/student-25M-v2/sentence-model.safetensors
```

## 训练你自己的模型

### 重排模型（判别式五选一）

```bash
# 1. 造数据：语料 × 词库同音组挖矿
python training/gen_synthetic.py corpus.txt -n 150000 -o cases.jsonl

# 2. 防泄漏过滤 + 切分（与 eval/bench 评测集互斥）
python training/prep_train_data.py cases.jsonl -o data/

# 3. LoRA SFT（提示词与部署逐字一致；大词表显存优化：左 padding + logits_to_keep）
HF_HOME=./hf_cache python training/train_sft.py \
    --train data/train.jsonl --val data/val.jsonl \
    --out-adapter adapter_v1 --out-merged merged_v1 --epochs 2

# 4. 转 GGUF + 40 条基准验收（基线：裸 0.8B = 50%，2B = 80%）
python tools/llama.cpp/convert_hf_to_gguf.py merged_v1 --outtype q8_0 --outfile models/ime-v1.gguf
python eval/bench_rerank.py ime-v1.gguf
```

### 联想模型（生成式下一词预测）

```bash
# 1. 词边界掩码采样（rime-ice 词典最大正向匹配，gold 是完整词）
python training/gen_mask_samples.py --corpus domain.txt -n 500000 --dict-dir tools/rime-ice/cn_dicts

# 2. 0.8B LoRA 蒸馏（im_end 终止标记使模型输出单词即停）
python training/train_predict_distill.py \
    --train data/predict_mix_train.jsonl --val data/predict_mix_val.jsonl \
    --out-adapter adapter_predict --out-merged merged_predict --epochs 2
```

### 25M 小模型（重排热启动微调）

```bash
# 用 chinese-ime-lm 25M 权重热启动 + listwise 排序损失微调
python training/train_student.py \
    --data data/distill_v1.jsonl --out models/ime-lm/student-25M-v2/sentence-model.safetensors
```

与其他 GPU 任务共用机器？`tools/watcher_train.py` 会等占用方下线后自动跑完整链。

## 评测

| 模型 | 40 条 bench | 黄金集 282¹ | 体积 | 推理 |
|---|---|---|---|---|
| Qwen3.5-0.8B 零样本 | 50% | — | 774MB | 300ms |
| Qwen3.5-2B 零样本 | 80% | — | 1.2GB | 875ms |
| chinese-ime-lm 25M（零样本） | 57% | 45% | 25MB | 5ms |
| 本地 27B（仲裁档） | 95% | 80% | 6.8GB | 350ms |
| **0.8B + LoRA v1（重排）** | **95%** | **91%** | 774MB | 300ms |
| **25M 学生（listwise 蒸馏）** | **82%** | **80%** | **25MB** | **5ms** |

¹ 黄金集 = 与训练/评测三方互斥的防过拟合回归闸门（冷门诗词随机采样 + 未训练书面语分层）。分布所有权（候选由 rime 词库同音组生成）是微调收益的来源。

**联想评测**（300 条词边界验收集）：

| 模型 | 严格合理 | 复读率 |
|---|---|---|
| 0.8B 基座 | 22% | 55% |
| **0.8B + predict-v2（本管线）** | **~65%** | **0%** |
| Qwen3.5-4B | 76% | 13% |

评测工具：`eval/bench_rerank.py`（40 条重排）、`eval/replay_eval.py`（隔离回放引擎——ctypes 加载 rime.dll，完全不碰前台）、`eval/predict_eval.py`（联想）。

## 路线图

- **Phase A**：✅ 已完成（重排 95% bench / 91% 黄金集已上线；联想 v2 复读 0% 达标；25M 学生 80% 可部署）
- **Phase B**：每日滚动个性化（USER_PAIR 飞轮 + retract 墓碑 → 夜间 LoRA → 黄金集闸门 → 达标换模型）
- **Phase C**：25-50M 进程内打分器（[chinese-ime-lm](https://github.com/metasequoiaime/chinese-ime-lm) 架构 + 本项目分布数据微调），ONNX 加载——IPC 归零，重排 1-5ms；联想混合架构（首字 + 词典 + 打分）替代生成式
- **Phase D**：自有预训练底座（当飞轮数据积累到 10 亿字符时从零设计，当前用 chinese-ime-lm Apache-2.0 权重）

## 致谢与许可证

- **[FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)**（MIT）：本项目的前身。核心思路「LLM 裁判 Top5 候选」与提示词格式源自它
- **[iDvel/rime-ice](https://github.com/iDvel/rime-ice)**（GPL-3.0）：雾凇拼音词库与方案。**不入本仓库**，请自行获取
- **[metasequoiaime/chinese-ime-lm](https://github.com/metasequoiaime/chinese-ime-lm)**（Apache-2.0）：25M/4M 字符级打分器权重与格式规范——本项目学生模型的底座
- **[Transformers4IME](https://github.com/VisualJoyce/Transformers4IME) / [RIME-LMDG](https://github.com/amzxyz/RIME-LMDG)**：Phase C 早期参考
- 本仓库代码：MIT（见 [LICENSE](LICENSE)）

## 文档

- [事故报告 I：2060 主机四次全桌面冻结](docs/incident-2026-10-12-2060-host.md)
- [事故报告 II：T10 真实打字冻结与异步重构决策](docs/incident-2026-10-13-t10-typing.md)
- [Phase A 状态档案](docs/phase-a-status.md)
