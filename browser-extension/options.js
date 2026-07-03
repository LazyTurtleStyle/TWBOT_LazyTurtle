const serverInput = document.getElementById("server");
const worldInput = document.getElementById("world");
const status = document.getElementById("status");

chrome.storage.sync.get({ server: "http://localhost:5000", world: "" }, function (items) {
	serverInput.value = items.server;
	worldInput.value = items.world;
});

document.getElementById("save").addEventListener("click", function () {
	chrome.storage.sync.set({
		server: serverInput.value.trim(),
		world: worldInput.value.trim()
	}, function () {
		status.textContent = "Saved ✓";
		setTimeout(function () { status.textContent = ""; }, 2000);
	});
});
