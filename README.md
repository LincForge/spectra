# linc-spectra

Attach a debugger to a running Android app (JDWP via `kotlin-debug-adapter`, or zero-config `jdb` by fully-qualified name with no sources) or iOS-simulator app (`lldb-dap`) and read what UI and logcat cannot show — locals at a method hit, exception frames — through <!-- SPECTRA:TOOL-COUNT -->14<!-- /SPECTRA:TOOL-COUNT --> MCP tools.

## Install

**Prerequisites:** Python 3.13+ (stock macOS `python3` is older; `uv tool install linc-spectra` fetches its own Python 3.13 automatically, whereas running `pip` or `pip3` on an older interpreter fails with `No matching distribution found for linc-cli-kit`).

Install with [uv](https://docs.astral.sh/uv/), `pipx`, or `pip` (Python >= 3.13):

- `uv tool install linc-spectra`
- `pipx install linc-spectra`
- `pip install linc-spectra`

`uv tool install` places `spectra` (and `linc-spectra`) in `~/.local/bin`; a shell without `~/.local/bin` on `PATH` gets exit code `127` (`command not found`) — run `uv tool update-shell` and open a new shell (or add `~/.local/bin` to `PATH`).

Then check your environment with `uvx linc-spectra doctor` (or `spectra doctor` once installed):

- **What it checks:** `adb`, `kotlin-debug-adapter`, `jdb`, `lldb-dap` (macOS only), `xcrun` (macOS only), and `python` (>= 3.13). On macOS, `doctor` probes the `/usr/bin/jdb` and `/usr/bin/xcrun` stubs and reports them as failed until a JDK or Xcode / Command Line Tools is installed.
- **Exit codes:** exits `0` when at least one debugger lane is usable (Android: `adb` plus `kotlin-debug-adapter` or `jdb`; iOS on macOS: `xcrun` plus `lldb-dap`), or exits `1` with a `hint:` line for every failed check. Pass `--json` (`spectra doctor --json`) for a single `{ok, checks}` JSON object on stdout.

## Adapters

- **Android — zero-config `jdb` lane:** requires `adb` (Android SDK `platform-tools`), a debuggable build (`android:debuggable="true"`), and `jdb` (ships with any JDK on `PATH`). Breaks on methods and exceptions by fully-qualified JVM name without local source files.
- **Android — full DAP lane:** requires `adb`, a debuggable build, local source files, and [`kotlin-debug-adapter`](https://github.com/fwcd/kotlin-debug-adapter). Unpack a release zip from [`fwcd/kotlin-debug-adapter`](https://github.com/fwcd/kotlin-debug-adapter/releases) so `adapter/bin/kotlin-debug-adapter` is on `PATH` or installed under `~/tools/kotlin-debug-adapter/` (`~/tools/kotlin-debug-adapter/adapter/bin/kotlin-debug-adapter`).
- **iOS — simulator lane (experimental):** requires macOS with Xcode's `lldb-dap` (checked on `PATH` first, then resolved via `xcrun -f lldb-dap`) and `xcrun`; `spectra doctor` reports the macOS `/usr/bin/jdb` and `/usr/bin/xcrun` stubs as failed until a JDK or Xcode is installed. Simulator only.

## Finding your device id

- **Android (`device` / `device_serial`):** run `adb devices` and use the value in the first (serial) column (for example, `emulator-5554`).
- **iOS Simulator (`device` / `device_serial`):** run `xcrun simctl list devices booted` to find the booted simulator UDID, or pass the literal `booted`.

## Quickstart

<!-- quickstart:start -->
Register the MCP server and run either the zero-config `jdb` flow (no local sources) or the full DAP flow:

```bash
claude mcp add linc-spectra -- spectra bellows
# Or run directly without a global install:
claude mcp add linc-spectra -- uvx linc-spectra bellows

# Zero-config flow (Android jdb — no local source checkout needed):
#   spectra_method_breakpoint(device="emulator-5554", package="com.example.app", method_fqn="com.example.app.AuthRepository.login")
#   -> trigger the UI in the app
#   -> spectra_method_locals(session_id="<session_id>")

# Full DAP flow (Android kotlin-debug-adapter or iOS lldb-dap):
#   spectra_attach(device="emulator-5554", package="com.example.app", platform="android")
#   -> spectra_breakpoint(session_id="<session_id>", source_path="/abs/path/AuthRepository.kt", lines="42")
#   -> spectra_continue(session_id="<session_id>")
#   -> spectra_stacktrace(session_id="<session_id>")
#   -> spectra_variables(session_id="<session_id>", frame_id=0)  # 0 = stopped thread's top frame, or an id from spectra_stacktrace
#   -> spectra_detach(session_id="<session_id>")
```
<!-- quickstart:end -->

## MCP Tools (14)

linc-spectra exposes <!-- SPECTRA:TOOL-COUNT -->14<!-- /SPECTRA:TOOL-COUNT --> MCP tools grouped by workflow.

### DAP Session

| Tool | Description |
|------|-------------|
| `spectra_attach` | Attach a DAP debugger to a running debuggable app (Android JDWP or iOS Simulator LLDB) |
| `spectra_detach` | Detach the debugger, remove port forwards, and clean up the session |
| `spectra_sessions` | List all active debug sessions |

### Stepping

| Tool | Description |
|------|-------------|
| `spectra_breakpoint` | Set `file:line` breakpoints on local source files |
| `spectra_continue` | Resume execution and wait for the next breakpoint or stop event |
| `spectra_step` | Step `next` (over), `in` (into), or `out` (out of the current frame) |

### Inspection

| Tool | Description |
|------|-------------|
| `spectra_stacktrace` | Get the call stack for the stopped thread |
| `spectra_variables` | Inspect local and scope variables at a stack frame |
| `spectra_evaluate` | Evaluate an expression in the current frame context (REPL at a breakpoint) |

### Zero-Config jdb

| Tool | Description |
|------|-------------|
| `spectra_method_breakpoint` | Arm a zero-config breakpoint on a method by fully-qualified name (`<class>.<method>`) with no local sources |
| `spectra_method_locals` | Harvest captured local variables from an armed `spectra_method_breakpoint` hit |
| `spectra_exception_breakpoint` | Arm a zero-config exception catchpoint by fully-qualified exception class name |
| `spectra_exception_info` | Harvest the throwing frame, thread, and locals from an armed `spectra_exception_breakpoint` |

### Analytics

| Tool | Description |
|------|-------------|
| `spectra_usage_stats` | Aggregate per-tool call counts, latency percentiles, and error rates from `$LINC_HOME/mcp-calls.jsonl` |

## Limitations

- **Upstream `kotlin-debug-adapter` exception breakpoints do not work:** use `spectra_exception_breakpoint` (the `jdb` catchpoint) instead.
- **`file:line` breakpoints report `verified: true` even without local sources:** `kotlin-debug-adapter` marks non-existent paths as verified even though they can never bind; `spectra_breakpoint` attaches a `warning` field when `source_path` is missing locally (use `spectra_method_breakpoint` when you do not have the source checkout).
- **One debugger per process:** a target process exposes a single JDWP connection at a time — detach any active DAP session before arming a `jdb` session on the same app (and vice versa).
- **iOS simulator only:** iOS debugging via `lldb-dap` supports booted iOS simulators on macOS only (experimental; physical iOS devices are not supported).
- **iOS `spectra_evaluate` takes expressions only:** iOS expressions run in `lldb-dap`'s expression-only (`watch`) context and LLDB commands (such as backtick-prefixed commands) are refused.
- **`jdb` renders values as text:** a debuggee string containing line breaks can add a fake `name = value` line to a `jdb` reply (`spectra` keeps the first value of a name within a reply).
- **Bounded `jdb` waits and stepping:** `spectra_method_locals` and `spectra_exception_info` wait at most `180 s` (`wait_s` `0`–`180`), and manual stepping takes `max_steps` `0`–`16`.
- **Python >= 3.13:** requires Python 3.13 or newer.

## Trust

- **No telemetry, no phone-home.** spectra runs locally over stdio MCP and opens no network listener; it talks only to your local `adb`, `jdb`, `kotlin-debug-adapter`, `xcrun`, and `lldb-dap` processes.
- **Local usage log under your control.** Every MCP tool call is recorded in `$LINC_HOME/mcp-calls.jsonl` (default `~/.linc/mcp-calls.jsonl`, rotated at 10 MB). Control logging with `SPECTRA_USAGE_LOG=off|redacted|full`:
  - `redacted` (default) — sensitive fields (`expression` and `source_path`) are replaced by length-only placeholders (`<redacted len=N>`, no digest), expression text is scrubbed from recorded error strings, and the log file is opened owner-only (`0600`) with `O_NOFOLLOW`.
  - `off` — nothing is written to disk.
  - `full` — raw parameter values are logged (long strings truncated at 500 characters, byte buffers summarized by length). Opt in only on a machine you trust.
- **`spectra_evaluate` runs code inside the debuggee.** By design, `spectra_evaluate` and the `jdb` tools execute inside the attached debuggable application. Identifiers that reach `adb shell`, `xcrun simctl`, or `jdb` stdin are validated with strict `re.fullmatch` patterns before execution. See [SECURITY.md](https://github.com/LincForge/spectra/blob/main/SECURITY.md) for the threat model and vulnerability reporting instructions.

## How this repo works

This repository is a **release mirror** of LINC's private repository — each release lands here as a single commit. Issues are welcome; pull requests are accepted by arrangement (open an issue first). See [CONTRIBUTING.md](https://github.com/LincForge/spectra/blob/main/CONTRIBUTING.md).

---

Built by [LINC Innovations](https://lincinnovations.com/audit?utm_source=github&utm_medium=readme&utm_campaign=spectra). Licensed under [Apache-2.0](https://github.com/LincForge/spectra/blob/main/LICENSE).
