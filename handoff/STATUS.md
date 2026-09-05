# 状态报告

## 第二轮审查（2026-09-06，Arena，基于 main@57f5e2f + PR 分支 df19cbb）

### 输入证据
- Hermes 侧 main 新增 a39c10b（build_llama_ios.sh 加 -DLLAMA_BUILD_APP=OFF + libtool 合并单库方案）与 57f5e2f（LAST_BUILD.txt：xcframework 37MB 构建成功，nm 验证 4 处符号）——CMake 层发现与 Arena 的头文件层发现互补，已互审通过
- `tools/verify/verify_llama_api.sh` 在沙箱重跑：两探针结论符合预期（v0.4.0 API 漂移修复成立）

### 本轮新发现并已修复（三处，两个分支交付）
1. **必炸**：`core/model_manifest.py validate()` 只放行 https:// → `install-on-iphone15.sh` 内置的 make_manifest（--base-url bundle://models）与交接提示第 3 步均 exit 2 中断（`--skip-url-check` 跳不过 validate）。已放行 bundle://（App 内分发场景无 CDN 语义）；`check_urls` 同时跳过非 http(s)（否则 urlopen 对未知 scheme 抛未捕获 ValueError）。**沙箱实证**：462000000 字节稀疏文件 + 真实文件名复刻 → exit 0，39 测试通过
2. **质量偏离**：Swift buildPrompt 恒英文指令 + "American English"（源头是 docs/08 PocketPal 手测示例，docs/03 并无 prompt 章节佐证"全称论"），违反模型卡/core/docs/01/docs/06 的"中→英必须中文指令+「英语」"。已按 core/prompt.py `_default` 逐字模板修复，Support.swift 误导注释与 docs/08 示例同步修正
3. **再运行炸弹**（Hermes a39c10b 引入）：构建成功后重跑 `build_llama_ios.sh --device-only`（LAST_BUILD.txt 明文记载的再生成路径）→ find 会把 out/libllama-all.a 拷到自身（cp same-file，set -e 中断）；device-only 路径 libtool 输出还落在输入 glob 目录内。已修（find 排除产物目录、合并产物写 merged/ 子目录），cp 行为新旧对比已仿真实证

### 分支交付（均为 arena/* + PR，未动 main）
- `arena/fix-first-build-blockers @ df19cbb`（原 9 处编译修复之上追加）：948d4e1 manifest bundle://、df19cbb prompt 指令语言
- `arena/fix-build-script-rerun @ 067a752`（基于 main@57f5e2f，仅动 build_llama_ios.sh）：幂等修复
- 已知合并摩擦：PR 分支与 main 在 handoff/LAST_BUILD.txt 双边有改动（纯文档，冲突可手解）

### Hermes 下一步注意（已写入 NEXT.md）
- 下载权重前先 `mkdir -p ios/Resources/models`（目录被 .gitignore，新克隆不存在；交接提示里的裸 curl 不会建目录）
- 两个 arena 分支都合并后再跑装机流程

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

## Arena 会话引导（2026-09-06 实测：跨会话快照会丢失**整个 .git 目录**、**~/.ssh/config**，
## 并把私钥权限 600 改成 644；私钥与 known_hosts 文件本身保留）
恢复步骤（每次新会话先跑）：
```bash
chmod 600 ~/.ssh/id_ed25519
cat > ~/.ssh/config <<'EOF'
Host github.com-ondevice
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
git clone -b arena/fix-first-build-blockers \
  git@github.com-ondevice:lin2mm/ondevice-translator.git ~/ondevice-translator
cd ~/ondevice-translator
git config user.name "Arena (agent)"; git config user.email "arena@users.noreply.github.com"
```
若私钥也丢失：ssh-keygen 重新生成 → 维护者在 GitHub Deploy Keys 换公钥 → 再执行上述步骤。
**原则：GitHub 远程是唯一事实源，workspace 是一次性工作台——任何未 push 的改动都会丢。**

## 本地验证工具
- `tools/verify/verify_llama_api.sh`：复现 llama.cpp API 编译级核对（升级 llama 版本后先跑它）
