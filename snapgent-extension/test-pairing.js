// Quick Node smoke test for core/pairing.js (run: node test-pairing.js). Not shipped.
const fs = require("fs");
const ZSPairing = new Function(fs.readFileSync(__dirname + "/core/pairing.js", "utf8") + "; return ZSPairing;")();

const ok = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + "  " + name); if (!cond) process.exitCode = 1; };

const BASE = "ws://127.0.0.1:17613";

// --- URL building -------------------------------------------------------
ok("no token keeps the bare bridge URL", ZSPairing.bridgeUrl(BASE, "") === BASE);
ok("missing token keeps the bare bridge URL", ZSPairing.bridgeUrl(BASE, null) === BASE && ZSPairing.bridgeUrl(BASE) === BASE);
ok("token rides on the query string", ZSPairing.bridgeUrl(BASE, "abc123") === BASE + "/?token=abc123");
ok("url-safe token is left alone", ZSPairing.bridgeUrl(BASE, "x_y-z.A~") === BASE + "/?token=x_y-z.A~");
ok("reserved characters are percent-encoded", ZSPairing.bridgeUrl(BASE, "a/b+c=d&e") === BASE + "/?token=a%2Fb%2Bc%3Dd%26e");
ok("non-ASCII token is percent-encoded", ZSPairing.bridgeUrl(BASE, "\u00e9") === BASE + "/?token=%C3%A9");

// The bridge parses the query with parse_qs, so the token must come back out
// exactly as it went in - a mangled token would look like a wrong token (1008).
const roundTrip = "a/b+c=d&e #frag";
const parsed = new URL(ZSPairing.bridgeUrl(BASE, roundTrip));
ok("token survives a URL parse", parsed.searchParams.get("token") === roundTrip);
ok("the bridge path stays /", parsed.pathname === "/");

// --- close-code classification -----------------------------------------
ok("1008 asks for pairing", ZSPairing.isAuthClose({ code: 1008 }) === true);
ok("1006 network drop is not a pairing problem", ZSPairing.isAuthClose({ code: 1006 }) === false);
ok("1012 token rotation is not a pairing problem", ZSPairing.isAuthClose({ code: 1012 }) === false);
ok("1000 clean close is not a pairing problem", ZSPairing.isAuthClose({ code: 1000 }) === false);
ok("missing event is not a pairing problem", ZSPairing.isAuthClose(null) === false && ZSPairing.isAuthClose(undefined) === false);
