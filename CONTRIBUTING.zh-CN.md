# 贡献指南

[English](CONTRIBUTING.md) | **简体中文**

感谢你帮助改进 Codex Scheduler。

## 开发约定

- 主要支持平台是 macOS。
- 需要 Python 3.10+。
- 运行时目前只使用 Python 标准库。
- Scheduler 管理操作应保持本地化；metadata 或队列管理操作不应启动模型 turn。
- 不要削弱审批安全、工作区/thread 串行化、任务持久化保证或双语 UI 的 fallback 行为。
- 新增用户可见 UI 文案时，请同时添加英文和简体中文；内部状态值保持语言无关。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q server install.py tests
```

## Public 仓库卫生

不要提交本地任务历史、会话 metadata 缓存、用户绝对路径、token、credential、`.env` 或生成的 Application Support 状态。
