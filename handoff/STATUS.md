# 状态报告

## 已验证 ✓（2026-09-06，Arena，基于 main@7639637）

### 环境/仓库
- Arena 侧已通过 Deploy Key（arena-ondevice-translator-ro）克隆私有仓库，`main @ 7639637`，工作树干净，63 文件与交接一致
- 工作分支：`arena/fix-first-build-blockers`（含本分支全部补丁，见下方）

### 外部事实核查（有证据，可复核）
- llama.cpp `v0.4.0` 标签**真实存在**：`5266f24`（2026-09-04），是最新语义化版本标签 → `tools/build_llama_ios.sh` 的 `--branch v0.4.0` 可用
- v0.4.0 **支持 hunyuan-dense 架构**（src/llama-arch.cpp:121、llama-model.cpp:288、tokenizer "hunyuan"/"hunyuan-dense"）
- v0.4.0 的 `llama_model_params` **没有** `use_mmap`/`use_lock` 字段（改用 `load_mode` 枚举）；`llama_sampler_init_penalties` 改为 **5 参** `(n_vocab, penalty_last_n, ...)`——原 LlamaBridge.mm 两处必然编译失败
- 上述结论用 g++ 对真实 v0.4.0 头文件做了探针编译双向验证：补丁后写法 `-fsyntax-only` 通过；补丁前写法复现出完全相同的 3 个错误
- Apple 文档确认：`LanguageAvailability.Status` 现为 `installed/supported/unsupported`（无 `.downloadRequired`）；`TranslationSession(installedSource:target:)` 为真实 iOS 26 API；`TranslationSession.prepareTranslation(for:)` 静态方法查无此 API（实例方法才存在）
- `python3 -m pytest tests/`：39 passed（core 逻辑）
- `python3 tools/lint_platform_code.py`：通过（仅静态约束，非编译验证）
- `tools/build_manifest.py --skip-url-check` 参数存在（install 脚本的调用成立）
- core/prompt.py 采样参数（0.7/0.6/20/1.05 与 0.3/0.8/40/1.08）与 bridge 的 defaults/retry 一致

### 本分支修复的编译阻塞（9 处，均为"首个真机构建"链路上的必炸点）
| # | 文件 | 问题 |
|---|---|---|
| 1 | ios/Bridge/LlamaBridge.mm | `mp.use_mmap`/`mp.use_lock` 字段在 v0.4.0 不存在 → 改 `mp.load_mode = LLAMA_LOAD_MODE_MMAP`（刻意不用 MLOCK：会钉死 462MB 物理内存） |
| 2 | ios/Bridge/LlamaBridge.mm | `llama_sampler_init_penalties` 4 参调用 → 5 参；且新 API 里 last_n=0 是关闭惩罚，显式给 64 |
| 3 | ios/Sources/EngineFactory.swift | `make()` 返回裸 `HyMTLlamaEngine`，不满足 `TranslateEngine.translate()` 协议 → 包成 `LocalLLMTranslateEngine`（分句/术语/校验链路本就该走这里） |
| 4 | ios/Sources/EngineFactory.swift | switch `.downloadRequired`（SDK 无此 case）+ 不存在的静态 `prepareTranslation(for:)` → 改 `default:` 抛可见错误（符合"绝不无声兜底"规则） |
| 5 | ios/Sources/LocalMTApp.swift + LocalLLMEngine.swift | 两处同名 `Character.isCJK` 扩展 → invalid redeclaration；保留一处（区间取并集） |
| 6 | ios/Sources/ModelStore.swift | 引用不存在的 `NetworkPath`/`NWPathParameters` 且未 import Network → 重写为 `NWPathMonitor` 最小实现 `NetworkChecker` |
| 7 | ios/Sources/ModelStore.swift | 非 throws 函数里裸 `try availableCapacity()` → `try?`（拿不到容量按乐观处理） |
| 8 | ios/Sources/ModelStore.swift | `SHA256.Digest.hexString` 不存在 → 手工 `%02x` 拼 hex |
| 9 | ios/project.yml | `SWIFT_VERSION "5.10"` 不是合法语言模式（仅 4/4.2/5/6）→ 改 `"5.0"` |

附带修正：`SamplingParams` 补 `.standard`（原代码在用但未定义）；`.safe` 温度从 0.7 改 0.3——bridge 按 `temperature<0.5` 选重试档，0.7 会导致降档重试永远不触发。

### 顺带记录、未修（超出本次最小范围）
- android/app/src/main/cpp/llama_jni.cpp:57-58 有同样的 `use_mmap/use_lock` 漂移（Android 是后续阶段）
- ModelStore 的下载代理把 `URLSessionDataDelegate` 回调用在 `downloadTask` 上（数据回调不触发、完成回调不落盘）——阶段 2 的运行时下载路径需重写；本次只保证编译通过
- ModelStore 的 `states` AsyncStream 的 continuation 未接到 `self.continuation`（进度流不产数据，编译无碍）
- `llama_new_context_with_model` 在 v0.4.0 已标记 DEPRECATED（仍可编译，仅警告）；将来统一换 `llama_init_from_model`
- 采样链顺序（temp→top_k→top_p→penalties）与 llama.cpp 官方示例（penalties 在前）不同，效果未实测，留待真机调优

## 待验证 ~（需要 Mac/Hermes）
- [ ] `tools/build_llama_ios.sh` 产出 `ios/Vendor/llama.xcframework`（关注点：脚本 find -maxdepth 4 能否捞到全部 lib*.a；xcframework 要求 libllama.a 必在）
- [ ] `xcodegen generate` + `xcodebuild` 真机 arm64 编译（本分支修完 9 处后应能过；Swift 侧Arena 无法编译，仍可能有个别漏网）
- [ ] 462MB 权重下载 + SHA256 + manifest 生成
- [ ] 装机、信任开发者、飞行模式中↔英实测

## 依赖缺失
- Mac 侧：Xcode 26.6 已装（Hermes 报告）；`xcodegen`/`cmake`/`ninja` 由 install 脚本 preflight 自检提示
- Arena 侧沙箱：无 Xcode/macOS SDK，Swift 无法编译验证（已用静态审查 + 头文件探针兜底）
