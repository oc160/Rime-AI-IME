# -*- coding: utf-8 -*-
"""watcher_train.py — 等待资源窗口并自动执行：训练 → GGUF 转换 → bench 验收

适用于与其他 GPU 任务共用一台机器的场景：等大占用任务下线、内存空闲后自动开跑。
触发条件（每 120s 探测，均可用环境变量覆盖）：
  IME_WATCH_NAME  要等待的进程命令行关键字（默认 llama-server）
  IME_WATCH_FREE  物理内存空闲阈值 GB（默认 7.5）
"""
import subprocess, time, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根（tools/ 的上一级）
LOG = open(os.path.join(ROOT, "reports", "watcher.log"), "a", encoding="utf-8")

def log(m):
    line = f"{time.strftime('%m-%d %H:%M:%S')} {m}"
    LOG.write(line + "\n"); LOG.flush()
    print(line, flush=True)

def tenant_alive():
    # wmic 已不可靠；用 CIM 查命令行。注意输出必须是整数，避开区域小数逗号
    watch = os.environ.get("IME_WATCH_NAME", "llama-server")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"@(Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" "
         f"| Where-Object {{$_.CommandLine -like '*{watch}*'}}).Count"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        return int(out) > 0
    except ValueError:
        return True   # 探测失败按"在线"处理，宁可多等不可误开

def ram_free_gb():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[int](Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        return int(out) / 1048576   # KB → GB
    except ValueError:
        return 0.0

def run(cmd, logname, env_extra=None, cwd=ROOT):
    env = dict(os.environ, **(env_extra or {}))
    with open(os.path.join(ROOT, "reports", logname), "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    return r.returncode

def main():
    log("watcher 启动：等待对方 9B 服务下线 + 内存空闲 >7.5GB")
    waited = 0
    while True:
        alive, free = tenant_alive(), ram_free_gb()
        if waited % 1800 == 0:
            log(f"状态：占用方在线={alive}，RAM空闲={free:.1f}GB")
        if not alive and free > float(os.environ.get("IME_WATCH_FREE", "7.5")):
            break
        time.sleep(120)
        waited += 120
    log(f"条件满足（对方下线，RAM {ram_free_gb():.1f}GB），60s 后开训")
    time.sleep(60)

    env = {"HF_HOME": r"C:\hf_cache", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    for bs, ck in ((24, 1), (16, 1)):   # 混合线性注意力参考内核显存吃紧，一律开检查点
        rc = run([sys.executable, "train_sft.py",
                  "--train", "data/train.jsonl", "--val", "data/val.jsonl",
                  "--out-adapter", "models/adapter_v1",
                  "--out-merged", "models/hf/qwen35-08b-ime-v1",
                  "--epochs", "2", "--bs", str(bs), "--grad-ckpt", str(ck),
                  "--limit", "60000"],
                 f"train_auto_bs{bs}.log", env)
        log(f"训练退出码 {rc} (bs={bs},ckpt={ck})")
        if rc == 0:
            break
        log(f"bs={bs} 失败，{'降档重试' if bs != 16 else '放弃'}")
    if rc != 0:
        log("训练失败，链路终止（看 train_auto_bs*.log）")
        return

    merged = os.path.join(ROOT, "models", "hf", "qwen35-08b-ime-v1")
    gguf = os.path.join(ROOT, "models", "qwen35-08b-ime-v1-Q8_0.gguf")
    rc = run([sys.executable, os.path.join(ROOT, "tools", "llama.cpp", "convert_hf_to_gguf.py"),
              merged, "--outtype", "q8_0", "--outfile", gguf], "convert_auto.log")
    log(f"GGUF 转换退出码 {rc} → {gguf}")
    if rc != 0:
        return

    rc = run([sys.executable, "bench_rerank.py", gguf], "bench_auto.log")
    log(f"bench 退出码 {rc}（结果见 reports/bench_summary.md / bench_raw.jsonl）")
    log("全链完成：训练 + 转换 + 验收")

if __name__ == "__main__":
    main()
