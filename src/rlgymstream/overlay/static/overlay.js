/**
 * RLGymStream Overlay – live update via SSE.
 *
 * All overlay pages source this script.  It opens an EventSource
 * to /api/events and dispatches the latest state to page-specific
 * render functions that are expected to exist on each page.
 *
 * A second EventSource connects to /api/live_events for high-frequency
 * Stats API data (boost, speed, stats, event ticker).
 */

(function () {
    "use strict";

    let currentState = null;
    let currentLiveStats = null;

    function connectState() {
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
            setTimeout(connectState, 3000);
        };
    }

    function connectLiveStats() {
        const evtSource = new EventSource("/api/live_events");

        evtSource.addEventListener("live_stats", function (e) {
            try {
                currentLiveStats = JSON.parse(e.data);
                if (typeof window.renderLiveStats === "function") {
                    window.renderLiveStats(currentLiveStats);
                }
            } catch (err) {
                console.error("Failed to parse live stats:", err);
            }
        });

        evtSource.onerror = function () {
            console.warn("Live stats SSE lost, reconnecting in 3s…");
            evtSource.close();
            setTimeout(connectLiveStats, 3000);
        };
    }

    // Initial fetch for state
    fetch("/api/state")
        .then(r => r.json())
        .then(state => {
            currentState = state;
            if (typeof window.renderState === "function") {
                window.renderState(state);
            }
        })
        .catch(err => console.error("Initial fetch failed:", err));

    connectState();
    connectLiveStats();

    // Expose for debugging
    window.getOverlayState = function () { return currentState; };
    window.getLiveStats = function () { return currentLiveStats; };
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

/**
 * Convert Stats API speed to display km/h.
 * The Speed field is already in km/h — just round it.
 */
function uuToKmh(speed) {
    return Math.round(speed);
}

/**
 * Format match clock seconds into M:SS.
 */
function formatClock(seconds, isOvertime) {
    if (isOvertime) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return `+${m}:${String(s).padStart(2, "0")}`;
    }
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
}
