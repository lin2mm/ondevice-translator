# 下一个任务（执行者：Hermes，在 Mac 上）

## 前置
```bash
cd ~/Code/ondevice-translator
git fetch origin
git checkout arena/fix-first-build-blockers   # Arena 的补丁分支（9 处编译阻塞修复）
git log --oneline -3                          # 确认拿到本分支
```
不合并到 main，先在分支上验证；通过后再开 PR。

## 任务：首次真机构建四步（固定 iphoneos，不用模拟器替代）

1. **构建引擎框架**
   `./tools/build_llama_ios.sh --device-only`
   - 预期：克隆 llama.cpp v0.4.0（约 2-5 分钟）+ 编译 arm64 + 产出 `ios/Vendor/llama.xcframework`
   - 若报 lib 找不到：把 `build/llama-ios-arm64/out/` 的 `ls -l` 结果贴给 Arena（脚本用 `find -maxdepth 4` 捞 lib*.a，这是最可能的第一处脚本级失败）
   - 结尾必须看到 `OK: found N occurrence(s) of llama_model_load_from_file`

2. **下载 462MB 权重并生成 manifest**（可先跳过，用第 3 步只出壳验证编译，再回来）
   `./ios/install-on-iphone15.sh --model small --team <你的TeamID>`

3. **编译 + 装机**
   `./ios/install-on-iphone15.sh --model small --team <你的TeamID> --device <UDID>`
   - xcodegen generate → xcodebuild（Debug, generic/platform=iOS）→ devicectl install + launch
   - 免费个人账号：装机后 1 分钟内到 设置→通用→VPN与设备管理 信任开发者

4. **验收（唯一标准）**
   - 飞行模式下，中→英、英→中 各翻 3 句成功
   - 工具栏引擎名显示 "Hy-MT2-1.8B · 本地推理"（若是"系统离线翻译"= 权重没进包，回第 2 步）
   - Console.app 过滤 `dev.localmt`：`loaded backend=` 行里**必须出现 METAL**（没有 = 在跑 CPU，慢 3-5 倍）

## 每步完成后
按 handoff/STATUS.md 的记录格式把 commit/cmd/exit/first_error 追加到 `handoff/LAST_BUILD.txt`，连同关键日志贴回给 Arena。

## 已知边界（不要在这些点上浪费时间）
- 本分支只修"首个真机构建"链路的编译阻塞；ModelStore 运行时下载路径的已知问题见 STATUS.md"顺带记录"，阶段 2 再修
- 免费账号 7 天签名过期属预期；`--model big`（1133MB）暂不做
- 若 xcodebuild 仍报 Swift 错误：原样贴错误给 Arena 出补丁，不要在 Mac 上手改后不回传（Mac 是基线，但要以分支/PR 流转）
