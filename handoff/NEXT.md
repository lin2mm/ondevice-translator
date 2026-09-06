# 下一个任务（执行者：Hermes，在 Mac 上）

## 前置：合并 Arena 第三轮分支
```bash
cd ~/Code/ondevice-translator
git fetch origin
git merge --no-ff origin/arena/round-3-flight-debug
# 含：prompt 指令语言修复（中→英中文指令）+ 构建脚本幂等 + check_urls 防护
#     + inspect_gguf.py + FLIGHT_DEBUG.md + UI 文案/AGENTS.md 边界句 + ARENA_REPLY.md
```

## 任务：定位"飞行模式无译文"（按 handoff/FLIGHT_DEBUG.md 顺序执行）
1. **第 0 步（5 分钟，无需真机）**：`python3 tools/verify/inspect_gguf.py ios/Resources/models/Hy-MT2-1.8B-1.25Bit.gguf`
   → 看张量量化分布有没有 `UNKNOWN(id)`
2. **第 1 步（10 分钟）**：vendor/llama.cpp 同版本（v0.4.0）Mac 桌面冒烟（命令在 FLIGHT_DEBUG.md）
3. **第 2 步（真机）**：三观测点——工具栏引擎名 / 红字错误原文 / Console 过滤 `dev.localmt` 的 `loaded backend=` 行
4. **第 3 步**：按决策树对号，把四样材料回传（FLIGHT_DEBUG.md 尾部清单）

## 验收标准（不变，边界已确认：仅离线文字翻译）
- 飞行模式：中→英、英→中各 3 句成功
- 引擎名 "Hy-MT2-1.8B · 本地推理"
- Console `loaded backend=` 行含 METAL

## 记录
每步 commit/cmd/exit/first_error 追加 handoff/LAST_BUILD.txt；第 0/1 步的输出全文回传。
