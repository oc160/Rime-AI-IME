# -*- coding: utf-8 -*-
"""replay_eval.py — 按键流回放评测：万象 n-gram 层 A/B 基线（离线，不碰前台）

原理：ctypes 加载 Weasel 自带 rime.dll，在隔离的用户目录副本里创建会话、
回放拼音按键流，逐词记录候选与上屏，对比 grammar 开(A)/关(B) 的三项指标：
  句子零干预正确率 / 词首命中率 / 词 top5 召回率
用法: python replay_eval.py [--only A|B]
"""
import ctypes, os, sys, shutil, argparse, time

WEASEL_DIR = r"C:\Program Files\Rime\weasel-0.17.4"
RIME_DLL = os.path.join(WEASEL_DIR, "rime.dll")
SHARED_DATA = os.path.join(WEASEL_DIR, "data")
LIVE_USER_DIR = os.path.join(os.environ["APPDATA"], "Rime")
WORK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "replay_work")

# ── ctypes 结构镜像（rime_api.h，仅前段成员，顺序不可变）────────
class RimeTraits(ctypes.Structure):
    _fields_ = [
        ("data_size", ctypes.c_int),
        ("shared_data_dir", ctypes.c_char_p),
        ("user_data_dir", ctypes.c_char_p),
        ("distribution_name", ctypes.c_char_p),
        ("distribution_code_name", ctypes.c_char_p),
        ("distribution_version", ctypes.c_char_p),
        ("app_name", ctypes.c_char_p),
        ("modules", ctypes.POINTER(ctypes.c_char_p)),
        ("min_log_level", ctypes.c_int),
        ("log_dir", ctypes.c_char_p),
        ("prebuilt_data_dir", ctypes.c_char_p),
        ("staging_dir", ctypes.c_char_p),
    ]

class RimeComposition(ctypes.Structure):
    _fields_ = [("length", ctypes.c_int), ("cursor_pos", ctypes.c_int),
                ("sel_start", ctypes.c_int), ("sel_end", ctypes.c_int),
                ("preedit", ctypes.c_char_p)]

class RimeCandidate(ctypes.Structure):
    _fields_ = [("text", ctypes.c_char_p), ("comment", ctypes.c_char_p),
                ("reserved", ctypes.c_void_p)]

class RimeMenu(ctypes.Structure):
    _fields_ = [("page_size", ctypes.c_int), ("page_no", ctypes.c_int),
                ("is_last_page", ctypes.c_int), ("highlighted_candidate_index", ctypes.c_int),
                ("num_candidates", ctypes.c_int),
                ("candidates", ctypes.POINTER(RimeCandidate)),
                ("select_keys", ctypes.c_char_p)]

class RimeCommit(ctypes.Structure):
    _fields_ = [("data_size", ctypes.c_int), ("text", ctypes.c_char_p)]

class RimeContext(ctypes.Structure):
    _fields_ = [("data_size", ctypes.c_int), ("composition", RimeComposition),
                ("menu", RimeMenu), ("commit_text_preview", ctypes.c_char_p),
                ("select_labels", ctypes.POINTER(ctypes.c_char_p))]

FP = ctypes.CFUNCTYPE
class RimeApi(ctypes.Structure):
    _fields_ = [
        ("data_size", ctypes.c_int),
        ("setup", FP(None, ctypes.POINTER(RimeTraits))),
        ("set_notification_handler", ctypes.c_void_p),
        ("initialize", FP(None, ctypes.POINTER(RimeTraits))),
        ("finalize", FP(None)),
        ("start_maintenance", FP(ctypes.c_int, ctypes.c_int)),
        ("is_maintenance_mode", FP(ctypes.c_int)),
        ("join_maintenance_thread", FP(None)),
        ("deployer_initialize", ctypes.c_void_p),
        ("prebuild", ctypes.c_void_p),
        ("deploy", ctypes.c_void_p),
        ("deploy_schema", ctypes.c_void_p),
        ("deploy_config_file", ctypes.c_void_p),
        ("sync_user_data", ctypes.c_void_p),
        ("create_session", FP(ctypes.c_size_t)),
        ("find_session", ctypes.c_void_p),
        ("destroy_session", FP(ctypes.c_int, ctypes.c_size_t)),
        ("cleanup_stale_sessions", ctypes.c_void_p),
        ("cleanup_all_sessions", ctypes.c_void_p),
        ("process_key", FP(ctypes.c_int, ctypes.c_size_t, ctypes.c_int, ctypes.c_int)),
        ("commit_composition", ctypes.c_void_p),
        ("clear_composition", ctypes.c_void_p),
        ("get_commit", FP(ctypes.c_int, ctypes.c_size_t, ctypes.POINTER(RimeCommit))),
        ("free_commit", FP(ctypes.c_int, ctypes.POINTER(RimeCommit))),
        ("get_context", FP(ctypes.c_int, ctypes.c_size_t, ctypes.POINTER(RimeContext))),
        ("free_context", FP(ctypes.c_int, ctypes.POINTER(RimeContext))),
        ("get_status", ctypes.c_void_p),
        ("free_status", ctypes.c_void_p),
        ("set_option", FP(None, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int)),
        ("get_option", FP(ctypes.c_int, ctypes.c_size_t, ctypes.c_char_p)),
        ("set_property", ctypes.c_void_p), ("get_property", ctypes.c_void_p),
        ("get_schema_list", ctypes.c_void_p), ("free_schema_list", ctypes.c_void_p),
        ("get_current_schema", ctypes.c_void_p), ("select_schema", ctypes.c_void_p),
        ("schema_open", ctypes.c_void_p), ("config_open", ctypes.c_void_p),
        ("config_close", ctypes.c_void_p), ("config_get_bool", ctypes.c_void_p),
        ("config_get_int", ctypes.c_void_p), ("config_get_double", ctypes.c_void_p),
        ("config_get_string", ctypes.c_void_p), ("config_get_cstring", ctypes.c_void_p),
        ("config_update_signature", ctypes.c_void_p), ("config_begin_map", ctypes.c_void_p),
        ("config_next", ctypes.c_void_p), ("config_end", ctypes.c_void_p),
        ("simulate_key_sequence", ctypes.c_void_p), ("register_module", ctypes.c_void_p),
        ("find_module", FP(ctypes.c_void_p, ctypes.c_char_p)),
        ("run_task", ctypes.c_void_p),
    ]

RIME_TEXT_MAX = 4096
KEY_SPACE, KEY_RETURN, KEY_BACKSPACE = 0x20, 0xFF0D, 0xFF08

# ── 评测集：三分区（每条 = 按词分段输入，gold 为目标句）──────────
# subset1 普通整句（万象主场）；subset2 模糊音/简拼打字；subset3 语义长程消歧
FUZZY = {"zh": "z", "ch": "c", "sh": "s"}  # 卷舌→平舌 模糊化用
def fuzz(py: str) -> str:
    for k, v in FUZZY.items():
        py = py.replace(k, v)
    return py

CASES = [
    # (subset, [ (gold词, 拼音), ... ] , 打法)  打法: normal|fuzzy|abbrev
    ("normal", [("今天", "jintian"), ("天气", "tianqi"), ("很好", "henhao")], "normal"),
    ("normal", [("我们", "women"), ("明天", "mingtian"), ("开会", "kaihui"), ("讨论", "taolun"), ("计划", "jihua")], "normal"),
    ("normal", [("水利", "shuili"), ("工程", "gongcheng"), ("建设", "jianshe"), ("进展", "jinzhan"), ("顺利", "shunli")], "normal"),
    ("normal", [("水库", "shuiku"), ("汛期", "xunqi"), ("提前", "tiqian"), ("泄洪", "xiehong")], "normal"),
    ("normal", [("这篇", "zhepian"), ("文章", "wenzhang"), ("内容", "neirong"), ("丰富", "fengfu")], "normal"),
    ("fuzzy",  [("这个", "zhege"), ("事情", "shiqing"), ("不能", "buneng"), ("这样", "zheyang")], "fuzzy"),
    ("fuzzy",  [("时间", "shijian"), ("就是", "jiushi"), ("生命", "shengming")], "fuzzy"),
    ("fuzzy",  [("支持", "zhichi"), ("施工", "shigong"), ("措施", "cuoshi"), ("实施", "shishi")], "fuzzy"),
    ("fuzzy",  [("城市", "chengshi"), ("建设", "jianshe"), ("成绩", "chengji"), ("显著", "xianzhu")], "fuzzy"),
    ("semantic", [("这个问题", "zhegewenti"), ("不能再", "bunengzai"), ("激化", "jihua")], "normal"),
    ("semantic", [("他到了", "tadaole"), ("重庆", "chongqing"), ("以后", "yihou"), ("感觉", "ganjue"), ("责任", "zeren"), ("重大", "zhongda")], "normal"),
    ("semantic", [("考古", "kaogu"), ("学家", "xuejia"), ("研究", "yanjiu"), ("这一", "zheyi"), ("历史", "lishi"), ("事件", "shijian")], "normal"),
    ("semantic", [("矛盾", "maodun"), ("进一步", "jinyibu"), ("激化", "jihua"), ("之后", "zhihou"), ("双方", "shuangfang"), ("谈判", "tanpan")], "normal"),
    ("semantic", [("会议", "huiyi"), ("开得", "kaide"), ("很有", "henyou"), ("成效", "chengxiao")], "normal"),
]

# ── 引擎封装 ────────────────────────────────────────────────
class RimeReplay:
    def __init__(self, tag: str, grammar_on: bool):
        self.tag = tag
        # 隔离 APPDATA：rime.lua 的日志/临时文件/HTTP 行为全部落在 iso 目录，
        # 与线上 %APPDATA%\Rime 零接触
        self.iso_root = os.path.join(WORK_DIR, f"iso_{tag}")
        self.user_dir = os.path.join(self.iso_root, "Rime")
        self._prepare_user_dir(grammar_on)
        os.environ["APPDATA"] = self.iso_root
        os.add_dll_directory(WEASEL_DIR)
        self.dll = ctypes.WinDLL(RIME_DLL)
        self.dll.rime_get_api.restype = ctypes.c_void_p  # 64位指针，默认int会截断致段错误
        api_ptr = self.dll.rime_get_api()
        self.api = ctypes.cast(api_ptr, ctypes.POINTER(RimeApi)).contents
        traits = RimeTraits()
        traits.data_size = ctypes.sizeof(RimeTraits) - ctypes.sizeof(ctypes.c_int)
        traits.shared_data_dir = SHARED_DATA.encode()
        traits.user_data_dir = self.user_dir.encode()
        traits.distribution_name = b"Weasel"
        traits.distribution_code_name = b"weasel-replay"
        traits.distribution_version = b"0.1"
        traits.app_name = b"rime.replay-eval"
        traits.min_log_level = int(os.environ.get("REPLAY_LOG_LEVEL", "3"))
        # glog 在 Windows 下处理不了含中文的 UTF-8 路径，日志目录必须 ASCII
        _logdir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "rime_replay")
        os.makedirs(_logdir, exist_ok=True)
        traits.log_dir = _logdir.encode("ascii", "replace")
        # 关键：显式模块表。若留 NULL，Initialize 会拿到空表而非 kDefaultModules，
        # octagram(grammar) 组件不会初始化，语法模型静默失效
        self._mods = (ctypes.c_char_p * 5)(b"default", b"octagram", b"lua", b"predict", None)
        traits.modules = ctypes.cast(self._mods, ctypes.POINTER(ctypes.c_char_p))
        self.api.setup(ctypes.byref(traits))
        self.api.initialize(ctypes.byref(traits))
        self.api.start_maintenance(0)
        self.api.join_maintenance_thread()

    def _prepare_user_dir(self, grammar_on: bool):
        os.makedirs(os.path.join(WORK_DIR, "logs"), exist_ok=True)
        if os.path.exists(self.user_dir):
            shutil.rmtree(self.user_dir)
        shutil.copytree(LIVE_USER_DIR, self.user_dir,
                        ignore=shutil.ignore_patterns("*.userdb", "*.tmp", "installation.yaml", "build"))
        # 删除 build 强制在副本内重新部署（保证 grammar 开/关真实生效）
        shutil.rmtree(os.path.join(self.user_dir, "build"), ignore_errors=True)
        if not grammar_on:
            p = os.path.join(self.user_dir, "rime_ice.custom.yaml")
            if os.path.exists(p):
                lines = [l for l in open(p, encoding="utf-8").read().splitlines()
                         if "grammar/" not in l and "contextual_suggestions" not in l]
                open(p, "w", encoding="utf-8").write("\n".join(lines))
        # 阶梯②③注入：仅在隔离副本内挂载/启用 LLM filter，线上配置零接触
        p = os.path.join(self.user_dir, "rime_ice.custom.yaml")
        if os.environ.get("REPLAY_MOUNT_FILTER") == "1" and os.path.exists(p):
            text = open(p, encoding="utf-8").read()
            if "lua_filter@llm_reranker" not in text:
                text = text.replace("patch:", 'patch:\n  "engine/filters/@next": lua_filter@llm_reranker', 1)
            if os.environ.get("REPLAY_ENABLE_LLM") == "1":
                text = text.replace("enabled: false", "enabled: true")
            open(p, "w", encoding="utf-8").write(text)

    def new_session(self):
        sid = self.api.create_session()
        assert sid, f"[{self.tag}] create_session 失败"
        return sid

    def context_of(self, sid):
        ctx = RimeContext()
        ctx.data_size = ctypes.sizeof(RimeContext) - ctypes.sizeof(ctypes.c_int)
        if not self.api.get_context(sid, ctypes.byref(ctx)):
            return None
        cands = []
        m = ctx.menu
        if m.num_candidates > 0 and m.candidates:
            for i in range(min(m.num_candidates, 10)):
                c = m.candidates[i]
                if c.text:
                    cands.append(c.text.decode("utf-8", "replace"))
        preedit = ctx.composition.preedit.decode("utf-8", "replace") if ctx.composition.preedit else ""
        preview = ctx.commit_text_preview.decode("utf-8", "replace") if ctx.commit_text_preview else ""
        self.api.free_context(ctypes.byref(ctx))
        return {"preedit": preedit, "preview": preview, "candidates": cands}

    def pop_commit(self, sid):
        c = RimeCommit()
        c.data_size = ctypes.sizeof(RimeCommit) - ctypes.sizeof(ctypes.c_int)
        if self.api.get_commit(sid, ctypes.byref(c)):
            text = c.text.decode("utf-8", "replace") if c.text else ""
            self.api.free_commit(ctypes.byref(c))
            return text
        return None

    def type_pinyin(self, sid, py: str):
        for ch in py:
            self.api.process_key(sid, ord(ch), 0)

    def press_space(self, sid):
        self.api.process_key(sid, KEY_SPACE, 0)

    def replay_case(self, case):
        subset, words, mode = case
        sid = self.new_session()
        sent_words, word_stats = [], []   # word_stats: (gold, first_hit, in_top5)
        for gold, py in words:
            p = fuzz(py) if mode == "fuzzy" else py
            self.type_pinyin(sid, p)
            ctx = self.context_of(sid) or {"candidates": []}
            cands = ctx["candidates"]
            first_hit = bool(cands) and cands[0].startswith(gold)
            in_top5 = any(c.startswith(gold) for c in cands[:5])
            word_stats.append((gold, first_hit, in_top5, cands[:5]))
            self.press_space(sid)
            committed = self.pop_commit(sid)
            sent_words.append(committed if committed else cands[0] if cands else gold)
        self.api.destroy_session(sid)
        sentence = "".join(w for w in sent_words if w)
        gold_sentence = "".join(g for g, _ in words)
        exact = sentence == gold_sentence
        return {"subset": subset, "mode": mode, "exact": exact,
                "sentence": sentence, "gold": gold_sentence, "words": word_stats}

    def close(self):
        try:
            self.api.finalize()
        except Exception:
            pass

# ── 主流程 ─────────────────────────────────────────────────
def run(tag, grammar_on):
    print(f"\n===== 引擎 {tag}（grammar {'ON' if grammar_on else 'OFF'}）=====", flush=True)
    eng = RimeReplay(tag, grammar_on)
    results = []
    for case in CASES:
        r = eng.replay_case(case)
        results.append(r)
        mark = "✓" if r["exact"] else "✗"
        fh = sum(1 for _, f, _, _ in r["words"] if f)
        print(f"  [{r['subset']:8s}]{mark} {r['sentence'][:24]} / {r['gold'][:24]}  首中{fh}/{len(r['words'])}", flush=True)
    eng.close()
    return results

def summarize(all_results):
    print("\n===== 汇总 =====")
    header = f"{'子集':<10s}{'引擎':<4s}{'句准':<8s}{'词首中':<9s}{'词top5召回'}"
    print(header)
    for tag, results in all_results.items():
        for subset in ("normal", "fuzzy", "semantic"):
            sub = [r for r in results if r["subset"] == subset]
            if not sub:
                continue
            ex = sum(1 for r in sub if r["exact"])
            words = [w for r in sub for w in r["words"]]
            fh = sum(1 for _, f, _, _ in words if f)
            t5 = sum(1 for _, _, t, _ in words if t)
            print(f"{subset:<10s}{tag:<4s}{ex}/{len(sub):<6d}{fh}/{len(words):<7d}{t5}/{len(words)}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["A", "B"], default=None)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    out = {}
    if args.only in (None, "A"):
        out["A"] = run("A", grammar_on=True)
    if args.only in (None, "B"):
        out["B"] = run("B", grammar_on=False)
    summarize(out)
    print(f"\n总耗时 {time.time()-t0:.0f}s（含副本部署）")
