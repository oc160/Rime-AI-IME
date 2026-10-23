// ime_lua.cpp — librime-lua C 模块（零 C++ 运行库）
// require("ime"):
//   ime.version()              -> string
//   ime.load(path)             -> bool（同步，~0.1s，init 时调一次）
//   ime.query(key, ctx, {候选}) -> true（后台线程打分，立即返回）
//   ime.pickup()               -> nil | {key, idx(1基), margin}
// 设计对应 PC 端 v8：查询异步化，击键路径只做微秒级状态读写。
// 编译: clang++ -fPIC -shared -fno-exceptions -fno-rtti -nostdlib++ \
//         ime_lua.cpp ime_inference.cpp -l:librime_jni.so -lm -llog
extern "C" {
#include <lua.h>
#include <lauxlib.h>
#include "ime_inference.h"
}
#include <pthread.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#define IME_KEY_CAP   256
#define IME_CTX_CAP   768
#define IME_CAND_MAX  32
#define IME_CAND_CAP  48

struct ImeJob {
    char key[IME_KEY_CAP];
    char ctx[IME_CTX_CAP];
    char cands[IME_CAND_MAX][IME_CAND_CAP];
    int n;
};
struct ImeResult {
    char key[IME_KEY_CAP];
    int idx;
    float margin;
};

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv = PTHREAD_COND_INITIALIZER;
static ImeJob    g_job;     static bool g_job_has = false;
static ImeResult g_res;    static bool g_res_has = false;
static pthread_t g_thr;    static bool g_thr_started = false;
static void*     g_handle = nullptr;

static void* ime_worker(void*) {
    for (;;) {
        pthread_mutex_lock(&g_mu);
        while (!g_job_has) pthread_cond_wait(&g_cv, &g_mu);
        ImeJob job;
        memcpy(&job, &g_job, sizeof(ImeJob));
        g_job_has = false;
        pthread_mutex_unlock(&g_mu);

        ImeResult r;
        r.key[0] = '\0'; r.idx = 0; r.margin = 0.0f;
        snprintf(r.key, IME_KEY_CAP, "%s", job.key);
        float best = -1e30f, second = -1e30f; int bi = -1;
        for (int i = 0; i < job.n; i++) {
            float s = ime_score(g_handle, job.ctx, job.cands[i]);
            if (s > best) { second = best; best = s; bi = i; }
            else if (s > second) second = s;
        }
        if (bi >= 0) {
            r.idx = bi + 1;
            r.margin = (job.n > 1) ? (best - second) : 1.0f;
        } else {
            r.idx = 0;
        }

        pthread_mutex_lock(&g_mu);
        memcpy(&g_res, &r, sizeof(ImeResult));
        g_res_has = true;
        pthread_mutex_unlock(&g_mu);
    }
    return nullptr;
}

extern "C" {

static int l_version(lua_State* L) {
    lua_pushstring(L, ime_version());
    return 1;
}

static int l_load(lua_State* L) {
    const char* path = luaL_checkstring(L, 1);
    g_handle = ime_load(path);
    lua_pushboolean(L, g_handle != nullptr);
    return 1;
}

static int l_query(lua_State* L) {
    const char* key = luaL_checkstring(L, 1);
    const char* ctx = luaL_optstring(L, 2, "");
    luaL_checktype(L, 3, LUA_TTABLE);
    ImeJob job;
    snprintf(job.key, IME_KEY_CAP, "%s", key);
    snprintf(job.ctx, IME_CTX_CAP, "%s", ctx);
    job.n = 0;
    int n_tab = (int)lua_rawlen(L, 3);
    for (int i = 1; i <= n_tab && job.n < IME_CAND_MAX; i++) {
        lua_rawgeti(L, 3, i);
        if (lua_type(L, -1) == LUA_TSTRING) {
            snprintf(job.cands[job.n], IME_CAND_CAP, "%s", lua_tostring(L, -1));
            job.n++;
        }
        lua_pop(L, 1);
    }
    if (job.n < 2 || !g_handle) { lua_pushboolean(L, 0); return 1; }
    if (!g_thr_started) {
        if (pthread_create(&g_thr, nullptr, ime_worker, nullptr) == 0) g_thr_started = true;
    }
    if (!g_thr_started) { lua_pushboolean(L, 0); return 1; }
    pthread_mutex_lock(&g_mu);
    memcpy(&g_job, &job, sizeof(ImeJob));   // 只留最新请求
    g_job_has = true;
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_mu);
    lua_pushboolean(L, 1);
    return 1;
}

static int l_pickup(lua_State* L) {
    pthread_mutex_lock(&g_mu);
    bool has = g_res_has;
    ImeResult r;
    memcpy(&r, &g_res, sizeof(ImeResult));
    g_res_has = false;
    pthread_mutex_unlock(&g_mu);
    if (!has || r.idx == 0) { lua_pushnil(L); return 1; }
    lua_createtable(L, 0, 3);
    lua_pushstring(L, r.key);               lua_setfield(L, -2, "key");
    lua_pushinteger(L, (lua_Integer)r.idx); lua_setfield(L, -2, "idx");
    lua_pushnumber(L, (lua_Number)r.margin); lua_setfield(L, -2, "margin");
    return 1;
}

__attribute__((visibility("default")))
int luaopen_ime(lua_State* L) {
    static const luaL_Reg reg[] = {
        {"version", l_version},
        {"load",    l_load},
        {"query",   l_query},
        {"pickup",  l_pickup},
        {nullptr, nullptr}
    };
    lua_newtable(L);
    for (const luaL_Reg* r = reg; r->name; r++) {
        lua_pushcfunction(L, r->func);
        lua_setfield(L, -2, r->name);
    }
    return 1;
}

} // extern "C"
