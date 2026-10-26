/* =====================================================================
   toktracker Telegram Mini-App Client Logic
   Fast, Lightweight, Mobile-Optimized Real-Time Feed
   ===================================================================== */

(function () {
    "use strict";

    // 1. Telegram WebApp Integration
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) {
        try {
            tg.ready();
            tg.expand();
            if (tg.setHeaderColor) {
                tg.setHeaderColor("#0b0e14");
            }
            if (tg.setBackgroundColor) {
                tg.setBackgroundColor("#0b0e14");
            }
        } catch (e) {
            console.warn("Telegram WebApp init hint:", e);
        }
    }

    // State
    let currentPeriod = "today";
    let activeTab = "tab-matches";
    let pollInterval = null;
    let isFetching = false;

    // Cache DOM Elements
    const elements = {
        btnRefresh: document.getElementById("maBtnRefresh"),
        periodPills: document.querySelectorAll(".period-pill"),
        navTabs: document.querySelectorAll(".nav-tab-btn"),
        tabViews: document.querySelectorAll(".ma-tab-view"),
        
        // KPIs
        kpiNetEur: document.getElementById("kpiNetEur"),
        kpiNetUsd: document.getElementById("kpiNetUsd"),
        kpiCoins: document.getElementById("kpiCoins"),
        kpiGifts: document.getElementById("kpiGifts"),
        kpiStreamers: document.getElementById("kpiStreamers"),
        kpiLiveCount: document.getElementById("kpiLiveCount"),
        liveBanner: document.getElementById("maLiveBanner"),
        liveBannerText: document.getElementById("maLiveBannerText"),

        // Containers
        liveBattlesContainer: document.getElementById("maLiveBattlesContainer"),
        recentBattlesContainer: document.getElementById("maRecentBattlesContainer"),
        streamersContainer: document.getElementById("maStreamersContainer"),
        giftsContainer: document.getElementById("maGiftsContainer"),
        
        // Badges
        badgeMatches: document.getElementById("badgeMatches"),
        badgeStreamers: document.getElementById("badgeStreamers"),
    };

    // Formatters
    function formatNumber(num) {
        return new Intl.NumberFormat("de-DE").format(Math.round(num || 0));
    }

    function formatCurrency(val, currency = "EUR") {
        return new Intl.NumberFormat("de-DE", {
            style: "currency",
            currency: currency,
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        }).format(val || 0);
    }

    function escapeHtml(str) {
        if (!str) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function getRelativeTime(timestamp) {
        if (!timestamp) return "";
        try {
            const date = new Date(timestamp);
            const now = new Date();
            const diffSec = Math.floor((now - date) / 1000);
            if (diffSec < 60) return "gerade eben";
            if (diffSec < 3600) return `vor ${Math.floor(diffSec / 60)}m`;
            if (diffSec < 86400) return `vor ${Math.floor(diffSec / 3600)}h`;
            return date.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
        } catch (e) {
            return timestamp;
        }
    }

    // Trigger haptic feedback inside Telegram if available
    function triggerHaptic(type = "light") {
        if (tg && tg.HapticFeedback) {
            try {
                if (type === "light" || type === "medium" || type === "heavy") {
                    tg.HapticFeedback.impactOccurred(type);
                } else if (type === "success" || type === "error" || type === "warning") {
                    tg.HapticFeedback.notificationOccurred(type);
                }
            } catch (e) {}
        }
    }

    // Fetch and render data
    async function fetchMiniAppData() {
        if (isFetching) return;
        isFetching = true;
        if (elements.btnRefresh) elements.btnRefresh.classList.add("spinning");

        try {
            const res = await fetch(`/api/miniapp/data?period=${encodeURIComponent(currentPeriod)}`, {
                headers: { "Accept": "application/json" }
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            renderMiniApp(data);
        } catch (err) {
            console.error("Mini-App Fetch Fehler:", err);
        } finally {
            isFetching = false;
            if (elements.btnRefresh) elements.btnRefresh.classList.remove("spinning");
        }
    }

    function renderMiniApp(data) {
        const totals = data.totals || {};
        const streamers = data.streamers || [];
        const liveBattles = data.live_battles || [];
        const recentBattles = data.recent_battles || [];
        const recentGifts = data.recent_gifts || [];

        // 1. Render KPIs
        const netEur = totals.eur_net !== undefined ? totals.eur_net : (totals.total_eur_net || 0);
        const netUsd = totals.usd_net !== undefined ? totals.usd_net : (totals.total_usd_net || 0);
        if (elements.kpiNetEur) elements.kpiNetEur.textContent = formatCurrency(netEur, "EUR");
        if (elements.kpiNetUsd) elements.kpiNetUsd.textContent = `$${formatNumber(netUsd)} USD`;
        if (elements.kpiCoins) elements.kpiCoins.textContent = formatNumber(totals.total_coins);
        if (elements.kpiGifts) elements.kpiGifts.textContent = formatNumber(totals.total_gifts_sent || totals.gift_events_count);

        const liveCount = streamers.filter(s => s.is_live).length;
        if (elements.kpiStreamers) elements.kpiStreamers.textContent = `${liveCount} / ${streamers.length}`;
        if (elements.kpiLiveCount) elements.kpiLiveCount.textContent = `${liveCount} LIVE`;

        // 2. Live Banner Status
        if (elements.liveBanner && elements.liveBannerText) {
            if (liveBattles.length > 0) {
                elements.liveBanner.className = "ma-live-banner";
                elements.liveBannerText.textContent = `⚡ JETZT: ${liveBattles.length} Battle(s) LIVE im Gange!`;
            } else if (liveCount > 0) {
                elements.liveBanner.className = "ma-live-banner";
                elements.liveBannerText.textContent = `🟢 ${liveCount} Streamer aktiv LIVE auf Sendung`;
            } else {
                elements.liveBanner.className = "ma-live-banner no-active";
                elements.liveBannerText.textContent = `💤 Aktuell keine Streamer LIVE – Auto-Monitor aktiv`;
            }
        }

        // 3. Badges
        if (elements.badgeMatches) elements.badgeMatches.textContent = liveBattles.length > 0 ? `LIVE (${liveBattles.length})` : recentBattles.length;
        if (elements.badgeStreamers) elements.badgeStreamers.textContent = streamers.length;

        // 4. Render Live & Recent Battles
        renderLiveBattles(liveBattles);
        renderRecentBattles(recentBattles);

        // 5. Render Streamer Overview
        renderStreamers(streamers);

        // 6. Render Gift Ticker
        renderGifts(recentGifts);
    }

    function renderLiveBattles(liveBattles) {
        if (!elements.liveBattlesContainer) return;
        if (!liveBattles || liveBattles.length === 0) {
            elements.liveBattlesContainer.innerHTML = "";
            return;
        }

        let html = '<div class="section-head"><div class="section-title">🔴 LIVE AKTUELLE MATCHES</div></div>';
        liveBattles.forEach(item => {
            const b = item.battle || {};
            const mode = b.battle_mode || "1v1";
            const sName = item.display_name || item.username;
            const sAvatar = item.avatar_url || "/static/avatar_default.png";
            const oName = b.opponent_display_name || b.opponent_username || "Gegner";
            const oAvatar = b.opponent_avatar_url || "/static/avatar_default.png";

            const sScore = b.streamer_score || 0;
            const oScore = b.opponent_score || 0;
            const totalScore = Math.max(1, sScore + oScore);
            const sPct = Math.round((sScore / totalScore) * 100);
            const oPct = 100 - sPct;

            html += `
                <div class="battle-card-mobile active-match">
                    <div class="battle-card-header">
                        <span class="match-mode-pill mode-${escapeHtml(mode)}">${escapeHtml(mode.toUpperCase())}</span>
                        <span class="match-result-badge live">LIVE DUELL</span>
                    </div>

                    <div class="battle-arena-1v1">
                        <div class="combatant-side">
                            <div class="combatant-avatar-wrap">
                                <img src="${escapeHtml(sAvatar)}" class="combatant-avatar ${sScore >= oScore ? 'winner-glow' : ''}" onerror="this.src='/static/avatar_default.png'">
                            </div>
                            <div class="combatant-name">${escapeHtml(sName)}</div>
                            <div class="combatant-score" style="color: #38bdf8;">${formatNumber(sScore)}</div>
                        </div>

                        <div class="vs-center-mobile">
                            <span class="vs-text">VS</span>
                        </div>

                        <div class="combatant-side">
                            <div class="combatant-avatar-wrap">
                                <img src="${escapeHtml(oAvatar)}" class="combatant-avatar ${oScore >= sScore ? 'winner-glow' : ''}" onerror="this.src='/static/avatar_default.png'">
                            </div>
                            <div class="combatant-name">${escapeHtml(oName)}</div>
                            <div class="combatant-score" style="color: #ef4444;">${formatNumber(oScore)}</div>
                        </div>
                    </div>

                    <div class="duel-score-bar-mobile">
                        <div class="bar-side-left" style="width: ${sPct}%;"></div>
                        <div class="bar-side-right" style="width: ${oPct}%;"></div>
                    </div>

                    <div class="battle-card-footer">
                        <span>Punkte-Vorsprung: ${formatNumber(Math.abs(sScore - oScore))}</span>
                        <span class="battle-earnings-badge">LIVE BATTLE</span>
                    </div>
                </div>
            `;
        });

        elements.liveBattlesContainer.innerHTML = html;
    }

    function renderRecentBattles(battles) {
        if (!elements.recentBattlesContainer) return;
        if (!battles || battles.length === 0) {
            elements.recentBattlesContainer.innerHTML = `
                <div class="ma-empty-state">
                    <div class="ma-empty-icon">⚔️</div>
                    <div>Noch keine Matches im gewählten Zeitraum erfasst.</div>
                </div>
            `;
            return;
        }

        let html = "";
        battles.forEach(b => {
            const mode = b.battle_mode || "1v1";
            const res = (b.result || "DRAW").toUpperCase();
            const resClass = res === "WIN" ? "win" : (res === "LOSE" ? "lose" : "draw");
            const resLabel = res === "WIN" ? "SIEG" : (res === "LOSE" ? "NIEDERLAGE" : "UNENTSCHIEDEN");

            const sName = b.streamer_display_name || b.streamer_username || "Streamer";
            const sAvatar = b.streamer_avatar_url || "/static/avatar_default.png";
            const sScore = b.streamer_score || 0;
            const relTime = getRelativeTime(b.started_at || b.created_at);

            // Is FFA (3er or 4er)?
            const isFFA = (mode === "1v1v1" || mode === "1v1v1v1");

            if (isFFA) {
                // Collect and sort FFA contestants
                const contestants = [
                    { name: sName, avatar: sAvatar, score: sScore, isTracked: true, rank: b.streamer_rank || 1 },
                    { name: b.opponent_display_name || b.opponent_username || "Gegner 1", avatar: b.opponent_avatar_url || "/static/avatar_default.png", score: b.opponent_score || 0, isTracked: false, rank: 0 },
                    { name: b.opponent2_display_name || b.opponent2_username || "Gegner 2", avatar: b.opponent2_avatar_url || "/static/avatar_default.png", score: b.opponent2_score || 0, isTracked: false, rank: 0 }
                ];
                if (mode === "1v1v1v1") {
                    contestants.push({
                        name: b.opponent3_display_name || b.opponent3_username || "Gegner 3",
                        avatar: b.opponent3_avatar_url || "/static/avatar_default.png",
                        score: b.opponent3_score || 0,
                        isTracked: false,
                        rank: 0
                    });
                }

                // Sort by score descending
                contestants.sort((a, b) => b.score - a.score);
                const rankEmojis = ["🥇 #1", "🥈 #2", "🥉 #3", "#4"];

                let rowsHtml = "";
                contestants.forEach((c, idx) => {
                    const rankText = rankEmojis[idx] || `#${idx + 1}`;
                    rowsHtml += `
                        <div class="ffa-row-item ${c.isTracked ? 'is-tracked' : ''}">
                            <div class="ffa-player-meta">
                                <span class="ffa-rank-tag">${rankText}</span>
                                <img src="${escapeHtml(c.avatar)}" class="ffa-avatar-small" onerror="this.src='/static/avatar_default.png'">
                                <span class="ffa-name">${escapeHtml(c.name)} ${c.isTracked ? '⭐' : ''}</span>
                            </div>
                            <div class="ffa-score">${formatNumber(c.score)}</div>
                        </div>
                    `;
                });

                html += `
                    <div class="battle-card-mobile">
                        <div class="battle-card-header">
                            <span class="match-mode-pill mode-${escapeHtml(mode)}">${escapeHtml(mode === '1v1v1' ? '3ER-MATCH' : '4ER-EINZEL')}</span>
                            <span class="match-result-badge ${resClass}">Rang #${b.streamer_rank || 1} · ${resLabel}</span>
                        </div>

                        <div class="ffa-ranks-list">
                            ${rowsHtml}
                        </div>

                        <div class="battle-card-footer">
                            <span>${escapeHtml(relTime)}</span>
                            <span class="battle-earnings-badge">${formatCurrency(b.eur_net, "EUR")} (${formatNumber(b.coins_earned)} Coins)</span>
                        </div>
                    </div>
                `;
            } else {
                // 1v1 or 2v2 Duell
                const oName = b.opponent_display_name || b.opponent_username || "Gegner";
                const oAvatar = b.opponent_avatar_url || "/static/avatar_default.png";
                const oScore = b.opponent_score || 0;

                const totalScore = Math.max(1, sScore + oScore);
                const sPct = Math.round((sScore / totalScore) * 100);
                const oPct = 100 - sPct;

                html += `
                    <div class="battle-card-mobile">
                        <div class="battle-card-header">
                            <span class="match-mode-pill mode-${escapeHtml(mode)}">${escapeHtml(mode.toUpperCase())}</span>
                            <span class="match-result-badge ${resClass}">${resLabel}</span>
                        </div>

                        <div class="battle-arena-1v1">
                            <div class="combatant-side">
                                <div class="combatant-avatar-wrap">
                                    <img src="${escapeHtml(sAvatar)}" class="combatant-avatar ${res === 'WIN' ? 'winner-glow' : ''}" onerror="this.src='/static/avatar_default.png'">
                                    ${res === 'WIN' ? '<span class="rank-crown">👑</span>' : ''}
                                </div>
                                <div class="combatant-name">${escapeHtml(sName)}</div>
                                <div class="combatant-score" style="color: #38bdf8;">${formatNumber(sScore)}</div>
                            </div>

                            <div class="vs-center-mobile">
                                <span class="vs-text">VS</span>
                            </div>

                            <div class="combatant-side">
                                <div class="combatant-avatar-wrap">
                                    <img src="${escapeHtml(oAvatar)}" class="combatant-avatar ${res === 'LOSE' ? 'winner-glow' : ''}" onerror="this.src='/static/avatar_default.png'">
                                    ${res === 'LOSE' ? '<span class="rank-crown">👑</span>' : ''}
                                </div>
                                <div class="combatant-name">${escapeHtml(oName)}</div>
                                <div class="combatant-score" style="color: #ef4444;">${formatNumber(oScore)}</div>
                            </div>
                        </div>

                        <div class="duel-score-bar-mobile">
                            <div class="bar-side-left" style="width: ${sPct}%;"></div>
                            <div class="bar-side-right" style="width: ${oPct}%;"></div>
                        </div>

                        <div class="battle-card-footer">
                            <span>${escapeHtml(relTime)}</span>
                            <span class="battle-earnings-badge">${formatCurrency(b.eur_net, "EUR")} (${formatNumber(b.coins_earned)} Coins)</span>
                        </div>
                    </div>
                `;
            }
        });

        elements.recentBattlesContainer.innerHTML = html;
    }

    function renderStreamers(streamers) {
        if (!elements.streamersContainer) return;
        if (!streamers || streamers.length === 0) {
            elements.streamersContainer.innerHTML = `
                <div class="ma-empty-state">
                    <div class="ma-empty-icon">👥</div>
                    <div>Keine Streamer in der Datenbank hinterlegt.</div>
                </div>
            `;
            return;
        }

        let html = "";
        streamers.forEach(s => {
            const avatar = s.avatar_url || "/static/avatar_default.png";
            const name = s.display_name || s.username;
            const viewers = s.is_live ? `${formatNumber(s.current_viewers)} Zuschauer` : "Offline";

            html += `
                <div class="streamer-card-mobile ${s.is_live ? 'is-live' : ''}">
                    <div class="streamer-info-left">
                        <div class="streamer-avatar-box">
                            <img src="${escapeHtml(avatar)}" class="streamer-avatar-ma" onerror="this.src='/static/avatar_default.png'">
                            ${s.is_live ? '<span class="streamer-live-dot"></span>' : ''}
                        </div>
                        <div class="streamer-text-col">
                            <div class="streamer-title">${escapeHtml(name)}</div>
                            <div class="streamer-subtitle">@${escapeHtml(s.username)} · ${viewers}</div>
                        </div>
                    </div>

                    <div class="streamer-financials-right">
                        <div class="streamer-eur-net">${formatCurrency(s.eur_net, "EUR")}</div>
                        <div class="streamer-coins">${formatNumber(s.total_coins)} Coins</div>
                    </div>
                </div>
            `;
        });

        elements.streamersContainer.innerHTML = html;
    }

    function renderGifts(gifts) {
        if (!elements.giftsContainer) return;
        if (!gifts || gifts.length === 0) {
            elements.giftsContainer.innerHTML = `
                <div class="ma-empty-state">
                    <div class="ma-empty-icon">🎁</div>
                    <div>Keine Geschenke im ausgewählten Zeitraum erfasst.</div>
                </div>
            `;
            return;
        }

        let html = "";
        gifts.forEach(g => {
            const relTime = getRelativeTime(g.created_at);
            const count = g.repeat_count > 1 ? `${g.repeat_count}x ` : "";

            html += `
                <div class="gift-item-mobile">
                    <div class="gift-sender-info">
                        <div class="gift-icon-placeholder">🎁</div>
                        <div class="gift-text-wrap">
                            <div class="gift-sender-name">${escapeHtml(g.sender_username || 'Anonym')}</div>
                            <div class="gift-target-streamer">${count}${escapeHtml(g.gift_name)} → @${escapeHtml(g.streamer_username)} · ${relTime}</div>
                        </div>
                    </div>
                    <div class="gift-coins-val">+${formatNumber(g.total_coins)} Coins</div>
                </div>
            `;
        });

        elements.giftsContainer.innerHTML = html;
    }

    // Event Handlers: Period Pills
    elements.periodPills.forEach(pill => {
        pill.addEventListener("click", () => {
            const period = pill.getAttribute("data-period");
            if (period === currentPeriod) return;
            triggerHaptic("light");
            currentPeriod = period;
            elements.periodPills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            fetchMiniAppData();
        });
    });

    // Event Handlers: Bottom Navigation Tabs
    elements.navTabs.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");
            if (targetTab === activeTab) return;
            triggerHaptic("medium");
            activeTab = targetTab;

            elements.navTabs.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            elements.tabViews.forEach(view => {
                if (view.id === targetTab) {
                    view.classList.add("active");
                } else {
                    view.classList.remove("active");
                }
            });
        });
    });

    // Refresh Button Click
    if (elements.btnRefresh) {
        elements.btnRefresh.addEventListener("click", () => {
            triggerHaptic("light");
            fetchMiniAppData();
        });
    }

    // Auto-polling every 8 seconds for live match updates
    fetchMiniAppData();
    pollInterval = setInterval(fetchMiniAppData, 8000);

    // Visibility change handling
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            if (pollInterval) clearInterval(pollInterval);
        } else {
            fetchMiniAppData();
            pollInterval = setInterval(fetchMiniAppData, 8000);
        }
    });

})();
