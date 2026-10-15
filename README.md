# mempalace-validator

Enforcement point for the MemPalace taxonomy policy (see
`~/.claude/CLAUDE.md`, "MemPalace Taxonomy and Maintenance Protocol").
A streamable-HTTP MCP proxy between every local MCP client (codex,
claude code, antigravity) and the MemPalace server, so policy is
enforced at the filing boundary instead of by agent discipline.

The full prose protocol (filing rules, maintenance/audit procedure,
policy-change governance, proxy-down fallback) lives in the deployer's
shared agent-directives file and is intentionally not bundled in this
repo; `policy.json` in the repo is the canonical template for the
enforceable rules.

## What it does
- Normalizes legacy room aliases (`prs`/`pull_requests` -> `pull-requests`,
  `pr-review`/`pr-reviews` -> `reviews`, `reference` -> `references`).
- Rejects writes to rooms outside the 11-room canonical set
  (`decisions`, `architecture`, `implementation`, `testing`, `workflow`,
  `pull-requests`, `reviews`, `feedback`, `references`, `status`, `diary`),
  including `update_drawer` moves (drift prevention).
- Validates wings: project wings from an allowlist; `wing_<agent>` diary
  wings auto-allowed by pattern; model-version/task-name/capitalized wings
  rejected. `diary_write` agent names are validated too (they seed wings).
  The two wing styles are a deliberate two-category convention — do not
  converge on one.
- Hard-blocks near-duplicate `add_drawer` calls via upstream
  `mempalace_check_duplicate` (threshold 0.9).
- `checkpoint` validation is atomic: one bad item rejects the whole call.
- Logs every decision to `~/.mempalace/validator-report.jsonl`;
  every request to `~/.mempalace/validator-requests.jsonl`.
- Fails open on engine errors (write goes through, `engine-error` reported).
- Reads, `kg_*`, deletes, and unknown methods pass through untouched.

## Layout
- `proxy.py` — the proxy + policy engine (Python 3.12 stdlib only).
- `policy.json` — policy template; the live copy is `~/.mempalace/policy.json`
  (hot-reloaded on mtime change; that file is the machine source of truth).
- `tests/test_policy.py` — unit tests: `python3 -m pytest tests/ -q`.
- `mempalace-validator.service` — systemd user unit (127.0.0.1:8766).
- `docs/PLAN.md` — full design plan.

## Operations
    systemctl --user enable --now mempalace-validator   # start + autostart
    systemctl --user status mempalace-validator         # health
    tail -f ~/.mempalace/validator-report.jsonl         # decisions
    python3 -m pytest tests/ -q                         # unit tests

Emergency bypass (enforcement off, forwarding on):
    systemctl --user stop mempalace-validator
    ./run.sh --passthrough   # not implemented: passthrough = stop the proxy
                             # and point clients back at :8765 (one-line config revert)

Rollback: point each client's MCP URL from :8766 back to :8765
(codex: ~/.codex/config.toml; claude code: ~/.claude.json mcpServers.mempalace).

## Adding wings/rooms to the policy
1. Decide (human review — new project wings need a real project identity).
2. Edit `~/.mempalace/policy.json` (`known_wings` or `room_aliases`).
3. No restart needed (hot reload). Commit the change to this repo and update
   the live copy.

## Why a proxy (and why here)
All local clients are HTTP MCP clients on the same upstream URL, so one
localhost interception point covers every write path. A client-side wrapper
cannot stop direct low-level MCP calls, and Codex hooks cannot rewrite tool
arguments. Consumer-side: no MemPalace upstream changes. Code lives in
`~/mempalace-validator` (first-party personal infra, own git repo); config and
reports live in `~/.mempalace/`.
