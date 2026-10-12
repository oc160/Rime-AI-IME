# -*- coding: utf-8 -*-
"""bench_rerank.py — 输入法候选词重排模型横评（CPU 模式）
自动起停 llama-server（端口 8082，-ngl 0），对每个模型跑 40 条拼音歧义用例 × 2 种提示模式，
输出命中率与延迟统计，写 reports/bench_raw.jsonl 与 reports/bench_summary.md
用法: python bench_rerank.py [model1.gguf model2.gguf ...]
"""
import json, time, subprocess, sys, os, shutil, urllib.request, statistics

_HERE = os.path.dirname(os.path.abspath(__file__))

def _find_root():
    """兼容两种布局：原项目 scripts/ 子目录 与 套件根目录（kit 把脚本放根上）"""
    for cand in (os.path.dirname(_HERE), _HERE):
        for sub in ("tools", "models", "assets"):
            if os.path.isdir(os.path.join(cand, sub)):
                return cand
    return _HERE

ROOT = _find_root()
MODEL_DIR = os.path.join(ROOT, "models")

def _find_server():
    for c in (os.path.join(ROOT, "tools", "llama", "llama-server.exe"),
              os.path.join(ROOT, "tools", "llama.cpp", "build", "bin", "Release", "llama-server.exe"),
              os.path.join(ROOT, "tools", "llama.cpp", "build", "bin", "llama-server.exe")):
        if os.path.isfile(c):
            return c
    return shutil.which("llama-server") or ""

SERVER = _find_server()

def _default_models():
    found = []
    for name in ("Qwen3.5-0.8B-Q8_0.gguf", "Qwen3.5-2B-Q4_K_M.gguf",
                 "LFM2.5-350M-Q8_0.gguf", "MiniCPM5-1B-Q8_0.gguf"):
        if os.path.isfile(os.path.join(MODEL_DIR, name)):
            found.append(name)
    return found

# 命令行模型名可相对 models/ 也可绝对路径
MODELS = [m if os.path.isabs(m) else os.path.join(MODEL_DIR, m) for m in sys.argv[1:]] \
         or [os.path.join(MODEL_DIR, n) for n in _default_models()]
PORT = 8082
URL = f"http://127.0.0.1:{PORT}/v1/chat/completions"

# ── 数据集：(上下文, 拼音, 候选[5], 期望首选) ──────────────
CASES = [
    ("这个问题不能再", "jihua", ["计划", "激化", "极化", "记话", "集画"], "激化"),
    ("下季度项目推进的", "jihua", ["激化", "计划", "极化", "集画", "记话"], "计划"),
    ("", "jihua", ["激化", "计划", "极化", "记话", "集画"], "计划"),
    ("他的话里带着讽刺，两人矛盾进一步", "jihua", ["计划", "极化", "激化", "记话", "集画"], "激化"),
    ("聊天时他只会发", "hehe", ["合合", "和和", "呵呵", "河河", "何何"], "呵呵"),
    ("今天的会议开得很有成", "xiaoguo", ["笑过", "效果", "校过", "小锅", "销过"], "效果"),
    ("这部电影拍得很差，观众哈哈大", "xiao", ["消", "笑", "校", "销", "肖"], "笑"),
    ("经过治疗他的病情已经好", "zhuan", ["专", "转", "赚", "砖", "撰"], "转"),
    ("老板给他涨了工资，收入翻了一", "fan", ["翻", "反", "烦", "凡", "帆"], "翻"),
    ("这项工作责任重大，千万不能出半点差", "cuo", ["错", "措", "挫", "搓", "锉"], "错"),
    ("论文投稿前需要再仔细校对一遍，避免出现错", "wu", ["物", "误", "悟", "雾", "务"], "误"),
    ("我们要从根本上解决这个问", "ti", ["替", "题", "提", "体", "踢"], "题"),
    ("公司今年的营业额创了历史新", "gao", ["告", "稿", "高", "糕", "膏"], "高"),
    ("冬天的哈尔滨非常寒冷，滴水成", "bing", ["兵", "冰", "饼", "病", "丙"], "冰"),
    ("工程验收前要对闸门进行全面的", "jiancha", ["检查", "检察", "拣茶", "剪差", "简察"], "检查"),
    ("水利工程中，闸门主要用来控制水", "liu", ["流", "留", "刘", "榴", "瘤"], "流"),
    ("汛期来临前，水库需要腾空库容提前", "xiehong", ["泄洪", "卸红", "谢宏", "械轰", "屑洪"], "泄洪"),
    ("大坝安全鉴定报告中，需要对工程缺陷进行详细", "miao", ["秒数", "描述", "庙书", "渺述", "妙属"], "描述"),
    ("结构计算必须严格按照最新", "guifan", ["桂帆", "规范", "归返", "轨范", "鬼饭"], "规范"),
    ("试验数据表明，混凝土强度达到设计", "yaoqiu", ["要求", "要囚", "药球", "摇球", "耀秋"], "要求"),
    ("", "yaoqiu", ["药球", "要求", "要囚", "摇球", "耀秋"], "要求"),
    ("老师布置的作业太多了，学生叫苦连", "tian", ["甜", "填", "天", "田", "添"], "天"),
    ("他做事情总是犹豫不", "jue", ["决", "绝", "觉", "诀", "爵"], "决"),
    ("新来的领导讲话水平很高，大家听得津津有", "wei", ["为", "味", "位", "卫", "未"], "味"),
    ("这家餐厅的菜品味道一", "ban", ["搬", "班", "般", "斑", "扳"], "般"),
    ("比赛进行到最后时刻，比分依然不分上", "xia", ["瞎", "虾", "下", "夏", "吓"], "下"),
    ("经过反复修改，方案终于定下来了，大家心里的一块石头落了", "di", ["地", "低", "底", "抵", "弟"], "地"),
    ("他的脾气很倔，九头牛都拉不", "hui", ["回", "会", "汇", "灰", "挥"], "回"),
    ("春天来了，公园里百花齐", "fang", ["防", "放", "方", "房", "芳"], "放"),
    ("环境污染问题日益严重，治理刻不容", "huan", ["缓", "换", "患", "幻", "唤"], "缓"),
    ("", "shijian", ["事件", "时间", "实践", "世间", "识见"], "时间"),
    ("考古学家正在研究这一历史", "shijian", ["时间", "事件", "实践", "世间", "识见"], "事件"),
    ("马克思主义强调理论必须与", "shijian", ["事件", "世间", "实践", "时间", "识见"], "实践"),
    ("他历尽千辛万苦，看透了", "rensheng", ["人声", "人生", "人参", "人胜", "仁生"], "人生"),
    ("课堂上不要交头接耳，影响别人听", "ke", ["客", "科", "课", "刻", "克"], "课"),
    ("医生叮嘱他按时吃", "yao", ["要", "药", "耀", "咬", "腰"], "药"),
    ("今年的粮食产量再创新高，农民喜获丰", "shou", ["收", "手", "首", "守", "瘦"], "收"),
    ("这个问题很复", "za", ["杂", "咂", "砸", "扎", "咋"], "杂"),
    ("", "zhongguo", ["中果", "中国", "忠果", "种锅", "中锅"], "中国"),
    ("黄河是中国的母亲", "he", ["和", "河", "何", "合", "荷"], "河"),
]

# ── 提示模式 ─────────────────────────────────────────────
SYS_LIST = ("你是输入法候选词排序助手。根据对话历史和当前输入，从候选词列表中选出最符合语境的词。"
            "按优先级从高到低列出最多5个候选词，用顿号分隔，不要拼音、数字序号或任何解释。")
SYS_SINGLE = ("你是输入法候选词排序助手。根据对话历史和当前输入，从候选词列表中选出最符合语境的词。"
              "只输出这1个词本身，不要拼音、数字序号或任何解释。")

def build_messages(mode, ctx, pinyin, cands):
    c = "、".join(cands)
    if ctx:
        if mode == "list":
            u = (f"对话历史：「{ctx}」\n当前输入拼音：「{pinyin}」\n候选词：{c}\n"
                 "按最符合对话语境的顺序列出前5个候选词（顿号分隔）：")
        else:
            u = (f"对话历史：「{ctx}」\n当前输入拼音：「{pinyin}」\n候选词：{c}\n"
                 "只输出最符合对话语境的那1个候选词：")
    else:
        if mode == "list":
            u = f"输入拼音：「{pinyin}」\n候选词：{c}\n按最可能的顺序列出前5个候选词（顿号分隔）："
        else:
            u = f"输入拼音：「{pinyin}」\n候选词：{c}\n只输出最可能的那1个候选词："
    return [{"role": "system", "content": SYS_LIST if mode == "list" else SYS_SINGLE},
            {"role": "user", "content": u}]

def call(mode, ctx, pinyin, cands):
    body = {"model": "main", "stream": False, "temperature": 0.0,
            "max_tokens": 20 if mode == "list" else 6,
            "messages": build_messages(mode, ctx, pinyin, cands),
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    ms = (time.perf_counter() - t0) * 1000
    content = d["choices"][0]["message"].get("content") or ""
    return content, ms, d.get("timings", {})

def first_word(content):
    for seg in content.replace(",", "、").replace("，", "、").split("、"):
        w = seg.strip(" \n.。;；0123456789①②③④⑤")
        if w:
            return w
    return ""

def start_server(model):
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    log = open(os.path.join(ROOT, "reports", f"bench_server_{os.path.basename(model)}.log"), "w", encoding="utf-8")
    p = subprocess.Popen([SERVER, "-m", model, "--port", str(PORT),
                          "--alias", "main", "-c", "1024", "-ngl", "0", "-t", "10",
                          "--jinja", "--parallel", "1", "--no-webui"],
                         stdout=log, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if json.loads(r.read())["status"] == "ok":
                    return p
        except Exception:
            pass
    raise RuntimeError(f"{model} 服务启动超时")

def stop_server(p):
    subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True)
    time.sleep(2)

def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not SERVER:
        sys.exit("找不到 llama-server.exe（tools/llama/ 或 PATH）")
    if not MODELS:
        sys.exit("models/ 下没有可测模型，用法: python bench_rerank.py <model.gguf>")
    print(f"server: {SERVER}")
    raw_path = os.path.join(ROOT, "reports", "bench_raw.jsonl")
    summary = []
    with open(raw_path, "w", encoding="utf-8") as raw:
        for model in MODELS:
            print(f"\n===== {model} =====")
            try:
                proc = start_server(model)
            except Exception as e:
                print("  启动失败:", e)
                continue
            try:
                for mode in ("list", "single"):
                    # 预热 3 次（首问含模板编译/缓存填充）
                    for i in range(3):
                        c = CASES[i % len(CASES)]
                        call(mode, *c[:3])
                    hits, lats, empty = 0, [], 0
                    for ctx, py, cands, exp in CASES:
                        try:
                            content, ms, tim = call(mode, ctx, py, cands)
                        except Exception as e:
                            content, ms, tim = f"__ERROR__ {e}", 0, {}
                        top = first_word(content)
                        hit = (top == exp)
                        lats.append(ms)
                        if "__ERROR__" in content or not content.strip():
                            empty += 1
                        if hit:
                            hits += 1
                        raw.write(json.dumps({"model": model, "mode": mode, "ctx": ctx, "pinyin": py,
                                              "cands": cands, "expected": exp, "content": content,
                                              "top": top, "hit": hit, "ms": round(ms, 1)},
                                             ensure_ascii=False) + "\n")
                    n = len(CASES)
                    row = {"model": model, "mode": mode, "hit": hits, "n": n,
                           "hit_rate": round(hits / n, 3), "empty": empty,
                           "mean_ms": round(statistics.mean(lats)),
                           "p50_ms": round(pct(lats, 0.5)), "p95_ms": round(pct(lats, 0.95))}
                    summary.append(row)
                    print(f"  [{mode:6s}] 命中 {hits}/{n} ({row['hit_rate']:.0%})  空回复 {empty}"
                          f"  延迟 mean {row['mean_ms']}ms / P50 {row['p50_ms']}ms / P95 {row['p95_ms']}ms")
            finally:
                stop_server(proc)

    with open(os.path.join(ROOT, "reports", "bench_summary.md"), "w", encoding="utf-8") as f:
        f.write("# 候选词重排模型横评（CPU · -ngl 0 · 10线程 · enable_thinking=false）\n\n")
        f.write(f"- 用例：{len(CASES)} 条拼音歧义句（含无上下文/语境反转/成语接续/水利工程专业词）\n")
        f.write("- 模式：list=原版Top5列表输出(max_tokens=20)；single=只输出最优1词(max_tokens=6)\n")
        f.write("- 命中=模型首选与期望一致；延迟含HTTP往返\n\n")
        f.write("| 模型 | 模式 | 命中率 | 空回复 | mean | P50 | P95 |\n|---|---|---|---|---|---|---|\n")
        for r in summary:
            f.write(f"| {r['model']} | {r['mode']} | {r['hit']}/{r['n']} ({r['hit_rate']:.0%}) "
                    f"| {r['empty']} | {r['mean_ms']}ms | {r['p50_ms']}ms | {r['p95_ms']}ms |\n")
    print("\n报告已写入 reports/bench_summary.md")

if __name__ == "__main__":
    main()
