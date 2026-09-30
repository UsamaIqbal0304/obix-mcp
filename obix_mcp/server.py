"""server.py — the MCP server: an agent's view of a Niagara station over oBIX.

Transport is JSON-RPC 2.0 over stdio, framed by newlines, which is what MCP
stdio servers speak. It is implemented here rather than taken from a package
for the same reason the protocol client is: this process is going to be pointed
at somebody's building, and a reviewer should be able to read all of it.

The shape of the tool surface is a deliberate argument about what an agent
should be able to do to a control system:

  * Reading is free. Walking a lobby, reading a point and querying a history
    cannot move plant.
  * Writing is off unless it was turned on at start-up, and then only inside an
    allowlist of href prefixes. Two gates, because "this deployment may write"
    and "this agent may write *here*" are different decisions.
  * There is no tool that writes a batch, and no tool that takes a raw URL and
    a raw body. An agent that can construct arbitrary requests has the whole
    station, allowlist or not.
  * Watches are leased and the lease is short, so the server renews them rather
    than leaving an agent to discover its subscription expired while it thought.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .obix import LOBBY_CHILDREN, SERVLET_PATH, ObixClient, ObixError
from .watch import Watch, changes_to_rows, make_watch, safe_interval

NAME = "obix-mcp"
VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# One watch per session unless an agent asks for more. A JACE-class controller
# is not a server, and a tool that lets an agent open watches in a loop is a
# tool that takes a building's controller down. EVIDENCE.md §J records that
# Tridium documents no maximum, which is a reason to pick one here rather than
# a reason not to.
MAX_WATCHES = 4
MAX_HREFS_PER_WATCH = 200


class Bridge:
    def __init__(self, client: ObixClient):
        self.client = client
        self.watches: dict[str, Watch] = {}

    # ------------------------------------------------------------------ tools

    def tool_about(self) -> dict:
        obj = self.client.about()
        out = {c.name: c.value() for c in obj.children if c.name}
        return {"station": out, "href": obj.href or SERVLET_PATH + "/about/"}

    def tool_lobby(self) -> dict:
        obj = self.client.lobby()
        children = [{"name": c.name, "href": c.href, "contract": c.contract,
                     "display": c.display or c.display_name}
                    for c in obj.children if c.name or c.href]
        present = {c["name"] for c in children}
        # Naming what is missing is more useful than listing what is there: a
        # lobby without `histories` means the station has no history feed
        # exposed, and an agent that does not know that will keep asking.
        absent = [n for n in LOBBY_CHILDREN if n not in present]
        return {"children": children, "documented_but_absent": absent,
                "note": ("The export branch is mounted as 'continuousControl', not "
                         "'export' — that is the station's own name for it.")}

    def tool_read(self, href: str, depth: int = 2) -> dict:
        return self.client.read(href).as_dict(depth=max(0, min(int(depth), 6)))

    def tool_write(self, href: str, type: str, value) -> dict:
        if type not in ("bool", "int", "real", "str", "enum", "abstime", "reltime"):
            raise ObixError(f"{type!r} is not an oBIX value type")
        obj = self.client.write(href, type, value)
        return {"written": True, "href": href, "result": obj.as_dict(depth=1)}

    def tool_watch_open(self, hrefs: list[str]) -> dict:
        if len(self.watches) >= MAX_WATCHES:
            raise ObixError(
                f"{MAX_WATCHES} watches are already open, which is this bridge's limit on "
                f"a controller. Close one with obix_watch_close, or add hrefs to an open "
                f"one instead of opening another.")
        if not hrefs:
            raise ObixError("open a watch on at least one href")
        if len(hrefs) > MAX_HREFS_PER_WATCH:
            raise ObixError(f"{len(hrefs)} hrefs is past this bridge's limit of "
                            f"{MAX_HREFS_PER_WATCH} per watch")
        watch = make_watch(self.client)
        first = watch.add(hrefs)
        self.watches[watch.href] = watch
        return {"watch": watch.href, "lease_seconds": watch.lease_seconds,
                "registered": len(watch.hrefs),
                "first_reading": changes_to_rows(first)}

    def tool_watch_poll(self, watch: str, refresh: bool = False) -> dict:
        w = self._watch(watch)
        out = w.poll_refresh() if refresh else w.poll_changes()
        return {"watch": w.href, "refresh": bool(refresh),
                "seconds_until_expiry": round(w.seconds_until_expiry, 1),
                "changes": changes_to_rows(out)}

    def tool_watch_follow(self, watch: str, seconds: float, interval: float = None) -> dict:
        """Poll a watch for a while and return every change seen.

        An agent that wants to know whether a set point actually moved the plant
        has to wait, and the alternative to this tool is the agent calling poll
        in a loop as fast as the model can emit tokens. The interval is clamped
        to Tridium's own recommendation for a controller."""
        w = self._watch(watch)
        step = safe_interval(interval)
        seconds = max(step, min(float(seconds), 300.0))
        deadline = time.monotonic() + seconds
        polls, rows = 0, []
        while True:
            out = w.poll_changes()
            polls += 1
            rows.extend(changes_to_rows(out))
            if time.monotonic() + step > deadline:
                break
            time.sleep(step)
        return {"watch": w.href, "polls": polls, "interval_seconds": step,
                "seconds": round(seconds, 1), "changes": rows}

    def tool_watch_close(self, watch: str) -> dict:
        w = self._watch(watch)
        w.delete()
        self.watches.pop(w.href, None)
        return {"closed": w.href, "open_watches": len(self.watches)}

    def tool_history(self, href: str, query: str = "") -> dict:
        """Read a history feed, optionally through one of the station's own
        canned queries.

        Tridium documents that histories are exposed to oBIX clients "through a
        predefined set of query options, available as Lobby URIs using the HTTP
        GET mechanism (without an oBIX op)" and names Today, Last 24 Hours and
        Yesterday among them. It does not publish the URL template, so this
        tool asks the station: it reads the feed object and uses the href of the
        child whose name or display matches, rather than guessing a path.
        EVIDENCE.md §G"""
        feed = self.client.read(href)
        if not query:
            return {"href": href, "feed": feed.as_dict(depth=2),
                    "queries": [c.name or c.display for c in feed.children
                                if c.tag == "op" or c.name]}
        want = query.strip().lower().replace(" ", "")
        for c in feed.children:
            for label in (c.name, c.display, c.display_name):
                if label and label.strip().lower().replace(" ", "") == want:
                    if not c.href:
                        raise ObixError(f"the station's {label!r} query has no href")
                    return {"href": c.href, "query": label,
                            "result": self.client.read(c.href).as_dict(depth=3)}
        available = [c.name or c.display for c in feed.children if c.name or c.display]
        raise ObixError(
            f"{query!r} is not a query this feed offers. It offers: "
            f"{', '.join(available) or '(none listed)'}. These are the station's own "
            f"canned queries; this bridge does not construct history URLs itself.")

    def _watch(self, href: str) -> Watch:
        w = self.watches.get(href)
        if w is None:
            raise ObixError(f"no open watch {href!r}. Open ones: "
                            f"{', '.join(self.watches) or '(none)'}")
        return w

    def close(self):
        for w in list(self.watches.values()):
            try:
                w.delete()
            except ObixError:
                pass
        self.watches.clear()


# --------------------------------------------------------------- tool schemas

def tool_schemas(writes_enabled: bool) -> list[dict]:
    tools = [
        dict(name="obix_about",
             description="Read the station's oBIX about object: product name, version, "
                         "vendor and server time. The cheapest way to confirm the bridge "
                         "is talking to the station you think it is.",
             inputSchema={"type": "object", "properties": {}}),
        dict(name="obix_lobby",
             description="List the top level of the station's oBIX tree, and name the "
                         "documented branches that are absent. Start here: every other "
                         "href comes from a walk that starts at the lobby.",
             inputSchema={"type": "object", "properties": {}}),
        dict(name="obix_read",
             description="Read one oBIX object by href — a folder, a point, a schedule. "
                         "Hrefs are relative to " + SERVLET_PATH + " and come from the "
                         "lobby or from a previous read; this tool will not follow one "
                         "off-station.",
             inputSchema={"type": "object", "required": ["href"], "properties": {
                 "href": {"type": "string",
                          "description": "e.g. 'config/Drivers/' or 'about/'"},
                 "depth": {"type": "integer", "minimum": 0, "maximum": 6, "default": 2,
                           "description": "How many levels of children to include. "
                                          "Deep reads on a controller are expensive."}}}),
        dict(name="obix_watch_open",
             description="Open a server-side watch on a set of hrefs and return the first "
                         "reading of each. A watch is how you follow changing values "
                         "without re-reading points; it is leased for about 30 seconds, "
                         "so poll it or it is collected.",
             inputSchema={"type": "object", "required": ["hrefs"], "properties": {
                 "hrefs": {"type": "array", "items": {"type": "string"},
                           "maxItems": MAX_HREFS_PER_WATCH}}}),
        dict(name="obix_watch_poll",
             description="Poll an open watch once. Returns only what changed, unless "
                         "refresh is true, which returns every registered value and costs "
                         "the station more.",
             inputSchema={"type": "object", "required": ["watch"], "properties": {
                 "watch": {"type": "string", "description": "the watch href from obix_watch_open"},
                 "refresh": {"type": "boolean", "default": False}}}),
        dict(name="obix_watch_follow",
             description="Poll an open watch for a number of seconds and return every "
                         "change seen. Use this instead of calling obix_watch_poll in a "
                         "loop: the interval is clamped to the vendor's own recommended "
                         "floor for a controller.",
             inputSchema={"type": "object", "required": ["watch", "seconds"], "properties": {
                 "watch": {"type": "string"},
                 "seconds": {"type": "number", "minimum": 1, "maximum": 300},
                 "interval": {"type": "number", "minimum": 10,
                              "description": "seconds between polls; values below 10 are "
                                             "raised to 10"}}}),
        dict(name="obix_watch_close",
             description="Delete a watch on the station. Do this when finished rather than "
                         "letting the lease lapse.",
             inputSchema={"type": "object", "required": ["watch"], "properties": {
                 "watch": {"type": "string"}}}),
        dict(name="obix_history",
             description="Read a history feed under the histories branch. Called without a "
                         "query it lists the queries the station itself offers (Today, Last "
                         "24 Hours, and so on); called with one it follows that query's own "
                         "href. This bridge never constructs a history URL.",
             inputSchema={"type": "object", "required": ["href"], "properties": {
                 "href": {"type": "string"},
                 "query": {"type": "string",
                           "description": "one of the names the station listed"}}}),
    ]
    if writes_enabled:
        tools.append(dict(
            name="obix_write",
            description="Write a value to a writable point, inside the allowlist this "
                        "bridge was started with. The type is the oBIX element name and "
                        "must match the point: writing a numeric set point needs 'real'. "
                        "This moves plant.",
            inputSchema={"type": "object", "required": ["href", "type", "value"],
                         "properties": {
                             "href": {"type": "string"},
                             "type": {"type": "string",
                                      "enum": ["bool", "int", "real", "str", "enum",
                                               "abstime", "reltime"]},
                             "value": {"description": "bool, number or string, matching type"}}}))
    return tools


# ------------------------------------------------------------------ transport

def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Stdio:
    def __init__(self, bridge: Bridge, writes_enabled: bool):
        self.bridge = bridge
        self.writes_enabled = writes_enabled

    def run(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()

    def handle(self, msg: dict):
        method, mid = msg.get("method"), msg.get("id")
        # A notification has no id and takes no reply. Answering one is a
        # protocol error that shows up as a confusing client-side warning.
        if mid is None:
            return None
        try:
            if method == "initialize":
                return self._ok(mid, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": NAME, "version": VERSION},
                })
            if method == "tools/list":
                return self._ok(mid, {"tools": tool_schemas(self.writes_enabled)})
            if method == "tools/call":
                params = msg.get("params") or {}
                return self._ok(mid, self.call(params.get("name", ""),
                                               params.get("arguments") or {}))
            if method == "ping":
                return self._ok(mid, {})
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown method {method}"}}
        except Exception as exc:  # a crash here kills the session; a message does not
            return self._ok(mid, _result(f"{type(exc).__name__}: {exc}", is_error=True))

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name: str, args: dict) -> dict:
        fn = {
            "obix_about": lambda: self.bridge.tool_about(),
            "obix_lobby": lambda: self.bridge.tool_lobby(),
            "obix_read": lambda: self.bridge.tool_read(args.get("href", ""),
                                                       args.get("depth", 2)),
            "obix_write": lambda: self.bridge.tool_write(args.get("href", ""),
                                                         args.get("type", ""),
                                                         args.get("value")),
            "obix_watch_open": lambda: self.bridge.tool_watch_open(args.get("hrefs") or []),
            "obix_watch_poll": lambda: self.bridge.tool_watch_poll(
                args.get("watch", ""), bool(args.get("refresh"))),
            "obix_watch_follow": lambda: self.bridge.tool_watch_follow(
                args.get("watch", ""), args.get("seconds", 30), args.get("interval")),
            "obix_watch_close": lambda: self.bridge.tool_watch_close(args.get("watch", "")),
            "obix_history": lambda: self.bridge.tool_history(args.get("href", ""),
                                                             args.get("query", "")),
        }.get(name)
        if fn is None:
            return _result(f"no tool named {name!r}", is_error=True)
        if name == "obix_write" and not self.writes_enabled:
            return _result("This bridge is read-only. Writes are enabled at start-up with "
                           "--allow-write and an --write-allow prefix, deliberately.",
                           is_error=True)
        try:
            return _result(json.dumps(fn(), indent=1, default=str))
        except ObixError as exc:
            return _result(str(exc), is_error=True)


# ----------------------------------------------------------------------- main

def build_client(argv=None) -> tuple[ObixClient, argparse.Namespace]:
    ap = argparse.ArgumentParser(
        prog="obix-mcp",
        description="An MCP server that reads a Niagara station over oBIX.")
    ap.add_argument("--station", default=os.environ.get("OBIX_STATION", ""),
                    help="host, or http(s)://host:port. The oBIX path is fixed at "
                         + SERVLET_PATH + " by the station itself.")
    ap.add_argument("--user", default=os.environ.get("OBIX_USER", ""),
                    help="station user. Prefer OBIX_USER in the environment.")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--dump", action="store_true",
                    help="print every request and reply to stderr. The bodies this tool "
                         "sends for watch operations are derived from the oBIX "
                         "specification, not from Tridium's documentation, which shows "
                         "none — this is how you check them against a real station. "
                         "Credentials are never printed.")
    ap.add_argument("--insecure", action="store_true",
                    help="do not verify the station's TLS certificate. For a JACE still "
                         "carrying its self-signed certificate, and nothing else.")
    ap.add_argument("--allow-write", action="store_true",
                    help="permit writes at all. Off by default: a write moves plant.")
    ap.add_argument("--write-allow", action="append", default=[], metavar="HREF_PREFIX",
                    help="an href prefix writes are permitted under. Repeatable. "
                         "Required by --allow-write; there is no allow-everything value.")
    args = ap.parse_args(argv)

    if not args.station:
        ap.error("no station given: pass --station or set OBIX_STATION")
    # The password is read from the environment only. Not a flag, because a flag
    # lands in shell history, in `ps` output and in whatever log wraps the
    # process.
    password = os.environ.get("OBIX_PASSWORD", "")
    if args.user and not password:
        print(f"{NAME}: OBIX_USER is set but OBIX_PASSWORD is empty; the station will "
              f"almost certainly answer 401.", file=sys.stderr)
    if args.allow_write and not args.write_allow:
        ap.error("--allow-write needs at least one --write-allow prefix. Enabling writes "
                 "everywhere is not offered.")
    if args.insecure:
        print(f"{NAME}: TLS verification is off. Anyone on the path between here and the "
              f"station can read the credentials.", file=sys.stderr)

    client = ObixClient(args.station, args.user, password, timeout=args.timeout,
                        verify_tls=not args.insecure, allow_write=args.allow_write,
                        write_allowlist=args.write_allow,
                        trace=_stderr_trace if args.dump else None)
    return client, args


def _stderr_trace(method, url, sent, got):
    """stderr, not stdout: stdout is the JSON-RPC channel and a stray line on it
    ends the session. Prints no headers, so the Authorization header cannot
    reach a log through here."""
    print(f"--> {method} {url}" + (f"\n{sent}" if sent else ""), file=sys.stderr)
    print(f"<-- {got}", file=sys.stderr)


def main(argv=None) -> int:
    client, args = build_client(argv)
    bridge = Bridge(client)
    try:
        Stdio(bridge, writes_enabled=args.allow_write).run()
    finally:
        bridge.close()
    return 0
