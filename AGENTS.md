# AGENTS.md

## 项目目标
开发免费、端侧、可离线运行的中英双向翻译 App。首建目标：462MB Hy-MT2 轻量模型在 iPhone 15 真机离线翻译。

## 范围
1. iOS 版：A 档（本地 LLM）+ C 档（系统 Translation 框架兜底）
2. Android 版：后续阶段，当前无成功建证据
3. 术语表、纠错记忆为正式功能；纠错记忆不等同于模型权重更新

## 安全边界
- 绝不提交模型权重（.gguf）、私钥 (.p12/.p8/.mobileprovision)、令牌、凭证
- 持久化模型在 Application Support/Models/
- 免费签名 7 天有效，需手动重新签名安装

## 工作分支
- 所有补丁通过独立分支 + PR 交接
- 同一时刻不要让两个 Agent 同时修改同一分支

## 依赖版本
- llama.cpp v0.4.0
- GGML_METAL=ON、GGML_METAL_EMBED_LIBRARY=ON
- Xcode 26.6+，iOS 18.0+ 部署目标