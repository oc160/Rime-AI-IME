# android-trime — AI 候选重排上手机（Trime 进程内推理）

目标：把 4M 字符级 Transformer 打分器塞进 Trime（Android Rime）进程内，
击键路径零阻塞（v8 异步架构：lua filter 投递 → 后台线程打分 → 下一键回收结果）。

```
cpp/ime_inference.{h,cpp}   推理库：safetensors 加载 + char-transformer 前向 + 打分
                             零 C++ 运行库依赖（-nostdlib++ 可编），int8 自动反量化
cpp/ime_lua.cpp             librime-lua C 模块：ime.load/query/pickup（后台线程）
android/rime.lua            v8-inproc：文件握手 sidecar → 进程内 C 模块
android/luna_pinyin.custom.yaml  方案补丁：lua filter + ai_rerank 开关 + 参数
android/test_ime.py         Termux 直测脚本（不进 Trime 也能验证 .so + 模型）
dist/libime.so              zig 交叉编译产物（89KB，待真机验证）
dist/ime-model-4M.safetensors  4M 零样本模型（手机实测 4/5，单候选 30-45ms）
apk/trime-patched-unsigned.apk 官方 v3.3.12 arm64 + libime.so 注入（未签名）
```

## 路线 A：PC 构建（本套件 dist/ 即此产物，已含）

```bash
pip install ziglang                                    # zig 0.16 工具链
# 桩库：链接期满足符号，运行时由 Android 系统 libm/liblog 顶替
printf 'float logf(float x){return 0;} float expf(float x){return 0;} float erff(float x){return 0;}' > libm_stub.c
zig cc -target aarch64-linux-musl -shared -fPIC -Wl,-soname,libm.so libm_stub.c -o libm.so
printf 'int __android_log_print(int p,const char*t,const char*f,...){return 0;}' > liblog_stub.c
zig cc -target aarch64-linux-musl -shared -fPIC -Wl,-soname,liblog.so liblog_stub.c -o liblog.so
# 主库：从官方 APK 解出 librime_jni.so（lua 符号宿主）一起链接
zig c++ -target aarch64-linux-musl -shared -fPIC -O2 -fno-exceptions -fno-rtti \
  -nostdlib++ -DIME_ANDROID_LOG -I <lua5.4头文件> \
  ime_lua.cpp ime_inference.cpp librime_jni.so libm.so liblog.so -o libime.so
```

要点：
- `DT_NEEDED = librime_jni.so + libm.so + liblog.so + libc.so`，全部由 Trime 进程提供
- lua 头文件用 lua-5.4.x 的 lua.h/luaconf.h/lauxlib.h（Trime 内嵌 Lua 5.4，
  155 个 lua 符号从 librime_jni.so 导出，已验证）
- **lua.h 必须在 extern "C" 里 include**，否则符号被 C++ 修饰、运行时解析失败
- PC 回归验证：同源码编 x86_64 DLL 跑 5 用例，与手机旧版逐例一致（4/5）

## 路线 B：Termux 原生编译（无 PC 时）

```bash
pkg install libc++ -y
cd ~/ime && tar xzf /sdcard/ime_ai/lua54.tar.gz   # lua 头文件
clang++ -O2 -fPIC -shared -I lua-5.4.7/src ime_lua.cpp ime_inference.cpp \
  -L . -l:librime_jni.so -lm -o libime.so
```

注意：Termux 产物链 Termux 的 libc++_shared.so，装进 Trime 前需确认
`readelf -d libime.so | grep NEEDED` 无 libc++_shared（有则回到路线 A）。

## APK 补丁与签名

```bash
# PC：把 libime.so 追加进官方 APK（python，原条目不动）
python -c "import zipfile; z=zipfile.ZipFile('trime-patched.apk','a',compression=zipfile.ZIP_DEFLATED); z.write('libime.so','lib/arm64-v8a/libime.so')"
# 手机 Termux：生成密钥 + 签名
pkg install apksigner openssl -y
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 10950 -nodes -subj "/CN=rime-llm"
openssl pkcs8 -topk8 -inform PEM -outform DER -in key.pem -out key.pk8 -nocrypt
apksigner sign --key key.pk8 --cert cert.pem --out trime-signed.apk trime-patched.apk
```

## 安装与配置（一次性）

1. 卸载官方 Trime（签名不同不能覆盖）→ 安装 trime-signed.apk
2. 设置向导：存储模式选「使用应用专属存储空间」（官方默认"从外部同步"缺授权会崩，
   见 2026-10-17 排障：`Skip starting rime: storage not available` → 弹键盘必崩）
3. Trime 设置 → 存储 → 数据存储模式 → 「从外部存储同步」，SAF 选 `/sdcard/rime`
4. `/sdcard/rime/` 放入：`rime.lua` + `luna_pinyin.custom.yaml` + `ime-model.safetensors`
5. Trime 里点「部署」（外部同步模式会先把 /sdcard/rime 导入应用目录）
6. 切到 Trime 键盘打字验证

## 验证

```bash
adb logcat -s ime        # 应看到 loading/loaded OK（rime.lua 首次过滤时加载）
```
- 候选重排生效表现：同音句子里首选随语境变化（如"这个问题不能再激化"）
- ai_rerank 开关在方案选单里可随时关（luna_pinyin.custom.yaml 已加）
- 数据飞轮照常落 `rime_train.jsonl`（用户目录），同步模式会导出回 /sdcard/rime

## 已知边界

- 词长 >6 的候选不入模（C 层 IME_CAND_CAP=48 字节）
- min_margin=0.03 起步：分差低于此值不重排（防误排，可在 custom.yaml 调）
- 未做：多候选共享上下文 KV（现在每候选重算前缀，预计可再快 ~3×）
