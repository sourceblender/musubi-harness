# Changelog

All notable changes to `musubi-harness` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.1](https://github.com/sourceblender/musubi-harness/compare/v1.1.0...v1.1.1) - 2026-09-26

### Security
- Capture also refuses these credential formats (`SECRET_RE`): OpenAI `sk-…` and
  Anthropic `sk-ant-…` keys, GitHub `gho_`/`ghu_`/`ghs_`/`ghr_` tokens, AWS
  `AKIA`/`ASIA` key ids, Slack `xox*-` tokens, Google `AIza…` keys, JWTs (signed
  or not), and encrypted, PGP and other PEM private-key blocks. The Bearer rule
  no longer refuses lowercase identifiers such as `Bearer credentials_file`,
  which used to cost whole turns about auth config.
  Measured: 24 of 24 credential samples caught (was 9); across 1,137 tracked
  files in six repositories, no new false positives and 7 old ones removed.

## [1.1.0](https://github.com/sourceblender/musubi-harness/compare/v1.0.1...v1.1.0) - 2026-09-26

### Added
- `musubi-memory-data`: a public, stdlib-only HTTP client for the six Musubi
  operations the harness calls (`status`, `recent`, `search`, `get`,
  `capture-durable`, `receipt-lookup`). It speaks the same argv and JSON as the
  operator `memory-data` tool, so the capture → outbox → drainer → receipt →
  readback contract is unchanged. Endpoint and credential come from
  `MUSUBI_API_URL` and `MUSUBI_TOKEN` in the child environment, which plugins
  fill from their own settings.
- `PluginRuntime.memory_data_bin()` falls back to that client **last**, after
  every existing resolution step. Before 1.1.0 an install without the private
  operator tool raised `memory_data_unavailable` and could not capture or recall.
- `tests/test_memory_data_parity.py`: both binaries run against one fake Musubi
  and must send identical requests and print identical JSON. It runs when
  `MUSUBI_PARITY_MEMORY_DATA` points at an operator `memory-data`. CI skips
  it, because that tool is private and cannot be installed there.
- `MUSUBI_TOKEN` must be a JWT (`iss`, `sub`, `presence`, `scope`):
  `receipt-lookup` decodes those claims to self-attest the observer, and exits 2
  on an opaque token. The other commands only send it as the bearer.

- `PluginRuntime.local_tool_environment(config)`: `tool_environment` minus
  `MUSUBI_API_URL` / `MUSUBI_TOKEN` (`TRANSPORT_ENV`), for subprocesses that
  only touch the local outbox. The MCP facade's `remember` uses it; the drain
  and memory-data reads keep the full environment. Additive:
  `tool_environment` is unchanged.

### Security
- The bundled client refuses HTTP redirects, so the bearer token is never sent
  to a `Location` target. It accepts only `http`/`https` URLs without
  credentials, query or fragment, and caps responses at 16 MiB.
- stderr carries only locally-generated text (status, method, path, a
  well-formed server error code, and a socket errno with its message looked up
  locally). Response bodies and
  server-supplied exception text are never printed, so a server or proxy that
  echoes the Authorization header cannot leak it through our logs in any
  encoding. `MUSUBI_TOKEN` must use the RFC 6750 token alphabet; a malformed
  one is refused without echoing it.
- A malformed `MUSUBI_API_URL` is a configuration error (exit 2), never a
  traceback.

### Unchanged
- Where an operator `memory-data` is configured, on `PATH`, beside
  `musubi-harness`, or in the development root, it is still the one used.

## [1.0.1] - 2026-09-26

### Added
- `py.typed` marker (PEP 561) so downstream mypy sees the package as
  type-complete. The wheel now ships the marker file.

## [1.0.0] - 2026-09-26

### Added
- Initial public release of `musubi-harness` — the host-neutral Musubi
  memory runtime extracted from the `lib/musubi_harness/` source tree in
  the fleet-tools workspace.

### Notes
- This is the same code that the `musubi-claude`, `musubi-codex`,
  `musubi-livekit`, `musubi-hermes`, and `musubi-openclaw` seat adapters
  have been depending on at runtime via the workspace-internal
  `sys.path.insert(...)` shim. The extraction here makes that runtime
  dependency a real, versioned PyPI package so the adapters can pin
  against `musubi-harness>=1.0.0` instead of the workspace layout.

[Unreleased]: https://github.com/sourceblender/musubi-harness/compare/HEAD
[1.0.1]: https://github.com/sourceblender/musubi-harness/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/sourceblender/musubi-harness/compare/HEAD...v1.0.0
