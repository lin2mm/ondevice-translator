# 本地 AI 翻译（中英互译）· iPhone 15 + Android
> 操作清单见 docs/08-你要做的操作清单.md（Mac 签名怎么设 / CI 怎么点 / PocketPal 怎么验收）。

一句话：**自带 462MB 的翻译专用小模型（腾讯 Hy-MT2-1.8B 1.25-bit，Apache-2.0）+ llama.cpp，权重不进 App 包（manifest 驱动下载 + sha256 校验），自学习走"翻译记忆 few-shot + 术语统计挖掘"（不训练），iOS 用系统 `Translation` 框架兜底（因为 iPhone 15 永远不支持 Apple Intelligence）。**

## 先读这个顺序

1. `docs/00-方案总览.md` —— 结论、每条约束对应的硬事实、体积/内存预算
2. `docs/01-选型与模型清单.md` —— 模型阶梯、许可证、官方 prompt 模板与采样参数
3. `docs/02-自学习设计.md` —— 四层自学习（L1 TM / L2 术语挖掘 / L3 画像 / L4 可选 LoRA）+ 可量化验收指标
4. `docs/03-iOS-实现.md` / `docs/04-Android-实现.md` —— 两端工程设置与必踩的坑
5. `docs/05-分发-下载-UX规格.md` —— 免费 CDN 账、manifest 契约、下载状态机、6 屏 UI 规格
6. `docs/06-风险与坑清单.md` —— 18 条会死人的事 + **本项目未验证清单**

## 怎么拿到安装包（详见 docs/07-安装包的三条路.md）

| 我要的 | 跑这一条 | 结果 |
|---|---|---|
| iPhone 15 上装原生 App | `./ios/install-on-iphone15.sh --model small`（**必须在你的 Mac 上**） | 自动下权重→实测 sha256→编译→用免费 Apple ID 签名→装机→启动；`--dry-run` 可在任何机器预演 |
| Android APK（本机不装工具） | 把仓库推到 GitHub → Actions 里 Run “Android APK” → Release 下 `.apk` | 模型已内置，装完即离线可用 |
| 完全不装东西先试 | `./web/deploy-github-pages.sh <用户名> <repo>` → Safari 添加到主屏幕 | PWA 外壳；只能跑小模型，**别用它判断项目可行性** |
| 本机自打 APK | `./android/build-apk.sh --model both --install` | 含 NDK 编译 llama.cpp + 16KB 页对齐 |

> 我不在这里产出 `.ipa`/`.apk`，原因写在 docs/07 第一节（Linux 沙箱 + 无签名 + 产物你也取不走）。

## 仓库里可跑的东西

```bash
python3 -m unittest tests.test_all          # 39 条：分句/占位符/体检重试/术语表/自学习挖掘/manifest 选档降级/缓存失效
python3 tools/lint_platform_code.py         # 平台代码硬约束自查（16KB 页、mmap、cacheDir、明文 http、manifest 字段两端同源）
python3 tools/build_manifest.py --dir ./weights --base-url https://cdn.x/models \
    --version 2026.09.05-a --out ios/LocalMT/Resources/manifest.json \
    --copy-to android/app/src/main/assets/manifest.json --prune-to 1
```

## 结构

```
core/    行为规格（Python 参考实现，两端移植必须与 tests/golden.jsonl 对拍）
  model_manifest.py  manifest schema / validate / 按机型选档与降级
  segmenter.py       分句 + token 预算打包 + 数字/URL/代码占位符保护与还原
  glossary.py        enforce / hint / keep / forbid 四类术语 + 自检 + Hy-MT2 术语块渲染
  learn.py           自学习：SQLite 翻译记忆、3-gram 相似召回、跨句统计挖术语
  prompt.py          Hy-MT2 官方 6 类模板（中英双版）+ 采样参数
  validate.py        输出体检（漏译/跑飞/重复/丢数字/丢占位符/脚本不对）+ 重试档位
  engine.py          编排：检测→分句→保护→注入→生成→体检→还原→缓存
ios/Sources/         ModelStore.swift（断点续传+校验+磁盘预算）、TranslationEngine.swift（llama.cpp + 内存告警卸载）
android/…/model/     ModelStore.kt + ModelDownloadWorker（WorkManager，Range 续传，指数退避）
android/…/engine/    TranslationEngine.kt（JNI + onTrimMemory）+ cpp/llama_jni.cpp
tools/               build_manifest.py / lint_platform_code.py
tests/               test_all.py / golden.jsonl（两端对拍）
```

## 已知状态（不粉饰）

- ✅ `core/` 已通过 39 条单测；所有模型体积、许可证、官方模板均从 Hugging Face API / 模型卡实测核实。
- ⚠️ **未验证**：手机上的 tokens/s、内存峰值、发热降速、1.25-bit 量化后的中英质量衰减、llama.cpp 当前版本对 `hunyuan_v1_dense` 架构的支持程度。沙箱只有 2 核 1GB、无 Xcode/NDK/CMake，跑不了模型也编不出 ipa/apk。
- **第一天该做的事**：装 HuggingFace 上 `AngelSlim/Hy-MT1.5-1.8B-1.25bit-GGUF` 里那个 7MB 官方 Demo APK，在你手上最好的 Android 机上跑几十句中英 —— 10 分钟就能判定"462MB 档的质量与速度"是否可接受，这决定项目要不要继续，比写代码优先。
