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

**Closed.** The concrete web server is `com.tridium.jetty.BJettyWebServer`. Its
`doRegister(BINiagaraWebServlet)` rejects the name unless `SlotPath.isValidName`
holds — an invalid name logs `web.badServletName` and is never mapped — and refuses
a second servlet under a name already taken (`web.duplicateServlet`). The mapping is
in `addWebServlet`'s privileged action: it builds a jetty `ServletContextHandler`
whose **context path is `"/" + getServletName()`** (a `StringBuilder` of `'/'` and
the name — the very construction §A already found for the lobby path), mounts the
servlet at `/*` inside it, and calls `setAllowNullPathInfo(true)`, so the bare
context path with no trailing slash — `GET /obix` — still reaches the servlet. So
the jetty mount point and the lobby path derive from the *same* `"/" + servletName`
and agree; only the five getters diverge, by returning fixed literals. That is the
whole of "not configurable in practice": changing the name moves the mount and the
resolver together but leaves the advertised literals at `/obix`, so the WSDL, SOAP
endpoint and stylesheet link would name a servlet that is no longer there. Closes §K.5.

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
itself, so it is not in the list. Twelve agents are registered on `ObixLobby`,
but only **seven write an element** into a GET of `/obix`: `about`, `alarms`,
`batch`, `config`, `continuousControl`, `histories` and `watchService`. The
other five — `alarm`, `bql`, `def`, `ord`, `units` — have an `encodeLobbyChild`
whose whole body is a single `return`, so each resolves but appears in no
listing (§N). `LOBBY_CHILDREN` in `obix_mcp/obix.py` is the seven that are
listed; `UNLISTED_BRANCHES` is the five that are not.

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

### D.1 The challenge, found: `baja.jar`, `web-rt.jar`, `jetty-rt.jar`

The gap above said the challenge "lives in the platform's web stack, which was not
read". It has now been read, which closes §K.4. Located by scanning all 756 module
jars for `WWW-Authenticate`, `realm=` and `HTTPBasicScheme`; the station-side path is
three classes.

**The scheme.** `com.tridium.authn.BHTTPBasicAuthenticationScheme` in `baja.jar`
extends `BPasswordAuthenticationScheme`, and its `SCHEME_NAME` is the string
**`n4HTTPbasic`** — not `basic`, which belongs to
`BLegacyBasicAuthenticationScheme` (`SCHEME_NAME = "basic"`). `getLoginConfiguration()`
returns a `NiagaraLoginConfiguration` over `UsernamePasswordLoginModule`, so this is
an ordinary username/password JAAS login once the credentials are in hand.

**Two callback handlers are registered on that one scheme.** From `web-rt.jar`'s
`module.xml`, both of these carry `<agent><on type="baja:HTTPBasicAuthenticationScheme"/>`:

  * `com.tridium.web.authn.BWebHTTPBasicCallbackHandler` — **this is RFC 7617.**
    `handleRequest` reads `Authorization`, takes `substring(indexOf(" ") + 1)`,
    `Base64.getDecoder().decode(...)`, then `new String(bytes)` **with no charset
    argument**, splits on the first `:` into a `String` username and a `char[]`
    password, sets `readyForCallback = true` and returns `0`. Returning 0 means no
    challenge: it proceeds straight to the login module. Note the charset — the
    credentials are decoded in the JVM's *default* charset, not an explicit UTF-8, so
    a non-ASCII password is a real portability risk between a client that encodes
    UTF-8 and a station that decodes something else.
  * `com.tridium.web.authn.BHttpBasicCallbackHandler` — **challenge only, and not
    RFC 7617 at all.** It extends `BHttpHeaderCallbackHandler`. Its `processHello`
    parses the `Authorization` header with `javax.baja.web.authn.AuthMessage
    .decodeFromString`, reads a **`username` parameter** out of it and
    `Base64.getUrlDecoder()`-decodes that as UTF-8, then builds a fresh `AuthMessage`
    with scheme `BASIC`, sets it as `WWW-Authenticate`, sets status `401` and returns
    `1`. Its `handle(Callback[])` unconditionally throws
    `UnsupportedCallbackException("HTTP Basic Callback Handler does not support any
    callbacks.")`, so it can never complete a login by itself.

**The `AuthMessage` wire format** (`javax.baja.web.authn.AuthMessage`, `web-rt.jar`)
is Niagara's own, not the HTTP `auth-param` grammar. `decodeFromString` takes
`indexOf(' ')`; everything before it is the scheme and everything after is tokenized
on `,`, each token split at the first `=` into a trimmed key and value, with
`IllegalArgumentException("parameter missing '='")` and
`("duplicate parameter")` as the two failures. If there is no space at all the whole
string is the scheme and there are no parameters. The named parameter constants are
`handshakeToken`, `username`, `authToken`; the schemes named are `HELLO` and `BEARER`;
`ILLEGAL_TOKEN_CHARS` is ``(),/:;<=>?@[\]{}``.

**What is actually on the wire.** `com.tridium.jetty.NiagaraAuthenticator` is what
sends it. When a handler returns the challenge code and **is** a
`BHttpHeaderCallbackHandler`, the authenticator sets status 401, reads back the
`WWW-Authenticate` header the handler just set, `AuthMessage.decodeFromString`s it,
**adds `handshakeToken` = the Niagara web session id**, re-sets the header, stores
`authenticationScheme` and `callbackHandler` as attributes on that web session, and
returns jetty's `SEND_CONTINUE`. So the challenge a client sees is shaped like:

```
WWW-Authenticate: BASIC handshakeToken=<niagara web session id>
```

Two things follow, and both matter to a client author:

  * **There is no `realm`.** No `realm=` literal exists on this path in `web-rt.jar`,
    `jetty-rt.jar` or `baja.jar` — the nine jars that do contain one are all HTTP
    *client* or platform-daemon code. The scheme token is also upper-case `BASIC`
    rather than the conventional `Basic`. A client that only engages Basic auth after
    recognising a well-formed `Basic realm="..."` challenge has nothing to match on.
  * **The challenge is stateful.** The handler instance and the scheme are parked on a
    server-side session and the token names it. A client that treats 401 as "resend
    with credentials and forget" is not participating in that handshake.

Which is why this bridge sends `Authorization: Basic …` **pre-emptively on every
request** rather than waiting to be challenged: the pre-emptive path is handled by
`BWebHTTPBasicCallbackHandler`, which is real RFC 7617 and returns 0 without ever
issuing a challenge. Waiting for the challenge is the path that leads into the
session-bound `AuthMessage` handshake instead.

**Which handler a given request gets, resolved.** Both handlers are agents on the same
scheme, so the choice is made where `NiagaraAuthenticator.authenticateHeader` asks the
registry for one. It decodes the `Authorization` header as an `AuthMessage` and branches
on the scheme token: the literal **`HELLO`** (Niagara's own two-step handshake) makes it
call `scheme.getAgentOn(BHttpHeaderCallbackHandler.class)`, which returns the
challenge-only `BHttpBasicCallbackHandler`; **anything else — a normal `Basic <base64>`
credential — makes it call `getAgentOn(BWebCallbackHandler.class)`, which returns the
RFC 7617 `BWebHTTPBasicCallbackHandler`.** Both concrete handlers share the abstract
supertype `BHttpCallbackHandler`, so the authenticator casts to that and calls
`handleRequest` polymorphically. The disambiguation is by requested base class, not by
any per-scheme priority: `BWebHTTPBasicCallbackHandler extends BWebCallbackHandler` and
`BHttpBasicCallbackHandler extends BHttpHeaderCallbackHandler`, and only one of the two
agents is assignable to each. So a pre-emptive `Authorization: Basic …` — never the
`HELLO` token — is routed to the RFC 7617 handler every time, the same handler whose
decode uses the default charset. This establishes that a station's 401 to an oBIX client
carries no realm, that the credential-parsing path uses the default charset, and now
which handler that path is.

## E. Two write paths, and the export descriptor is only one of them

`ExposingWritableControlPointsForExt-…html`: writable control points are exposed
by adding an `ObixExport` descriptor under the ObixNetwork's Exports folder, and
"only point types with priority input slots are valid candidates" — the four
being `BooleanWritable`, `EnumWritable`, `NumericWritable`, `StringWritable`.
Each descriptor names a `Point` ORD and a `Priority` level, and creates a link on
the target so "contention from within the Framework does not occur".

That is Tier B, and taken alone it invites a wrong conclusion — that the Exports
folder is the list of what an outside caller may move. The bytecode says otherwise.
There are **two** write paths, and only the first one consults an export.

**Path 1, the export descriptor — a POST, not a PUT.**
`javax.baja.obix.driver.export.BObixExport` implements `BIObixEncodable`,
`BIObixInvocable` and `BIStatus`; it does **not** implement `BIObixWritable`. Its
static initialiser makes the `writePoint` property a
`new BObixOp("write", "obix:WritePointIn", "obix:Point")`, so a descriptor advertises
an *op* and the write is an invocation. `invoke(ObixDecoder, ObixEncoder)` calls
`decoder.addFacets(getFacets())`, decodes, and sets the descriptor's **own** `out`
`BStatusValue` — accepting a complex carrying a `value` child, or a bare `BDouble`,
`BBoolean` or `BString`, and silently leaving the value alone for anything else. The
link `doConfigure` made is what carries that value to the point's priority input;
this method takes no user and performs no permission check of its own. Any exception
becomes a `WrapperException`.

`doConfigure(Context)` resolves the `point` ORD against `Sys.getStation()` and
requires both `BIWritablePoint` and `BControlPoint`, else it sets status `fault` with
reason `"<ord> is not a writable point."`; the other fault reason is
`" is already linked in priority "`. The four type branches are exactly
`BBooleanWritable`, `BNumericWritable`, `BEnumWritable`, `BStringWritable`,
confirming the guide's claim from code. Two defaults worth knowing before trusting a
descriptor: `priority` defaults to `BPriorityLevel.level_10`, and
`DISALLOWED_PRIORITIES` is `BEnumRange.make([0], [BPriorityLevel.none.getTag()])` —
a single forbidden value, `none`. **Levels 1 through 16 are all permitted, including
level 1.** The descriptor does not keep anybody away from an override level; a human
chose 10, and a human can choose 1.

**Path 2, the generic property set — and it never looks at an export.**
`ObixUtils.serviceWrite(OrdTarget, ObixDecoder, ObixEncoder)` branches to
`BIObixWritable.write(decoder)` when the target implements it — and **nothing does.**
Scanning every one of the 756 jars in `/opt/Niagara/Niagara-4.15.5.22/modules/`, the
only classes that so much as name `BIObixWritable` are `ObixUtils`, which does the
`instanceof`, and the interface itself. So on a stock station every PUT falls through
to the generic branch:

  * target null, or `getPropertyInParent()` null → `BadUriErr("PUT " + href)`;
  * `newCopy(true)`, then `decoder.decode(value)`;
  * if the component is a `com.tridium.program.BCode` and the property is `source` or
    `classFile` → `PermissionException("Cannot write program code")`, the one
    hardcoded refusal in the method;
  * walk `getPropertyPathInComponent()` down to the owning complex, then
    `BComplex.set(prop, value, OrdTarget.getUser())`;
  * re-resolve the ORD and encode the result, so a PUT answers with the new state.

The whole body sits in one `catch (PermissionException)` — the exception table covers
offsets 5 to 209 — rethrown as `PermissionErr(user.getName() + " cannot write this
object")`. That is the real boundary. **There is no reference to an export descriptor,
an Exports folder or any allow-list anywhere in `serviceWrite`.** What stops a PUT is
the oBIX user's Niagara permissions on the target component's *category*, which is
what makes that caught `PermissionException` fire in the first place.

By contrast `serviceInvoke` has **no** exception table, so a refused action does not
come back as a well-formed `PermissionErr` the way a refused PUT does. Its own shape:
`BIObixInvocable` → that object's `invoke`; otherwise `getSlotInComponent()` cast to
`Action`; a `BObixLobby` target with a null action re-encodes the lobby rather than
erroring; any other null action → `BadUriErr("POST " + href)`; the body is decoded
against `action.getParameterDefault()` when there is one, then
`BComponent.invoke(action, val, cx)` with the `OrdTarget` itself as context, followed
by `waitForPointExecute(target, EngineManager.getCycles(), Clock.ticks())`. Unlike the
write path, `BIObixInvocable` has nine implementors: `BAlarmServiceQuery`,
`BAlarmWrapper`, `BBatchOp`, `BObixHistoryAppend`, `BObixHistoryQuery`,
`BObixHistoryRollup`, `BObixOp`, `BObixExport`, and `BObixTransformHistoryQuery` in
`obixSeriesTransform-rt`.

One detail ties this to §N: `doConfigure` builds the op-in contract URIs as
`/obix/def/baja:StatusBoolean` and friends — pointing into the `def` branch, which is
one of the five that resolve but write no element into the lobby listing. A descriptor
cites a URI the lobby never advertises.

Three consequences this bridge takes seriously:

  * a point being visible over oBIX does not make it writable, but the converse trap is
    the dangerous one — **the absence of an export does not make a property safe**,
    because the generic PUT path does not consult the export list at all;
  * the fence that actually holds is the station's own permission model on the
    component's category, which is engineered per-user and is not visible in any
    listing the bridge can read — so the bridge cannot compute what it is allowed to
    write, and must not pretend to;
  * a write is a plant movement at a priority, not a variable assignment. Hence
    two gates: `--allow-write` for the deployment and `--write-allow` for the
    subtree, and no write tool advertised at all without them. Those gates are this
    bridge's own fence, chosen precisely because the protocol does not supply one.

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

**Corrected in §N.6.** The station's own names for those queries were read out of
`BObixHistoryAgent` after this section was written: they are `today`,
`last24Hours` and `yesterday`, there are eleven of them, they are `ref` elements
rather than ops, and the same document carries four ops — one of which appends
records to the history.

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
  3. ~~**No mechanism for a station to export its own alarms as an oBIX feed.**~~
     **Closed in §N.** The gap was in the documentation: the guide documents alarm
     *import* only, but a stock station serves its open alarms through the alarm
     service's `query` op and its `feed`, and serves individual records at
     `/obix/alarm/<uuid>` — a branch no lobby lists. The bridge exposes
     `obix_alarms` for the first and reports the second.
  4. ~~**The HTTP challenge** (401, `WWW-Authenticate`, scheme selection, realm) is
     not in either oBIX jar.~~ **Closed in §D.1.** It is not in either oBIX jar because
     it is in `baja.jar`, `web-rt.jar` and `jetty-rt.jar`: the scheme is
     `n4HTTPbasic` over `UsernamePasswordLoginModule`, two callback handlers are
     registered on it (one real RFC 7617, one challenge-only), and the challenge jetty
     emits is `WWW-Authenticate: BASIC handshakeToken=<web session id>` with **no
     realm**. The agent-selection rule that picks between the two handlers is now read
     too (§D.1): `NiagaraAuthenticator` branches on the `Authorization` scheme token —
     the literal `HELLO` routes to the challenge-only handler, a normal `Basic`
     credential to the RFC 7617 one, via `getAgentOn` of two different base classes.
  5. ~~**Which URL prefix the web server derives from `servletName`.**~~ **Closed in §A.**
     `com.tridium.jetty.BJettyWebServer.doRegister` mounts the servlet at jetty
     context path `"/" + getServletName()` (with `/*` under it and
     `setAllowNullPathInfo(true)`), the same `"/" + servletName` the lobby path and the
     resolver use. The five getters return fixed `/obix` literals, so the mount and the
     advertised paths only agree while the name is left at its default.
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
from the decoder, but never sent to a real station), and authentication — though
§D.1 has since read the challenge out of `baja.jar`, `web-rt.jar` and `jetty-rt.jar`,
so what is left there is narrower than it was. What a first real run should record
about auth: whether the 401 body and headers match `BASIC handshakeToken=…` with no
realm, and whether a non-ASCII password survives the round trip given the station
decodes credentials in its default charset. `--dump` prints every request and
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

---

## N. Alarms, the branch no lobby lists, and the href rule that governs the whole tree

Read after §B and §G. Two of those sections were incomplete and one was wrong;
this section is where that is said. Classes read: `BAlarmsLobbyAgent`,
`BAlarmServiceAgent`, `BAlarmServiceQuery`, `BAlarmClassAgent`,
`BAlarmLobbyAgent`, `BAlarmWrapper`, `BObixHistoryAgent`, `ObixEncoder`,
`ObixUtils`, `BadUriErr`.

### N.1 Where a station's own alarms actually are

§K listed as gap 3 that this install documents no way for a station to export its
alarms, and this bridge shipped no alarm tool for that reason. The gap was in the
documentation, not in the station. **A stock station serves its open alarms
through an op**, and here is the whole path:

| Claim | Where it is read |
| --- | --- |
| The `alarms` lobby child is a *ref* to the alarm service, advertised at `/obix/config/Services/AlarmService` | `BAlarmsLobbyAgent extends BShortcutLobbyAgent`; `getComponent()` is `Sys.getService(BAlarmService.TYPE)`, and `encodeLobbyChild` sets the href to `makeSlotPathUri(encoder, getComponent().toPathString())` = `concat(concat(lobbyPath, "config"), "/Services/AlarmService")` |
| A bare GET of `/obix/alarms/` by *name* does **not** reach the service | the shortcut's `resolve("")` builds `station:\|slot:`, which resolves to the station root. Only the advertised href reaches the service; the bridge follows the ref, never the name |
| Walking the service finds no alarms | its component children are alarm *classes*. The records are not slots on it |
| What makes it usable is added after the component | `BAlarmServiceAgent.encodeFinishing` writes `<int name="count">` (the open-alarm count), an `op` named `query` with `in="obix:AlarmFilter" out="obix:AlarmQueryOut"`, and a `feed` named `feed` |
| The op's href is built from `~alarmQuery`, the feed's from `~alarmFeed` | both are `ldc` constants passed to `encoder.getChildHref(encoder.getHref(), …)` — see N.4 for what that returns |
| The filter is read by child name, and every child is optional | `BAlarmServiceQuery.invoke` looks up `limit`, `start`, `end` and tolerates each being absent. An empty filter means every open alarm |
| It becomes BQL | `alarm:` + `bql:select * from openAlarms`, with `where alarmClass = '…'` added when the op belongs to one alarm class, and `lastUpdate` bounds when the filter carried times |
| The reply puts records *before* the totals | the records are a `<list name="data" of="obix:Alarm">`, and `count`, `start`, `end` are written after it, because the station streams a cursor and only then knows them. Nothing may depend on child order |
| A record names several contracts at once | `is="obix:Alarm obix:AckAlarm"` plus `obix:PointAlarm` or `obix:StatefulAlarm` |
| A record's only durable handle is a UUID | `<str name="niagara-uuid">`. `source`, `sourceStation`, `msgText`, `alarmClass`, `priority`, `alarmValue`, `timestamp`, `normalTimestamp`, `ackTimestamp`, `ackUser`, `originalSource`, `originalAlarmClass`, `originalPriority` are the rest |
| One alarm class can be asked instead of the service | `BAlarmClassAgent.encodeFinishing` advertises the same `query` op and `feed`, scoped by the BQL `where` above |

### N.2 Five lobby agents write nothing, and `alarm` is the useful one

§B lists seven lobby children. Twelve agents are registered on `ObixLobby`; the
other five — `alarm`, `bql`, `def`, `ord`, `units` — each have an
`encodeLobbyChild(ObixEncoder, Context)` that is **a single `return`**: three
bytes of bytecode, no element. They resolve but appear in no listing. `bql` runs
a BQL query, `ord` resolves a raw ORD, `def` is the contract dictionary, `units`
the unit database, and `alarm` is the one worth a tool.

`BAlarmLobbyAgent` — the singular `alarm`, not the `alarms` shortcut —
`getLobbyName()` returns `"alarm"`. Its `resolve(String, Context)` is not empty:
it splits the URI at the first `/`, decodes the first part with
`BUuid.decodeFromString`, fetches that record from the alarm database and wraps
it in `new BAlarmWrapper(record, ackFlag)` — where `ackFlag` is
`remainder.contains("ack")`.

So `/obix/alarm/<uuid>` is a live, readable object that **appears in no lobby
listing**. A client that only walks what it is shown cannot reach it; a client
that has a UUID from a query reply can. That is why `obix.py` carries
`UNLISTED_BRANCHES` (the five) beside `LOBBY_CHILDREN` (the seven), and why
`obix_lobby` reports them: an unlisted branch is worth telling an operator about,
not worth pretending away.

### N.3 What an ack is, and the four ways the station makes one awkward

Every record the station encodes advertises an `ack` op. An ack is a POST that
**writes to the alarm database** — it sets the record's ack user, ack time and
state, and with a `forceCleared` child it also drives the source to normal and
fires the service's `auditForceClear` action. Four details, all from
`BAlarmWrapper` and `BAlarmServiceAgent`:

  1. **`ackUser` is mandatory.** `invoke` calls `get("ackUser").toString()`
     before any branch, so an `obix:AlarmAckIn` without that child throws a
     `NullPointerException` rather than defaulting to the authenticated user.
  2. **`ackUser` is then ignored.** The value is overwritten with the
     authenticated user's name. Mandatory and discarded at once.
  3. **The advertised out contract is not the one the reply carries.** The op is
     written with `out="obix:AlarmAckOut"`; the reply is
     `is="obix:AckAlarmOut"`. A client matching the advertised contract finds
     nothing. Both spellings are Tridium's.
  4. **The ack path is reached by URI, not by contract.** `resolve` turns *any*
     remainder containing the substring `ack` into an ack-capable wrapper.

**What the bridge does with it.** `alarms.py` reads and cannot ack. This is the
same shape as the batch defence in §M: not a check inside the ack path, which a
later edit could relax, but the absence of anything that could express one — no
verb argument, no ack contract string, no tool, and no request builder that takes
a record href. `tests/test_alarms.py` walks every string constant in the module
and fails on any that mentions ack outside the two field names a record reports,
and separately drives a hand-written ack at the fixture to prove the fixture
would have performed it. The test is about what the bridge can express.

### N.4 The href rule, which is not an alarm detail

`ObixEncoder.getChildHref(parentHref, name)` decides what every child element's
`href` says:

  * `name == null` → `null`;
  * `parentHref.equals(docHref) && parentHref.endsWith("/")` → **`name + "/"`,
    bare and relative**;
  * `parentHref == null` → `name + "/"`, likewise;
  * otherwise → `ObixUtils.concat(parentHref, name + "/")`.

`ObixEncoder.configChild(parentHref, OrdTarget)` — the path **every component
child** goes through, not a special case for a few agents — calls it for each
child's href, and falls back to `"|" + ord.encodeToString()` for a child whose
name it cannot determine.

The consequence is the opposite of what a reader expects: **the relative form is
what an ordinary GET returns.** A direct read makes the object the document
element, its href ends in a slash, and so every child comes back bare. Nested
encodings — where the parent href differs from the document href — get the
concatenated form. A GET of the alarm service — at
`/obix/config/Services/AlarmService/`, the href the `alarms` ref advertises —
answers with its query op at `~alarmQuery/`, which means
`/obix/config/Services/AlarmService/~alarmQuery/`; a client that resolves
relative hrefs against the servlet path instead asks for `/obix/~alarmQuery/`
and is told 404 by a station that is working correctly.

**The lobby is the one document where the two rules agree**, which is how a
bridge passes every test it has against a lobby walk and then fails on the first
nested op. This one did. `obix.py`'s `resolve_href` implements the rule above and
`decode` applies it to the whole document, depth first, against the path the
document was read from; `tests/test_obix.py` pins each branch, and the fixture
serves relative hrefs exactly where the station does so that serving them
absolute again fails the suite.

`|`-prefixed hrefs are left alone: an ORD is not a path, and joining one onto a
path would invent a URL.

### N.5 A diagnostic the station throws away

`BAlarmLobbyAgent.resolve` builds `new BadUriErr("UUID not found: " + s)` for a
UUID the database does not hold — and throws it inside a try whose handler is
`catch (Exception) { throw new BadUriErr(); }`. `BadUriErr extends
RuntimeException`, so the method discards its own message. Since
`ObixEncoder.encode(Throwable)` uses `getMessage()` and falls back to
`Throwable.toString()` when it is null, what reaches the client is
`<err is="obix:BadUriErr" display="com.tridium.obix.util.BadUriErr"/>`.

An unparseable UUID, a well-formed UUID that is simply absent, and a database
error are therefore **indistinguishable**. `ObixFault` in `obix.py` recognises a
`display` that is a bare Java class name and says so, because the station will
not.

### N.6 Histories, corrected

§G is right that Tridium publishes no URL template and right that a bridge must
follow the station's own hrefs. It is wrong about the names, and it does not
mention that the same document carries ops that write.
`BObixHistoryAgent.encodeFinishing` writes, in this order:

  * `count`, `start`, `end`;
  * four **ops**, whose `in`/`out` are **def paths and not contract names** —
    `query` (`/obix/def/obix:HistoryFilter` → `/obix/def/obix:HistoryQueryOut`),
    `rollup`, `feed` (a `feed` element, `of="/obix/def/obix:HistoryRecord"`) and
    `append` (`/obix/def/obix:HistoryAppendIn`). A client matching
    `in="obix:HistoryFilter"` matches nothing. Their hrefs are built from
    `~historyQuery`, `~historyRollup`, `~historyFeed`, `~historyAppend` through
    the rule in N.4;
  * eleven **refs** — the canned queries, which a GET follows — named
    `unboundedQuery`, `today`, `last24Hours`, `yesterday`, `weekToDate`,
    `lastWeek`, `last7Days`, `monthToDate`, `lastMonth`,
    `yearToDate (limit=1000)` and `lastYear (limit=1000)`. Two of those names
    carry a space and brackets *inside the `name` attribute*. Their hrefs are
    relative and already carry the bounds the station computed from its own
    clock: `~historyQuery?start=…&end=…`, plus `&limit=1000` on the last two and
    `~historyQuery?limit=1000` on `unboundedQuery`.

So the documentation's "Today, Last 24 Hours and Yesterday" are `today`,
`last24Hours` and `yesterday`, and there are eight more. `obix_history` lists
what the station offers, matches a caller's spelling case- and space-insensitively
(and on the leading word, so `yearToDate` reaches the bracketed name), and
**refuses to follow any of the four ops by name** — one of them appends records to
a history. `append` is a write, and this bridge's history tool is a read.

### N.7 Not tested against a station

As §L. Everything above is read out of the shipped jars with `javap` and proved
against `tests/fixture_station.py`, which was written from this section — the
fixture acks, force-clears, appends history records and reproduces both the
contract mismatch and the contentless fault, so that the tests are about what the
bridge does rather than about what the fixture happens to support. The most
likely thing to differ on a real JACE is the exact `href` form on a nested
document, which is why N.4 is implemented as a rule with its branches pinned
rather than as a string fix-up for the two cases that prompted it.
