# Phase A 状态快照（2026-10-13 03:20 · 收工定稿）

一句话：**v8 异步架构上线并通过真实打字验证（零冻结）；训练暂停于 step 475（loss 0.074）；
开机自启已注册；Phase C 底座候选更新为 chinese-ime-lm。**

## 明日作战计划（定稿）

### 上午（大部分自动）
1. **零操作**：开机 → HKCU Run 自启链自动拉起 llama-server + sidecar + 缓存预热 + WeaselServer
2. **恢复训练**：`pythonw watcher_train.py` → 0.8B v1（60k×2ep，~4.5h，每500步断点保存）
   → 自动链：LoRA 合并 → GGUF 转换 → 40 条 bench 验收（目标 ≥75%，基线 50%）
3. **并行（训练期间 CPU 活）**：
   - 27B Bonsai 上考场（40 条 bench，判教师资格：考过 80% → v2 教师的教师）
   - chinese-ime-lm 4M/25M 权重下载（HF，Apache-2.0）
   - 黄金集生成：冷门诗词随机采样 500-1000 联 + 分层覆盖 + 与 bench/train 三方互斥

### 下午
4. 0.8B v1 验收过线 → 换打字端点（改 autostart_config.json + 重启 llama-server，**输入法不动**）
5. **一次部署升级包**：置信度门槛（sidecar 读 logprob，低置信不干预）
   + max_preedit_len 10→30（整句候选重排）+ 腾讯词库恢复 + switcher 加「AI重排」开关
6. 黄金集烟测（0.8B v1 vs 4M/25M 同台）→ 预热 → **交接用户实测**
7. **GitHub 发布**（oc160/Rime-AI-IME）：v8 最终代码 + 训练成果数字
   + 隐私脱敏硬闸（*.log/rime_train.jsonl/backup_*/主机名/第三方模型名绝不上传）
   + CHANGELOG + v0.1.0 tag + 致谢补 chinese-ime-lm 与 LIME
8. Phase C 立项判据：chinese-ime-lm 黄金集成绩 vs 蒸馏 0.8B → 直接采用 / 自训 / PinyinGPT 备胎

### 今晚新增结论（设计讨论存档）
- 终态=打分器（数学上无第三形态）；打分器是全架构最可替换部件，护城河=飞轮+v8架构+rime生态+蒸馏管道
- Phase C 档位映射：②sidecar 档可跑约束 beam（B 形态），③进程内 1-5ms 档只能是批量打分（A 形态）
- 蒸馏对象必须领域对口（否决 Jev：无中文能力+专有无蒸馏面）
- chinese-ime-lm 的实验结论三连命中我们的设计（词典命中不动/格子+打分合体/直接解码不如引擎）


## 今晚完成清单

| # | 项 | 结果 |
|---|---|---|
| 1 | Kit 评审 + 修复（v7：熔断盲区/超时默认/死代码/解析；gen_synthetic v2：倒排索引+偏移 bug+多音字；bench/replay 路径自适应） | ✅ 全部完成 |
| 2 | 现状测试：脚本编译/gen 冒烟/回放基线复现（B 列逐项一致）/bench 全量 40 | ✅ 通过 |
| 3 | 基线坐实 | 2B Q4 single **80%**(32/40 完整)、0.8B Q8 single **50%** |
| 4 | 数据：wiki 713MB → 15 万样本（泄漏剔除 2011）→ train 151832/val 3098 | ✅ |
| 5 | 首次打字测试（v7 同步架构） | ❌ AppHang（击键路径同步阻塞+双服务器 churn）→ 完整回滚+根因报告 |
| 6 | **v8 异步重构**：击键路径只剩文件读写，sidecar 独立进程推理 | ✅ 隔离验证 T1（无 sidecar 零开销）T2（122 请求全链） |
| 7 | v8 上线部署（单实例纪律+预热交接） | ✅ 用户实测零冻结，18 请求/13 次重排生效/18 条 USER_PAIR 入库 |
| 8 | 训练（60k×2ep, bs24+ckpt+logits_to_keep, 显存 4.2GB） | ⏸ 停在 step 475/5000, loss 0.074（关机暂停） |

## 线上状态（关机前）

- 小狼毫 = rime-ice + v8 lua（enabled:true，rpc 目录 `%APPDATA%\Rime\llm_rpc`），方案列表仅 rime_ice
- 腾讯大词库已裁（`rime_ice.dict.yaml` 移除了 tencent import 行——恢复时改回即可）
- **关机后**：llama-server/sidecar 不自启 → 小狼毫自动降级为纯词库模式（v8 安全直通，打字无任何影响）；重排恢复只需我起两个服务，不碰输入法
- 完整原始配置备份：`<项目目录>\backup_rime_*`

## 开机自启（已注册，2026-10-13 02:35）

- 注册方式：HKCU Run 键 → `pythonw %USERPROFILE%\ime_boot\autostart_boot.py`（用户级免提权；schtasks 被本机 UAC 策略拒绝）
- 自启链：起 llama-server(8081) → ping 探测并起 sidecar → 预热 build 文件缓存 → 预拉起 WeaselServer 进程
- 实测依据：方案加载仅 0.06s，首切卡顿元凶是进程启动+冷盘读，均已前置到登录时
- **换模型只改 `autostart_config.json` 的 gguf 字段**，注册表和脚本都不动
- 兜底：任一服务启动失败 → v8 安全直通，打字无影响（日志 reports/autostart.log）

## GitHub 开源仓库（用户指定：https://github.com/oc160/Rime-AI-IME）

仓库现状：MIT、双语 README、目录 docs/eval/rime/tools/training、5 commits——结构已对齐 v8，内容待同步今晚最终版。

明天下午的同步清单：
1. **代码同步**：v8 最终版（rime.lua 含 rpc_dir、sidecar.py 含 ping 探测、autostart 体系、
   train_sft（logits_to_keep/左对齐/断点保存）、prep_train_data（防泄漏+增强）、bench/replay 自适应、gen_synthetic v2）
2. **训练成果**：0.8B v1 的 bench 数字 → 更新 README 评测表
3. **隐私脱敏（上传前硬闸）**：不上传 rime_train.jsonl / *.log / backup_rime_* / replay_work/ /
   corpus/ / data/ / models/ / 任何含主机名、用户路径、第三方 9B 模型名的文件；
   docs/ 事故报告过一遍脱敏（主机名→<HOST>，用户名→<USER>，无关第三方模型名删去）
4. **开源标准**：.gitignore 补齐 → CHANGELOG.md → release tag（v0.1.0：async sidecar 架构首发）→
   commit message 规范（一处一事）

## 明早恢复步骤（按序）

```
1. 起 llama-server: tools/llama/llama-server.exe -m models/Qwen3.5-0.8B-Q8_0.gguf --port 8081 -t 6 -ngl 0 --jinja --no-webui
2. 起 sidecar:      pythonw sidecar.py --log reports/sidecar_live.log   （默认目录即 lua 的 rpc 目录）
3. 恢复训练:        watcher_train.py（自动：训练→GGUF 转换→bench 40 条验收，日志 reports/watcher.log）
4. 验收 ≥75% 后：
   a. llama-server 换新模型 qwen35-08b-ime-v1-Q8_0.gguf（重启服务即可，输入法不动）
   b. rime_ice.custom.yaml: max_preedit_len 10→30（整句候选重排，解决"名句在第2候选"类问题）
   c. 恢复腾讯词库（rime_ice.dict.yaml 加回 import 行 + tar 解出 tencent.dict.yaml）
   d. 一次重新部署（单实例纪律：先杀 WeaselServer/Deployer，预热后再交接）
5. 黄金集烟测（见下）通过才交用户
```

## 黄金集设计（按用户要求修订：冷门诗词、防过拟合）

- **不用**名句（人生若只如初见等高频句 = 拟合词库/模型熟面孔，无泛化意义）
- 从冷门诗词语料**随机采样**（500-1000 联，全唐诗/宋词随机抽取，非人工挑选）
- 经 gen_synthetic 同音组挖矿生成样本，与 40 条 bench、train/val **三方互斥**（防泄漏过滤复用 prep_train_data 的判据）
- 分层覆盖：日常口语 / 新闻书面 / 专业词 / 冷门诗词 按比例
- 用途：每轮换模型的**回归闸门**（不达标不换），不针对单句调参

## 现行技术路线（三层 + 演进）

```
① 候选生成层  rime-ice 词库（雾凇拼音，整句组词 + 字词候选 Top5）
② 重排层      librime-lua v8 异步 filter —— 击键路径仅文件读写（微秒级）
              ↕ 文件握手 llm_rpc/
              sidecar 进程 → llama-server（OpenAI 协议）→ 结果下一键生效
              （sidecar/推理死掉 = 安全直通，打字无感）
③ 模型层      Qwen3.5-0.8B + LoRA(r32 全投影) SFT
              数据 = wiki×词库同音组挖矿（gen_synthetic v2）
              验收 = 40 条歧义 bench（50%→目标≥75%）+ 黄金集回归闸门
飞轮          USER_PAIR 真实选择偏好自动入库（DPO/Phase B 个性化原料）
演进          Phase B 每日个性化 LoRA → Phase C 30-100M 专用打分器（ONNX 进程内，IPC 归零）
```

## 关键文件

- 代码：`gen_synthetic.py` `prep_train_data.py` `train_sft.py` `sidecar.py` `watcher_train.py` `bench_rerank.py` `replay_eval.py` `serve_ime.py`
- 线上件：`rime/rime.lua`（v8）`rime/rime_ice.custom.yaml`（v8）
- 报告：`reports/incident_2026-10-13_t10_typing.md`（冻结根因）`reports/bench_summary.md`（40 条基线）
- 数据：`data/train.jsonl` `data/val.jsonl`（152k，防泄漏已做）`corpus/wiki_zh.txt`
- 快照包：`ai_ime_v8_snapshot_20261013.zip`（代码+配置+报告，不含大文件）
