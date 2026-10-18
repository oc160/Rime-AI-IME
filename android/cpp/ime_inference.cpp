// ime_inference.cpp — 字符级 Transformer 打分器（零 C++ 运行库依赖版）
// 实现 chinese-ime-lm 格式的 safetensors 加载 + 前向推理 + 候选打分。
// 纯 C 数据结构 + malloc/fopen，无 STL/异常/RTTI：
//   clang++ -O2 -shared -fPIC -fno-exceptions -fno-rtti -nostdlib++ ... -lm -llog
// 可加载进任意 Android 进程（Termux / Trime / APK 内嵌）。
// 日志：stderr + Android logcat（tag=ime），便于真机诊断。

#include "ime_inference.h"
#include <math.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

#if defined(__ANDROID__) || defined(IME_ANDROID_LOG)
extern "C" int __android_log_print(int prio, const char* tag, const char* fmt, ...);
#define ANDROID_LOG_INFO 4
#define IME_ANDROID 1
#endif

#ifdef IME_ANDROID
#define IME_LOG(...) do { \
    fprintf(stderr, "[ime] " __VA_ARGS__); \
    fprintf(stderr, "\n"); \
    __android_log_print(ANDROID_LOG_INFO, "ime", __VA_ARGS__); \
} while (0)
#else
#define IME_LOG(...) do { fprintf(stderr, "[ime] " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#endif

// ─── 动态字符串数组 ───────────────────────────────────────
struct StrList {
    char** items = nullptr;
    int n = 0, cap = 0;
    bool push(const char* s, int len) {
        if (n == cap) {
            cap = cap ? cap * 2 : 64;
            char** p = (char**)realloc(items, cap * sizeof(char*));
            if (!p) return false;
            items = p;
        }
        char* d = (char*)malloc(len + 1);
        if (!d) return false;
        memcpy(d, s, len); d[len] = '\0';
        items[n++] = d;
        return true;
    }
    void free_all() {
        for (int i = 0; i < n; i++) ::free(items[i]);
        ::free(items); items = nullptr; n = cap = 0;
    }
};

// ─── 简易 JSON 提取（仅词表数组与配置字段） ──────────────
// 返回 malloc 的去转义字符串（处理 \" \\ \n）；调用方 free。
static char* json_find_string_dup(const char* json, const char* key) {
    char pattern[128];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char* p = strstr(json, pattern);
    if (!p) return nullptr;
    p += strlen(pattern);
    while (*p && *p != ':') p++;
    if (*p == ':') p++;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '"') return nullptr;
    p++;
    int cap = 256, len = 0;
    char* out = (char*)malloc(cap);
    if (!out) return nullptr;
    while (*p) {
        if (len + 8 > cap) {
            cap *= 2;
            char* o = (char*)realloc(out, cap);
            if (!o) { free(out); return nullptr; }
            out = o;
        }
        if (p[0] == '\\' && p[1] == '"')      { out[len++] = '"';  p += 2; }
        else if (p[0] == '\\' && p[1] == '\\') { out[len++] = '\\'; p += 2; }
        else if (p[0] == '\\' && p[1] == 'n')  { out[len++] = '\n'; p += 2; }
        else if (p[0] == '\\' && p[1])         { out[len++] = p[1]; p += 2; }
        else if (p[0] == '"') break;
        else { out[len++] = *p; p++; }
    }
    out[len] = '\0';
    return out;
}

static int json_find_int(const char* json, const char* key, int default_val) {
    char* s = json_find_string_dup(json, key);
    if (!s) return default_val;
    int v = atoi(s);
    free(s);
    return v;
}

// 解析 JSON 字符串数组（元素支持 \uXXXX / \" 转义）
static void json_parse_string_array(const char* json, const char* key, StrList* out) {
    char* inner = json_find_string_dup(json, key);
    if (!inner || inner[0] != '[') { free(inner); return; }
    const char* p = inner + 1;
    char buf[16];
    while (*p && *p != ']') {
        while (*p == ' ' || *p == ',' || *p == '\n' || *p == '\t') p++;
        if (*p != '"') break;
        p++;
        int len = 0;
        while (*p && *p != '"') {
            if (p[0] == '\\' && p[1] == 'u') {
                int code = 0;
                sscanf(p + 2, "%4x", &code);
                if (code < 0x80)        buf[len++] = (char)code;
                else if (code < 0x800) { buf[len++] = (char)(0xC0 | (code >> 6));
                                        buf[len++] = (char)(0x80 | (code & 0x3F)); }
                else                   { buf[len++] = (char)(0xE0 | (code >> 12));
                                        buf[len++] = (char)(0x80 | ((code >> 6) & 0x3F));
                                        buf[len++] = (char)(0x80 | (code & 0x3F)); }
                if (len > 12) len = 12;   // 单字符最长 4 字节，防御异常数据
                p += 6;
            } else if (p[0] == '\\' && p[1] == '"') { buf[len++] = '"'; p += 2; }
            else { if (len < 15) buf[len++] = *p; p++; }
            if (len >= 15) { while (*p && *p != '"') p++; break; }  // 超长元素截断
        }
        if (*p == '"') p++;
        out->push(buf, len);
    }
    free(inner);
}

// ─── Safetensors 加载 ─────────────────────────────────────
struct TensorInfo {
    char name[96];
    int shape[4]; int nshape;
    uint64_t offset = 0, size = 0;
    char dtype[8];
};

#define IME_MAX_TENSORS 512
#define IME_MAX_LAYER 8

struct SafetensorsFile {
    FILE* f = nullptr;
    char* header_json = nullptr;
    uint64_t data_offset = 0;
    TensorInfo tensors[IME_MAX_TENSORS];
    int n_tensors = 0;

    TensorInfo* find(const char* name) {
        for (int i = 0; i < n_tensors; i++)
            if (strcmp(tensors[i].name, name) == 0) return &tensors[i];
        return nullptr;
    }

    bool open(const char* path) {
        close();
        f = fopen(path, "rb");
        if (!f) return false;
        uint64_t header_len = 0;
        if (fread(&header_len, 8, 1, f) != 1) return false;
        header_json = (char*)malloc(header_len + 1);
        if (!header_json) return false;
        if (fread(header_json, 1, header_len, f) != header_len) return false;
        header_json[header_len] = '\0';
        data_offset = 8 + header_len;

        // 解析 {"name": {"dtype":"...","shape":[...],"data_offsets":[a,b]}, ...}
        const char* p = header_json + 1;
        char key[128];
        while (*p && *p != '}') {
            while (*p == ' ' || *p == ',' || *p == '\n' || *p == '\t' || *p == '\r') p++;
            if (*p == '}' || !*p) break;
            if (*p != '"') { p++; continue; }
            p++;
            int klen = 0;
            while (*p && *p != '"' && klen < 127) key[klen++] = *p++;
            key[klen] = '\0';
            if (*p == '"') p++;
            while (*p && *p != ':' && *p != ',' && *p != '}') p++;
            if (*p != ':') continue;
            p++;
            while (*p == ' ') p++;
            if (*p != '{') continue;
            // 找值对象的匹配 }
            const char* vstart = p;
            int bd = 0;
            while (*p) {
                if (*p == '"') {
                    p++;
                    while (*p && *p != '"') { if (*p == '\\') p++; p++; }
                    if (*p == '"') p++;
                    continue;
                }
                if (*p == '{') bd++;
                else if (*p == '}') { bd--; if (bd == 0) break; }
                p++;
            }
            if (strcmp(key, "__metadata__") != 0 && bd == 0 &&
                n_tensors < IME_MAX_TENSORS) {
                // 值对象体 → TensorInfo
                TensorInfo& ti = tensors[n_tensors];
                snprintf(ti.name, sizeof(ti.name), "%s", key);
                size_t blen = (size_t)(p - vstart + 1);
                char* body = (char*)malloc(blen + 1);
                if (body) {
                    memcpy(body, vstart, blen); body[blen] = '\0';
                    const char* dt = strstr(body, "\"dtype\":\"");
                    if (dt) snprintf(ti.dtype, sizeof(ti.dtype), "%s", dt + 9);
                    else ti.dtype[0] = '\0';
                    for (int c = 0; c < 8 && ti.dtype[c]; c++)
                        if (ti.dtype[c] == '"') { ti.dtype[c] = '\0'; break; }
                    ti.nshape = 0;
                    const char* sh = strstr(body, "\"shape\":[");
                    if (sh) {
                        // 只在 [ ] 区间内解析（防止越界吃到 data_offsets）
                        const char* sp = sh + 9;
                        const char* se = strchr(sp, ']');
                        if (!se) se = body + strlen(body);
                        while (sp < se && ti.nshape < 4) {
                            char* endp = nullptr;
                            long v = strtol(sp, &endp, 10);
                            if (endp == sp) break;
                            ti.shape[ti.nshape++] = (int)v;
                            sp = endp;
                            while (sp < se && (*sp == ' ' || *sp == ',')) sp++;
                        }
                    }
                    const char* dor = strstr(body, "\"data_offsets\":[");
                    if (dor) {
                        unsigned long long o1 = 0, o2 = 0;
                        if (sscanf(dor + 16, "%llu,%llu", &o1, &o2) == 2) {
                            ti.offset = o1; ti.size = o2 - o1;
                        }
                    }
                    free(body);
                }
                if (ti.nshape > 0) n_tensors++;
            }
            if (*p == '}') p++;
        }
        return n_tensors > 0;
    }

    void close() {
        if (f) { fclose(f); f = nullptr; }
        if (header_json) { free(header_json); header_json = nullptr; }
        n_tensors = 0;
    }

    // 读取张量为 float 数组（malloc，调用方 free；int8 自动反量化）
    float* read_tensor(const char* name, int64_t* out_count) {
        TensorInfo* ti = find(name);
        if (!ti) { *out_count = 0; return nullptr; }
        int64_t total = 1;
        for (int i = 0; i < ti->nshape; i++) total *= ti->shape[i];
        *out_count = total;
        float* result = (float*)malloc(total * sizeof(float));
        if (!result) { *out_count = 0; return nullptr; }

        if (strcmp(ti->dtype, "I8") == 0) {
            int8_t* raw = (int8_t*)malloc(total);
            if (!raw) { free(result); *out_count = 0; return nullptr; }
            fseeko(f, (off_t)(data_offset + ti->offset), SEEK_SET);
            if (fread(raw, 1, total, f) != (size_t)total) { free(raw); free(result); *out_count = 0; return nullptr; }
            // 对应 ".scale" 张量（逐行缩放）
            char sname[112];
            snprintf(sname, sizeof(sname), "%s.scale", name);
            TensorInfo* sti = find(sname);
            if (sti) {
                int64_t scale_size = 1;
                for (int i = 0; i < sti->nshape; i++) scale_size *= sti->shape[i];
                float* scales = (float*)malloc(scale_size * sizeof(float));
                if (scales) {
                    fseeko(f, (off_t)(data_offset + sti->offset), SEEK_SET);
                    if (fread(scales, 4, scale_size, f) == (size_t)scale_size) {
                        int cols = ti->shape[ti->nshape - 1];
                        for (int64_t i = 0; i < total; i++)
                            result[i] = (float)raw[i] * scales[i / cols];
                        free(scales); free(raw);
                        return result;
                    }
                    free(scales);
                }
            }
            for (int64_t i = 0; i < total; i++) result[i] = (float)raw[i];
            free(raw);
            return result;
        }
        if (strcmp(ti->dtype, "F32") == 0) {
            fseeko(f, (off_t)(data_offset + ti->offset), SEEK_SET);
            if (fread(result, 4, total, f) != (size_t)total) { free(result); *out_count = 0; return nullptr; }
            return result;
        }
        if (strcmp(ti->dtype, "F16") == 0) {
            uint16_t* raw = (uint16_t*)malloc(total * 2);
            if (!raw) { free(result); *out_count = 0; return nullptr; }
            fseeko(f, (off_t)(data_offset + ti->offset), SEEK_SET);
            if (fread(raw, 2, total, f) != (size_t)total) { free(raw); free(result); *out_count = 0; return nullptr; }
            for (int64_t i = 0; i < total; i++) {
                uint16_t h = raw[i];
                uint32_t sign = (h >> 15) & 1;
                uint32_t exp = (h >> 10) & 0x1F;
                uint32_t mant = h & 0x3FF;
                if (exp == 0) result[i] = (sign ? -1.0f : 1.0f) * (float)mant * 1.4013e-45f;
                else if (exp == 31) result[i] = sign ? -INFINITY : INFINITY;
                else {
                    uint32_t fv = (sign << 31) | ((exp - 15 + 127) << 23) | (mant << 13);
                    memcpy(&result[i], &fv, 4);
                }
            }
            free(raw);
            return result;
        }
        free(result);
        *out_count = 0;
        return nullptr;
    }
};

// ─── 模型 ────────────────────────────────────────────────
struct IMEModel {
    int vocab_size = 8192;
    int n_layer = 4;
    int n_head = 4;
    int n_embd = 224;
    int ctx_len = 128;

    float* tok_w = nullptr;   // [vocab, n_embd]
    float* pos_w = nullptr;   // [ctx, n_embd]
    float* lnf_w = nullptr;   // [n_embd]
    float* lnf_b = nullptr;
    float* ln1_w[IME_MAX_LAYER]; float* ln1_b[IME_MAX_LAYER];
    float* qkv_w[IME_MAX_LAYER]; float* qkv_b[IME_MAX_LAYER];
    float* proj_w[IME_MAX_LAYER]; float* proj_b[IME_MAX_LAYER];
    float* ln2_w[IME_MAX_LAYER]; float* ln2_b[IME_MAX_LAYER];
    float* fc_w[IME_MAX_LAYER]; float* fc_b[IME_MAX_LAYER];
    float* out_w[IME_MAX_LAYER]; float* out_b[IME_MAX_LAYER];

    // 词表：id -> 字符串 + FNV-1a 开放定址哈希
    StrList vocab;
    int* hash_slot = nullptr;
    int hash_cap = 0;
    int bos_id = 2, unk_id = 1;

    // 推理缓冲
    float *x = nullptr, *y = nullptr, *q = nullptr, *k = nullptr, *v = nullptr,
         *att = nullptr, *hidden = nullptr, *tmp = nullptr, *vals = nullptr;
    int ids_buf[640];

    ~IMEModel() { destroy(); }

    void destroy() {
        free(tok_w); free(pos_w); free(lnf_w); free(lnf_b);
        for (int i = 0; i < IME_MAX_LAYER; i++) {
            free(ln1_w[i]); free(ln1_b[i]); free(qkv_w[i]); free(qkv_b[i]);
            free(proj_w[i]); free(proj_b[i]); free(ln2_w[i]); free(ln2_b[i]);
            free(fc_w[i]); free(fc_b[i]); free(out_w[i]); free(out_b[i]);
            ln1_w[i]=ln1_b[i]=qkv_w[i]=qkv_b[i]=proj_w[i]=proj_b[i]=nullptr;
            ln2_w[i]=ln2_b[i]=fc_w[i]=fc_b[i]=out_w[i]=out_b[i]=nullptr;
        }
        tok_w=pos_w=lnf_w=lnf_b=nullptr;
        free(x); free(y); free(q); free(k); free(v);
        free(att); free(hidden); free(tmp); free(vals);
        x=y=q=k=v=att=hidden=tmp=vals=nullptr;
        free(hash_slot); hash_slot = nullptr; hash_cap = 0;
        vocab.free_all();
    }

    static uint32_t fnv1a(const char* s, int len) {
        uint32_t h = 2166136261u;
        for (int i = 0; i < len; i++) { h ^= (unsigned char)s[i]; h *= 16777619u; }
        return h;
    }
    void hash_insert(const char* s, int id) {
        int len = (int)strlen(s);
        uint32_t h = fnv1a(s, len) & (uint32_t)(hash_cap - 1);
        while (hash_slot[h] != 0) h = (h + 1) & (uint32_t)(hash_cap - 1);
        hash_slot[h] = id + 1;
    }
    int lookup(const char* s, int len) const {
        if (!hash_slot) return unk_id;
        uint32_t h = fnv1a(s, len) & (uint32_t)(hash_cap - 1);
        while (hash_slot[h] != 0) {
            int id = hash_slot[h] - 1;
            const char* vs = id < vocab.n ? vocab.items[id] : "";
            if ((int)strlen(vs) == len && memcmp(vs, s, len) == 0) return id;
            h = (h + 1) & (uint32_t)(hash_cap - 1);
        }
        return unk_id;
    }

    bool load(SafetensorsFile& st) {
        int64_t n;
        tok_w = st.read_tensor("tok.weight", &n);
        pos_w = st.read_tensor("pos.weight", &n);
        lnf_w = st.read_tensor("ln_f.weight", &n);
        lnf_b = st.read_tensor("ln_f.bias", &n);
        if (!tok_w || !pos_w) {
            IME_LOG("tok_w=%p pos_w=%p lnf_w=%p", tok_w, pos_w, lnf_w);
            TensorInfo* t = st.find("tok.weight");
            TensorInfo* pp = st.find("pos.weight");
            IME_LOG("idx tok: %s dtype=%s nshape=%d | pos: %s dtype=%s nshape=%d",
                    t ? "found" : "MISSING", t ? t->dtype : "?", t ? t->nshape : -1,
                    pp ? "found" : "MISSING", pp ? pp->dtype : "?", pp ? pp->nshape : -1);
            return false;
        }
        for (int i = 0; i < IME_MAX_LAYER; i++) { fc_b[i] = nullptr; out_b[i] = nullptr; }
        char pname[64];
        for (int i = 0; i < n_layer && i < IME_MAX_LAYER; i++) {
            snprintf(pname, sizeof(pname), "blocks.%d.ln1.weight", i);  ln1_w[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.ln1.bias", i);    ln1_b[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.qkv.weight", i);  qkv_w[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.qkv.bias", i);    qkv_b[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.proj.weight", i); proj_w[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.proj.bias", i);   proj_b[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.ln2.weight", i);  ln2_w[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.ln2.bias", i);    ln2_b[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.fc.weight", i);   fc_w[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.out.weight", i);  out_w[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.fc.bias", i);     fc_b[i] = st.read_tensor(pname, &n);
            snprintf(pname, sizeof(pname), "blocks.%d.out.bias", i);    out_b[i] = st.read_tensor(pname, &n);
            if (!qkv_w[i]) {
                IME_LOG("layer %d load fail: ln1_w=%p qkv_w=%p fc_w=%p (name=%s)",
                        i, ln1_w[i], qkv_w[i], fc_w[i], pname);
                return false;
            }
        }
        int hidden_dim = 4 * n_embd;
        x = (float*)calloc(ctx_len * n_embd, 4);
        y = (float*)calloc(ctx_len * n_embd, 4);
        q = (float*)calloc(ctx_len * n_embd, 4);
        k = (float*)calloc(ctx_len * n_embd, 4);
        v = (float*)calloc(ctx_len * n_embd, 4);
        att = (float*)calloc(ctx_len * ctx_len, 4);
        hidden = (float*)calloc(hidden_dim, 4);
        tmp = (float*)calloc(n_embd, 4);
        vals = (float*)calloc(vocab_size, 4);
        return x && y && q && k && v && att && hidden && tmp && vals;
    }

    bool build_vocab(const StrList& v) {
        // 完整保留原始顺序与下标（token id 不能因剔除特殊 token 而错位）
        for (int i = 0; i < v.n; i++)
            if (!vocab.push(v.items[i], (int)strlen(v.items[i]))) return false;
        hash_cap = 1;
        while (hash_cap < vocab.n * 2) hash_cap <<= 1;
        hash_slot = (int*)calloc(hash_cap, sizeof(int));
        if (!hash_slot) return false;
        int n = v.n < vocab_size ? v.n : vocab_size;
        for (int i = 0; i < n; i++) {
            const char* s = vocab.items[i];
            if (!s[0]) continue;
            if (!strcmp(s, "<pad>") || !strcmp(s, "<unk>") ||
                !strcmp(s, "<bos>")) continue;
            hash_insert(s, i);   // 原始 id
        }
        return true;
    }

    void layer_norm(const float* input, float* output, int T,
                    const float* gamma, const float* beta) {
        for (int t = 0; t < T; t++) {
            const float* in = input + t * n_embd;
            float* out = output + t * n_embd;
            float mean = 0;
            for (int i = 0; i < n_embd; i++) mean += in[i];
            mean /= n_embd;
            float var = 0;
            for (int i = 0; i < n_embd; i++) { float d = in[i] - mean; var += d * d; }
            var /= n_embd;
            float inv_std = 1.0f / sqrtf(var + 1e-5f);
            for (int i = 0; i < n_embd; i++)
                out[i] = (in[i] - mean) * inv_std * gamma[i] + beta[i];
        }
    }

    static void matvec(const float* W, const float* b, const float* x_t,
                       float* y_t, int out_dim, int in_dim) {
        for (int o = 0; o < out_dim; o++) {
            float sum = b ? b[o] : 0.0f;
            const float* w_row = W + (int64_t)o * in_dim;
            for (int i = 0; i < in_dim; i++) sum += w_row[i] * x_t[i];
            y_t[o] = sum;
        }
    }

    static float gelu(float val) {
        return 0.5f * val * (1.0f + erff(val / 1.41421356f));
    }

    void forward(const int* ids, int T) {
        int head_dim = n_embd / n_head;
        float scale = 1.0f / sqrtf((float)head_dim);

        for (int t = 0; t < T; t++) {
            const float* tok_row = &tok_w[(int64_t)ids[t] * n_embd];
            const float* pos_row = &pos_w[(int64_t)t * n_embd];
            float* x_row = &x[(int64_t)t * n_embd];
            for (int i = 0; i < n_embd; i++) x_row[i] = tok_row[i] + pos_row[i];
        }

        for (int layer = 0; layer < n_layer; layer++) {
            layer_norm(x, y, T, ln1_w[layer], ln1_b[layer]);

            // QKV 投影（拼接权重按 [3*n_embd, n_embd] 拆三段）
            for (int t = 0; t < T; t++) {
                const float* y_t = &y[(int64_t)t * n_embd];
                matvec(qkv_w[layer], qkv_b[layer], y_t, &q[(int64_t)t * n_embd], n_embd, n_embd);
                matvec(qkv_w[layer] + (int64_t)n_embd * n_embd,
                       qkv_b[layer] ? qkv_b[layer] + n_embd : nullptr, y_t,
                       &k[(int64_t)t * n_embd], n_embd, n_embd);
                matvec(qkv_w[layer] + 2ll * n_embd * n_embd,
                       qkv_b[layer] ? qkv_b[layer] + 2 * n_embd : nullptr, y_t,
                       &v[(int64_t)t * n_embd], n_embd, n_embd);
            }

            // 因果注意力
            for (int t = 0; t < T; t++) {
                for (int h = 0; h < n_head; h++) {
                    int off = h * head_dim;
                    for (int s = 0; s <= t; s++) {
                        float dot = 0;
                        for (int d = 0; d < head_dim; d++)
                            dot += q[(int64_t)t * n_embd + off + d] * k[(int64_t)s * n_embd + off + d];
                        att[(int64_t)t * ctx_len + s] = dot * scale;
                    }
                    float max_val = -INFINITY;
                    for (int s = 0; s <= t; s++)
                        if (att[(int64_t)t * ctx_len + s] > max_val)
                            max_val = att[(int64_t)t * ctx_len + s];
                    float sum = 0;
                    for (int s = 0; s <= t; s++) {
                        att[(int64_t)t * ctx_len + s] = expf(att[(int64_t)t * ctx_len + s] - max_val);
                        sum += att[(int64_t)t * ctx_len + s];
                    }
                    float inv_sum = 1.0f / sum;
                    for (int d = 0; d < head_dim; d++) {
                        float acc = 0;
                        for (int s = 0; s <= t; s++)
                            acc += att[(int64_t)t * ctx_len + s] * v[(int64_t)s * n_embd + off + d];
                        y[(int64_t)t * n_embd + off + d] = acc * inv_sum;
                    }
                }
            }

            // 输出投影 + 残差
            for (int t = 0; t < T; t++) {
                matvec(proj_w[layer], proj_b[layer], &y[(int64_t)t * n_embd], tmp, n_embd, n_embd);
                for (int i = 0; i < n_embd; i++) x[(int64_t)t * n_embd + i] += tmp[i];
            }

            // LN2 + FFN + 残差（fc/out 可能无 bias：4M 有、25M 无）
            layer_norm(x, y, T, ln2_w[layer], ln2_b[layer]);
            int hidden_dim = 4 * n_embd;
            for (int t = 0; t < T; t++) {
                matvec(fc_w[layer], fc_b[layer], &y[(int64_t)t * n_embd], hidden, hidden_dim, n_embd);
                for (int i = 0; i < hidden_dim; i++) hidden[i] = gelu(hidden[i]);
                matvec(out_w[layer], out_b[layer], hidden, tmp, n_embd, hidden_dim);
                for (int i = 0; i < n_embd; i++) x[(int64_t)t * n_embd + i] += tmp[i];
            }
        }

        layer_norm(x, y, T, lnf_w, lnf_b);   // 只做一次
    }

    static int utf8_char_len(const char* s) {
        unsigned char c = (unsigned char)*s;
        if ((c & 0x80) == 0) return 1;
        if ((c & 0xE0) == 0xC0) return 2;
        if ((c & 0xF0) == 0xE0) return 3;
        if ((c & 0xF8) == 0xF0) return 4;
        return 1;
    }

    int encode(const char* text, int bytes, int* out, int max_out) const {
        int n = 0;
        const char* p = text;
        const char* end = text + bytes;
        while (p < end && n < max_out) {
            int len = utf8_char_len(p);
            if (p + len > end) len = 1;
            out[n++] = lookup(p, len);
            p += len;
        }
        return n;
    }

    // 打分: log P(candidate | context) / len(candidate)
    float score(const char* context, const char* candidate) {
        int cand_ids[64];
        int cand_len = encode(candidate, (int)strlen(candidate), cand_ids, 64);
        if (cand_len == 0) return -INFINITY;

        int ctx_raw[640];
        int ctx_n = encode(context, (int)strlen(context), ctx_raw, 640);

        int max_ctx = ctx_len - cand_len - 1;
        if (max_ctx < 0) max_ctx = 0;
        int ctx_start = ctx_n > max_ctx ? ctx_n - max_ctx : 0;

        // 序列: [BOS] + context(截断后) + candidate；构造保证 T ≤ ctx_len
        int prefix_len = 1 + (ctx_n - ctx_start);
        int T = prefix_len + cand_len;

        ids_buf[0] = bos_id;
        for (int i = 0; ctx_start + i < ctx_n; i++) ids_buf[1 + i] = ctx_raw[ctx_start + i];
        for (int i = 0; i < cand_len; i++) ids_buf[prefix_len + i] = cand_ids[i];

        forward(ids_buf, T);

        double total_logprob = 0;
        for (int pos = prefix_len - 1; pos < T - 1; pos++) {
            const float* h = &y[(int64_t)pos * n_embd];
            float max_val = -INFINITY;
            for (int vi = 0; vi < vocab_size; vi++) {
                const float* tok_row = &tok_w[(int64_t)vi * n_embd];
                float sum = 0;
                for (int i = 0; i < n_embd; i++) sum += h[i] * tok_row[i];
                vals[vi] = sum;
                if (sum > max_val) max_val = sum;
            }
            float logsumexp = 0;
            for (int vi = 0; vi < vocab_size; vi++)
                logsumexp += expf(vals[vi] - max_val);
            logsumexp = max_val + logf(logsumexp);
            total_logprob += vals[ids_buf[pos + 1]] - logsumexp;
        }
        return (float)(total_logprob / cand_len);
    }
};

// ─── C API ───────────────────────────────────────────────
static SafetensorsFile g_st;
static IMEModel g_model;
static bool g_loaded = false;

extern "C" {

const char* ime_version(void) { return "ime_inference v2.0 (char-transformer, no-cpp-rt)"; }

void* ime_load(const char* model_path) {
    IME_LOG("loading %s", model_path);
    if (g_loaded) { g_model.destroy(); g_st.close(); g_loaded = false; }
    if (!g_st.open(model_path)) {
        IME_LOG("safetensors open failed");
        return nullptr;
    }
    IME_LOG("tensors found: %d", g_st.n_tensors);
    StrList vocab;
    json_parse_string_array(g_st.header_json, "vocab", &vocab);
    IME_LOG("vocab entries: %d", vocab.n);
    char* config = json_find_string_dup(g_st.header_json, "config");
    if (config) {
        g_model.vocab_size = json_find_int(config, "vocab", 8192);
        g_model.n_layer = json_find_int(config, "n_layer", 6);
        g_model.n_head = json_find_int(config, "n_head", 4);
        g_model.n_embd = json_find_int(config, "n_embd", 192);
        g_model.ctx_len = json_find_int(config, "context", 64);
        free(config);
    }
    IME_LOG("cfg: vocab=%d layer=%d head=%d embd=%d ctx=%d",
            g_model.vocab_size, g_model.n_layer, g_model.n_head, g_model.n_embd, g_model.ctx_len);
    if (!g_model.load(g_st)) {
        IME_LOG("model load failed");
        vocab.free_all();
        return nullptr;
    }
    bool ok = g_model.build_vocab(vocab);
    vocab.free_all();
    if (!ok) { g_model.destroy(); IME_LOG("vocab build failed"); return nullptr; }
    g_loaded = true;
    IME_LOG("loaded OK");
    return (void*)&g_model;
}

float ime_score(void* handle, const char* context, const char* candidate) {
    if (!handle || !g_loaded) return -INFINITY;
    return ((IMEModel*)handle)->score(context, candidate);
}

int ime_best(void* handle, const char* context,
             const char** candidates, int n_candidates) {
    if (!handle || !g_loaded || n_candidates <= 0) return -1;
    IMEModel* m = (IMEModel*)handle;
    int best_idx = 0;
    float best_score = -INFINITY;
    for (int i = 0; i < n_candidates; i++) {
        float s = m->score(context, candidates[i]);
        if (s > best_score) { best_score = s; best_idx = i; }
    }
    return best_idx;
}

void ime_free(void* handle) {
    if (handle) {
        g_model.destroy();
        g_st.close();
        g_loaded = false;
    }
}

} // extern "C"
