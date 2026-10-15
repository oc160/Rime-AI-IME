# 领域词库 v2：从 custom_phrase 迁入真词库（事故修复）

日期：2026-10-15

## 事故回顾（v1 = custom_phrase 挂载，错误机制）

用户反馈两个症状（打 `meibian` 想 output 没变）：
1. 打 `bian` 想要「变」，前四位被 边墩~dun / 边梁~liang / 边梁翼板 / 边墙~qiang 霸占，变 掉到第 7
2. 打全 `diaohongkurong`/`tiaohongkurong` 都出不来「调洪库容」，只有 掉红库容/条红库容

隔离副本探针（scripts/probe_bian.py，无 userdb、无 sidecar）完全复现，机理：

- custom_phrase（tabledb/stabledb）的编码列是**原始字符串**匹配：官方模板 35 条全是
  `u`/`ig`/`umu` 这类自造缩写码，文件头明言"编码可以随便起，不限于拼音"、
  "只建议固定非完整拼音的编码"。
- 我写入的带空格编码 `bian dun`：
  - 打 `bian` → 是 `bian dun` 的字符串前缀 → 生成补全候选 `~dun`，且 initial_quality=99 → 置顶霸占
  - 打全 `biandun` → 第 5 字符 `d` 对不上编码里的空格 → **永远无命中**
- 结论：custom_phrase 只适合缩写码置顶，**不适合领域词汇入库**。

另发现两处读音错误：调洪库容、调蓄 的「调」均为 tiáo，v1 只写了 diao 码。

## v2 修复（真词库，prism 音节匹配）

三件套（staging 见 `rime_stage/`）：
1. `custom_domain.dict.yaml`：333 行（331 词 + 调洪库容/调蓄 diao/tiao 双码），weight=50000
2. `rime_ice.dict.yaml`：import_tables 末尾追加 `- custom_domain`（雾凇官方推荐扩展位；
   注意 **import_tables 不递归**，不能经聚合器套聚合器——首版 rime_ice_plus 因此丢掉全部基础词表，单音节 bian 空候选）
3. `custom_phrase.txt`：还原为官方原版 35 条（.bak 备份仍在）

## 离线验证（scripts/probe_stage.py，隔离副本）

| 输入 | v1（custom_phrase） | v2（真词库） |
|---|---|---|
| bian | 1边墩~dun 2边梁 3边梁翼板 4边墙 … 7变 | 边/变 单字回归前列，无 99 权重抢占 |
| biandun | 1边顿（边墩不出现） | **1边墩** |
| diaohongkurong | 1掉红库容 | **1调洪库容** |
| tiaohongkurong | 1条红库容 | **1调洪库容** |
| jiaoshen | 1较深（校审不出现） | **1校审** |
| gaocheng | 3高程 | 高程前列 |

真词库额外收益：领域词可参与造句/造词、可被 userdb 动态调频（custom_phrase 完整编码不参与造词）。

## v3（同日 15:44）：用户反馈两项调整

1. **补全候选整体关闭**：`rime_ice.custom.yaml` 补 `"translator/enable_completion": false`。
   rime 没有"补全排到末尾"的档位，只有开/关；关掉后 `~剩余拼音` 提示不再出现在候选里
   （含雾凇自带的 水位~wei 等），全拼打完才出词。首音节 tiao/shui/bian 探针均为纯单字。
2. **调洪库容/调蓄 勘误**：调=tiáo，删除 diao 双码，仅保留 tiao 码
   （`diaohongkurong` 不再出 调洪库容，`tiaohongkurong` 第 1）。

table.bin 60,664,120 → 60,664,056（-64B ≈ 2 条 diao 码）。

## 部署

- `WeaselDeployer.exe /deploy` 是**快速通道**：dict 源码变更不触发重编译（本次实测静默跳过，
  日志无任何 dict 编译行）。托盘菜单"重新部署"等价于全量校验。
- 命令行全量部署：`python scripts/deploy_live.py`（ctypes 调 rime.dll
  `start_maintenance(full_check=1)`），8 秒完成 prism+table 重编译；
  之后再跑一次 `WeaselDeployer /deploy` 让运行中的 WeaselServer 换用新表（engine 重启）。
- 2026-10-15 15:41 部署完成：table.bin 60,656,260 → 60,664,120 字节（+7.8KB ≈ 333 条），
  engine 已重启加载新表。

## v4（16:33）：击键延迟修复 —— lua filter 惰性枚举

用户反馈"打 AI 会卡一下"。实测定位（scripts/probe_lua_fix.py + rime_profile.lua 分段计时）：

- **根因**：`llm_reranker` filter 第一行 `for cand in input:iter()` 把翻译器候选流
  **全量抽干**。rime 本身惰性按页生成（1-2ms），但单字母 preedit 实际产生
  1660+ 个候选（造句组装+单字+符号），全量枚举耗时：大写 A = 420-670ms、
  小写 a = 94-163ms——**不止打 AI，每个拼音首字母都在吃这个延迟**。
- **修复**：`local next_fn, st = input:iter()` 接住 (函数,状态) 两返回值，
  只手动取前 K = max(max_cands, max_rerank_pos) = 9 个，其余用
  `for c in next_fn, st do yield(c) end` 流式直通，零处理。
  （坑：iter() 返回的是有状态迭代对，只接第一个值再手动调用会报
  `LuaTranslation::Next error: bad argument #2 expected Translation`，候选全空。）
- **实测**：A 605/670ms → **2.5ms**；a 148/163ms → **3ms**；
  候选回归零变化（biandun→1.边墩、jiaoshen→2.校审、翻页直通完整）。
- 原版 v8 全量抽干 lua 在 tools/rime-llm-reranker-win 仓库留档。
- 部署：rime.lua 替换 + WeaselServer 重启（16:33，PID 8608，日志 0 错误）。
