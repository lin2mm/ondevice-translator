# 03 · iOS 实现（目标机：iPhone 15 / A16 / 6GB / iOS 26.x）

## 1. 先定生死线

- **iPhone 15 不支持 Apple Intelligence**（A16 + 6GB，被永久排除）→ `FoundationModels.SystemLanguageModel` 在这台机器上不可用。不要把它当主引擎；可以作为"有则更好"的加分路径（运行时查 `availability`）。
- 可以用的是 **`Translation` 框架**（iOS 18+ 的 `TranslationSession`，系统托管离线语言包，与 Apple Intelligence 无关）：零 App 体积、内存友好，适合键盘/分享扩展这类内存上限很死的容器。
- 主力必须自带：llama.cpp + `Hy-MT2-1.8B-1.25Bit.gguf`（462MB）。
- 中国大陆地区：Apple Intelligence 能力受地区限制，`Translation` 语言包下载也依赖系统。上线前在**中国区真机**确认 `LanguageAvailability().status(from:to:)` 是否 `.installed`，别假设。

## 2. 工程设置

| 项 | 值 | 为什么 |
|---|---|---|
| Deployment target | iOS 18.0 | `Translation` 框架 iOS 18 起；`TranslationSession(installedSource:target:)` 直接构造需 iOS 26 → 写 `if #available(iOS 26.0, *)`，否则回退 `.translationTask` 修饰符 |
| 架构 | arm64 only | 省一半体积；模拟器跑 Metal 没意义 |
| 首包 | ≤ 45MB（llama.cpp xcframework 约 8–14MB） | 权重走下载，不进 ipa |
| Entitlement | `com.apple.developer.kernel.increased-memory-limit` | 6GB 机器上抬升可用内存上限，是 mmap 权重 + KV cache 不被 jetsam 的保险 |
| ATS | 允许 HTTP 仅用于本地调试；生产全 HTTPS | 审核 |
| 隐私 | 隐私营养标签填"不收集数据"（前提：埋点也不上传文本） | 真离线是本产品的卖点 |

## 3. llama.cpp 静态库（脚本在 `tools/build_llama_ios.sh`）

关键 CMake 开关（写死在脚本里，别手敲）：

```
-DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=18.0
-DLLAMA_METAL=ON -DLLAMA_METAL_EMBED_LIBRARY=ON
-DLLAMA_BUILD_APP=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
-DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF
```

四个必须知道的坑：
1. **`LLAMA_METAL_EMBED_LIBRARY=ON`**：不内嵌 Metal 源，运行时找不到 `.metal` 资源会**静默回落 CPU**，慢 3–5 倍且没有报错。判据：跑分后看 `ggml_backend` 日志里有没有 `METAL`。
2. 权重文件放 **`Application Support/Models/`**，不要放 `tmp/`（会被系统清理）、不要放 `Bundle`（等于把 462MB 打进 ipa）。
3. 加载参数：`use_mmap = true`、`n_gpu_layers = 999`（Metal）、`ctx = 2048` 起，内存告警降到 1024；`n_threads = 4`（A16 上更多线程反而更慢更热）。
4. 用 GGUF 自带 chat template 渲染 messages（tokenizer.chat_template），别手拼 ChatML。**停止词必须同时包含 end-of-turn 特殊 token 与换行符**，否则模型会把下一轮的 user 提示词也生成出来，界面上就是一坨自问自答。判据：译文里绝不该出现 "user" 或 "###" 字样 —— 加进 OutputGuard 的黑名单。

## 4. 代码分层（`ios/Sources/`）

| 文件 | 职责 | 对应 Python 规格 |
|---|---|---|
| `TranslationEngine.swift` | `Engine` 协议 + 三后端选择器（A: llama.cpp，C: 系统 Translation） | `core/engine.py` |
| `HyMTLlamaEngine.swift` | llama.cpp 封装：加载/生成/取消/内存告警卸载 | 同上 |
| `AppleTranslationEngine.swift` | `TranslationSession` 包装，iOS 26 直连 + iOS 18 修饰符回退 | — |
| `ModelStore.swift` | manifest 解析、断点续传、sha256 校验、磁盘预算、降级 | `core/model_manifest.py` |
| `TranslateCore.swift` | **待移植**：分句/保护/体检/缓存。行为以 `core/*.py` 为准，移植后用 `tests/golden.jsonl` 对拍 | `segmenter.py` `validate.py` |

## 5. 内存与发热（6GB 机器的实操）

- 监听 `DispatchSource.makeMemorySource` + `UIApplication.didReceiveMemoryWarningNotification`：告警即 `unload()`（下次翻译再 mmap，冷启动 ~200–400ms，可接受）。
- 长文（>1200 token）**必须让 UI 分批**：每 4 个 chunk 让出一次 runloop，并检查 `ProcessInfo.processInfo.thermalState`；`.serious/.critical` 时自动把 ctx 降到 1024、切 `C档`（系统 Translation）并在界面写明"为控制发热暂时使用系统翻译"。
- 空闲 60s 或切后台 → `unload`。**不要**为"秒开"常驻模型：iOS 后台被 jetsam 杀掉是常态，还落一个"耗电"差评。
- 不要用 `Timer` 轮询式 streaming；用 `AsyncStream` + 每 ~40ms 合并一次 UI 更新（translation token 很快，逐 token 刷 SwiftUI 会把 CPU 吃光）。

## 6. 扩展与系统集成（"轻量"的另一半）

- **键盘扩展不能自带 LLM**：iOS 键盘扩展内存上限极紧，462MB mmap 会被直接杀。正解：键盘只做输入 + `UITextInputTraits`/CustomAction 把文本丢回主 App 翻译（Apple 对自定义键盘的推荐做法），或在扩展里用 `TranslationSession`（系统进程承担模型）。
- **分享扩展（Share Extension）**：同理用 `TranslationSession`；LLM 路径改为"写入 App Group 队列 → 唤起主 App 处理"。
- **App Intents / Shortcuts / Spotlight / Live Text**：`TranslateIntent(text:, source:, target:)` 让"翻译选中内容""照片里文字翻译"零成本可用 —— 微软 Translator 的差距主要就在这些系统入口，而不是模型质量。
- **App Group**：`group.dev.localmt`，扩展与主 App 共享术语表与 TM（只读镜像，避免并发写 SQLite）。
- iOS 27（若你愿意等）：Foundation Models 开放了自定义模型提供方（Core AI / MLX），LiteRT-LM v0.15 已带 Apple Foundation 适配器 → 你的 Hy-MT2 可作为 provider 暴露给系统级 API。当前先按 iOS 26 落地。

## 7. 打包与分发（对应"两个安装包 + 全免费"）

| 路线 | 成本 | 限制 |
|---|---|---|
| App Store | 99 USD/年 | 唯一"永久安装不掉的免费"路径；审核要说明"完全离线、不收集数据、含开源权重声明" |
| TestFlight | 0（含在开发者账号内） | 版本 90 天过期、1 万人上限 → 不适合"永久" |
| 免费个人账号自签（Xcode / AltStore / SideStore） | 0 | 7 天重签、同时最多 3 个 App、不能后台常驻 |
| 欧盟第三方分发（iOS 17.4+） | 视商店 | 只覆盖欧盟，且仍要走 notarization |

建议：**iOS 走 App Store 免费（付费开发者账号是这里唯一绕不开的钱），Android 走 Play 免费上架（一次性 25 USD）+ 官网 APK 侧载兜底。**

## 8. 上真机前自查

1. `Increased Memory Limit` 已加且 provisioning profile 里生效（否则 entitlement 被静默丢弃）。
2. 权重路径在 `Application Support`，被 iOS 备份**排除**（462MB 不该进 iCloud 备份；用 `URLResourceKey.filePathKey` + `isExcludedFromBackupKey`）。
3. 首屏在无网（飞行模式）下从冷启动到出第一个 token < 3s。
4. 低电量模式 + `.serious` 热状态下不会崩，只会降级。
5. iPhone 15 与 iPhone 17 Pro 上延迟差 < 4×，否则说明 Metal 没生效。
