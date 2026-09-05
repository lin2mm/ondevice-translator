# 状态报告

## 已验证 ✓
- 项目路径：`~/Code/ondevice-translator/`
- Git 初始化完成，无远程仓库
- `ios/Vendor/llama.xcframework` 未跟踪（已排除）
- `ios/Resources/models/*.gguf` 未放置（462MB 权重待后处理）
- `ios/project.yml` 已配置，直接 `xcodegen generate` 可出工程
- `tools/build_llama_ios.sh` 脚本完整，可构建 xcframework
- `ios/install-on-iphone15.sh` 脚本完整，支持 `--model small`

## 待验证 ~
- [ ] llama.xcframework 产出（需运行 `tools/build_llama_ios.sh`）
- [ ] App 编译成功（需 Xcode 真机构建）
- [ ] 462MB 权重下载并校验 SHA256
- [ ] manifest.json 生成（需 `tools/build_manifest.py`）
- [ ] 手机装机并信任开发者
- [ ] 实际中文→英文/英文→中文离线翻译

## 依赖缺失
- `gh` CLI 虽已安装，但无有效身份验证 Token（需您在 GitHub 官网生成）

## 记录方式
每次构建完成后，在 `handoff/LAST_BUILD.txt` 追加：
```
commit: <HEAD>
time: <ISO8601>
cmd: <执行的主要命令>
exit: <0|非0>
first_error: <若有>
```