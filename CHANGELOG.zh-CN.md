# v1.4.7 — 中英文双语界面与文档

- 新增英文和简体中文 UI，并支持自动识别系统语言。
- 设置中新增语言选项：跟随系统、English、简体中文。
- 语言偏好只保存在本地，不会启动 Codex turn，也不会修改任务数据。
- 完成任务状态、队列/历史空状态、弹窗、提示、设置、writer 诊断、metadata 同步信息和表单校验的双语化。
- 安装与卸载流程支持中英文输出。
- 新增 `README.zh-CN.md`、`SECURITY.zh-CN.md`、`CONTRIBUTING.zh-CN.md` 和本更新日志。
- 内部任务状态标识保持语言无关，并兼容 v1.4.6 的任务数据与设置。
- 不主动改变调度、额度等待、writer 交接、审批或耐久任务持久化行为。

## 历史版本

完整英文历史请参见 [CHANGELOG.md](CHANGELOG.md)。

### v1.4.6 — Durable Task Queue

- 修复旧 worker 保存旧 `tasks.json` 快照时可能覆盖新任务的问题。
- 创建、编辑和删除任务改为单锁事务。
- worker 只持久化当前任务的运行时字段。
- 新增单调 task-store revision、备份恢复和 append-only 任务事件日志。
- 保留安全 thread handoff、writer diagnostics、额度等待和自动续跑能力。

### v1.4.5 / v1.4.6 — Safe Thread Handoff

- 区分真实 active turn 与 retained writer ownership。
- 本地 writer diagnostics 不启动模型调用，也不会删除 Codex writer lock。
- 可选安全自动交接只会请求 ChatGPT Desktop 正常退出，不会强制结束进程。

### v1.4.3 — Metadata Sync Resilience

- 修复新建任务页面可能一直等待项目/模型 metadata 的问题。
- 会话、项目、模型 metadata 可以独立刷新，并在失败时继续使用缓存。

### v1.4.2 — Fresh Low-Color App Icon

- 使用更简洁、低色彩的 calendar + terminal + clock 图标。

### v1.4.1 / v1.4.0 — Minimal Realtime UI

- 重构为更简洁的任务优先界面，并加入本地实时状态、会话选择器、缓存优先 metadata 和键盘操作。

### v1.3.x / v1.2 / v1.1

- 增加 metadata 刷新、Git checkpoint、额度等待、精确定时、多任务队列和 quota-free 本地管理等基础能力。
