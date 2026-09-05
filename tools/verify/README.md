# llama.cpp API 编译级核对探针

用途：用真实 llama.cpp 头文件验证 `ios/Bridge/LlamaBridge.mm` 所用 API 的存在性与签名，
不需要 Xcode / Mac（Linux 的 g++ 即可），也不需要编权重或链接。

- `probe_v040.cpp` —— 补丁后 LlamaBridge 用到的**全部** llama 调用的最小复刻，应编译通过
- `probe_old.cpp` —— 补丁前写法（`use_mmap`/`use_lock` + 4 参 penalties），**预期编译失败**，
  作为 v0.4.0 API 漂移的对照证据

跑法（会浅克隆 llama.cpp，约几秒～几分钟视网络）：

    ./tools/verify/verify_llama_api.sh            # 默认核对 v0.4.0
    ./tools/verify/verify_llama_api.sh v0.5.0     # 升级 llama 版本后先跑这个再改桥接代码

结论记录：handoff/STATUS.md（2026-09-06，基于 v0.4.0 = 5266f24）。
