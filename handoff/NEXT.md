# 下一个任务

## 任务描述
首次构建并验证：让 462MB Hy-MT2 轻量模型在 iPhone 15 真机上实际离线翻译。

## 成功条件
1. `tools/build_llama_ios.sh` 成功生成 `ios/Vendor/llama.xcframework`
2. `xcodegen generate` 生成 `LocalMT.xcodeproj`
3. Xcode 编译出 `LocalMT.app`（arm64 真机）
4. `./ios/install-on-iphone15.sh --model small` 成功装机
5. 飞行模式下中→英、英→中均能翻译，输出正确

## 阻止因素
- 需要 `gh` Token 才能创建远程仓库
- Apple ID 需在 Xcode 中配置（免费个人账号）
- iPhone 15 需信任此电脑并在"设置→通用→VPN与设备管理"信任开发者

## 下一步
1. 您在 GitHub 生成 Personal Access Token：Settings → Developer settings → PAT → Generate new token (classic) → 勾选 `repo` 权限
2. 运行 `gh auth login --with-token` 完成身份验证
3. 回复仓库地址，我创建远程仓库并推送

> 提示：如果已有私有仓库，直接提供仓库 URL 即可