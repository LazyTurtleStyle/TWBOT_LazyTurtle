// These defaults are rewritten by the webmanager when it serves the zip,
// so a freshly downloaded extension works without touching Options.
const DEFAULT_SERVER = "http://localhost:5000";
const DEFAULT_WORLD = "";

function flashBadge(text, color) {
	chrome.action.setBadgeText({ text: text });
	chrome.action.setBadgeBackgroundColor({ color: color });
	setTimeout(function () { chrome.action.setBadgeText({ text: "" }); }, 4000);
}

async function restoreSession(base, world) {
	try {
		let exportUrl = base + "/app/tw-cookies-export";
		if (world) exportUrl += "?world=" + encodeURIComponent(world);
		const resp = await fetch(exportUrl);
		if (!resp.ok) throw new Error("webmanager returned HTTP " + resp.status);
		const cookies = await resp.json();
		if (!cookies.length) throw new Error("no cookies in bot session");

		let gameDomain = null;
		for (const c of cookies) {
			const domain = (c.domain || "").replace(/^\./, "");
			if (!domain) continue;
			if (!domain.startsWith("www.")) gameDomain = domain;
			await chrome.cookies.set({
				url: "https://" + domain + (c.path || "/"),
				name: c.name,
				value: c.value,
				path: c.path || "/",
				secure: true,
				httpOnly: !!c.httpOnly,
				sameSite: "no_restriction",
				expirationDate: c.expirationDate
			});
		}

		const url = gameDomain
			? "https://" + gameDomain + "/game.php"
			: "https://www.tribalwars.nl";
		chrome.tabs.create({ url: url });
		flashBadge("OK", "#93A56A");
	} catch (e) {
		console.error("TWB session restore failed:", e);
		flashBadge("ERR", "#D24B3E");
	}
}

// --- The other direction: hand this browser's session back to the bot. ---
// Used when the bot itself got logged out and there is no PC around to read
// cookies off. The game sid is HttpOnly, so only chrome.cookies can see it.
const TW_TLD = "tribalwars.nl";

async function cookieHeaderFor(url) {
	const cookies = await chrome.cookies.getAll({ url: url });
	return cookies.map(function (c) { return c.name + "=" + c.value; }).join("; ");
}

// The world host this browser is logged into, e.g. nl115.tribalwars.nl.
// Prefer the world the extension was built for, else any non-portal host.
async function findGameHost(world) {
	const cookies = await chrome.cookies.getAll({ domain: TW_TLD });
	const hosts = new Set();
	for (const c of cookies) {
		const d = (c.domain || "").replace(/^\./, "");
		if (d && d !== TW_TLD && !d.startsWith("www.")) hosts.add(d);
	}
	if (world && hosts.has(world + "." + TW_TLD)) return world + "." + TW_TLD;
	for (const h of hosts) return h;
	// Nothing host-scoped found; fall back to the baked world so the caller
	// still gets a real error from the cookie lookup rather than a blank one.
	return world ? world + "." + TW_TLD : null;
}

async function postForm(url, field, value) {
	const body = new URLSearchParams();
	body.set(field, value);
	const resp = await fetch(url, { method: "POST", body: body });
	if (!resp.ok) throw new Error("webmanager returned HTTP " + resp.status);
	const data = await resp.json();
	if (!data || !data.ok) throw new Error((data && data.error) || "the bot rejected those cookies");
	return data;
}

async function sendSession(base, world) {
	const suffix = world ? "?world=" + encodeURIComponent(world) : "";

	// TribalWars rotates the sid on every request and the old one dies with it
	// (see core/instance_lock.py). An open game tab polls on its own, so it
	// would rotate the session out from under the bot seconds after handover.
	// Close every TW tab first, then let any in-flight request settle before
	// reading the jar, so what we send is the last sid anyone minted.
	const tabs = await chrome.tabs.query({ url: "*://*." + TW_TLD + "/*" });
	for (const t of tabs) {
		try { await chrome.tabs.remove(t.id); } catch (e) { /* already gone */ }
	}
	if (tabs.length) await new Promise(function (r) { setTimeout(r, 1500); });

	const host = await findGameHost(world);
	if (!host) throw new Error("no TribalWars cookies in this browser - log into the world first");

	const gameHeader = await cookieHeaderFor("https://" + host + "/game.php");
	if (!gameHeader) throw new Error("not logged into " + host + " in this browser");
	await postForm(base + "/app/session/set" + suffix, "session", gameHeader);

	// Portal cookies are a bonus: they only matter for the restore direction
	// later on, so a missing portal login must not fail the world handover.
	let portal = false;
	try {
		const portalHeader = await cookieHeaderFor("https://www." + TW_TLD + "/");
		if (portalHeader) {
			await postForm(base + "/app/portal-cookies/set" + suffix, "cookies", portalHeader);
			portal = true;
		}
	} catch (e) {
		console.warn("TWB portal cookie handover failed:", e);
	}
	return { host: host, portal: portal };
}

// Toolbar click: use the configured (or baked-in) server and world.
chrome.action.onClicked.addListener(async () => {
	const { server, world } = await chrome.storage.sync.get({ server: DEFAULT_SERVER, world: DEFAULT_WORLD });
	let base = server.trim().replace(/\/+$/, "");
	if (base && !/^https?:\/\//i.test(base)) base = "http://" + base;
	restoreSession(base, world);
});

// "Open game" button on the webmanager dashboard (relayed by content.js).
// The requesting page IS the webmanager, so its origin is the server address.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
	if (!msg || !sender.tab || !sender.tab.url) return;
	const origin = new URL(sender.tab.url).origin;
	if (msg.type === "twb-open-game") {
		restoreSession(origin, msg.world || "");
		return;
	}
	if (msg.type === "twb-send-session") {
		sendSession(origin, msg.world || "").then(
			function (r) { sendResponse({ ok: true, host: r.host, portal: r.portal }); },
			function (e) { sendResponse({ ok: false, error: String(e.message || e) }); }
		);
		return true;  // keep the channel open for the async reply
	}
});
