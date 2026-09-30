# EVIDENCE

Every protocol decision in this bridge, and where it came from.

The source is one Niagara 4.15.5.22 install — its shipped jars and its shipped
documentation, nothing else. Not the oBIX specification, not a vendor blog, not
recollection. Where the install answers a question, the answer is here with the
class and method it was read out of. Where it does not, that is here too, under
§K, said plainly rather than filled in from the specification and hoped for.

Read §K before trusting this against a controller you care about. The bridge has
**not** been run against a JACE: see §L.

  Install: `/opt/Niagara/Niagara-4.15.5.22`, `obixDriver` module
  `vendorVersion="4.15.5.22"`.

## How to check any of it yourself

```sh
NIAGARA=/opt/Niagara/Niagara-4.15.5.22
mkdir -p /tmp/obix-rt && unzip -qo $NIAGARA/modules/obixDriver-rt.jar -d /tmp/obix-rt
javap -p -c -classpath /tmp/obix-rt javax.baja.obix.driver.BObixServer | less
javap -p -c -classpath /tmp/obix-rt com.tridium.obix.watch.BObixWatch | less
# the documentation, which is HTML inside a jar:
mkdir -p /tmp/obix-doc && unzip -qo $NIAGARA/modules/docObix-doc.jar -d /tmp/obix-doc
```

Jar hashes, so a disagreement can be traced to a different build rather than to
a mistake here:

| jar | sha256 |
|---|---|
| `docObix-doc.jar` | `d72e0b5652230cbff8dbc4bd5db5f74fd206d2b6ad3a8169f10b14782ec75651` |
| `docStationSecurity-doc.jar` | `fb221d6622a50b0e189a2db5676b1ae38d10dad1f8048f7625d255008b74f400` |
| `docDrivers-doc.jar` | `0bc35fa0b1848e3dbaabe007991740229942e3e5a3690afdafde7988f445cffe` |
| `obixDriver-rt.jar` | `adea7a80bfb80ccac6070feee7ae5a431bd99ecba184c9ba5f879ce7eafb28b8` |
| `obix-rt.jar` | `67f56c646fc60db78367af518db8935e4c507c5331beb06a92b670933dd133f0` |
| `web-rt.jar` | `2645d48548eaf2c326bdd1d34f3e4638edbad526f307fcde1ff206dbc495bf30` |

---

## A. The servlet path is `/obix` and is not configurable in practice

`BObixServer` carries a `servletName` property whose default is `"obix"`. None of
the five path methods reads it — each is a single `ldc` of a fixed string:

| method | literal it returns |
|---|---|
| `BObixServer.getServletPath()` | `/obix` |
| `BObixServer.getSoapPath()` | `/obix/soap` |
| `BObixServer.getWsdlPath()` | `/obix/wsdl` |
| `BObixServer.getXsdPath()` | `/obix/xsd` |
| `BObixServer.getXslPath()` | `/obix/xsl` |

So the tree is at `http://host/obix`, and this client hard-codes that
(`SERVLET_PATH`). It deliberately offers no setting for it: a setting would imply
the station honours one.

The guide agrees in words — `obixDriver-ObixServer.html` gives `Servlet Name` as
read-only and "Currently fixed at: `obix`" — and `EnablingAnOBIXServer-…html`
documents the oBIX URI as `http://[hostnameOrIP]/obix` and the WSDL as
`http://[hostnameOrIP]/obix/wsdl`.

**Now read, and it changes the shape of the answer.** The property is not inert.
Two live code paths read it while the five getters do not:

  * `BWebServlet` (in `web-rt.jar`) registers the servlet by calling
    `BWebServer.register(BINiagaraWebServlet)`, and that interface's only
    identifying method is `getServletName()`. `BWebServlet` also unregisters and
    re-registers whenever the `servletName` property changes. So the name, not
    the hard-coded path, is the servlet's identity to the web server.
  * `BObixServer` builds *every* `ObixEncoder` with a lobby path of
    `"/" + getServletName()` — two construction sites in `service(WebOp)`, both a
    `StringBuilder` of `'/'` and the property — and `BObixServer.resolve` strips
    that same prefix off every URI it is handed, via `ObixUtils.resource`.

So the hard-coded `/obix` in the five getters is what the WSDL, the stylesheet
link and the SOAP endpoint advertise, while resolution and registration follow
the property. A station with the property changed would be internally
inconsistent, which is presumably why the guide gives `Servlet Name` as read-only
and "Currently fixed at: `obix`". This client still hard-codes `/obix`: on a
stock station the two agree, and offering a setting would imply the station
honours one everywhere, which it does not.

**Still not closed:** the URL prefix the web server derives from the servlet name
is in `BWebServer.doRegister`, which is abstract — the concrete web server was
not read.

## B. The lobby's children, with the one name that surprises people

Each lobby agent's mounted name, read from its own class:

| agent class | mounted name | how |
|---|---|---|
| `com.tridium.obix.server.BObixAbout` | `about` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BAlarmLobbyAgent` | `alarm` | getLobbyName() returns the lobbyName field, set in <clinit> |
| `com.tridium.obix.server.BAlarmsLobbyAgent` | `alarms` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BBatchOp` | `batch` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BBqlLobbyAgent` | `bql` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BStationLobbyAgent` | `config` | getLobbyName() returns the lobbyName field, set in <clinit> |
| `com.tridium.obix.server.BExportLobbyAgent` | `continuousControl` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BContractLobbyAgent` | `def` | getLobbyName() returns the lobbyName field, set in <clinit> |
| `com.tridium.obix.server.BHistoriesLobbyAgent` | `histories` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BOrdLobbyAgent` | `ord` | getLobbyName() returns the literal |
| `com.tridium.obix.server.BUnitLobbyAgent` | `units` | getLobbyName() returns the lobbyName field, set in <clinit> |
| `com.tridium.obix.watch.BObixWatchService` | `watchService` | getLobbyName() returns the literal |

The export branch is mounted as **`continuousControl`**. An agent that walks to
`/obix/export` because the documentation calls it the export agent finds nothing.
This is the single most useful thing in this file, and `obix_lobby` says it in
the tool reply rather than leaving it to be rediscovered.

`BShortcutLobbyAgent` declares `getLobbyName()` abstract and mounts nothing
itself, so it is not in the list. Twelve names, and `LOBBY_CHILDREN` in
`obix_mcp/obix.py` is exactly these twelve.

## C. GET reads, PUT writes, POST invokes

`BObixServer.service(WebOp)` branches on the request method: GET encodes the
resolved target (`ObixEncoder.encode(OrdTarget)`), PUT calls
`ObixUtils.serviceWrite`, POST invokes. The station's own WSDL declares the same
three as `obixRead`, `obixWrite` and `obixInvoke`, each returning `obix:obj`.
Responses are `text/xml`.

`VERB_READ`, `VERB_WRITE`, `VERB_INVOKE` in `obix_mcp/obix.py`.

## D. Authentication: HTTP Basic is not on by default

`BObixServer.service(WebOp)` reads the `Authorization` header only to decide
whether to reuse or invalidate the HTTP session (it is tied to the
`allowSessionReuse` property). It never parses `Basic `, never decodes Base64 and
never compares credentials. The challenge itself is not in `obixDriver-rt.jar` or
`obix-rt.jar` at all — no `WWW-Authenticate`, no `realm=` literal anywhere in
either — so it lives in the platform's web stack, which was not read. That is a
gap, not a finding of "no auth".

What the shipped Station Security guide does say:

  * `auth_AssigningAuthenticationSchemes.html`: "By default, each new station
    comes with `DigestScheme` and `AXDigestScheme` already installed.
    `DigestScheme` is assigned to all users."
  * `auth_NiagaraAuthentication.html`: "Other schemes, such as HTTP-Basic, apply
    only to certain web logins (for example, Obix clients)."
  * `auth_AuthenticationSchemes.html`: "The system supports only schemes that
    have been added to the `AuthenticationService`." `HTTPBasicScheme` lives in
    the `baja` palette and has to be added there and assigned to the user.

So a station that has never had `HTTPBasicScheme` added answers 401 to this tool
no matter what the password is, and that is what `_STATUS_HELP[401]` says. It is
the first thing to check on a station that "should work".

The station as an oBIX *client* does build a Basic header — `obix.net.ObixSession`
in `obix-rt.jar` assembles `"Basic " + Base64(user + ":" + password)` — which is
the only literal Basic construction in either jar, and it is outbound.

## E. Writes reach a point through an export descriptor

`ExposingWritableControlPointsForExt-…html`: writable control points are exposed
by adding an `ObixExport` descriptor under the ObixNetwork's Exports folder, and
"only point types with priority input slots are valid candidates" — the four
being `BooleanWritable`, `EnumWritable`, `NumericWritable`, `StringWritable`.
Each descriptor names a `Point` ORD and a `Priority` level, and creates a link on
the target so "contention from within the Framework does not occur".

Two consequences this bridge takes seriously:

  * a point being visible over oBIX does not make it writable — the export has to
    exist, at a priority someone chose;
  * a write is a plant movement at a priority, not a variable assignment. Hence
    two gates: `--allow-write` for the deployment and `--write-allow` for the
    subtree, and no write tool advertised at all without them.

The guide shows no literal write request anywhere. The element name is the type
(§H), and `obix_write` requires it explicitly rather than inferring it, because
inferring it is how a set point gets written as a `str`.

## F. Watches

The service and the watch, with their in/out contracts, read from the `BObixOp`
triples in each class's static initialiser:

| class | op | in | out |
|---|---|---|---|
| `BObixWatch` | `add` | `obix:WatchIn` | `obix:WatchOut` |
| `BObixWatch` | `remove` | `obix:WatchIn` | `obix:Nil` |
| `BObixWatch` | `pollChanges` | `obix:Nil` | `obix:WatchOut` |
| `BObixWatch` | `pollRefresh` | `obix:Nil` | `obix:WatchOut` |
| `BObixWatch` | `delete` | `obix:Nil` | `obix:Nil` |
| `BObixWatchService` | `make` | `obix:Nil` | `obix:Watch` |

  * `BObixWatchService.defaultLeaseTime` defaults to `BRelTime.makeSeconds(30)`.
  * `BObixWatch`'s own `lease` property defaults to `BRelTime.makeSeconds(15)`.

Because those two disagree, this client reads the lease out of the Watch object
the station actually returned and only falls back to 30s when there is none
(`DEFAULT_LEASE_SECONDS`). For the same reason it takes each op's href from the
station's reply rather than assembling `.../watch1/pollChanges` from a template.

The bodies, which the documentation never shows, are evidenced from the code that
reads and writes them:

  * **WatchIn.** `BObixWatch.getHrefs(ObixDecoder)` is
    `document.elem("list").elems()`; `doAdd` then reads attribute `val` off each
    child and skips any child without one. So a WatchIn needs one element named
    `list` whose children carry hrefs in `val`. The list's `name` attribute and
    the children's element names are never read by this station. This bridge
    sends `<list name="hrefs"><uri val="…"/></list>` anyway, because another
    oBIX server may read what this one ignores.
  * **WatchOut.** `doAdd`, `doPollChanges` and `doPollRefresh` each build
    `Obj().setIs("obix:WatchOut")` then `initList("values", "obix:obj")`, so the
    readings arrive inside `<list name="values" of="obix:obj">`.
  * **One bad href does not cost the others.** In the `doAdd` loop, a failure to
    resolve one href is caught per item: `encoder.setHref(href)` then
    `encoder.encode(Throwable)`, then the loop continues. So an unknown point
    comes back as an `<err>` row among the good readings. `changes_to_rows`
    returns it as a row, and the tests assert it.
  * A watch belongs to the user that made it: `BObixWatch.checkUser` throws
    `PermissionErr("Cannot access/modify another user's watch")`.

## G. Histories are read through the station's own canned queries

`histories` is a lobby branch (§B). Tridium documents that histories are exposed
to oBIX clients through a predefined set of query options available as Lobby URIs
by HTTP GET without an oBIX op, and names Today, Last 24 Hours and Yesterday
among them. It publishes **no URL template** for them.

So `obix_history` does not construct history URLs. Called without a query it
lists what the feed itself offers; called with one it follows that child's own
`href`. A bridge that guessed `~today` would work on one station and silently
read nothing on the next.

## H. Value types and faults are element names

`com.tridium.obix.util.Obj` — the station's encoder — writes exactly these
element names: `obj`, `list`, `op`, `ref`, `err`, `feed`, `bool`, `int`, `real`,
`str`, `enum`, `abstime`, `reltime`, `uri`; and these attributes: `name`, `href`,
`is`, `of`, `in`, `out`, `val`, `display`, `displayName`, `status`, `unit`,
`units`, `writable`, `icon`, `min`, `max`, `precision`, `range`, `tz`, `element`,
`xmlns`.

`ObixObject.value()` therefore types a value by its element name and by nothing
else: a `str` whose `val` reads `"true"` stays the string `"true"`. Guessing from
the text is how a mode string becomes a boolean.

Faults come back as `<err>`: `ObixEncoder.encode(Throwable)` calls
`Obj.initErr(...)`, sets the href, and sets `is` to one of `obix:BadUriErr`,
`obix:PermissionErr` or `obix:UnsupportedErr`. Those three are the contracts the
test fixture uses, for that reason.

## I. What a station says when the server is off or unlicensed

Read straight out of `BObixServer.service(WebOp)`:

  * unlicensed — `sendError(403, "Unlicensed oBIX Server")`, before anything else;
  * disabled, either `!getEnabled()` or the ObixNetwork's status being disabled —
    `sendError(410, "oBIX Server Disabled")`.

`obixDriver-ObixNetwork.html` agrees on the second: "If false, remote oBIX clients
cannot access the station's lobby. The server returns HTTP error code 410 for all
requests."

The licence requirement is documented exactly:
`LicenseRequirement-OBIXDriver-…html` — "To support an oBIX server, the
obixDriver license must contain the attribute export=true and be licensed for the
web feature."

**403 is therefore ambiguous** — whole-server licensing, or a permission refusal
on one object — and the body is what separates them. `_STATUS_HELP[403]` says so
and quotes the string to look for, and when the station sends an `<err>` instead,
the fault replaces the generic advice rather than sitting next to it.

`BObixNetwork.checkObixLicense()` calls
`LicenseManager.getFeature("tridium", "obixDriver")` and builds its limits from
`Feature.list()`, so the numeric limits come from the licence file, not from the
jar. There is no compiled-in ceiling on points, devices or watches.

## J. Rates, and what a controller can take

  * `obixDriver-ObixPointDeviceExt.html` and `-ObixAlarmDeviceExt.html`, on Watch
    Interval: "defaults to 2 seconds … To reduce the load on the platform,
    increase this value to 10 seconds or more." That sentence is the floor in
    `MIN_POLL_SECONDS = 10.0`, and `safe_interval()` raises anything lower rather
    than documenting the floor and hoping.
  * `BObixClient.sessionTimeout` defaults to 15s; `watchSafetyFactor` to 10s with
    a 5s floor; `BObixServer`'s `HttpSession` max-inactive fallback is 60s.
  * `MAX_EXECUTE_WAIT_TICKS = 5000` in `com.tridium.obix.util.ObixUtils` — the
    call site was not traced, so no behaviour is claimed from it.

Nothing in the install documents a maximum watch count, session count, payload
size or tree depth. `MAX_WATCHES = 4` and `MAX_HREFS_PER_WATCH = 200` in
`server.py` are therefore **this bridge's choices, not the station's limits**:
they exist because an agent in a loop will open watches until something breaks,
and the thing that breaks is a controller in a building. Raise them deliberately,
with a station in front of you.

## K. What this install does not answer

Stated as gaps, not filled in:

  1. **No literal oBIX request or response document anywhere in the shipped
     documentation.** Checked by exhaustive grep across the extracted HTML, not
     assumed. Everything in §F about bodies comes from the code instead.
  2. **No watch or history URL templates.** Hence §G's design.
  3. **No mechanism for a station to export its own alarms as an oBIX feed.** The
     guide documents alarm *import* only, though `obixDriver-rt.jar` contains
     server-side alarm feed classes (`BAlarmServiceFeed`, `BAlarmsLobbyAgent`).
     This bridge exposes no alarm tool for that reason.
  4. **The HTTP challenge** (401, `WWW-Authenticate`, scheme selection, realm) is
     not in either oBIX jar — see §D.
  5. **Which URL prefix the web server derives from `servletName`** — narrowed.
     The property is read for servlet registration and for URI resolution, and
     ignored by the five path getters; the concrete web server's `doRegister` was
     not read. See §A.
  6. **`signUp` is absent.** The spec's watch sign-up operation appears nowhere in
     either jar (exhaustive string grep).
  7. **No limits of any kind on concurrency or size** — see §J.

## L. What has not been tested

This bridge has been tested against `tests/fixture_station.py`, a small HTTP
server written from this file. That proves the client and the fixture agree, and
that the fixture agrees with the evidence above. **It does not prove a Niagara
station agrees**, and it has not been run against a JACE-8000 or any other
controller.

The two things most likely to differ, in order: the WatchIn body (§F — evidenced
from the decoder, but never sent to a real station), and authentication (§D — the
challenge lives in code that was not read). `--dump` prints every request and
reply for exactly that first run.

---

## M. The batch op: one POST for many reads

Read after §A, because the batch op is where §A's lobby path stops being trivia.
Added after §A–§L were written, which is why it sits at the end rather than next
to the read verbs in §C.

All of this is `com.tridium.obix.server.BBatchOp` in `obixDriver-rt.jar`, with
`com.tridium.obix.util.ObixUtils` and `com.tridium.obix.util.Obj` in `obix-rt.jar`:

```sh
javap -p -c -cp $NIAGARA/modules/obix-rt.jar:$NIAGARA/modules/obixDriver-rt.jar \
  com.tridium.obix.server.BBatchOp
```

| what | evidence |
|---|---|
| Lobby name `batch`, and it is an **op**, not a folder | `getLobbyName()` returns the literal; `encodeLobbyChild` is `Obj().initOp("batch", "obix:BatchIn", "obix:BatchOut")` with `setHref(encoder.getHref())` |
| The request document element must be named `list` | `invoke` takes `decoder.getDocument()`, throws `IllegalArgumentException("BatchIn list not encountered.")` when it is null and `IllegalArgumentException("Expecting list element but encountered " + name)` otherwise. A WatchIn wraps its list in an `<obj>`; a BatchIn must not |
| Each item must be named `uri` | else `Exception("Unexpected batch element: " + name)` |
| Each item needs `val` | read as `elem.get("val", null)`; null gives `Exception("Missing val attribute: " + elem)` |
| **`val` must be a full path containing the lobby path** | `ObixUtils.resource(encoder.getLobbyPath(), val)` is `val.indexOf(lobbyPath)` and throws `BadUriErr` on -1. The lobby path is `"/" + servletName` (§A), so `/obix/...`. Then `~` → `\|obix:` and `/\|` → `\|`, then `BObixLobby.resolve` |
| The verb is the item's `is` attribute, matched with `contains` | `elem.get("is", "")` then `contains("obix:Read")` → `encoder.encode(OrdTarget)`; `contains("obix:Write")` → `ObixUtils.serviceWrite`; `contains("obix:Invoke")` → `ObixUtils.serviceInvoke`; anything else → `Exception("Unknown batch contract: " + elem)`. The default is the empty string, so an item with no `is` is a fault, not a read |
| A write or invoke item needs a child **named** `in` | `ObixUtils.child("in", elem)` compares each child's `name` *attribute*, not its element name; missing gives `Exception("Batch write missing child named 'in'")` or `... invoke ...` |
| The reply is `<list of="obix:BatchOut">`, with no `name` and no `is` | `Obj().initList(null, "obix:BatchOut")`, and `initList(name, of)` is `setElement("list")`, `setName(name)`, `setOf(of)`. A client matching on `is="obix:BatchOut"` finds nothing |
| Every row carries back the `val` that was sent | `encoder.setHref(val)` runs before resolution, and `Obj.initErr(href, display)` takes the encoder's href — so faults carry it too, and rows can be matched by href rather than by position |
| One bad item is one row, and the rest still run | the whole per-item body is inside one `catch (Exception)` that calls `encoder.encode(Throwable)`, and `encoder.commit()` runs at the end of every iteration |
| A fault row is `<err href=… display=…/>`, and its `is` is optional | `ObixEncoder.encode(Throwable)` calls `abort()` first, so a half-written item is discarded, then `initErr(href, message)`. `is` is set only for `BadUriErr`/`UnresolvedException` → `obix:BadUriErr`, `PermissionErr`/`AuthenticationException`/`PermissionException` → `obix:PermissionErr`, `UnsupportedErr` → `obix:UnsupportedErr`. A plain `Exception` gets none |
| A bad `val` displays as a Java class name | `ObixUtils.resource` throws the no-argument `BadUriErr`, whose message is null, so `encode(Throwable)` falls back to `Throwable.toString()`. `batch.py` translates that one string rather than showing it |
| A document-level failure is one `<err>` document, not an empty BatchOut | the throw happens before the reply list is opened; `BObixServer.service(WebOp)` catches it, builds a fresh encoder and calls `encode(Throwable)`. It sets no HTTP error status, so this arrives as a 200 |

**What the bridge does with it.** `obix_batch_read` builds read items and nothing
else. The batch op is the one place where the write gates in `obix.py` could be
walked around — a batch write is a POST to the batch op, not a PUT to a point, so
neither `--allow-write` nor `--write-allow` is consulted on the way past. The
defence is not a check in the batch path, which a later edit could relax: it is
that `batch.py` has no verb argument and never passes a caller's `is` through.
`tests/test_batch.py` asserts that no code in the module names another contract,
and separately drives a hand-written batch write at the fixture to show the
fixture would have performed it. The test is about what the bridge can express.

**Not tested against a station.** Like §F, all of this is read out of bytecode and
proved against a fixture written from it. The batch reply's exact `href` form on a
real JACE is the most likely thing to differ, which is why rows are matched by
href *and* the hrefs with no row are reported rather than dropped.
