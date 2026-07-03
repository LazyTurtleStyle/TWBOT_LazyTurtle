// Runs only on the TWB webmanager (match pattern is baked in at download).
// Marks the page so it knows the extension is installed, and relays
// "open game" requests from the dashboard to the service worker.
document.documentElement.setAttribute("data-twb-ext", "1");

window.addEventListener("message", function (ev) {
	if (ev.source !== window || !ev.data || ev.data.type !== "twb-open-game") return;
	chrome.runtime.sendMessage({ type: "twb-open-game", world: ev.data.world || "" });
});
