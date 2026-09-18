# 安全策略

[English](SECURITY.md) | **简体中文**

## 支持版本

安全修复会应用到 Codex Scheduler 的最新版本。

## 安全模型

Codex Scheduler 是本地 macOS 工具。本地 UI server 只绑定到 `127.0.0.1`，本地 API 使用每次启动生成的内存 token，Scheduler 状态默认保存在 `~/Library/Application Support/CodexNativeScheduler`。

定时 Codex 任务会按照用户选择的权限运行。Scheduler 不是 sandbox，也不会获得超出本地 Codex/ChatGPT 环境已有权限之外的能力。敏感的交互式审批请求不会自动批准。可选的 ChatGPT writer 自动交接默认关闭，并且只有用户对任务明确开启后才可能请求应用正常退出。

## 报告漏洞

如果仓库启用了 GitHub Private Vulnerability Reporting，请优先使用它。如果无法使用，请只创建一个最小公开 issue 请求私下联系渠道，不要在公开 issue 中提交密钥、利用细节、个人数据或可直接运行的 PoC。

## 密钥与敏感数据

不要提交 API key、access token、private key、`.env`、Scheduler 任务数据或本地 Application Support 数据。每次 push 前请检查 `git diff --cached`。
