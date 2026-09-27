# Contributing

Thanks for your interest in `musubi-harness`. This package is the shared
host-neutral runtime that every seat adapter in the `musubi-*` family
(Claude Code, Codex, Grok Build, LiveKit, Hermes, OpenClaw) depends on. Changes here
ripple out to every adapter — please keep that contract clean.

## Ground rules

1. **The dependency arrow points one way.** `adapter → harness`, never the
   reverse. This package must never import from a host adapter. If you
   find yourself wanting to, the change belongs in the adapter, not
   here.
2. **No new runtime dependencies without discussion.** The harness
   intentionally has zero non-stdlib runtime deps today. Every transport
   dep (MCP, agent framework, etc.) belongs in the adapter that uses
   it. Open an issue before adding anything here.
3. **No API surface expansion without an adapter confirmation.** If you
   add a new public symbol, confirm that at least one seat adapter
   actually needs it — or be ready to remove it before merge.
4. **`queued` and `verified` stay distinct.** A local row is a durable
   promise; a verified row with an exact `object_id` is a receipt. If a
   PR risks collapsing the two, it will be reverted.

## Development setup

```bash
git clone https://github.com/sourceblender/musubi-harness
cd musubi-harness
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

(`.` extras are added when they exist; today the package has no
optional-dependency groups.)

## Before opening a PR

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest
```

CI runs the same four checks on Python 3.12 against every PR.

## Pull request flow

1. Open a PR from a topic branch.
2. Describe the contract change and which adapters are affected.
3. CI must be green before review.
4. A maintainer reviews for contract integrity, dependency discipline,
   and type strictness. New code lands `mypy --strict`-clean.
5. Merge via squash. The release-please bot opens a follow-up PR to
   cut the version.

## Releases

Releases are managed by release-please. Conventional Commit messages
on `main` (`feat:`, `fix:`, `refactor:`, etc.) drive the next version
proposal; merging the release-please PR publishes `musubi-harness` to
PyPI.

## Security issues

Please email `ericmey@gmail.com` rather than opening a public issue.
See `SECURITY.md`.
