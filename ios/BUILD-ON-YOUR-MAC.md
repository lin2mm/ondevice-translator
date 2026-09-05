# 在你的 iPhone 15 上跑起来：两条路

> 前提说清：**这个工作区（Linux 沙箱，无 Xcode / 无 iOS SDK / 无你的签名证书 / 无连线设备）不可能产出 `.ipa`。**
> iOS 的产物必须由 macOS 上的 Xcode 编译并用你的 Apple 身份签名 —— 这是苹果的硬规定，不是工具链问题。
> 所以这里给你两条路：A 今天就能在你手机上测模型（不写一行代码）；B 用我给的工程做出可安装的 App。

---

## 我在本次直接读到的权重事实（不是引用二手文章）

用 HTTP Range 拉了 HF 上那两个 GGUF 的文件头，解析 GGUF v3 KV 元数据得到：

| 键 | 值 | 对我们意味着什么 |
|---|---|---|
| `general.architecture` | **`hunyuan-dense`** | 你的 llama.cpp **必须有这个架构**才能加载。老版本（< 2025 年中）会直接报 `unknown architecture` |
| `hunyuan-dense.block_count` / `embedding_length` / `head_count` | 32 / 2048 / 16 | 1.8B 结构确认；KV = 2·32·2048·B ≈ **128KB/token → 2048 ctx ≈ 262MB KV**，与 docs/00 的预算吻合 |
| `hunyuan-dense.context_length` | **262144** | ⚠️ 危险默认值！任何"按模型最大 ctx 分配"的 App 会瞬间要 34GB KV → **必须在 App 里显式把 n_ctx 钉在 2048/1024** |
| `general.sampling.temp/top_k/top_p` | 0.7 / 20 / 0.8 | 采样默认值已内嵌在权重里；读得到的引擎会自动用对（与模型卡一致） |
| `tokenizer.ggml.model` / `.pre` | `gpt2`(BPE) / `hunyuan-dense` | 快速分词器；`pre` 需要新版 llama.cpp，否则**预分词不对 → 质量悄悄变差且不报错** |
| `tokenizer.chat_template` | **不存在** | ⚠️ 重要：这两个 GGUF **没有内嵌 chat 模板**。引擎会退回内置模板（多半是 ChatML），所以指令必须全部塞在 user 消息里（`core/prompt.py` 已经这么做），并且要**自己在 native 侧提供模板字符串**才能保证与官方 `apply_chat_template` 一致 |
| 体积 | 1.25Bit = 462MB；Q4_K_M = 1133MB | 与 manifest 默认值一致（我已写进 `tools/build_manifest.py` 的 DEFAULTS） |

> `docs/06` 的 D 节把"llama.cpp 是否支持 hunyuan-dense"列为未验证风险 —— **现在可以用下面 5 分钟的方式自己验掉**，而且验证工具就是现成的 App。

---

## A. 今天，0 代码：用 PocketPal 在你 iPhone 15 上直接跑这个模型

PocketPal AI 是免费开源的 iOS 端 llama.cpp 图形壳（App Store 有），支持"HF 搜索 GGUF / 添加自定义模型 URL / 设 ctx"，正好当**模型可运行性的试纸**。

步骤：
1. App Store 装 `PocketPal AI`（免费）。
2. `☰ → Models → +（Add Custom Model / Hugging Face）`，填直链：
   ```
   https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf      # 1133MB，先试这个
   https://huggingface.co/tencent/Hy-MT2-1.8B-1.25Bit-GGUF/resolve/main/Hy-MT2-1.8B-1.25Bit.gguf  # 462MB
   ```
3. **Context size 手动填 2048**（不要留空/不要 8192）。
4. 系统提示（System Prompt）填我们的模板，把指令塞进 user 侧也可以：
   ```
   将以下文本翻译为 英语，注意只需要输出翻译后的结果，不要额外解释：
   ```
   反方向填 `Translate the following text into Chinese. Note that you should only output the translated result without any additional explanation:`
5. 采样参数：`temperature 0.7, top_k 20, top_p 0.8, repeat_penalty 1.05`。
6. 测这 6 句（覆盖我们的红线用例）：
   | 输入 | 你要看什么 |
   |---|---|
   | `总计 1,280 美元，含税。` | 数字一位都不能错、"美元"要变 `$`/`USD` |
   | `请把 {{user_name}} 的订单发到 support@x.com` | 占位符与邮箱必须原样出现 |
   | `这个方案不太行吧…` | 语气词/省略号别被吞 |
   | `服务器 504 了，先回滚再查日志` | 运维黑话别硬译 |
   | 一段 300 字中文（粘技术文档） | 是否截断、是否复读、耗时 |
   | 一句短英 `ok thanks` | 小模型最容易在这里"原样吐回/加解释" |
7. **记录 PocketPal 显示的 tokens/s、峰值内存、机身是否烫**，以及加载是否成功。

判定：
- 加载失败 `unknown architecture` → 你的 PocketPal 内 llama.cpp 太旧：换更新版本，或先用 MNN/MLC 路线（docs/04）；这也是决定"要不要自己编 xcframework"的分水岭。
- 能加载但 1.25bit 失败而 Q4 成功 → 说明**极限量化类型不在上游**，主力档必须改成 Q4_K_M（1133MB），"最轻量"目标随之上移 → **这会改变整个方案的体积假设，务必回来改 docs/00 的表格**。
- Q4 在 iPhone 15（6GB）上被杀 → 用 462MB 档 + ctx 1024，接受质量小幅回退。
- 质量 OK 且 tok/s ≥ 12 → 整个项目值得做；<5 tok/s → 中英日常聊天场景体验不合格，需要先解决加速（Metal 全 offload / SME2 / 换 2bit）。

---

## B. 真正的安装包：用本仓库的 `ios/` 工程（阶段 1 已可编译）

阶段 1 刻意**只依赖系统 `Translation` 框架**：零第三方包、零权重下载、iPhone 15 纯离线可用 —— 目的是先把"签名 → 装机 → UI → 延迟测量"这条链路跑通，再插 llama 引擎（`ios/Sources/EngineFactory.swift` 里只有一个 TODO 钩子）。

### 你需要什么
- 一台 macOS（自己/借的/Mac mini 时租均可）+ Xcode 16+；
- 一个 Apple ID（**免费个人账号即可装真机**，限制：7 天后需重签、同时最多 3 个自签 App）；
- 一根数据线；iPhone 15 已信任这台电脑。

### 步骤
```bash
brew install xcodegen
cd ondevice-translator/ios
xcodegen generate                       # 生成 LocalMT.xcodeproj
open LocalMT.xcodeproj
```
Xcode 里：
1. 选中 `LocalMT` target → **Signing & Capabilities** → Team 选你的 Personal Team，Bundle ID 改成你自己的域（`dev.localmt.app` 会被别人占用/校验失败），点 **Try An App ID** 确认。
2. 设备选你的 iPhone 15 → **⌘R**。首次会在 iPhone 上装并启动。
3. iPhone 上：设置 → 通用 → VPN 与设备管理 → 信任你的开发者证书（**必须在 1 分钟内做**，否则启动即闪退）。
4. 想要 `.ipa`（发给别人 / 用 AltStore 装）：
```bash
xcodebuild -project LocalMT.xcodeproj -scheme LocalMT -configuration Release \
  -destination 'generic/platform=iOS' -derivedDataPath ./dd archive \
  -archivePath ./dd/LocalMT.xcarchive CODE_SIGNING_ALLOWED=NO
xcodebuild -exportArchive -archivePath ./dd/LocalMT.xcarchive -exportOptionsPlist ExportOptions.plist -outputPath ./ipa
```
> 无签名 archive 只能验"能不能编译打包"。**能装到别人手机上的 `.ipa` 一定要签名**：要么付费开发者账号（99 USD/年）走 TestFlight/App Store，要么 AltServer/AltStore 用你自己的免费账号签（7 天）。没有第三条合规路子。

### 阶段 1 → 阶段 2（插本地模型）
1. 取消 `project.yml` 里 `dependencies.package: llama.swift` 的注释（该 SPM 包直接提供 llama.cpp 的 XCFramework，免手编）。
2. `Sources/TranslationEngine.swift`（已写好，含 `use_mmap=true`、内存告警自动卸载、ctx 降级）+ `ios/Resources/manifest.json` 换成 `tools/build_manifest.py` 生成的真实清单。
3. **务必把 `n_ctx` 写死 2048**，别信权重里的 262144（上面实测的坑）。
4. `Tokenizer.chat_template` 不存在 → 在 native 侧显式传模板字符串（照 Hy-MT2 官方 `apply_chat_template` 语义），并跑 `tests/golden.jsonl` 对拍。
5. 想要 `Increased Memory Limit`（6GB 机器加载 1133MB 档的保险）：**这项 entitlement 需要付费开发者账号**，个人免费账号加不上 —— 免费账号阶段请只用 462MB 档 + ctx 1024。

### 装好后在手机上做 4 个验收动作
1. **飞行模式**打开 App → 完整翻译一遍（这是"离线"的唯一硬证据）。
2. 连 Mac 跑 Instruments 或看 Settings→Developer→Memory，记峰值 RSS（目标 ≤1.1GB）。
3. 连续 20 句，画耗时曲线：若第 15 句起明显变慢 → 热降频，需在 UI 加节流/降档（docs/03 §5）。
4. 把 `App 体积` 和 `首次可用耗时` 记下来：阶段 1 应 ≈ 4MB / <1s；阶段 2 = 4MB + 462MB 下载 / 下载后 <3s 首字。


---

## 补（2026-09-05）：编译前必须先有 llama.xcframework

`ios/project.yml` 现在依赖 `Vendor/llama.xcframework`（桥接层 `LlamaBridge.mm` 直接 include `llama.h`/`ggml-backend.h`），
所以手工路线的正确顺序是：

```bash
./tools/build_llama_ios.sh          # 首次 ~6 分钟，产出 ios/Vendor/llama.xcframework
cd ios && xcodegen generate && open LocalMT.xcodeproj   # 然后 Cmd+R
```

或者一条命令全包（含下权重 + 装机 + 启动）：`./ios/install-on-iphone15.sh --model small`。
缺这一步的报错长这样，别去查 Xcode 设置：`llama.h file not found` / `framework 'llama' not found`。

GUI 路线的逐项 Build Settings、免费签名规则、报错对号入座表：**docs/08 第 6 节**。
