# 显示型联想（下一词预测）评测：Qwen3.5-2B / 4B vs Spark-X2.5-4B

日期：2026-10-13 · 裁决方式：同一裁判逐条人工判定

## 背景

输入法形态演进的下一步：上屏后在候选区显示「下一个词」联想（librime-predict +
predict.db 静态库，运行时零 LLM）。上线前需选定**烘库引擎**——用哪个模型离线批量
生成下一词。本文是三模型同卷横评。

## 方法

- **掩码卷**：从私有领域公文语料（约 150 篇 / 195 万字，内容不随仓库发布）随机
  切出 200 条前缀，gold 为原文下一个词。切位为随机字符位（非词边界），因此
  「完全命中」是下限指标——gold 本身常有任意性（切在词中间）。
- **统一提示词**：明确要求「只输出填入结尾的下一个词」；贪心解码
  （temperature=0）；`enable_thinking:false`（Qwen 与 Spark 模板均默认开思维链，
  必须显式关闭）。工具：`eval/mask_eval.py`。
- **判分**：`eval/mask_judge.py` 自动分出 完全命中 / 复读机（答案复述上文尾部），
  其余由同一裁判逐条判「合理/不合理」——合理 = 该位置自然 continuation（含跨字
  补全，如前缀尾字「裂」补「隙」成词）。
- 样本文件（`reports/mask_domain_samples.jsonl` 等）因含真实文档内容**不进仓库**，
  在本机 reports/ 目录留档。

## 结果（200 条，CPU 推理，Q4_K_M 同档量化）

| 指标 | Qwen3.5-2B | Qwen3.5-4B | Spark-X2.5-4B |
|---|---|---|---|
| 完全命中 | 1% | 2% | 2% |
| **严格合理（命中+人工判定合理）** | 44% | **76%** | 60% |
| 复读机率 | 30% | 13% | 14% |
| CPU 延迟 p50 / p95 | 278 / 362ms | 636 / 833ms | 683 / 2791ms |
| 许可证 | Apache-2.0 | Apache-2.0 | Apache-2.0 |

判定门槛（预先设定）：严格合理 ≥40% → 联想功能复活。三家均过线，
**烘库引擎选定 Qwen3.5-4B**。

## Spark-X2.5-4B 观察

讯飞系开源 4B：混合注意力（每组 1 层全注意力 + 3 层滑窗），原生 1M 上下文，
llama.cpp 主线已含 `spark2_5` 架构原生支持（早期 1.7B 需民间 fork 的问题已解决），
Q4_K_M 2.6GB 一次加载成功。

- **亮点**：跨字补全质量好（标准编号前缀补出「SL/T」正解、地名河流名补字、
  「堆积物」「精细化需求」等名词位准确）；能写出真实法条句式；复读控制与
  Qwen4B 相当（14% vs 13%）
- **败笔**：数量级灾难（「总库容数字」位补「亿」，同题另一次冒烟却答对「立方米」，
  存在服务端非确定性）；行政分级位漏「三级」答「等级」；政策口号跨字位漏
  「统治理」答「则」（Qwen4B 同位答对）；CPU 延迟长尾明显（p95 2.8s）
- **结论**：其差异化（1M ctx、agent 能力）对下一词预测无增益，本任务低于
  Qwen4B 16 个百分点。GGUF 留档备用，不采用

## 后训练路线（下一步）

1. **先训 4B-LoRA**：领域语料继续预训练 + 4B 蒸馏软标签，目标严格合理 76%→85%+、
   复读 <5%、标准编号类错误清零；掩码集重切到词边界后完全命中冲 20%+
2. **再蒸馏 0.8B**：教师出 top-k token 分布（软标签，非 argmax），KL 蒸馏 + 领域
   CPT；目标 65-75%——接近未训练 4B，用于活推理（CPU ~100ms 级）与个性化快烘
3. 数据：私有领域语料（千万字级，每字位一条样本）+ rime_train.jsonl 飞轮 +
   retract 墓碑（回删样本降权/过滤）

## 复现

```bash
# 服务端（任一被测模型）
tools/llama/llama-server.exe -m models/<model>.gguf --port 8083 -ngl 0 -c 2048 --no-webui
# 评测 + 判分（samples 为本地私有文件，格式见 eval/mask_eval.py docstring）
python -X utf8 eval/mask_eval.py --port 8083 --tag <name> --samples <samples.jsonl>
python -X utf8 eval/mask_judge.py reports/mask_<name>.txt            # 自动部分
python -X utf8 eval/mask_judge.py reports/mask_<name>.txt --verdicts "RRW..."  # 人工回填
```
