# Capture exchange identity contract

Status: draft for Codex and Claude adapter review, 2026-09-28. This document
defines the capture boundary. Host adapters supply evidence from their own
transcripts; the harness must not infer an exchange from a hook's turn ID.

## Terms and boundary

An **exchange** is one terminal assistant answer and the ordered, eligible
tty-voiced input span since the preceding terminal answer in the same context,
or a classified trigger when that span is empty.
The span may contain several inputs. A steer, queued input, or injected peer
message can join the span. `tty-voiced` describes the input channel, not the
speaker: a host field such as `origin.kind=human` does not prove Eric spoke.

A **terminal answer** is an assistant message that ends a response to the
user. The adapter must identify it by a host record and a host terminal
marker, rather than by the presence of a Stop hook. For Codex, this is an
assistant `response_item` with `phase=final_answer` and its `msg_*` ID. For
Claude, this is the last assistant message with a terminal `stop_reason` of
`end_turn`, `stop_sequence`, or `refusal`; its model `message.id` is the anchor.
Claude transcript lines representing one model message are deduplicated by
`message.id` before selecting the terminal answer.

A **context** is the transcript conversation visible to the answer. A new
session or explicit context reset (including `/clear`) closes the previous
context. Compaction within a context does not close it. Each adapter must
prove these boundaries from host evidence; uncertainty at a boundary is a
decline, not a cross-context fold.

## Identity and replay

Each terminal-answer occurrence yields at most one capture event. Its identity
is a tuple of version, host, session, and the terminal answer's host message
ID (for example, `exchange.v1:<host>:<session>:<answer_id>`). An implementation
may encode that tuple as a string, but it must preserve the namespace and exact
IDs. Turn IDs, prompt IDs, timestamps, input text,
answer text, and content hashes are not exchange identities. In particular,
several exchanges may share one Codex turn ID.

The event carries the ordered eligible input records, their host record IDs
where available, the terminal-answer record ID, and enough transcript
provenance to rederive the span. Replay of the same transcript yields the same
event ID and span. Canonical content for the identity check is the ordered
input record host IDs, the exact input text for each record, the answer record
host ID, the exact answer text, and any trigger class, host record ID, and exact
trigger text.
Preserve whitespace and record order; normalization must not hide divergence.
The envelope carries exact ordered input IDs when they fit its metadata limit.
If they do not, it carries their count and the SHA-256 of a canonical JSON
array of those IDs (UTF-8, no added whitespace); the full list remains
rederivable from the transcript. It also carries the SHA-256 of a canonical
JSON array of each input record's exact text, so different per-record texts
cannot collide merely because their joined display text matches. Both
adapters use the same overflow and text-digest rules.

Test vector: for input IDs `["msg-u1","msg-u2"]`, the ID digest is
`771d33cf781a6d602e0f9a1aa015f1f8091adf517ad1acf7faad995fe304b2e9`.
For exact texts `["a\n\nb","é — tide"]`, the text digest is
`76c47782eab50b4bde0213914d1773556a210858b7c25d8f798c9dcb579bfd5f`.
The preimage is the UTF-8 encoding of the bare JSON array, with non-ASCII
characters unescaped and no spaces after separators (`ensure_ascii=False`,
`separators=(",", ":")` in Python).
An already stored event with the same ID and same canonical content is an
idempotent replay. The same ID with a different span, trigger, or answer is
an identity collision and must fail closed with a diagnostic; it must never
silently overwrite the stored event. Replaying a previous final must not
prevent a later final under the same turn ID from being captured.

The adapter must resolve the anchor and span from the transcript when the
native Stop payload omits the final message ID. A staged prompt or the Stop
payload can help locate candidates, but cannot alone establish identity or
exclude earlier inputs. If multiple terminal answers remain plausible for one
hook invocation, decline that invocation with an explicit ambiguity reason;
do not attach an arbitrary final. A later replay may recover it when the
transcript is complete.

This contract applies to new capture events. Existing turn-keyed event IDs
remain historical records; migration or backfill needs an explicit, separate
plan to avoid duplicate or falsely matched captures.

## Input span and machine records

Walk records in host transcript order within one context. After each terminal
answer, begin a new pending span. Append every eligible tty-voiced input,
including multiple inputs before the next terminal answer. At a terminal
answer, capture the pending span with that answer and clear it. An input with
no terminal answer yet remains pending, including after an interrupted turn;
fold it into the next terminal answer in the same context. A context reset or
session end discards a pending span without manufacturing a capture event, and
emits `pending_input_discarded_at_boundary` with the count of input records.

Machine records may provide context but never become tty-voiced input merely
because the host serializes them as a `user` message. Classify compaction
summaries, tool output, task notifications, and slash-command output from host
structure. On Claude, `promptSource=typed` or `queued` establishes tty-voiced
input regardless of text. A command-name record with no `promptSource` is a
classified `slash-command` trigger; when a local-command-stdout record has a
`parentUuid` naming it, that stdout is classified as machine output attached
to the trigger. Prefixes alone and substring matches are never sufficient
for this classification. An unpaired stdout or otherwise unclassifiable
record that could change the span makes that exchange ambiguous and must
decline. A turn containing only classified machine records and no terminal
answer creates no capture event; report a named no-final class when useful.

On Codex, inspect `internal_chat_message_metadata_passthrough.content_item_kinds`
on each `response_item` user message. `user.text` and `user.image` designate
tty-voiced content; `agents_md.instructions`,
`environments.environment_context`, and `plugins.recommendations` designate
host-supplied context. Classify each item by this metadata, not by a textual
prefix. If the kinds are absent or include an unknown kind, decline rather
than assume that all `role=user` content is tty-voiced. The adapter must also
avoid counting repeated serialization of the same host message ID twice.

An answer with no eligible tty-voiced input since the preceding terminal
answer may be captured with an empty voice span and a classified trigger,
including a background-task notification or a slash command. The trigger is
recorded separately with its class and host record ID; its text is never
laundered into tty-voiced input. If there are several plausible triggers,
the adapter must preserve their ordered provenance or decline as ambiguous.
An answer with neither eligible input nor a classified trigger declines as
`no_eligible_input`; no input is invented.

The harness envelope represents these as `input_kind=voice` (the default for
older adapters) or `input_kind=trigger`. A trigger envelope has empty
`user_text` and separate `trigger_class`, `trigger_record_id`, and
`trigger_text` fields. Delivery renders it as `Trigger (<class>): <text>`,
never as `User: <text>`. A voice envelope has nonempty `user_text` and no
trigger fields. The trigger fields are optional with defaults so existing
envelopes remain valid, and absent from serialized voice envelopes so exact
legacy replay remains idempotent. Trigger classes are closed vocabulary:
`task-notification`, `peer-message`, `scheduled`, and `slash-command`.
The harness and both adapters must update this vocabulary together; an
adapter must not emit a trigger class the harness cannot validate.
Secret screening and size limits apply to `trigger_text`; refused envelopes
redact it before storage.

## Conformance cases

| Transcript shape | Required result |
| --- | --- |
| One tty-voiced input, one terminal answer | One event anchored to the answer's host ID. |
| Two tty-voiced inputs, then one answer | One event with both inputs in transcript order. |
| Input, answer A, steered input, answer B under one Codex turn ID | Two events with distinct answer IDs and disjoint spans. |
| Tty-voiced input, compaction summary, terminal answer | One event containing the input; summary excluded from voice. |
| Tty-voiced input in a no-final turn, then a later terminal answer in the same context | One event at the later answer, containing the earlier input. |
| Tty-voiced input, interrupted tool use, later terminal answer in the same context | One event at the later answer, containing the earlier input. |
| Tty-voiced input, `/clear` or new session, then answer | Earlier input does not cross the boundary; emit `pending_input_discarded_at_boundary` with its count. |
| Peer message containing the literal text `/clear` or `<local-command-stdout>` | Tty-voiced input; neither a reset nor machine output. |
| Claude slash command with command-name and parent-linked local-command-stdout | Classified `slash-command` trigger; stdout excluded from voice. |
| Claude slash command with command-name and no stdout, then answer | One event with an empty voice span and `slash-command` trigger. |
| Background-task notification, then answer with no tty-voiced input | One event with an empty voice span and `task-notification` trigger. |
| Command-only or task-notification-only turn with no answer | No event; named no-final decline. |
| Unpaired command stdout or unresolved input provenance | Decline affected exchange as ambiguous. |
| Terminal answer with no eligible input or classified trigger | Decline as `no_eligible_input`. |
| Stop payload lacking final ID, transcript proving one matching final | Resolve anchor from transcript and capture once. |
| Stop payload lacking final ID, several plausible finals | Decline this invocation; replay when resolvable. |
| Exact replay of an already captured answer | Idempotent success, no second event. |
| Same answer ID with changed span or answer | Collision, fail closed. |

## Evidence behind this draft

Codex September 2026 transcripts show multiple terminal `final_answer`
messages under one `turn_id`, each with a distinct `msg_*` ID. Twenty-six
`turn_aborted` events in 85 transcripts had no final answer in their aborted
task segments; thirteen were followed by another task in the same transcript
with a different turn ID. The installed Interrupt hook clears the staged
prompt. The user-message metadata in those transcripts distinguishes 28,261
`user.text` records from host-supplied context kinds. No Codex user record in
that sample begins with Claude's command-name or local-command-stdout markers;
literal mentions of such markers occur inside ordinary text. Thus a
turn-keyed event, a stage-only input boundary, and textual prefix matching
would all lose or misclassify real exchanges.

Claude September 2026 transcripts show one distinct `end_turn` message ID per
prompt ID where `end_turn` exists, plus terminal `stop_sequence` and `refusal`
answers. Some tty-voiced input records have no answer under their own prompt
ID and are followed by a later answer. Claude prompt IDs label machine
records too; record classification and forward folding are necessary. Among
12,227 terminal answers measured in the September transcripts, 253 had only a
background-task notification since the previous answer, and 43 had only slash
command records. These are classified triggers, not invented tty-voiced text.
