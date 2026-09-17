# Contributing

Thanks for helping improve Codex Scheduler.

## Development

- macOS is the primary supported platform.
- Python 3.10+ is required.
- The project currently uses only the Python standard library at runtime.
- Keep scheduler management local and avoid starting model turns for metadata-only or queue-management actions.
- Do not weaken approval safety, workspace/thread serialization, or task persistence guarantees.

## Tests

Run before opening a pull request:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q server install.py tests
```

## Public-repository hygiene

Do not commit local task history, cached conversation metadata, absolute user-specific paths, tokens, credentials, `.env` files, or generated application-support state.
