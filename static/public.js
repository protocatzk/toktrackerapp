let currentPublicPeriod = "24h";
let publicStreamers = [];
let publicGifts = [];
let currentLeaderboardView = localStorage.getItem("toktracker_pub_view") || (window.innerWidth <= 800 ? "cards" : "cards");

// Chart.js Instanzen für Analytics
let pubTimelineChartInstance = null;
let pubDaytimeChartInstance = null;
let battlesLoadedForPeriod = null;
let analyticsLoadedForPeriod = null;

// Formatters
function fmtEUR(v) {
    return new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" }).format(v || 0);
}

function fmtUSD(v) {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(v || 0);
}

function fmtNum(v) {
    return new Intl.NumberFormat("de-DE").format(Math.round(v || 0));
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function applyTheme(theme) {
    const normalized = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", normalized);
    try {
        localStorage.setItem("toktracker_theme", normalized);
    } catch (e) {}
    const btn = document.getElementById("btnThemeToggle");
    if (btn) {
        btn.textContent = normalized === "dark" ? "Theme: DARK" : "Theme: LIGHT";
    }
    // Falls Charts geladen sind, Farben aktualisieren
    if (pubTimelineChartInstance) {
        pubTimelineChartInstance.update();
    }
}

// Tab Switching
function switchPublicTab(tabId) {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-tab") === tabId);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === tabId);
    });

    if (tabId === "pub-tab-battles") {
        loadPublicBattles();
    } else if (tabId === "pub-tab-analytics") {
        loadPublicAnalytics();
    }
}

// Ansicht-Umschalter für Leaderboard (Karten vs. Tabelle)
function setLeaderboardView(view) {
    currentLeaderboardView = view;
    try {
        localStorage.setItem("toktracker_pub_view", view);
    } catch (e) {}

    const btnCards = document.getElementById("pubBtnViewCards");
    const btnTable = document.getElementById("pubBtnViewTable");
    const gridEl = document.getElementById("pubStreamerCardsGrid");
    const tableWrapEl = document.getElementById("pubStreamerTableWrap");

    if (view === "table") {
        if (btnCards) btnCards.classList.remove("active");
        if (btnTable) btnTable.classList.add("active");
        if (gridEl) gridEl.style.display = "none";
        if (tableWrapEl) tableWrapEl.style.display = "block";
    } else {
        if (btnCards) btnCards.classList.add("active");
        if (btnTable) btnTable.classList.remove("active");
        if (gridEl) gridEl.style.display = "grid";
        if (tableWrapEl) tableWrapEl.style.display = "none";
    }
}

// =====================================================================
// 1. HAUPTDATEN: OVERVIEW & LEADERBOARD
// =====================================================================
async function loadPublicOverview(isSilent = false) {
    try {
        const res = await fetch(`/api/public/overview?period=${encodeURIComponent(currentPublicPeriod)}`);
        if (!res.ok) return;
        const data = await res.json();

        const t = data.totals || {};
        const netEurEl = document.getElementById("pubSumNetEur");
        const netUsdEl = document.getElementById("pubSumNetUsd");
        const coinsEl = document.getElementById("pubSumCoins");
        const streamersSubEl = document.getElementById("pubSumStreamersSub");
        const grossEurEl = document.getElementById("pubSumGrossEur");
        const grossUsdEl = document.getElementById("pubSumGrossUsd");
        const giftsSentEl = document.getElementById("pubSumGiftsSent");
        const giftEventsEl = document.getElementById("pubSumGiftEvents");

        if (netEurEl) netEurEl.textContent = fmtEUR(t.eur_net);
        if (netUsdEl) netUsdEl.textContent = `${fmtUSD(t.usd_net)} USD (~50% Netto)`;
        if (coinsEl) coinsEl.textContent = fmtNum(t.total_coins);
        if (streamersSubEl) streamersSubEl.textContent = `${fmtNum(t.streamer_count)} Streamer (${fmtNum(t.live_count)} LIVE)`;
        if (grossEurEl) grossEurEl.textContent = fmtEUR(t.eur_gross);
        if (grossUsdEl) grossUsdEl.textContent = `${fmtUSD(t.usd_gross)} USD`;
        if (giftsSentEl) giftsSentEl.textContent = fmtNum(t.total_gifts_sent);
        if (giftEventsEl) giftEventsEl.textContent = `${fmtNum(t.gift_events_count)} Transaktionen`;

        const liveCountEl = document.getElementById("pubLiveCount");
        const totalStreamersEl = document.getElementById("pubTotalStreamers");
        const badgeStreamersEl = document.getElementById("pubBadgeStreamers");
        const badgeGiftsEl = document.getElementById("pubBadgeGifts");

        if (liveCountEl) liveCountEl.textContent = fmtNum(t.live_count || 0);
        if (totalStreamersEl) totalStreamersEl.textContent = fmtNum(t.streamer_count || 0);
        if (badgeStreamersEl) badgeStreamersEl.textContent = fmtNum(t.streamer_count || 0);
        if (badgeGiftsEl) badgeGiftsEl.textContent = fmtNum(t.gift_events_count || 0);

        const nowStr = new Date().toLocaleTimeString("de-DE", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });
        const lastUpdateEl = document.getElementById("pubLastUpdate");
        if (lastUpdateEl) lastUpdateEl.textContent = nowStr;

        if (data.rates) {
            const netPer1k = (data.rates.usd_per_diamond || 0.005) * 1000;
            const grossPer1k = (data.rates.gross_usd_per_coin || 0.0106) * 1000;
            const netEl = document.getElementById("pubRateNetInfo");
            const grossEl = document.getElementById("pubRateGrossInfo");
            const fxEl = document.getElementById("pubRateFxInfo");
            if (netEl) netEl.textContent = `${fmtUSD(netPer1k)} USD pro 1.000 Coins (~50 % Creator-Anteil)`;
            if (grossEl) grossEl.textContent = `${fmtUSD(grossPer1k)} USD pro 1.000 Coins`;
            if (fxEl) fxEl.textContent = String(data.rates.usd_to_eur_rate || 0.92);
        }

        publicStreamers = data.streamers || [];
        publicGifts = data.recent_gifts || [];

        renderPublicLeaderboard();
        renderPublicGifts();
    } catch (e) {
        if (!isSilent) console.error("Fehler beim Laden der öffentlichen Übersicht:", e);
    }
}

function renderPublicLeaderboard() {
    const filterVal = (document.getElementById("pubInputStreamerFilter")?.value || "").trim().toLowerCase();
    const onlyLive = Boolean(document.getElementById("pubCheckOnlyLive")?.checked);

    const filtered = publicStreamers.filter((s) => {
        if (onlyLive && !s.is_live) return false;
        if (!filterVal) return true;
        const u = (s.username || "").toLowerCase();
        const d = (s.display_name || "").toLowerCase();
        return u.includes(filterVal) || d.includes(filterVal);
    });

    const metaEl = document.getElementById("pubStreamerTableMeta");
    if (metaEl) metaEl.textContent = `${filtered.length} von ${publicStreamers.length} Streamern`;

    // 1. Mobile & Tablet Card Grid Rendering
    const gridEl = document.getElementById("pubStreamerCardsGrid");
    if (gridEl) {
        if (filtered.length === 0) {
            gridEl.innerHTML = `<div class="muted" style="padding:30px; text-align:center; grid-column:1/-1;">Keine Streamer für die aktuelle Auswahl gefunden.</div>`;
        } else {
            gridEl.innerHTML = filtered.map((s, idx) => {
                const rank = idx + 1;
                const topRankClass = rank === 1 ? "top-1" : rank === 2 ? "top-2" : rank === 3 ? "top-3" : "";
                const avatar = s.avatar_url || "/pictures/bot_avatar.jpg";
                const isLive = Boolean(s.is_live);
                const statusHtml = isLive
                    ? `<span class="status-tag live">● LIVE</span>`
                    : s.is_tracking_active
                    ? `<span class="status-tag polling">STANDBY</span>`
                    : `<span class="status-tag">OFFLINE</span>`;

                const topGifterStr = s.top_gifter ? `@${escapeHtml(s.top_gifter.sender_username)}` : "-";
                const topGiftStr = s.top_gift ? `${escapeHtml(s.top_gift.gift_name)} (${fmtNum(s.top_gift.coins)})` : "-";

                return `
                    <div class="streamer-card-mobile ${isLive ? 'is-live' : ''}">
                        <div class="streamer-card-header">
                            <span class="rank-badge ${topRankClass}">#${rank}</span>
                            <img src="${escapeHtml(avatar)}" alt="${escapeHtml(s.username)}" class="streamer-card-avatar" onerror="this.src='/pictures/bot_avatar.jpg'" />
                            <div class="streamer-card-info">
                                <div class="streamer-card-title">${escapeHtml(s.display_name || s.username)}</div>
                                <div class="streamer-card-uname">@${escapeHtml(s.username)}</div>
                            </div>
                            <div>${statusHtml}</div>
                        </div>

                        <div class="streamer-card-kpi-row">
                            <div class="streamer-card-kpi-item">
                                <div class="kpi-lbl">Creator Netto</div>
                                <div class="kpi-val highlight">${fmtEUR(s.eur_net)}</div>
                            </div>
                            <div class="streamer-card-kpi-item">
                                <div class="kpi-lbl">Coins gesamt</div>
                                <div class="kpi-val coins">💎 ${fmtNum(s.total_coins)}</div>
                            </div>
                            <div class="streamer-card-kpi-item">
                                <div class="kpi-lbl">Zuschauer / Follower</div>
                                <div class="kpi-val" style="font-size:13px;">${fmtNum(s.current_viewers || 0)} <span class="muted" style="font-size:10px;">/ ${fmtNum(s.follower_count || 0)}</span></div>
                            </div>
                            <div class="streamer-card-kpi-item">
                                <div class="kpi-lbl">Gifts / Spender</div>
                                <div class="kpi-val" style="font-size:13px;">${fmtNum(s.total_gifts_sent)} <span class="muted" style="font-size:10px;">(${fmtNum(s.unique_gifters)})</span></div>
                            </div>
                        </div>

                        <div style="font-size:11px; color:var(--text-muted); display:flex; justify-content:space-between;">
                            <span>Top Spender: <strong>${topGifterStr}</strong></span>
                            <span>Top Gift: <strong>${topGiftStr}</strong></span>
                        </div>

                        <div class="streamer-card-footer">
                            <button type="button" class="sm primary" onclick="showPublicStreamerDetail(${s.id}); return false;" style="flex:1;">
                                📊 Detail-Auswertung
                            </button>
                            <a class="btn-link sm mono" href="https://www.tiktok.com/@${encodeURIComponent(s.username)}/live" target="_blank" rel="noopener">
                                TikTok ↗
                            </a>
                        </div>
                    </div>
                `;
            }).join("");
        }
    }

    // 2. Desktop Tabellen-Ansicht Rendering
    const tbody = document.getElementById("pubStreamerTableBody");
    if (tbody) {
        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="10" class="muted">Keine Streamer für diese Filterauswahl gefunden.</td></tr>`;
            return;
        }

        tbody.innerHTML = filtered.map((s, idx) => {
            const rank = idx + 1;
            const rankClass = rank === 1 ? "top-1" : rank === 2 ? "top-2" : rank === 3 ? "top-3" : (rank <= 3 && s.total_coins > 0 ? "top-rank" : "");

            let statusBadge = `<span class="status-tag">OFFLINE</span>`;
            if (s.is_live) {
                statusBadge = `<span class="status-tag live">● LIVE</span>`;
            } else if (s.is_tracking_active) {
                statusBadge = `<span class="status-tag polling">STANDBY</span>`;
            }

            const topGift = s.top_gift ? `${escapeHtml(s.top_gift.gift_name)} (${fmtNum(s.top_gift.coins)})` : "-";
            const topGifter = s.top_gifter ? `@${escapeHtml(s.top_gifter.sender_username)} (${fmtNum(s.top_gifter.coins)})` : "-";

            return `
                <tr>
                    <td class="mono">
                        <span class="rank-badge ${rankClass}">#${rank}</span>
                    </td>
                    <td>
                        <strong><a href="#" onclick="showPublicStreamerDetail(${s.id}); return false;">${escapeHtml(s.display_name || s.username)}</a></strong>
                        <div class="mono muted">@${escapeHtml(s.username)}</div>
                    </td>
                    <td>${statusBadge}</td>
                    <td class="num-col hide-md">
                        <strong>${fmtNum(s.current_viewers || 0)}</strong>
                        <div class="muted">${fmtNum(s.follower_count || 0)} Follower</div>
                    </td>
                    <td class="num-col">
                        <strong>${fmtNum(s.total_coins)}</strong>
                        <div class="muted">${fmtNum(s.total_gifts_sent)} Gifts · ${fmtNum(s.unique_gifters)} Spender</div>
                    </td>
                    <td class="num-col"><strong>${fmtEUR(s.eur_net)}</strong></td>
                    <td class="num-col hide-sm">${fmtUSD(s.usd_net)}</td>
                    <td class="num-col hide-md">${fmtEUR(s.eur_gross)}</td>
                    <td class="mono hide-sm">
                        <div>Gift: ${topGift}</div>
                        <div class="muted">Spender: ${topGifter}</div>
                    </td>
                    <td>
                        <div class="action-group">
                            <button type="button" class="sm primary" onclick="showPublicStreamerDetail(${s.id}); return false;">Auswertung</button>
                            <a class="btn-link sm mono hide-sm" href="https://www.tiktok.com/@${encodeURIComponent(s.username)}/live" target="_blank" rel="noopener">TikTok ↗</a>
                        </div>
                    </td>
                </tr>
            `;
        }).join("");
    }
}

function renderPublicGifts() {
    const tbody = document.getElementById("pubGiftLogBody");
    if (!tbody) return;
    const filterVal = (document.getElementById("pubInputGiftFilter")?.value || "").trim().toLowerCase();

    const filtered = publicGifts.filter((g) => {
        if (!filterVal) return true;
        return (
            (g.streamer_username || "").toLowerCase().includes(filterVal) ||
            (g.sender_username || "").toLowerCase().includes(filterVal) ||
            (g.sender_nickname || "").toLowerCase().includes(filterVal) ||
            (g.gift_name || "").toLowerCase().includes(filterVal)
        );
    });

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="muted">Keine Geschenke im gewählten Zeitraum.</td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map((g) => {
        const ts = (g.created_at || "").replace("T", " ").substring(0, 19);
        const isWhale = (g.total_coins || 0) >= 1000;
        const rowClass = isWhale ? "whale-gift-highlight" : "";

        return `
            <tr class="${rowClass}">
                <td class="mono muted">${escapeHtml(ts)}</td>
                <td class="mono"><strong>@${escapeHtml(g.streamer_username)}</strong></td>
                <td>
                    ${escapeHtml(g.sender_nickname || g.sender_username)}
                    <span class="mono muted hide-sm">(@${escapeHtml(g.sender_username)})</span>
                    ${g.is_super_fan ? `<span class="status-tag polling">SUPERFAN</span>` : ""}
                    ${isWhale ? `<span class="whale-tag">WHALE 💎</span>` : ""}
                </td>
                <td class="hide-sm"><strong>${g.repeat_count}x</strong> ${escapeHtml(g.gift_name)}</td>
                <td class="num-col"><strong>${fmtNum(g.total_coins)}</strong></td>
                <td class="num-col"><strong>${fmtEUR(g.eur_net)}</strong></td>
                <td class="num-col hide-sm">${fmtUSD(g.usd_net)}</td>
            </tr>
        `;
    }).join("");
}

async function showPublicStreamerDetail(streamerId) {
    try {
        const res = await fetch(
            `/api/public/streamers/${encodeURIComponent(streamerId)}?period=${encodeURIComponent(currentPublicPeriod)}`
        );
        if (!res.ok) return;
        const data = await res.json();
        const s = data.streamer;

        const box = document.getElementById("pubStreamerDetailBox");
        if (box) box.style.display = "block";

        const titleEl = document.getElementById("pubDetailTitle");
        const subEl = document.getElementById("pubDetailSubtitle");
        if (titleEl) titleEl.textContent = `Auswertung: ${s.display_name || s.username} (@${s.username})`;
        if (subEl) {
            subEl.textContent = `Netto: ${fmtEUR(data.summary.eur_net)} (${fmtUSD(data.summary.usd_net)}) · Coins: ${fmtNum(data.summary.total_coins)} · Brutto: ${fmtEUR(data.summary.eur_gross)} · Geschenke: ${fmtNum(data.summary.total_gifts_sent)}`;
        }

        const ttLink = document.getElementById("pubDetailTikTokLink");
        if (ttLink) {
            ttLink.href = `https://www.tiktok.com/@${encodeURIComponent(s.username)}/live`;
        }

        const giftsBody = document.getElementById("pubDetailGiftsBody");
        if (giftsBody) {
            giftsBody.innerHTML = data.gift_breakdown.length === 0
                ? `<tr><td colspan="4" class="muted">Keine Geschenke im Zeitraum aufgezeichnet.</td></tr>`
                : data.gift_breakdown.map((g) => `
                    <tr>
                        <td><strong>${escapeHtml(g.gift_name)}</strong> <span class="mono muted">(${g.diamond_count})</span></td>
                        <td class="num-col">${fmtNum(g.total_quantity)}x</td>
                        <td class="num-col">${fmtNum(g.total_coins)}</td>
                        <td class="num-col"><strong>${fmtEUR(g.eur_net)}</strong></td>
                    </tr>
                `).join("");
        }

        const giftersBody = document.getElementById("pubDetailGiftersBody");
        if (giftersBody) {
            giftersBody.innerHTML = data.top_gifters.length === 0
                ? `<tr><td colspan="4" class="muted">Keine Spenderdaten verfügbar.</td></tr>`
                : data.top_gifters.map((g) => `
                    <tr>
                        <td><strong>@${escapeHtml(g.sender_username)}</strong> <span class="mono muted">(${escapeHtml(g.sender_nickname || '')})</span></td>
                        <td class="num-col">${fmtNum(g.gifts_sent)}x</td>
                        <td class="num-col">${fmtNum(g.total_coins)}</td>
                        <td class="num-col"><strong>${fmtEUR(g.eur_net)}</strong></td>
                    </tr>
                `).join("");
        }

        const sessionsList = document.getElementById("pubDetailSessionsList");
        if (sessionsList) {
            sessionsList.innerHTML = data.sessions.length === 0
                ? `<div>Keine Sessions im Zeitraum.</div>`
                : data.sessions.map((sess) => {
                    const startStr = (sess.started_at || "").replace("T", " ").substring(0, 16);
                    return `<div>• Session #${sess.id}: gestartet ${startStr} · ${fmtNum(sess.diamonds_collected)} Coins · Peak: ${fmtNum(sess.peak_viewers)} Viewers</div>`;
                }).join("");
        }

        // Weich zum Detail-Bereich scrollen
        if (box) {
            box.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
    } catch (e) {
        console.error("Fehler beim Laden der Streamer-Details:", e);
    }
}

// =====================================================================
// 2. LIVE-BATTLES & MATCH-CENTER
// =====================================================================
async function loadPublicBattles(isSilent = false) {
    try {
        const resultVal = document.getElementById("pubSelectBattleResult")?.value || "ALL";
        const modeVal = document.getElementById("pubSelectBattleMode")?.value || "ALL";
        const searchVal = document.getElementById("pubInputBattleSearch")?.value || "";

        let url = `/api/public/battles?period=${encodeURIComponent(currentPublicPeriod)}&limit=50`;
        if (resultVal !== "ALL") url += `&result=${encodeURIComponent(resultVal)}`;
        if (modeVal !== "ALL") url += `&mode=${encodeURIComponent(modeVal)}`;
        if (searchVal.trim()) url += `&search=${encodeURIComponent(searchVal.trim())}`;

        const [battlesRes, statsRes] = await Promise.all([
            fetch(url),
            fetch(`/api/public/battles/stats?period=${encodeURIComponent(currentPublicPeriod)}`)
        ]);

        if (battlesRes.ok) {
            const bData = await battlesRes.json();
            renderPublicBattlesFeed(bData);
            renderPublicLiveActiveBattles(bData.live_active_battles || []);
        }

        if (statsRes.ok) {
            const sData = await statsRes.json();
            renderPublicBattleStats(sData);
        }
    } catch (e) {
        if (!isSilent) console.error("Fehler loadPublicBattles:", e);
    }
}

function renderPublicBattleStats(stats) {
    if (!stats) return;
    const totalEl = document.getElementById("pubBattleTotal");
    const ongoingSubEl = document.getElementById("pubBattleOngoingSub");
    const winrateEl = document.getElementById("pubBattleWinrate");
    const winLossDrawEl = document.getElementById("pubBattleWinLossDraw");
    const netEurEl = document.getElementById("pubBattleNetEur");
    const coinsEl = document.getElementById("pubBattleCoins");
    const maxScoreEl = document.getElementById("pubBattleMaxScore");
    const topRivalEl = document.getElementById("pubBattleTopRival");
    const topRivalSubEl = document.getElementById("pubBattleTopRivalSub");
    const badgeBattlesEl = document.getElementById("pubBadgeBattles");

    const total = stats.total_battles || 0;
    if (totalEl) totalEl.textContent = fmtNum(total);
    if (badgeBattlesEl) badgeBattlesEl.textContent = fmtNum(total);
    if (ongoingSubEl) ongoingSubEl.textContent = `${fmtNum(stats.ongoing_battles || 0)} aktiv`;
    if (winrateEl) winrateEl.textContent = `${(stats.winrate_pct || 0).toFixed(1)} %`;
    if (winLossDrawEl) winLossDrawEl.textContent = `${stats.wins || 0}S / ${stats.losses || 0}N / ${stats.draws || 0}U`;
    if (netEurEl) netEurEl.textContent = fmtEUR(stats.battle_eur_net || 0);
    if (coinsEl) coinsEl.textContent = `${fmtNum(stats.battle_coins || 0)} Coins`;
    if (maxScoreEl) maxScoreEl.textContent = `${fmtNum(stats.max_streamer_score || 0)} Pkt`;

    if (topRivalEl) {
        if (stats.top_rival_username) {
            topRivalEl.textContent = `@${stats.top_rival_username}`;
            if (topRivalSubEl) topRivalSubEl.textContent = `${stats.top_rival_battles || 0} Duelle (${stats.top_rival_wins || 0}S)`;
        } else {
            topRivalEl.textContent = "-";
            if (topRivalSubEl) topRivalSubEl.textContent = "Keine Duelle";
        }
    }
}

function renderPublicLiveActiveBattles(liveBattles) {
    const container = document.getElementById("pubLiveBattlesContainer");
    const list = document.getElementById("pubLiveBattlesList");
    if (!container || !list) return;

    const validLive = (liveBattles || []).filter(item => item && item.battle);
    if (validLive.length === 0) {
        container.style.display = "none";
        list.innerHTML = "";
        return;
    }

    container.style.display = "block";
    list.innerHTML = validLive.map(item => {
        const b = item.battle || {};
        const bMode = b.battle_mode || "1v1";
        const isInternal = Boolean(b.is_internal);
        const isFfa = b.is_ffa || bMode === "1v1v1" || bMode === "1v1v1v1";

        if (isFfa) {
            const players = (b.participants && b.participants.length > 0) ? b.participants : [
                { username: item.username, display_name: item.display_name, avatar_url: item.avatar_url, score: b.streamer_score || 0, is_tracked: true },
                { username: b.opponent_username || "Gegner 1", display_name: b.opponent_display_name || b.opponent_username || "Gegner 1", avatar_url: b.opponent_avatar_url, score: b.opponent_score || 0 },
                { username: b.opponent2_username || "Gegner 2", display_name: b.opponent2_display_name || b.opponent2_username || "Gegner 2", avatar_url: b.opponent2_avatar_url, score: b.opponent2_score || 0 },
            ];
            if ((bMode === "1v1v1v1" || b.opponent3_username) && (!b.participants || b.participants.length < 4)) {
                players.push({ username: b.opponent3_username || "Gegner 3", display_name: b.opponent3_display_name || b.opponent3_username || "Gegner 3", avatar_url: b.opponent3_avatar_url, score: b.opponent3_score || 0 });
            }
            players.sort((x, y) => (y.score || 0) - (x.score || 0));

            const rankBadges = ["🥇 #1", "🥈 #2", "🥉 #3", "4."];
            const modeLabel = bMode === "1v1v1v1" ? "⚡ 4ER-EINZEL LIVE" : "🔺 1 VS 1 VS 1 (3ER) LIVE";

            return `
                <div class="live-active-card card-ffa" style="background:var(--bg-card); border:1px solid #f59e0b; border-left:4px solid #f59e0b; border-radius:8px; padding:12px; margin-bottom:10px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">
                        <div style="display:flex; align-items:center; gap:8px;">
                            <span class="live-pulse" style="background:#f59e0b;"></span>
                            <span class="mode-badge mode-${bMode}" style="font-size:11px; padding:2px 8px;">${modeLabel}</span>
                            <span class="muted" style="font-size:11px;">Battle #${escapeHtml(String(b.battle_id || ""))} · Live im Gange</span>
                        </div>
                        <div>
                            <span class="pct-pill" style="border-color:#f59e0b; color:#f59e0b; font-weight:700;">🔥 +${fmtNum(b.total_coins || b.coins_earned || 0)} Coins</span>
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:10px;">
                        ${players.map((p, idx) => `
                            <div style="background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08); border-radius:6px; padding:8px; text-align:center;">
                                <div style="display:flex; justify-content:space-between; align-items:center; font-size:10px; font-weight:700; margin-bottom:4px;">
                                    <span style="color:#f59e0b;">${rankBadges[idx] || `#${idx+1}`}</span>
                                    ${p.is_tracked ? '<span style="color:#10b981;">● Getrackt</span>' : ''}
                                </div>
                                <div style="width:38px; height:38px; margin:0 auto 4px; border-radius:50%; overflow:hidden;">
                                    <img src="${escapeHtml(p.avatar_url || '/pictures/bot_avatar.jpg')}" style="width:100%; height:100%; object-fit:cover;" onerror="this.src='/pictures/bot_avatar.jpg'" />
                                </div>
                                <div style="font-size:12px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(p.display_name || p.username || '')}</div>
                                <div class="mono" style="font-size:10px; color:var(--text-muted);">@${escapeHtml(p.username || '')}</div>
                                <div class="mono" style="font-size:15px; font-weight:800; color:#38bdf8; margin-top:4px;">${fmtNum(p.score || 0)} <span style="font-size:9px; color:var(--text-muted);">Pkt</span></div>
                            </div>
                        `).join("")}
                    </div>
                </div>
            `;
        }

        if (isInternal) {
            const sA = b.streamer_a || {};
            const sB = b.streamer_b || {};
            const scoreA = fmtNum(sA.score || 0);
            const scoreB = fmtNum(sB.score || 0);
            const totCoins = fmtNum(b.total_coins || 0);
            const totEur = fmtEUR(b.total_eur_net || 0);

            return `
                <div class="live-active-card card-derby" style="background:var(--bg-card); border:1px solid #8b5cf6; border-left:4px solid #8b5cf6; border-radius:8px; padding:12px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                    <div style="display:flex; align-items:center; gap:12px;">
                        <span class="live-pulse" style="background:#8b5cf6;"></span>
                        <div>
                            <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                <span class="mode-badge mode-derby" style="font-size:10px; padding:2px 6px;">⚔️ INTERNES DERBY</span>
                                <strong>@${escapeHtml(sA.username || '')}</strong> <span class="pct-pill" style="font-size:10px; color:#10b981; border-color:#10b981;">+${fmtEUR(sA.eur_net || 0)}</span>
                                <span style="color:#8b5cf6; font-weight:800;">VS</span>
                                <strong>@${escapeHtml(sB.username || '')}</strong> <span class="pct-pill" style="font-size:10px; color:#10b981; border-color:#10b981;">+${fmtEUR(sB.eur_net || 0)}</span>
                            </div>
                            <div class="muted" style="font-size:11px; margin-top:2px;">Battle #${escapeHtml(String(b.battle_id || ""))} · Live im Gange</div>
                        </div>
                    </div>
                    <div style="font-family:'JetBrains Mono', Consolas, monospace; font-size:20px; font-weight:800;">
                        <span style="color:#38bdf8;">${scoreA}</span> : <span style="color:#ec4899;">${scoreB}</span>
                    </div>
                    <div style="text-align:right;">
                        <span class="pct-pill" style="border-color:#f59e0b; color:#f59e0b; font-weight:700;">🔥 +${totCoins} Coins (${totEur})</span>
                    </div>
                </div>
            `;
        }

        const sName = escapeHtml(item.username || "Streamer");
        const oName = escapeHtml(b.opponent_display_name || b.opponent_username || "Gegner");
        const sScore = fmtNum(b.streamer_score || 0);
        const oScore = fmtNum(b.opponent_score || 0);
        const coins = fmtNum(b.coins_earned || 0);

        let modeBadge = `<span class="mode-badge mode-1v1" style="font-size:10px; padding:2px 6px;">1v1</span>`;
        if (bMode === "2v2") modeBadge = `<span class="mode-badge mode-2v2" style="font-size:10px; padding:2px 6px;">2v2</span>`;

        return `
            <div class="live-active-card" style="background:var(--bg-card); border:1px solid #ef4444; border-radius:8px; padding:12px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                <div style="display:flex; align-items:center; gap:12px;">
                    <span class="live-pulse"></span>
                    <div>
                        <div style="display:flex; align-items:center; gap:6px;">
                            ${modeBadge}
                            <strong>@${sName}</strong> <span style="color:#ef4444; font-weight:700;">VS</span> <strong>@${oName}</strong>
                        </div>
                        <div class="muted" style="font-size:11px; margin-top:2px;">Battle #${escapeHtml(String(b.battle_id || ""))} · Live im Gange</div>
                    </div>
                </div>
                <div style="font-family:'JetBrains Mono', Consolas, monospace; font-size:20px; font-weight:800;">
                    <span style="color:#38bdf8;">${sScore}</span> : <span style="color:#ec4899;">${oScore}</span>
                </div>
                <div>
                    <span class="pct-pill" style="border-color:#10b981; color:#10b981; font-weight:700;">+${coins} Coins</span>
                </div>
            </div>
        `;
    }).join("");
}

function renderPublicBattlesFeed(data) {
    const feed = document.getElementById("pubBattlesFeed");
    const empty = document.getElementById("pubBattlesEmptyState");
    const countMeta = document.getElementById("pubBattleCountMeta");
    if (!feed) return;

    const total = data.total || 0;
    if (countMeta) countMeta.textContent = `${fmtNum(total)} Duelle`;

    const battles = data.battles || [];
    if (battles.length === 0) {
        if (empty) empty.style.display = "block";
        feed.innerHTML = "";
        if (empty) feed.appendChild(empty);
        return;
    }

    if (empty) empty.style.display = "none";

    feed.innerHTML = battles.map(b => {
        const isWin = b.result === "WIN";
        const isLose = b.result === "LOSE";
        const isDraw = b.result === "DRAW";
        const isOngoing = b.result === "ONGOING" || b.status === "active";

        const bMode = b.battle_mode || "1v1";
        const isFfa = bMode === "1v1v1" || bMode === "1v1v1v1";
        const is2v2 = bMode === "2v2" || Boolean(b.teammate_username);

        let cardClass = isWin ? "card-win" : isLose ? "card-lose" : "card-draw";
        let resultLabel = isWin ? "🏆 SIEG" : isLose ? "❌ NIEDERLAGE" : isOngoing ? "🔴 LIVE DUELL" : "🤝 UNENTSCHIEDEN";
        let resultClass = isWin ? "win" : isLose ? "lose" : isOngoing ? "ongoing" : "draw";

        if (b.is_internal) {
            cardClass = "card-derby";
            resultLabel = b.winner_username ? `🏆 SIEGER: @${escapeHtml(b.winner_username)}` : "⚔️ INTERNES DERBY";
            resultClass = "win";
        } else if (isFfa && !isOngoing) {
            const rank = b.streamer_rank || (isWin ? 1 : 2);
            if (rank === 1) {
                cardClass = "card-win";
                resultLabel = "🥇 1. PLATZ (SIEG)";
                resultClass = "win";
            } else if (rank === 2) {
                cardClass = "card-lose";
                resultLabel = "🥈 2. PLATZ";
                resultClass = "lose";
            } else if (rank === 3) {
                cardClass = "card-lose";
                resultLabel = "🥉 3. PLATZ";
                resultClass = "lose";
            } else {
                cardClass = "card-lose";
                resultLabel = "4. PLATZ";
                resultClass = "lose";
            }
        }

        let modeBadge = `<span class="mode-badge mode-1v1">⚔️ 1 VS 1</span>`;
        if (b.is_internal) {
            modeBadge = `<span class="mode-badge mode-derby">⚔️ INTERNES DERBY</span>`;
        } else if (bMode === "2v2") {
            modeBadge = `<span class="mode-badge mode-2v2">👥 2 VS 2 TEAM</span>`;
        } else if (bMode === "1v1v1") {
            modeBadge = `<span class="mode-badge mode-1v1v1">🔺 1 VS 1 VS 1 (3ER)</span>`;
        } else if (bMode === "1v1v1v1") {
            modeBadge = `<span class="mode-badge mode-1v1v1v1">⚡ 4ER-EINZEL (1V1V1V1)</span>`;
        }

        const dateStr = (b.created_at || "").replace("T", " ").substring(0, 16);
        const sScore = b.streamer_score || 0;
        const oScore = b.opponent_score || 0;
        const totScore = sScore + oScore;
        const sPct = totScore > 0 ? Math.round((sScore / totScore) * 100) : 50;
        const oPct = 100 - sPct;

        let matchupHtml = "";
        if (isFfa) {
            const players = [
                {
                    username: b.streamer_username,
                    name: b.streamer_display_name || b.streamer_username,
                    avatar: b.streamer_avatar_url || '/pictures/bot_avatar.jpg',
                    score: sScore,
                    isMine: true,
                },
                {
                    username: b.opponent_username || "Gegner 1",
                    name: b.opponent_display_name || b.opponent_username || "Gegner 1",
                    avatar: b.opponent_avatar_url || '/pictures/bot_avatar.jpg',
                    score: oScore,
                    isMine: false,
                },
                {
                    username: b.opponent2_username || "Gegner 2",
                    name: b.opponent2_display_name || b.opponent2_username || "Gegner 2",
                    avatar: b.opponent2_avatar_url || '/pictures/bot_avatar.jpg',
                    score: b.opponent2_score || 0,
                    isMine: false,
                },
            ];
            if (bMode === "1v1v1v1" || b.opponent3_username) {
                players.push({
                    username: b.opponent3_username || "Gegner 3",
                    name: b.opponent3_display_name || b.opponent3_username || "Gegner 3",
                    avatar: b.opponent3_avatar_url || '/pictures/bot_avatar.jpg',
                    score: b.opponent3_score || 0,
                    isMine: false,
                });
            }

            players.sort((x, y) => (y.score || 0) - (x.score || 0));
            const maxScore = Math.max(1, ...players.map(p => p.score || 0));
            const rankPills = ["🥇 #1", "🥈 #2", "🥉 #3", "4."];

            matchupHtml = `
                <div class="battle-ffa-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:10px; margin:10px 0;">
                    ${players.map((p, idx) => {
                        const rankPill = rankPills[idx] || `#${idx + 1}`;
                        const pct = Math.round(((p.score || 0) / maxScore) * 100);
                        const isWinnerSlot = idx === 0 && p.score > 0;
                        return `
                            <div class="ffa-player-card ${p.isMine ? 'is-my-streamer' : ''} ${isWinnerSlot ? 'is-winner' : ''}" style="background:rgba(255,255,255,0.03); border:1px solid ${isWinnerSlot ? '#f59e0b' : 'rgba(255,255,255,0.08)'}; border-radius:8px; padding:10px; text-align:center;">
                                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; font-size:11px; font-weight:700;">
                                    <span style="color:#f59e0b;">${rankPill}</span>
                                    ${isWinnerSlot ? '<span>👑</span>' : '<span></span>'}
                                </div>
                                <div class="participant-avatar-wrap" style="width:44px; height:44px; margin:0 auto 6px; border-radius:50%; overflow:hidden;">
                                    <img src="${escapeHtml(p.avatar)}" alt="${escapeHtml(p.username)}" class="battle-avatar ${isWinnerSlot ? 'winner-ring' : ''}" onerror="this.src='/pictures/bot_avatar.jpg';" />
                                </div>
                                <div style="font-size:12px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div>
                                <div class="mono" style="font-size:10px; color:var(--text-muted);">@${escapeHtml(p.username)}</div>
                                <div class="mono" style="font-size:15px; font-weight:800; color:${isWinnerSlot ? '#10b981' : '#38bdf8'}; margin-top:4px;">${fmtNum(p.score || 0)} <span style="font-size:10px; color:var(--text-muted);">Pkt</span></div>
                                <div style="height:4px; background:rgba(255,255,255,0.1); border-radius:2px; margin-top:6px; overflow:hidden;">
                                    <div style="height:100%; width:${pct}%; background:${isWinnerSlot ? '#10b981' : '#38bdf8'};"></div>
                                </div>
                            </div>
                        `;
                    }).join("")}
                </div>
            `;
        } else if (is2v2) {
            matchupHtml = `
                <div class="battle-arena-1v1">
                    <div class="battle-participant streamer">
                        <div class="participant-avatar-wrap">
                            <img src="${escapeHtml(b.streamer_avatar_url || '/pictures/bot_avatar.jpg')}" alt="Streamer" class="battle-avatar ${isWin ? 'winner-ring' : ''}" onerror="this.src='/pictures/bot_avatar.jpg'" />
                        </div>
                        <div class="participant-name">${escapeHtml(b.streamer_display_name || b.streamer_username)}</div>
                        <div class="participant-handle mono">@${escapeHtml(b.streamer_username)}</div>
                        ${b.teammate_username ? `<div class="mono muted" style="font-size:10px;">+ @${escapeHtml(b.teammate_username)}</div>` : ''}
                        <div class="participant-score ${isWin ? 'score-win' : ''}">${fmtNum(sScore)}</div>
                    </div>
                    <div class="battle-versus-center">
                        <span class="vs-badge">VS</span>
                        <div class="versus-bar-container">
                            <div class="versus-bar-fill-streamer" style="width: ${sPct}%;"></div>
                            <div class="versus-bar-fill-opponent" style="width: ${oPct}%;"></div>
                        </div>
                        <div class="score-delta-text">${sPct}% : ${oPct}%</div>
                    </div>
                    <div class="battle-participant opponent">
                        <div class="participant-avatar-wrap">
                            <img src="${escapeHtml(b.opponent_avatar_url || '/pictures/bot_avatar.jpg')}" alt="Gegner" class="battle-avatar ${isLose ? 'winner-ring' : ''}" onerror="this.src='/pictures/bot_avatar.jpg'" />
                        </div>
                        <div class="participant-name">${escapeHtml(b.opponent_display_name || b.opponent_username || 'Gegner')}</div>
                        <div class="participant-handle mono">@${escapeHtml(b.opponent_username || 'unbekannt')}</div>
                        ${b.opponent2_username ? `<div class="mono muted" style="font-size:10px;">+ @${escapeHtml(b.opponent2_username)}</div>` : ''}
                        <div class="participant-score ${isLose ? 'score-win' : ''}">${fmtNum(oScore)}</div>
                    </div>
                </div>
            `;
        } else {
            matchupHtml = `
                <div class="battle-arena-1v1">
                    <div class="battle-participant streamer">
                        <div class="participant-avatar-wrap">
                            <img src="${escapeHtml(b.streamer_avatar_url || '/pictures/bot_avatar.jpg')}" alt="Streamer" class="battle-avatar ${isWin ? 'winner-ring' : ''}" onerror="this.src='/pictures/bot_avatar.jpg'" />
                        </div>
                        <div class="participant-name">${escapeHtml(b.streamer_display_name || b.streamer_username)}</div>
                        <div class="participant-handle mono">@${escapeHtml(b.streamer_username)}</div>
                        <div class="participant-score ${isWin ? 'score-win' : ''}">${fmtNum(sScore)}</div>
                    </div>

                    <div class="battle-versus-center">
                        <span class="vs-badge">VS</span>
                        <div class="versus-bar-container">
                            <div class="versus-bar-fill-streamer" style="width: ${sPct}%;"></div>
                            <div class="versus-bar-fill-opponent" style="width: ${oPct}%;"></div>
                        </div>
                        <div class="score-delta-text">${sPct}% : ${oPct}%</div>
                    </div>

                    <div class="battle-participant opponent">
                        <div class="participant-avatar-wrap">
                            <img src="${escapeHtml(b.opponent_avatar_url || '/pictures/bot_avatar.jpg')}" alt="Gegner" class="battle-avatar ${isLose ? 'winner-ring' : ''}" onerror="this.src='/pictures/bot_avatar.jpg'" />
                        </div>
                        <div class="participant-name">${escapeHtml(b.opponent_display_name || b.opponent_username || 'Gegner')}</div>
                        <div class="participant-handle mono">@${escapeHtml(b.opponent_username || 'unbekannt')}</div>
                        <div class="participant-score ${isLose ? 'score-win' : ''}">${fmtNum(oScore)}</div>
                    </div>
                </div>
            `;
        }

        const mvpName = b.mvp_supporter_nickname || b.mvp_supporter_username;
        const mvpPoints = b.mvp_supporter_points || 0;
        const mvpHtml = mvpName ? `
            <div class="battle-mvp-strip" style="margin-top:8px;">
                <div class="mvp-badge-container">
                    <span class="mvp-icon">👑</span>
                    <span class="mvp-label">MVP:</span>
                    <span class="mvp-name">${escapeHtml(mvpName)}</span>
                </div>
                <div class="mvp-points">${fmtNum(mvpPoints)} Pkt</div>
            </div>
        ` : "";

        return `
            <div class="battle-match-card ${cardClass}" style="margin-bottom:12px;">
                <div class="battle-card-header">
                    <div class="battle-meta-left">
                        ${modeBadge}
                        <span class="battle-timestamp mono">${escapeHtml(dateStr)}</span>
                        ${b.duration_seconds ? `<span class="battle-duration mono">${Math.floor(b.duration_seconds / 60)}m ${b.duration_seconds % 60}s</span>` : ""}
                    </div>
                    <div>
                        <span class="result-badge ${resultClass}">${resultLabel}</span>
                    </div>
                </div>

                ${matchupHtml}
                ${mvpHtml}

                <div class="battle-card-footer">
                    <span class="battle-earnings-badge">+${fmtNum(b.coins_earned || 0)} Coins (${fmtEUR(b.eur_net || 0)})</span>
                    <span class="mono muted" style="font-size:11px;">Match ID #${escapeHtml(String(b.id || ''))}</span>
                </div>
            </div>
        `;
    }).join("");
}

// =====================================================================
// 3. TREND- & EINNAHMEN-ANALYTICS (CHART.JS)
// =====================================================================
async function loadPublicAnalytics() {
    try {
        const res = await fetch(`/api/public/analytics?period=${encodeURIComponent(currentPublicPeriod)}`);
        if (!res.ok) return;
        const data = await res.json();

        renderPublicAnalyticsCharts(data);
    } catch (e) {
        console.error("Fehler loadPublicAnalytics:", e);
    }
}

function renderPublicAnalyticsCharts(data) {
    if (!data) return;

    // 1. Whale-Konzentration & Averages
    const t = data.totals || {};
    const top1 = t.whale_share_top1_pct || 0;
    const top3 = t.whale_share_top3_pct || 0;
    const top5 = t.whale_share_top5_pct || 0;

    const t1PctEl = document.getElementById("pubWhaleTop1Pct");
    const t1BarEl = document.getElementById("pubWhaleTop1Bar");
    const t3PctEl = document.getElementById("pubWhaleTop3Pct");
    const t3BarEl = document.getElementById("pubWhaleTop3Bar");
    const t5PctEl = document.getElementById("pubWhaleTop5Pct");
    const t5BarEl = document.getElementById("pubWhaleTop5Bar");

    if (t1PctEl) t1PctEl.textContent = `${top1.toFixed(1)} %`;
    if (t1BarEl) t1BarEl.style.width = `${Math.min(top1, 100)}%`;
    if (t3PctEl) t3PctEl.textContent = `${top3.toFixed(1)} %`;
    if (t3BarEl) t3BarEl.style.width = `${Math.min(top3, 100)}%`;
    if (t5PctEl) t5PctEl.textContent = `${top5.toFixed(1)} %`;
    if (t5BarEl) t5BarEl.style.width = `${Math.min(top5, 100)}%`;

    const avgGiftEl = document.getElementById("pubAvgCoinsPerGift");
    const avgGifterEl = document.getElementById("pubAvgCoinsPerGifter");
    if (avgGiftEl) avgGiftEl.textContent = fmtNum(t.avg_coins_per_gift || 0);
    if (avgGifterEl) avgGifterEl.textContent = fmtNum(t.avg_coins_per_gifter || 0);

    const statsEl = document.getElementById("pubAnalyticsTimelineStats");
    if (statsEl) statsEl.textContent = `${fmtNum(t.total_coins || 0)} Coins · ${fmtEUR(t.eur_net || 0)}`;

    // 2. Timeline Chart (Chart.js)
    const timeline = data.timeline || [];
    const canvasTimeline = document.getElementById("pubChartTimeline");

    if (canvasTimeline && window.Chart) {
        if (pubTimelineChartInstance) {
            pubTimelineChartInstance.destroy();
            pubTimelineChartInstance = null;
        }

        const labels = timeline.map(item => item.label);
        const eurData = timeline.map(item => item.eur_net);
        const coinsData = timeline.map(item => item.coins);

        pubTimelineChartInstance = new Chart(canvasTimeline, {
            type: "line",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: "Netto-Auszahlung (€)",
                        data: eurData,
                        borderColor: "#10b981",
                        backgroundColor: "rgba(16, 185, 129, 0.12)",
                        borderWidth: 2.5,
                        fill: true,
                        tension: 0.35,
                        yAxisID: "y",
                    },
                    {
                        label: "Coins",
                        data: coinsData,
                        borderColor: "#f59e0b",
                        backgroundColor: "transparent",
                        borderWidth: 2,
                        borderDash: [4, 4],
                        pointRadius: 2,
                        tension: 0.35,
                        yAxisID: "y1",
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: "index",
                    intersect: false,
                },
                plugins: {
                    legend: {
                        labels: { color: "#94a3b8", font: { family: "'JetBrains Mono', Consolas, monospace", size: 11 } }
                    },
                    tooltip: {
                        backgroundColor: "#1e293b",
                        titleColor: "#f8fafc",
                        bodyColor: "#f1f5f9",
                        borderColor: "#334155",
                        borderWidth: 1,
                    }
                },
                scales: {
                    x: {
                        grid: { color: "rgba(255, 255, 255, 0.05)" },
                        ticks: { color: "#64748b", font: { family: "'JetBrains Mono', monospace", size: 10 } }
                    },
                    y: {
                        type: "linear",
                        display: true,
                        position: "left",
                        grid: { color: "rgba(255, 255, 255, 0.06)" },
                        ticks: {
                            color: "#10b981",
                            callback: (v) => `${v} €`
                        }
                    },
                    y1: {
                        type: "linear",
                        display: true,
                        position: "right",
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: "#f59e0b",
                            callback: (v) => `${v}`
                        }
                    }
                }
            }
        });
    }

    // 3. Daytime Activity Chart
    const daytime = data.time_of_day || [];
    const canvasDaytime = document.getElementById("pubChartDaytime");

    if (canvasDaytime && window.Chart) {
        if (pubDaytimeChartInstance) {
            pubDaytimeChartInstance.destroy();
            pubDaytimeChartInstance = null;
        }

        const dLabels = daytime.map(d => d.label);
        const dCoins = daytime.map(d => d.coins);

        pubDaytimeChartInstance = new Chart(canvasDaytime, {
            type: "bar",
            data: {
                labels: dLabels,
                datasets: [{
                    label: "Coins nach Uhrzeit",
                    data: dCoins,
                    backgroundColor: "rgba(99, 102, 241, 0.65)",
                    borderRadius: 4,
                    hoverBackgroundColor: "#818cf8",
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: "#64748b", font: { size: 9 } }
                    },
                    y: {
                        grid: { color: "rgba(255, 255, 255, 0.05)" },
                        ticks: { color: "#64748b", font: { size: 9 } }
                    }
                }
            }
        });
    }

    // 4. Beliebteste Geschenke Tabelle
    const giftsBody = document.getElementById("pubAnalyticsGiftsBody");
    if (giftsBody) {
        const topGifts = data.top_gifts || [];
        if (topGifts.length === 0) {
            giftsBody.innerHTML = `<tr><td colspan="5" class="muted">Keine Geschenke im gewählten Zeitraum.</td></tr>`;
        } else {
            giftsBody.innerHTML = topGifts.slice(0, 15).map(g => `
                <tr>
                    <td><strong>${escapeHtml(g.gift_name)}</strong></td>
                    <td class="num-col">${fmtNum(g.total_quantity)}x</td>
                    <td class="num-col">💎 ${fmtNum(g.total_coins)}</td>
                    <td class="num-col"><strong>${fmtEUR(g.eur_net)}</strong></td>
                    <td class="num-col hide-sm">${(g.pct_of_total || 0).toFixed(1)} %</td>
                </tr>
            `).join("");
        }
    }
}

// =====================================================================
// 4. INITIALISIERUNG & EVENT-LISTENER
// =====================================================================
document.addEventListener("DOMContentLoaded", () => {
    // 1. Theme Initialisierung
    const savedTheme = localStorage.getItem("toktracker_theme") || "dark";
    applyTheme(savedTheme);
    const themeBtn = document.getElementById("btnThemeToggle");
    if (themeBtn) {
        themeBtn.addEventListener("click", () => {
            const cur = document.documentElement.getAttribute("data-theme") || "dark";
            applyTheme(cur === "dark" ? "light" : "dark");
        });
    }

    // 2. View Toggle (Karten vs. Tabelle)
    setLeaderboardView(currentLeaderboardView);
    document.getElementById("pubBtnViewCards")?.addEventListener("click", () => setLeaderboardView("cards"));
    document.getElementById("pubBtnViewTable")?.addEventListener("click", () => setLeaderboardView("table"));

    // 3. Tab Switching
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            const tabId = btn.getAttribute("data-tab");
            if (tabId) switchPublicTab(tabId);
        });
    });

    // 4. Zeitraum-Auswahl
    const periodSel = document.getElementById("pubPeriodSelect");
    if (periodSel) {
        periodSel.addEventListener("change", (e) => {
            currentPublicPeriod = e.target.value;
            loadPublicOverview();

            // Aktiven Tab bei Zeitraumwechsel neu laden
            const activeTab = document.querySelector(".tab-btn.active")?.getAttribute("data-tab");
            if (activeTab === "pub-tab-battles") loadPublicBattles();
            if (activeTab === "pub-tab-analytics") loadPublicAnalytics();
        });
    }

    // 5. Refresh-Button
    document.getElementById("pubBtnRefresh")?.addEventListener("click", () => {
        loadPublicOverview();
        const activeTab = document.querySelector(".tab-btn.active")?.getAttribute("data-tab");
        if (activeTab === "pub-tab-battles") loadPublicBattles();
        if (activeTab === "pub-tab-analytics") loadPublicAnalytics();
    });

    // 6. Filter-Eingaben
    document.getElementById("pubInputStreamerFilter")?.addEventListener("input", renderPublicLeaderboard);
    document.getElementById("pubCheckOnlyLive")?.addEventListener("change", renderPublicLeaderboard);
    document.getElementById("pubInputGiftFilter")?.addEventListener("input", renderPublicGifts);

    // Battle Filter
    document.getElementById("pubSelectBattleResult")?.addEventListener("change", () => loadPublicBattles());
    document.getElementById("pubSelectBattleMode")?.addEventListener("change", () => loadPublicBattles());
    document.getElementById("pubInputBattleSearch")?.addEventListener("input", () => loadPublicBattles());

    // 7. Schließen des Detail-Panels
    document.getElementById("pubBtnCloseDetail")?.addEventListener("click", () => {
        const box = document.getElementById("pubStreamerDetailBox");
        if (box) box.style.display = "none";
    });

    // 8. Erste Datenladung
    loadPublicOverview();

    // 9. Automatisches Polling alle 25 Sekunden (schonend & performant)
    setInterval(() => {
        loadPublicOverview(true);
        const activeTab = document.querySelector(".tab-btn.active")?.getAttribute("data-tab");
        if (activeTab === "pub-tab-battles") loadPublicBattles(true);
    }, 25000);
});
