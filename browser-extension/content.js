// Runs only on the TWB webmanager (match pattern is baked in at download).
// Marks the page so it knows the extension is installed, and relays
// "open game" requests from the dashboard to the service worker.
// Runs at document_start so page scripts can test the attribute inline; the
// ready ping covers the case where the injection lands late anyway.
document.documentElement.setAttribute("data-twb-ext", "1");
window.addEventListener("DOMContentLoaded", function () {
	window.postMessage({ type: "twb-ext-ready" }, "*");
});

window.addEventListener("message", function (ev) {
	if (ev.source !== window || !ev.data) return;
	if (ev.data.type === "twb-open-game") {
		chrome.runtime.sendMessage({ type: "twb-open-game", world: ev.data.world || "" });
		return;
	}
	// Handing the browser's own session to the bot: the page needs the result
	// back, so relay the reply as a message it can listen for.
	if (ev.data.type === "twb-send-session") {
		chrome.runtime.sendMessage(
			{ type: "twb-send-session", world: ev.data.world || "" },
			function (resp) {
				window.postMessage({
					type: "twb-send-session-result",
					ok: !!(resp && resp.ok),
					host: resp && resp.host,
					portal: !!(resp && resp.portal),
					error: (resp && resp.error) || (chrome.runtime.lastError && chrome.runtime.lastError.message) || "extension did not answer"
				}, "*");
			}
		);
	}
});
