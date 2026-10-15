#!/usr/bin/env python3
"""mempalace-validator: streamable-HTTP MCP proxy enforcing the MemPalace
taxonomy policy at the filing boundary.

- Transparent pass-through for reads and unknown methods (forward-compatible).
- On write tools: normalize room aliases, reject unknown rooms, validate
  wings, hard-block near-duplicate add_drawer, and log every decision.
- Fail-open on engine errors (the write still goes through, error reported).

Stdlib only. See docs/PLAN.md.
"""
import argparse
import json
import os
import re
import threading
import time
import http.server
import urllib.request
import urllib.error

HOME = os.path.expanduser("~")
POLICY_PATH = os.environ.get("MEMPALACE_POLICY", os.path.join(HOME, ".mempalace", "policy.json"))
REPORT_PATH = os.environ.get("MEMPALACE_REPORT", os.path.join(HOME, ".mempalace", "validator-report.jsonl"))
REQLOG_PATH = os.environ.get("MEMPALACE_REQLOG", os.path.join(HOME, ".mempalace", "validator-requests.jsonl"))
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_file_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _append(path, obj):
    try:
        with _file_lock:
            with open(path, "a") as f:
                f.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def report(action, **kw):
    kw["action"] = action
    kw["ts"] = _now()
    _append(REPORT_PATH, kw)


def reqlog(**kw):
    kw["ts"] = _now()
    _append(REQLOG_PATH, kw)


class Policy:
    """Hot-reloading policy file (checked on each request via mtime)."""

    def __init__(self):
        self._mtime = None
        self._data = None
        self.get()

    def _load(self):
        try:
            with open(POLICY_PATH) as f:
                return json.load(f)
        except Exception as e:
            report("engine-error", reason=f"policy load failed: {e}")
            return None

    def get(self):
        try:
            mt = os.stat(POLICY_PATH).st_mtime
        except OSError:
            mt = None
        if mt != self._mtime:
            self._data = self._load()
            self._mtime = mt
        return self._data


POLICY = Policy()


# ---------- pure decision functions (unit-tested) ----------

def decide_room(room, data):
    """Return (verdict, new_room). verdict in allow|normalized|reject|none."""
    if not room:
        return ("none", None)
    canonical = set(data.get("canonical_rooms", []))
    aliases = data.get("room_aliases", {})
    if room in canonical:
        return ("allow", room)
    if room in aliases:
        return ("normalized", aliases[room])
    return ("reject", None)


def decide_wing(wing, data):
    """Return (verdict, wing). verdict in allow|agent-auto|reject|none."""
    if not wing:
        return ("none", None)
    if wing in set(data.get("known_wings", [])):
        return ("allow", wing)
    pattern = data.get("agent_wing_pattern", r"^wing_[a-z0-9][a-z0-9_-]*$")
    if re.fullmatch(pattern, wing):
        return ("agent-auto", wing)
    return ("reject", wing)


def _room_error(room, data):
    aliases = data.get("room_aliases", {})
    valid = sorted(set(data.get("canonical_rooms", [])))
    lines = [
        "MEMPALACE VALIDATOR: write rejected (unknown room)",
        f"offending value: {room!r}",
        "valid rooms: " + ", ".join(valid),
    ]
    if aliases:
        lines.append("aliases that would be normalized: "
                     + ", ".join(f"{k} -> {v}" for k, v in sorted(aliases.items())))
    lines.append("action: file under an existing room, or add a mapping to "
                 f"{POLICY_PATH} after human review.")
    return "\n".join(lines)


def _wing_error(wing, data):
    return "\n".join([
        "MEMPALACE VALIDATOR: write rejected (unknown wing)",
        f"offending value: {wing!r}",
        "known wings: " + ", ".join(sorted(data.get("known_wings", []))),
        f"agent diary wings must match: {data.get('agent_wing_pattern')}",
        "action: new project wings require a real project identity; add the "
        f"wing to {POLICY_PATH} after human review.",
    ])


def validate_call(name, args, data):
    """Validate one write-tool call.

    Returns (ok, new_args, report_entries, error_text).
    report_entries: list of (action, field_dict) to record when forwarded.
    data=None (policy unreadable) fails open: allow, no rewrites.
    """
    args = dict(args or {})
    if data is None:
        return True, args, [], None
    entries = []

    if name == "mempalace_add_drawer":
        rv, newroom = decide_room(args.get("room"), data)
        if rv == "reject":
            return False, None, None, _room_error(args.get("room"), data)
        if rv == "normalized":
            entries.append(("normalized", {"field": "room", "from": args["room"], "to": newroom}))
            args["room"] = newroom
        wv, _w = decide_wing(args.get("wing"), data)
        if wv == "reject":
            return False, None, None, _wing_error(args.get("wing"), data)
        if wv == "agent-auto":
            entries.append(("allowed", {"field": "wing", "note": "agent wing auto-allowed"}))
        return True, args, entries, None

    if name == "mempalace_update_drawer":
        if args.get("room"):
            rv, newroom = decide_room(args["room"], data)
            if rv == "reject":
                return False, None, None, _room_error(args["room"], data)
            if rv == "normalized":
                entries.append(("normalized", {"field": "room", "from": args["room"], "to": newroom}))
                args["room"] = newroom
        if args.get("wing"):
            wv, _w = decide_wing(args["wing"], data)
            if wv == "reject":
                return False, None, None, _wing_error(args["wing"], data)
            if wv == "agent-auto":
                entries.append(("allowed", {"field": "wing", "note": "agent wing auto-allowed"}))
        return True, args, entries, None

    if name == "mempalace_checkpoint":
        for i, item in enumerate(args.get("items") or []):
            rv, newroom = decide_room(item.get("room"), data)
            if rv == "reject":
                return False, None, None, (
                    f"MEMPALACE VALIDATOR: write rejected (checkpoint item {i} unknown room)\n"
                    + _room_error(item.get("room"), data))
            if rv == "normalized":
                entries.append(("normalized", {"field": f"items[{i}].room",
                                               "from": item["room"], "to": newroom}))
                item["room"] = newroom
            wv, _w = decide_wing(item.get("wing"), data)
            if wv == "reject":
                return False, None, None, (
                    f"MEMPALACE VALIDATOR: write rejected (checkpoint item {i} unknown wing)\n"
                    + _wing_error(item.get("wing"), data))
        diary = args.get("diary") or {}
        if diary.get("wing"):
            wv, _w = decide_wing(diary["wing"], data)
            if wv == "reject":
                return False, None, None, (
                    "MEMPALACE VALIDATOR: write rejected (diary wing unknown)\n"
                    + _wing_error(diary["wing"], data))
        return True, args, entries, None

    if name == "mempalace_diary_write":
        if args.get("wing"):
            wv, _w = decide_wing(args["wing"], data)
            if wv == "reject":
                return False, None, None, _wing_error(args["wing"], data)
        agent = args.get("agent_name")
        if not agent or not AGENT_NAME_RE.fullmatch(agent):
            return False, None, None, "\n".join([
                "MEMPALACE VALIDATOR: write rejected (invalid agent_name)",
                f"offending value: {agent!r}",
                "agent names must be lowercase [a-z0-9_-] (they seed wing_<agent>).",
                "action: use a canonical agent identity, e.g. codex, claude, antigravity.",
            ])
        return True, args, entries, None

    if name == "mempalace_mine":
        if args.get("wing"):
            wv, _w = decide_wing(args["wing"], data)
            if wv == "reject":
                return False, None, None, _wing_error(args["wing"], data)
        else:
            entries.append(("allowed", {"field": "wing",
                                        "note": "mine: wing derived from source dir, not pre-validated"}))
        return True, args, entries, None

    if name == "mempalace_create_tunnel":
        for field in ("source_wing", "target_wing"):
            wv, _w = decide_wing(args.get(field), data)
            if wv == "reject":
                return False, None, None, f"{field}\n" + _wing_error(args.get(field), data)
        for field in ("source_room", "target_room"):
            rv, _r = decide_room(args.get(field), data)
            if rv == "reject":
                return False, None, None, f"{field}\n" + _room_error(args.get(field), data)
        return True, args, entries, None

    return True, args, entries, None  # passthrough tools


WRITE_TOOLS = {
    "mempalace_add_drawer", "mempalace_update_drawer", "mempalace_checkpoint",
    "mempalace_diary_write", "mempalace_mine", "mempalace_create_tunnel",
}


def check_duplicate_upstream(upstream_url, session_id, content, threshold):
    """Call upstream mempalace_check_duplicate. Returns (is_dup, matches, err)."""
    payload = {
        "jsonrpc": "2.0", "id": -1, "method": "tools/call",
        "params": {"name": "mempalace_check_duplicate",
                   "arguments": {"content": content, "threshold": threshold}},
    }
    req = urllib.request.Request(
        upstream_url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 **({"Mcp-Session-Id": session_id} if session_id else {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", "replace")
    if "data: " in body:
        for line in body.splitlines():
            if line.startswith("data: "):
                body = line[6:]
                break
    parsed = json.loads(body)
    for c in parsed.get("result", {}).get("content", []):
        if c.get("type") == "text":
            data = json.loads(c["text"])
            return bool(data.get("is_duplicate")), data.get("matches") or [], None
    return False, [], None


# ---------- HTTP proxy ----------

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "mempalace-validator/0.1"
    upstream_url = "http://127.0.0.1:8765/mcp"
    upstream = "http://127.0.0.1:8765"
    client_names = {}   # issued session id -> client name
    _name_lock = threading.Lock()

    def log_message(self, fmt, *args):  # quiet; we log to JSONL ourselves
        pass

    def _headers_for_upstream(self):
        out = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection"):
                continue
            if k.lower() == "mcp-session-id" and v in ProxyHandler.client_names:
                continue  # our own issued id: the upstream does not use sessions
            out[k] = v
        return out

    def _send(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in ("transfer-encoding", "content-length", "connection",
                             "server", "date"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward_post(self, body, tool_name):
        t0 = time.time()
        session = self.headers.get("Mcp-Session-Id")
        client = "unknown"
        with ProxyHandler._name_lock:
            client = ProxyHandler.client_names.get(session, "unknown")
        reqlog(client=client, path=self.path, tool=tool_name)

        if tool_name in WRITE_TOOLS:
            try:
                data = POLICY.get()
                ok, new_args, entries, err = validate_call(tool_name, self._tool_args, data)
            except Exception as e:
                report("engine-error", client=client, tool=tool_name, reason=str(e))
                ok, new_args, entries, err = True, self._tool_args, [], None
            if not ok:
                for action, field in (entries or []):
                    report(action, client=client, tool=tool_name,
                           drawer_id=self._tool_args.get("drawer_id"), **field)
                report("rejected", client=client, tool=tool_name,
                       drawer_id=self._tool_args.get("drawer_id"), reason=err.splitlines()[0])
                self._respond_tool_error(self._json_id, err)
                return
            body = self._rebuild_body(new_args)
            if tool_name == "mempalace_add_drawer":
                try:
                    data = POLICY.get() or {}
                    is_dup, matches, derr = check_duplicate_upstream(
                        self.upstream_url, session, new_args.get("content", ""),
                        data.get("duplicate_threshold", 0.9))
                    if derr:
                        report("engine-error", client=client, tool=tool_name,
                               reason=f"dedup check failed: {derr}")
                    elif is_dup:
                        ids = [m.get("id") for m in matches]
                        report("duplicate-block", client=client, tool=tool_name,
                               duplicates=ids,
                               similarities=[m.get("similarity") for m in matches])
                        self._respond_tool_error(self._json_id, "\n".join([
                            "MEMPALACE VALIDATOR: write blocked (near-duplicate content)",
                            "existing drawers: " + ", ".join(str(i) for i in ids),
                            f"policy: hard block on add_drawer near-duplicates "
                            f"(threshold {data.get('duplicate_threshold', 0.9)})",
                            "action: if this filing is intentional (an updated record), "
                            "use mempalace_update_drawer on the existing drawer instead.",
                        ]))
                        return
                except Exception as e:
                    report("engine-error", client=client, tool=tool_name,
                           reason=f"dedup check exception: {e}")
            for action, field in (entries or []):
                report(action, client=client, tool=tool_name,
                       drawer_id=new_args.get("drawer_id"), **field)
            report("allowed", client=client, tool=tool_name,
                   drawer_id=new_args.get("drawer_id"),
                   room=new_args.get("room"), wing=new_args.get("wing"))

        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(self.upstream_url, data=body,
                                       headers=self._headers_for_upstream(),
                                       method="POST"), timeout=120)
            status, rheaders, rbody = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            status, rheaders, rbody = e.code, dict(e.headers), e.read()
        except Exception as e:
            report("engine-error", client=client, tool=tool_name,
                   reason=f"upstream unreachable: {e}")
            self._send(502, {"Content-Type": "application/json"},
                       json.dumps({"jsonrpc": "2.0", "id": self._json_id,
                                   "error": {"code": -32000,
                                             "message": f"upstream unreachable: {e}"}}).encode())
            return
        if self.path == "/mcp":
            incoming_sid = self.headers.get("Mcp-Session-Id")
            if incoming_sid is None and self._client_info:
                # Upstream issues no session id; we mint one so the client's
                # subsequent requests carry it back for attribution.
                import uuid
                sid = uuid.uuid4().hex
                with ProxyHandler._name_lock:
                    ProxyHandler.client_names[sid] = self._client_info
                rheaders["Mcp-Session-Id"] = sid
        self._send(status, rheaders, rbody)
        reqlog(client=client, path=self.path, tool=tool_name,
               upstream_status=status, ms=round((time.time() - t0) * 1000))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._json_id = None
        self._tool_args = None
        self._client_info = None
        payload = {}
        try:
            payload = json.loads(body) if body else {}
            self._json_id = payload.get("id")
            if payload.get("method") == "tools/call":
                self._tool_args = payload.get("params", {}).get("arguments")
            ci = (payload.get("params") or {}).get("clientInfo") or {}
            self._client_info = ci.get("name")
        except Exception:
            pass
        tool_name = (payload.get("params") or {}).get("name") \
            if payload.get("method") == "tools/call" else None
        self._current_tool = tool_name
        if self.path == "/mcp" and tool_name:
            self._forward_post(body, tool_name)
        else:
            self._forward_post(body, None)

    def _rebuild_body(self, new_args):
        return json.dumps({
            "jsonrpc": "2.0", "id": self._json_id, "method": "tools/call",
            "params": {"name": self._tool_name_for(new_args),
                       "arguments": new_args},
        }).encode()

    def _tool_name_for(self, args):
        return self._current_tool

    def _respond_tool_error(self, json_id, text):
        body = json.dumps({
            "jsonrpc": "2.0", "id": json_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }).encode()
        self._send(200, {"Content-Type": "application/json"}, body)

    def do_GET(self):
        # optional server->client SSE stream; stream through without buffering
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(self.upstream_url, method="GET",
                                       headers=self._headers_for_upstream()), timeout=300)
            self.send_response(resp.status)
            for k, v in dict(resp.headers).items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except Exception as e:
            report("engine-error", reason=f"GET proxy failed: {e}")
            try:
                self._send(502, {"Content-Type": "text/plain"}, str(e).encode())
            except Exception:
                pass

    def do_DELETE(self):
        # session termination: pass through
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(self.upstream_url, method="DELETE",
                                       headers=self._headers_for_upstream()), timeout=30)
            self._send(resp.status, dict(resp.headers), resp.read())
        except Exception as e:
            self._send(502, {"Content-Type": "text/plain"}, str(e).encode())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--upstream", default="http://127.0.0.1:8765/mcp")
    args = ap.parse_args()
    ProxyHandler.upstream_url = args.upstream
    ProxyHandler.upstream = args.upstream.rsplit("/mcp", 1)[0]
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    report("started", tool="validator", note=f"listening on {args.host}:{args.port}")
    print(f"mempalace-validator listening on {args.host}:{args.port} "
          f"(upstream {args.upstream})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
