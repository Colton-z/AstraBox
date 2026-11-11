// The harness's web page as an AstraBox Assistant shows it, inside the console.
//
// The Assistant is used through the bundle's panel, so the page opens on it rather than
// on a new Conversation. The harness keeps no selected panel across reloads and has no
// address for one; `ctx.layout.selectPanel` is its own way to choose. Plugins register in
// no fixed order and selecting a panel that is not registered yet throws, so the selection
// is tried until the bundle's panel exists.
//
// The page shows the console's language. It announces itself to the console, which
// answers with its language now and again whenever it changes; the page applies it with
// the harness's own `ctx.locale.setLocale`, and applies it again when the harness's stored
// preference, read after the page loads, or the page's own language setting replaces it.
window.__ModuleLoader__.load({ id: "@astrabox/dsh-panel-landing", factory: (require) => {
	var module = { exports: {} };
	var exports = module.exports;
	const PANEL = "dsh-superagent";
	const ATTEMPTS = 200;
	const INTERVAL_MS = 50;
	// Live composition can apply the plugin again; only the page's first load lands.
	let landed = false;
	exports.inject = ["layout", "locale"];
	exports.apply = (ctx) => {
		if (landed) return;
		landed = true;
		let wanted = "";
		const follow = () => {
			const snapshot = ctx.locale.getLocale();
			if (!wanted || snapshot.active === wanted) return;
			if (!snapshot.locales.some((locale) => locale.id === wanted)) return;
			ctx.locale.setLocale(wanted);
		};
		window.addEventListener("message", (event) => {
			if (event.source !== window.parent || event.data?.type !== "astrabox.locale") return;
			wanted = String(event.data.locale || "");
			follow();
		});
		ctx.locale.subscribe(follow);
		if (window.parent !== window) window.parent.postMessage({ type: "astrabox.panel-ready" }, "*");
		let attempt = 0;
		const land = () => {
			try {
				ctx.layout.selectPanel(PANEL);
			} catch (error) {
				attempt += 1;
				if (attempt < ATTEMPTS) {
					setTimeout(land, INTERVAL_MS);
					return;
				}
				console.error("[astrabox-panel-landing] the dsh-superagent panel was never registered", error);
			}
		};
		land();
	};
	return module.exports;
} });
