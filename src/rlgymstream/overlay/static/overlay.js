/**
 * RLGymStream Overlay – live update via SSE.
 *
 * All overlay pages source this script.  It opens an EventSource
 * to /api/events and dispatches the latest state to page-specific
 * render functions that are expected to exist on each page.
 */

(function () {
    "use strict";

    let currentState = null;

    function connect() {
        const evtSource = new EventSource("/api/events");

        evtSource.addEventListener("state", function (e) {
            try {
                currentState = JSON.parse(e.data);
                if (typeof window.renderState === "function") {
                    window.renderState(currentState);
                }
            } catch (err) {
                console.error("Failed to parse state:", err);
            }
        });

        evtSource.onerror = function () {
            console.warn("SSE connection lost, reconnecting in 3s…");
            evtSource.close();
            setTimeout(connect, 3000);
        };
    }

    // Also do an initial fetch so we have data immediately
    fetch("/api/state")
        .then(r => r.json())
        .then(state => {
            currentState = state;
            if (typeof window.renderState === "function") {
                window.renderState(state);
            }
        })
        .catch(err => console.error("Initial fetch failed:", err));

    connect();

    // Expose for debugging
    window.getOverlayState = function () { return currentState; };
})();

/* ── Helper utilities ──────────────────────────────────────── */

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function formatRating(r) {
    return r != null ? r.toFixed(1) : "—";
}

function formatMMR(mmr) {
    return mmr != null ? String(mmr) : "—";
}

function rankClass(rank) {
    if (rank === 1) return "gold";
    if (rank === 2) return "silver";
    if (rank === 3) return "bronze";
    return "";
}

