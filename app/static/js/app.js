"use strict";

const SFDC_BASE = "https://nutanix.lightning.force.com/lightning/r";

// Append `&region=APJ|EMEA|AMER|Unknown` when the active filter isn't "all".
function _regionQs(region) {
    return region && region !== "all" ? `&region=${encodeURIComponent(region)}` : "";
}

const API = {
    topRisks: (limit = 50, region = "all") => fetch(`/api/top-risks?limit=${limit}${_regionQs(region)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }),
    search: (q) => fetch(`/api/search?q=${encodeURIComponent(q)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }),
    fastSearch: (q, limit = 50, region = "all") => fetch(`/api/accounts/search?q=${encodeURIComponent(q)}&limit=${limit}${_regionQs(region)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }),
    accountsList: (offset = 0, limit = 100, region = "all") => fetch(`/api/accounts?offset=${offset}&limit=${limit}${_regionQs(region)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }),
    regionCounts: () => fetch("/api/regions/counts").then(r => r.ok ? r.json() : { counts: {}, total: 0 }).catch(() => ({ counts: {}, total: 0 })),
    accountStats: () => fetch("/api/accounts/stats").then(r => r.json()),
    account: (id, name, refresh = false) =>
        fetch(`/api/account/${id}?account_name=${encodeURIComponent(name)}&refresh=${refresh}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    licenses: (id, name) =>
        fetch(`/api/licenses/${id}?account_name=${encodeURIComponent(name)}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    emailDraft: (id, name, includeLicense = false) =>
        fetch(`/api/account/${id}/email-draft?account_name=${encodeURIComponent(name)}&include_license=${includeLicense}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    contacts: (id, name) =>
        fetch(`/api/account/${id}/contacts?account_name=${encodeURIComponent(name)}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    sendEmail: (payload) =>
        fetch("/api/email/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }).then(r => {
            if (!r.ok) return r.json().then(d => { throw new Error(d.detail || `HTTP ${r.status}`); });
            return r.json();
        }),
    createDraft: (payload) =>
        fetch("/api/email/draft", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }).then(r => {
            if (!r.ok) return r.json().then(d => { throw new Error(d.detail || `HTTP ${r.status}`); });
            return r.json();
        }),
    outlookStatus: () => fetch("/api/outlook/status").then(r => r.json()),
    myAccounts: (username, limit = 50) =>
        fetch(`/api/my-accounts?username=${encodeURIComponent(username)}&limit=${limit}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    infrastructure: (id) =>
        fetch(`/api/infrastructure/${id}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    cxmRoster: (region = "all") => fetch(
        region && region !== "all"
            ? `/api/cxm/roster?region=${encodeURIComponent(region)}`
            : "/api/cxm/roster"
    ).then(r => r.json()),
    accountsByCxm: (cxm, offset = 0, limit = 100, region = "all") =>
        fetch(`/api/accounts/by-cxm?cxm=${encodeURIComponent(cxm)}&offset=${offset}&limit=${limit}${_regionQs(region)}`).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        }),
    financials: (id) => fetch(`/api/account/${id}/financials`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json();
    }),
    aiAdvisory: (id, name) => fetch(`/api/account/${id}/ai-advisory?account_name=${encodeURIComponent(name)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json();
    }),
    status: () => fetch("/api/status").then(r => r.json()),
    riskCategories: () => fetch("/api/risk-categories").then(r => r.json()),
    customRisks: (accountId) => fetch(`/api/account/${accountId}/custom-risks`).then(r => r.json()),
    addCustomRisk: (accountId, body) =>
        fetch(`/api/account/${accountId}/custom-risks`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
    updateCustomRisk: (accountId, riskId, body) =>
        fetch(`/api/account/${accountId}/custom-risks/${riskId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
    deleteCustomRisk: (accountId, riskId) =>
        fetch(`/api/account/${accountId}/custom-risks/${riskId}`, { method: "DELETE" }).then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json();
        }),
};

let allAccounts = [];
let activeTab = "all";
let currentSort = { key: "overall_score", dir: "desc" };
let currentSearchTerm = "";
let outlookConnected = false;
let outlookAuthUrl = null;
let outlookToken = null;
let outlookMode = "disabled";

let currentView = "directory";
let dirPage = { offset: 0, limit: 100, total: 0 };
let dirAccounts = [];
let dirSearchResults = null;
let totalAccountCount = 0;

let currentAccountData = null;
let currentAccountId = null;
let currentAccountName = null;
let currentSection = null;
let riskCategoriesCache = null;
let activeChartInstances = [];

let cxmRosterCache = null;

// Region quick-filter (APJ / EMEA / AMER / Unknown / all)
let currentRegion = "all";

// ── Initialisation ──────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    setupSearch();
    setupRiskTabs();
    setupSorting();
    setupMyAccounts();
    setupKeyboard();
    setupOutlookListener();
    setupRouting();
    setupCxmSearch();
    setupRegionFilter();
    loadStatus();
    loadOutlookStatus();
    loadAccountIndex();
    loadPortfolioRisk();
    loadRegionCounts();
    setInterval(() => { loadStatus(); loadPortfolioRisk(); }, 60000);
    // Safety net: never let the cold-start overlay get stuck.
    setTimeout(hideWakeOverlay, 12000);
});

function hideWakeOverlay() {
    const el = document.getElementById("wake-overlay");
    if (!el || el.classList.contains("hide")) return;
    el.classList.add("hide");
    setTimeout(() => el.remove(), 500);
}

// ── Region quick-filter ────────────────────────────────────────────────

function setupRegionFilter() {
    // Chips already wire onclick="setRegion(...)" in the markup; this hook
    // exists so we can extend behaviour (e.g. keyboard nav) in one place.
}

function setRegion(region) {
    if (region === currentRegion) return;
    currentRegion = region;
    document.querySelectorAll(".region-chip").forEach(c => {
        c.classList.toggle("active", c.dataset.region === region);
    });
    // Refresh whichever view is currently active. The CXM detail drill-in
    // is treated as part of the CXM-portfolio view, so re-rendering the
    // roster from scratch is the simplest correct behaviour.
    if (currentView === "directory") {
        if (currentSearchTerm && currentSearchTerm.length >= 2) {
            fastSearch(currentSearchTerm);
        } else {
            loadDirectoryPage(0);
        }
    } else if (currentView === "top-risks") {
        loadTopRisks();
    } else if (currentView === "cxm-portfolio") {
        loadCxmPortfolio();
    }
}

async function loadRegionCounts() {
    try {
        const data = await API.regionCounts();
        const counts = data.counts || {};
        const total = data.total || 0;
        document.querySelectorAll(".region-count").forEach(span => {
            const r = span.dataset.regionCount;
            const v = r === "all" ? total : (counts[r] || 0);
            span.textContent = v > 0 ? v.toLocaleString() : "";
        });
    } catch (e) {
        // Non-fatal — chips still work without badge counts.
    }
}

function regionPillHtml(region) {
    const r = region || "Unknown";
    const cls = ["APJ", "EMEA", "AMER", "Unknown"].includes(r) ? r : "Unknown";
    return `<span class="region-pill region-${cls}">${escHtml(r)}</span>`;
}

// ── Page Routing ────────────────────────────────────────────────────────

function setupRouting() {
    window.addEventListener("popstate", () => handleRoute());
    handleRoute();
}

function handleRoute() {
    const hash = window.location.hash || "";
    const accountMatch = hash.match(/^#\/account\/([^/]+)$/);
    const sectionMatch = hash.match(/^#\/account\/([^/]+)\/(.+)$/);

    if (sectionMatch) {
        const id = decodeURIComponent(sectionMatch[1]);
        const section = decodeURIComponent(sectionMatch[2]);
        if (currentAccountData && currentAccountId === id) {
            showPage("section-view");
            currentSection = section;
            renderSectionView(section, currentAccountData);
        } else {
            navigateToAccount(id, "");
        }
    } else if (accountMatch) {
        const id = decodeURIComponent(accountMatch[1]);
        if (currentAccountData && currentAccountId === id) {
            showPage("account-view");
        } else {
            navigateToAccount(id, "");
        }
    } else {
        showPage("dashboard-view");
    }
}

function showPage(pageId) {
    document.querySelectorAll(".page-view").forEach(p => p.classList.remove("active"));
    const page = document.getElementById(pageId);
    if (page) page.classList.add("active");
    destroyCharts();
    window.scrollTo(0, 0);
}

function navigateTo(hash) {
    window.history.pushState(null, "", hash);
    handleRoute();
}

function showDashboard() {
    navigateTo("#/");
}

function showAccountView(id, name) {
    currentAccountId = id;
    currentAccountName = name;
    navigateTo(`#/account/${encodeURIComponent(id)}`);
}

function showSectionView(section) {
    currentSection = section;
    navigateTo(`#/account/${encodeURIComponent(currentAccountId)}/${encodeURIComponent(section)}`);
}

async function navigateToAccount(id, name) {
    currentAccountId = id;
    currentAccountName = name;
    showPage("account-view");

    const header = document.getElementById("account-header");
    header.innerHTML = `<div class="loading-spinner"><div class="spinner"></div>Loading account data\u2026</div>`;
    document.getElementById("account-stats-row").innerHTML = "";
    document.getElementById("section-tile-grid").innerHTML = "";

    try {
        const data = await API.account(id, name || id);
        currentAccountData = data;
        currentAccountName = data.account_name || name;
        renderAccountView(data);
        window.history.replaceState(null, "", `#/account/${encodeURIComponent(id)}`);
    } catch (err) {
        header.innerHTML = `<div class="empty-state"><p>Failed to load account data.</p>
            <button class="btn btn-secondary btn-sm" onclick="showDashboard()">Back to Dashboard</button></div>`;
        console.error(err);
    }
}

function destroyCharts() {
    activeChartInstances.forEach(c => { try { c.destroy(); } catch (_) {} });
    activeChartInstances = [];
}

// ── Keyboard shortcuts ──────────────────────────────────────────────────

function setupKeyboard() {
    document.addEventListener("keydown", e => {
        if (e.key === "Escape") {
            const hash = window.location.hash || "";
            if (hash.includes("/account/") && hash.split("/").length > 3) {
                showAccountView(currentAccountId, currentAccountName);
            } else if (hash.includes("/account/")) {
                showDashboard();
            }
        }
        if (e.key === "/" && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
            e.preventDefault();
            const searchInput = document.getElementById("search-input");
            if (searchInput) searchInput.focus();
        }
    });
}

// ── Status ──────────────────────────────────────────────────────────────

async function loadStatus() {
    try {
        const [st, fresh] = await Promise.all([
            API.status(),
            fetch("/api/sync/freshness").then(r => r.ok ? r.json() : {latest_runs: {}, snapshot_freshness: {}}).catch(() => ({latest_runs: {}, snapshot_freshness: {}})),
        ]);
        const fmtAge = (mins) => {
            if (mins == null || mins < 0) return "never synced";
            if (mins < 60) return `${mins}m ago`;
            const h = Math.floor(mins / 60), m = mins % 60;
            return h < 24 ? `${h}h ${m}m ago` : `${Math.floor(h/24)}d ${h%24}h ago`;
        };
        const minsSince = (iso) => {
            if (!iso) return -1;
            const t = new Date(iso); if (isNaN(t)) return -1;
            return Math.max(0, Math.floor((Date.now() - t.getTime()) / 60000));
        };
        const pills = document.querySelectorAll(".source-pill");
        pills.forEach(pill => {
            const src = pill.dataset.source;
            const dot = pill.querySelector(".dot");
            if (src && st.connectors) {
                const state = st.connectors[src];
                const cls = state === "live" ? "active" : state === "expired" ? "expired" : state === "demo" ? "demo" : "inactive";
                dot.className = "dot " + cls;
                const latest = (fresh.latest_runs || {})[src];
                const snap = (fresh.snapshot_freshness || {})[src];
                const lastIso = latest ? latest.started_at : snap;
                const mins = minsSince(lastIso);
                let tip = `${src}: ${state} · last sync ${fmtAge(mins)}`;
                if (latest && latest.accounts_processed != null) {
                    tip += ` · ${latest.accounts_processed} accts (${latest.status})`;
                }
                if (state === "expired") tip += " — re-run login script";
                pill.title = tip;
            }
        });
    } catch (e) {
        console.warn("Could not load status", e);
    }
}

// ── Data Loading ────────────────────────────────────────────────────────

async function loadAccountIndex() {
    const pollIndex = async () => {
        try {
            const stats = await API.accountStats();
            totalAccountCount = stats.total_accounts;
            document.getElementById("stat-total").textContent = totalAccountCount.toLocaleString();
            if (stats.index_ready && totalAccountCount > 0) {
                document.getElementById("stat-index").textContent = "Ready";
                document.getElementById("stat-index").style.color = "var(--risk-low)";
                document.getElementById("search-input").placeholder =
                    "Search all " + totalAccountCount.toLocaleString() + " accounts\u2026";
                loadDirectoryPage(0);
                loadRegionCounts();
                return;
            }
        } catch (e) { /* ignore */ }
        document.getElementById("stat-index").textContent = "Loading\u2026";
        setTimeout(pollIndex, 1500);
    };
    pollIndex();
}

async function loadDirectoryPage(offset) {
    const tbody = document.getElementById("directory-tbody");
    tbody.innerHTML = `<tr><td colspan="6"><div class="loading-spinner"><div class="spinner"></div>Loading\u2026</div></td></tr>`;
    try {
        const data = await API.accountsList(offset, dirPage.limit, currentRegion);
        dirPage.total = data.total;
        dirPage.offset = data.offset;
        dirAccounts = data.accounts;
        totalAccountCount = data.total;
        document.getElementById("stat-total").textContent = data.total.toLocaleString();
        renderDirectoryTable(dirAccounts);
        updatePagination();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><p>Failed to load accounts.</p></div></td></tr>`;
        console.error(err);
    }
}

async function fastSearch(term) {
    if (!term || term.length < 2) {
        dirSearchResults = null;
        loadDirectoryPage(0);
        return;
    }
    const tbody = document.getElementById("directory-tbody");
    tbody.innerHTML = `<tr><td colspan="6"><div class="loading-spinner"><div class="spinner"></div>Searching\u2026</div></td></tr>`;
    try {
        const t0 = performance.now();
        const results = await API.fastSearch(term, 100, currentRegion);
        const elapsed = (performance.now() - t0).toFixed(0);
        dirSearchResults = results;
        document.getElementById("stat-showing").textContent = `${results.length} results (${elapsed}ms)`;
        renderDirectoryTable(results, term);
        document.getElementById("pagination-bar").style.display = "none";
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><p>Search failed.</p></div></td></tr>`;
        console.error(err);
    }
}

async function loadTopRisks() {
    if (currentView !== "top-risks") {
        switchView("top-risks");
        return;
    }
    showTableLoading();
    try {
        allAccounts = await API.topRisks(50, currentRegion);
        currentSearchTerm = "";
        document.getElementById("search-input").value = "";
        refresh();
    } catch (err) {
        showTableError("Failed to load risk data. Check connector configuration.");
        console.error(err);
    }
}

async function searchAccounts(term) {
    showTableLoading();
    try {
        allAccounts = await API.search(term);
        currentSearchTerm = term;
        refresh();
    } catch (err) {
        showTableError("Search failed. Please try again.");
        console.error(err);
    }
}

async function loadAccountDetail(id, name) {
    navigateToAccount(id, name);
}

function refresh() {
    const filtered = filterByTab(sortAccounts(allAccounts));
    renderStats(allAccounts);
    renderTable(filtered);
}

// ── View switching ──────────────────────────────────────────────────────

function switchView(view) {
    currentView = view;
    document.querySelectorAll(".tab-bar:first-of-type .tab, [data-view]").forEach(t => t.classList.remove("active"));
    const activeBtn = document.querySelector(`[data-view="${view}"]`);
    if (activeBtn) activeBtn.classList.add("active");

    const dirContainer = document.getElementById("directory-table-container");
    const riskContainer = document.getElementById("risk-table-container");
    const riskTabs = document.getElementById("risk-tabs");
    const cxmContainer = document.getElementById("cxm-portfolio-container");

    dirContainer.style.display = "none";
    riskContainer.style.display = "none";
    riskTabs.style.display = "none";
    cxmContainer.style.display = "none";

    if (view === "directory") {
        dirContainer.style.display = "";
        document.getElementById("stat-mode").textContent = "Directory";
        document.getElementById("search-input").placeholder = "Search all " + totalAccountCount.toLocaleString() + " accounts\u2026";
        if (dirSearchResults === null) {
            loadDirectoryPage(dirPage.offset);
        } else {
            renderDirectoryTable(dirSearchResults, currentSearchTerm);
        }
    } else if (view === "cxm-portfolio") {
        cxmContainer.style.display = "";
        document.getElementById("stat-mode").textContent = "CXM Portfolio";
        document.getElementById("search-input").placeholder = "Search accounts\u2026";
        loadCxmPortfolio();
    } else {
        riskContainer.style.display = "";
        riskTabs.style.display = "";
        document.getElementById("stat-mode").textContent = "Top Risks";
        document.getElementById("search-input").placeholder = "Search accounts\u2026";
        if (!allAccounts.length) loadTopRisks();
        else refresh();
    }
}

// ── Directory table rendering ───────────────────────────────────────────

function renderDirectoryTable(accounts, highlight) {
    hideWakeOverlay();
    const tbody = document.getElementById("directory-tbody");
    if (!accounts || !accounts.length) {
        tbody.innerHTML = `<tr><td colspan="6">
            <div class="empty-state"><div class="icon">&#128269;</div><p>No accounts found.</p></div></td></tr>`;
        document.getElementById("stat-showing").textContent = "0";
        return;
    }
    document.getElementById("stat-showing").textContent = highlight
        ? `${accounts.length} results`
        : `${dirPage.offset + 1}\u2013${Math.min(dirPage.offset + accounts.length, dirPage.total)} of ${dirPage.total.toLocaleString()}`;

    tbody.innerHTML = accounts.map(a => {
        const name = highlight ? highlightText(a.name, highlight) : escHtml(a.name);
        const cxm = a.cxm ? escHtml(a.cxm) : `<span class="text-muted">\u2014</span>`;
        return `
        <tr onclick="loadAccountDetail('${a.id}', '${escAttr(a.name)}')">
            <td><strong>${name}</strong></td>
            <td><span class="cell-meta">${escHtml(a.industry || "\u2014")}</span></td>
            <td><span class="cell-meta">${escHtml(a.type || "")}</span></td>
            <td>${regionPillHtml(a.region)}</td>
            <td><span class="cell-meta">${escHtml(a.owner || "\u2014")}</span></td>
            <td><span class="cell-meta">${cxm}</span></td>
        </tr>`;
    }).join("");
}

function highlightText(text, query) {
    if (!query) return escHtml(text);
    const safe = escHtml(text);
    const tokens = query.toLowerCase().split(/\s+/).filter(Boolean);
    let result = safe;
    for (const tok of tokens) {
        const re = new RegExp(`(${tok.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
        result = result.replace(re, '<span class="search-highlight">$1</span>');
    }
    return result;
}

function updatePagination() {
    const bar = document.getElementById("pagination-bar");
    bar.style.display = "flex";
    const start = dirPage.offset + 1;
    const end = Math.min(dirPage.offset + dirPage.limit, dirPage.total);
    document.getElementById("pagination-info").textContent =
        `${start.toLocaleString()}\u2013${end.toLocaleString()} of ${dirPage.total.toLocaleString()} accounts`;
    document.getElementById("btn-prev").disabled = dirPage.offset === 0;
    document.getElementById("btn-next").disabled = dirPage.offset + dirPage.limit >= dirPage.total;
}

function nextPage() {
    if (dirPage.offset + dirPage.limit < dirPage.total) {
        loadDirectoryPage(dirPage.offset + dirPage.limit);
    }
}

function prevPage() {
    if (dirPage.offset > 0) {
        loadDirectoryPage(Math.max(0, dirPage.offset - dirPage.limit));
    }
}

function sortDirectory(key) {}

function refreshView() {
    if (currentView === "directory") {
        dirSearchResults = null;
        document.getElementById("search-input").value = "";
        currentSearchTerm = "";
        loadAccountIndex();
    } else if (currentView === "cxm-portfolio") {
        cxmRosterCache = null;
        loadCxmPortfolio();
    } else {
        loadTopRisks();
    }
}

// ── CXM Portfolio ───────────────────────────────────────────────────────

function setupCxmSearch() {
    const input = document.getElementById("cxm-search-input");
    if (!input) return;
    let debounce;
    input.addEventListener("input", () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
            const q = input.value.trim().toLowerCase();
            if (cxmRosterCache) renderCxmRosterGrid(cxmRosterCache, q);
        }, 150);
    });
}

async function loadCxmPortfolio() {
    const grid = document.getElementById("cxm-roster-grid");
    grid.innerHTML = `<div class="loading-spinner"><div class="spinner"></div>Loading CXM roster\u2026</div>`;
    document.getElementById("cxm-roster-view").style.display = "";
    document.getElementById("cxm-detail-container").style.display = "none";
    try {
        const cxms = await API.cxmRoster(currentRegion);
        cxmRosterCache = cxms;
        renderCxmRosterGrid(cxms, "");
    } catch (err) {
        grid.innerHTML = `<div class="cxm-roster-empty">Failed to load CXM roster.</div>`;
        console.error(err);
    }
}

function renderCxmRosterGrid(cxms, filter) {
    const grid = document.getElementById("cxm-roster-grid");
    const filtered = filter
        ? cxms.filter(c => c.name.toLowerCase().includes(filter))
        : cxms;

    if (!filtered.length) {
        grid.innerHTML = `<div class="cxm-roster-empty">No CXMs match &ldquo;${escHtml(filter)}&rdquo;</div>`;
        document.getElementById("stat-showing").textContent = "0 CXMs";
        return;
    }

    document.getElementById("stat-showing").textContent = `${filtered.length} CXM${filtered.length !== 1 ? "s" : ""}`;

    grid.innerHTML = filtered.map(c => {
        const progTags = Object.entries(c.programs || {})
            .sort((a, b) => b[1] - a[1])
            .map(([prog, cnt]) => `<span class="cxm-program-tag"><strong>${cnt}</strong> ${escHtml(prog)}</span>`)
            .join("");
        return `
        <div class="cxm-card" onclick="drillIntoCxm('${escAttr(c.name)}', '${escAttr(c.email || "")}', ${c.account_count})">
            <div class="cxm-card-name">${escHtml(c.name)}</div>
            <div class="cxm-card-email">${escHtml(c.email || "\u2014")}</div>
            <div class="cxm-card-count"><strong>${c.account_count}</strong> account${c.account_count !== 1 ? "s" : ""}</div>
            <div class="cxm-card-programs">${progTags || '<span class="cxm-program-tag">No program data</span>'}</div>
        </div>`;
    }).join("");
}

async function drillIntoCxm(name, email, count) {
    document.getElementById("cxm-roster-view").style.display = "none";
    const detailContainer = document.getElementById("cxm-detail-container");
    detailContainer.style.display = "";

    document.getElementById("cxm-detail-header").innerHTML = `
        <button class="cxm-detail-back" onclick="backToCxmRoster()" title="Back to CXM roster">&#9664; Back</button>
        <div class="cxm-detail-info">
            <div class="cxm-detail-name">${escHtml(name)}</div>
            <div class="cxm-detail-email">${escHtml(email || "\u2014")}</div>
        </div>
        <div class="cxm-detail-count">${count} account${count !== 1 ? "s" : ""}</div>`;

    const tbody = document.getElementById("cxm-detail-tbody");
    tbody.innerHTML = `<tr><td colspan="6"><div class="loading-spinner"><div class="spinner"></div>Loading accounts\u2026</div></td></tr>`;

    try {
        const data = await API.accountsByCxm(name, 0, 500, currentRegion);
        if (!data.accounts || !data.accounts.length) {
            tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><div class="icon">&#128269;</div><p>No accounts found.</p></div></td></tr>`;
            return;
        }
        tbody.innerHTML = data.accounts.map(a => `
            <tr onclick="loadAccountDetail('${a.id}', '${escAttr(a.name)}')">
                <td><strong>${escHtml(a.name)}</strong></td>
                <td><span class="cell-meta">${escHtml(a.industry || "\u2014")}</span></td>
                <td><span class="cell-meta">${escHtml(a.type || "")}</span></td>
                <td>${regionPillHtml(a.region)}</td>
                <td><span class="cell-meta">${escHtml(a.owner || "\u2014")}</span></td>
                <td><span class="cell-meta">${escHtml(a.cxm_program || "\u2014")}</span></td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><p>Failed to load accounts.</p></div></td></tr>`;
        console.error(err);
    }
}

function backToCxmRoster() {
    document.getElementById("cxm-detail-container").style.display = "none";
    document.getElementById("cxm-roster-view").style.display = "";
}

// ── Search ──────────────────────────────────────────────────────────────

function setupSearch() {
    const input = document.getElementById("search-input");
    let debounce;
    input.addEventListener("input", () => {
        clearTimeout(debounce);
        const delay = currentView === "directory" ? 150 : 400;
        debounce = setTimeout(() => {
            const q = input.value.trim();
            currentSearchTerm = q;
            if (currentView === "directory") {
                if (q.length >= 2) {
                    fastSearch(q);
                } else if (q.length === 0) {
                    dirSearchResults = null;
                    loadDirectoryPage(0);
                }
            } else {
                if (q.length >= 2) {
                    searchAccounts(q);
                } else if (q.length === 0) {
                    loadTopRisks();
                }
            }
        }, delay);
    });
}

// ── Tabs ────────────────────────────────────────────────────────────────

function setupRiskTabs() {
    document.querySelectorAll("#risk-tabs .tab").forEach(tab => {
        tab.addEventListener("click", () => {
            document.querySelector("#risk-tabs .tab.active")?.classList.remove("active");
            tab.classList.add("active");
            activeTab = tab.dataset.level;
            refresh();
        });
    });
}

function filterByTab(accounts) {
    if (activeTab === "all") return accounts;
    return accounts.filter(a => a.risk_level === activeTab);
}

// ── Sorting ─────────────────────────────────────────────────────────────

function setupSorting() {
    document.querySelectorAll("th.sortable").forEach(th => {
        th.addEventListener("click", () => {
            const key = th.dataset.sort;
            if (currentSort.key === key) {
                currentSort.dir = currentSort.dir === "desc" ? "asc" : "desc";
            } else {
                currentSort = { key, dir: "desc" };
            }
            document.querySelectorAll("th.sortable").forEach(h => {
                h.classList.remove("sorted", "asc", "desc");
                h.querySelector(".sort-arrow").textContent = "";
            });
            th.classList.add("sorted", currentSort.dir);
            th.querySelector(".sort-arrow").textContent = currentSort.dir === "desc" ? "\u25BC" : "\u25B2";
            refresh();
        });
    });
}

function sortAccounts(accounts) {
    const k = currentSort.key;
    const dir = currentSort.dir === "desc" ? -1 : 1;
    return [...accounts].sort((a, b) => {
        let va = getSortValue(a, k);
        let vb = getSortValue(b, k);
        if (typeof va === "string") return dir * va.localeCompare(vb);
        return dir * ((va ?? 0) - (vb ?? 0));
    });
}

function getSortValue(account, key) {
    const sf = account.salesforce || {};
    const ins = account.insights || {};
    const cs = account.cs_insights || {};
    switch (key) {
        case "account_name": return account.account_name || "";
        case "overall_score": return account.overall_score || 0;
        case "risk_level": return { critical: 4, high: 3, medium: 2, low: 1 }[account.risk_level] || 0;
        case "open_cases": return sf.open_cases ?? 0;
        case "escalated_cases": return sf.escalated_cases ?? 0;
        case "critical_alerts": return ins.total_critical_alerts ?? 0;
        case "cs_health": return cs.health_score ?? 0;
        default: return 0;
    }
}

// ── Stats ───────────────────────────────────────────────────────────────

function renderStats(accounts) {
    if (currentView === "top-risks") {
        const total = accounts.length;
        document.getElementById("stat-showing").textContent = `${total} analyzed`;
    }
}

// ── Table ───────────────────────────────────────────────────────────────

function renderTable(accounts) {
    const tbody = document.getElementById("accounts-tbody");
    if (!accounts.length) {
        tbody.innerHTML = `<tr><td colspan="8">
            <div class="empty-state"><div class="icon">&#128269;</div>
            <p>No accounts match the current filter.</p></div></td></tr>`;
        return;
    }

    tbody.innerHTML = accounts.map(a => {
        const level = a.risk_level;
        const score = a.overall_score;
        const sf = a.salesforce || {};
        const ins = a.insights || {};
        const cs = a.cs_insights || {};
        const meta = cs.account_metadata || {};
        const owner = meta.account_owner || "";
        const se = meta.systems_engineer || "";
        const ownerSe = [owner, se].filter(Boolean).join(" / ") || "\u2014";

        return `
        <tr onclick="loadAccountDetail('${a.account_id}', '${escAttr(a.account_name)}')">
            <td>
                <strong>${escHtml(a.account_name)}</strong><br>
                <span class="cell-meta">${meta.region || ""} ${meta.vertical ? "\u00b7 " + meta.vertical : ""}</span>
            </td>
            <td>
                <div class="score-bar-container">
                    <div class="score-bar"><div class="fill ${level}" style="width:${score}%"></div></div>
                    <span class="score-value" style="color:var(--risk-${level})">${score}</span>
                </div>
            </td>
            <td><span class="risk-badge ${level}">${level}</span></td>
            <td>${sf.open_cases ?? "\u2014"} ${sf.p1_cases ? '<span class="p1-tag">P1:' + sf.p1_cases + '</span>' : ''}</td>
            <td>${sf.escalated_cases ?? "\u2014"}</td>
            <td>${ins.total_critical_alerts ?? "\u2014"} ${ins.eol_exposure_count ? '<span class="eol-tag">EOL:' + ins.eol_exposure_count + '</span>' : ''}</td>
            <td>${cs.health_score ?? "\u2014"}</td>
            <td><span class="cell-meta">${escHtml(ownerSe)}</span></td>
        </tr>`;
    }).join("");
}

function showTableLoading() {
    document.getElementById("accounts-tbody").innerHTML = `<tr><td colspan="8">
        <div class="loading-spinner"><div class="spinner"></div>Analysing accounts\u2026</div></td></tr>`;
}

function showTableError(msg) {
    document.getElementById("accounts-tbody").innerHTML = `<tr><td colspan="8">
        <div class="empty-state"><div class="icon">&#9888;&#65039;</div><p>${msg}</p></div></td></tr>`;
}

// ══════════════════════════════════════════════════════════════════════════
// ACCOUNT VIEW (Page 2) — Full-screen tile grid
// ══════════════════════════════════════════════════════════════════════════

function renderAccountView(data) {
    const level = data.risk_level;
    const score = data.overall_score;
    const sf = data.salesforce || {};
    const ins = data.insights || {};
    const cs = data.cs_insights || {};
    const gl = data.engagement || data.glean || {};
    const customRisks = data.custom_risks || [];
    const sfdcLink = `${SFDC_BASE}/Account/${data.account_id}/view`;

    const activeCxm = customRisks.filter(r => r.status === "active");
    const criticalCxm = activeCxm.filter(r => r.severity === "critical").length;

    const headerEl = document.getElementById("account-header");
    headerEl.innerHTML = `
        <div class="account-header-top">
            <button class="back-btn" onclick="showDashboard()" title="Back to Dashboard">&larr;</button>
            <div class="account-header-info">
                <h2>${escHtml(data.account_name)}</h2>
                <div class="account-header-meta">
                    <span class="cell-meta">${data.account_id}</span>
                    <a href="${sfdcLink}" target="_blank" class="btn btn-sm btn-secondary" style="font-size:0.72rem;text-decoration:none">Open in Salesforce &#x2197;</a>
                    <span class="source-pill"><span class="dot ${data.connector_modes?.salesforce === 'live' ? 'active' : 'demo'}"></span>SF</span>
                    <span class="source-pill"><span class="dot ${data.connector_modes?.insights === 'live' ? 'active' : 'demo'}"></span>Insights</span>
                    <span class="source-pill"><span class="dot ${data.connector_modes?.csinsights === 'live' ? 'active' : 'demo'}"></span>CS</span>
                    <span class="source-pill"><span class="dot ${data.connector_modes?.planhat === 'live' ? 'active' : 'demo'}"></span>Planhat</span>
                    <span class="source-pill"><span class="dot ${(data.connector_modes?.glean ?? data.connector_modes?.engagement) === 'live' ? 'active' : 'demo'}"></span>Glean</span>
                </div>
            </div>
            <div class="account-header-score">
                <div class="gauge-circle ${level}" style="--pct:${score}%"><span>${score}</span></div>
                <span class="risk-badge ${level}">${level} risk</span>
            </div>
        </div>`;

    const advStats = ins.advisory_stats || {};
    const renewalDays = cs.days_to_renewal;
    const ph = data.planhat || {};

    const statsEl = document.getElementById("account-stats-row");
    statsEl.innerHTML = `
        <div class="acct-stat"><div class="acct-stat-value" style="color:var(--risk-${level})">${score}</div><div class="acct-stat-label">Risk Score</div></div>
        <div class="acct-stat"><div class="acct-stat-value">${sf.open_cases ?? 0}</div><div class="acct-stat-label">Open Cases</div></div>
        <div class="acct-stat"><div class="acct-stat-value">${ins.total_clusters ?? 0}<span style="font-size:0.65em;color:var(--text-muted)"> / ${ins.total_nodes ?? 0}n</span></div><div class="acct-stat-label">Clusters / Nodes</div></div>
        <div class="acct-stat"><div class="acct-stat-value" ${ins.eol_exposure_count > 0 ? 'style="color:var(--risk-high)"' : ''}>${ins.eol_exposure_count ?? 0}</div><div class="acct-stat-label">EOL Nodes</div></div>
        <div class="acct-stat"><div class="acct-stat-value" ${(advStats.critical || 0) > 0 ? 'style="color:var(--risk-critical)"' : ''}>${advStats.total || 0}</div><div class="acct-stat-label">Advisories</div></div>
        <div class="acct-stat"><div class="acct-stat-value" ${renewalDays != null && renewalDays < 90 ? 'style="color:var(--risk-critical)"' : ''}>${renewalDays != null ? renewalDays : '\u2014'}</div><div class="acct-stat-label">Renewal Days</div></div>
        <div class="acct-stat"><div class="acct-stat-value" ${gl.total_mentions === 0 ? 'style="color:var(--risk-high)"' : ''}>${gl.total_mentions ?? '\u2014'}</div><div class="acct-stat-label">Int. Activity</div></div>`;

    const tiles = [
        {
            id: "risk-factors", icon: "\u26A0", title: "Risk Factors",
            metrics: [`Score: ${score}`, `${(data.factors || []).length} factors`, topFactor(data.factors)],
            color: tileColor(score, [60, 40, 20]),
        },
        {
            id: "ai-advisory", icon: "\u2728", title: "AI Advisory",
            metrics: ["Claude-generated action plan", "Prioritized next steps"],
            color: "neutral",
        },
        {
            id: "incidents", icon: "\uD83D\uDCCB", title: "Incident Tickets",
            metrics: [`${sf.open_cases ?? 0} open`, `${sf.p1_cases ?? 0} P1`, `${sf.escalated_cases ?? 0} escalated`],
            color: tileColor(sf.p1_cases || sf.escalated_cases ? 60 : (sf.open_cases || 0) > 10 ? 50 : 20, [60, 40, 20]),
        },
        {
            id: "infrastructure", icon: "\uD83D\uDDA5", title: "Nutanix Infrastructure",
            metrics: [`${ins.total_clusters ?? 0} clusters / ${ins.total_nodes ?? 0} nodes`,
                (() => { const hd = ins.hypervisor_distribution || {}; const a = hd["AHV"] || 0; const e = hd["ESXI"] || hd["ESXi"] || 0; const t = a + e; return t ? `AHV ${Math.round(a/t*100)}% / ESXi ${Math.round(e/t*100)}%` : ''; })(),
                `${ins.eol_exposure_count ?? 0} EOL`],
            color: tileColor(ins.eol_exposure_count ? 60 : (advStats.critical || 0) > 0 ? 50 : 20, [60, 40, 20]),
        },
        {
            id: "license", icon: "\uD83D\uDCCA", title: "License & Adoption",
            metrics: [cs.adoption_score ? `${cs.adoption_score}% adopted` : "Load data", `CS Health: ${cs.health_score ?? '\u2014'}`],
            color: tileColor(cs.adoption_score ? (100 - cs.adoption_score) : 30, [70, 50, 25]),
        },
        {
            id: "planhat", icon: "\uD83D\uDC65", title: "Planhat CS",
            metrics: _planhatTileMetrics(ph),
            color: _planhatTileColor(ph),
        },
        {
            id: "renewal", icon: "\uD83D\uDD52", title: "Renewal Insights",
            metrics: [renewalDays != null ? `${renewalDays} days` : "N/A", cs.renewal_risk_label || "\u2014", cs.total_renewal_value ? `$${cs.total_renewal_value.toLocaleString()}` : ""],
            color: tileColor(renewalDays != null ? (renewalDays < 60 ? 80 : renewalDays < 180 ? 50 : 15) : 20, [60, 40, 20]),
        },
        {
            id: "cxm-flags", icon: "\uD83D\uDEA9", title: "CXM Risk Flags",
            metrics: [`${activeCxm.length} active`, `${criticalCxm} critical`],
            color: tileColor(criticalCxm > 0 ? 70 : activeCxm.length > 0 ? 40 : 10, [60, 40, 20]),
        },
        {
            id: "glean-intel", icon: "\uD83D\uDD0D", title: "Glean",
            metrics: [
                `${gl.total_mentions ?? 0} internal touchpoints`,
                `${gl.recent_mentions_30d ?? 0} recent (30d)`,
                gl.escalation_mentions ? `${gl.escalation_mentions} escalation ref(s)` : "",
            ],
            color: tileColor(
                gl.total_mentions === 0 ? 60 : gl.total_mentions < 5 ? 45 :
                gl.escalation_mentions > 2 ? 55 : 15,
                [60, 40, 20]
            ),
        },
        {
            id: "financials", icon: "\uD83D\uDCB0", title: "Account Financials",
            metrics: ["Revenue & contract data", "Employee count & deal analytics"],
            color: "neutral",
        },
        {
            id: "email", icon: "\u2709", title: "Email & Outreach",
            metrics: ["Generate email draft", "Contact management"],
            color: "neutral",
        },
    ];

    const gridEl = document.getElementById("section-tile-grid");
    gridEl.innerHTML = tiles.map(t => `
        <div class="section-tile section-tile-${t.color}" onclick="showSectionView('${t.id}')">
            <div class="section-tile-icon">${t.icon}</div>
            <div class="section-tile-title">${t.title}</div>
            <div class="section-tile-metrics">
                ${t.metrics.filter(Boolean).map(m => `<div class="section-tile-metric">${escHtml(m)}</div>`).join("")}
            </div>
            <div class="section-tile-indicator ${t.color}"></div>
        </div>`).join("");

    renderRiskComposition(data);
}

// ── Risk Composition (per-account doughnut) ─────────────────────────────

function renderRiskComposition(data) {
    const panel = document.getElementById("risk-composition-panel");
    const factors = data.factors || [];
    if (!panel || !factors.length) {
        if (panel) panel.style.display = "none";
        return;
    }

    const groups = {};
    factors.forEach(f => {
        const src = f.source || "other";
        if (!groups[src]) groups[src] = { source: src, weighted_total: 0, top_factors: [] };
        groups[src].weighted_total += f.weighted_score || 0;
        groups[src].top_factors.push({
            description: f.description, weighted_score: f.weighted_score || 0,
        });
    });
    const slices = Object.values(groups)
        .filter(g => g.weighted_total > 0)
        .sort((a, b) => b.weighted_total - a.weighted_total);
    if (!slices.length) { panel.style.display = "none"; return; }

    const total = slices.reduce((s, x) => s + x.weighted_total, 0);
    panel.style.display = "block";

    const ageMin = data.data_age_minutes;
    const ageStr = (ageMin == null || ageMin < 0) ? ""
        : ageMin < 60 ? `data ${ageMin}m old`
        : `data ${Math.floor(ageMin/60)}h ${ageMin%60}m old`;
    const meta = document.getElementById("risk-composition-meta");
    if (meta) {
        meta.textContent = `weighted total ${total.toFixed(1)} · ${slices.length} sources${ageStr ? " · " + ageStr : ""}${data.data_is_stale ? " · STALE" : ""}`;
    }

    const legend = document.getElementById("risk-composition-legend");
    if (legend) {
        legend.innerHTML = slices.map(s => {
            const pct = total > 0 ? (s.weighted_total / total * 100).toFixed(1) : 0;
            const color = sourceColor(s.source);
            return `<div class="rc-legend-row" onclick="showSectionView('risk-factors')" title="${escAttr((s.top_factors[0] || {}).description || '')}">
                <span class="rc-legend-swatch" style="background:${color}"></span>
                <span class="rc-legend-label">${escHtml(s.source)}</span>
                <span class="rc-legend-pct">${pct}%</span>
                <span class="rc-legend-val">${s.weighted_total.toFixed(1)}</span>
            </div>`;
        }).join("");
    }

    const canvas = document.getElementById("risk-composition-chart");
    if (!canvas || typeof Chart === "undefined") return;
    const ctx = canvas.getContext("2d");
    const chart = new Chart(ctx, {
        type: "doughnut",
        data: {
            labels: slices.map(s => s.source),
            datasets: [{
                data: slices.map(s => Math.round(s.weighted_total * 10) / 10),
                backgroundColor: slices.map(s => sourceColor(s.source)),
                borderColor: "rgba(0,0,0,0)",
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: "62%",
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const v = ctx.parsed;
                            const pct = total > 0 ? (v / total * 100).toFixed(1) : 0;
                            return `${ctx.label}: ${v} (${pct}%)`;
                        },
                    },
                },
            },
            onClick: (_, els) => {
                if (els.length) showSectionView("risk-factors");
            },
        },
    });
    activeChartInstances.push(chart);
}

// ── Portfolio Risk Composition (dashboard widget) ───────────────────────

let portfolioRiskChart = null;

function _portfolioRiskLevelLabel(level, threshold) {
    if (level === "high")   return `High (\u2265${threshold ?? 75})`;
    if (level === "medium") return `Medium (\u2265${threshold ?? 50})`;
    return "selected";
}

async function _composePortfolioRiskReason() {
    // Best-effort: surface stale snapshots, demo/fallback connectors, or
    // a disabled AI self-heal. Failures here must never block render.
    let freshness, status;
    try {
        [freshness, status] = await Promise.all([
            fetch("/api/sync/freshness").then(r => r.json()),
            fetch("/api/status").then(r => r.json()),
        ]);
    } catch (_) {
        return "";
    }

    const causes = [];
    const staleAfter = (freshness && freshness.stale_after_hours) || 12;
    const snaps = (freshness && freshness.snapshot_freshness) || {};
    const now = Date.now();
    for (const [src, iso] of Object.entries(snaps)) {
        if (!iso) continue;
        const ts = Date.parse(iso);
        if (isNaN(ts)) continue;
        const ageHours = (now - ts) / 3600000;
        if (ageHours > staleAfter) {
            causes.push(`${src} snapshot ${Math.round(ageHours)}h stale`);
        }
    }

    const connectors = (status && status.connectors) || {};
    for (const [src, mode] of Object.entries(connectors)) {
        const m = String(mode || "").toLowerCase();
        if (m === "demo" || m.includes("fallback")) {
            causes.push(`${src} in ${m} mode`);
        }
    }

    if (status && status.sync && status.sync.ai_heal === false) {
        causes.push("AI self-heal disabled");
    }

    return causes.length ? causes.join("; ") + "." : "";
}

async function _renderPortfolioRiskBanner(bannerEl, requestedLevel, requestedThreshold) {
    if (!bannerEl) return;
    const stateEl  = bannerEl.querySelector(".prb-state");
    const reasonEl = bannerEl.querySelector(".prb-reason");
    if (stateEl) {
        const labelTxt = _portfolioRiskLevelLabel(requestedLevel, requestedThreshold);
        stateEl.textContent = `No accounts currently cross the ${labelTxt} risk threshold. Showing the All Accounts breakdown instead.`;
    }
    if (reasonEl) reasonEl.textContent = "";
    bannerEl.classList.remove("hidden");
    const reason = await _composePortfolioRiskReason();
    if (reasonEl) reasonEl.textContent = reason;
}

async function loadPortfolioRisk() {
    const select = document.getElementById("portfolio-risk-level");
    const requestedLevel = select ? select.value : "high";
    const sideEl = document.getElementById("portfolio-risk-side");
    const canvas = document.getElementById("portfolio-risk-chart");
    const banner = document.getElementById("portfolio-risk-banner");
    if (!sideEl || !canvas) return;
    if (banner) banner.classList.add("hidden");
    sideEl.innerHTML = `<div class="loading-spinner"><div class="spinner"></div>Loading&hellip;</div>`;

    let data;
    try {
        data = await fetch(`/api/portfolio/risk-breakdown?level=${requestedLevel}`).then(r => r.json());
    } catch (e) {
        sideEl.innerHTML = `<div class="empty-state"><p>Failed to load: ${escHtml(String(e))}</p></div>`;
        return;
    }

    let slices = (data.slices || []).filter(s => s.weighted_total > 0);

    // Auto-fall-back: if the requested level is empty but other accounts
    // exist, render the All Accounts breakdown and explain why.
    if (!slices.length && requestedLevel !== "all") {
        let fallback;
        try {
            fallback = await fetch(`/api/portfolio/risk-breakdown?level=all`).then(r => r.json());
        } catch (e) {
            fallback = null;
        }
        const fbSlices = ((fallback && fallback.slices) || []).filter(s => s.weighted_total > 0);
        if (fbSlices.length) {
            await _renderPortfolioRiskBanner(banner, requestedLevel, data.threshold);
            data = fallback;
            slices = fbSlices;
        }
    }

    if (!slices.length) {
        sideEl.innerHTML = `<div class="empty-state"><p>No accounts at <strong>${requestedLevel}</strong> risk yet. The first scheduler tick will populate this widget.</p></div>`;
        if (portfolioRiskChart) { try { portfolioRiskChart.destroy(); } catch (_) {} portfolioRiskChart = null; }
        return;
    }

    const total = data.total || slices.reduce((s, x) => s + x.weighted_total, 0);

    sideEl.innerHTML = slices.map(s => {
        const color = sourceColor(s.source);
        const pct = total > 0 ? (s.weighted_total / total * 100).toFixed(1) : 0;
        const top = (s.top_accounts || []).slice(0, 10);
        return `<div class="pr-source-block" style="--rc-color:${color}">
            <h4>
                <span><span class="rc-legend-swatch" style="display:inline-block;background:${color};vertical-align:middle;margin-right:6px"></span>${escHtml(s.source)} <span class="cell-meta" style="font-weight:400;font-size:0.72rem">(${s.account_count} accts)</span></span>
                <span>${pct}%</span>
            </h4>
            ${top.length ? `<ol>${top.map(a =>
                `<li onclick="showAccountView('${escAttr(a.account_id)}', '${escAttr(a.account_name)}')">${escHtml(a.account_name)} <span style="opacity:0.6">(${(a.source_score || 0).toFixed(1)})</span></li>`
            ).join("")}</ol>` : `<div class="cell-meta" style="font-size:0.75rem">no contributing accounts</div>`}
        </div>`;
    }).join("");

    if (typeof Chart === "undefined") return;
    if (portfolioRiskChart) { try { portfolioRiskChart.destroy(); } catch (_) {} portfolioRiskChart = null; }
    const ctx = canvas.getContext("2d");
    portfolioRiskChart = new Chart(ctx, {
        type: "doughnut",
        data: {
            labels: slices.map(s => s.source),
            datasets: [{
                data: slices.map(s => Math.round(s.weighted_total * 10) / 10),
                backgroundColor: slices.map(s => sourceColor(s.source)),
                borderColor: "rgba(0,0,0,0)",
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: "62%",
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const v = ctx.parsed;
                            const pct = total > 0 ? (v / total * 100).toFixed(1) : 0;
                            return `${ctx.label}: ${v} (${pct}%)`;
                        },
                    },
                },
            },
        },
    });
}

function topFactor(factors) {
    if (!factors || !factors.length) return "";
    const top = [...factors].sort((a, b) => b.weighted_score - a.weighted_score)[0];
    return top ? top.description.substring(0, 40) : "";
}

function tileColor(value, thresholds) {
    if (value >= thresholds[0]) return "critical";
    if (value >= thresholds[1]) return "high";
    if (value >= thresholds[2]) return "medium";
    return "low";
}

// ══════════════════════════════════════════════════════════════════════════
// SECTION VIEWS (Page 3) — Full-screen drill-down
// ══════════════════════════════════════════════════════════════════════════

function renderSectionView(section, data) {
    const headerEl = document.getElementById("section-header");
    const titles = {
        "risk-factors": "Risk Factors",
        "ai-advisory": "AI Advisory",
        "incidents": "Incident Tickets",
        "infrastructure": "Nutanix Infrastructure",
        "license": "License & Adoption",
        "planhat": "Planhat CS",
        "renewal": "Renewal Insights",
        "cxm-flags": "CXM Risk Flags",
        "glean-intel": "Glean",
        "financials": "Account Financials",
        "email": "Email & Outreach",
    };
    headerEl.innerHTML = `
        <div class="section-header-top">
            <button class="back-btn" onclick="showAccountView('${escAttr(currentAccountId)}', '${escAttr(currentAccountName)}')" title="Back to Account">&larr;</button>
            <div>
                <h2>${titles[section] || section}</h2>
                <span class="cell-meta">${escHtml(data.account_name)}</span>
            </div>
        </div>`;

    showPage("section-view");

    const contentEl = document.getElementById("section-content");
    contentEl.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading\u2026</div>';

    switch (section) {
        case "risk-factors": renderRiskFactorsSection(data, contentEl); break;
        case "ai-advisory": renderAdvisorySection(data, contentEl); break;
        case "incidents": renderIncidentsSection(data, contentEl); break;
        case "infrastructure": renderInfraSection(data, contentEl); break;
        case "license": renderLicenseSection(data, contentEl); break;
        case "planhat": renderPlanhatSection(data, contentEl); break;
        case "renewal": renderRenewalSection(data, contentEl); break;
        case "cxm-flags": renderCxmFlagsSection(data, contentEl); break;
        case "glean-intel": renderGleanSection(data, contentEl); break;
        case "financials": renderFinancialsSection(data, contentEl); break;
        case "email": renderEmailSection(data, contentEl); break;
        default: contentEl.innerHTML = '<div class="empty-state"><p>Unknown section.</p></div>';
    }
}

// ── AI Advisory Section (Claude-powered, deterministic fallback) ────────

async function renderAdvisorySection(data, container) {
    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Generating advisory\u2026</div>';
    let res = null;
    try {
        res = await API.aiAdvisory(data.account_id, data.account_name);
    } catch (e) {
        container.innerHTML = '<div class="empty-state"><p>Advisory unavailable right now.</p></div>';
        return;
    }

    const isClaude = res.source === "claude";
    const badge = isClaude
        ? `<span class="tag claude" style="vertical-align:middle">Generated by Claude \u00b7 ${escHtml(res.model || "")}</span>`
        : `<span class="tag" style="vertical-align:middle">Deterministic advisory</span>`;
    const note = isClaude
        ? "Live action plan written by Claude from this account's risk signals."
        : "Synthesized locally from the account's risk signals. Set an Anthropic API key to enable Claude-written advisories.";

    // Render the advisory text: bullet lines become list items, the rest paragraphs.
    const lines = (res.advisory || "").split("\n").map(l => l.trim()).filter(Boolean);
    let html = "";
    let inList = false;
    for (const line of lines) {
        const isBullet = /^[-*\u2022]\s+/.test(line);
        if (isBullet) {
            if (!inList) { html += '<ul class="advisory-list">'; inList = true; }
            html += `<li>${escHtml(line.replace(/^[-*\u2022]\s+/, ""))}</li>`;
        } else {
            if (inList) { html += "</ul>"; inList = false; }
            html += `<p style="color:var(--text-secondary);line-height:1.6;margin:8px 0">${escHtml(line)}</p>`;
        }
    }
    if (inList) html += "</ul>";

    container.innerHTML = `
        <div class="section-data-block">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
                <h3 style="margin:0">Recommended Action Plan</h3>${badge}
            </div>
            <p style="color:var(--text-muted);font-size:0.8rem;margin:0 0 14px">${note}</p>
            ${html || '<p class="text-muted">No advisory generated.</p>'}
        </div>`;
}

// ── Risk Factors Section ────────────────────────────────────────────────

function renderRiskFactorsSection(data, container) {
    const factors = data.factors || [];
    const recs = data.recommendations || [];

    const sourceGroups = {};
    factors.forEach(f => {
        sourceGroups[f.source] = (sourceGroups[f.source] || 0) + f.weighted_score;
    });

    const sorted = [...factors].sort((a, b) => b.weighted_score - a.weighted_score);

    container.innerHTML = `
        <div class="section-charts-row">
            <div class="chart-card">
                <h3>Score by Source</h3>
                <div class="chart-wrap"><canvas id="risk-source-chart"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>All Factors (weighted)</h3>
                <div class="chart-wrap chart-wrap-tall"><canvas id="risk-factors-chart"></canvas></div>
            </div>
        </div>
        <div class="section-data-block">
            <h3>Recommendations (${recs.length})</h3>
            ${recs.length ? recs.map(r => `<div class="recommendation-item">${escHtml(r)}</div>`).join("") : '<p class="text-muted">No active recommendations.</p>'}
        </div>
        <div class="section-data-block">
            <h3>Factor Details</h3>
            ${sorted.map(f => `<div class="factor-row">
                <span class="source-tag ${f.source}">${f.source}</span>
                <span class="desc">${escHtml(f.description)}</span>
                <span class="score-value" style="color:var(--risk-${scoreLevel(f.weighted_score)})">${f.weighted_score.toFixed(1)}</span>
            </div>`).join("")}
        </div>`;

    const srcLabels = Object.keys(sourceGroups);
    const srcValues = Object.values(sourceGroups).map(v => Math.round(v * 10) / 10);
    const srcColors = srcLabels.map(s => sourceColor(s));

    if (srcLabels.length && typeof Chart !== "undefined") {
        const ctx1 = document.getElementById("risk-source-chart").getContext("2d");
        activeChartInstances.push(new Chart(ctx1, {
            type: "doughnut",
            data: { labels: srcLabels, datasets: [{ data: srcValues, backgroundColor: srcColors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1" } } } },
        }));
    }

    if (sorted.length && typeof Chart !== "undefined") {
        const ctx2 = document.getElementById("risk-factors-chart").getContext("2d");
        activeChartInstances.push(new Chart(ctx2, {
            type: "bar",
            data: {
                labels: sorted.map(f => f.description.substring(0, 35)),
                datasets: [{ data: sorted.map(f => Math.round(f.weighted_score * 10) / 10), backgroundColor: sorted.map(f => sourceColor(f.source)), borderWidth: 0 }],
            },
            options: {
                indexAxis: "y", responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { x: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148,163,184,0.1)" } }, y: { ticks: { color: "#cbd5e1", font: { size: 11 } }, grid: { display: false } } },
            },
        }));
    }
}

function sourceColor(src) {
    const map = { salesforce: "#3b82f6", insights: "#f59e0b", csinsights: "#10b981", planhat: "#ec4899", cxm: "#a855f7", engagement: "#06b6d4", glean: "#06b6d4" };
    return map[src] || "#64748b";
}

// ── Incident Tickets Section ────────────────────────────────────────────

function renderIncidentsSection(data, container) {
    const sf = data.salesforce || {};
    const cases = sf.cases || [];
    const openCases = cases.filter(c => !c.IsClosed);

    const pCounts = { P1: 0, P2: 0, P3: 0, P4: 0 };
    const statusCounts = {};
    openCases.forEach(c => {
        const pri = (c.Priority || "P3").substring(0, 2);
        pCounts[pri] = (pCounts[pri] || 0) + 1;
        const st = c.Status || "Unknown";
        statusCounts[st] = (statusCounts[st] || 0) + 1;
    });

    const aged = countAgedCases(openCases, 14);
    const sortedCases = [...openCases].sort((a, b) => ((a.Priority || "P9")[1] || "9").localeCompare((b.Priority || "P9")[1] || "9"));

    const caseRows = sortedCases.map(c => {
        const caseLink = `${SFDC_BASE}/Case/${c.Id}/view`;
        const pri = (c.Priority || "").substring(0, 2);
        const priClass = pri === "P1" ? "critical" : pri === "P2" ? "high" : "medium";
        const created = c.CreatedDate ? new Date(c.CreatedDate) : null;
        const ageDays = created ? Math.floor((Date.now() - created.getTime()) / 86400000) : "\u2014";
        return `<tr>
            <td><span class="risk-badge ${priClass}" style="font-size:0.65rem;padding:2px 6px">${escHtml(pri)}</span></td>
            <td><a href="${caseLink}" target="_blank" class="case-link">${escHtml((c.Subject || "No subject").substring(0, 80))}</a></td>
            <td>${escHtml(c.CaseNumber || "")}</td>
            <td>${escHtml(c.Status || "")}</td>
            <td>${ageDays}d</td>
            <td>${c.IsEscalated ? '<span class="risk-badge critical" style="font-size:0.6rem;padding:1px 5px">ESC</span>' : ""}</td>
        </tr>`;
    }).join("");

    const sfRecs = (data.recommendations || []).filter(r => r.toLowerCase().includes("case") || r.toLowerCase().includes("escalat") || r.toLowerCase().includes("p1"));

    container.innerHTML = `
        <div class="section-stats-row">
            <div class="acct-stat"><div class="acct-stat-value">${sf.open_cases ?? 0}</div><div class="acct-stat-label">Open</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${sf.p1_cases > 0 ? 'style="color:var(--risk-critical)"' : ''}>${sf.p1_cases ?? 0}</div><div class="acct-stat-label">P1</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${sf.p2_cases > 0 ? 'style="color:var(--risk-high)"' : ''}>${sf.p2_cases ?? 0}</div><div class="acct-stat-label">P2</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${sf.escalated_cases > 0 ? 'style="color:var(--risk-critical)"' : ''}>${sf.escalated_cases ?? 0}</div><div class="acct-stat-label">Escalated</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${aged > 3 ? 'style="color:var(--risk-high)"' : ''}>${aged}</div><div class="acct-stat-label">Aged (&gt;14d)</div></div>
            <div class="acct-stat"><div class="acct-stat-value">${sf.total_cases ?? 0}</div><div class="acct-stat-label">Total</div></div>
        </div>
        <div class="section-charts-row">
            <div class="chart-card">
                <h3>Priority Distribution</h3>
                <div class="chart-wrap"><canvas id="priority-chart"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>Status Distribution</h3>
                <div class="chart-wrap"><canvas id="status-chart"></canvas></div>
            </div>
        </div>
        <div class="section-data-block">
            <h3>All Open Cases (${openCases.length})</h3>
            ${openCases.length ? `<div class="table-scroll"><table class="infra-table">
                <thead><tr><th>Pri</th><th>Subject</th><th>Case #</th><th>Status</th><th>Age</th><th>Esc</th></tr></thead>
                <tbody>${caseRows}</tbody></table></div>` : '<p class="text-muted">No open cases.</p>'}
        </div>
        ${sfRecs.length ? `<div class="section-data-block"><h3>Recommendations</h3>${sfRecs.map(r => `<div class="recommendation-item">${escHtml(r)}</div>`).join("")}</div>` : ""}`;

    if (typeof Chart !== "undefined") {
        const pLabels = Object.keys(pCounts);
        const pValues = Object.values(pCounts);
        const pColors = ["#ef4444", "#f59e0b", "#3b82f6", "#64748b"];
        const ctx1 = document.getElementById("priority-chart").getContext("2d");
        activeChartInstances.push(new Chart(ctx1, {
            type: "bar",
            data: { labels: pLabels, datasets: [{ data: pValues, backgroundColor: pColors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: "#94a3b8", stepSize: 1 }, grid: { color: "rgba(148,163,184,0.1)" } }, x: { ticks: { color: "#cbd5e1" }, grid: { display: false } } } },
        }));

        const sLabels = Object.keys(statusCounts);
        const sValues = Object.values(statusCounts);
        const sColors = sLabels.map((_, i) => ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#a855f7", "#64748b"][i % 6]);
        const ctx2 = document.getElementById("status-chart").getContext("2d");
        activeChartInstances.push(new Chart(ctx2, {
            type: "doughnut",
            data: { labels: sLabels, datasets: [{ data: sValues, backgroundColor: sColors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1" } } } },
        }));
    }
}

function countAgedCases(cases, ageDays) {
    const now = Date.now();
    return cases.filter(c => {
        if (c.IsClosed) return false;
        const d = c.CreatedDate ? new Date(c.CreatedDate).getTime() : 0;
        return d && (now - d) / 86400000 >= ageDays;
    }).length;
}

// ── Infrastructure Section ──────────────────────────────────────────────

function renderInfraSection(data, container) {
    const ins = data.insights || {};
    const clusters = ins.clusters || [];
    const advStats = ins.advisory_stats || {};
    const advisories = ins.applicable_advisories || [];
    const eolVersions = ins.eol_versions_in_use || [];
    const approachingEolVersions = ins.approaching_eol_versions || [];
    const approachingEolCount = ins.approaching_eol_count || 0;
    const versionsMap = ins.versions_summary || {};
    const hwSummary = ins.hardware_summary || {};
    const hvDist = ins.hypervisor_distribution || {};
    const ahvVersions = ins.ahv_versions || {};
    const esxiVersions = ins.esxi_versions || {};
    const hwPartnerDist = ins.hw_partner_distribution || {};

    const pulseConn = ins.pulse_connected ?? 0;
    const pulseDisc = ins.pulse_disconnected ?? 0;
    const hasPulseCounts = (pulseConn > 0 || pulseDisc > 0);
    let pulseHtml;
    if (hasPulseCounts) {
        const activeColor = pulseConn > 0 ? 'color:var(--risk-low)' : 'color:var(--text-muted)';
        const disabledColor = pulseDisc > 0 ? 'color:var(--risk-high)' : 'color:var(--text-muted)';
        pulseHtml = `<span style="${activeColor}">${pulseConn} Active</span><br><span style="${disabledColor};font-size:0.85em">${pulseDisc} Stale/Off</span>`;
    } else {
        pulseHtml = ins.pulse_enabled === true ? 'Yes' : ins.pulse_enabled === false ? '<span style="color:var(--risk-critical)">No</span>' : '\u2014';
    }
    const pulseLabel = hasPulseCounts ? 'Pulse Status' : 'Pulse';

    const eolCount = ins.eol_exposure_count || 0;

    const aosDistribution = {};
    clusters.forEach(c => {
        const v = c.aos_version || "Unknown";
        aosDistribution[v] = (aosDistribution[v] || 0) + (c.node_count || 0);
    });

    const totalHvNodes = Object.values(hvDist).reduce((a, b) => a + b, 0);
    const ahvNodes = hvDist["AHV"] || 0;
    const esxiNodes = hvDist["ESXI"] || hvDist["ESXi"] || 0;
    const ahvPct = totalHvNodes ? Math.round(ahvNodes / totalHvNodes * 100) : 0;
    const esxiPct = totalHvNodes ? Math.round(esxiNodes / totalHvNodes * 100) : 0;

    let eolBanner = '';
    if (eolVersions.length) {
        eolBanner += `<div class="eol-banner" style="background:rgba(239,68,68,0.12);border-left:3px solid var(--risk-critical);padding:6px 12px;margin-bottom:4px;border-radius:4px;font-size:0.8rem"><strong>EOL:</strong> ${eolVersions.join(', ')} — upgrade required</div>`;
    }
    if (approachingEolVersions.length) {
        eolBanner += `<div class="eol-banner" style="background:rgba(245,158,11,0.12);border-left:3px solid var(--risk-high);padding:6px 12px;margin-bottom:4px;border-radius:4px;font-size:0.8rem"><strong>Approaching EOL (within 180 days):</strong> ${approachingEolVersions.join(', ')} — plan upgrades</div>`;
    }

    let hvDistHtml = '';
    if (totalHvNodes > 0) {
        hvDistHtml = `<div class="acct-stat">
            <div class="acct-stat-value" style="font-size:0.95rem">
                <span style="color:#3b82f6">${ahvPct}% AHV</span>
                <span style="color:var(--text-muted);font-size:0.75rem;margin:0 4px">/</span>
                <span style="color:#10b981">${esxiPct}% ESXi</span>
            </div>
            <div class="acct-stat-label">Hypervisor Split</div>
        </div>`;
    }

    const hwDisplay = Object.keys(hwPartnerDist).length ? hwPartnerDist : hwSummary;
    const topHw = Object.entries(hwDisplay).sort((a, b) => b[1] - a[1]);
    const hwStatHtml = topHw.length
        ? `<div class="acct-stat"><div class="acct-stat-value" style="font-size:0.95rem">${escHtml(topHw[0][0])}</div><div class="acct-stat-label">Primary HW${topHw.length > 1 ? ` (+${topHw.length - 1})` : ''}</div></div>`
        : '';

    container.innerHTML = `
        <div class="section-stats-row">
            <div class="acct-stat"><div class="acct-stat-value">${ins.total_clusters ?? 0}</div><div class="acct-stat-label">Clusters</div></div>
            <div class="acct-stat"><div class="acct-stat-value">${ins.total_nodes ?? 0}</div><div class="acct-stat-label">Nodes</div></div>
            ${hvDistHtml}
            ${hwStatHtml}
            <div class="acct-stat"><div class="acct-stat-value">${pulseHtml}</div><div class="acct-stat-label">${pulseLabel}</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${eolCount > 0 ? 'style="color:var(--risk-critical)"' : ''}>${eolCount}</div><div class="acct-stat-label">EOL Nodes</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${approachingEolCount > 0 ? 'style="color:var(--risk-high)"' : ''}>${approachingEolCount}</div><div class="acct-stat-label">Approaching EOL</div></div>
            <div class="acct-stat"><div class="acct-stat-value" ${(advStats.critical || 0) > 0 ? 'style="color:var(--risk-critical)"' : ''}>${advStats.total || 0}</div><div class="acct-stat-label">Advisories</div></div>
        </div>
        ${eolBanner}
        <div class="section-charts-row">
            <div class="chart-card" style="grid-column:span 1">
                <h3>AOS Version Distribution</h3>
                <div class="chart-wrap" style="height:${Math.max(200, Math.min(Object.keys(aosDistribution).length * 28 + 30, 400))}px"><canvas id="aos-chart"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>Hypervisor Distribution</h3>
                <div class="chart-wrap"><canvas id="hv-chart"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>Hardware Partners</h3>
                <div class="chart-wrap"><canvas id="hw-chart"></canvas></div>
            </div>
        </div>
        ${renderHvVersionDetails(ahvVersions, esxiVersions)}
        <div class="section-data-block">
            <h3>Cluster Details</h3>
            ${renderInfraClusterTable(clusters)}
        </div>
        ${renderVersionsSummary(versionsMap)}
        <div class="section-data-block">
            <h3>Advisory Compliance</h3>
            ${renderAdvisoryCompliance(advisories, advStats)}
        </div>
        ${infraRecs(data)}`;

    if (typeof Chart !== "undefined") {
        const chartColors = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#a855f7", "#06b6d4", "#ec4899", "#64748b", "#84cc16", "#f97316"];
        const aosSorted = Object.entries(aosDistribution).sort((a, b) => b[1] - a[1]);
        const aosLabels = aosSorted.map(([v]) => v);
        const aosValues = aosSorted.map(([, c]) => c);
        if (aosLabels.length) {
            const ctx = document.getElementById("aos-chart").getContext("2d");
            activeChartInstances.push(new Chart(ctx, {
                type: "bar",
                data: { labels: aosLabels, datasets: [{ label: "Nodes", data: aosValues, backgroundColor: "#3b82f6", borderWidth: 0, borderRadius: 3, barPercentage: 0.7 }] },
                options: {
                    indexAxis: "y", responsive: true, maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: (ctx) => `${ctx.parsed.x.toLocaleString()} nodes` } },
                    },
                    scales: {
                        x: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(148,163,184,0.08)" } },
                        y: { ticks: { color: "#cbd5e1", font: { size: 11 } }, grid: { display: false } },
                    },
                },
            }));
        }

        const hvLabels = Object.keys(hvDist);
        const hvValues = Object.values(hvDist);
        if (hvLabels.length) {
            const ctx2 = document.getElementById("hv-chart").getContext("2d");
            activeChartInstances.push(new Chart(ctx2, {
                type: "doughnut",
                data: { labels: hvLabels, datasets: [{ data: hvValues, backgroundColor: ["#3b82f6", "#10b981", "#f59e0b", "#64748b"], borderWidth: 0 }] },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1" } } } },
            }));
        }

        const hwKeys = Object.keys(hwDisplay);
        const hwVals = Object.values(hwDisplay);
        if (hwKeys.length) {
            const ctx3 = document.getElementById("hw-chart").getContext("2d");
            activeChartInstances.push(new Chart(ctx3, {
                type: "doughnut",
                data: { labels: hwKeys, datasets: [{ data: hwVals, backgroundColor: chartColors.slice(0, hwKeys.length), borderWidth: 0 }] },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1" } } } },
            }));
        }

        initHvVersionCharts(ahvVersions, esxiVersions);
        initVersionCharts(versionsMap);
    }
}

function infraRecs(data) {
    const recs = (data.recommendations || []).filter(r =>
        r.toLowerCase().includes("cluster") || r.toLowerCase().includes("eol") ||
        r.toLowerCase().includes("advisory") || r.toLowerCase().includes("pulse") ||
        r.toLowerCase().includes("ncc") || r.toLowerCase().includes("contract") ||
        r.toLowerCase().includes("node") || r.toLowerCase().includes("upgrade") ||
        r.toLowerCase().includes("patch") || r.toLowerCase().includes("insight")
    );
    if (!recs.length) return "";
    return `<div class="section-data-block"><h3>Recommendations</h3>${recs.map(r => `<div class="recommendation-item">${escHtml(r)}</div>`).join("")}</div>`;
}

function renderHvVersionDetails(ahvVersions, esxiVersions) {
    const ahvEntries = Object.entries(ahvVersions).sort((a, b) => b[1] - a[1]);
    const esxiEntries = Object.entries(esxiVersions).sort((a, b) => b[1] - a[1]);
    if (!ahvEntries.length && !esxiEntries.length) return '';

    let cards = '';
    if (ahvEntries.length) {
        const h = Math.max(180, Math.min(ahvEntries.length * 28 + 40, 360));
        cards += `<div class="chart-card">
            <h3 style="color:#3b82f6">AHV Versions (${ahvEntries.length})</h3>
            <div class="chart-wrap" style="height:${h}px"><canvas id="ahv-ver-chart"></canvas></div>
        </div>`;
    }
    if (esxiEntries.length) {
        const h = Math.max(180, Math.min(esxiEntries.length * 28 + 40, 360));
        cards += `<div class="chart-card">
            <h3 style="color:#10b981">ESXi Versions (${esxiEntries.length})</h3>
            <div class="chart-wrap" style="height:${h}px"><canvas id="esxi-ver-chart"></canvas></div>
        </div>`;
    }
    return `<div class="section-data-block" style="background:transparent;border:none;padding:0">
        <div class="section-charts-row" style="grid-template-columns:repeat(auto-fit,minmax(380px,1fr))">${cards}</div>
    </div>`;
}

function initHvVersionCharts(ahvVersions, esxiVersions) {
    if (typeof Chart === "undefined") return;

    const makeChart = (canvasId, entries, color) => {
        if (!entries.length) return;
        const el = document.getElementById(canvasId);
        if (!el) return;
        const maxItems = 15;
        let labels = entries.map(([v]) => v);
        let values = entries.map(([, c]) => c);
        if (labels.length > maxItems) {
            const rest = values.slice(maxItems).reduce((a, b) => a + b, 0);
            labels = labels.slice(0, maxItems).concat(["Others"]);
            values = values.slice(0, maxItems).concat([rest]);
        }
        activeChartInstances.push(new Chart(el.getContext("2d"), {
            type: "bar",
            data: {
                labels,
                datasets: [{ label: "Nodes", data: values, backgroundColor: color, borderWidth: 0, borderRadius: 3, barPercentage: 0.7 }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: (ctx) => `${ctx.parsed.x.toLocaleString()} nodes` } },
                },
                scales: {
                    x: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(148,163,184,0.08)" } },
                    y: { ticks: { color: "#cbd5e1", font: { size: 9, family: "var(--font-mono)" } }, grid: { display: false } },
                },
            },
        }));
    };

    const ahv = Object.entries(ahvVersions || {}).sort((a, b) => b[1] - a[1]);
    const esxi = Object.entries(esxiVersions || {}).sort((a, b) => b[1] - a[1]);
    makeChart("ahv-ver-chart", ahv, "#3b82f6");
    makeChart("esxi-ver-chart", esxi, "#10b981");
}

function renderInfraClusterTable(clusters) {
    if (!clusters || !clusters.length) return '<p class="text-muted">No cluster data available.</p>';
    const hasRichData = clusters.some(c => c.hypervisor && c.hypervisor !== 'unknown' && c.hypervisor !== '');
    if (hasRichData) {
        const rows = clusters.map(c => {
            const pulseIcon = c.pulse_active ? '&#x2714;' : '<span style="color:var(--risk-critical)">&#x2716;</span>';
            const contractCls = (c.contract_status || '').toLowerCase() === 'expired' ? 'color:var(--risk-critical)' : '';
            const hwDisplay = c.hw_partner || (c.hardware_models || []).join(', ') || '\u2014';
            const hvType = c.hypervisor_type || (c.hypervisor || '').split(' ')[0] || '';
            const hvColor = hvType.toUpperCase() === 'AHV' ? '#3b82f6' : hvType.toUpperCase() === 'ESXI' ? '#10b981' : 'var(--text-muted)';
            const nccBadge = c.ncc_version ? `<span class="infra-version-pill" style="font-size:0.65rem">${escHtml(c.ncc_version)}</span>` : '';
            const compVersions = Object.entries(c.component_versions || {})
                .filter(([k]) => !['AOS', 'Hypervisor', 'PC', 'NCC'].includes(k))
                .map(([k, v]) => `${k}: ${v}`)
                .join(', ');
            const compHtml = compVersions ? `<div style="font-size:0.65rem;color:var(--text-muted);margin-top:1px">${escHtml(compVersions)}</div>` : '';

            return `<tr>
                <td style="font-family:var(--font-mono);font-size:0.72rem">${escHtml(c.cluster_name || c.cluster_id || '\u2014')}</td>
                <td>${c.node_count || '\u2014'}</td>
                <td><span class="infra-version-badge">${escHtml(c.aos_version || '\u2014')}</span></td>
                <td><span style="color:${hvColor};font-weight:600;font-size:0.72rem">${escHtml(hvType)}</span><div style="font-size:0.65rem;color:var(--text-muted)">${escHtml(c.hypervisor_version || c.hypervisor || '')}</div></td>
                <td>${escHtml(c.pc_version || '\u2014')}</td>
                <td>${nccBadge}${compHtml}</td>
                <td style="font-size:0.75rem">${escHtml(hwDisplay)}</td>
                <td>${pulseIcon}</td>
                <td style="${contractCls}">${escHtml(c.contract_status || '\u2014')}</td>
            </tr>`;
        }).join('');
        return `<div class="table-scroll"><table class="infra-table">
            <thead><tr><th>Cluster</th><th>Nodes</th><th>AOS</th><th>Hypervisor</th><th>PC</th><th>NCC / Components</th><th>Hardware</th><th>Pulse</th><th>Contract</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    }
    const rows = clusters.map(c => {
        const contractCls = (c.contract_status || '').toLowerCase() === 'expired' ? 'color:var(--risk-critical)' : '';
        return `<tr>
            <td><span class="infra-version-badge">${escHtml(c.aos_version || '\u2014')}</span></td>
            <td>${c.node_count || '\u2014'}</td>
            <td style="${contractCls}">${escHtml(c.contract_status || '\u2014')}</td>
        </tr>`;
    }).join('');
    return `<div class="table-scroll"><table class="infra-table">
        <thead><tr><th>AOS Version</th><th>Nodes</th><th>Contract</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

function renderVersionsSummary(versions) {
    if (!versions || !Object.keys(versions).length) return '';
    const skip = new Set(["AOS", "Hypervisor"]);
    const sections = Object.entries(versions)
        .filter(([k, v]) => !skip.has(k) && Object.keys(v).length > 0);
    if (!sections.length) return '';

    const cards = sections.map(([category]) => {
        const chartId = `ver-chart-${category.toLowerCase().replace(/\s+/g, '-')}`;
        const height = "220px";
        return `<div class="chart-card">
            <h3>${escHtml(category)} Versions</h3>
            <div class="chart-wrap" style="height:${height}"><canvas id="${chartId}"></canvas></div>
        </div>`;
    }).join('');

    return `<div class="section-data-block" style="background:transparent;border:none;padding:0">
        <div class="section-charts-row" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">${cards}</div>
    </div>`;
}

function initVersionCharts(versions) {
    if (typeof Chart === "undefined" || !versions) return;
    const skip = new Set(["AOS", "Hypervisor"]);
    const palette = ["#3b82f6", "#06b6d4", "#10b981", "#f59e0b", "#a855f7", "#ec4899", "#ef4444", "#84cc16", "#f97316", "#64748b"];
    const catColors = { PC: "#06b6d4", NCC: "#f59e0b", LCM: "#a855f7", Foundation: "#10b981", Files: "#ec4899", Objects: "#3b82f6" };

    Object.entries(versions).forEach(([category, dist]) => {
        if (skip.has(category) || !Object.keys(dist).length) return;
        const chartId = `ver-chart-${category.toLowerCase().replace(/\s+/g, '-')}`;
        const el = document.getElementById(chartId);
        if (!el) return;

        const sorted = Object.entries(dist).sort((a, b) => b[1] - a[1]);
        const maxItems = 12;
        let labels = sorted.map(([v]) => v);
        let values = sorted.map(([, c]) => c);
        if (labels.length > maxItems) {
            const rest = values.slice(maxItems).reduce((a, b) => a + b, 0);
            labels = labels.slice(0, maxItems).concat(["Others"]);
            values = values.slice(0, maxItems).concat([rest]);
        }

        const color = catColors[category] || palette[Object.keys(versions).indexOf(category) % palette.length];

        const ctx = el.getContext("2d");
        activeChartInstances.push(new Chart(ctx, {
            type: "bar",
            data: {
                labels,
                datasets: [{
                    label: "Nodes",
                    data: values,
                    backgroundColor: color,
                    borderWidth: 0,
                    borderRadius: 3,
                    barPercentage: 0.75,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `${ctx.parsed.x.toLocaleString()} nodes`,
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: { color: "#94a3b8", font: { size: 10 } },
                        grid: { color: "rgba(148,163,184,0.08)" },
                    },
                    y: {
                        ticks: { color: "#cbd5e1", font: { size: 10, family: "var(--font-mono)" } },
                        grid: { display: false },
                    },
                },
            },
        }));
    });
}

function renderAdvisoryCompliance(advisories, stats) {
    if (!advisories || !advisories.length) {
        return `<div style="font-size:0.82rem;color:var(--risk-low);padding:8px 0">&#x2714; No applicable advisories \u2014 infrastructure is compliant.</div>`;
    }
    const sevColor = s => {
        const sl = (s || '').toLowerCase();
        return sl === 'critical' ? 'var(--risk-critical)' : sl === 'high' ? 'var(--risk-high)' : sl === 'medium' ? 'var(--risk-medium)' : 'var(--text-muted)';
    };
    const sevBg = s => {
        const sl = (s || '').toLowerCase();
        return sl === 'critical' ? 'rgba(239,68,68,0.15)' : sl === 'high' ? 'rgba(245,158,11,0.15)' : sl === 'medium' ? 'rgba(59,130,246,0.15)' : 'rgba(148,163,184,0.1)';
    };
    const headerBadges = [];
    if (stats.critical) headerBadges.push(`<span class="adv-count-badge" style="background:rgba(239,68,68,0.2);color:var(--risk-critical)">${stats.critical} Critical</span>`);
    if (stats.high) headerBadges.push(`<span class="adv-count-badge" style="background:rgba(245,158,11,0.2);color:var(--risk-high)">${stats.high} High</span>`);
    if (stats.medium) headerBadges.push(`<span class="adv-count-badge" style="background:rgba(59,130,246,0.2);color:var(--risk-medium)">${stats.medium} Medium</span>`);

    const rows = advisories.map(a => {
        const link = a.href ? `<a href="${a.href}" target="_blank" class="adv-link" title="View advisory">&#128196;</a>` : '';
        const typeBadge = a.type === 'security' ? '<span class="adv-type-badge adv-type-sec">SEC</span>' : '<span class="adv-type-badge adv-type-field">FA</span>';

        const affectedClusters = a.affected_clusters || [];
        const matchReasons = a.match_reasons || [];
        let clusterDetail = '';
        if (affectedClusters.length) {
            const clusterItems = affectedClusters.slice(0, 5).map(ac => {
                const models = (ac.hardware_models || []).join(', ');
                const parts = [`<strong>${escHtml(ac.cluster_name || ac.cluster_id)}</strong>`];
                if (ac.aos_version) parts.push(`AOS ${escHtml(ac.aos_version)}`);
                if (ac.node_count) parts.push(`${ac.node_count} node(s)`);
                if (models) parts.push(escHtml(models));
                if (ac.hypervisor) parts.push(escHtml(ac.hypervisor));
                return `<li style="font-size:0.75rem;margin:2px 0">${parts.join(' &middot; ')}</li>`;
            }).join('');
            const moreText = affectedClusters.length > 5 ? `<li style="font-size:0.75rem;color:var(--text-muted)">and ${affectedClusters.length - 5} more\u2026</li>` : '';
            clusterDetail = `<div style="margin-top:6px"><div style="font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:2px">Affected Clusters:</div><ul style="list-style:none;padding-left:8px;margin:0">${clusterItems}${moreText}</ul></div>`;
        }

        let reasonHtml = '';
        if (matchReasons.length) {
            reasonHtml = `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px">Match: ${matchReasons.map(r => escHtml(r)).join('; ')}</div>`;
        }

        return `<div class="adv-row" style="border-left:3px solid ${sevColor(a.severity)}; background:${sevBg(a.severity)}">
            <div class="adv-row-header">
                ${typeBadge}
                <span class="adv-severity-badge" style="color:${sevColor(a.severity)}">${escHtml(a.severity)}</span>
                <span class="adv-title">${escHtml(a.title || 'Advisory #' + a.number)}</span>
                ${link}
            </div>
            <div class="adv-row-body">
                <div class="adv-affected">${escHtml(a.affected_versions || '')}</div>
                ${a.summary ? `<div class="adv-summary">${escHtml(a.summary.substring(0, 200))}${a.summary.length > 200 ? '\u2026' : ''}</div>` : ''}
                ${a.affected_cluster_count ? `<div class="adv-impact">${a.affected_cluster_count} cluster(s) affected</div>` : ''}
                ${clusterDetail}
                ${reasonHtml}
            </div>
        </div>`;
    }).join('');

    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">${headerBadges.join(' ')}</div>${rows}`;
}

// ── License & Adoption Section ──────────────────────────────────────────

async function renderLicenseSection(data, container) {
    const cs = data.cs_insights || {};

    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Fetching license data\u2026</div>';

    let licenseData = null;
    try {
        licenseData = await API.licenses(data.account_id, data.account_name);
    } catch (e) {
        console.warn("License fetch failed, showing CS data only", e);
    }

    const families = licenseData?.product_families || [];
    const products = licenseData?.products || [];
    const recs = licenseData?.recommendations || [];
    const sfFamilies = licenseData?.sf_asset_families || [];
    const totalLicenses = licenseData?.total_licenses || 0;
    const hasFamilies = families.length > 0 && families.some(f => (f.total || 0) > 0);

    const chartFamilies = hasFamilies
        ? families.filter(f => (f.total || 0) > 0)
        : (cs.adoption_products || []);
    const chartH = Math.max(220, Math.min(chartFamilies.length * 48 + 40, 500));

    const familyDetailBlocks = hasFamilies ? families.filter(f => (f.total || 0) > 0).map((f, fi) => {
        const tiers = (f.tiers || []).filter(t => (t.total || 0) > 0);
        const metric = (f.metric || "cores").toUpperCase();
        const tierRows = tiers.map(t => {
            const tPct = t.adoption_pct || 0;
            const tUsed = Math.round(t.used || 0);
            const tTotal = Math.round(t.total || 0);
            const tAvail = Math.max(0, tTotal - tUsed);
            const tierLabel = t.tier || "Base";
            return { label: `${f.family} ${tierLabel}`, pct: tPct, used: tUsed, total: tTotal, avail: tAvail, metric };
        });
        const chartId = `family-tier-chart-${fi}`;
        const chartH = Math.max(100, tierRows.length * 56 + 50);
        return `<div class="chart-card" style="margin-bottom:0">
            <h3>${escHtml(f.family)}
                <span style="font-weight:400;color:var(--text-muted);text-transform:none;letter-spacing:0;font-size:0.8rem;margin-left:8px">
                    ${Math.round(f.used || 0).toLocaleString()} / ${Math.round(f.total || 0).toLocaleString()} ${metric} (${f.adoption_pct || 0}%)
                </span>
            </h3>
            <div class="chart-wrap" style="height:${chartH}px"><canvas id="${chartId}"></canvas></div>
        </div>`;
    }).join("") : "";

    container.innerHTML = `
        <div class="section-stats-row">
            ${hasFamilies ? families.filter(f => (f.total || 0) > 0).map(f => {
                const pct = f.adoption_pct || 0;
                const color = pct < 20 ? 'critical' : pct < 50 ? 'high' : pct < 75 ? 'medium' : 'low';
                return `<div class="acct-stat">
                    <div class="acct-stat-value" style="color:var(--risk-${color})">${pct}%</div>
                    <div class="acct-stat-label">${escHtml(f.family)}</div>
                </div>`;
            }).join("") : `<div class="acct-stat"><div class="acct-stat-value">${cs.adoption_score || 0}%</div><div class="acct-stat-label">CS Adoption</div></div>`}
        </div>
        <div class="section-charts-row" style="grid-template-columns:1fr">
            <div class="chart-card">
                <h3>Per-Product Adoption${totalLicenses ? ` <span style="font-weight:400;color:var(--text-muted);text-transform:none;letter-spacing:0">(${totalLicenses} licenses)</span>` : ''}</h3>
                <div class="chart-wrap" style="height:${chartH}px"><canvas id="product-adoption-chart"></canvas></div>
            </div>
        </div>
        ${familyDetailBlocks ? `<div class="section-data-block" style="background:transparent;border:none;padding:0">
            <div class="section-charts-row" style="grid-template-columns:repeat(auto-fit,minmax(380px,1fr))">${familyDetailBlocks}</div>
        </div>` : ""}
        ${sfFamilies.length ? `<div class="section-data-block"><h3>SFDC Asset Families</h3><div class="mini-grid">${sfFamilies.filter(f => f.total_quantity > 0).map(f => statMini(f.family, `${f.active_quantity} / ${f.total_quantity}`, f.expired_quantity > 0 ? "high" : "")).join("")}</div></div>` : ""}
        ${recs.length ? `<div class="section-data-block"><h3>Adoption Recommendations (${recs.length})</h3>${recs.map(r => {
            const priColor = r.priority === "critical" ? "critical" : r.priority === "high" ? "high" : "medium";
            return `<div class="adoption-rec ${priColor}"><div class="adoption-rec-header"><span class="risk-badge ${priColor}" style="font-size:0.6rem;padding:2px 6px">${r.priority}</span><strong>${escHtml(r.area)}</strong></div><div class="adoption-rec-msg">${escHtml(r.message)}</div><div class="adoption-rec-action">${escHtml(r.action)}</div></div>`;
        }).join("")}</div>` : ""}`;

    if (typeof Chart !== "undefined" && chartFamilies.length) {
        const labels = chartFamilies.map(f => f.family || f.product || f.name || "");
        const values = chartFamilies.map(f => f.adoption_pct || 0);
        const colors = values.map(v => v < 20 ? "#ef4444" : v < 50 ? "#f59e0b" : v < 75 ? "#3b82f6" : "#10b981");

        const ctx = document.getElementById("product-adoption-chart").getContext("2d");
        activeChartInstances.push(new Chart(ctx, {
            type: "bar",
            data: {
                labels,
                datasets: [{
                    label: "Adoption %",
                    data: values,
                    backgroundColor: colors,
                    borderWidth: 0,
                    borderRadius: 4,
                    barPercentage: 0.65,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => {
                                const f = chartFamilies[ctx.dataIndex];
                                const used = Math.round(f.used || 0).toLocaleString();
                                const total = Math.round(f.total || 0).toLocaleString();
                                return `${ctx.parsed.x}% (${used} / ${total})`;
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        max: 100,
                        ticks: { color: "#94a3b8", callback: v => v + '%', font: { size: 11 } },
                        grid: { color: "rgba(148,163,184,0.08)" },
                    },
                    y: {
                        ticks: { color: "#cbd5e1", font: { size: 13, weight: '600' } },
                        grid: { display: false },
                    },
                },
            },
        }));

        // Per-family tier breakdown charts
        families.filter(f => (f.total || 0) > 0).forEach((f, fi) => {
            const tiers = (f.tiers || []).filter(t => (t.total || 0) > 0);
            if (!tiers.length) return;
            const chartId = `family-tier-chart-${fi}`;
            const el = document.getElementById(chartId);
            if (!el) return;

            const metric = (f.metric || "cores").toUpperCase();
            const tierLabels = tiers.map(t => t.tier || "Base");
            const usedData = tiers.map(t => Math.round(t.used || 0));
            const availData = tiers.map(t => Math.max(0, Math.round((t.total || 0) - (t.used || 0))));

            const tierCtx = el.getContext("2d");
            activeChartInstances.push(new Chart(tierCtx, {
                type: "bar",
                data: {
                    labels: tierLabels,
                    datasets: [
                        {
                            label: "Used",
                            data: usedData,
                            backgroundColor: "#f59e0b",
                            borderWidth: 0,
                            borderRadius: 3,
                            barPercentage: 0.6,
                        },
                        {
                            label: "Available",
                            data: availData,
                            backgroundColor: "rgba(148,163,184,0.18)",
                            borderWidth: 0,
                            borderRadius: 3,
                            barPercentage: 0.6,
                        },
                    ],
                },
                options: {
                    indexAxis: "y",
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: true,
                            position: "top",
                            align: "end",
                            labels: { color: "#94a3b8", boxWidth: 12, padding: 12, font: { size: 10 } },
                        },
                        tooltip: {
                            callbacks: {
                                label: (tip) => {
                                    const t = tiers[tip.dataIndex];
                                    const pct = t.adoption_pct || 0;
                                    return `${tip.dataset.label}: ${tip.parsed.x.toLocaleString()} ${metric}` +
                                        (tip.datasetIndex === 0 ? ` (${pct}% adopted)` : '');
                                },
                                afterBody: (items) => {
                                    const t = tiers[items[0].dataIndex];
                                    return `Total: ${Math.round(t.total || 0).toLocaleString()} ${metric}`;
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            stacked: true,
                            ticks: {
                                color: "#94a3b8",
                                font: { size: 10 },
                                callback: v => v >= 1000 ? Math.round(v / 1000) + 'K' : v,
                            },
                            grid: { color: "rgba(148,163,184,0.08)" },
                        },
                        y: {
                            stacked: true,
                            ticks: { color: "#cbd5e1", font: { size: 12, weight: '600' } },
                            grid: { display: false },
                        },
                    },
                },
            }));
        });
    }
}

// ── Renewal Insights Section ────────────────────────────────────────────

function renderRenewalSection(data, container) {
    const cs = data.cs_insights || {};
    const days = cs.days_to_renewal;
    const riskScore = cs.renewal_risk_score;
    const riskLabel = cs.renewal_risk_label || "unknown";
    const value = cs.total_renewal_value || cs.contract_value || 0;
    const count = cs.renewal_count || 0;
    const src = cs.renewal_source || "estimated";
    const signals = cs.cs_risk_signals || [];
    const renewals = cs.renewals || [];

    const daysColor = days != null ? (days < 60 ? 'critical' : days < 180 ? 'high' : 'low') : 'low';
    const riskColorVal = riskScore != null ? (riskScore >= 70 ? 'critical' : riskScore >= 40 ? 'high' : 'low') : 'low';
    const valueStr = value > 0 ? '$' + value.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '\u2014';

    const renewalRecs = (data.recommendations || []).filter(r =>
        r.toLowerCase().includes("renewal") || r.toLowerCase().includes("sentiment") ||
        r.toLowerCase().includes("health") || r.toLowerCase().includes("engagement") ||
        r.toLowerCase().includes("customer success")
    );

    const hasProductData = renewals.some(r => (r.products || []).length > 0);
    const timelineH = Math.max(220, Math.min(renewals.length * 50 + 60, 420));

    const renewalRows = renewals.map((r, i) => {
        const rVal = r.value || 0;
        const rColor = r.risk_label === 'On Track' ? 'low' : r.risk_label === 'At Risk' ? 'high' : (r.risk_score || 0) >= 70 ? 'critical' : (r.risk_score || 0) >= 40 ? 'high' : 'low';
        const products = r.products || [];
        const daysToThis = r.close_date ? Math.round((new Date(r.close_date + 'T00:00:00Z') - new Date()) / 86400000) : null;
        const daysStr = daysToThis != null ? `${daysToThis}d` : '';
        const daysC = daysToThis != null ? (daysToThis < 60 ? 'critical' : daysToThis < 180 ? 'high' : 'low') : '';

        const productRows = products.filter(p => p.value > 0 || p.qty > 0).map(p =>
            `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:0.8rem">
                <span style="color:var(--text-secondary);min-width:60px;font-weight:600">${escHtml(p.product)}</span>
                <span style="color:var(--text-muted);flex:1;text-align:center">${p.qty.toLocaleString()} units</span>
                <span style="color:var(--text-primary);font-weight:600;font-variant-numeric:tabular-nums">$${p.value.toLocaleString(undefined, {maximumFractionDigits: 0})}</span>
            </div>`
        ).join('');

        return `<div class="renewal-row" style="border:1px solid rgba(148,163,184,0.12);border-radius:8px;padding:12px 16px;margin-bottom:8px;background:rgba(15,23,42,0.4)">
            <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer" onclick="this.parentElement.querySelector('.renewal-detail').classList.toggle('hidden')">
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600;font-size:0.88rem;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(r.name || 'Renewal')}</div>
                    <div style="font-size:0.75rem;color:var(--text-muted);margin-top:2px">${escHtml(r.stage || '')} &middot; ${r.close_date || ''}</div>
                </div>
                <div style="display:flex;gap:16px;align-items:center;flex-shrink:0;margin-left:12px">
                    ${daysStr ? `<span style="font-size:0.8rem;color:var(--risk-${daysC});font-weight:600">${daysStr}</span>` : ''}
                    <span style="font-size:0.95rem;font-weight:700;font-variant-numeric:tabular-nums;color:var(--text-primary)">${rVal > 0 ? '$' + rVal.toLocaleString(undefined, {maximumFractionDigits: 0}) : '$0'}</span>
                    <span class="risk-badge ${rColor}" style="font-size:0.65rem;padding:2px 8px">${escHtml(r.risk_label || '')}</span>
                    <span style="color:var(--text-muted);font-size:0.75rem">${products.length ? '&#9660;' : ''}</span>
                </div>
            </div>
            <div class="renewal-detail hidden" style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(148,163,184,0.1)">
                ${productRows || '<div style="font-size:0.8rem;color:var(--text-muted)">No product line items available</div>'}
            </div>
        </div>`;
    }).join('');

    container.innerHTML = `
        <div class="renewal-hero-row">
            <div class="renewal-hero-item">
                <div class="renewal-hero-value" style="color:var(--risk-${daysColor})">${days != null ? days : '\u2014'}</div>
                <div class="renewal-hero-label">Days to Renewal</div>
            </div>
            <div class="renewal-hero-item">
                <div class="renewal-hero-value" style="color:var(--risk-${riskColorVal})">${riskScore != null ? riskScore.toFixed(0) + '%' : '\u2014'}</div>
                <div class="renewal-hero-label">Renewal Risk</div>
            </div>
            <div class="renewal-hero-item">
                <div class="renewal-hero-value">${valueStr}</div>
                <div class="renewal-hero-label">${count} Renewal${count !== 1 ? 's' : ''} Value</div>
            </div>
            <div class="renewal-hero-item">
                <div class="renewal-hero-value">${escHtml(riskLabel)}</div>
                <div class="renewal-hero-label">Status <span class="source-tag source-${src}">${src}</span></div>
            </div>
        </div>
        <div class="section-charts-row">
            <div class="chart-card">
                <h3>Renewal Timeline</h3>
                <div class="chart-wrap" style="height:${timelineH}px"><canvas id="renewal-timeline-chart"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>Renewal Value by Product</h3>
                <div class="chart-wrap"><canvas id="renewal-product-chart"></canvas></div>
            </div>
        </div>
        ${renewalRows ? `<div class="section-data-block"><h3>Renewal Details <span style="font-weight:400;color:var(--text-muted);font-size:0.8rem">(click to expand)</span></h3>${renewalRows}</div>` : ''}
        ${signals.length ? `<div class="section-data-block"><h3>Risk Signals</h3>${signals.map(s => `<div class="recommendation-item" style="color:var(--risk-high)">&#9888; ${escHtml(s)}</div>`).join("")}</div>` : ''}
        ${renewalRecs.length ? `<div class="section-data-block"><h3>Recommendations</h3>${renewalRecs.map(r => `<div class="recommendation-item">${escHtml(r)}</div>`).join("")}</div>` : ''}`;

    if (typeof Chart !== "undefined") {
        // Timeline chart — horizontal bars showing $ value per renewal by date
        if (renewals.length) {
            const sorted = [...renewals].sort((a, b) => (a.close_date || '').localeCompare(b.close_date || ''));
            const tlLabels = sorted.map(r => {
                const d = r.close_date || '';
                const short = d ? new Date(d + 'T00:00:00Z').toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) : '?';
                return short;
            });
            const tlValues = sorted.map(r => r.value || 0);
            const tlColors = sorted.map(r => {
                const rl = (r.risk_label || '').toLowerCase();
                if (rl.includes('churn') || rl.includes('at risk')) return '#ef4444';
                if (rl.includes('slip') || rl.includes('competition')) return '#f59e0b';
                return '#10b981';
            });

            const ctx1 = document.getElementById("renewal-timeline-chart").getContext("2d");
            activeChartInstances.push(new Chart(ctx1, {
                type: "bar",
                data: {
                    labels: tlLabels,
                    datasets: [{
                        label: "Value ($)",
                        data: tlValues,
                        backgroundColor: tlColors,
                        borderWidth: 0,
                        borderRadius: 4,
                        barPercentage: 0.55,
                    }],
                },
                options: {
                    indexAxis: "y",
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                title: (items) => {
                                    const r = sorted[items[0].dataIndex];
                                    return r.name || r.close_date || '';
                                },
                                label: (tip) => `$${tip.parsed.x.toLocaleString()}`,
                                afterLabel: (tip) => {
                                    const r = sorted[tip.dataIndex];
                                    const d = r.close_date ? Math.round((new Date(r.close_date + 'T00:00:00Z') - new Date()) / 86400000) : null;
                                    return [
                                        `Stage: ${r.stage || '?'}`,
                                        `Risk: ${r.risk_label || '?'}`,
                                        d != null ? `${d} days away` : '',
                                    ].filter(Boolean);
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            ticks: {
                                color: "#94a3b8",
                                font: { size: 10 },
                                callback: v => v >= 1000000 ? '$' + (v / 1000000).toFixed(1) + 'M' : v >= 1000 ? '$' + Math.round(v / 1000) + 'K' : '$' + v,
                            },
                            grid: { color: "rgba(148,163,184,0.08)" },
                        },
                        y: {
                            ticks: { color: "#cbd5e1", font: { size: 11, weight: '500' } },
                            grid: { display: false },
                        },
                    },
                },
            }));
        }

        // Product doughnut — aggregate value across all renewals by product family
        const prodTotals = {};
        renewals.forEach(r => {
            (r.products || []).forEach(p => {
                if (!p.value) return;
                prodTotals[p.product] = (prodTotals[p.product] || 0) + p.value;
            });
        });
        const prodEntries = Object.entries(prodTotals).sort((a, b) => b[1] - a[1]);
        if (prodEntries.length) {
            const prodColors = { NCI: "#3b82f6", NCM: "#06b6d4", NKP: "#a855f7", NUS: "#10b981", NDB: "#f59e0b", NAI: "#ec4899", Support: "#94a3b8" };
            const fallbackPalette = ["#6366f1", "#14b8a6", "#f97316", "#8b5cf6", "#ef4444", "#22d3ee"];
            const pLabels = prodEntries.map(([k]) => k);
            const pValues = prodEntries.map(([, v]) => v);
            const pColors = pLabels.map((l, i) => prodColors[l] || fallbackPalette[i % fallbackPalette.length]);

            const ctx2 = document.getElementById("renewal-product-chart").getContext("2d");
            activeChartInstances.push(new Chart(ctx2, {
                type: "doughnut",
                data: {
                    labels: pLabels,
                    datasets: [{ data: pValues, backgroundColor: pColors, borderWidth: 0 }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: "55%",
                    plugins: {
                        legend: {
                            display: true,
                            position: "right",
                            labels: { color: "#cbd5e1", padding: 12, font: { size: 11 } },
                        },
                        tooltip: {
                            callbacks: {
                                label: (tip) => {
                                    const total = pValues.reduce((a, b) => a + b, 0);
                                    const pct = total > 0 ? Math.round(tip.parsed / total * 100) : 0;
                                    return `${tip.label}: $${tip.parsed.toLocaleString()} (${pct}%)`;
                                },
                            },
                        },
                    },
                },
            }));
        }
    }
}

// ── CXM Risk Flags Section ──────────────────────────────────────────────

async function renderCxmFlagsSection(data, container) {
    const accountId = data.account_id;
    let customRisks = data.custom_risks || [];

    if (!riskCategoriesCache) {
        try { riskCategoriesCache = await API.riskCategories(); } catch (_) { riskCategoriesCache = []; }
    }

    const active = customRisks.filter(r => r.status === "active");
    const resolved = customRisks.filter(r => r.status !== "active");

    const categoryOptions = riskCategoriesCache.map(c =>
        `<option value="${c.id}">${escHtml(c.label)} (${c.group})</option>`
    ).join("");

    container.innerHTML = `
        <div class="section-data-block">
            <h3>Add Risk Flag</h3>
            <div class="cxm-add-form" id="cxm-add-form">
                <div class="cxm-form-row">
                    <label>Category</label>
                    <select id="cxm-category">${categoryOptions}</select>
                </div>
                <div class="cxm-form-row">
                    <label>Severity</label>
                    <div class="severity-selector" id="cxm-severity">
                        <button class="sev-btn" data-sev="critical">Critical</button>
                        <button class="sev-btn" data-sev="high">High</button>
                        <button class="sev-btn active" data-sev="medium">Medium</button>
                        <button class="sev-btn" data-sev="low">Low</button>
                    </div>
                </div>
                <div class="cxm-form-row">
                    <label>Title (optional)</label>
                    <input type="text" id="cxm-title" placeholder="Custom title or leave blank for category default">
                </div>
                <div class="cxm-form-row">
                    <label>Notes</label>
                    <textarea id="cxm-notes" rows="3" placeholder="Details, context, source of information\u2026"></textarea>
                </div>
                <button class="btn btn-primary btn-sm" onclick="submitCxmFlag('${escAttr(accountId)}')">Add Risk Flag</button>
                <span id="cxm-add-status" class="cxm-status-msg"></span>
            </div>
        </div>
        <div class="section-data-block">
            <h3>Active Flags (${active.length})</h3>
            <div id="cxm-active-list">
                ${active.length ? active.map(r => renderCxmFlagCard(r, accountId)).join("") : '<p class="text-muted">No active risk flags for this account.</p>'}
            </div>
        </div>
        ${resolved.length ? `<div class="section-data-block"><h3>Resolved / Mitigated (${resolved.length})</h3><div id="cxm-resolved-list">${resolved.map(r => renderCxmFlagCard(r, accountId, true)).join("")}</div></div>` : ''}`;

    document.querySelectorAll("#cxm-severity .sev-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll("#cxm-severity .sev-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
        });
    });
}

function renderCxmFlagCard(flag, accountId, isResolved) {
    const catInfo = riskCategoriesCache?.find(c => c.id === flag.category);
    const catLabel = catInfo ? catInfo.label : flag.category;
    const groupLabel = catInfo ? catInfo.group : "";
    const created = flag.created_at ? new Date(flag.created_at).toLocaleDateString() : "";
    const actions = isResolved ? "" : `
        <div class="cxm-flag-actions">
            <button class="btn btn-secondary btn-sm" onclick="resolveCxmFlag('${escAttr(accountId)}', '${escAttr(flag.id)}', 'mitigated')">Mitigate</button>
            <button class="btn btn-secondary btn-sm" onclick="resolveCxmFlag('${escAttr(accountId)}', '${escAttr(flag.id)}', 'resolved')">Resolve</button>
            <button class="btn btn-secondary btn-sm" style="color:var(--risk-critical)" onclick="deleteCxmFlag('${escAttr(accountId)}', '${escAttr(flag.id)}')">Delete</button>
        </div>`;
    return `<div class="cxm-flag-card ${isResolved ? 'resolved' : ''}" data-risk-id="${flag.id}">
        <div class="cxm-flag-header">
            <span class="risk-badge ${flag.severity}">${flag.severity}</span>
            <span class="cxm-flag-group">${escHtml(groupLabel)}</span>
            <span class="cxm-flag-title">${escHtml(flag.title || catLabel)}</span>
            ${flag.status !== 'active' ? `<span class="cxm-flag-status-badge">${flag.status}</span>` : ''}
        </div>
        ${flag.notes ? `<div class="cxm-flag-notes">${escHtml(flag.notes)}</div>` : ''}
        <div class="cxm-flag-meta">
            <span>${escHtml(flag.created_by || 'Unknown')}</span> &middot; <span>${created}</span>
            ${flag.resolved_at ? ` &middot; Resolved: ${new Date(flag.resolved_at).toLocaleDateString()}` : ''}
        </div>
        ${actions}
    </div>`;
}

async function submitCxmFlag(accountId) {
    const category = document.getElementById("cxm-category").value;
    const sevBtn = document.querySelector("#cxm-severity .sev-btn.active");
    const severity = sevBtn ? sevBtn.dataset.sev : "medium";
    const title = document.getElementById("cxm-title").value.trim();
    const notes = document.getElementById("cxm-notes").value.trim();
    const statusEl = document.getElementById("cxm-add-status");

    try {
        statusEl.textContent = "Adding\u2026";
        statusEl.className = "cxm-status-msg info";
        await API.addCustomRisk(accountId, { category, severity, title, notes, created_by: "cxm_user" });
        document.getElementById("cxm-title").value = "";
        document.getElementById("cxm-notes").value = "";
        statusEl.textContent = "Risk flag added!";
        statusEl.className = "cxm-status-msg success";
        const freshData = await API.account(accountId, currentAccountName, true);
        currentAccountData = freshData;
        renderCxmFlagsSection(freshData, document.getElementById("section-content"));
    } catch (err) {
        statusEl.textContent = "Failed to add: " + err.message;
        statusEl.className = "cxm-status-msg error";
    }
}

async function resolveCxmFlag(accountId, riskId, status) {
    try {
        await API.updateCustomRisk(accountId, riskId, { status });
        const freshData = await API.account(accountId, currentAccountName, true);
        currentAccountData = freshData;
        renderCxmFlagsSection(freshData, document.getElementById("section-content"));
    } catch (err) {
        console.error("Failed to resolve flag:", err);
    }
}

async function deleteCxmFlag(accountId, riskId) {
    if (!confirm("Delete this risk flag permanently?")) return;
    try {
        await API.deleteCustomRisk(accountId, riskId);
        const freshData = await API.account(accountId, currentAccountName, true);
        currentAccountData = freshData;
        renderCxmFlagsSection(freshData, document.getElementById("section-content"));
    } catch (err) {
        console.error("Failed to delete flag:", err);
    }
}

// ── Account Financials Section ──────────────────────────────────────────

async function renderFinancialsSection(data, container) {
    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading financial data\u2026</div>';
    let fin;
    try {
        fin = await API.financials(data.account_id);
    } catch (err) {
        container.innerHTML = '<div class="empty-state"><p>Failed to load financial data.</p></div>';
        console.error(err);
        return;
    }

    const fmtCurrency = (v) => {
        if (v == null) return "\u2014";
        if (v >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
        if (v >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
        if (v >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
        return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
    };
    const fmtNum = (v) => v != null ? v.toLocaleString() : "\u2014";

    const metrics = [
        { label: "Annual Revenue", value: fmtCurrency(fin.annual_revenue), raw: fin.annual_revenue },
        { label: "Active Renewal ARR", value: fmtCurrency(fin.arr), raw: fin.arr },
        { label: "TCV Bookings", value: fmtCurrency(fin.tcv_bookings), raw: fin.tcv_bookings },
        { label: "Total Lifetime Bookings", value: fmtCurrency(fin.total_lifetime_bookings), raw: fin.total_lifetime_bookings },
        { label: "Open Pipeline", value: fmtCurrency(fin.open_pipeline), raw: fin.open_pipeline },
        { label: "Open Renewal Pipeline", value: fmtCurrency(fin.open_renewal_pipeline), raw: fin.open_renewal_pipeline },
        { label: "Avg Deal Size", value: fmtCurrency(fin.avg_deal_size), raw: fin.avg_deal_size },
    ];

    const signals = [];
    if (fin.annual_revenue && fin.arr && fin.arr > 0) {
        const pct = (fin.arr / fin.annual_revenue * 100).toFixed(1);
        signals.push({ type: "info", text: `Nutanix ARR is ${pct}% of company annual revenue` });
    }
    if (fin.arr == null || fin.arr === 0) {
        signals.push({ type: "warn", text: "No ARR data available \u2014 account may lack active subscriptions" });
    }
    if (fin.open_pipeline && fin.open_pipeline > 0) {
        signals.push({ type: "positive", text: `Active new-business pipeline: ${fmtCurrency(fin.open_pipeline)}` });
    }
    if (fin.open_renewal_pipeline && fin.open_renewal_pipeline > 0) {
        signals.push({ type: "info", text: `Open renewal pipeline: ${fmtCurrency(fin.open_renewal_pipeline)}` });
    }
    if (fin.last_transaction_date) {
        const daysSince = Math.floor((Date.now() - new Date(fin.last_transaction_date).getTime()) / 86400000);
        if (daysSince > 365) {
            signals.push({ type: "warn", text: `Last transaction was ${daysSince} days ago \u2014 potential stalled growth` });
        } else {
            signals.push({ type: "info", text: `Last new ACV transaction: ${fin.last_transaction_date}` });
        }
    }
    const ev = fin.employee_verification || {};
    const empCount = ev.chosen_value || fin.number_of_employees;
    if (empCount && fin.annual_revenue) {
        const revPerEmp = fin.annual_revenue / empCount;
        signals.push({ type: "info", text: `Revenue per employee: ${fmtCurrency(revPerEmp)}` });
    }
    if (ev.confidence === "high") {
        signals.push({ type: "positive", text: "Employee count verified — multiple sources agree" });
    } else if (ev.confidence === "low" || ev.confidence === "none") {
        signals.push({ type: "warn", text: "Employee count has low confidence — limited data sources" });
    }

    const metaItems = [];
    if (fin.account_segment) metaItems.push(`<span class="fin-meta-tag">${escHtml(fin.account_segment)}</span>`);
    if (fin.total_closed_won_opps) metaItems.push(`<span class="fin-meta-tag">${fin.total_closed_won_opps} Closed Won Opportunities</span>`);
    if (fin.contract_start_date) metaItems.push(`<span class="fin-meta-tag">Contract since ${fin.contract_start_date}</span>`);
    if (fin.last_transaction_date) metaItems.push(`<span class="fin-meta-tag">Last txn ${fin.last_transaction_date}</span>`);

    const revenueMetrics = metrics.slice(0, 4);
    const pipelineMetrics = metrics.slice(4);

    container.innerHTML = `
        ${metaItems.length ? `<div class="fin-meta-row">${metaItems.join("")}</div>` : ""}
        <div class="fin-panels">
            <div class="fin-panel">
                <h3 class="fin-panel-title">Revenue & Contract Breakdown</h3>
                <div class="fin-panel-body">
                    <div class="fin-panel-chart"><canvas id="fin-revenue-chart"></canvas></div>
                    <div class="fin-panel-kpis">
                        ${revenueMetrics.map(m => `<div class="fin-kpi"><span class="fin-kpi-val">${m.value}</span><span class="fin-kpi-lbl">${m.label}</span></div>`).join("")}
                    </div>
                </div>
            </div>
            <div class="fin-panel">
                <h3 class="fin-panel-title">Pipeline Overview</h3>
                <div class="fin-panel-body">
                    <div class="fin-panel-chart"><canvas id="fin-pipeline-chart"></canvas></div>
                    <div class="fin-panel-kpis">
                        ${pipelineMetrics.map(m => `<div class="fin-kpi"><span class="fin-kpi-val">${m.value}</span><span class="fin-kpi-lbl">${m.label}</span></div>`).join("")}
                    </div>
                </div>
            </div>
        </div>
        ${_renderEmployeeVerificationCard(fin)}
        ${signals.length ? `
        <div class="fin-signals-section">
            <h3 class="fin-panel-title">Signals & Observations</h3>
            <div class="fin-signals">
                ${signals.map(s => `<div class="fin-signal fin-signal-${s.type}">${escHtml(s.text)}</div>`).join("")}
            </div>
        </div>` : ""}`;

    _renderFinRevenueChart(fin);
    _renderFinPipelineChart(fin);
}

function _renderEmployeeVerificationCard(fin) {
    const ev = fin.employee_verification;
    const sfStd = fin.number_of_employees;
    if (!ev && !sfStd) return "";

    const fmtN = (v) => v != null && v > 0 ? v.toLocaleString() : null;
    const chosen = ev ? ev.chosen_value : sfStd;
    const confidence = ev ? ev.confidence : (sfStd ? "low" : "none");
    const src = ev ? ev.sources || {} : {};

    const sourceLabel = {
        wikidata: "Wikidata (Public)",
        sf_dnb: "D&B Enrichment",
        sf_imputed: "Imputed (SF)",
        sf_standard: "Salesforce Std",
        consensus: "Multi-Source Consensus",
    };
    const chosenSrc = ev ? (ev.chosen_source || "sf_standard") : "sf_standard";

    const confClass = { high: "emp-conf-high", medium: "emp-conf-med", low: "emp-conf-low", none: "emp-conf-none" }[confidence] || "emp-conf-none";
    const confLabel = { high: "High", medium: "Medium", low: "Low", none: "N/A" }[confidence] || "N/A";

    const sourceRows = [
        { key: "wikidata", label: "Wikidata (Public)", val: src.wikidata },
        { key: "sf_dnb", label: "D&B Enrichment", val: src.sf_dnb },
        { key: "sf_imputed", label: "Imputed (SF)", val: src.sf_imputed },
        { key: "sf_standard", label: "Salesforce Std", val: src.sf_standard || sfStd },
    ];
    const srcHtml = sourceRows.map(r => {
        const display = fmtN(r.val) || "\u2014";
        const active = ev && ev.chosen_source === r.key ? " emp-src-active" : "";
        return `<div class="emp-src-row${active}"><span class="emp-src-label">${r.label}</span><span class="emp-src-val">${display}</span></div>`;
    }).join("");

    return `
    <div class="emp-verify-card">
        <div class="emp-verify-main">
            <div class="emp-verify-chosen">
                <span class="emp-verify-number">${fmtN(chosen) || "\u2014"}</span>
                <span class="emp-verify-label">Employees</span>
                <span class="emp-conf-badge ${confClass}">${confLabel} confidence</span>
            </div>
            <div class="emp-verify-source">
                <span class="emp-verify-via">via ${sourceLabel[chosenSrc] || chosenSrc}</span>
            </div>
        </div>
        <details class="emp-verify-details">
            <summary>View all sources</summary>
            <div class="emp-src-grid">${srcHtml}</div>
        </details>
    </div>`;
}

function _renderFinChart(canvasId, items) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    const labels = [], values = [], colors = [];
    for (const [lbl, val, col] of items) {
        if (val && val > 0) { labels.push(lbl); values.push(val); colors.push(col); }
    }
    if (!values.length) { ctx.closest(".fin-panel-chart").style.display = "none"; return; }
    const chart = new Chart(ctx, {
        type: "doughnut",
        data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0, hoverOffset: 6 }] },
        options: {
            responsive: true, maintainAspectRatio: true,
            cutout: "62%",
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { label: (c) => c.label + ": $" + c.raw.toLocaleString(undefined, { maximumFractionDigits: 0 }) } },
            },
        },
    });
    activeChartInstances.push(chart);
}

function _renderFinRevenueChart(fin) {
    _renderFinChart("fin-revenue-chart", [
        ["Annual Revenue", fin.annual_revenue, "#3b9eff"],
        ["Active Renewal ARR", fin.arr, "#2ed573"],
        ["TCV Bookings", fin.tcv_bookings, "#ffa502"],
        ["Lifetime Bookings", fin.total_lifetime_bookings, "#9c88ff"],
    ]);
}

function _renderFinPipelineChart(fin) {
    _renderFinChart("fin-pipeline-chart", [
        ["Open New Pipeline", fin.open_pipeline, "#3b9eff"],
        ["Open Renewal Pipeline", fin.open_renewal_pipeline, "#2ed573"],
        ["Avg Deal Size", fin.avg_deal_size, "#ffa502"],
    ]);
}

// ── Email & Outreach Section ────────────────────────────────────────────

function renderGleanSection(data, container) {
    const gl = data.engagement || data.glean || {};
    const mentions = gl.total_mentions ?? 0;
    const recent = gl.recent_mentions_30d ?? 0;
    const escalation = gl.escalation_mentions ?? 0;
    const comments = gl.knowledge_articles ?? 0;
    const trend = gl.mention_trend || "none";
    const sources = gl.top_sources || [];
    const results = gl.top_results || [];
    const signals = gl.glean_risk_signals || [];
    const contributors = gl.unique_contributors ?? 0;

    const trendIcon = trend === "increasing" ? "\u2197" : trend === "declining" ? "\u2198" : trend === "stable" ? "\u2192" : "\u2014";
    const trendColor = trend === "increasing" ? "var(--risk-low)" : trend === "declining" ? "var(--risk-high)" : "var(--text-secondary)";
    const isLive = gl.source === "salesforce_engagement";

    container.innerHTML = `
        <div class="section-charts-row">
            <div class="chart-card">
                <h3>Glean Overview ${isLive ? '<span class="source-tag" style="background:rgba(6,182,212,0.15);color:#06b6d4;font-size:0.7rem;margin-left:8px">LIVE from SF</span>' : ''}</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;padding:16px">
                    <div class="acct-stat"><div class="acct-stat-value">${mentions}</div><div class="acct-stat-label">Total Touchpoints</div></div>
                    <div class="acct-stat"><div class="acct-stat-value">${recent}</div><div class="acct-stat-label">Recent (30d)</div></div>
                    <div class="acct-stat"><div class="acct-stat-value" ${escalation > 0 ? 'style="color:var(--risk-high)"' : ''}>${escalation}</div><div class="acct-stat-label">Escalation Refs</div></div>
                    <div class="acct-stat"><div class="acct-stat-value">${gl.task_count ?? 0}</div><div class="acct-stat-label">Tasks</div></div>
                    <div class="acct-stat"><div class="acct-stat-value">${gl.event_count ?? 0}</div><div class="acct-stat-label">Meetings</div></div>
                    <div class="acct-stat"><div class="acct-stat-value">${comments}</div><div class="acct-stat-label">Internal Notes</div></div>
                    <div class="acct-stat"><div class="acct-stat-value">${contributors}</div><div class="acct-stat-label">Contributors</div></div>
                    <div class="acct-stat" style="grid-column:span 2"><div class="acct-stat-value" style="color:${trendColor}">${trendIcon} ${trend}</div><div class="acct-stat-label">Activity Trend</div></div>
                </div>
            </div>
            <div class="chart-card">
                <h3>Activity Breakdown</h3>
                <div class="chart-wrap"><canvas id="glean-sources-chart"></canvas></div>
            </div>
        </div>
        ${signals.length ? `<div class="section-data-block">
            <h3>Risk Signals (${signals.length})</h3>
            ${signals.map(s => `<div class="recommendation-item">${escHtml(s)}</div>`).join("")}
        </div>` : ''}
        <div class="section-data-block">
            <h3>Recent Activities (${results.length})</h3>
            ${results.length ? results.map(r => `
                <div class="factor-row" style="flex-direction:column;align-items:flex-start;gap:4px">
                    <div style="display:flex;gap:8px;align-items:center;width:100%">
                        <span class="source-tag glean">${escHtml(r.source || 'unknown')}</span>
                        <span class="desc" style="font-weight:600">${r.url ? `<a href="${escAttr(r.url)}" target="_blank" style="color:var(--accent)">${escHtml(r.title)}</a>` : escHtml(r.title)}</span>
                    </div>
                    ${r.snippet ? `<div style="color:var(--text-secondary);font-size:0.82rem;padding-left:8px">${escHtml(r.snippet)}</div>` : ''}
                    <div style="display:flex;gap:12px;font-size:0.75rem;color:var(--text-muted);padding-left:8px">
                        ${r.author ? `<span>${escHtml(r.author)}</span>` : ''}
                        ${r.date ? `<span>${r.date.substring(0, 10)}</span>` : ''}
                    </div>
                </div>
            `).join("") : '<p class="text-muted">No internal activities found for this account.</p>'}
        </div>`;

    if (sources.length && typeof Chart !== "undefined") {
        const ctx = document.getElementById("glean-sources-chart").getContext("2d");
        const palette = ["#3b9eff", "#2ed573", "#ffa502", "#ff4757", "#a29bfe", "#fd79a8", "#00cec9", "#636e72"];
        new Chart(ctx, {
            type: "doughnut",
            data: {
                labels: sources.map(s => s.source),
                datasets: [{
                    data: sources.map(s => s.count),
                    backgroundColor: sources.map((_, i) => palette[i % palette.length]),
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: {
                    legend: { position: "right", labels: { color: "#a0b4c8", font: { size: 12 } } },
                },
            },
        });
    }
}

// ── Planhat Section ─────────────────────────────────────────────────────

function _planhatTileMetrics(ph) {
    if (!ph || !ph.planhat_company_id) {
        return ["Not mapped yet", "Run scripts/planhat_login.py"];
    }
    const out = [];
    if (ph.health_score != null) {
        const arrow = ph.health_trend === "improving" ? "\u2197"
            : ph.health_trend === "declining" ? "\u2198" : "\u2192";
        out.push(`Health ${ph.health_score} ${arrow}`);
    }
    if (ph.lifecycle_phase) out.push(`Phase: ${ph.lifecycle_phase}`);
    if (ph.nps_score != null) out.push(`NPS ${ph.nps_score}`);
    if ((ph.open_tasks || []).length || (ph.open_conversations || []).length) {
        const tasks = (ph.open_tasks || []).length;
        const convos = (ph.open_conversations || []).length;
        out.push(`${tasks} tasks \u00b7 ${convos} convos`);
    }
    if (!out.length) out.push("No signals yet");
    return out.slice(0, 3);
}

function _planhatTileColor(ph) {
    if (!ph || !ph.planhat_company_id) return "neutral";
    if (ph.churn_risk_flag) return tileColor(80, [60, 40, 20]);
    if (ph.health_score == null) return "neutral";
    // Lower health score = higher risk. Invert into the same colour scale
    // tileColor uses elsewhere.
    return tileColor(100 - ph.health_score, [60, 40, 20]);
}

function renderPlanhatSection(data, container) {
    const ph = data.planhat || {};
    const mapped = !!ph.planhat_company_id;

    if (!mapped) {
        container.innerHTML = `
            <div class="empty-state">
                <p><strong>${escHtml(data.account_name)}</strong> is not mapped to a Planhat company yet.</p>
                <p style="font-size:0.85em;color:var(--text-muted)">
                    The next scheduled sync will attempt a case-insensitive exact-name match against the
                    configured Planhat tenant (<code>${escHtml(ph.source || "app.planhat.example")}</code>).
                    If the connector is not yet authenticated, run
                    <code>python scripts/planhat_login.py</code> once and set <code>PLANHAT_LIVE=true</code>.
                </p>
            </div>`;
        return;
    }

    const healthTrendIcon = ph.health_trend === "improving" ? "\u2197"
        : ph.health_trend === "declining" ? "\u2198" : "\u2192";
    const healthColor = ph.health_score == null ? "var(--text-secondary)"
        : ph.health_score >= 70 ? "var(--risk-low)"
        : ph.health_score >= 40 ? "var(--risk-medium)"
        : "var(--risk-high)";

    const renewalDays = ph.days_to_renewal;
    const renewalColor = renewalDays == null ? "var(--text-secondary)"
        : renewalDays < 60 ? "var(--risk-high)"
        : renewalDays < 180 ? "var(--risk-medium)"
        : "var(--risk-low)";

    const fmtMoney = v => v == null ? "\u2014" : "$" + Math.round(v).toLocaleString();
    const fmtDate = v => v ? String(v).slice(0, 10) : "\u2014";

    const taskRows = (ph.open_tasks || []).map(t => `
        <tr>
            <td>${escHtml(t.title || "(untitled)")}</td>
            <td>${escHtml(fmtDate(t.due))}</td>
            <td><span class="cell-meta">${escHtml(t.status || "open")}</span></td>
        </tr>`).join("");

    const convoRows = (ph.open_conversations || []).map(c => `
        <tr>
            <td>${escHtml(c.subject || "(no subject)")}</td>
            <td>${escHtml(c.type || "\u2014")}</td>
            <td>${escHtml(c.sentiment || "\u2014")}</td>
            <td>${escHtml(fmtDate(c.date))}</td>
        </tr>`).join("");

    const matchBadge = ph.match_method
        ? `<span class="source-tag" style="background:rgba(6,182,212,0.15);color:#06b6d4;font-size:0.7rem;margin-left:8px">match: ${escHtml(ph.match_method)}</span>`
        : "";

    container.innerHTML = `
        <div class="section-charts-row">
            <div class="chart-card">
                <h3>Planhat Overview ${matchBadge}</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;padding:16px">
                    <div class="acct-stat">
                        <div class="acct-stat-value" style="color:${healthColor}">${ph.health_score ?? '\u2014'} ${healthTrendIcon}</div>
                        <div class="acct-stat-label">Health (${escHtml(ph.health_trend || 'unknown')})</div>
                    </div>
                    <div class="acct-stat">
                        <div class="acct-stat-value">${escHtml(ph.lifecycle_phase || '\u2014')}</div>
                        <div class="acct-stat-label">Lifecycle Phase</div>
                    </div>
                    <div class="acct-stat">
                        <div class="acct-stat-value" ${ph.churn_risk_flag ? 'style="color:var(--risk-high)"' : ''}>${ph.churn_risk_flag ? 'AT RISK' : 'OK'}</div>
                        <div class="acct-stat-label">Churn Flag</div>
                    </div>
                    <div class="acct-stat">
                        <div class="acct-stat-value">${ph.nps_score ?? '\u2014'}</div>
                        <div class="acct-stat-label">NPS (${escHtml(fmtDate(ph.nps_responded_at))})</div>
                    </div>
                    <div class="acct-stat">
                        <div class="acct-stat-value" style="color:${renewalColor}">${renewalDays != null ? renewalDays + 'd' : '\u2014'}</div>
                        <div class="acct-stat-label">Days to Renewal</div>
                    </div>
                    <div class="acct-stat">
                        <div class="acct-stat-value">${fmtMoney(ph.arr)}</div>
                        <div class="acct-stat-label">ARR (MRR: ${fmtMoney(ph.mrr)})</div>
                    </div>
                </div>
            </div>
            <div class="chart-card">
                <h3>Recent Engagement</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:16px">
                    <div class="acct-stat">
                        <div class="acct-stat-value">${escHtml(fmtDate(ph.last_touch_at))}</div>
                        <div class="acct-stat-label">Last Touch</div>
                    </div>
                    <div class="acct-stat">
                        <div class="acct-stat-value">${escHtml(fmtDate(ph.last_meeting_at))}</div>
                        <div class="acct-stat-label">Last Meeting</div>
                    </div>
                    <div class="acct-stat" style="grid-column:span 2">
                        <div class="acct-stat-value">${escHtml(fmtDate(ph.last_email_at))}</div>
                        <div class="acct-stat-label">Last Email</div>
                    </div>
                </div>
                <p style="color:var(--text-muted);font-size:0.78rem;padding:0 16px 14px">
                    Planhat company id: <code>${escHtml(ph.planhat_company_id)}</code> \u00b7
                    Renewal date: ${escHtml(fmtDate(ph.renewal_date))}
                </p>
            </div>
        </div>

        <div class="section-data-block">
            <h3>Open Tasks (${(ph.open_tasks || []).length})</h3>
            ${taskRows ? `
                <table class="data-table">
                    <thead><tr><th>Title</th><th>Due</th><th>Status</th></tr></thead>
                    <tbody>${taskRows}</tbody>
                </table>` : `<div class="empty-state"><p>No open tasks in Planhat.</p></div>`}
        </div>

        <div class="section-data-block">
            <h3>Open Conversations (${(ph.open_conversations || []).length})</h3>
            ${convoRows ? `
                <table class="data-table">
                    <thead><tr><th>Subject</th><th>Type</th><th>Sentiment</th><th>Date</th></tr></thead>
                    <tbody>${convoRows}</tbody>
                </table>` : `<div class="empty-state"><p>No open conversations in Planhat.</p></div>`}
        </div>`;
}

function renderEmailSection(data, container) {
    container.innerHTML = `
        <div class="section-data-block">
            <h3>Email Draft & Outreach</h3>
            <div id="email-content">
                <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                    <button class="btn btn-primary btn-sm" onclick="loadEmailDraft('${escAttr(data.account_id)}', '${escAttr(data.account_name)}', false)">
                        Generate Email Draft
                    </button>
                    <button class="btn btn-secondary btn-sm" onclick="loadEmailDraft('${escAttr(data.account_id)}', '${escAttr(data.account_name)}', true)">
                        Include License Data
                    </button>
                </div>
                <p style="color:var(--text-muted);font-size:0.78rem;margin-top:6px">
                    Generates a professional email with flagged problems and recommendations, pre-populated with customer contacts from Salesforce.
                </p>
            </div>
        </div>`;
}

// ── My Accounts ─────────────────────────────────────────────────────────

function setupMyAccounts() {
    document.getElementById("btn-my-accounts").addEventListener("click", () => {
        document.getElementById("my-accounts-overlay").style.display = "flex";
        document.getElementById("my-accounts-username").focus();
    });
    document.getElementById("my-accounts-overlay").addEventListener("click", e => {
        if (e.target === e.currentTarget) closeMyAccountsModal();
    });
    document.getElementById("my-accounts-username").addEventListener("keydown", e => {
        if (e.key === "Enter") submitMyAccounts();
    });
}

function closeMyAccountsModal() {
    document.getElementById("my-accounts-overlay").style.display = "none";
}

async function submitMyAccounts() {
    const username = document.getElementById("my-accounts-username").value.trim();
    if (!username) return;
    closeMyAccountsModal();
    switchView("top-risks");
    showTableLoading();
    try {
        allAccounts = await API.myAccounts(username);
        currentSearchTerm = `my:${username}`;
        refresh();
    } catch (err) {
        showTableError("Failed to load your accounts.");
        console.error(err);
    }
}

// ── CSV Export ───────────────────────────────────────────────────────────

function exportCSV() {
    const params = currentSearchTerm ? `q=${encodeURIComponent(currentSearchTerm)}` : `limit=100`;
    window.open(`/api/export/csv?${params}`, "_blank");
}

// ── Helpers ─────────────────────────────────────────────────────────────

function scoreLevel(s) {
    if (s >= 12) return "critical";
    if (s >= 8) return "high";
    if (s >= 4) return "medium";
    return "low";
}

function riskColor(level) {
    const map = { critical: "#ef4444", high: "#f59e0b", medium: "#3b82f6", low: "#10b981" };
    return map[level] || "#64748b";
}

function escHtml(str) {
    if (str === null || str === undefined) return "";
    const s = String(str);
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
}

function escAttr(str) {
    if (!str) return "";
    return String(str).replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

function statMini(label, value, highlight) {
    const v = value !== undefined && value !== null ? value : "\u2014";
    const cls = highlight ? `highlight-${highlight}` : "";
    return `<div class="stat-mini ${cls}">
        <div class="stat-mini-label">${label}</div>
        <div class="stat-mini-value">${v}</div>
    </div>`;
}

// ── Outlook integration ─────────────────────────────────────────────────

async function loadOutlookStatus() {
    try {
        const st = await API.outlookStatus();
        outlookMode = st.mode || "disabled";
        outlookAuthUrl = st.auth_url || null;
        if (st.mode === "outlook_app" || st.mode === "power_automate") {
            outlookConnected = true;
        } else if (st.enabled && !outlookConnected) {
            outlookConnected = false;
        }
    } catch (e) {
        console.warn("Outlook status check failed", e);
    }
}

function setupOutlookListener() {
    window.addEventListener("message", e => {
        if (e.data && e.data.type === "outlook_auth_success") {
            outlookConnected = true;
            const emailContent = document.getElementById("email-content");
            if (emailContent) {
                const badge = emailContent.querySelector(".outlook-status");
                if (badge) {
                    badge.className = "outlook-status connected";
                    badge.textContent = "Outlook Connected";
                }
            }
        }
    });
}

// ── Email Draft ─────────────────────────────────────────────────────────

let currentEmailDraft = null;

async function loadEmailDraft(accountId, accountName, includeLicense) {
    const container = document.getElementById("email-content");
    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Generating email draft\u2026</div>';
    try {
        const data = await API.emailDraft(accountId, accountName, includeLicense);
        currentEmailDraft = data;
        renderEmailComposer(data, container, accountId, accountName);
    } catch (err) {
        container.innerHTML = '<p style="color:var(--risk-high);font-size:0.85rem">Failed to generate email draft.</p>';
        console.error(err);
    }
}

function renderEmailComposer(data, container, accountId, accountName) {
    const toEmails = data.to || [];
    const ccEmails = data.cc || [];
    const allContacts = data.all_contacts || [];
    const problems = data.flagged_problems || [];
    const actions = data.action_items || [];

    const outlookBtnClass = outlookConnected ? "btn-primary" : "btn-secondary";
    const outlookBtnDisabled = outlookConnected ? "" : "disabled";
    const outlookStatusClass = outlookConnected ? "connected" : "disconnected";
    const modeLabels = {
        "outlook_app": "Outlook Ready (Desktop App)",
        "power_automate": "Outlook Ready (Power Automate)",
        "ms_graph": "Outlook Connected (MS Graph)",
    };
    const outlookStatusText = outlookConnected
        ? (modeLabels[outlookMode] || "Outlook Connected")
        : (outlookAuthUrl ? "Connect Outlook" : "Outlook Not Configured");

    const problemsHtml = problems.length ? problems.map(p => {
        const sevColor = p.severity === "critical" ? "critical" : p.severity === "high" ? "high" : "medium";
        return `<div class="email-problem-item ${sevColor}">
            <span class="risk-badge ${sevColor}" style="font-size:0.6rem;padding:2px 6px">${p.severity}</span>
            <span class="source-tag ${p.source}" style="font-size:0.6rem">${p.source}</span>
            <span>${escHtml(p.description)}</span>
        </div>`;
    }).join("") : '<p style="color:var(--risk-low);font-size:0.85rem">No significant problems flagged.</p>';

    const actionsHtml = actions.length ? actions.map(a => {
        const priColor = a.priority === "critical" ? "critical" : a.priority === "high" ? "high" : "medium";
        return `<div class="email-action-item">
            <span class="risk-badge ${priColor}" style="font-size:0.6rem;padding:2px 6px">${a.priority}</span>
            <span>${escHtml(a.action)}</span>
        </div>`;
    }).join("") : '';

    const contactChips = allContacts.map(c => {
        const inTo = toEmails.includes(c.email);
        const inCc = ccEmails.includes(c.email);
        const state = inTo ? "to" : inCc ? "cc" : "none";
        const primaryTag = c.is_primary ? ' <span class="contact-primary-tag">Primary</span>' : '';
        return `<div class="contact-chip ${state}" data-email="${escHtml(c.email)}" onclick="toggleContactChip(this)">
            <div class="contact-chip-name">${escHtml(c.name)}${primaryTag}</div>
            <div class="contact-chip-meta">${escHtml(c.title || '')} &middot; ${escHtml(c.email)}</div>
            <div class="contact-chip-state">${state === 'to' ? 'TO' : state === 'cc' ? 'CC' : '\u2014'}</div>
        </div>`;
    }).join("");

    container.innerHTML = `
        <div class="email-composer">
            <div class="email-outlook-bar">
                <span class="outlook-status ${outlookStatusClass}" ${outlookAuthUrl && !outlookConnected ? `onclick="connectOutlook()" style="cursor:pointer"` : ''}>
                    ${outlookStatusText}
                </span>
            </div>
            <div class="email-flagged-section">
                <div class="email-section-title">Flagged Problems (${problems.length})</div>
                ${problemsHtml}
            </div>
            ${actionsHtml ? `<div class="email-actions-section"><div class="email-section-title">Recommendations (${actions.length})</div>${actionsHtml}</div>` : ''}
            <div class="email-contacts-section">
                <div class="email-section-title">Recipients (click to toggle To/CC/Remove)</div>
                <div class="contact-chips-grid">${contactChips}</div>
                <div class="email-add-recipient" style="margin-top:8px">
                    <input type="email" id="extra-email-input" placeholder="Add email address\u2026" style="flex:1">
                    <select id="extra-email-field" style="width:70px"><option value="to">To</option><option value="cc">CC</option></select>
                    <button class="btn btn-secondary btn-sm" onclick="addExtraRecipient()">Add</button>
                </div>
            </div>
            <div class="email-compose-section">
                <div class="email-section-title">Email Preview</div>
                <div class="email-field-row"><label>Subject</label><input type="text" id="email-subject" value="${escHtml(data.subject)}" class="email-input"></div>
                <div class="email-field-row"><label>To</label><div id="email-to-display" class="email-pills-display">${toEmails.map(e => `<span class="email-pill">${escHtml(e)}</span>`).join('') || '<span class="email-pill empty">No recipients</span>'}</div></div>
                <div class="email-field-row"><label>CC</label><div id="email-cc-display" class="email-pills-display">${ccEmails.map(e => `<span class="email-pill cc">${escHtml(e)}</span>`).join('') || '<span class="email-pill empty">None</span>'}</div></div>
                <div class="email-body-preview" id="email-body-preview">${data.body_html || ''}</div>
            </div>
            <div class="email-actions-bar">
                <button class="btn btn-primary btn-sm" onclick="copyEmailToClipboard()">Copy to Clipboard</button>
                <button class="btn btn-secondary btn-sm" onclick="copyEmailAsText()">Copy as Plain Text</button>
                <button class="btn ${outlookBtnClass} btn-sm" onclick="sendViaOutlook()" ${outlookBtnDisabled}>${outlookMode === 'power_automate' ? 'Send via Outlook' : 'Send via Outlook'}</button>
                <button class="btn ${outlookBtnClass} btn-sm" onclick="saveDraftOutlook()" ${outlookBtnDisabled}>Save as Draft</button>
                <button class="btn btn-secondary btn-sm" onclick="openInMailClient()">Open in Mail Client</button>
            </div>
            <div id="email-status-msg" class="email-status-msg" style="display:none"></div>
        </div>`;
}

function toggleContactChip(chip) {
    const states = ["to", "cc", "none"];
    const current = chip.classList.contains("to") ? "to" : chip.classList.contains("cc") ? "cc" : "none";
    const nextIdx = (states.indexOf(current) + 1) % states.length;
    const next = states[nextIdx];
    chip.classList.remove("to", "cc", "none");
    chip.classList.add(next);
    chip.querySelector(".contact-chip-state").textContent = next === "to" ? "TO" : next === "cc" ? "CC" : "\u2014";
    updateRecipientDisplays();
}

function addExtraRecipient() {
    const input = document.getElementById("extra-email-input");
    const field = document.getElementById("extra-email-field");
    const email = input.value.trim();
    if (!email || !email.includes("@")) return;
    const grid = document.querySelector(".contact-chips-grid");
    const state = field.value;
    const chip = document.createElement("div");
    chip.className = `contact-chip ${state}`;
    chip.dataset.email = email;
    chip.onclick = function() { toggleContactChip(this); };
    chip.innerHTML = `
        <div class="contact-chip-name">${escHtml(email)}</div>
        <div class="contact-chip-meta">Manually added</div>
        <div class="contact-chip-state">${state.toUpperCase()}</div>`;
    grid.appendChild(chip);
    input.value = "";
    updateRecipientDisplays();
}

function getRecipientsFromChips() {
    const chips = document.querySelectorAll(".contact-chip");
    const to = [];
    const cc = [];
    chips.forEach(c => {
        const email = c.dataset.email;
        if (!email) return;
        if (c.classList.contains("to")) to.push(email);
        else if (c.classList.contains("cc")) cc.push(email);
    });
    return { to, cc };
}

function updateRecipientDisplays() {
    const { to, cc } = getRecipientsFromChips();
    const toDisplay = document.getElementById("email-to-display");
    const ccDisplay = document.getElementById("email-cc-display");
    if (toDisplay) {
        toDisplay.innerHTML = to.length
            ? to.map(e => `<span class="email-pill">${escHtml(e)}</span>`).join('')
            : '<span class="email-pill empty">No recipients</span>';
    }
    if (ccDisplay) {
        ccDisplay.innerHTML = cc.length
            ? cc.map(e => `<span class="email-pill cc">${escHtml(e)}</span>`).join('')
            : '<span class="email-pill empty">None</span>';
    }
}

function showEmailStatus(msg, type) {
    const el = document.getElementById("email-status-msg");
    if (!el) return;
    el.style.display = "block";
    el.className = `email-status-msg ${type}`;
    el.textContent = msg;
    if (type === "success") setTimeout(() => { el.style.display = "none"; }, 5000);
}

async function copyEmailToClipboard() {
    if (!currentEmailDraft) return;
    try {
        const subject = document.getElementById("email-subject")?.value || currentEmailDraft.subject;
        const { to, cc } = getRecipientsFromChips();
        const header = `To: ${to.join(", ")}\nCC: ${cc.join(", ")}\nSubject: ${subject}\n\n`;
        const html = currentEmailDraft.body_html || "";
        const blob = new Blob([html], { type: "text/html" });
        const textBlob = new Blob([header + (currentEmailDraft.body_text || "")], { type: "text/plain" });
        await navigator.clipboard.write([new ClipboardItem({ "text/html": blob, "text/plain": textBlob })]);
        showEmailStatus("Email copied to clipboard (HTML + plain text)", "success");
    } catch (err) {
        try {
            const { to, cc } = getRecipientsFromChips();
            const subject = document.getElementById("email-subject")?.value || currentEmailDraft.subject;
            const text = `To: ${to.join(", ")}\nCC: ${cc.join(", ")}\nSubject: ${subject}\n\n${currentEmailDraft.body_text || ""}`;
            await navigator.clipboard.writeText(text);
            showEmailStatus("Email copied as plain text", "success");
        } catch (e2) {
            showEmailStatus("Failed to copy \u2014 check browser clipboard permissions", "error");
        }
    }
}

async function copyEmailAsText() {
    if (!currentEmailDraft) return;
    try {
        const { to, cc } = getRecipientsFromChips();
        const subject = document.getElementById("email-subject")?.value || currentEmailDraft.subject;
        const text = `To: ${to.join(", ")}\nCC: ${cc.join(", ")}\nSubject: ${subject}\n\n${currentEmailDraft.body_text || ""}`;
        await navigator.clipboard.writeText(text);
        showEmailStatus("Plain text email copied to clipboard", "success");
    } catch (err) {
        showEmailStatus("Failed to copy", "error");
    }
}

function openInMailClient() {
    if (!currentEmailDraft) return;
    const { to, cc } = getRecipientsFromChips();
    const subject = document.getElementById("email-subject")?.value || currentEmailDraft.subject;
    const body = currentEmailDraft.body_text || "";
    const mailto = `mailto:${to.join(",")}?cc=${cc.join(",")}&subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    window.open(mailto, "_blank");
}

function connectOutlook() {
    if (!outlookAuthUrl) {
        showEmailStatus("Outlook is not configured. Set MS_GRAPH_CLIENT_ID in .env", "error");
        return;
    }
    const w = 600, h = 700;
    const left = (screen.width - w) / 2;
    const top = (screen.height - h) / 2;
    window.open(outlookAuthUrl, "outlook_auth", `width=${w},height=${h},left=${left},top=${top}`);
}

async function sendViaOutlook() {
    if (!outlookConnected) {
        showEmailStatus("Please connect Outlook first", "error");
        return;
    }
    const { to, cc } = getRecipientsFromChips();
    if (!to.length) {
        showEmailStatus("Add at least one recipient to the To field", "error");
        return;
    }
    const subject = document.getElementById("email-subject")?.value || currentEmailDraft.subject;
    const methodLabel = outlookMode === "outlook_app" ? "Outlook Desktop" : outlookMode === "power_automate" ? "Power Automate" : "Outlook";
    showEmailStatus(`Sending email via ${methodLabel}...`, "info");
    try {
        await API.sendEmail({ to, cc, subject, body_html: currentEmailDraft.body_html, token: outlookToken });
        showEmailStatus(`Email sent successfully via ${methodLabel}!`, "success");
    } catch (err) {
        showEmailStatus(`Send failed: ${err.message}`, "error");
    }
}

async function saveDraftOutlook() {
    if (!outlookConnected) {
        showEmailStatus("Please connect Outlook first", "error");
        return;
    }
    const { to, cc } = getRecipientsFromChips();
    const subject = document.getElementById("email-subject")?.value || currentEmailDraft.subject;
    showEmailStatus("Creating draft in Outlook...", "info");
    try {
        const result = await API.createDraft({ to, cc, subject, body_html: currentEmailDraft.body_html, token: outlookToken });
        const link = result.web_link ? ` <a href="${result.web_link}" target="_blank" style="color:var(--accent)">Open in Outlook</a>` : '';
        const note = result.note ? ` \u2014 ${result.note}` : '';
        const el = document.getElementById("email-status-msg");
        el.style.display = "block";
        el.className = "email-status-msg success";
        el.innerHTML = `Draft saved!${link}${note}`;
    } catch (err) {
        showEmailStatus(`Draft creation failed: ${err.message}`, "error");
    }
}

// ── Global exports ──────────────────────────────────────────────────────

window.loadAccountDetail = loadAccountDetail;
window.showDashboard = showDashboard;
window.showAccountView = showAccountView;
window.showSectionView = showSectionView;
window.loadTopRisks = loadTopRisks;
window.exportCSV = exportCSV;
window.loadEmailDraft = loadEmailDraft;
window.toggleContactChip = toggleContactChip;
window.addExtraRecipient = addExtraRecipient;
window.copyEmailToClipboard = copyEmailToClipboard;
window.copyEmailAsText = copyEmailAsText;
window.openInMailClient = openInMailClient;
window.connectOutlook = connectOutlook;
window.sendViaOutlook = sendViaOutlook;
window.saveDraftOutlook = saveDraftOutlook;
window.closeMyAccountsModal = closeMyAccountsModal;
window.submitMyAccounts = submitMyAccounts;
window.switchView = switchView;
window.nextPage = nextPage;
window.prevPage = prevPage;
window.sortDirectory = sortDirectory;
window.refreshView = refreshView;
window.submitCxmFlag = submitCxmFlag;
window.resolveCxmFlag = resolveCxmFlag;
window.deleteCxmFlag = deleteCxmFlag;
