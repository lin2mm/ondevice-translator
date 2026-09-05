# 下一个任务（执行者：Hermes，在 Mac 上）

## 前置：合并两个 Arena 分支（都有实证依据，见 STATUS.md 第二轮审查）
```bash
cd ~/Code/ondevice-translator
git fetch origin
git merge --no-ff origin/arena/fix-first-build-blockers   # 9 处编译修复 + manifest bundle:// + prompt 指令语言
git merge --no-ff origin/arena/fix-build-script-rerun     # build_llama_ios.sh 幂等修复
# handoff/LAST_BUILD.txt 可能有纯文档冲突：两边内容都保留（Arena 段落在文件尾部）
```

## 任务：从权重到真机离线翻译（固定 iphoneos，不用模拟器替代）
1. **下载 462MB 权重**（注意：先建目录，models/ 被 gitignore，新克隆里不存在）
   ```bash
   mkdir -p ios/Resources/models
   curl -fL --retry 3 -C - -o ios/Resources/models/Hy-MT2-1.8B-1.25Bit.gguf \
     https://huggingface.co/tencent/Hy-MT2-1.8B-1.25Bit-GGUF/resolve/main/Hy-MT2-1.8B-1.25Bit.gguf
   shasum -a 256 ios/Resources/models/Hy-MT2-1.8B-1.25Bit.gguf
   # 期望 cc497fe8f033b52b3b8b00a7669e9661435432f9d4cd43f7ed24400c01507a93（交接文档值）
   ```
2. **一键流程**（内部会生成 manifest——bundle:// 已放行，不会 exit 2——然后编译+装机）：
   ```bash
   ./ios/install-on-iphone15.sh --model small --team <你的TeamID> --device <UDID>
   ```
   或按交接提示逐步执行；manifest 这步现在带不带 `--skip-url-check` 都能过。
3. **验收（唯一标准）**
   - 飞行模式：中→英、英→中各 3 句成功
   - 引擎名显示 "Hy-MT2-1.8B · 本地推理"（显示"系统离线翻译" = 权重没进包）
   - Console.app 过滤 dev.localmt：`loaded backend=` 行含 METAL
4. **记录**：每步 commit/cmd/exit/first_error 追加 handoff/LAST_BUILD.txt，连同关键日志回传 Arena

## 已知边界（不要在这些点上浪费时间）
- 免费账号 7 天签名过期属预期；`--model big` 暂不做
- xcodebuild 若报新 Swift 错误：原样贴回，Arena 出补丁（不要 Mac 上手改后不回传）
- ModelStore 运行时下载路径的已知问题（阶段 2）本轮不修
