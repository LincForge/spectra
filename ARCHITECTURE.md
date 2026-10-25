# linc-spectra Architecture

## Overview

Spectra manages stateful mobile debug sessions across stateless MCP calls. It provides two complementary lanes: a DAP (Debug Adapter Protocol) lane wrapping `kotlin-debug-adapter` (Android JDWP) or `lldb-dap` (iOS Simulator) over subprocess stdio, and a zero-config `jdb` lane for Android method-entry and exception breakpoints that need no local source checkout. Sessions auto-reap after 600s of inactivity.

## Component Map

```text
src/linc_spectra/
├── cli.py                 # Click CLI (spectra doctor, spectra bellows, spectra logo)
├── mcp_server.py          # FastMCP (14 tools)
├── _logging.py            # stderr-only structlog configuration for stdio MCP safety
└── core/
    ├── dap_client.py      # DAP wire protocol (Content-Length framing, request/event correlation)
    ├── jdwp_bridge.py     # Android JDWP setup + kotlin-debug-adapter discovery
    ├── lldb_bridge.py     # iOS Simulator attach via lldb-dap
    ├── jdb_session.py     # Zero-config Android method & exception breakpoints via jdb
    ├── session_manager.py # Thread-safe DAP session registry with 600s inactivity reaper
    ├── adb_subprocess.py  # ADB helpers (PID lookup, JDWP port forward/remove, jdwp list)
    ├── ios_subprocess.py  # xcrun simctl wrapper for booted simulator PID lookup
    ├── ports.py           # Ephemeral localhost TCP port selection for adb forward
    ├── toolchain.py       # Toolchain discovery and macOS stub probes (lldb-dap, jdb, xcrun)
    ├── paths.py           # Path helpers ($HOME redaction for doctor and usage stats)
    ├── validation.py      # re.fullmatch validation for packages, bundle IDs, and JVM FQNs
    ├── observability.py   # Local JSONL MCP call logger ($LINC_HOME/mcp-calls.jsonl)
    └── usage_stats.py     # Read-only usage analytics aggregator over mcp-calls.jsonl
```

## Key Abstractions

| Abstraction | Role |
|-------------|------|
| `DAPClient` | DAP wire protocol over subprocess stdio (`dap_client.py`). Main thread sends requests; daemon reader thread dispatches responses and async `stopped` events via `seq` correlation and `threading.Event`. |
| `DebugSession` | Dataclass (`session_manager.py`) holding `session_id`, `platform`, `client`, `stopped_thread_id`, and `last_activity`. |
| `DebugSessionManager` | Thread-safe session registry (`session_manager.py`) with a daemon reaper thread (600s timeout) and JDWP port forwarding cleanup. |
| `JdbMethodSession` / `JdbExceptionSession` | Two-phase zero-config `jdb` sessions (`jdb_session.py`) that arm method or exception breakpoints by fully-qualified JVM name, auto-capture locals or exception frames on hit, and resume the app within a deadline to avoid Android input-dispatch ANRs. |

## Design Decisions

| Decision | Chosen | Rejected | Why |
|----------|--------|----------|-----|
| DAP transport | Subprocess stdio | TCP socket | Simpler adapter lifecycle, no extra listener port to manage |
| Source-free Android debugging | `jdb_session.py` (`jdb`) | Synthetic paths in `kotlin-debug-adapter` | `kotlin-debug-adapter` reports `verified: true` on non-local paths that never bind; `jdb` binds by JVM class/method name without sources |
| Input validation | `re.fullmatch` in `validation.py` | Shell quoting alone | `adb shell` re-parses arguments with device `sh -c`, and `jdb` reads line-oriented stdin |
| Session timeout | 600s auto-reap | Manual cleanup only | Releases JDWP forwards and subprocesses when an agent abandons a session |
