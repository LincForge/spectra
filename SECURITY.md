# Security

## Report a vulnerability

Use GitHub's **private vulnerability reporting** on this repository (Security → Report a
vulnerability). Please do not open a public issue for security problems. We aim to
acknowledge within 3 business days.

## Threat model in one paragraph

spectra runs on your machine over stdio MCP only and opens no network listener; it attaches
only to debuggable builds on Android devices and iOS simulators that you connect.
`spectra_evaluate` and the `jdb` path execute code inside the debuggee **by design**, so any
local caller that can invoke the MCP server can run code in the attached application. Android
package names reach `adb shell` (re-parsed by the device's `sh -c`), JVM class/method targets
reach `jdb` stdin (line-oriented), and iOS bundle IDs reach `xcrun simctl` as an argument;
each is validated with `re.fullmatch` in `core/validation.py` before any subprocess or
debugger command is sent. Android serials and simulator UDIDs are allowlisted (serial
characters `[A-Za-z0-9._:-]`, first character alphanumeric; UDID = UUID or `booted`; never a
leading `-`) and only ever passed as `argv`, never through a shell. Each `jdb` session runs in
a private working directory per session so `.jdbrc` / `jdb.ini` files in the working directory
are ignored (a `jdb` rc file in the user's home directory is still read, as `jdb` always does).
The local MCP usage log is written on your machine
only and is redacted by default (`SPECTRA_USAGE_LOG=off|redacted|full`, default `redacted`).

On the iOS simulator the debuggee is a macOS process running with your user's privileges, so
an expression that calls into the process (including C or Objective-C/Swift function calls
such as `(int)getpid()`) runs on your Mac as you. spectra sends iOS expressions to `lldb-dap`
in an expression-only (`watch`) context and refuses backtick-prefixed input, so
LLDB debugger commands (`platform shell`, `script`) cannot be issued through
`spectra_evaluate`; because target expressions themselves still execute inside the simulator
process on the host, treat access to the MCP server as local code execution.

## Independent review

### 2026-10-24 pre-release review

An adversarial security review and first-time user ("stranger") run conducted on 2026-10-24 evaluated the pre-release v0.1.0 candidate (private `linc-spectra` commit `88cc41f`, candidate wheel SHA-256 `d7a441307df273487cf02c3c380867fc203c92fa25fbfaf306becaba5ef5dce3`). Testing used an isolated session with no access to fix history, operating in-process against recording fakes for `adb`, `xcrun`, `jdb`, `kotlin-debug-adapter`, and `lldb-dap`, plus live verification against a booted iOS Simulator with Xcode `lldb-dap`.

Across 42 adversarial attempts, 29 held and 16 security/reliability findings were identified (1 High confirmed on a live iOS Simulator, 3 Medium, 12 Low across 6 groups), all fixed before release:
- **LLDB debugger command execution in iOS `spectra_evaluate` (High):** `spectra_evaluate` forwarded iOS expressions to `lldb-dap` with `context="repl"`, allowing backtick-prefixed or command-shaped inputs (`platform shell`, `script`) to execute LLDB debugger commands on the macOS host. Fixed by sending iOS evaluations in expression-only `watch` context and refusing backtick-prefixed expressions before they reach the adapter.
- **Structured logging on MCP `stdout` in `spectra bellows` (Medium):** Default `structlog` configuration wrote log lines to `stdout` on the first tool call, corrupting the stdio JSON-RPC stream. Fixed by routing `structlog` and standard-library logging to `stderr` before starting the MCP stdio server.
- **Unbounded `wait_s` and worker pool exhaustion on JDB collect tools (Medium):** Passing a huge or infinite `wait_s` to `spectra_exception_info` or manual `spectra_method_locals` tied up all 8 worker threads so `spectra_sessions` and `spectra_detach` hung, and wait loops ignored subprocess EOF or session closure. Fixed by rejecting non-finite or negative `wait_s`, capping `wait_s` at 180 s, waking waits on session close or `jdb` exit, keeping waiting sessions listed and detachable, rejecting concurrent collects on the same session, and running `spectra_sessions`, `spectra_detach`, and `spectra_usage_stats` outside the blocking pool.
- **Quadratic JDB stdout reader and reader thread leak (Medium):** Reading `jdb` output character-by-character caused multi-second stalls on long single-line local values and left the reader thread spinning after session close. Fixed by reading 64 KiB binary chunks with incremental UTF-8 replacement decoding, truncating lines above 64 KiB, bounding the line queue to 10,000 lines, and joining the reader thread on `close()`.
- **Unbounded manual `max_steps` in `spectra_method_locals` (Low):** Large `max_steps` values issued unbounded `next` commands without returning. Fixed by requiring `max_steps` in `0..16` and enforcing a 20 s manual stepping budget.
- **Hostile adapter framing and queue growth (Low):** Malformed DAP `Content-Length` headers, newline-less header or `stderr` streams, unrequested responses, and synthetic event names could grow memory. Fixed by capping header lines at 1 KiB and `Content-Length` at 16 MiB, dropping responses without a pending request, capping distinct event queues at 64 (always admitting standard DAP events), reading `stderr` in 4 KiB chunks, and keeping only the first value per local name within a single JDB reply.
- **Android serial and iOS Simulator UDID validation (Low):** Device identifiers and leading-dash values were not validated before reaching `adb` or `xcrun` `argv`. Fixed by allowlisting Android serials (`[A-Za-z0-9._:-]`, alphanumeric first character, max 128 chars) and iOS Simulator UDIDs (canonical UUID or `booted`), capping package, bundle ID, class/method, and path lengths, bounding breakpoint line numbers and `frame_id`, and enabling strict MCP argument types.
- **Non-UTF-8 JDB output handling (Low):** Invalid UTF-8 bytes in debuggee strings terminated the JDB reader thread and emptied captured locals. Fixed by decoding with an incremental UTF-8 replacement decoder.
- **macOS `lldb-dap` discovery and system stub verification (Low):** `spectra doctor` and `spectra_attach` missed `lldb-dap` under Xcode / Command Line Tools when not on `PATH` and reported `/usr/bin/jdb` and `/usr/bin/xcrun` stubs as healthy when no JDK or developer directory was configured. Fixed by resolving `lldb-dap` via `xcrun -f lldb-dap`, verifying `/usr/bin/jdb` via `/usr/libexec/java_home` and `/usr/bin/xcrun` via `xcode-select -p`, isolating each `jdb` session in a private temporary directory, and returning actionable `adb` missing-tool errors.
- **Rotated usage log permissions and link safety (Low):** Rotating a pre-existing world-readable usage log preserved its permissions on the rotated file, hard-linked log paths were accepted, and tool results returning an `"error"` dictionary were recorded as `success: true`. Fixed by tightening the descriptor to `0600` with `fchmod` before rotation, refusing files with `st_nlink > 1`, recording `"error"` dictionaries as `success: false`, and redacting the home directory prefix to `~` in `spectra_usage_stats`.

Candidate wheel SHA-256 (pre-fix): `d7a441307df273487cf02c3c380867fc203c92fa25fbfaf306becaba5ef5dce3`.

### 2026-10-25 post-fix verification

A second verification run on 2026-10-25 re-executed all 42 adversarial vectors, the live iOS Simulator `lldb-dap` evaluation check, and the stranger run against the fixed release candidate wheel (`48e96ce7b15124502f06ae387db02e0fc58f2a64c191889b43dd52445ef34bd0`, built from private `linc-spectra` commit `92b51f3`).

Results: 0 High, 0 Medium, 0 Low reproduced. All 42 vectors, the live iOS Simulator `watch`-context and backtick-refusal checks, and the stranger run passed cleanly.

### Release candidate verification

The reviewed v0.1.0 release candidate contains all fixes above:
- Source: private `linc-spectra` commit `92b51f3` (tree `ff0aa93d098ad2e9f196cafba3ccc17695309f88`). Commits after it and before the `v0.1.0` tag change only `SECURITY.md` and its test, not the package.
- Reviewed candidate wheel SHA-256: `48e96ce7b15124502f06ae387db02e0fc58f2a64c191889b43dd52445ef34bd0`
- The wheel published on PyPI will have a different SHA-256 even though its contents are the same, because a wheel records each file's timestamp and the published build's timestamps differ from the candidate's.
- After `v0.1.0` is published, this section names the public `v0.1.0` tag, its commit, and the SHA-256 of the wheel PyPI serves. To check that hash: `pip download linc-spectra==0.1.0 --no-deps -d .` then `shasum -a 256 linc_spectra-0.1.0-py3-none-any.whl`.
