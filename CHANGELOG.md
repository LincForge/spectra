# Changelog

All notable changes to linc-spectra will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.1.0] - 2026-10-24

First public release.

### Added
- `spectra doctor [--json]` checks adb, kotlin-debug-adapter, jdb, lldb-dap, xcrun and Python, prints a `hint:` line for every failed check, and exits 0 only when a debugger lane is usable (Android: adb plus kotlin-debug-adapter or jdb; iOS on macOS: xcrun plus lldb-dap), else 1. `--json` prints a single `{"ok": ..., "checks": [...]}` object to stdout. Tool paths under the home directory are shown as `~/...` so the report can be shared without the user name.
- A `linc-spectra` console script next to `spectra`, so `uvx linc-spectra doctor` works.
- Public CI: a test workflow on Ubuntu and macOS with Python 3.13 that runs the device-free suite (`-m "not device"`), a tag-triggered release workflow using PyPI trusted publishing (no API token), and Dependabot with version-update PRs disabled.
- `spectra_method_breakpoint` auto-captures local variables across steps when the breakpoint hits and resumes the app within a hard deadline (default on), preventing Android ANRs while caching the result for `spectra_method_locals` (#62).
- `device=` parameter alias for `device_serial` on `spectra_attach`, `spectra_method_breakpoint`, and `spectra_exception_breakpoint` (#64).
- Strict MCP argument validation via `strict_tools(server)`: unknown tool arguments return a validation error and all tool schemas advertise `additionalProperties: false` (#60).
- Licence: Apache-2.0.

### Changed
- Requires `linc-cli-kit>=0.1.4`, the first release with `strict_tools`, which the MCP server uses to reject unknown tool arguments. Older versions failed at import.
- `spectra fuelcheck` is now a hidden alias of `spectra doctor` with the same output and exit code; it exits 1 when no debugger lane is usable.
- `spectra --version` and the banner read the version from the installed package metadata (`0.0.0` when not installed) instead of a hard-coded string.
- Integration e2e tests pick their Android device from `SPECTRA_TEST_SERIAL`, then `ANDROID_SERIAL`; with neither set they skip ("set SPECTRA_TEST_SERIAL or ANDROID_SERIAL") before running `adb devices` rather than guess, so a shared host never runs them against a personal device that happens to be listed first. Single-device hosts can opt in to the first `adb devices` row in `device` state with `SPECTRA_TEST_ANY_DEVICE=1` (any other value is ignored). An env-named serial that is not attached skips the tests, naming the variable but not its value, and both e2e modules carry the `device` marker so `pytest -m "not device"` deselects them.
- Folded `jdb` method and exception sessions into `DebugSessionManager` with 180 s TTL reaping, `spectra_sessions` listing, `spectra_detach` support, and OS-chosen free local ports (#54).
- Offloaded all MCP tool handlers off the asyncio event loop onto bounded worker threads with per-session lock serialization (#55).
- Graduated pull-request diff-mutation testing to a blocking CI check (#56).
- Discover `lldb-dap` via `xcrun -f lldb-dap` on macOS, reject `/usr/bin/jdb` and `/usr/bin/xcrun` stubs when no JDK or developer directory is configured, surface actionable `AdbNotFoundError` messages pointing to `spectra doctor`, and remove `$LINC_HOME` from `kotlin-debug-adapter` search paths.

### Fixed
- `.forge.yaml` is valid YAML again with a single `test_command`, and the manifest display name reads "LINC Spectra".
- Per-type bounded DAP event queues (`EVENT_QUEUE_MAXLEN = 256`) with condition-variable waiting, stale `stopped` event draining before resume commands, and thread-matched step completion (#49).
- DAP `terminated` and `exited` event handling so dead sessions fail pending requests immediately, short-circuit session-scoped tools with a clear error, and reap after a 60 s grace period (#50).
- Background daemon drain for debug adapter `stderr` into a bounded tail (`STDERR_TAIL_LINES = 50`), surfaced in `DAPError` and `DAPTimeoutError` diagnostics (#51).
- Resolved `frame_id=0` to the top stack frame via `stackTrace(levels=1)` in `spectra_variables` and `spectra_evaluate`, and added a 10 s timeout to `adb forward` and `adb forward --remove` (#53).
- Made `spectra_detach` idempotent on recently closed, auto-captured, or reaped `jdb` sessions (`detached: true, already_detached: true`) (#63).
- `spectra bellows` routes all structlog and stdlib logging to stderr before serving, so stdout carries only MCP JSON-RPC (log events no longer corrupt the stdio stream on the first tool call).
- `spectra_method_locals` / `spectra_exception_info` reject a NaN, infinite or negative `wait_s` and cap larger values at 180 s. Manual `max_steps` must be 0-16 and stepping stops after 20 s (`truncated` in the result). A collect now ends when its session is detached or reaped or `jdb` exits, and stays listed and detachable while it waits; a second concurrent collect on the same session is refused. `spectra_sessions`, `spectra_detach` and `spectra_usage_stats` no longer queue behind long collect waits.
- `jdb` stdout reader uses 64 KiB binary reads with incremental UTF-8 replacement decoding, truncates lines longer than 64 KiB with a marker, stops and joins on `close()`, bounds the line queue to 10,000 lines (reporting `dropped_lines` when non-zero), and keeps the first value per local name within a single reply.
- Fixed MCP server version reporting, exact `jdb` step-out method matching, private `jdb` working directory isolation, and public tool docstrings.

### Security
- Added `SPECTRA_USAGE_LOG=off|redacted|full` usage-log privacy modes (default `redacted`: length-only redaction of `expression` and `source_path`, error scrubbing on failed evaluations, `0600` file permissions with `O_NOFOLLOW`, and `SPECTRA_SESSION_ID` session attribution accepted only as 1-128 chars of `[A-Za-z0-9._-]`, else `manual`) and shared the `LINC_HOME` path resolver across `observability` and `usage_stats`.
- Validated Android package names, iOS bundle identifiers, and JVM class/method targets with `re.fullmatch` (`core/validation.py`) before they reach `adb shell` or `jdb` stdin, and rejected line breaks in `jdb` commands (#52).
- The DAP client now bounds hostile adapter input: header lines are capped at 1 KiB and `Content-Length` at 16 MiB, and a malformed frame ends the session cleanly; responses to no pending request are dropped, event names are capped at 64 (standard DAP events always admitted), and adapter `stderr` is read in bounded pieces.
- iOS `spectra_evaluate` now evaluates in expression-only `watch` context and refuses backtick-prefixed LLDB debugger commands.
- Validate Android serials and iOS Simulator UDIDs, cap identifier lengths and breakpoint/stacktrace bounds, and enforce strict MCP argument types.
- Force rotated usage logs to `0600`, refuse hard-linked or non-regular usage log paths, record tool results carrying `"error"` as `success: false`, and redact the user home prefix with `~` in `spectra_usage_stats`.

### Removed
- The `spectra attach` CLI stub, which only printed a placeholder; use the `spectra_attach` MCP tool.
- Dropped the unused `linc-device-kit` runtime dependency and its `spectra fuelcheck` row: spectra drives adb, xcrun, jdb and the debug adapters directly.

