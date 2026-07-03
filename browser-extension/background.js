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

// Toolbar click: use the configured (or baked-in) server and world.
chrome.action.onClicked.addListener(async () => {
	const { server, world } = await chrome.storage.sync.get({ server: DEFAULT_SERVER, world: DEFAULT_WORLD });
	let base = server.trim().replace(/\/+$/, "");
	if (base && !/^https?:\/\//i.test(base)) base = "http://" + base;
	restoreSession(base, world);
});

// "Open game" button on the webmanager dashboard (relayed by content.js).
// The requesting page IS the webmanager, so its origin is the server address.
chrome.runtime.onMessage.addListener((msg, sender) => {
	if (msg && msg.type === "twb-open-game" && sender.tab && sender.tab.url) {
		restoreSession(new URL(sender.tab.url).origin, msg.world || "");
	}
});
