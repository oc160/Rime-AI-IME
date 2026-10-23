# test_ime.py — 手机端验证 C++ 推理库（Termux 运行）
# 用法: python3 test_ime.py [模型文件名]   缺省 model-4M.safetensors
import ctypes, os, sys, time

RESULT_PATH = "/sdcard/ime_ai/results.txt"
_lines = []
def out(s):
    print(s, flush=True)
    _lines.append(s)

def save():
    try:
        with open(RESULT_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(_lines) + "\n\n")
    except Exception as e:
        print("(结果写入失败:", e, ")")

HERE = os.path.dirname(os.path.abspath(__file__))
SO = os.path.join(HERE, "libime_arm64.so")
MODEL = os.path.join(HERE, sys.argv[1] if len(sys.argv) > 1 else "model-4M.safetensors")

import io, contextlib
# 加载日志(stderr)不进结果文件，其余输出全记录
out(f"模型: {os.path.basename(MODEL)} ({os.path.getsize(MODEL)//1024} KB)")
print("加载 .so...", flush=True)
lib = ctypes.CDLL(SO)

lib.ime_version.restype = ctypes.c_char_p
lib.ime_load.restype = ctypes.c_void_p
lib.ime_load.argtypes = [ctypes.c_char_p]
lib.ime_score.restype = ctypes.c_float
lib.ime_score.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
lib.ime_best.restype = ctypes.c_int
lib.ime_best.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p), ctypes.c_int]

out("版本: " + lib.ime_version().decode())
print("加载模型...", flush=True)
t0 = time.time()
handle = lib.ime_load(MODEL.encode())
if not handle:
    out("✗ 模型加载失败")
    save()
    sys.exit(1)
out(f"✓ 模型加载成功 ({time.time()-t0:.1f}s)")

tests = [
    ("这个问题不能再", ["计划", "激化", "极化"], "激化"),
    ("他历尽千辛万苦，看透了", ["人声", "人生", "人参"], "人生"),
    ("水利工程的", ["质量", "资料", "智力"], "质量"),
    ("聊天时他只会发", ["合合", "和和", "呵呵"], "呵呵"),
    ("今天的会议开得很有成", ["笑过", "效果", "校过"], "效果"),
]

hit = 0
times = []
for ctx, cands, expected in tests:
    ctx_b = ctx.encode('utf-8')
    t0 = time.time()
    scores = [lib.ime_score(handle, ctx_b, c.encode('utf-8')) for c in cands]
    elapsed = (time.time() - t0) * 1000
    times.append(elapsed)
    best = cands[scores.index(max(scores))]
    ok = best == expected
    hit += ok
    out(f"{'✓' if ok else '✗'} [{ctx}] → {best} (期望 {expected})  {elapsed:.0f}ms")

cands_ptr = (ctypes.c_char_p * 3)(*[c.encode() for c in ["计划", "激化", "极化"]])
idx = lib.ime_best(handle, "这个问题不能再".encode(), cands_ptr, 3)
out(f"批量选优: index={idx} → {['计划','激化','极化'][idx]}")

out(f"结果: {hit}/{len(tests)} 正确  延迟 min/avg/max = {min(times):.0f}/{sum(times)/len(times):.0f}/{max(times):.0f}ms")
save()
print("✓ 全部测试完成")
