# Android / Trime 端部署（进程内推理）

在 Trime（同文输入法）里跑与 PC 同源的 LLM 候选重排：lua 直接加载自带的
`libime.so`（C++ 4M 模型推理，进程内后台线程打分），击键路径零网络零进程开销。
支持 9 键（九宫格数字输入）与 26 键全拼，简体输出。

## 依赖文件（两类）

**本仓库提供**（`android/`）：
- `rime.lua` — v8-inproc：惰性候选枚举 + 九键数字重排 + 插入符剥离 + 决策日志
- `luna_pinyin.custom.yaml` / `clover_jiugong.custom.yaml` / `default.custom.yaml` — 方案补丁
- `cpp/` — 推理库源码（含 bcmp 垫片，见下）

**外部获取**（GPL 许可文件与模型，不入本仓库，自行下载放入 Rime 目录）：
- `clover_jiugong.schema.yaml` — 九宫格方案，取自 SivanLaai/rime-pure（GPL-3.0）
- 九键键盘主题（可选）— 同仓库 tongwenfeng_plus.trime.yaml
- `ime-model.safetensors` — 4M 打分模型（见主 README 模型发布说明）

## 编译 libime.so

路线 A（PC，无 NDK）：
```bash
pip install ziglang   # zig 0.16
# 桩库（链接期满足符号，运行时由系统 libm/liblog 顶替）
printf 'float logf(float x){return 0;} float expf(float x){return 0;} float erff(float x){return 0;}' > libm_stub.c
zig cc -target aarch64-linux-musl -shared -fPIC -Wl,-soname,libm.so libm_stub.c -o libm.so
printf 'int __android_log_print(int p,const char*t,const char*f,...){return 0;}' > liblog_stub.c
zig cc -target aarch64-linux-musl -shared -fPIC -Wl,-soname,liblog.so liblog_stub.c -o liblog.so
# 主库：lua 5.4 头文件 + 从官方 Trime APK 解出的 librime_jni.so（lua 符号宿主）
# 源文件必须包含 bcmp_shim.c —— zig/musl 产物引用 bcmp，Android bionic 不导出，
# 缺它 dlopen 必挂（PC 回归测不出的真机专属坑）
zig c++ -target aarch64-linux-musl -shared -fPIC -O2 -fno-exceptions -fno-rtti   -nostdlib++ -DIME_ANDROID_LOG -I <lua头目录>   bcmp_shim.c ime_lua.cpp ime_inference.cpp librime_jni.so libm.so liblog.so -o libime.so
```

路线 B（Termux 原生 clang++）：注意产物不得依赖 Termux 的 libc++_shared
（`readelf -d libime.so | grep NEEDED` 检查）。

## 注入 APK 与签名

```bash
python -c "import zipfile; z=zipfile.ZipFile('trime-patched.apk','a',compression=zipfile.ZIP_DEFLATED); z.write('libime.so','lib/arm64-v8a/libime.so')"
# 自签（apksigner / uber-apk-signer），卸载官方 Trime（签名不同）后安装
```

## 安装与配置（一次性）

1. 首启向导存储模式先选「应用专属存储」，再在设置里切「从外部存储同步」选 /sdcard/rime
2. /sdcard/rime/ 放入上述全部文件（模型重命名为 ime-model.safetensors）
3. Trime 点「部署」，**部署后切换一次系统输入法再切回**（关键，见排障）
4. 常规 → 嵌入式预编辑模式 → 首选词预览（输入栏出首选词而非数字码）

## 重排候选数热调（免部署）

/sdcard/rime/rerank_knob.txt 写 2~32 的整数，1 秒内生效（C 层上限 32）；
删文件回落 yaml 配置值。推理耗时随候选数近似线性（无共享上下文 KV）。

## 排障铁律（全部真机踩坑实录）

1. **部署不热替换已存会话**：改 lua/配置后除「部署」外必须切一次输入法，
   否则后台应用一直跑旧引擎，症状是"改了没生效"
2. **测试输入框不等于真实应用**：外部应用内嵌预编辑带尾部插入符，
   lua 解析前须 preedit:gsub("[^%w%s']+$","")，测试框里复现不了
3. **bcmp**：musl 目标交叉编译的必踩坑，垫片见 cpp/bcmp_shim.c
4. **全量 drain 候选流**：多音节上万候选枚举数秒，键盘表现为"无候选"，
   必须惰性枚举（只取前 K，尾流直通）
