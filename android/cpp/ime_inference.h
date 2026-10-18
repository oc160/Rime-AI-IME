// ime_inference.h — 4M 字符级打分器 C++ 进程内推理（零外部依赖）
// 用法: 编译为共享库，从 Java/Lua/C 调用
#ifndef IME_INFERENCE_H
#define IME_INFERENCE_H

#ifdef _WIN32
#define IME_EXPORT __declspec(dllexport)
#else
#define IME_EXPORT __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

// 初始化：加载 safetensors 模型文件，返回句柄（失败返回 NULL）
IME_EXPORT void* ime_load(const char* model_path);

// 打分：log P(candidate | context) / len(candidate)
// 返回归一化对数概率（越大越好）
IME_EXPORT float ime_score(void* handle, const char* context, const char* candidate);

// 批量选优：返回最佳候选的索引（0-based）
IME_EXPORT int ime_best(void* handle, const char* context,
             const char** candidates, int n_candidates);

// 释放
IME_EXPORT void ime_free(void* handle);

// 获取版本信息
IME_EXPORT const char* ime_version(void);

#ifdef __cplusplus
}
#endif

#endif // IME_INFERENCE_H
