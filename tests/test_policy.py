import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import proxy  # noqa: E402

DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "policy.json")))


def test_decide_room_canonical():
    assert proxy.decide_room("status", DATA) == ("allow", "status")
    for r in DATA["canonical_rooms"]:
        assert proxy.decide_room(r, DATA)[0] == "allow"


def test_decide_room_aliases():
    assert proxy.decide_room("prs", DATA) == ("normalized", "pull-requests")
    assert proxy.decide_room("pull_requests", DATA) == ("normalized", "pull-requests")
    assert proxy.decide_room("pr-review", DATA) == ("normalized", "reviews")
    assert proxy.decide_room("pr-reviews", DATA) == ("normalized", "reviews")
    assert proxy.decide_room("reference", DATA) == ("normalized", "references")


def test_decide_room_unknown():
    assert proxy.decide_room("bogus-room", DATA)[0] == "reject"
    assert proxy.decide_room(None, DATA) == ("none", None)


def test_decide_wing():
    assert proxy.decide_wing("crucible", DATA) == ("allow", "crucible")
    assert proxy.decide_wing("wing_codex", DATA) == ("agent-auto", "wing_codex")
    assert proxy.decide_wing("wing_new-agent_2", DATA) == ("agent-auto", "wing_new-agent_2")
    assert proxy.decide_wing("wing_GPT-5", DATA)[0] == "reject"      # capitalization variant
    assert proxy.decide_wing("gpt-5.5", DATA)[0] == "reject"          # model version
    assert proxy.decide_wing("task-2026-10", DATA)[0] == "reject"     # temporary session
    assert proxy.decide_wing(None, DATA) == ("none", None)


def test_add_drawer_normalizes():
    ok, args, entries, err = proxy.validate_call(
        "mempalace_add_drawer", {"wing": "crucible", "room": "prs", "content": "x"}, DATA)
    assert ok and err is None
    assert args["room"] == "pull-requests"
    assert ("normalized", {"field": "room", "from": "prs", "to": "pull-requests"}) in entries


def test_add_drawer_rejects_unknown_room():
    ok, args, entries, err = proxy.validate_call(
        "mempalace_add_drawer", {"wing": "crucible", "room": "nope", "content": "x"}, DATA)
    assert not ok and args is None
    assert "unknown room" in err and "'nope'" in err


def test_add_drawer_rejects_unknown_wing():
    ok, args, entries, err = proxy.validate_call(
        "mempalace_add_drawer", {"wing": "My-Project", "room": "status", "content": "x"}, DATA)
    assert not ok
    assert "unknown wing" in err


def test_update_drawer_move_block():
    ok, args, entries, err = proxy.validate_call(
        "mempalace_update_drawer", {"drawer_id": "d1", "room": "old-room-alias-x"}, DATA)
    assert not ok and "unknown room" in err
    ok2, args2, _, _ = proxy.validate_call(
        "mempalace_update_drawer", {"drawer_id": "d1", "room": "status"}, DATA)
    assert ok2
    ok3, args3, _, _ = proxy.validate_call(
        "mempalace_update_drawer", {"drawer_id": "d1", "content": "edit only"}, DATA)
    assert ok3  # content-only updates always pass


def test_checkpoint_atomic():
    items = [
        {"wing": "crucible", "room": "status", "content": "a"},
        {"wing": "crucible", "room": "bogus", "content": "b"},
    ]
    ok, args, entries, err = proxy.validate_call(
        "mempalace_checkpoint", {"items": items}, DATA)
    assert not ok and "item 1" in err
    items2 = [
        {"wing": "crucible", "room": "prs", "content": "a"},
        {"wing": "wing_claude", "room": "diary", "content": "b"},
    ]
    ok2, args2, entries2, _ = proxy.validate_call(
        "mempalace_checkpoint", {"items": items2,
                                 "diary": {"agent_name": "codex"}}, DATA)
    assert ok2
    assert args2["items"][0]["room"] == "pull-requests"  # normalized in place


def test_diary_write_agent_name():
    ok, _, _, err = proxy.validate_call(
        "mempalace_diary_write", {"agent_name": "GPT-5"}, DATA)
    assert not ok and "agent_name" in err
    ok2, _, _, _ = proxy.validate_call(
        "mempalace_diary_write", {"agent_name": "codex"}, DATA)
    assert ok2
    ok3, _, _, _ = proxy.validate_call(
        "mempalace_diary_write", {"agent_name": "codex", "wing": "wing_codex"}, DATA)
    assert ok3
    ok4, _, _, _ = proxy.validate_call(
        "mempalace_diary_write", {"agent_name": "codex", "wing": "Bad-Wing"}, DATA)
    assert not ok4


def test_mine_and_tunnel():
    ok, _, _, err = proxy.validate_call(
        "mempalace_mine", {"source": "/x", "wing": "task-temp"}, DATA)
    assert not ok and "unknown wing" in err
    ok2, _, entries2, _ = proxy.validate_call("mempalace_mine", {"source": "/x"}, DATA)
    assert ok2
    ok3, _, _, _ = proxy.validate_call(
        "mempalace_create_tunnel",
        {"source_wing": "crucible", "source_room": "status",
         "target_wing": "rickshaw", "target_room": "reviews"}, DATA)
    assert ok3
    ok4, _, _, _ = proxy.validate_call(
        "mempalace_create_tunnel",
        {"source_wing": "crucible", "source_room": "junk",
         "target_wing": "rickshaw", "target_room": "reviews"}, DATA)
    assert not ok4


def test_passthrough_tools():
    ok, args, entries, err = proxy.validate_call(
        "mempalace_search", {"query": "x"}, DATA)
    assert ok and args == {"query": "x"} and not entries and err is None
    ok2, _, _, _ = proxy.validate_call("mempalace_kg_add", {"subject": "s"}, DATA)
    assert ok2


def test_missing_policy_fails_open():
    ok, args, entries, err = proxy.validate_call(
        "mempalace_add_drawer", {"wing": "anything", "room": "whatever", "content": "x"}, None)
    assert ok and err is None
