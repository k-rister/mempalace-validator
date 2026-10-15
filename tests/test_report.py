import calendar
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import proxy  # noqa: E402


LINES = [
    {"ts": "2026-10-14T08:00:00-0500", "action": "started", "tool": "validator"},
    {"ts": "2026-10-14T09:00:00-0500", "action": "allowed", "client": "codex-mcp-client",
     "tool": "mempalace_add_drawer", "wing": "crucible", "room": "status"},
    {"ts": "2026-10-14T10:00:00-0500", "action": "normalized", "client": "unknown",
     "tool": "mempalace_add_drawer", "field": "room", "from": "prs", "to": "pull-requests"},
    {"ts": "2026-10-14T11:00:00-0500", "action": "rejected", "client": "unknown",
     "tool": "mempalace_add_drawer", "reason": "MEMPALACE VALIDATOR: write rejected (unknown room)"},
    {"ts": "2026-10-14T12:00:00-0500", "action": "duplicate-block", "client": "claude-code",
     "tool": "mempalace_add_drawer", "duplicates": ["d1"], "similarities": [1.0]},
    {"ts": "2026-10-14T13:00:00-0500", "action": "engine-error", "client": None,
     "reason": "GET proxy failed: HTTP Error 404: Not Found"},
]
RAW = [json.dumps(l) for l in LINES] + ["not json"]


def test_parse_since_relative():
    now = 1_000_000
    assert proxy.parse_since("7d", now=now) == now - 7 * 86400
    assert proxy.parse_since("24h", now=now) == now - 86400
    assert proxy.parse_since("90s", now=now) == now - 90


def test_parse_since_all():
    assert proxy.parse_since("all") is None


def test_parse_since_iso():
    naive = proxy.parse_since("2026-10-14")
    offset = proxy.parse_since("2026-10-14T08:00:00-0500")
    assert naive == time.mktime(time.strptime("2026-10-14", "%Y-%m-%d"))  # naive -> local
    assert offset == calendar.timegm(time.strptime("2026-10-14 08:00:00", "%Y-%m-%d %H:%M:%S")) + 5 * 3600


def test_parse_since_invalid():
    for bad in ("", "7w", "yesterday", "2026-13-45"):
        try:
            proxy.parse_since(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad!r}")


def test_summarize_counts():
    s = proxy.summarize_report(RAW)
    assert s["lines_in_window"] == 6
    assert s["malformed"] == 1
    assert s["decisions_by_action"] == {
        "allowed": 1, "normalized": 1, "rejected": 1,
        "duplicate-block": 1, "engine-error": 1}
    assert s["started_count"] == 1
    assert "started" not in s["decisions_by_action"]
    assert s["by_client"]["codex-mcp-client"] == {"allowed": 1}
    assert s["by_client"]["unknown"] == {"normalized": 1, "rejected": 1}
    assert s["by_client"]["(unattributed)"] == {"engine-error": 1}
    assert s["by_wing"] == {"crucible": 1}
    assert s["engine_error_reasons"] == {"GET proxy failed: HTTP Error 404: Not Found": 1}
    assert len(s["details"]) == 4  # everything except the single allowed


def test_summarize_window_filter():
    cutoff = proxy._parse_ts("2026-10-14T11:00:00-0500")
    s = proxy.summarize_report(RAW, cutoff=cutoff)
    assert s["decisions_by_action"] == {"rejected": 1, "duplicate-block": 1, "engine-error": 1}
    assert s["started_count"] == 0  # started line at 08:00 is before the cutoff


def test_summarize_empty():
    s = proxy.summarize_report([])
    assert s["lines_in_window"] == 0
    assert s["decisions_by_action"] == {}
    assert s["details"] == []


def test_render_text_sections():
    s = proxy.summarize_report(RAW)
    text = proxy.render_text(s, "7d")
    for needle in ("decisions by action:", "by client:", "by wing", "engine errors:",
                   "codex-mcp-client", "(unattributed)", "process starts: 1", "last:"):
        assert needle in text, needle


def test_render_json_roundtrip():
    s = proxy.summarize_report(RAW)
    back = json.loads(proxy.render_json(s))
    assert back["decisions_by_action"] == s["decisions_by_action"]
    assert back["malformed"] == 1


def test_run_report_missing_file(capsys):
    assert proxy.run_report(["--file", "/nonexistent/validator-report.jsonl"]) == 1
    assert "no decision report" in capsys.readouterr().err


def test_run_report_bad_since(capsys):
    assert proxy.run_report(["--since", "7w"]) == 2
    assert "unparseable" in capsys.readouterr().err


def test_run_report_reads_file(tmp_path, capsys):
    path = tmp_path / "report.jsonl"
    path.write_text("\n".join(RAW) + "\n")
    assert proxy.run_report(["--file", str(path), "--since", "all"]) == 0
    out = capsys.readouterr().out
    assert "decisions by action:" in out
    assert "malformed: 1" in out
