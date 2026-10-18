/* bcmp 垫片：zig/musl 目标会引用 bcmp（memcmp 的 BSD 别名），
   Android bionic 不导出该符号导致 dlopen 失败。
   手写字节循环，不产生任何 libc 调用，避免编译器再展开成 bcmp。 */
__attribute__((visibility("default")))
int bcmp(const void* a, const void* b, unsigned long n) {
  const unsigned char* x = (const unsigned char*)a;
  const unsigned char* y = (const unsigned char*)b;
  for (unsigned long i = 0; i < n; ++i) if (x[i] != y[i]) return 1;
  return 0;
}
