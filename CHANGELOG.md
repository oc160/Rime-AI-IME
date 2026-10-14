# Changelog

## Unreleased

- **回删追踪（retraction tombstone）**：上屏后 5 秒内连按退格删完整词，自动记录
  retract 墓碑（引用原 USER_PAIR 的 seq）到 rime_train.jsonl——训练时对被回删的
  选择降权/过滤。yaml 增挂 lua_processor@llm_retract_tracker；训练日志字段增加
  ts/seq，USER_PAIR 与 retract 通过 seq 关联
- **候选窗口 5→9 / 重排位 5→9**（线上验证）：更宽的数据飞轮收集面
- **显示型联想评测**（docs/prediction-mask-eval.md）：Qwen3.5-2B/4B vs
  Spark-X2.5-4B 三家同卷（200 条领域掩码，统一提示词+同一裁判），
  严格合理率 44% / 76% / 60%——烘库引擎选定 Qwen3.5-4B；新增
  eval/mask_eval.py + eval/mask_judge.py 统一评测与判分工具
- 后训练路线确定：先 4B-LoRA（85%+ 目标）再 0.8B 蒸馏（活推理/个性化快烘）

## v0.1.0（2026-10-13）

首个功能版本：异步 sidecar 架构 + LoRA 任务微调全管线 + 首个微调模型。

### 架构
- **v8 异步重排**：击键路径仅微秒级文件读写（req.json/resp.json 握手），推理在独立
  sidecar 进程——推理由同步 curl 改为异步文件握手后，冻结类问题在架构上不可能发生
  （实测：推理服务缺席时打字耗时与无 filter 完全一致）
- 双后端 sidecar：OpenAI 兼容端点（llama-server）| 进程内 25MB 判别式打分器
- 开机自启链（推理服务 + sidecar + 文件缓存预热 + WeaselServer 预拉起）
- 置信度门槛：首选 token 概率低于阈值不干预词库（防误推）；置信度随 USER_PAIR 入库
- 方案选单「AI 开/关」运行时开关；max_preedit_len 可调（整句候选重排）
- 数据飞轮：USER_PAIR 真实选择偏好自动收集（enabled:false 也收集）

### 训练管线
- `gen_synthetic.py`：词库同音组挖矿（倒排索引 + pypinyin 多音字 + 上下文偏移修复）
- `prep_train_data.py`：与 bench 用例三方互斥的防泄漏过滤 + 无上下文增强
- `train_sft.py`：LoRA SFT（logits_to_keep 大词表显存优化、左对齐、断点保存、最佳适配器）
- `label_teacher.py`：教师批量标注（GPU batch，一致率过滤）
- `ime_lm_scorer.py`：metasequoiaime/chinese-ime-lm 打分器的 Python/torch 移植
- `replay_eval.py`：ctypes 加载 rime.dll 的隔离按键流回放评测（A/B 门禁）

### 首个微调模型（Qwen3.5-0.8B + LoRA r32）
| 评测 | 基线(零样本) | 微调后 |
|---|---|---|
| 40 条难句 bench | 50% | **95%** |
| 黄金集 282（heldout 书面 260） | — | **94%** |
| 黄金集（冷门诗词 22） | — | 55%（v2 数据增强目标） |
| 首选置信度 | 0.35-0.72 | 0.97+ |

### 工程教训（详见 docs/incident-2026-10-13.md）
- 击键路径同步阻塞 = 应用雪崩（TSF 同步 COM）；异步文件握手根治
- 部署器进程堆积会互相改写配置；单实例纪律 + rime.dll 直刷部署（10 秒）
- 评测与部署的解析语义必须逐字对齐

### 致谢
- [FuJiK0/rime-llm-reranker](https://github.com/FuJiK0/rime-llm-reranker)（macOS 原版）
- [iDvel/rime-ice](https://github.com/iDvel/rime-ice)（雾凇拼音）
- [metasequoiaime/chinese-ime-lm](https://github.com/metasequoiaime/chinese-ime-lm)（打分器架构参考与权重，Apache-2.0）
- [Exploring and Adapting Chinese GPT to Pinyin Input Method](https://arxiv.org/abs/2203.00249)（Phase C 底座候选）

### 联想副本（predict-v2）
- `training/train_predict_distill.py`：0.8B 联想专用 LoRA 微调
  - 底座：Qwen3.5-0.8B 基座（非 ime-v1——零迁移已实证）
  - 数据：领域+wiki 1:1 词边界样本 + 防复读过滤
  - 关键修复：词尾追加 im_end 终止标记（v1 不刹车的根因）
  - 结果：复读 0%（基线 55%），词级输出，领域方向正确
- 评测：`eval/predict_eval.py` + 验收集 `eval_domain_v2_boundary.jsonl`（300 条）
