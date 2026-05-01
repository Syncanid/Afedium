# AGENTS.md — AFEDIUM

## How to run

```bash
python3 core.py
```

Python 3.9+ required. The entrypoint auto-installs `websockets`, `requests`, `pyglet` via pip on first boot (gate-kept by `config/core.json` → `allow_global_install`).

## Architecture

Everything is a plugin (`AfediumPluginBase`, defined in `lib/plugin.py`). There are three plugin sources:
- **System modules** — `system/*.py`, loaded on startup
- **PYZ modules** — `pyz_modules/*.pyz`, auto-loaded after system modules
- **Dev plugins** — `dev_plugins/*/`, watched by `dev_tools.py`; re-packaged into `.pyz` on change (hot-reload when `config/dev_tools.json` → `hot_reload: true`)

Plugin lifecycle: `setup()` → `main_loop()` → `teardown()`. All plugins must use `self.stop_event.wait(timeout)` instead of `time.sleep()` for cooperative instant shutdown.

## Key directories

| Path | Purpose |
|------|---------|
| `config/*.json` | All runtime configuration (editable, auto-reloaded where supported) |
| `lib/` | Framework core (plugin base, commands, events, config, logging) |
| `system/` | Built-in system modules (WS server, module manager, display driver, dev tools) |
| `dev_plugins/` | Source-form dev plugins (pack to `.pyz` via `pack` command) |
| `pyz_modules/` | Installed PYZ plugins (zip archives with `info.json` + `main.py` + `src/`) |
| `logs/` | `afedium.log` (midnight rotation, 7-day retention) + `afedium_debug.log` |

## Commands in the terminal REPL (`$:` prompt)

Frequently useful:
- `help` — list all registered commands
- `get <path>` / `set <path> <value>` — read/write system state (white-listed to `static`, `dynamic`, `loaded_plugins`)
- `quit` — graceful shutdown; `quit <module>` — stop one module
- `reload` — hot restart the process; `upgrade` — git pull + restart
- `dev list` / `dev reload <id>` / `pack [module]` — dev plugin management

## GPU rendering gotcha

`display_driver.py` uses a **dual-process** architecture (main process → Pyglet/OpenGL 3.3 child via `multiprocessing.Pipe`) to bypass the Python GIL. If display breaks, try `display restart` or `display clear` in the terminal REPL.

## WebSocket protocol

Server on port `11840` (TCP, WebSocket) and `12840` (UDP broadcast for service discovery). Control codes `0x01`–`0x1B` are handled by `external_handler.py`. Full protocol docs in `Afedium_ws.md`.

## Config quirks

- `config/core.json` → `debugging: true` enables `DEBUG`-level console output + a separate debug log file
- `config/core.json` → `Disabled` list deactivates specific modules (use module ID, not filename)
- `config/ws_server.json` → `auth_type` supports `"none"`, `"password"`, or `"captcha"`

## No testing/linting infra

This repo has no CI pipelines, test frameworks, linters, formatters, or pre-commit hooks. There is no `pyproject.toml` or `setup.py`.

## Style conventions

- All module files must expose `Info: dict` (id, version, author, pip_dependencies, linux_dependencies) and class `AFEDIUMPlugin`
- Plugins communicate via `EventHandler` (event-driven) and `comm_lib` (command tree); never write directly to stdout — use `ctx.reply()`
- Config files are JSON; the `Config` class auto-merges missing keys from a `default` dict
- Plugin resources are loaded from `src/` (relative to plugin root) or from `external_resource_dir` if configured
