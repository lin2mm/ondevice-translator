# 飞行模式无译文 · 定位手册（FLIGHT_DEBUG）

> 现状（ARENA_PROMPT.md @640b59a）：编译 ✅ / 装机 ✅ / 启动 ✅ / 飞行模式无译文 ❌。
> 原则：先分离"模型与 llama.cpp 的兼容性"和"iOS 特有行为"，再把设备当黑盒读观测点。
> 全程不需要新功能代码；每步产出直接可回传 Arena 的证据。

## 第 0 步（Mac，5 分钟，无真机）：GGUF 元数据体检

```bash
python3 tools/verify/inspect_gguf.py ios/Resources/models/Hy-MT2-1.8B-1.25Bit.gguf
```

判读：
- `general.architecture` 应为 `hunyuan-dense`（v0.4.0 支持 ✓）
- **张量量化分布若出现 `UNKNOWN(id)`** → stock llama.cpp v0.4.0 拒载，根因即此（1.25Bit
  极端量化可能依赖厂商 fork 的自定义 kernel）。这不是 iOS 问题，改方向是：换 Q4_K_M
  独立 App（既定规划）或引入厂商 patch 重编 xcframework——把结果带回，Arena 出方案
- `tokenizer.chat_template 存在: False` 属预期（模板宿主自带，LlamaBridge.mm）

## 第 1 步（Mac，10 分钟）：同版本 llama.cpp 桌面冒烟

用**与 xcframework 完全相同的 v0.4.0 源码**在本机 CPU 跑一次真实推理，端到端验证
模型文件 + 量化 + 分词器：

```bash
cd vendor/llama.cpp          # build_llama_ios.sh 已浅克隆的 v0.4.0
cmake -B build-mac-smoke -DLLAMA_BUILD_TOOLS=ON -DGGML_METAL=OFF
cmake --build build-mac-smoke --target llama-cli -j
echo 'Translate the following text into Chinese. Note that you should only output the translated result without any additional explanation:

The method described here has run stably in three production environments.' \
| ./build-mac-smoke/bin/llama-cli -m ../../ios/Resources/models/Hy-MT2-1.8B-1.25Bit.gguf \
    -p "$(cat)" -n 64 --no-cnv
```

- 能出合理译文 → 模型/量化/分词全链路 OK，问题锁定 iOS 侧（进第 2 步）
- 加载失败/乱码/复读 → 根因在模型与 llama.cpp，真机调试可免（回传完整 stderr）

## 第 2 步（真机）：三个观测点（不猜，只读）

打开 App 前先开飞行模式。依次记录：

| # | 观测点 | 位置 | 判读 |
|---|---|---|---|
| 1 | **工具栏引擎名** | 底部 status 栏 | `Hy-MT2-1.8B · 本地推理（…MB）` = 权重在包内、A 档已选；`系统离线翻译 · 未内置权重` = gguf 没进 bundle → 重跑 `install-on-iphone15.sh --model small` 并确认 xcodegen 之后 Resources 阶段含 gguf |
| 2 | **红字错误文本** | 翻译按钮下方 | `权重不在 App 包内`（LTLlama code 1）→ 同上；`加载失败：…量化类型`（code 2）→ 第 0 步的 UNKNOWN 场景；`ctx 分配失败`（code 3）→ 降 n_ctx；`本地模型输出未通过校验（疑似复读）` → 模板/采样问题，回传 Console |
| 3 | **Console 日志** | Mac Console.app，过滤 subsystem `dev.localmt` | `loaded backend=` 行：必须含 `METAL`；没有 METAL = 在跑 CPU（慢 3-5 倍但仍该有输出）；完全无此行 = 加载未到达，看 #2 的错误 |

另：设置→通用→iPhone 储存空间 里该 App 体积应 ~500MB（452MB 模型+App 本体），
一眼判定权重是否真的打进了包。

## 第 3 步：按症状走决策树

```
无译文
├─ 引擎名 = 系统离线翻译 ──────────→ 权重未进包（观测点1）→ 重打包
├─ 红字 = 权重不在包内(code 1) ───→ 同上
├─ 红字 = 加载失败(code 2)
│   ├─ 第0步 UNKNOWN 量化 ────────→ 模型不兼容 stock v0.4.0 → Arena 出方案
│   └─ 第0步 全已知量化 ──────────→ 回传 Console 全文（怀疑 mmap/路径/签名strip）
├─ 红字 = 复读校验失败 ───────────→ 模板/全角竖线问题 → 回传 10 条原始输出
├─ 转圈不停（无红字）──────────────→ 在生成但极慢 → Console 看 tokens/s；METAL?
└─ 闪退 ──────────────────────────→ Xcode 重跑看崩溃栈；疑 jetsamen（内存）
```

## 回传给 Arena 的最小材料
1. 第 0 步的完整输出（量化分布一行是关键）
2. 第 1 步桌面冒烟：出译文 or 完整 stderr
3. 真机三观测点的原文（引擎名/红字/`loaded backend=` 行）
4. 复现输入的原文与方向（中→英 / 英→中）
