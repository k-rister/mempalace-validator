# MemPalace Filing Validator — Plan (v0.1 draft, 2026-10-14)

Enforces the MemPalace Taxonomy and Maintenance Protocol (CLAUDE.md "Enforcement"
section) at the filing boundary: normalize aliases, reject unknown rooms,
validate wings, duplicate-detect before filing, and emit a report for
rejected/normalized writes. Consumer-side only — no upstream MemPalace changes.

## 1. Goals / non-goals
Goals
- Single interception point for ALL local writes to the palace.
- Normalize legacy room aliases (pull_requests/prs → pull-requests; pr-review/pr-reviews → reviews; reference → references).
- Reject writes to rooms outside the 11-room canonical set (including via update_drawer moves).
- Validate wings: known project wings from allowlist; agent diary wings match `wing_<name>` pattern.
- Run duplicate detection before add_drawer; report near-duplicates.
- Append every decision to a machine-readable report (drift visibility).
Non-goals (v1)
- No KG triple validation, no deletion gating, no content rewriting.
- No multi-host / remote / multi-user deployment (localhost only).
- No changes to MemPalace source (user is a consumer; upstream issues filed as user reports).

## 2. Architecture
Shared enforcement point: a local streamable-HTTP MCP proxy between every client
and the MemPalace server.

    codex / claude-code / antigravity
            |  http://127.0.0.1:8766/mcp
            v
    +--------------------------------+
    | mempalace-validator (proxy)    |   Python 3.12, stdlib only
    |  - session pass-through        |   ~600 LOC, single package
    |  - policy engine on writes     |
    |  - JSONL decision report       |
    +--------------------------------+
            |  http://127.0.0.1:8765/mcp
            v
    MemPalace server (unchanged, single-threaded)

Why a proxy:
- Recon: codex config.toml AND claude code (two project scopes) both use
  type=http at http://localhost:8765/mcp; antigravity wing exists (config TBD).
  All-HTTP clients => one interception point covers 100% of local writes.
- A client-side wrapper CLI cannot prevent direct low-level MCP calls; the
  policy explicitly requires no bypass. Only path-level interception enforces.
- Codex hooks can approve/deny but cannot rewrite tool arguments, so hooks
  can reject but not normalize — insufficient alone.
- No upstream change required; bug #2748-style upstream gaps stay upstream.

## 3. Proxy requirements
- Streamable-HTTP MCP pass-through:
  - initialize: open upstream session, map client-session-id -> upstream-session-id,
    return upstream Mcp-Session-Id header to client.
  - notifications/initialized, tools/list, ping, resources: pass through.
  - tools/call: policy-engine pass for write tools (see matrix), then forward.
  - Unknown methods: pass through untouched (forward-compatible).
- Multi-session: concurrent clients each get an isolated upstream session map.
- Stream response bodies without buffering (JSON or text/event-stream).
- Fail-safe: policy-engine exception => forward call as-is, log engine-error to
  report (never lose a write). `--strict` flag flips to fail-closed.
- `--passthrough` flag: no validation (emergency), reporting still on.
- Deployment: run script now; systemd user unit `mempalace-validator.service`
  (Restart=always) recommended (no user units exist yet on this box).
- Bind 127.0.0.1 only; upstream already has no auth — proxy must not widen access.

## 4. Policy engine
Config file: ~/.mempalace/policy.json (machine source of truth; CLAUDE.md points
to it). Hot-reload on mtime change.

    {
      "version": 1,
      "canonical_rooms": ["decisions","architecture","implementation","testing",
        "workflow","pull-requests","reviews","feedback","references","status","diary"],
      "room_aliases": {
        "pull_requests": "pull-requests", "prs": "pull-requests",
        "pr-review": "reviews", "pr-reviews": "reviews",
        "reference": "references"
      },
      "known_wings": ["CommonDataModel","bench-trafficgen","bench-coremark",
        "bench-stream","crucible","crucible-dev-tools","ephemeral-buffer-mcp",
        "krister-setup","linux-dataplane-accelerator","multiplex","rickshaw",
        "tool-bpf"],
      "agent_wing_pattern": "^wing_[a-z0-9][a-z0-9_-]*$",
      "known_agents": ["codex","claude","antigravity"],
      "duplicate_threshold": 0.9
    }

Decision procedure per write argument:
1. Normalize: apply room_aliases. (report action=normalized)
2. Validate room: must be in canonical_rooms, else REJECT.
3. Validate wing: in known_wings OR matches agent_wing_pattern (auto-allow new
   agent wings, report); else REJECT with "add to policy.json after human
   review" guidance.
4. Duplicate detection (add_drawer only): forward-check via upstream
   mempalace_check_duplicate(content); score >= threshold => report
   action=duplicate-warning, forward anyway (server also checks; see decision D4).
5. Forward the (possibly rewritten) call; report action=allowed.

## 5. Per-tool behavior matrix
| Tool                      | room arg(s)              | wing arg(s)            | v1 behavior |
|---------------------------|--------------------------|------------------------|-------------|
| add_drawer                | room                     | wing                   | full pipeline (steps 1-5) |
| update_drawer             | room (if given)          | wing (if given)        | full pipeline on given args; blocks drift via moves; content-only updates pass |
| checkpoint                | items[].room, diary.wing | items[].wing           | per-item validation; ANY invalid item => reject whole call atomically (checkpoint is all-or-nothing); dedup is upstream (checkpoint self-dedups) |
| diary_write               | -                        | wing (opt), agent_name | wing vs agent_wing_pattern; agent_name sanity (non-empty, [a-z0-9_-]+) |
| mine                      | -                        | wing                   | validate wing only; mined rooms are content-derived => post-hoc audit only |
| create_tunnel             | source/target room       | source/target wing     | validate (cheap, same rules) |
| delete_* (drawer/tunnel/by_source) | -               | -                      | pass through (deletion governance stays human) |
| kg_*                      | -                        | -                      | pass through (out of v1 scope) |
| all read tools            | -                        | -                      | untouched pass-through |

## 6. Rejection + report
Rejection response: valid MCP tool result, text block stating:
  - policy rule violated, offending value, closest valid option(s)
  - "add to policy.json after human review" pointer
  - never raises an MCP-level exception (client session stays healthy)
Report: ~/.mempalace/validator-report.jsonl, one JSON object per decision:
  {"ts","client" (from clientInfo on initialize),"tool","action":
   "allowed|normalized|rejected|duplicate-warning|engine-error|passthrough",
   "field","from","to","wing","room","drawer_id","duplicate_score","reason"}
Summary subcommand: `mempalace-validator report [--since 7d]` -> counts by
action/tool/wing; used by the periodic audit (CLAUDE.md maintenance procedure).

## 7. Phases
- P0 (decisions): lock policy.json, D1-D7 below, port, clients. ~1h
- P1 (passthrough proxy): session mapping + transparent forwarding + request
  log; verify with a test client (reads + one write to scratch wing).
- P2 (policy engine): normalization, rejection, dedup-warn, report file.
- P3 (tests):
  - unit: table-driven policy engine (aliases, unknown room, wing pattern,
    checkpoint atomicity, update_drawer move-block, engine-error fail-open)
  - integration: in-process mock upstream; assert pass-through untouched,
    normalized args rewritten, rejected => NO upstream call, session isolation
    across 2 concurrent clients
  - live smoke: 3 real writes (1 normalized, 1 rejected, 1 allowed), then
    delete the test drawers (confirmed test data, per deletion policy)
- P4 (rollout): switch codex + claude code (both scopes) + antigravity (if
  found) configs 8765 -> 8766; verify tools/list + one write per client;
  document rollback (revert URL; or run proxy --passthrough as stopgap).
- P5 (handoff): README; update CLAUDE.md Enforcement section (validator live;
  manual checks are the fallback when proxy is down); policy.json = single
  source of truth for the room/wing rules.

## 8. Risks / mitigations
- SPOF (proxy down => no memory writes): systemd Restart=always, --passthrough
  mode, one-line URL rollback. Writes are queued by nothing; availability is
  the tradeoff, accepted for a personal-store enforcement point.
- Upstream protocol drift: pass-through is version-tolerant; engine only keys
  on tool NAMES and argument names.
- Engine bug silently skips validation: fail-open default + engine-error in
  report; --strict mode available; P3 unit tests cover the decision path.
- policy.json drifts from CLAUDE.md text: CLAUDE.md becomes a pointer to
  policy.json for the machine-checkable rules; prose stays for rationale.

## 9. Open decisions (need user)
- D1: rollout posture — enforce immediately (recommended: audit+migration
  already validated the mapping) vs report-only dry-run week first.
- D2: client coverage — codex + claude code certain; locate antigravity's
  MCP config and include it (otherwise it bypasses the validator).
- D3: unknown project wings — reject by default, human adds to policy.json
  (recommended); confirm.
- D4: add_drawer near-duplicate — warn-and-forward (recommended; matches
  server behavior) vs block-and-report.
- D5: engine-error semantics — fail-open+report (recommended) vs fail-closed.
- D6: deployment — systemd user service (recommended) vs manual run script.
- D7: port 8766 for the proxy (8765 stays the server) — confirm.

## 10. Estimates
- Proxy + engine: ~600 LOC Python (stdlib only).
- Tests: ~300 LOC.
- Total effort: P1-P3 ~half a day focused work; P4-P5 ~1h.

## 11. Decisions — locked 2026-10-14 (user-approved)
- D1: enforce on day one (no dry-run period).
- D2: cover antigravity too (config location TBD — hunt during P4;
  ~/antigravity contains project checkouts, MCP config not yet located).
- D3: unknown project wings rejected; human adds to policy.json.
- D4: add_drawer near-duplicate => HARD BLOCK (threshold 0.9; upstream
  check_duplicate returns {is_duplicate, matches[{id,wing,room,similarity}]}).
- D5: fail-open on engine errors + engine-error report.
- D6: systemd user service (Restart=always).
- D7: proxy port 8766.

## 12. Placement (user-approved)
- Code: ~/mempalace-validator — dedicated first-party repo,
  own git. NOT in a project workspace (codex-qwen) and NOT in /opt
  (that's for vendored third-party installs, e.g. mcp-jira-server).
- Config + reports: ~/.mempalace/policy.json, validator-report.jsonl,
  validator-requests.jsonl (alongside the palace itself).
- Service: ~/.config/systemd/user/mempalace-validator.service.
- Optional later: push to GitHub (private) for durability — not required.

## 13. Status (2026-10-14, antigravity closed same day)
- P1-P3 done: proxy + policy engine deployed at ~/mempalace-validator
  (git), 13/13 unit tests, live smoke verified: alias normalization (prs ->
  pull-requests write landed correctly), unknown-room rejection, duplicate
  hard-block (similarity 1.0), read pass-through, per-client attribution via
  minted Mcp-Session-Id (upstream 3.10.0 issues no session id).
- P4 done: systemd user service `mempalace-validator` enabled+running
  (127.0.0.1:8766). codex (~/.codex/config.toml) and claude code
  (~/.claude.json, 2 project scopes) switched 8765 -> 8766; backups at
  *.bak-validator. New client sessions enforce from day one.
- P4 open item CLOSED (2026-10-14): antigravity's MCP config was
  ~/.gemini/config/mcp_config.json (serverUrl key); switched 8765 -> 8766.
  All three clients (codex, claude code, antigravity) now route through the
  proxy. Also found: ~/.gemini/GEMINI.md is a third symlink to the canonical
  protocol file ~/ai-directives.md.
- P5 done: protocol Enforcement section replaced with live validator rules
  (report paths, policy.json governance, rejection handling, engine-error
  canary, fallback procedure, antigravity coverage caveat); Maintenance
  procedure now names the decision report as the primary drift signal.
  NOTE: the protocol doc is a single canonical file, ~/ai-directives.md,
  symlinked as BOTH ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md — edit only the
  canonical file.
- P6 done (2026-10-15): `report` summary subcommand — the last open item from
  section 6. `python3 proxy.py report [--since 7d|24h|90s|ISO|all] [--json]
  [--detail] [--file]`: read-only aggregation of validator-report.jsonl —
  decisions by action, by client (unknown/unattributed as visible buckets),
  by wing (drawer-level writes only), engine-error reasons + last-occurrence
  canary line; `started` lines reported separately as process generations.
  12 unit tests (suite now 25/25). jq recipes in the monitoring runbook remain
  for request-log latency views.
- P7 done (2026-10-15): public-push prep — all commit authors scrubbed to the
  neutral identity (krister <krister@localhost>; repo-local git identity set
  so it cannot recur), .gitignore for pycache, service unit uses %h instead
  of an absolute home path (installed copy synced), this doc's absolute home
  paths generalized. Push to GitHub pending.
- Notes: upstream mempalace 3.10.0; initialize responses are plain JSON;
  proxy strips its own minted session ids before forwarding (upstream is
  sessionless). check_duplicate ~0.1s warm (first call in a session can be
  slow — approval/semantic-index overhead, not proxy latency).
