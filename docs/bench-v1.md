# 候选词重排模型横评（CPU · -ngl 0 · 10线程 · enable_thinking=false）

- 用例：40 条拼音歧义句（含无上下文/语境反转/成语接续/水利工程专业词）
- 模式：list=原版Top5列表输出(max_tokens=20)；single=只输出最优1词(max_tokens=6)
- 命中=模型首选与期望一致；延迟含HTTP往返

| 模型 | 模式 | 命中率 | 空回复 | mean | P50 | P95 |
|---|---|---|---|---|---|---|
| <项目目录>\models\qwen35-08b-ime-v1-Q8_0.gguf | list | 38/40 (95%) | 0 | 297ms | 294ms | 343ms |
| <项目目录>\models\qwen35-08b-ime-v1-Q8_0.gguf | single | 38/40 (95%) | 0 | 276ms | 275ms | 315ms |
