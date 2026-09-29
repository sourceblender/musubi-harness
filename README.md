# musubi-harness

The host-neutral Musubi memory runtime — the shared core that every
`musubi-*` family host adapter depends on.

| Adapter | Repo |
| --- | --- |
| Claude Code | [sourceblender/musubi-claude](https://github.com/sourceblender/musubi-claude) |
| Codex | [sourceblender/musubi-codex](https://github.com/sourceblender/musubi-codex) |
| Grok Build | [sourceblender/musubi-grok](https://github.com/sourceblender/musubi-grok) |
| OpenCode | [sourceblender/musubi-opencode](https://github.com/sourceblender/musubi-opencode) |
| LiveKit | [sourceblender/musubi-livekit](https://github.com/sourceblender/musubi-livekit) |
| Hermes | [sourceblender/musubi-hermes](https://github.com/sourceblender/musubi-hermes) |
| OpenClaw | [sourceblender/musubi-openclaw](https://github.com/sourceblender/musubi-openclaw) |

## What this package is

`musubi-harness` is the single source of truth for the **host-neutral
contract** every Musubi seat adapter must honor:

- **Capture policy.** What is and is not a load-bearing turn envelope.
- **Outbox.** A per-identity, per-zone SQLite store of shadow records.
  Stage → drain → readback → receipt. `shadow`, `pending`, `accepted`,
  `verified`, `dead` — each is a distinct state and the contract says
  so.
- **Delivery.** Verified delivery only after a canonical readback returns
  the exact `object_id`. `queued` is a durable local promise; `verified`
  is the receipt. Never collapse the two.
- **Resolution.** Terminalization of legacy ambiguity with versioned
  operator evidence.
- **Namespace policy.** `actor == presence-prefix`; one seat cannot read
  or write under another seat's namespace.
- **Identity.** Env or `$PLUGIN_DATA/config.json` — all-or-nothing,
  never derived from the host.

What it is **not**: a host binding. There is no MCP server, no Claude
hook, no Codex hook, no Grok plugin, no OpenClaw manifest here. Those live in the
adapter repos. This package is the substrate they all stand on.

## What this package gives you

Three Python imports + two console scripts.

### Imports

```python
from musubi_harness import (
    # Capture
    CaptureDecision, CapturePolicy, TurnEnvelope,
    # Outbox + delivery
    Outbox, DeliveryStore, Drainer, DeliveryClient, MemoryDataClient,
    DeliveryJob, Readback, ReceiptLookup, canonical_request_digest,
    DeliveryNonMutatingRejection, DeliveryTerminalError, DeliveryTransientError,
    CAPTURE_CONTENT_TYPE, CAPTURE_OPERATION_ID,
    # Plugin contracts
    PluginRuntime, RuntimeConfig, RuntimeConfigError,
    PluginMcpFacade, PluginContinuity,
    # Resolution
    BoundaryEvidence, LiveReceiptObservation, LiveTypedNonMutatingRejection,
    OperatorAbandon, ProvenNonMutatingRejection, ReceiptObservation,
    ResolutionEvidence, parse_resolution_evidence,
    RESOLUTION_KINDS, LIVE_REJECTION_SCHEMA_VERSION, RESOLUTION_SCHEMA_VERSION,
)
```

### Console scripts

After `pip install musubi-harness`:

```
musubi-harness              --db <path> <command> [...]
musubi-harness-conformance  --source <name> [--file <jsonl>]
```

`musubi-harness` subcommands:
`enqueue`, `status`, `inspect`, `stage`, `remember`, `delivery-status`,
`resolve`, `drain`.

## Installation

```bash
pip install musubi-harness
```

That installs the package, the two console scripts, and nothing else.
The harness has no runtime dependencies beyond the Python standard
library — every transport-specific dep belongs in the adapter that
uses it.

## Versioning

Semver. `1.x.y` is the long-lived stable line that the host adapters
already pin against. Breaking changes to `musubi_harness` API surface
require a `2.0.0`.

## Cross-adapter invariants

The harness exists so that every seat adapter — Claude Code, Codex,
Grok Build, LiveKit, Hermes, OpenClaw — ships the same memory contract. To keep
that promise:

1. **Never import from a host adapter in this package.** The dependency
   arrow points one way: `adapter → harness`, never the reverse.
2. **Never collapse `queued` and `verified`.** A local row is a
   promise; a verified row with an exact `object_id` is a receipt.
3. **Never derive identity from the host.** Env or config, all-or-nothing.
4. **Never silently swallow failure.** Anything that goes wrong with
   capture or delivery must be visible (typically in the adapter's
   `degraded.jsonl`) but must not break the host session.

## Development

```bash
git clone https://github.com/sourceblender/musubi-harness
cd musubi-harness
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
mypy src
pytest
```

CI is `ruff` + `mypy --strict` + `pytest` on Python 3.12. Release is
managed by release-please; merging a release-please PR publishes to
PyPI as `musubi-harness`.

## License

Apache-2.0.
