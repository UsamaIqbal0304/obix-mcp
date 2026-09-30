"""Tests for the MCP layer: the tool surface, the JSON-RPC framing, and the
gates that decide what an agent is allowed to do to a building.

The protocol client is tested in test_obix.py. What is tested here is the part a
reviewer of this repo actually has to trust: that a read-only bridge cannot be
talked into a write, that a watch is closed rather than leaked, that one bad
call returns an error to the model instead of killing the session, and that the
poll floor cannot be argued below the vendor's own recommendation.
"""
import base64
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obix_mcp.obix import ObixClient, ObixError
from obix_mcp.server import (MAX_HREFS_PER_WATCH, MAX_WATCHES, NAME, PROTOCOL_VERSION,
                             Bridge, Stdio, build_client, tool_schemas)
from tests import fixture_station

TEMP = "config/AHU-01/SupplyAirTemp/"
SETPOINT = "config/AHU-01/ZoneSetpoint/"
MODE = "config/AHU-01/Mode/"


class Base(unittest.TestCase):
    def setUp(self):
        self.base, self.station, stop = fixture_station.serve("ok", False)
        self.addCleanup(stop)

    def bridge(self, **kw):
        kw.setdefault("user", "admin")
        kw.setdefault("password", "x")
        b = Bridge(ObixClient(self.base, **kw))
        self.addCleanup(b.close)
        return b

    def stdio(self, writes_enabled=False, **kw):
        if writes_enabled:
            kw.setdefault("allow_write", True)
            kw.setdefault("write_allowlist", ["config/"])
        return Stdio(self.bridge(**kw), writes_enabled=writes_enabled)

    def call(self, io_, name, **args):
        """One tools/call, unwrapped to (payload_or_text, is_error)."""
        reply = io_.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
        res = reply["result"]
        text = res["content"][0]["text"]
        if res.get("isError"):
            return text, True
        return json.loads(text), False


class TestToolSurface(unittest.TestCase):
    def test_write_tool_is_absent_entirely_when_writes_are_off(self):
        # Not merely refused when called: a tool an agent cannot see is a tool
        # it will not spend a turn arguing with.
        names = [t["name"] for t in tool_schemas(False)]
        self.assertNotIn("obix_write", names)
        self.assertIn("obix_read", names)

    def test_write_tool_appears_when_writes_are_on(self):
        self.assertIn("obix_write", [t["name"] for t in tool_schemas(True)])

    def test_every_tool_has_a_description_and_a_schema(self):
        for t in tool_schemas(True):
            self.assertTrue(t["description"].strip(), t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            for req in t["inputSchema"].get("required", []):
                self.assertIn(req, t["inputSchema"]["properties"], t["name"])

    def test_no_tool_takes_a_raw_url_or_a_raw_body(self):
        # The whole point of the allowlist is lost if an agent can hand the
        # bridge a URL and a payload.
        for t in tool_schemas(True):
            for prop in t["inputSchema"].get("properties", {}):
                self.assertNotIn(prop, ("url", "uri", "body", "xml", "path"), t["name"])

    def test_the_watch_cap_is_stated_in_the_schema(self):
        schema = [t for t in tool_schemas(False) if t["name"] == "obix_watch_open"][0]
        self.assertEqual(schema["inputSchema"]["properties"]["hrefs"]["maxItems"],
                         MAX_HREFS_PER_WATCH)


class TestHandshake(Base):
    def test_initialize_answers_with_the_protocol_version_and_name(self):
        io_ = self.stdio()
        res = io_.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {}})["result"]
        self.assertEqual(res["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(res["serverInfo"]["name"], NAME)
        self.assertIn("tools", res["capabilities"])

    def test_a_notification_gets_no_reply(self):
        # Answering a message with no id is a protocol error, and clients log it
        # as one.
        self.assertIsNone(self.stdio().handle({"jsonrpc": "2.0",
                                               "method": "notifications/initialized"}))

    def test_unknown_method_is_a_jsonrpc_error_not_a_crash(self):
        reply = self.stdio().handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_unknown_tool_is_an_error_to_the_model(self):
        text, err = self.call(self.stdio(), "obix_teleport")
        self.assertTrue(err)
        self.assertIn("obix_teleport", text)

    def test_a_station_level_failure_comes_back_as_a_tool_error(self):
        # An exception escaping here would end the session, and the agent would
        # see a dead server rather than a station that said no.
        text, err = self.call(self.stdio(), "obix_read", href="config/NoSuchThing/")
        self.assertTrue(err)
        self.assertIn("BadUriErr", text)

    def test_a_line_of_jsonrpc_in_gets_a_line_of_json_out(self):
        io_ = self.stdio()
        out = io.StringIO()
        io_.run(stdin=io.StringIO(
            '\n'
            'not json at all\n'
            '{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
            '{"jsonrpc":"2.0","method":"notifications/cancelled"}\n'), stdout=out)
        lines = [l for l in out.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 1, out.getvalue())
        self.assertEqual(json.loads(lines[0])["id"], 9)


class TestReads(Base):
    def test_about_names_the_station(self):
        payload, err = self.call(self.stdio(), "obix_about")
        self.assertFalse(err)
        self.assertEqual(payload["station"]["productName"], "oBIX fixture station")

    def test_lobby_reports_the_branch_name_that_surprises_people(self):
        payload, err = self.call(self.stdio(), "obix_lobby")
        self.assertFalse(err)
        self.assertIn("continuousControl", [c["name"] for c in payload["children"]])
        self.assertIn("continuousControl", payload["note"])

    def test_lobby_names_documented_branches_the_station_lacks(self):
        # A station with no histories branch is a station with no history feed
        # exposed. Saying so once saves an agent asking for one repeatedly.
        full = self.station.lobby_xml()
        self.station.lobby_xml = lambda: full.replace(
            '<ref name="histories" href="histories/"/>', "")
        payload, err = self.call(self.stdio(), "obix_lobby")
        self.assertFalse(err, payload)
        self.assertEqual(payload["documented_but_absent"], ["histories"])
        # The unlisted branch is reported as unlisted, not as missing: it is
        # absent from every station's lobby, including a complete one.
        self.assertEqual(payload["unlisted_branches"], ["alarm"])

    def test_a_complete_lobby_reports_nothing_absent(self):
        payload, err = self.call(self.stdio(), "obix_lobby")
        self.assertFalse(err, payload)
        self.assertEqual(payload["documented_but_absent"], [])

    def test_read_depth_is_clamped_rather_than_refused(self):
        payload, err = self.call(self.stdio(), "obix_read", href="about/", depth=99)
        self.assertFalse(err)
        self.assertTrue(payload["children"])

    def test_read_will_not_follow_an_href_off_the_servlet(self):
        text, err = self.call(self.stdio(), "obix_read", href="/prototypes/")
        self.assertTrue(err)


class TestWriteGates(Base):
    def test_read_only_bridge_refuses_and_says_how_writes_are_enabled(self):
        text, err = self.call(self.stdio(), "obix_write", href=SETPOINT,
                              type="real", value=22.0)
        self.assertTrue(err)
        self.assertIn("--allow-write", text)
        self.assertEqual(self.station.writes, [])

    def test_a_write_outside_the_allowlist_is_refused(self):
        io_ = self.stdio(writes_enabled=True, allow_write=True,
                         write_allowlist=["config/Boiler/"])
        text, err = self.call(io_, "obix_write", href=SETPOINT, type="real", value=22.0)
        self.assertTrue(err)
        self.assertEqual(self.station.writes, [])

    def test_a_write_inside_the_allowlist_reaches_the_station(self):
        io_ = self.stdio(writes_enabled=True)
        payload, err = self.call(io_, "obix_write", href=SETPOINT, type="real", value=22.5)
        self.assertFalse(err, payload)
        self.assertEqual(self.station.writes, [(SETPOINT, "real", 22.5)])

    def test_a_type_oBIX_does_not_have_is_refused_before_the_request(self):
        io_ = self.stdio(writes_enabled=True)
        text, err = self.call(io_, "obix_write", href=SETPOINT, type="float", value=1)
        self.assertTrue(err)
        self.assertIn("float", text)
        self.assertEqual(self.station.writes, [])

    def test_writing_a_read_only_point_reports_the_stations_permission_error(self):
        io_ = self.stdio(writes_enabled=True)
        text, err = self.call(io_, "obix_write", href=TEMP, type="real", value=5.0)
        self.assertTrue(err)
        self.assertEqual(self.station.writes, [])


class TestWatches(Base):
    def test_open_poll_close_round_trip(self):
        io_ = self.stdio()
        opened, err = self.call(io_, "obix_watch_open", hrefs=[TEMP, MODE])
        self.assertFalse(err, opened)
        self.assertEqual(opened["registered"], 2)
        self.assertEqual(opened["lease_seconds"], 30.0)
        self.assertEqual(len(opened["first_reading"]), 2)

        polled, err = self.call(io_, "obix_watch_poll", watch=opened["watch"])
        self.assertFalse(err, polled)
        self.assertLessEqual(polled["seconds_until_expiry"], 30.0)
        self.assertEqual([r["href"] for r in polled["changes"]], ["/obix/" + TEMP])

        refreshed, err = self.call(io_, "obix_watch_poll", watch=opened["watch"],
                                   refresh=True)
        self.assertEqual(len(refreshed["changes"]), 2)

        closed, err = self.call(io_, "obix_watch_close", watch=opened["watch"])
        self.assertEqual(closed["open_watches"], 0)
        self.assertEqual(self.station.watches, {})

    def test_polling_a_watch_that_was_never_opened_lists_the_open_ones(self):
        io_ = self.stdio()
        opened, _ = self.call(io_, "obix_watch_open", hrefs=[MODE])
        text, err = self.call(io_, "obix_watch_poll", watch="/obix/watchService/watch99/")
        self.assertTrue(err)
        self.assertIn(opened["watch"], text)

    def test_the_watch_limit_names_the_way_out_of_it(self):
        io_ = self.stdio()
        for _ in range(MAX_WATCHES):
            _, err = self.call(io_, "obix_watch_open", hrefs=[MODE])
            self.assertFalse(err)
        text, err = self.call(io_, "obix_watch_open", hrefs=[MODE])
        self.assertTrue(err)
        self.assertIn("obix_watch_close", text)

    def test_an_empty_watch_is_refused(self):
        text, err = self.call(self.stdio(), "obix_watch_open", hrefs=[])
        self.assertTrue(err)

    def test_too_many_hrefs_for_one_watch_is_refused_before_the_request(self):
        io_ = self.stdio()
        text, err = self.call(io_, "obix_watch_open",
                              hrefs=[MODE] * (MAX_HREFS_PER_WATCH + 1))
        self.assertTrue(err)
        self.assertEqual(self.station.watches, {})

    def test_follow_clamps_the_interval_to_the_vendors_floor(self):
        # An agent asking for half-second polling on a JACE is asking for an
        # outage. The floor is not negotiable through the tool surface.
        io_ = self.stdio()
        opened, _ = self.call(io_, "obix_watch_open", hrefs=[TEMP])
        payload, err = self.call(io_, "obix_watch_follow", watch=opened["watch"],
                                 seconds=1, interval=0.5)
        self.assertFalse(err, payload)
        self.assertEqual(payload["interval_seconds"], 10.0)
        self.assertEqual(payload["polls"], 1)

    def test_closing_the_bridge_deletes_the_watches_on_the_station(self):
        # A leaked watch costs the controller until its lease lapses.
        b = self.bridge()
        b.tool_watch_open([TEMP])
        b.tool_watch_open([MODE])
        self.assertEqual(len(self.station.watches), 2)
        b.close()
        self.assertEqual(self.station.watches, {})
        self.assertEqual(b.watches, {})


class TestHistory(Base):
    FEED = "histories/AHU-01/SupplyAirTemp/"

    def test_without_a_query_it_lists_the_stations_own_queries(self):
        payload, err = self.call(self.stdio(), "obix_history", href=self.FEED)
        self.assertFalse(err, payload)
        # Tridium's own names, not the prose spelling in the documentation.
        self.assertIn("last24Hours", payload["queries"])
        self.assertIn("yearToDate (limit=1000)", payload["queries"])
        # The four ops are listed as things this tool will not invoke, and one
        # of them writes records into the history.
        self.assertEqual(sorted(payload["ops_this_tool_will_not_invoke"]),
                         ["append", "feed", "query", "rollup"])

    def test_a_query_is_followed_by_the_stations_href_not_a_guessed_one(self):
        payload, err = self.call(self.stdio(), "obix_history", href=self.FEED,
                                 query="last 24 hours")
        self.assertFalse(err, payload)
        self.assertEqual(payload["query"], "last24Hours")
        # The station's href is relative and carries the bounds:
        # `~historyQuery?start=...&end=...`. It resolves under the history, and
        # the query string has to survive both the resolution and the request.
        self.assertTrue(payload["href"].startswith(
            "/obix/histories/AHU-01/SupplyAirTemp/~historyQuery?start="), payload["href"])
        asked = {c["name"]: c["value"] for c in payload["result"]["children"]
                 if c.get("name", "").startswith("asked-")}
        self.assertEqual(sorted(asked), ["asked-end", "asked-start"])

    def test_a_bracketed_name_is_reachable_by_its_leading_word(self):
        payload, err = self.call(self.stdio(), "obix_history", href=self.FEED,
                                 query="yearToDate")
        self.assertFalse(err, payload)
        self.assertEqual(payload["query"], "yearToDate (limit=1000)")
        self.assertIn("limit=1000", payload["href"])

    def test_an_op_cannot_be_reached_by_naming_it_as_a_query(self):
        text, err = self.call(self.stdio(), "obix_history", href=self.FEED,
                              query="append")
        self.assertTrue(err)
        self.assertIn("writes records into the history", text)
        self.assertEqual(self.station.history_appends, [])

    def test_an_unknown_query_lists_what_the_feed_offers(self):
        text, err = self.call(self.stdio(), "obix_history", href=self.FEED,
                              query="last decade")
        self.assertTrue(err)
        self.assertIn("last24Hours", text)
        self.assertIn("does not construct history URLs", text)


class TestStartup(unittest.TestCase):
    """argparse writes usage to stderr and exits; captured so a passing run is
    quiet and the message itself can be asserted."""

    def _refused(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            build_client(argv)
        return err.getvalue()

    def setUp(self):
        for k in ("OBIX_STATION", "OBIX_USER", "OBIX_PASSWORD"):
            self._restore(k)

    def _restore(self, key):
        old = os.environ.pop(key, None)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, key, old)

    def test_no_station_is_an_error_not_a_default(self):
        self.assertIn("OBIX_STATION", self._refused([]))

    def test_writes_need_an_allowlist(self):
        self.assertIn("--write-allow",
                      self._refused(["--station", "jace-1", "--allow-write"]))

    def test_there_is_no_password_flag(self):
        # The password comes from the environment only, so it stays out of shell
        # history and out of `ps`.
        self.assertIn("unrecognized",
                      self._refused(["--station", "jace-1", "--password", "secret"]))

    def test_password_comes_from_the_environment(self):
        os.environ["OBIX_PASSWORD"] = "from-env"
        client, args = build_client(["--station", "jace-1", "--user", "admin"])
        self.assertFalse(args.allow_write)
        self.assertEqual(client.base, "http://jace-1/obix")
        # The client keeps no password attribute; it holds the header it will
        # send and nothing else. Assert both halves of that.
        self.assertEqual(
            base64.b64decode(client._auth.split()[1]).decode(), "admin:from-env")
        plain = [k for k, v in vars(client).items() if v == "from-env"]
        self.assertEqual(plain, [])

    def test_allowlist_reaches_the_client(self):
        os.environ["OBIX_PASSWORD"] = "x"
        client, args = build_client(["--station", "jace-1", "--user", "a",
                                     "--allow-write", "--write-allow", "config/AHU-01/"])
        self.assertTrue(client.allow_write)
        self.assertEqual(client.write_allowlist, ["config/AHU-01/"])


class TestDump(Base):
    def test_dump_shows_the_bodies_and_never_the_credentials(self):
        # --dump exists so the SPEC-DERIVED watch bodies can be checked against
        # a station that disagrees. It must not turn a password into a log line.
        seen = []
        client = ObixClient(self.base, "admin", "hunter2",
                            trace=lambda *a: seen.append(a))
        b = Bridge(client)
        self.addCleanup(b.close)
        b.tool_watch_open([TEMP])
        joined = "\n".join("\n".join(str(x) for x in row) for row in seen)
        self.assertIn("obix:WatchIn", joined)
        self.assertIn("POST", joined)
        self.assertNotIn("hunter2", joined)
        self.assertNotIn("Authorization", joined)

    def test_a_trace_that_raises_does_not_cost_the_caller_the_reply(self):
        def angry(*a):
            raise RuntimeError("no")
        b = Bridge(ObixClient(self.base, "admin", "x", trace=angry))
        self.addCleanup(b.close)
        self.assertEqual(b.tool_about()["station"]["serverName"], "fixture")

    def test_the_dump_flag_wires_the_trace(self):
        os.environ["OBIX_PASSWORD"] = "x"
        self.addCleanup(os.environ.pop, "OBIX_PASSWORD", None)
        plain, _ = build_client(["--station", "jace-1", "--user", "a"])
        dumping, _ = build_client(["--station", "jace-1", "--user", "a", "--dump"])
        self.assertIsNone(plain.trace)
        self.assertIsNotNone(dumping.trace)


if __name__ == "__main__":
    unittest.main()
