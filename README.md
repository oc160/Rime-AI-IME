# rime-llm-reranker

**简体中文** | [English](README.en.md)

**Rime 输入法 LLM 候选词重排 + 联想，双端**：词库出候选，本地模型根据上下文把最合适的词顶到第一候选；上屏后预测下一个词作为联想提示——打字路径零阻塞。

| 端 | 方案 | 文档 |
|---|---|---|
| **Windows**（Weasel/小狼毫 + 雾凇拼音） | rime.lua v8 异步架构（sidecar 文件握手） | 本 README 下文 |
| **Android**（Trime/同文 v3.3.12） | 进程内 C++ 推理（4M 模型 libime.so），支持九宫格 | [`android/BUILD.md`](android/BUILD.md) |

两端共享同一套重排核心（惰性候选枚举、异步查询/回收、置信度门槛、数据飞轮），修复互向移植。

基于 [FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)（macOS 版，MIT）移植起步，架构已重构为异步 sidecar，并附带完整的任务微调训练管线（重排 + 联想两条线）。

> [下载预训练模型](https://github.com/oc160/rime-llm-reranker/releases/tag/v0.1.0-model)（重排 0.8B / 25M 学生 / 联想 0.8B，共三个 GGUF）

> **状态**：v1 正式版。双端（Windows / Android）真机长期使用中；击键路径零阻塞架构经多机实测验证。推理服务异常时自动回退词库原生排序，打字无感。方案选单（F4/Ctrl+`）随时「AI关/AI开」。

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

> **架构动机**：Windows TSF 下应用在每次击键上同步等待输入法返回，击键路径对延迟零容忍——所以一切模型查询/推理都必须离开击键路径（异步文件握手 / 后台线程），这是本架构的第一设计原则。

## 使用提示

1. 部署后首次切换有一次性冷加载（数秒），可先在记事本预热。
2. 部署前建议关闭正在运行的 WeaselServer / WeaselDeployer（多实例会互相改写配置）。
3. 主力环境 Windows 10/11 + Weasel 0.17.x、Android + Trime 3.3.12；推理服务挂掉自动回退，无需人工干预。

## 快速开始

前置：[小狼毫 Weasel](https://github.com/rime/weasel) + Python 3.10+ + [llama.cpp](https://github.com/ggml-org/llama.cpp)（llama-server）+ [Release 里的 GGUF 模型](https://github.com/oc160/rime-llm-reranker/releases/tag/v0.1.0-model)。

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

- [Phase A 状态档案](docs/phase-a-status.md)
