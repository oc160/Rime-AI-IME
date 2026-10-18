## Unreleased

- **击键延迟修复：lua filter 惰性枚举**——原版第一行把翻译器候选流全量抽干，
  单字母 preedit 实测生成 1660+ 候选（造句组装+单字+符号），大写 A 每键 650ms、
  小写 a 每键 ~150ms。改为 `local next_fn, st = input:iter()` 只取前
  K = max(max_cands, max_rerank_pos) 个，其余 `for c in next_fn, st` 流式直通：
  A → 2.5ms，候选/翻页行为零变化（tools/probe_win 回归验证）。
  坑：librime-lua 的 iter() 返回 (函数,状态) 对，只接第一个值手动调用会报
  `LuaTranslation::Next error: bad argument #2` 并吞掉全部候选
- **领域词库入真词典（rime/custom_domain.dict.yaml）**：331 词 + 多音字双码，
  经 import_tables 并入 rime_ice。custom_phrase 的编码列是原始字符串匹配
  （只适合自造缩写码），写全拼带空格编码会"抢前缀、全拼永不命中"——
  完整事故记录见 docs/domain-dict-and-latency.md
- **补全候选关闭**（translator/enable_completion: false）：`~剩余拼音` 提示不进候选
- **命令行全量部署 tools/probe_win/deploy_live.py**：ctypes 调 rime.dll
  start_maintenance(full_check=1)。WeaselDeployer /deploy 是快速通道，
  词表源码变更不触发重编译；且会话级引擎不随部署广播刷新，需打断当前输入
  或重启 WeaselServer
- F4 方案选单「AI重排」运行时开关（switches/@next ai_rerank，默认开）上线

# Changelog

## v0.1.0（2026-10-13）

首个功能版本：异步 sidecar 架构 + LoRA 任务微调全管线 + 首个微调模型。

### 稳定性说明

早期同步版本（v6 及之前）曾在两台机器上造成系统级 UI 冻结。v8 异步重构后，
击键路径仅含微秒级文件读写，推理进程崩溃不再影响打字——多台机器连续使用
（含真实打字、隔离回放压力测试、开机关机循环）均零冻结。详细取证与架构
决策见 `docs/incident-2026-10-13-t10-typing.md`。

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

## 2026-10-18: post-commit prediction shell + heap-traversal root cause fix

**Prediction v2 (predict-v2 model wired into the shell)**
- `tools/pred_win/pred_sidecar.py`: resident sidecar, file-handshake
  (`pred_req.txt` / `pred_resp.txt`), owns llama-server lifecycle
  (CPU-only, `-t 4`, spawned BELOW_NORMAL priority so typing always wins),
  warmup query, self-heal on 2 consecutive ask failures.
- Prompt/decode recipe identical to the offline acceptance run
  (temperature 0, max_tokens 32, thinking off, repeat-filter per HANDOFF §4).
- Model word pinned to slot 1 (`智`), table words tail (`联`), full fallback
  ladder to the bigram table when the model is down.

**Prediction shell hardening (v9-v11)**
- Arm only on genuinely NEW commits (PRED_SEEN) — weasel focus-loss
  ClearComposition used to re-arm the trigger char instantly.
- CJK-only arming (first byte E4-E9): fullwidth punct like （ no longer
  ghost-arms; predictions only load when the table actually has entries.
- Adaptive rhythm gate: arm only when the commit key gap exceeds
  2.5× the rolling median of the last 10 inter-key intervals
  (clamped 0.35-0.9s); flow typing never arms. Digit chains exempt.
- Space dismisses (no quick-select); digits valid only within 6s TTL
  (stale invisible compositions after same-document clicks can't eat keys).

**Zero-blocking keystroke path**
- Config read once per session; sidecar response file read only when
  requests are pending; rerank/predict queries fire only at real pauses
  (inter-key gap ≥ 0.45s) — zero queries during flow typing.

**Root cause of the residual "sticky typing" (the big one)**
- Symptom: ~127ms per keystroke INSIDE the engine (M0/M1/M2 log-timestamp
  instrumentation, flag-gated via `pred_measure.flag`, kept in rime.lua).
- Bisected to the bigram pairs table parsed into lua objects: librime-lua
  walks the whole lua heap on every keystroke; 113k lines as ~500k small
  objects cost +120ms/key. Same data as ONE string: +0ms.
- Fix: pairs stay a single string; lookups are plain byte `find` + 64-key
  LRU cache, only ever executed at pauses/arm time, never on the hot path.
- Result: 127ms → 1.6ms/key, identical to a no-lua build.
