# TapeWebUI

Web application for scheduled backups to an IBM ULTRIUM-TD3 (LTO-3) FC tape
drive on Windows, built entirely on top of the IBM ITDT-SE CLI.

## Status

Vertical slice (milestone 1): project skeleton, ITDT transport, drive status
in the UI with live SSE updates. See `.agents/SPEC.md` for the full plan.

## Running

```bat
py -V:3.11 -m venv .venv
.venv\Scripts\pip install -e .[dev]
cd frontend && npm install && npm run build && cd ..
.venv\Scripts\python.exe run.py
```

The app listens on `http://127.0.0.1:8091` (8090 is taken by OledWebUI).

## Configuration

Settings come from `TAPE_*` environment variables (or `.env`):
`TAPE_HOST`, `TAPE_PORT`, `TAPE_ITDT_EXE`, `TAPE_DEVICE_NAME`,
`TAPE_FAKE_TRANSPORT` (offline mode without real ITDT), etc.
See `src/tapevaultui/config.py` for the full list.

## Development

```bat
.venv\Scripts\python.exe -m pytest          # tests (hardware-free, FakeItDtTransport)
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe scripts\bump-version.py
```
