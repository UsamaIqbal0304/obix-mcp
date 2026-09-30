"""Tests for the protocol client, against the fixture station.

What these prove and what they do not: they prove the client is internally
consistent and that it behaves the way EVIDENCE.md says a station does. They do
not prove a station agrees, because the fixture is derived from the same
evidence the client is. Where a test depends on a shape the shipped Niagara
documentation never showed — the WatchIn body, above all — the test says
SPEC-DERIVED, so the day a real station disagrees, the tests to re-read are
already marked.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obix_mcp.obix import (LOBBY_CHILDREN, SERVLET_PATH, ObixClient, ObixError,
                           ObixFault, decode, encode_value)
from obix_mcp.watch import _reltime_seconds, changes_to_rows, make_watch, safe_interval
from tests import fixture_station


class Base(unittest.TestCase):
    mode = "ok"
    auth = False

    def setUp(self):
        self.base, self.station, self._stop = fixture_station.serve(self.mode, self.auth)
        self.addCleanup(self._stop)

    def client(self, **kw):
        kw.setdefault("user", "admin")
        kw.setdefault("password", "x")
        return ObixClient(self.base, **kw)


class TestBase(unittest.TestCase):
    def test_base_accepts_bare_host(self):
        self.assertEqual(ObixClient("jace-1")._normalise_base("jace-1"),
                         "http://jace-1" + SERVLET_PATH)

    def test_base_accepts_url_with_port(self):
        c = ObixClient("http://10.0.0.5:8080")
        self.assertEqual(c.base, "http://10.0.0.5:8080/obix")

    def test_base_ignores_a_path_it_was_handed(self):
        # The servlet path is fixed in the station's own bytecode, so honouring
        # a caller's path would imply a station that does not exist.
        self.assertEqual(ObixClient("http://h/station/obix2").base, "http://h/obix")

    def test_href_may_not_leave_the_servlet(self):
        c = ObixClient("http://h")
        with self.assertRaises(ObixError):
            c.url_for("/prototypes/")
        with self.assertRaises(ObixError):
            c.url_for("http://elsewhere/obix/config/")

    def test_href_forms_agree(self):
        c = ObixClient("http://h")
        for form in ("config/x/", "/obix/config/x/", "http://h/obix/config/x/"):
            self.assertEqual(c.url_for(form), "http://h/obix/config/x/", form)


class TestDecode(unittest.TestCase):
    def test_namespaced_and_bare_both_decode(self):
        for doc in ('<real xmlns="http://obix.org/ns/schema/1.0" val="21.5"/>',
                    '<real val="21.5"/>'):
            self.assertEqual(decode(doc).value(), 21.5)

    def test_value_is_typed_by_tag_not_by_looks(self):
        self.assertIs(decode('<bool val="true"/>').value(), True)
        # A str whose val reads "true" is the string, not the boolean. Guessing
        # here is how a set point gets written as the wrong type.
        self.assertEqual(decode('<str val="true"/>').value(), "true")
        self.assertEqual(decode('<int val="7"/>').value(), 7)

    def test_err_raises_a_fault_with_the_stations_words(self):
        with self.assertRaises(ObixFault) as cm:
            decode('<err is="obix:BadUriErr" display="no such object" href="/obix/nope"/>')
        self.assertIn("BadUriErr", str(cm.exception))
        self.assertIn("no such object", str(cm.exception))

    def test_non_xml_is_a_clear_error(self):
        with self.assertRaises(ObixError) as cm:
            decode("<html>login page</html>\x00")
        self.assertIn("not XML", str(cm.exception))

    def test_encode_escapes_the_value(self):
        out = encode_value("str", 'a "quoted" & <angled> value').decode()
        self.assertIn("&quot;", out)
        self.assertIn("&amp;", out)
        self.assertNotIn("<angled>", out)


class TestRead(Base):
    def test_lobby_lists_every_documented_child(self):
        kids = {c.name for c in self.client().lobby().children}
        for name in LOBBY_CHILDREN:
            self.assertIn(name, kids, name)

    def test_export_branch_is_named_continuous_control(self):
        # The single most surprising thing in the evidence, and the one an agent
        # would otherwise get wrong on its first walk.
        kids = {c.name for c in self.client().lobby().children}
        self.assertIn("continuousControl", kids)
        self.assertNotIn("export", kids)

    def test_about_carries_a_product_name(self):
        about = self.client().about()
        self.assertEqual(about.child("productName").value(), "oBIX fixture station")

    def test_read_a_point(self):
        p = self.client().read("config/AHU-01/SupplyAirTemp/")
        self.assertEqual(p.tag, "real")
        self.assertEqual(p.value(), 13.4)
        self.assertEqual(p.unit, "obix:units/celsius")
        self.assertFalse(p.writable)

    def test_missing_object_is_a_fault_not_a_crash(self):
        with self.assertRaises(ObixError) as cm:
            self.client().read("config/NoSuchThing/")
        self.assertIn("BadUriErr", str(cm.exception))

    def test_reply_larger_than_the_cap_is_refused_with_advice(self):
        c = self.client(max_bytes=10)
        with self.assertRaises(ObixError) as cm:
            c.lobby()
        self.assertIn("read a child href", str(cm.exception))


class TestWrite(Base):
    def test_write_is_off_by_default(self):
        with self.assertRaises(ObixError) as cm:
            self.client().write("config/AHU-01/ZoneSetpoint/", "real", 22.0)
        self.assertIn("read-only", str(cm.exception))

    def test_write_needs_the_href_in_the_allowlist(self):
        c = self.client(allow_write=True, write_allowlist=["config/AHU-01/ZoneSetpoint/"])
        c.write("config/AHU-01/ZoneSetpoint/", "real", 22.0)
        self.assertEqual(self.station.points["config/AHU-01/ZoneSetpoint/"][1], 22.0)
        with self.assertRaises(ObixError) as cm:
            c.write("config/AHU-01/FanCommand/", "bool", False)
        self.assertIn("allowlist", str(cm.exception))
        self.assertEqual(len(self.station.writes), 1)

    def test_allowlist_prefix_covers_children_not_siblings(self):
        c = self.client(allow_write=True, write_allowlist=["config/AHU-01/"])
        c.write("config/AHU-01/Mode/", "str", "unoccupied")
        self.assertFalse(c._allowed(c.url_for("config/AHU-02/Mode/")))
        # A prefix must not match by string alone: AHU-01x is a different unit.
        self.assertFalse(c._allowed(c.url_for("config/AHU-01x/Mode/")))

    def test_writing_a_read_only_point_reports_the_stations_refusal(self):
        c = self.client(allow_write=True, write_allowlist=["config/"])
        with self.assertRaises(ObixError) as cm:
            c.write("config/AHU-01/SupplyAirTemp/", "real", 0)
        self.assertIn("PermissionErr", str(cm.exception))

    def test_wrong_type_is_the_stations_error_not_a_silent_coercion(self):
        c = self.client(allow_write=True, write_allowlist=["config/"])
        with self.assertRaises(ObixError) as cm:
            c.write("config/AHU-01/ZoneSetpoint/", "str", "22")
        self.assertIn("expected real", str(cm.exception))


class TestWatch(Base):
    def test_open_add_poll_close(self):
        c = self.client()
        w = make_watch(c)
        self.assertEqual(w.lease_seconds, 30.0)
        self.assertEqual(set(w.ops), {"add", "remove", "pollChanges", "pollRefresh", "delete"})
        first = w.add(["config/AHU-01/SupplyAirTemp/", "config/AHU-01/ZoneSetpoint/"])
        rows = changes_to_rows(first)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["tag"] for r in rows}, {"real"})
        changed = changes_to_rows(w.poll_changes())
        # The fixture drifts the supply temperature on each poll, the way plant
        # moves, so exactly one of the two should report.
        self.assertEqual([r["href"] for r in changed],
                         ["/obix/config/AHU-01/SupplyAirTemp/"])
        self.assertEqual(len(changes_to_rows(w.poll_refresh())), 2)
        w.delete()
        self.assertEqual(self.station.watches, {})

    def test_remove_stops_reporting_a_point(self):
        c = self.client()
        w = make_watch(c)
        w.add(["config/AHU-01/SupplyAirTemp/", "config/AHU-01/Mode/"])
        w.remove(["config/AHU-01/SupplyAirTemp/"])
        self.assertEqual(w.hrefs, ["config/AHU-01/Mode/"])
        self.assertEqual(changes_to_rows(w.poll_changes()), [])

    def test_the_watch_in_this_station_reads_is_a_list_of_val_attributes(self):
        # BObixWatch.getHrefs takes the first `list` element and reads `val` off
        # each child, so the names carried alongside are decoration. Asserting
        # the shape here keeps a future edit from quietly depending on a name
        # the station never looks at.
        from obix_mcp.watch import _watch_in
        body = _watch_in(["config/AHU-01/Mode/"]).decode()
        self.assertIn('is="obix:WatchIn"', body)
        self.assertIn('<list name="hrefs">', body)
        self.assertIn('<uri val="config/AHU-01/Mode/"/>', body)
        self.assertEqual(fixture_station._watch_in_hrefs(body.encode()),
                         ["config/AHU-01/Mode/"])
        # The same request with the decoration changed still registers, because
        # that is what this station's decoder does.
        other = ('<obj xmlns="http://obix.org/ns/schema/1.0">'
                 '<list name="anything"><str val="config/AHU-01/Mode/"/>'
                 '<obj display="no val here"/></list></obj>')
        self.assertEqual(fixture_station._watch_in_hrefs(other.encode()),
                         ["config/AHU-01/Mode/"])

    def test_a_bad_href_comes_back_as_a_row_not_an_exception(self):
        # One unknown point must not cost the agent the other nineteen.
        c = self.client()
        w = make_watch(c)
        rows = changes_to_rows(w.add(["config/AHU-01/Mode/", "config/Nope/"]))
        self.assertEqual([r["tag"] for r in rows], ["str", "err"])

    def test_expiry_is_reported_before_it_bites(self):
        c = self.client()
        w = make_watch(c)
        w.add(["config/AHU-01/Mode/"])
        self.assertLessEqual(w.seconds_until_expiry, 30.0)
        self.assertGreater(w.seconds_until_expiry, 25.0)


class TestReltime(unittest.TestCase):
    def test_durations_a_lease_can_use(self):
        self.assertEqual(_reltime_seconds("PT30S"), 30)
        self.assertEqual(_reltime_seconds("PT1M30S"), 90)
        self.assertEqual(_reltime_seconds("PT2H"), 7200)
        self.assertEqual(_reltime_seconds("P1DT1S"), 86401)
        self.assertEqual(_reltime_seconds("-PT5S"), -5)

    def test_durations_that_are_not_leases_return_none_rather_than_a_guess(self):
        for v in ("P1Y", "P3M", "", None, "30", "PT", "PTXS"):
            self.assertIsNone(_reltime_seconds(v), repr(v))

    def test_poll_interval_is_clamped_to_the_vendors_floor(self):
        self.assertEqual(safe_interval(0.1), 10.0)
        self.assertEqual(safe_interval(None), 10.0)
        self.assertEqual(safe_interval(45), 45.0)


class TestStationModes(unittest.TestCase):
    def _client(self, mode=None, auth=False):
        base, station, stop = fixture_station.serve(mode or "ok", auth)
        self.addCleanup(stop)
        return ObixClient(base, "admin", "x"), station

    def test_disabled_server_names_the_setting_to_change(self):
        c, _ = self._client("disabled")
        with self.assertRaises(ObixError) as cm:
            c.lobby()
        self.assertIn("410", str(cm.exception))
        self.assertIn("Server > Enabled", str(cm.exception))

    def test_unlicensed_server_says_to_check_the_licence_first(self):
        # The station sends 403 for this, the same code it sends when a user
        # lacks permission on one object, so the message has to carry both
        # readings and the station's own words to tell them apart.
        c, _ = self._client("unlicensed")
        with self.assertRaises(ObixError) as cm:
            c.lobby()
        self.assertIn("export=true", str(cm.exception))
        self.assertIn("Unlicensed oBIX Server", str(cm.exception))

    def test_a_permission_refusal_is_not_read_as_a_licence_problem(self):
        # Same code, different cause: writing a read-only point is refused with
        # 403 and an <err>, and that must not send the reader to the licence.
        c, _ = self._client()
        c.allow_write, c.write_allowlist = True, ["config/"]
        with self.assertRaises(ObixError) as cm:
            c.write("config/AHU-01/SupplyAirTemp/", "real", 5.0)
        self.assertIn("PermissionErr", str(cm.exception))
        self.assertNotIn("Unlicensed", str(cm.exception))

    def test_401_explains_that_basic_is_not_on_by_default(self):
        base, _s, stop = fixture_station.serve("ok", True)
        self.addCleanup(stop)
        with self.assertRaises(ObixError) as cm:
            ObixClient(base).lobby()          # no credentials at all
        self.assertIn("HTTPBasicScheme", str(cm.exception))
        self.assertIn("AuthenticationService", str(cm.exception))

    def test_credentials_are_never_in_a_message(self):
        base, _s, stop = fixture_station.serve("ok", True)
        self.addCleanup(stop)
        c = ObixClient(base, "admin", "hunter2")
        try:
            c.read("config/Nope/")
        except ObixError as exc:
            self.assertNotIn("hunter2", str(exc))
            self.assertNotIn("Basic ", str(exc))

    def test_unreachable_station_says_what_to_check(self):
        # Port 1 on loopback refuses immediately, so this does not hang.
        c = ObixClient("http://127.0.0.1:1", timeout=2)
        with self.assertRaises(ObixError) as cm:
            c.lobby()
        self.assertIn("/obix", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
