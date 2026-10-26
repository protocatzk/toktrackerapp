/**
 * TokTracker – Chat Intelligence, Wortwolke & Stream-Log Viewer
 * Frontend Controller (Vanilla JavaScript)
 * Modern Database Table Grid & Stream Log Inspector
 */
(function() {
    "use strict";

    // State Management
    const state = {
        viewMode: "table", // "table" (default database grid) | "stream" (bubble stream)
        streamerId: "",
        sessionId: "",
        period: "24h",
        minWordLen: 3,
        searchTerm: "",
        userFilter: "", // Direct TikTok username filter
        activeFilterWord: "",
        tierFilter: "", // "" | "whales" | "whale" | "shark" | "dolphin"
        orderDir: "DESC", // "DESC" (newest first) | "ASC" (oldest first)
        page: 1,
        pageSize: 50,
        totalCount: 0,
        totalPages: 1,
        isLiveTail: true,
        activeTab: "phrases", // "phrases" | "emojis"
        messages: [],
        oldestMsgId: null,
        newestMsgId: null,
        selectedMessage: null,
        isFetchingAnalysis: false,
        isFetchingMessages: false,
        pollTimer: null,
    };

    // DOM Elements Cache
    const elements = {
        // Filters & Controls Top
        selectStreamer: document.getElementById("selectStreamer"),
        selectSession: document.getElementById("selectSession"),
        periodPills: document.getElementById("periodPills"),
        selectMinWordLen: document.getElementById("selectMinWordLen"),
        btnRefreshAnalysis: document.getElementById("btnRefreshAnalysis"),
        btnExportMenu: document.getElementById("btnExportMenu"),

        // KPIs
        kpiTotalMsgs: document.getElementById("kpiTotalMsgs"),
        kpiAvgChars: document.getElementById("kpiAvgChars"),
        kpiUniqueChatters: document.getElementById("kpiUniqueChatters"),
        kpiDiversity: document.getElementById("kpiDiversity"),
        kpiWhaleMsgs: document.getElementById("kpiWhaleMsgs"),
        kpiWhaleShare: document.getElementById("kpiWhaleShare"),
        kpiTopChatter: document.getElementById("kpiTopChatter"),
        kpiTopChatterCount: document.getElementById("kpiTopChatterCount"),

        // Word Cloud & Phrases / Emojis
        wordCloudContainer: document.getElementById("wordCloudContainer"),
        wordCloudCountBadge: document.getElementById("wordCloudCountBadge"),
        tabBtnPhrases: document.getElementById("tabBtnPhrases"),
        tabBtnEmojis: document.getElementById("tabBtnEmojis"),
        phrasesListContainer: document.getElementById("phrasesListContainer"),
        emojisGridContainer: document.getElementById("emojisGridContainer"),

        // Viewer Header & Tools
        inputChatSearch: document.getElementById("inputChatSearch"),
        activeFilterPill: document.getElementById("activeFilterPill"),
        activeFilterText: document.getElementById("activeFilterText"),
        btnClearWordFilter: document.getElementById("btnClearWordFilter"),
        activeUserFilterPill: document.getElementById("activeUserFilterPill"),
        activeUserFilterText: document.getElementById("activeUserFilterText"),
        btnClearUserFilter: document.getElementById("btnClearUserFilter"),
        
        btnViewModeTable: document.getElementById("btnViewModeTable"),
        btnViewModeStream: document.getElementById("btnViewModeStream"),
        selectTierFilter: document.getElementById("selectTierFilter"),
        btnToggleSort: document.getElementById("btnToggleSort"),
        sortOrderLabel: document.getElementById("sortOrderLabel"),
        btnToggleLiveTail: document.getElementById("btnToggleLiveTail"),
        liveTailDot: document.getElementById("liveTailDot"),
        liveTailText: document.getElementById("liveTailText"),
        viewerMsgCountMeta: document.getElementById("viewerMsgCountMeta"),

        // Table & Stream Containers
        chatTableContainer: document.getElementById("chatTableContainer"),
        chatTableBody: document.getElementById("chatTableBody"),
        chatStreamArea: document.getElementById("chatStreamArea"),

        // Pagination Bar
        ciPaginationBar: document.getElementById("ciPaginationBar"),
        pagRangeStart: document.getElementById("pagRangeStart"),
        pagRangeEnd: document.getElementById("pagRangeEnd"),
        pagTotalCount: document.getElementById("pagTotalCount"),
        pagCurrentPage: document.getElementById("pagCurrentPage"),
        pagTotalPages: document.getElementById("pagTotalPages"),
        btnPagFirst: document.getElementById("btnPagFirst"),
        btnPagPrev: document.getElementById("btnPagPrev"),
        btnPagNext: document.getElementById("btnPagNext"),
        btnPagLast: document.getElementById("btnPagLast"),
        selectPageSize: document.getElementById("selectPageSize"),
        btnRefreshGrid: document.getElementById("btnRefreshGrid"),

        // Stream Footer
        ciStreamFooter: document.getElementById("ciStreamFooter"),
        btnLoadOlderMsgs: document.getElementById("btnLoadOlderMsgs"),
        streamSessionInfoText: document.getElementById("streamSessionInfoText"),

        // Record Inspector Modal
        recordInspectorModal: document.getElementById("recordInspectorModal"),
        inspModalSub: document.getElementById("inspModalSub"),
        btnCloseInspectorModal: document.getElementById("btnCloseInspectorModal"),
        btnDismissInspector: document.getElementById("btnDismissInspector"),
        inspId: document.getElementById("inspId"),
        inspTimestamp: document.getElementById("inspTimestamp"),
        inspStreamer: document.getElementById("inspStreamer"),
        inspSession: document.getElementById("inspSession"),
        inspUsername: document.getElementById("inspUsername"),
        inspNickname: document.getElementById("inspNickname"),
        inspTier: document.getElementById("inspTier"),
        inspUserId: document.getElementById("inspUserId"),
        inspComment: document.getElementById("inspComment"),
        inspRawJson: document.getElementById("inspRawJson"),
        btnCopyRawJson: document.getElementById("btnCopyRawJson"),
        btnFilterByThisUser: document.getElementById("btnFilterByThisUser"),
        btnOpenTikTokProfile: document.getElementById("btnOpenTikTokProfile"),

        // Export Modal
        exportModal: document.getElementById("exportModal"),
        btnCloseExportModal: document.getElementById("btnCloseExportModal"),
        btnDownloadCsv: document.getElementById("btnDownloadCsv"),
        btnDownloadJson: document.getElementById("btnDownloadJson"),
        btnDownloadTxt: document.getElementById("btnDownloadTxt"),

        // Floating Toast
        ciToast: document.getElementById("ciToast"),
        ciToastText: document.getElementById("ciToastText"),
    };

    // Utility Helpers
    function escapeHtml(str) {
        if (!str) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatNumber(num) {
        if (num === null || num === undefined) return "0";
        return new Intl.NumberFormat("de-DE").format(num);
    }

    function formatTime(isoStr) {
        if (!isoStr) return "";
        const parts = isoStr.split(" ");
        if (parts.length > 1) {
            return parts[1].substring(0, 8);
        }
        return isoStr.substring(11, 19) || isoStr;
    }

    function formatDateTime(isoStr) {
        if (!isoStr) return "";
        const parts = isoStr.split(" ");
        if (parts.length === 2) {
            const d = parts[0].split("-");
            const t = parts[1].substring(0, 8);
            if (d.length === 3) {
                return `${d[2]}.${d[1]}. ${t}`;
            }
        }
        return isoStr;
    }

    function highlightText(text, query) {
        if (!text) return "";
        if (!query) return escapeHtml(text);
        
        // Split query into tokens to highlight multiple words or @usernames
        const cleanTokens = query.trim().split(/\s+/).map(t => t.replace(/^@+/, "").trim()).filter(t => t.length > 0);
        if (cleanTokens.length === 0) return escapeHtml(text);

        let escaped = escapeHtml(text);
        cleanTokens.forEach(t => {
            const safeQuery = t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            const regex = new RegExp(`(${safeQuery})`, "gi");
            escaped = escaped.replace(regex, '<span class="highlight-term">$1</span>');
        });
        return escaped;
    }

    let toastTimer = null;
    function showToast(text = "Kopiert!") {
        if (!elements.ciToast) return;
        if (elements.ciToastText) elements.ciToastText.textContent = text;
        elements.ciToast.style.display = "flex";
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
            elements.ciToast.style.display = "none";
        }, 2200);
    }

    function copyToClipboard(text, successMsg = "In Zwischenablage kopiert!") {
        if (!text) return;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(() => {
                showToast(successMsg);
            }).catch(() => {
                legacyCopy(text, successMsg);
            });
        } else {
            legacyCopy(text, successMsg);
        }
    }

    function legacyCopy(text, successMsg) {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand("copy");
            showToast(successMsg);
        } catch (e) {}
        document.body.removeChild(ta);
    }

    // 1. Initialize
    async function init() {
        bindEvents();
        await loadStreamers();
        await loadSessions();
        await loadAnalysis();
        await loadMessages();
        startPolling();
    }

    // 2. Bind UI Events
    function bindEvents() {
        // Streamer Select
        if (elements.selectStreamer) {
            elements.selectStreamer.addEventListener("change", async (e) => {
                state.streamerId = e.target.value;
                state.sessionId = "";
                state.page = 1;
                await loadSessions();
                triggerFullRefresh();
            });
        }

        // Session Select
        if (elements.selectSession) {
            elements.selectSession.addEventListener("change", (e) => {
                state.sessionId = e.target.value;
                state.page = 1;
                updateSessionInfoLabel();
                triggerFullRefresh();
            });
        }

        // Period Pills
        if (elements.periodPills) {
            elements.periodPills.querySelectorAll(".ci-period-btn").forEach(btn => {
                btn.addEventListener("click", () => {
                    elements.periodPills.querySelectorAll(".ci-period-btn").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    state.period = btn.getAttribute("data-period") || "24h";
                    state.page = 1;
                    triggerFullRefresh();
                });
            });
        }

        // Min Word Length
        if (elements.selectMinWordLen) {
            elements.selectMinWordLen.addEventListener("change", (e) => {
                state.minWordLen = parseInt(e.target.value, 10) || 3;
                loadAnalysis();
            });
        }

        // Refresh Analysis Button
        if (elements.btnRefreshAnalysis) {
            elements.btnRefreshAnalysis.addEventListener("click", () => {
                triggerFullRefresh();
            });
        }

        // Phrases / Emojis Tabs
        if (elements.tabBtnPhrases && elements.tabBtnEmojis) {
            elements.tabBtnPhrases.addEventListener("click", () => {
                state.activeTab = "phrases";
                elements.tabBtnPhrases.classList.add("active");
                elements.tabBtnEmojis.classList.remove("active");
                elements.phrasesListContainer.style.display = "flex";
                elements.emojisGridContainer.style.display = "none";
            });

            elements.tabBtnEmojis.addEventListener("click", () => {
                state.activeTab = "emojis";
                elements.tabBtnEmojis.classList.add("active");
                elements.tabBtnPhrases.classList.remove("active");
                elements.phrasesListContainer.style.display = "none";
                elements.emojisGridContainer.style.display = "grid";
            });
        }

        // Search Input (Debounced)
        let searchTimer = null;
        if (elements.inputChatSearch) {
            elements.inputChatSearch.addEventListener("input", (e) => {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(() => {
                    state.searchTerm = e.target.value.trim();
                    state.page = 1;
                    loadMessages();
                }, 300);
            });
        }

        // Clear Word Filter
        if (elements.btnClearWordFilter) {
            elements.btnClearWordFilter.addEventListener("click", () => {
                clearWordFilter();
            });
        }

        // Clear User Filter
        if (elements.btnClearUserFilter) {
            elements.btnClearUserFilter.addEventListener("click", () => {
                clearUserFilter();
            });
        }

        // View Mode Switcher
        if (elements.btnViewModeTable) {
            elements.btnViewModeTable.addEventListener("click", () => {
                setViewMode("table");
            });
        }
        if (elements.btnViewModeStream) {
            elements.btnViewModeStream.addEventListener("click", () => {
                setViewMode("stream");
            });
        }

        // Tier Filter Selector
        if (elements.selectTierFilter) {
            elements.selectTierFilter.addEventListener("change", (e) => {
                state.tierFilter = e.target.value;
                state.page = 1;
                loadMessages();
            });
        }

        // Sort Order Toggle
        if (elements.btnToggleSort) {
            elements.btnToggleSort.addEventListener("click", () => {
                toggleSortOrder();
            });
        }

        // Live Tail Toggle
        if (elements.btnToggleLiveTail) {
            elements.btnToggleLiveTail.addEventListener("click", () => {
                state.isLiveTail = !state.isLiveTail;
                if (elements.liveTailDot) {
                    elements.liveTailDot.style.background = state.isLiveTail ? "#10b981" : "#64748b";
                }
                if (elements.liveTailText) {
                    elements.liveTailText.textContent = state.isLiveTail ? "Live: An" : "Live: Pausiert";
                }
                if (state.isLiveTail && state.viewMode === "stream") {
                    scrollChatToBottom();
                }
            });
        }

        // Page Size Selector
        if (elements.selectPageSize) {
            elements.selectPageSize.addEventListener("change", (e) => {
                state.pageSize = parseInt(e.target.value, 10) || 50;
                state.page = 1;
                loadMessages();
            });
        }

        // Pagination Buttons
        if (elements.btnPagFirst) {
            elements.btnPagFirst.addEventListener("click", () => {
                if (state.page > 1) {
                    state.page = 1;
                    loadMessages();
                }
            });
        }
        if (elements.btnPagPrev) {
            elements.btnPagPrev.addEventListener("click", () => {
                if (state.page > 1) {
                    state.page--;
                    loadMessages();
                }
            });
        }
        if (elements.btnPagNext) {
            elements.btnPagNext.addEventListener("click", () => {
                if (state.page < state.totalPages) {
                    state.page++;
                    loadMessages();
                }
            });
        }
        if (elements.btnPagLast) {
            elements.btnPagLast.addEventListener("click", () => {
                if (state.page < state.totalPages) {
                    state.page = state.totalPages;
                    loadMessages();
                }
            });
        }

        // Refresh Grid Button
        if (elements.btnRefreshGrid) {
            elements.btnRefreshGrid.addEventListener("click", () => {
                loadMessages();
            });
        }

        // Load Older Messages (Stream View only)
        if (elements.btnLoadOlderMsgs) {
            elements.btnLoadOlderMsgs.addEventListener("click", () => {
                loadOlderStreamMessages();
            });
        }

        // Export Modal & Downloads
        if (elements.btnExportMenu) {
            elements.btnExportMenu.addEventListener("click", () => {
                if (elements.exportModal) elements.exportModal.style.display = "flex";
            });
        }
        if (elements.btnCloseExportModal) {
            elements.btnCloseExportModal.addEventListener("click", () => {
                if (elements.exportModal) elements.exportModal.style.display = "none";
            });
        }
        if (elements.exportModal) {
            elements.exportModal.addEventListener("click", (e) => {
                if (e.target === elements.exportModal) {
                    elements.exportModal.style.display = "none";
                }
            });
        }
        if (elements.btnDownloadCsv) {
            elements.btnDownloadCsv.addEventListener("click", () => {
                triggerExportDownload("csv");
                if (elements.exportModal) elements.exportModal.style.display = "none";
            });
        }
        if (elements.btnDownloadJson) {
            elements.btnDownloadJson.addEventListener("click", () => {
                triggerExportDownload("json");
                if (elements.exportModal) elements.exportModal.style.display = "none";
            });
        }
        if (elements.btnDownloadTxt) {
            elements.btnDownloadTxt.addEventListener("click", () => {
                triggerExportDownload("txt");
                if (elements.exportModal) elements.exportModal.style.display = "none";
            });
        }

        // Inspector Modal Close & Actions
        if (elements.btnCloseInspectorModal) {
            elements.btnCloseInspectorModal.addEventListener("click", closeRecordInspector);
        }
        if (elements.btnDismissInspector) {
            elements.btnDismissInspector.addEventListener("click", closeRecordInspector);
        }
        if (elements.recordInspectorModal) {
            elements.recordInspectorModal.addEventListener("click", (e) => {
                if (e.target === elements.recordInspectorModal) {
                    closeRecordInspector();
                }
            });
        }
        if (elements.btnCopyRawJson) {
            elements.btnCopyRawJson.addEventListener("click", () => {
                if (elements.inspRawJson) {
                    copyToClipboard(elements.inspRawJson.textContent, "Rohdaten (JSON) kopiert!");
                }
            });
        }
        if (elements.btnFilterByThisUser) {
            elements.btnFilterByThisUser.addEventListener("click", () => {
                if (state.selectedMessage && state.selectedMessage.username) {
                    setUserFilter(state.selectedMessage.username);
                    closeRecordInspector();
                }
            });
        }

        // Top Chatter KPI click
        if (elements.kpiTopChatter) {
            elements.kpiTopChatter.style.cursor = "pointer";
            elements.kpiTopChatter.addEventListener("click", () => {
                const uname = elements.kpiTopChatter.getAttribute("data-username");
                if (uname) setUserFilter(uname);
            });
        }
    }

    function triggerFullRefresh() {
        loadAnalysis();
        loadMessages();
    }

    // Word Filter Handlers
    function setWordFilter(word) {
        state.activeFilterWord = word;
        state.page = 1;
        if (elements.activeFilterPill && elements.activeFilterText) {
            elements.activeFilterText.textContent = word;
            elements.activeFilterPill.style.display = "inline-flex";
        }
        // Highlight active word in cloud
        document.querySelectorAll(".cloud-word").forEach(el => {
            if (el.getAttribute("data-word") === word) {
                el.classList.add("active-filter");
            } else {
                el.classList.remove("active-filter");
            }
        });
        loadMessages();

        const viewerSection = document.getElementById("chatViewerSection");
        if (viewerSection) {
            viewerSection.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function clearWordFilter() {
        state.activeFilterWord = "";
        state.page = 1;
        if (elements.activeFilterPill) {
            elements.activeFilterPill.style.display = "none";
        }
        document.querySelectorAll(".cloud-word").forEach(el => {
            el.classList.remove("active-filter");
        });
        loadMessages();
    }

    // User Filter Handlers
    function setUserFilter(username) {
        if (!username) return;
        const clean = username.trim().replace(/^@+/, "");
        state.userFilter = clean;
        state.page = 1;

        if (elements.activeUserFilterPill && elements.activeUserFilterText) {
            elements.activeUserFilterText.textContent = clean;
            elements.activeUserFilterPill.style.display = "inline-flex";
        }

        loadMessages();

        const viewerSection = document.getElementById("chatViewerSection");
        if (viewerSection) {
            viewerSection.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function clearUserFilter() {
        state.userFilter = "";
        state.page = 1;
        if (elements.activeUserFilterPill) {
            elements.activeUserFilterPill.style.display = "none";
        }
        loadMessages();
    }

    // View Mode Switcher
    function setViewMode(mode) {
        state.viewMode = mode;
        if (mode === "table") {
            if (elements.btnViewModeTable) elements.btnViewModeTable.classList.add("active");
            if (elements.btnViewModeStream) elements.btnViewModeStream.classList.remove("active");
            if (elements.chatTableContainer) elements.chatTableContainer.style.display = "block";
            if (elements.chatStreamArea) elements.chatStreamArea.style.display = "none";
            if (elements.ciPaginationBar) elements.ciPaginationBar.style.display = "flex";
            if (elements.ciStreamFooter) elements.ciStreamFooter.style.display = "none";
        } else {
            if (elements.btnViewModeTable) elements.btnViewModeTable.classList.remove("active");
            if (elements.btnViewModeStream) elements.btnViewModeStream.classList.add("active");
            if (elements.chatTableContainer) elements.chatTableContainer.style.display = "none";
            if (elements.chatStreamArea) elements.chatStreamArea.style.display = "flex";
            if (elements.ciPaginationBar) elements.ciPaginationBar.style.display = "none";
            if (elements.ciStreamFooter) elements.ciStreamFooter.style.display = "flex";
            if (state.isLiveTail) scrollChatToBottom();
        }
    }

    // Sort Order Toggle
    function toggleSortOrder() {
        state.orderDir = state.orderDir === "DESC" ? "ASC" : "DESC";
        if (elements.sortOrderLabel) {
            elements.sortOrderLabel.textContent = state.orderDir === "DESC" ? "⏱ Neueste (DESC)" : "⏱ Älteste (ASC)";
        }
        state.page = 1;
        loadMessages();
    }

    function updateSessionInfoLabel() {
        if (!elements.streamSessionInfoText) return;
        if (!state.sessionId) {
            elements.streamSessionInfoText.textContent = "Alle Streams";
            return;
        }
        const opt = elements.selectSession ? elements.selectSession.selectedOptions[0] : null;
        if (opt) {
            elements.streamSessionInfoText.textContent = `Session #${state.sessionId} (${opt.textContent})`;
        }
    }

    // 3. API Loaders
    async function loadStreamers() {
        try {
            const res = await fetch("/api/overview?period=all");
            if (!res.ok) return;
            const data = await res.json();
            const streamers = data.streamers || [];

            if (elements.selectStreamer) {
                let html = '<option value="">Alle Streamer</option>';
                streamers.forEach(s => {
                    const isLive = s.is_live ? "🔴 " : "";
                    const name = s.display_name ? `${s.display_name} (@${s.username})` : `@${s.username}`;
                    html += `<option value="${s.id}">${isLive}${escapeHtml(name)}</option>`;
                });
                elements.selectStreamer.innerHTML = html;
            }
        } catch (e) {
            console.error("Fehler loadStreamers:", e);
        }
    }

    async function loadSessions() {
        try {
            let url = "/api/chat-intelligence/sessions?limit=80";
            if (state.streamerId) {
                url += `&streamer_id=${encodeURIComponent(state.streamerId)}`;
            }
            const res = await fetch(url);
            if (!res.ok) {
                if (res.status === 401 || res.status === 403) {
                    window.location.reload();
                    return;
                }
                return;
            }
            const data = await res.json();
            const sessions = data.sessions || [];

            if (elements.selectSession) {
                let html = '<option value="">Alle Streams des Zeitraums</option>';
                sessions.forEach(ss => {
                    const isLive = ss.status === "active";
                    const livePrefix = isLive ? "🔴 LIVE: " : "";
                    const streamerName = `@${ss.streamer_username}`;
                    const timeStr = ss.started_at ? ss.started_at.substring(0, 16).replace("T", " ") : "";
                    const countStr = formatNumber(ss.chat_count || 0);

                    html += `<option value="${ss.id}" ${state.sessionId == ss.id ? "selected" : ""}>
                        ${livePrefix}${escapeHtml(streamerName)} · ${timeStr} (${countStr} Nachrichten)
                    </option>`;
                });
                elements.selectSession.innerHTML = html;
            }
        } catch (e) {
            console.error("Fehler loadSessions:", e);
        }
    }

    async function loadAnalysis() {
        if (state.isFetchingAnalysis) return;
        state.isFetchingAnalysis = true;

        try {
            let url = `/api/chat-intelligence/analysis?period=${encodeURIComponent(state.period)}&min_word_len=${state.minWordLen}`;
            if (state.sessionId) {
                url += `&session_id=${encodeURIComponent(state.sessionId)}`;
            } else if (state.streamerId) {
                url += `&streamer_id=${encodeURIComponent(state.streamerId)}`;
            }

            const res = await fetch(url);
            if (!res.ok) {
                if (res.status === 401 || res.status === 403) {
                    window.location.reload();
                    return;
                }
                return;
            }
            const json = await res.json();
            const d = json.data || {};

            renderKPIs(d);
            renderWordCloud(d.words || []);
            renderPhrases(d.phrases || []);
            renderEmojis(d.emojis || []);
        } catch (e) {
            console.error("Fehler loadAnalysis:", e);
        } finally {
            state.isFetchingAnalysis = false;
        }
    }

    function renderKPIs(d) {
        const total = d.total_messages || 0;
        const unique = d.unique_chatters || 0;
        const whales = d.whale_messages || 0;
        const diversity = d.chatter_diversity_pct || 0;
        const avgChars = d.avg_chars_per_msg || 0;

        if (elements.kpiTotalMsgs) elements.kpiTotalMsgs.textContent = formatNumber(total);
        if (elements.kpiAvgChars) elements.kpiAvgChars.textContent = `Ø ${avgChars} Zeichen / Nachricht`;

        if (elements.kpiUniqueChatters) elements.kpiUniqueChatters.textContent = formatNumber(unique);
        if (elements.kpiDiversity) elements.kpiDiversity.textContent = `Chatter-Quote: ${diversity} %`;

        if (elements.kpiWhaleMsgs) elements.kpiWhaleMsgs.textContent = formatNumber(whales);
        const whalePct = total > 0 ? ((whales / total) * 100).toFixed(1) : 0;
        if (elements.kpiWhaleShare) elements.kpiWhaleShare.textContent = `${whalePct} % aller Nachrichten`;

        const top = (d.top_chatters && d.top_chatters.length > 0) ? d.top_chatters[0] : null;
        if (top) {
            if (elements.kpiTopChatter) {
                elements.kpiTopChatter.textContent = top.nickname || `@${top.username}`;
                elements.kpiTopChatter.title = `@${top.username} (Klicken zum Filtern)`;
                elements.kpiTopChatter.setAttribute("data-username", top.username);
            }
            if (elements.kpiTopChatterCount) {
                elements.kpiTopChatterCount.textContent = `${formatNumber(top.count)} Nachrichten (${top.pct}%)`;
            }
        } else {
            if (elements.kpiTopChatter) {
                elements.kpiTopChatter.textContent = "-";
                elements.kpiTopChatter.removeAttribute("data-username");
            }
            if (elements.kpiTopChatterCount) elements.kpiTopChatterCount.textContent = "Keine Daten";
        }
    }

    function renderWordCloud(words) {
        if (!elements.wordCloudContainer) return;
        if (!words || words.length === 0) {
            elements.wordCloudContainer.innerHTML = `
                <div style="color:var(--text-muted); font-size:13px; text-align:center; padding:30px;">
                    Keine Chatnachrichten im gewählten Zeitraum gefunden.
                </div>
            `;
            if (elements.wordCloudCountBadge) elements.wordCloudCountBadge.textContent = "0 Wörter";
            return;
        }

        if (elements.wordCloudCountBadge) {
            elements.wordCloudCountBadge.textContent = `${words.length} Wörter`;
        }

        const sorted = [...words];
        let html = "";
        sorted.forEach(w => {
            let wClass = "w-base";
            if (w.weight >= 70) wClass = "w-top";
            else if (w.weight >= 45) wClass = "w-high";
            else if (w.weight >= 25) wClass = "w-med";
            else if (w.weight >= 12) wClass = "w-low";

            const isActive = state.activeFilterWord === w.text ? "active-filter" : "";

            html += `
                <span class="cloud-word ${wClass} ${isActive}" data-word="${escapeHtml(w.text)}" title="${formatNumber(w.count)}x geschrieben (Klicken zum Filtern)">
                    <span>${escapeHtml(w.text)}</span>
                    <span class="cloud-word-count">${formatNumber(w.count)}</span>
                </span>
            `;
        });

        elements.wordCloudContainer.innerHTML = html;

        elements.wordCloudContainer.querySelectorAll(".cloud-word").forEach(el => {
            el.addEventListener("click", () => {
                const word = el.getAttribute("data-word");
                if (state.activeFilterWord === word) {
                    clearWordFilter();
                } else {
                    setWordFilter(word);
                }
            });
        });
    }

    function renderPhrases(phrases) {
        if (!elements.phrasesListContainer) return;
        if (!phrases || phrases.length === 0) {
            elements.phrasesListContainer.innerHTML = `
                <div style="color:var(--text-muted); font-size:12.5px; padding:16px; text-align:center;">
                    Keine wiederkehrenden Phrasen erkannt.
                </div>
            `;
            return;
        }

        const maxCount = phrases[0] ? phrases[0].count : 1;
        let html = "";
        phrases.forEach((p, idx) => {
            const barWidth = Math.max(5, Math.round((p.count / maxCount) * 100));
            html += `
                <div class="phrase-item" data-phrase="${escapeHtml(p.phrase)}" title="Klicken zum Filtern nach '${escapeHtml(p.phrase)}'">
                    <span class="phrase-rank">#${idx + 1}</span>
                    <span class="phrase-text">${escapeHtml(p.phrase)}</span>
                    <div class="phrase-meta">
                        <span class="phrase-count-badge">${formatNumber(p.count)}x</span>
                        <span class="mono muted" style="font-size:10.5px;">${p.pct}%</span>
                    </div>
                    <div class="phrase-bar" style="width: ${barWidth}%;"></div>
                </div>
            `;
        });

        elements.phrasesListContainer.innerHTML = html;

        elements.phrasesListContainer.querySelectorAll(".phrase-item").forEach(el => {
            el.addEventListener("click", () => {
                const phrase = el.getAttribute("data-phrase");
                setWordFilter(phrase);
            });
        });
    }

    function renderEmojis(emojis) {
        if (!elements.emojisGridContainer) return;
        if (!emojis || emojis.length === 0) {
            elements.emojisGridContainer.innerHTML = `
                <div style="color:var(--text-muted); font-size:12.5px; padding:16px; grid-column:1/-1; text-align:center;">
                    Keine Emojis gefunden.
                </div>
            `;
            return;
        }

        let html = "";
        emojis.forEach(emo => {
            html += `
                <div class="emoji-tile" data-emoji="${escapeHtml(emo.emoji)}" title="${formatNumber(emo.count)}x verwendet (Klicken zum Filtern)">
                    <div class="emoji-char">${escapeHtml(emo.emoji)}</div>
                    <div class="emoji-count">${formatNumber(emo.count)}</div>
                </div>
            `;
        });

        elements.emojisGridContainer.innerHTML = html;

        elements.emojisGridContainer.querySelectorAll(".emoji-tile").forEach(el => {
            el.addEventListener("click", () => {
                const emo = el.getAttribute("data-emoji");
                setWordFilter(emo);
            });
        });
    }

    // 4. Log & Database Messages Loader
    async function loadMessages() {
        if (state.isFetchingMessages) return;
        state.isFetchingMessages = true;

        try {
            const effectiveQuery = state.activeFilterWord || state.searchTerm;
            const offset = (state.page - 1) * state.pageSize;

            const params = new URLSearchParams({
                limit: state.pageSize.toString(),
                offset: offset.toString(),
                order_dir: state.orderDir,
                include_total: "true",
            });

            if (state.sessionId) {
                params.append("session_id", state.sessionId);
            } else if (state.streamerId) {
                params.append("streamer_id", state.streamerId);
            }

            if (state.userFilter) {
                params.append("username", state.userFilter);
            }

            if (effectiveQuery) {
                params.append("search", effectiveQuery);
            }

            if (state.tierFilter === "whales") {
                params.append("only_whales", "true");
            } else if (state.tierFilter) {
                params.append("min_tier", state.tierFilter);
            }

            const res = await fetch(`/api/chat-logs?${params.toString()}`);
            if (!res.ok) return;
            const data = await res.json();
            const msgs = data.messages || [];

            state.messages = msgs;
            state.totalCount = data.total !== undefined ? data.total : msgs.length;
            state.totalPages = Math.max(1, Math.ceil(state.totalCount / state.pageSize));

            if (state.messages.length > 0) {
                const ids = state.messages.map(m => m.id);
                state.oldestMsgId = Math.min(...ids);
                state.newestMsgId = Math.max(...ids);
            } else {
                state.oldestMsgId = null;
                state.newestMsgId = null;
            }

            // Render both Table and Stream representation
            renderTableRows();
            renderChatStream();
            updatePaginationUI();

            if (state.viewMode === "stream" && state.isLiveTail) {
                scrollChatToBottom();
            }
        } catch (e) {
            console.error("Fehler loadMessages:", e);
        } finally {
            state.isFetchingMessages = false;
        }
    }

    function renderTableRows() {
        if (!elements.chatTableBody) return;
        if (!state.messages || state.messages.length === 0) {
            elements.chatTableBody.innerHTML = `
                <tr>
                    <td colspan="7" style="text-align: center; padding: 48px; color: var(--text-muted); font-size: 13px;">
                        Keine passenden Datensätze gefunden.
                    </td>
                </tr>
            `;
            return;
        }

        const effectiveQuery = state.activeFilterWord || state.searchTerm;
        let html = "";

        state.messages.forEach(m => {
            let rowClass = "";
            let tierBadgeHtml = '<span class="tier-badge tier-badge-default">Chatter</span>';

            if (m.whale_tier === "whale") {
                rowClass = "row-whale";
                tierBadgeHtml = '<span class="tier-badge tier-badge-whale">🐋 Whale</span>';
            } else if (m.whale_tier === "shark") {
                rowClass = "row-shark";
                tierBadgeHtml = '<span class="tier-badge tier-badge-shark">🦈 Shark</span>';
            } else if (m.whale_tier === "dolphin" || m.is_whale) {
                rowClass = "row-dolphin";
                tierBadgeHtml = '<span class="tier-badge tier-badge-vip">🐬 VIP</span>';
            }

            const avatar = m.avatar_url || "/pictures/bot_avatar.jpg";
            const dateFormatted = formatDateTime(m.created_at);
            const streamerTag = `<span class="chat-streamer-tag">@${escapeHtml(m.streamer_username)}</span>`;
            const highlightedComment = highlightText(m.comment, effectiveQuery);

            html += `
                <tr class="${rowClass}" data-id="${m.id}">
                    <td class="col-id mono" style="color:var(--text-muted);">#${m.id}</td>
                    <td class="col-time mono" title="${escapeHtml(m.created_at)}">${dateFormatted}</td>
                    <td class="col-streamer">${streamerTag}</td>
                    <td class="col-user">
                        <div class="user-cell-box">
                            <img src="${escapeHtml(avatar)}" class="user-avatar-sm" onerror="this.src='/pictures/bot_avatar.jpg'" alt="" />
                            <div class="user-info-text">
                                <span class="user-nick" title="${escapeHtml(m.nickname)}">${escapeHtml(m.nickname || m.username)}</span>
                                <button type="button" class="user-handle-btn" data-username="${escapeHtml(m.username)}" title="Klicken zum Filtern nach @${escapeHtml(m.username)}">
                                    @${escapeHtml(m.username)}
                                </button>
                            </div>
                        </div>
                    </td>
                    <td class="col-tier">${tierBadgeHtml}</td>
                    <td class="col-comment">${highlightedComment}</td>
                    <td class="col-actions">
                        <div class="row-actions-group">
                            <button type="button" class="btn-row-action btn-act-filter-user" data-username="${escapeHtml(m.username)}" title="Nur nach @${escapeHtml(m.username)} filtern">
                                👤
                            </button>
                            <button type="button" class="btn-row-action btn-act-copy-text" data-comment="${escapeHtml(m.comment)}" title="Nachricht kopieren">
                                📋
                            </button>
                            <button type="button" class="btn-row-action btn-act-inspect" data-id="${m.id}" title="Datensatz im Inspektor öffnen">
                                🔍
                            </button>
                            <a href="https://www.tiktok.com/@${encodeURIComponent(m.username)}" target="_blank" rel="noopener noreferrer" class="btn-row-action" title="TikTok-Profil öffnen" onclick="event.stopPropagation();">
                                ↗
                            </a>
                        </div>
                    </td>
                </tr>
            `;
        });

        elements.chatTableBody.innerHTML = html;

        // Row Click: Open Inspector (unless clicked on a button or link)
        elements.chatTableBody.querySelectorAll("tr[data-id]").forEach(tr => {
            tr.addEventListener("click", (e) => {
                if (e.target.closest("button") || e.target.closest("a")) return;
                const id = parseInt(tr.getAttribute("data-id"), 10);
                const msg = state.messages.find(m => m.id === id);
                if (msg) openRecordInspector(msg);
            });
        });

        // Username click & action filter buttons
        elements.chatTableBody.querySelectorAll(".user-handle-btn, .btn-act-filter-user").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const uname = btn.getAttribute("data-username");
                if (uname) setUserFilter(uname);
            });
        });

        // Copy comment button
        elements.chatTableBody.querySelectorAll(".btn-act-copy-text").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const comment = btn.getAttribute("data-comment");
                if (comment) copyToClipboard(comment, "Chat-Nachricht kopiert!");
            });
        });

        // Inspect button
        elements.chatTableBody.querySelectorAll(".btn-act-inspect").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const id = parseInt(btn.getAttribute("data-id"), 10);
                const msg = state.messages.find(m => m.id === id);
                if (msg) openRecordInspector(msg);
            });
        });
    }

    function renderChatStream() {
        if (!elements.chatStreamArea) return;
        if (!state.messages || state.messages.length === 0) {
            elements.chatStreamArea.innerHTML = `
                <div style="color:var(--text-muted); font-size:13px; text-align:center; margin-top:50px;">
                    Keine passenden Chat-Nachrichten gefunden.
                </div>
            `;
            return;
        }

        const effectiveQuery = state.activeFilterWord || state.searchTerm;
        // In Stream View, chronological order (oldest top, newest bottom) is natural
        const streamMsgs = state.orderDir === "DESC" ? [...state.messages].reverse() : [...state.messages];
        let html = "";

        streamMsgs.forEach(m => {
            let whaleClass = "";
            let whalePill = "";
            if (m.whale_tier === "whale") {
                whaleClass = "whale-whale";
                whalePill = '<span class="ci-badge" style="background:#f59e0b; color:#000;">WHALE</span>';
            } else if (m.whale_tier === "shark") {
                whaleClass = "whale-shark";
                whalePill = '<span class="ci-badge" style="background:#38bdf8; color:#000;">SHARK</span>';
            } else if (m.whale_tier === "dolphin" || m.is_whale) {
                whaleClass = "whale-dolphin";
                whalePill = '<span class="ci-badge" style="background:#10b981; color:#000;">VIP</span>';
            }

            const avatar = m.avatar_url || "/pictures/bot_avatar.jpg";
            const timeStr = formatTime(m.created_at);
            const userDisplay = m.nickname || `@${m.username}`;
            const streamerTag = !state.streamerId ? `<span class="chat-streamer-tag">@${escapeHtml(m.streamer_username)}</span>` : "";
            const commentHighlighted = highlightText(m.comment, effectiveQuery);

            html += `
                <div class="chat-row ${whaleClass}" data-id="${m.id}">
                    <img src="${escapeHtml(avatar)}" class="chat-avatar" onerror="this.src='/pictures/bot_avatar.jpg'" alt="" />
                    <div class="chat-body">
                        <div class="chat-meta-line">
                            ${whalePill}
                            <span class="chat-username">${escapeHtml(userDisplay)}</span>
                            <button type="button" class="user-handle-btn" data-username="${escapeHtml(m.username)}" title="Nach @${escapeHtml(m.username)} filtern">
                                @${escapeHtml(m.username)}
                            </button>
                            ${streamerTag}
                            <span class="chat-time">${escapeHtml(timeStr)}</span>
                            <button type="button" class="btn-row-action btn-act-inspect sm" data-id="${m.id}" title="Inspektor öffnen" style="width:20px; height:20px; font-size:10px; margin-left:4px;">🔍</button>
                        </div>
                        <div class="chat-text-line">${commentHighlighted}</div>
                    </div>
                </div>
            `;
        });

        elements.chatStreamArea.innerHTML = html;

        // Wire user filter & inspect in stream view
        elements.chatStreamArea.querySelectorAll(".user-handle-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const uname = btn.getAttribute("data-username");
                if (uname) setUserFilter(uname);
            });
        });
        elements.chatStreamArea.querySelectorAll(".btn-act-inspect").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const id = parseInt(btn.getAttribute("data-id"), 10);
                const msg = state.messages.find(m => m.id === id);
                if (msg) openRecordInspector(msg);
            });
        });
    }

    function updatePaginationUI() {
        const start = state.totalCount === 0 ? 0 : (state.page - 1) * state.pageSize + 1;
        const end = Math.min(state.page * state.pageSize, state.totalCount);

        if (elements.pagRangeStart) elements.pagRangeStart.textContent = formatNumber(start);
        if (elements.pagRangeEnd) elements.pagRangeEnd.textContent = formatNumber(end);
        if (elements.pagTotalCount) elements.pagTotalCount.textContent = formatNumber(state.totalCount);
        if (elements.pagCurrentPage) elements.pagCurrentPage.textContent = formatNumber(state.page);
        if (elements.pagTotalPages) elements.pagTotalPages.textContent = formatNumber(state.totalPages);

        if (elements.viewerMsgCountMeta) {
            elements.viewerMsgCountMeta.textContent = `${formatNumber(state.totalCount)} Einträge`;
        }

        if (elements.btnPagFirst) elements.btnPagFirst.disabled = state.page <= 1;
        if (elements.btnPagPrev) elements.btnPagPrev.disabled = state.page <= 1;
        if (elements.btnPagNext) elements.btnPagNext.disabled = state.page >= state.totalPages;
        if (elements.btnPagLast) elements.btnPagLast.disabled = state.page >= state.totalPages;
    }

    async function loadOlderStreamMessages() {
        if (!state.oldestMsgId || state.isFetchingMessages) return;
        state.isFetchingMessages = true;

        const scrollContainer = elements.chatStreamArea;
        const prevScrollHeight = scrollContainer ? scrollContainer.scrollHeight : 0;

        try {
            const effectiveQuery = state.activeFilterWord || state.searchTerm;
            let url = `/api/chat-logs?limit=50&before_id=${state.oldestMsgId}`;
            if (state.sessionId) url += `&session_id=${encodeURIComponent(state.sessionId)}`;
            else if (state.streamerId) url += `&streamer_id=${encodeURIComponent(state.streamerId)}`;
            if (state.userFilter) url += `&username=${encodeURIComponent(state.userFilter)}`;
            if (effectiveQuery) url += `&search=${encodeURIComponent(effectiveQuery)}`;
            if (state.tierFilter === "whales") url += `&only_whales=true`;
            else if (state.tierFilter) url += `&min_tier=${encodeURIComponent(state.tierFilter)}`;

            const res = await fetch(url);
            if (!res.ok) return;
            const data = await res.json();
            const older = data.messages || [];

            if (older.length > 0) {
                state.messages = [...older, ...state.messages];
                state.oldestMsgId = Math.min(...state.messages.map(m => m.id));
                renderChatStream();

                if (scrollContainer) {
                    const newScrollHeight = scrollContainer.scrollHeight;
                    scrollContainer.scrollTop = newScrollHeight - prevScrollHeight;
                }
            } else {
                if (elements.btnLoadOlderMsgs) {
                    elements.btnLoadOlderMsgs.textContent = "Keine älteren Nachrichten mehr";
                    elements.btnLoadOlderMsgs.disabled = true;
                }
            }
        } catch (e) {
            console.error("Fehler loadOlderStreamMessages:", e);
        } finally {
            state.isFetchingMessages = false;
        }
    }

    async function pollNewMessages() {
        if (!state.isLiveTail || state.isFetchingMessages) return;

        try {
            const effectiveQuery = state.activeFilterWord || state.searchTerm;
            let url = `/api/chat-logs?limit=30`;
            if (state.newestMsgId) {
                url += `&after_id=${state.newestMsgId}`;
            }
            if (state.sessionId) url += `&session_id=${encodeURIComponent(state.sessionId)}`;
            else if (state.streamerId) url += `&streamer_id=${encodeURIComponent(state.streamerId)}`;
            if (state.userFilter) url += `&username=${encodeURIComponent(state.userFilter)}`;
            if (effectiveQuery) url += `&search=${encodeURIComponent(effectiveQuery)}`;
            if (state.tierFilter === "whales") url += `&only_whales=true`;
            else if (state.tierFilter) url += `&min_tier=${encodeURIComponent(state.tierFilter)}`;

            const res = await fetch(url);
            if (!res.ok) return;
            const data = await res.json();
            const newOnes = data.messages || [];

            if (newOnes.length > 0) {
                const newIds = newOnes.map(m => m.id);
                state.newestMsgId = Math.max(...newIds);
                state.totalCount += newOnes.length;
                state.totalPages = Math.max(1, Math.ceil(state.totalCount / state.pageSize));

                // In table view: If on page 1 and order is DESC, prepend new records
                if (state.viewMode === "table" && state.page === 1 && state.orderDir === "DESC") {
                    state.messages = [...newOnes.reverse(), ...state.messages].slice(0, state.pageSize);
                    renderTableRows();
                    updatePaginationUI();
                } else if (state.viewMode === "stream") {
                    state.messages = [...state.messages, ...newOnes];
                    renderChatStream();
                    if (state.isLiveTail) {
                        scrollChatToBottom();
                    }
                }
            }
        } catch (e) {
            // Ignore background polling errors
        }
    }

    function scrollChatToBottom() {
        if (elements.chatStreamArea) {
            elements.chatStreamArea.scrollTop = elements.chatStreamArea.scrollHeight;
        }
    }

    function startPolling() {
        clearInterval(state.pollTimer);
        state.pollTimer = setInterval(() => {
            pollNewMessages();
        }, 3000);
    }

    // 5. Record Inspector Modal Controller
    function openRecordInspector(msg) {
        if (!msg || !elements.recordInspectorModal) return;
        state.selectedMessage = msg;

        if (elements.inspModalSub) elements.inspModalSub.textContent = `#${msg.id}`;
        if (elements.inspId) elements.inspId.textContent = `#${msg.id}`;
        if (elements.inspTimestamp) elements.inspTimestamp.textContent = msg.created_at || "-";
        if (elements.inspStreamer) {
            const streamerName = msg.streamer_display_name ? `${msg.streamer_display_name} (@${msg.streamer_username})` : `@${msg.streamer_username}`;
            elements.inspStreamer.textContent = streamerName;
        }
        if (elements.inspSession) elements.inspSession.textContent = msg.session_id ? `#${msg.session_id}` : "Keine Session zugeordnet";
        if (elements.inspUsername) elements.inspUsername.textContent = `@${msg.username}`;
        if (elements.inspNickname) elements.inspNickname.textContent = msg.nickname || msg.username || "-";
        if (elements.inspUserId) elements.inspUserId.textContent = msg.user_id || "Nicht hinterlegt";

        if (elements.inspTier) {
            let tierHtml = '<span class="tier-badge tier-badge-default">Chatter</span>';
            if (msg.whale_tier === "whale") {
                tierHtml = '<span class="tier-badge tier-badge-whale">🐋 WHALE (Top-Spender)</span>';
            } else if (msg.whale_tier === "shark") {
                tierHtml = '<span class="tier-badge tier-badge-shark">🦈 SHARK (Spender)</span>';
            } else if (msg.whale_tier === "dolphin" || msg.is_whale) {
                tierHtml = '<span class="tier-badge tier-badge-vip">🐬 VIP / DOLPHIN</span>';
            }
            elements.inspTier.innerHTML = tierHtml;
        }

        if (elements.inspComment) {
            elements.inspComment.textContent = msg.comment || "";
        }

        if (elements.inspRawJson) {
            elements.inspRawJson.textContent = JSON.stringify(msg, null, 2);
        }

        if (elements.btnOpenTikTokProfile) {
            elements.btnOpenTikTokProfile.href = `https://www.tiktok.com/@${encodeURIComponent(msg.username)}`;
        }

        elements.recordInspectorModal.style.display = "flex";
    }

    function closeRecordInspector() {
        if (elements.recordInspectorModal) {
            elements.recordInspectorModal.style.display = "none";
        }
        state.selectedMessage = null;
    }

    // 6. Export Download Trigger
    function triggerExportDownload(format) {
        const effectiveQuery = state.activeFilterWord || state.searchTerm;
        let url = `/api/chat-intelligence/export?format=${encodeURIComponent(format)}`;
        if (state.sessionId) {
            url += `&session_id=${encodeURIComponent(state.sessionId)}`;
        } else if (state.streamerId) {
            url += `&streamer_id=${encodeURIComponent(state.streamerId)}`;
        }
        if (state.userFilter) {
            url += `&username=${encodeURIComponent(state.userFilter)}`;
        }
        if (effectiveQuery) {
            url += `&search=${encodeURIComponent(effectiveQuery)}`;
        }
        if (state.tierFilter === "whales" || state.tierFilter === "whale" || state.tierFilter === "shark" || state.tierFilter === "dolphin") {
            url += `&only_whales=true`;
        }

        const a = document.createElement("a");
        a.href = url;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        showToast(`${format.toUpperCase()}-Export wird heruntergeladen!`);
    }

    // Run on DOM ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
