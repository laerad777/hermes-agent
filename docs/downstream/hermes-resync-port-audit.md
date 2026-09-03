# Hermes resync port audit

Basis: upstream `05f548f35dd3242bf2ff74743e9112acde251f77`

Preserved candidate: `9cd472ea32a56596859972596d44e7bb88f5f82a`

## Classification

| Area | Decision | Reason |
| --- | --- | --- |
| `external/hermes-resync-plugin/**` | KEEP / OVERLAY | Continuity state, exact-recall tool, protocol and packaged assets remain outside core. The plugin now adapts the existing `on_session_reset` hook rather than adding a parallel boundary hook. |
| `external/hermes-resync-deploy/**` | KEEP / OVERLAY | Backup, health, launchd, controller and slot transactions are deployment concerns. |
| `gateway/lifecycle_observer.py` | MINIMAL CORE | Product-neutral payload bounding, deduplication and typed delivery facts. This extracted module replaces the old 328-line `gateway/run.py` concentration. |
| `gateway/run.py` | MINIMAL CORE | Emits an admitted-turn fact at the only layer that sees queued/internal/re-entry origins; stages bounded plugin context; wires streaming terminal outcomes; enriches the existing auto-reset hook. |
| `gateway/platforms/base.py` | MINIMAL CORE | Emits one final logical delivery result from the existing central delivery accounting point. Per-send adapter instrumentation was removed. |
| `gateway/stream_consumer.py` | MINIMAL CORE | Reports exactly one terminal streaming result. Preview sends and edits remain unobserved. |
| `gateway/session.py` | MINIMAL CORE | Explicit reset retains the previous session ID using the already-persisted `prev_session_id` field. No schema or migration. |
| `gateway/slash_commands.py` | MINIMAL CORE | Adds bounded route identity to the existing `on_session_reset` invocation. The proposed parallel `on_session_boundary` API was removed. |
| `hermes_cli/plugins.py` | MINIMAL CORE | Adds only the two missing generic hook names: admitted turn origin and terminal delivery result. |
| `acp_adapter/server.py`, `tools/mcp_tool.py` | UPSTREAMED / DROP | Current upstream already has the needed provider probe and snake/camel MCP structured-content handling; downstream hunks were discarded. |
| Old structured-delivery lifecycle test | DROP | Superseded by current upstream delivery-ledger and stream-finalization behavior plus focused observer tests. |

## Core boundary result

The preserved candidate changed eight existing core files with `+493/-18`, including
`gateway/run.py +328/-9`. The port keeps seven existing core files with small hook/wiring
hunks and adds one focused generic helper module. Product state, release logic, protocol,
skills, and deployment remain external.

## Required verification

- focused gateway observer, session continuity and stream-consumer tests;
- external plugin/deploy tests;
- existing upstream plugin and gateway lifecycle suites;
- `git diff --check` and exact path review;
- candidate-versus-basis broad test comparison before promotion.