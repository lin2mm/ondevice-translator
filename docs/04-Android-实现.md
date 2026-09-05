# 04 · Android 实现（覆盖 iPhone 15 同代的 6GB 机 + 4GB 老机）

## 1. 关键结论

- **别用 MediaPipe LLM Inference API 起新项目**：官方已标注"只维护不加新功能"，新特性给 **LiteRT-LM**（v0.16 有 C API 预编译库；Kotlin 稳定、Swift 早期预览）。
- **ML Kit GenAI 没有翻译 API**：官方列出的能力是 Summarization / Proofreading / Rewriting / Image description / Speech recognition / Prompt。想用 Gemini Nano 只能走 Prompt API 自写指令，且机型白名单窄（Pixel 9–11、Galaxy S24–S26、Xiaomi 15 等）→ 只作为"加速档"，不能当基座。
- 经典 **ML Kit `translate`**（约 30MB/语言、59 语、离线）适合当 C 档兜底：非配对语走英文枢轴，中英之间是直译，质量可用但不如自带模型。
- 主力：**llama.cpp 预编译 `.so` + `Hy-MT2-1.8B-1.25Bit.gguf`（462MB）**。4GB 机型降到 B 档（Marian/ONNX 或系统 ML Kit）。

## 2. Gradle / NDK 设置

```kotlin
android {
    namespace = "dev.localmt"
    compileSdk = 36
    defaultConfig {
        minSdk = 28            // API 28 = Android 9，llama.cpp armv8.2 dotprod 有保底
        ndk { abiFilters += listOf("arm64-v8a") }   // 只发 arm64；32 位老机走 C 档
        buildConfigField("long", "MODEL_BYTES", "462000000L")
    }
    packaging { jniLibs { useLegacyPackaging = false } }   // 必须： uncompressed + 16KB 对齐
    ndkVersion = "27.2.12479018"
    externalNativeBuild { cmake { path = file("src/main/cpp/CMakeLists.txt") } }
}
```

**16 KB 页大小是硬门槛**（Android 15+ 设备/模拟器可开 16KB 模式；新设备上架要求对齐）：
- 自己编译 llama.cpp 时加 `-DCMAKE_ANDROID_NDK_VERSION` 与
  `-DCMAKE_CXX_FLAGS="-march=armv8.2-a+dotprod+fp16 -fPIC"`，
  链接加 `-Wl,-z,max-page-size=16384`；
- 验证：`objdump -p libllama.so | grep LOAD` 的 align 应为 `0x4000`；或 `python3 tools/check_apk_align.py app.apk`。
- 直接下别人的 `.so` 大概率没做这件事 → 在 16KB 设备上 `dlopen` 失败，报 `UnsatisfiedLinkError`。**必须在 16KB 模式模拟器上跑一次**。

其他必设：
1. `android:extractNativeLibs="false"`（配合 uncompressed .so，安装后不复制一份，省 ~30MB）。
2. `android:largeHeap="true"` **不需要**（权重是 mmap，不进 Java 堆）；加了反而误导。
3. `android:hardwareAccelerated` 无关；GNN 后端：优先 `-DLLAMA_OPENCL=OFF`（llama.cpp 主线的 GPU 路径不稳定），CPU + KleidiAI/SME2 已是当前最稳的组合；`Hy-MT2` 的 2-bit 版官方称在 **Arm SME2** 设备上更快 → 支持检测：`cpuinfo` 里有没有 `sme`。
4. ABI 只发 arm64 后 APK 体积 ~14MB（含 JNI + Kotlin）；**别为体积把 x86_64 砍掉给模拟器留一份**（CI 里模拟器要跑 C 档冒烟）。

## 3. 目录与文件放置

```
/app/src/main/java/dev/localmt/
  engine/Engine.kt              协议（与 core/engine.py 同语义）
  engine/HyMTLlamaEngine.kt     JNI 封装
  engine/MlKitTranslateEngine.kt  C 档兜底
  ml/LLamaBridge.kt + cpp/llama_jni.cpp + cpp/CMakeLists.txt
  model/ModelStore.kt           manifest / sha256 / 断点续传 / 磁盘预算
  model/ModelSyncWorker.kt      WorkManager 下载（Constraints: NETWORK_TYPE_WIFI + DEVICE_IDLE）
  learn/Learner.kt              SQLite（与 core/learn.py 对拍：TM + 术语挖掘）
  text/{Segmenter,Glossary,OutputGuard}.kt
  ui/…                          Compose：Main / Pack / Glossary / Learned / History
```

**模型文件放 `context.filesDir/models/`**（不是 `cacheDir`，会被清；也不是 `noBackupFilesDir`——那个也要显式设置才不备份，`filesDir` 默认参与 Auto Backup，462MB 进云备份是灾难）→ 正确做法：
```kotlin
val dir = File(context.filesDir, "models").apply { mkdirs() }
// 并在 res/xml/backup_rules.xml 里 <exclude domain="file" path="models"/>
```

## 4. 内存 / 后台 / OEM（这才是 Android 上"跑不起来"的真原因）

| 问题 | 处理 |
|---|---|
| 系统内存压力 | `ComponentCallbacks2.onTrimMemory`：`>= TRIM_MEMORY_BACKGROUND` → `unload()`；`TRIM_MEMORY_COMPLETE` → 同时清 LRU 缓存 |
| 低内存机型（<4GB 可用） | manifest `min_ram_bytes` 自动降到 B 档；UI 明示"当前为轻量模式" |
| 后台被杀（MIUI/ColorOS/OneUI） | 只做前台服务（`foregroundServiceType="dataSync"` 仅用于下载，翻译不需要服务）；引导用户加白名单要**可选**，别硬拦 |
| 下载被限 | `WorkManager` + `setExpedited`（配额内）；断点续传用 HTTP Range；失败退避 `BackoffCriteria(30s, EXPONENTIAL)`，`setRequiredNetworkType(NETWORK_TYPE_NON_CONSTRAINED_STATUS)` 走用户"允许流量下载"开关 |
| 省电模式 | `PowerManager.isPowerSaveMode` → 限制并发 chunk 数 = 1，ctx 降 1024 |
| 温度 | `HardwarePropertiesManager` 不适用普通 App → 用 `BatteryManager.EXTRA_TEMPERATURE` 采样，>42℃ 降级 |
| 12 位色深/大文件 | `RandomAccessFile` + mmap 只读映射，注意 32 位进程 1GB 上限（已用 arm64 规避） |

## 5. 划词 / 全局取词（重要合规警告）

- Tencent demo 那种"任意 App 后台取词"依赖 **AccessibilityService**。**Google Play 的辅助功能政策**只允许服务于残障辅助用途，"翻译取词"会被拒（政策明写禁止用于内容抓取类功能）。国内商店宽松，但 Play 版必须换方案：
  1. **选区上下文请求**（`ACTION_PROCESS_TEXT`）——任意支持"复制/搜索/翻译"的输入框与浏览器都能用，合规且够用；
  2. **分享**（`ACTION_SEND`）+ App Shortcuts；
  3. 悬浮球只做"截图 → 端侧 OCR（ML Kit text recognition）→ 翻译"，不读屏幕内容；
  4. 国内版可选开 Accessibility 模式，**并从 Play 包剔除该模块**（按渠道 build variant 分）。

## 6. UI 要点（Compose）

- 双向自动检测：`lang = auto` 时先本地脚本占比判定（`core` 已实现，别为 2 个语言引入 fasttext）。
- 逐 chunk 上屏（`Flow<String>`）+ "翻译中 · 已用 0.4s"计时；**永远显示"本地"**（这是卖点）。
- 编辑译文即触发 `Translator.accept()` → 若学到术语，底部 Snackbar「我把 X→Y 记住了，点这里管理」。
- 设置：模型档位/体积/校验和、术语表（导入 CSV，实时 `self_check()` 报错）、已学到 N 项、清空学习数据、导出学习数据（给 L4 训练用）。

## 7. 打包（APK / AAB）

| 目标 | 产物 | 命令 |
|---|---|---|
| Play | `.aab`（arm64 only，密度无关） | `./gradlew bundleRelease` |
| 官网侧载 | 单文件 universal APK | `./gradlew assembleRelease` + `zipalign -P 16` + `apksigner` |
| 体积自检 | `ls -l app-release.apk`；确认 `lib/arm64-v8a/libllama.so` ≈ 8–12MB，且**没有** 462MB 权重 | — |

签名/合规：`minify` 打开但 `-keep class dev.localmt.ml.** { *; }`（JNI 反射）；R8 别剥 `@Keep` 的数据类（manifest JSON）。
