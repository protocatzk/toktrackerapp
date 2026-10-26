import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

try:
    from telegram import (
        BotCommand,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeDefault,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
        WebAppInfo,
    )
    from telegram.constants import ChatType, ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    PTB_AVAILABLE = True
except ImportError:
    PTB_AVAILABLE = False
    Update = Any  # type: ignore
    ContextTypes = Any  # type: ignore
    InlineKeyboardButton = Any  # type: ignore
    InlineKeyboardMarkup = Any  # type: ignore
    WebAppInfo = Any  # type: ignore

from config import BASE_DIR, config
from database import (
    build_streamer_bot_report,
    check_telegram_user_access,
    get_all_streamers_summary,
    get_dashboard_overview,
    get_db,
    get_donor_details,
    get_donor_radar_data,
    get_network_loyalty_and_overlap,
    get_recent_whale_gifts,
    get_settings,
    get_streamer_by_id_or_username,
    get_telegram_access_mode,
    list_telegram_users,
    save_donor_crm,
    set_telegram_access_mode,
    toggle_donor_vip,
    upsert_streamer,
    upsert_telegram_user_permission,
)
from tiktok_service import fetch_tiktok_user_info, monitor_manager

PICTURES_DIR = os.path.join(BASE_DIR, "pictures")
WELCOME_BANNER_PATH = os.path.join(PICTURES_DIR, "welcome_banner.jpg")
DAILY_REPORT_BANNER_PATH = os.path.join(PICTURES_DIR, "daily_report_banner.jpg")
BOT_AVATAR_PATH = os.path.join(PICTURES_DIR, "bot_avatar.jpg")

logger = logging.getLogger("telegram_service")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class AdminAuthManager:
    """
    Verwaltet Telegram-basierte Admin-Logins (6-stelliger Einmal-Code / OTP sowie
    1-Klick-Freigabe per Telegram Inline-Button) und signierte Admin-Session-Cookies.
    """

    COOKIE_NAME = "toktracker_admin_session"
    CHALLENGE_TTL_SECONDS = 600  # 10 Minuten Gültigkeit für Einmal-Codes
    SESSION_TTL_SECONDS = 86400 * 7  # 7 Tage Session-Dauer

    def __init__(self) -> None:
        self._challenges: Dict[str, Dict[str, Any]] = {}

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            cid
            for cid, ch in self._challenges.items()
            if ch.get("expires_at", 0) < now
        ]
        for cid in expired:
            self._challenges.pop(cid, None)

    def create_challenge(
        self,
        client_info: str = "Web-Browser",
        admin_label: str = "telegram_admin",
    ) -> Dict[str, Any]:
        self._cleanup_expired()
        challenge_id = secrets.token_hex(16)
        code = f"{secrets.randbelow(900000) + 100000}"
        now = time.time()
        challenge = {
            "challenge_id": challenge_id,
            "code": code,
            "created_at": now,
            "expires_at": now + self.CHALLENGE_TTL_SECONDS,
            "status": "pending",  # pending | approved | rejected | consumed
            "admin_label": admin_label,
            "client_info": client_info,
        }
        self._challenges[challenge_id] = challenge
        return challenge

    def get_latest_pending_challenge(self) -> Optional[Dict[str, Any]]:
        self._cleanup_expired()
        pending = [
            ch
            for ch in self._challenges.values()
            if ch.get("status") == "pending"
        ]
        if not pending:
            return None
        pending.sort(key=lambda c: c.get("created_at", 0), reverse=True)
        return pending[0]

    def approve_challenge(self, challenge_id: str, admin_label: str) -> Optional[Dict[str, Any]]:
        self._cleanup_expired()
        ch = self._challenges.get(challenge_id)
        if not ch or ch.get("status") not in ("pending", "approved"):
            return None
        ch["status"] = "approved"
        ch["admin_label"] = admin_label
        return ch

    def reject_challenge(self, challenge_id: str, admin_label: str) -> Optional[Dict[str, Any]]:
        self._cleanup_expired()
        ch = self._challenges.get(challenge_id)
        if not ch:
            return None
        ch["status"] = "rejected"
        ch["admin_label"] = admin_label
        return ch

    def get_recent_challenges(self, limit: int = 25) -> List[Dict[str, Any]]:
        self._cleanup_expired()
        items = list(self._challenges.values())
        items.sort(key=lambda c: c.get("created_at", 0), reverse=True)
        return items[:limit]

    def poll_challenge(self, challenge_id: str) -> Dict[str, Any]:
        self._cleanup_expired()
        ch = self._challenges.get(challenge_id)
        if not ch:
            return {"status": "expired"}
        status = ch.get("status", "pending")
        if status == "approved":
            ch["status"] = "consumed"
            return {
                "status": "approved",
                "admin_label": ch.get("admin_label") or "telegram_admin",
            }
        return {"status": status}

    def verify_code(self, code: str, challenge_id: Optional[str] = None) -> Optional[str]:
        """
        Prüft einen 6-stelligen Einmal-Code (oder optionales Admin-Notfallpasswort aus .env).
        Gibt bei Erfolg das `admin_label` zurück, sonst `None`.
        """
        self._cleanup_expired()
        clean_code = (code or "").strip().replace(" ", "")
        if not clean_code:
            return None

        # Optionaler Fallback auf ADMIN_PASSWORD aus .env, falls vom Betreiber gesetzt
        if config.admin_password and hmac.compare_digest(clean_code, config.admin_password):
            return "env_admin"

        if challenge_id and challenge_id in self._challenges:
            ch = self._challenges[challenge_id]
            if ch.get("status") in ("pending", "approved") and hmac.compare_digest(
                str(ch.get("code", "")), clean_code
            ):
                ch["status"] = "consumed"
                return str(ch.get("admin_label") or "telegram_admin")

        # Suche über alle aktiven Challenges (z. B. wenn der Code direkt per /login im Bot erzeugt wurde)
        for ch in list(self._challenges.values()):
            if ch.get("status") in ("pending", "approved") and hmac.compare_digest(
                str(ch.get("code", "")), clean_code
            ):
                ch["status"] = "consumed"
                return str(ch.get("admin_label") or "telegram_admin")

        return None

    def verify_telegram_auth(self, auth_data: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validiert die Authentifizierungsdaten des offiziellen Telegram Login Widgets (OAuth flow).
        Telegram-Spezifikation:
        1. secret_key = SHA256(bot_token)
        2. data_check_string = alphabetisch sortierte 'k=v\n...' Paare aller Attribute außer 'hash'
        3. HMAC-SHA256(data_check_string, secret_key) == received_hash
        4. Replay Protection: auth_date nicht älter als 86400s (24h)

        Rückgabe: (is_valid, error_reason, telegram_user_identifier)
        """
        received_hash = auth_data.get("hash")
        if not received_hash or not isinstance(received_hash, str):
            return False, "Fehlende Signatur (hash)", None

        try:
            auth_date = int(auth_data.get("auth_date", 0))
        except (ValueError, TypeError):
            return False, "Ungültiges auth_date Format", None

        now = int(time.time())
        # Replay-Schutz: max. 24h alt und max. 5 Min in der Zukunft
        if (now - auth_date) > 86400 or (auth_date - now) > 300:
            return False, "Login-Sitzung abgelaufen (älter als 24 Stunden)", None

        token = (config.telegram_bot_token or "").strip()
        if not token:
            return False, "Telegram Bot-Token nicht konfiguriert", None

        secret_key = hashlib.sha256(token.encode("utf-8")).digest()

        check_items = []
        for key in sorted(auth_data.keys()):
            if key == "hash":
                continue
            val = auth_data[key]
            if val is not None and val != "":
                check_items.append(f"{key}={val}")
        data_check_string = "\n".join(check_items)

        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            return False, "Ungültige kryptografische Signatur", None

        telegram_id = auth_data.get("id")
        username = (auth_data.get("username") or "").strip().lstrip("@")
        user_identifier = f"@{username}" if username else f"id:{telegram_id}"
        return True, None, user_identifier

    def create_session_token(
        self,
        admin_label: str = "telegram_admin",
        role: str = "admin",
        is_allowed: bool = True,
    ) -> str:
        payload = {
            "sub": admin_label,
            "role": role,
            "is_allowed": is_allowed,
            "iat": int(time.time()),
            "exp": int(time.time()) + self.SESSION_TTL_SECONDS,
            "nonce": secrets.token_hex(8),
        }
        raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        b64_payload = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
        sig = hmac.new(
            config.session_secret.encode("utf-8"),
            b64_payload.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{b64_payload}.{sig}"

    def verify_session_token(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token or "." not in token:
            return None
        try:
            b64_payload, sig = token.rsplit(".", 1)
            expected_sig = hmac.new(
                config.session_secret.encode("utf-8"),
                b64_payload.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return None
            padding = "=" * (-len(b64_payload) % 4)
            raw_bytes = base64.urlsafe_b64decode(b64_payload + padding)
            payload = json.loads(raw_bytes.decode("utf-8"))
            if int(payload.get("exp", 0)) < int(time.time()):
                return None
            payload.setdefault("role", "admin")
            payload.setdefault("is_allowed", True)
            return payload
        except Exception:
            return None

    def validate_webapp_init_data(self, init_data: str) -> Optional[Dict[str, Any]]:
        """
        Validiert die von der Telegram WebApp übergebene initData-Zeichenkette
        kryptografisch nach der offiziellen Telegram-Spezifikation (HMAC-SHA256).
        """
        if not init_data or not config.telegram_bot_token:
            return None
        try:
            parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
            received_hash = parsed.pop("hash", None)
            if not received_hash:
                return None

            data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
            secret_key = hmac.new(
                b"WebAppData",
                config.telegram_bot_token.encode("utf-8"),
                hashlib.sha256,
            ).digest()
            expected_hash = hmac.new(
                secret_key,
                data_check_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            if not hmac.compare_digest(received_hash, expected_hash):
                return None

            user_raw = parsed.get("user")
            user_data = json.loads(user_raw) if user_raw else {}
            auth_date = int(parsed.get("auth_date", 0))

            return {
                "user": user_data,
                "auth_date": auth_date,
                "query_id": parsed.get("query_id"),
                "chat_type": parsed.get("chat_type"),
            }
        except Exception:
            return None

    def is_admin_user(self, telegram_id: Optional[int] = None, username: Optional[str] = None) -> bool:
        """
        Prüft, ob ein Telegram-Nutzer Administrator-Rechte besitzt
        (über .env-Whitelist oder 'role=admin' in der telegram_users-Tabelle).
        """
        clean_uname = (username or "").strip().lstrip("@").lower()

        if telegram_id is not None:
            try:
                tid = int(telegram_id)
                if tid in config.env_allowed_telegram_ids:
                    return True
            except (ValueError, TypeError):
                pass

        if clean_uname and clean_uname in config.env_allowed_telegram_usernames:
            return True

        try:
            users = list_telegram_users()
            for u in users:
                u_tid = u.get("telegram_id")
                u_uname = str(u.get("username") or "").strip().lstrip("@").lower()
                is_allowed = int(u.get("is_allowed", 0)) == 1
                is_admin = u.get("role") == "admin"

                if is_allowed and is_admin:
                    if telegram_id is not None and u_tid:
                        try:
                            if int(u_tid) == int(telegram_id):
                                return True
                        except (ValueError, TypeError):
                            pass
                    if clean_uname and u_uname and clean_uname == u_uname:
                        return True
        except Exception:
            pass

        return False


admin_auth_manager = AdminAuthManager()


def generate_full_report_data(hours: int = 24, only_active: bool = False) -> Dict[str, Any]:
    """
    Erzeugt den vollständigen Bericht für alle Streamer (wird sowohl von der FastAPI-Route
    als auch vom Telegram-Bot für den 24h-Job genutzt).
    """
    cfg = get_settings()
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM streamers ORDER BY username ASC").fetchall()

    cards: List[Dict[str, Any]] = []
    for r in rows:
        s_dict = dict(r)
        uname = s_dict["username"]
        is_tracking = monitor_manager.is_tracking(uname)
        diag = monitor_manager.get_worker_diagnostics(uname)
        worker_state = (
            diag["state"]
            if diag
            else ("live" if s_dict.get("is_live") else "stopped")
        )
        card = build_streamer_bot_report(
            streamer_row=s_dict,
            hours=hours,
            is_tracking_active=is_tracking,
            worker_state=worker_state,
        )
        if only_active and card["period_coins"] == 0 and not card["is_live"]:
            continue
        cards.append(card)

    cards.sort(key=lambda c: (c["period_coins"], c["alltime_coins"]), reverse=True)

    total_period_coins = sum(c["period_coins"] for c in cards)
    total_period_usd_net = round(sum(c["period_usd_net"] for c in cards), 2)
    total_period_eur_net = round(sum(c["period_eur_net"] for c in cards), 2)
    total_period_usd_gross = round(sum(c["period_usd_gross"] for c in cards), 2)
    total_period_eur_gross = round(sum(c["period_eur_gross"] for c in cards), 2)
    total_gifts_sent = sum(c["period_gifts_sent"] for c in cards)
    live_now = sum(1 for c in cards if c["is_live"])

    md_lines = [
        f"📋 *toktracker – Streamer Einnahmen-Report ({hours}h)*",
        f"🕒 Stand: `{now.strftime('%d.%m.%Y %H:%M')}`",
        f"💰 *Gesamt Netto ({hours}h):* `{total_period_eur_net:,.2f} €` (`${total_period_usd_net:,.2f}`)",
        f"🪙 *Gesamt Coins ({hours}h):* `{total_period_coins:,}` ({total_gifts_sent:,} Gifts)",
        f"📡 *Streamer:* {len(cards)} überwacht ({live_now} aktuell LIVE)",
        "────────────────────────",
    ]
    html_lines = [
        f"<b>📋 toktracker – Streamer Einnahmen-Report ({hours}h)</b>",
        f"🕒 Stand: <code>{now.strftime('%d.%m.%Y %H:%M')}</code>",
        f"💰 <b>Gesamt Netto ({hours}h):</b> <code>{total_period_eur_net:,.2f} €</code> (<code>${total_period_usd_net:,.2f}</code>)",
        f"🪙 <b>Gesamt Coins ({hours}h):</b> <code>{total_period_coins:,}</code> ({total_gifts_sent:,} Gifts)",
        f"📡 <b>Streamer:</b> {len(cards)} überwacht ({live_now} aktuell LIVE)",
        "────────────────────────",
    ]

    for idx, c in enumerate(cards, start=1):
        status_dot = "🟢" if c["is_live"] else "⚪"
        top_g = (
            f"{c['top_gifts'][0]['gift_name']} ({c['top_gifts'][0]['total_coins']:,})"
            if c["top_gifts"]
            else "-"
        )
        top_u = (
            f"@{c['top_gifters'][0]['sender_username']} ({c['top_gifters'][0]['total_coins']:,})"
            if c["top_gifters"]
            else "-"
        )

        md_lines.append(
            f"*{idx}. {c['display_name']}* (@{c['username']}) {status_dot}\n"
            f"   • {hours}h Netto: *{c['period_eur_net']:,.2f} €* (`${c['period_usd_net']:,.2f}`) | `{c['period_coins']:,}` Coins\n"
            f"   • Top Gift: {top_g} | Top Gifter: {top_u}\n"
            f"   • All-Time: {c['alltime_eur_net']:,.2f} € ({c['alltime_coins']:,} Coins)"
        )
        html_lines.append(
            f"<b>{idx}. {html.escape(str(c['display_name']))}</b> (@{html.escape(str(c['username']))}) {status_dot}\n"
            f"   • {hours}h Netto: <b>{c['period_eur_net']:,.2f} €</b> (<code>${c['period_usd_net']:,.2f}</code>) | <code>{c['period_coins']:,}</code> Coins\n"
            f"   • Top Gift: {html.escape(top_g)} | Top Gifter: {html.escape(top_u)}\n"
            f"   • All-Time: {c['alltime_eur_net']:,.2f} € ({c['alltime_coins']:,} Coins)"
        )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "period_hours": hours,
        "cutoff_timestamp": cutoff,
        "currency_primary": cfg["currency_primary"],
        "totals": {
            "streamers_count": len(cards),
            "live_count": live_now,
            "period_coins": total_period_coins,
            "period_gifts_sent": total_gifts_sent,
            "period_usd_net": total_period_usd_net,
            "period_eur_net": total_period_eur_net,
            "period_usd_gross": total_period_usd_gross,
            "period_eur_gross": total_period_eur_gross,
        },
        "streamers": cards,
        "telegram_markdown": "\n\n".join(md_lines),
        "telegram_html": "\n\n".join(html_lines),
    }


class TelegramBotService:
    """
    Vollständig entkoppelter Telegram-Bot-Service auf Basis des offiziellen
    `python-telegram-bot` (v22+) SDKs inkl. `JobQueue` für den automatischen 24h-Bericht,
    Gruppen-Support (`/befehl@BotName` & `@BotName befehl`), mehrstufiger
    Zugriffskontrolle (`whitelist` per `[@]username` oder `id` vs. `public`) sowie
    Telegram-basierter Admin-Panel-Authentifizierung (`/login` & 1-Klick-Freigabe).
    """

    def __init__(self) -> None:
        self._app: Optional[Any] = None
        self._state: str = "disabled"
        self._last_error: str = ""
        self._bot_username: str = ""
        self._bot_username_raw: str = ""
        self._started_at: Optional[str] = None
        self._last_report_sent_at: Optional[str] = None
        self._last_disk_alert_time: float = 0.0
        self._disk_alert_active: bool = False
        self._logs: Deque[Dict[str, str]] = deque(maxlen=40)

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        self._logs.appendleft({"timestamp": ts, "level": level, "message": msg})
        if level == "ERROR":
            logger.error("%s", msg)
        else:
            logger.info("%s", msg)

    def get_diagnostics(self) -> Dict[str, Any]:
        try:
            mode = get_telegram_access_mode()
            users = list_telegram_users()
            allowed_count = sum(1 for u in users if int(u.get("is_allowed", 0)) == 1)
            pending_count = sum(1 for u in users if int(u.get("is_allowed", 0)) == 0)
        except Exception:
            mode = config.telegram_access_mode
            allowed_count = 0
            pending_count = 0

        return {
            "sdk_available": PTB_AVAILABLE,
            "state": self._state,
            "bot_username": self._bot_username,
            "has_token": bool(config.telegram_bot_token),
            "default_chat_id": config.telegram_chat_id,
            "access_mode": mode,
            "group_support": True,
            "allowed_users_count": allowed_count,
            "pending_users_count": pending_count,
            "started_at": self._started_at,
            "last_report_sent_at": self._last_report_sent_at,
            "last_error": self._last_error,
            "pictures": {
                "welcome_banner": os.path.exists(WELCOME_BANNER_PATH),
                "daily_report_banner": os.path.exists(DAILY_REPORT_BANNER_PATH),
                "bot_avatar": os.path.exists(BOT_AVATAR_PATH),
            },
            "logs": list(self._logs)[:15],
        }

    async def start(self) -> None:
        if not PTB_AVAILABLE:
            self._state = "disabled"
            self._log("INFO", "python-telegram-bot SDK nicht installiert – Bot deaktiviert.")
            return

        token = (config.telegram_bot_token or "").strip()
        if not token or token.startswith("123456789:"):
            self._state = "disabled"
            self._log(
                "INFO",
                "Kein TELEGRAM_BOT_TOKEN in .env hinterlegt – Bot im Standby (API & Web laufen normal).",
            )
            return

        self._state = "starting"
        self._last_error = ""
        try:
            builder = Application.builder().token(token)
            self._app = builder.build()

            # Lese- & Analyse-Befehle
            self._app.add_handler(CommandHandler(["start", "help"], self._cmd_help))
            self._app.add_handler(CommandHandler(["app", "miniapp"], self._cmd_miniapp))
            self._app.add_handler(CommandHandler(["id", "whoami"], self._cmd_whoami))
            self._app.add_handler(CommandHandler(["overview", "dashboard", "finanzen"], self._cmd_overview))
            self._app.add_handler(CommandHandler(["radar", "whales_radar"], self._cmd_radar))
            self._app.add_handler(CommandHandler(["whales", "whale_alerts", "alerts"], self._cmd_whales))
            self._app.add_handler(CommandHandler(["donor", "dossier", "spender"], self._cmd_donor))
            self._app.add_handler(CommandHandler(["vip", "vips"], self._cmd_vip))
            self._app.add_handler(CommandHandler(["loyalty", "switchers", "frontenwechsler"], self._cmd_loyalty))
            self._app.add_handler(CommandHandler(["streamers", "channels", "liste"], self._cmd_streamers))
            self._app.add_handler(CommandHandler(["gifts", "geschenke", "ticker"], self._cmd_gifts))
            self._app.add_handler(CommandHandler("report", self._cmd_report))
            self._app.add_handler(CommandHandler(["streamer", "stats"], self._cmd_streamer))
            self._app.add_handler(CommandHandler("status", self._cmd_status))

            # Schreib- & Verwaltungs-Befehle (immer nur für Admins)
            self._app.add_handler(CommandHandler("login", self._cmd_admin_login))
            self._app.add_handler(CommandHandler("add", self._cmd_add_streamer))
            self._app.add_handler(CommandHandler("track", self._cmd_track_streamer))
            self._app.add_handler(CommandHandler("stop", self._cmd_stop_streamer))
            self._app.add_handler(CommandHandler("allow", self._cmd_allow_user))
            self._app.add_handler(CommandHandler("revoke", self._cmd_revoke_user))
            self._app.add_handler(CommandHandler("users", self._cmd_list_users))
            self._app.add_handler(CommandHandler("mode", self._cmd_access_mode))

            # Callback-Handler für 1-Klick Admin-Login-Freigabe
            self._app.add_handler(
                CallbackQueryHandler(self._on_login_callback, pattern=r"^admin_login_(approve|reject):")
            )
            # Callback-Handler für interaktive Buttons (Overview, Radar, Whales, Donor VIP, Loyalty etc.)
            self._app.add_handler(
                CallbackQueryHandler(
                    self._on_interactive_callback,
                    pattern=r"^(overview_period|radar_tier|whales_min|donor_vip_toggle|donor_refresh|loyalty_days|streamers_list|gifts_refresh|overview_streamer):"
                )
            )

            # Gruppen-Mention-Handler
            self._app.add_handler(
                MessageHandler(
                    filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
                    self._on_group_mention,
                )
            )

            await self._app.initialize()
            bot_me = await self._app.bot.get_me()
            self._bot_username_raw = (bot_me.username or "").lower()
            self._bot_username = f"@{bot_me.username}" if bot_me.username else str(bot_me.id)

            await self._register_bot_commands()

            await self._app.start()
            if self._app.updater:
                await self._app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )

            if self._app.job_queue and config.telegram_chat_id:
                self._app.job_queue.run_repeating(
                    self._scheduled_24h_report_job,
                    interval=timedelta(hours=24),
                    first=timedelta(hours=24),
                    name="daily_24h_tiktok_report",
                )
                self._log(
                    "INFO",
                    f"24h-Automatik-Bericht für Chat-ID {config.telegram_chat_id} geplant.",
                )

            if self._app.job_queue:
                self._app.job_queue.run_repeating(
                    self._scheduled_disk_health_monitor_job,
                    interval=timedelta(minutes=15),
                    first=timedelta(seconds=45),
                    name="disk_health_monitor",
                )
                self._log(
                    "INFO",
                    "Speicherplatz-Überwachung aktiv (Intervall: alle 15 Min, Alarm bei >=85%).",
                )

            self._state = "running"
            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._log(
                "INFO",
                f"toktracker Telegram-Bot ({self._bot_username}) gestartet [Modus: {get_telegram_access_mode().upper()}, Admin-Auth: AKTIV].",
            )

        except Exception as exc:
            self._state = "error"
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log(
                "ERROR",
                f"Telegram-Bot konnte nicht gestartet werden ({self._last_error}). FastAPI & Web laufen weiter.",
            )
            await self._safe_cleanup()

    async def _register_bot_commands(self) -> None:
        if not self._app:
            return
        commands = [
            BotCommand("start", "Hauptmenü & Befehlsübersicht öffnen"),
            BotCommand("overview", "Finanz- & Performance-Dashboard (Heute, 24h, 7d)"),
            BotCommand("radar", "Spender-Radar & Rangliste nach Tiers (Whales, Sharks)"),
            BotCommand("whales", "Live-Stream der neuesten Großspenden"),
            BotCommand("donor", "Spender-Dossier, Stammkanal & VIP (<username>)"),
            BotCommand("vip", "VIP-Watchlist anzeigen oder Spender markieren"),
            BotCommand("loyalty", "Frontenwechsler & Streamer-Überschneidungen"),
            BotCommand("streamers", "Alle Kanäle mit Live-Status (🟢/⚪) & Zuschauern"),
            BotCommand("gifts", "Live Gift-Ticker der letzten Geschenke"),
            BotCommand("streamer", "Einzelabfrage für einen Streamer (<username>)"),
            BotCommand("report", "24h-Gesamtbericht aller überwachten Streamer"),
            BotCommand("status", "Laufzeit-Status aller aktiven TikTokLive-Worker"),
            BotCommand("app", "📱 toktracker Mini-App (Live-Matches & Einnahmen)"),
            BotCommand("id", "Eigene Telegram-ID, Gruppen-ID & Berechtigung"),
            BotCommand("login", "[Admin] Einmal-Code für das Web-Admin-Panel erzeugen"),
            BotCommand("add", "[Admin] Streamer hinzufügen & Live-Worker starten"),
            BotCommand("track", "[Admin] Live-Überwachung für einen Streamer starten"),
            BotCommand("stop", "[Admin] Live-Überwachung für einen Streamer stoppen"),
            BotCommand("allow", "[Admin] Nutzer per ID, @username oder Reply freischalten"),
            BotCommand("revoke", "[Admin] Nutzer-Berechtigung entziehen / sperren"),
            BotCommand("users", "[Admin] Freigeschaltete Nutzer & offene Anfragen auflisten"),
            BotCommand("mode", "[Admin] Zugriffsmodus wechseln (whitelist oder public)"),
        ]
        try:
            await self._app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
            await self._app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
            await self._app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        except Exception as exc:
            self._log("INFO", f"Hinweis bei set_my_commands: {exc}")

    async def stop(self) -> None:
        if not self._app:
            return
        self._log("INFO", "Stoppe Telegram-Bot...")
        await self._safe_cleanup()
        self._state = "stopped"

    async def _safe_cleanup(self) -> None:
        if not self._app:
            return
        try:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
        except Exception:
            pass
        try:
            if self._app.running:
                await self._app.stop()
        except Exception:
            pass
        try:
            await self._app.shutdown()
        except Exception:
            pass
        self._app = None

    # =================================================================
    # TELEGRAM ADMIN-PANEL AUTHENTIFIZIERUNG (OTP + 1-KLICK FREIGABE)
    # =================================================================

    def _resolve_admin_chat_ids(self, identifier_hint: Optional[str] = None) -> List[int]:
        """
        Ermittelt alle bekannten privaten Telegram-Chat-IDs von berechtigten Administratoren.
        """
        users = list_telegram_users()
        hint = (identifier_hint or "").strip().lstrip("@").lower()
        matched_ids: Set[int] = set()
        all_admin_ids: Set[int] = set()

        for cid in config.env_allowed_telegram_ids:
            if cid > 0:
                all_admin_ids.add(int(cid))
                if hint and str(cid) == hint:
                    matched_ids.add(int(cid))

        for u in users:
            if int(u.get("is_allowed", 0)) == 1 and u.get("role") == "admin":
                tg_id = u.get("telegram_id")
                uname = str(u.get("username") or "").strip().lstrip("@").lower()
                if tg_id and int(tg_id) > 0:
                    all_admin_ids.add(int(tg_id))
                    if hint and (hint == uname or hint == str(tg_id)):
                        matched_ids.add(int(tg_id))

        if hint and matched_ids:
            return sorted(matched_ids)
        return sorted(all_admin_ids)

    async def send_admin_login_challenge(
        self,
        identifier_hint: Optional[str] = None,
        client_info: str = "Web-Browser",
    ) -> Dict[str, Any]:
        """
        Erzeugt eine Login-Challenge und sendet den 6-stelligen Einmal-Code sowie
        einen Inline-Freigabe-Button an die hinterlegten Admin-Telegram-Chats.
        """
        challenge = admin_auth_manager.create_challenge(client_info=client_info)
        cid = challenge["challenge_id"]
        code = challenge["code"]

        if self._state != "running" or not self._app:
            self._log(
                "INFO",
                f"[Admin-Login] Lokaler Einmal-Code erzeugt: {code} (Telegram-Bot Status: {self._state})",
            )
            return {
                "ok": True,
                "challenge_id": cid,
                "delivered_count": 0,
                "bot_running": False,
                "bot_username": self._bot_username,
                "expires_in": AdminAuthManager.CHALLENGE_TTL_SECONDS,
                "message": (
                    "Telegram-Bot ist aktuell nicht verbunden. "
                    "Der Einmal-Code wurde im Server-Log ausgegeben (oder nutze das Admin-Passwort aus der .env)."
                ),
            }

        target_ids = await asyncio.to_thread(self._resolve_admin_chat_ids, identifier_hint)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Login im Browser freigeben",
                        callback_data=f"admin_login_approve:{cid}",
                    ),
                    InlineKeyboardButton(
                        "❌ Ablehnen",
                        callback_data=f"admin_login_reject:{cid}",
                    ),
                ]
            ]
        )

        msg_html = (
            "<b>🔐 toktracker · Admin-Panel Login-Anfrage</b>\n\n"
            f"• <b>Einmal-Code (OTP):</b> <code>{code}</code>\n"
            f"• <b>Anfrage:</b> <code>{html.escape(client_info)}</code>\n"
            "• <b>Gültigkeit:</b> 10 Minuten\n\n"
            "<i>Gib den 6-stelligen Code im Web-Browser ein oder bestätige den Login direkt per Klick:</i>"
        )

        delivered = 0
        for chat_id in target_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=msg_html,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                delivered += 1
            except Exception as exc:
                self._log("INFO", f"Login-Code konnte nicht an Admin-Chat {chat_id} gesendet werden: {exc}")

        if delivered > 0:
            self._log("INFO", f"Admin-Login-Code an {delivered} Admin-Chat(s) gesendet.")
            return {
                "ok": True,
                "challenge_id": cid,
                "delivered_count": delivered,
                "bot_running": True,
                "bot_username": self._bot_username,
                "expires_in": AdminAuthManager.CHALLENGE_TTL_SECONDS,
                "message": f"Einmal-Code & Freigabe-Anfrage an {delivered} Admin-Chat(s) gesendet.",
            }

        return {
            "ok": True,
            "challenge_id": cid,
            "delivered_count": 0,
            "bot_running": True,
            "bot_username": self._bot_username,
            "expires_in": AdminAuthManager.CHALLENGE_TTL_SECONDS,
            "message": (
                f"Öffne den Telegram-Bot {self._bot_username or ''} und sende den Befehl /login, "
                "um deinen 6-stelligen Einmal-Code direkt im Chat zu erhalten."
            ),
        }

    async def _cmd_admin_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Erlaubt einem Admin, direkt im Telegram-Chat mit `/login` einen frischen
        6-stelligen Einmal-Code abzurufen oder eine offene Browser-Anfrage freizugeben.
        """
        if not update.effective_message or not update.effective_user:
            return
        access = await self._authorize(update, require_admin=True)
        if not access:
            return

        tg_user = update.effective_user
        admin_label = f"@{tg_user.username}" if tg_user.username else str(tg_user.id)

        pending = admin_auth_manager.get_latest_pending_challenge()
        if pending:
            pending["admin_label"] = admin_label
            challenge = pending
        else:
            challenge = admin_auth_manager.create_challenge(
                client_info=f"Telegram /login ({admin_label})",
                admin_label=admin_label,
            )

        cid = challenge["challenge_id"]
        code = challenge["code"]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Aktuelle Browser-Sitzung sofort freigeben",
                        callback_data=f"admin_login_approve:{cid}",
                    )
                ]
            ]
        )

        await update.effective_message.reply_text(
            "<b>🔐 toktracker · Admin-Panel Einmal-Code</b>\n\n"
            f"Dein 6-stelliger Login-Code lautet:\n"
            f"👉 <code>{code}</code>\n\n"
            "<i>Gib diesen Code unter <code>/login</code> im Browser ein (gültig für 10 Minuten) "
            "oder klicke unten auf Freigeben, falls du die Login-Seite bereits geöffnet hast.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _on_login_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return

        tg_user = update.effective_user
        access = await asyncio.to_thread(
            check_telegram_user_access,
            int(tg_user.id),
            tg_user.username or "",
            tg_user.first_name or "",
            int(update.effective_chat.id) if update.effective_chat else int(tg_user.id),
            "",
        )

        if not access.get("is_admin"):
            await query.answer("⛔ Nur Administratoren dürfen Logins freigeben.", show_alert=True)
            return

        action_part, cid = query.data.split(":", 1)
        admin_label = f"@{tg_user.username}" if tg_user.username else (tg_user.first_name or str(tg_user.id))

        if action_part == "admin_login_approve":
            ch = admin_auth_manager.approve_challenge(cid, admin_label=admin_label)
            if not ch:
                await query.answer("Diese Login-Anfrage ist bereits abgelaufen oder wurde verwendet.", show_alert=True)
                return
            await query.answer("Admin-Login freigegeben!")
            self._log("INFO", f"Admin-Panel Login per 1-Klick freigegeben von {admin_label}.")
            try:
                await query.edit_message_text(
                    f"<b>✅ Admin-Panel Login freigegeben</b>\n\n"
                    f"• <b>Bestätigt durch:</b> <code>{html.escape(admin_label)}</code>\n"
                    f"• <b>Zeitpunkt:</b> <code>{datetime.now().strftime('%H:%M:%S')}</code>\n\n"
                    "<i>Der Browser wird automatisch in die Admin-Konsole weitergeleitet.</i>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            admin_auth_manager.reject_challenge(cid, admin_label=admin_label)
            await query.answer("Login-Anfrage abgelehnt.")
            self._log("INFO", f"Admin-Panel Login abgelehnt von {admin_label}.")
            try:
                await query.edit_message_text(
                    f"<b>❌ Admin-Panel Login-Anfrage abgelehnt</b> von <code>{html.escape(admin_label)}</code>.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    # =================================================================
    # ZUGRIFFSKONTROLLE & AUTORISIERUNG (PRIVATCHAT & GRUPPEN)
    # =================================================================

    @staticmethod
    def _is_group_chat(update: Update) -> bool:
        if not update.effective_chat:
            return False
        return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    async def _authorize(
        self, update: Update, require_admin: bool = False
    ) -> Optional[Dict[str, Any]]:
        if not update.effective_user or not update.effective_message or not update.effective_chat:
            return None

        tg_user = update.effective_user
        tg_chat = update.effective_chat
        access = await asyncio.to_thread(
            check_telegram_user_access,
            int(tg_user.id),
            tg_user.username or "",
            tg_user.first_name or "",
            int(tg_chat.id),
            tg_chat.title or "",
        )

        uname_display = (
            f"@{tg_user.username}"
            if tg_user.username
            else (tg_user.first_name or str(tg_user.id))
        )
        chat_context = (
            f" in Gruppe '{tg_chat.title}' ({tg_chat.id})"
            if self._is_group_chat(update)
            else ""
        )

        if not access["allowed"]:
            self._log(
                "INFO",
                f"Zugriff verweigert für {uname_display} (ID: {tg_user.id}){chat_context} [Modus: {access['mode']}].",
            )
            allow_hint = (
                f"<code>/allow @{html.escape(tg_user.username)}</code> oder <code>/allow {tg_user.id}</code>"
                if tg_user.username
                else f"<code>/allow {tg_user.id}</code>"
            )
            await update.effective_message.reply_text(
                "🔒 <b>toktracker · Zugriff beschränkt (Whitelist aktiv)</b>\n\n"
                "Du bist aktuell noch nicht für diesen Bot freigeschaltet.\n"
                f"• <b>Deine User-ID:</b> <code>{tg_user.id}</code>\n"
                f"• <b>Dein Username:</b> <code>{html.escape(uname_display)}</code>\n\n"
                f"<i>Ein Admin kann dich im Web-Interface, per Reply mit <code>/allow</code> oder mit {allow_hint} freischalten.</i>",
                parse_mode=ParseMode.HTML,
            )
            return None

        if require_admin and not access["is_admin"]:
            self._log(
                "INFO",
                f"Admin-Befehl verweigert für {uname_display} (ID: {tg_user.id}){chat_context}.",
            )
            await update.effective_message.reply_text(
                "⛔ <b>Keine Berechtigung:</b> Dieser Verwaltungsbefehl ist nur für Administratoren freigegeben.",
                parse_mode=ParseMode.HTML,
            )
            return None

        return access

    async def _send_with_optional_banner(
        self,
        bot: Any,
        chat_id: str | int,
        html_text: str,
        banner_path: str,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        if os.path.exists(banner_path):
            with open(banner_path, "rb") as photo_file:
                if len(html_text) <= 1000:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file,
                        caption=html_text,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to_message_id,
                        reply_markup=reply_markup,
                    )
                    return
                else:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file,
                        reply_to_message_id=reply_to_message_id,
                    )

        await bot.send_message(
            chat_id=chat_id,
            text=html_text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

    async def send_report_now(
        self, chat_id: Optional[str] = None, hours: int = 24
    ) -> Dict[str, Any]:
        target_chat = (chat_id or config.telegram_chat_id or "").strip()
        if self._state != "running" or not self._app:
            return {
                "ok": False,
                "error": f"Telegram-Bot läuft aktuell nicht (Status: {self._state}). Bitte TELEGRAM_BOT_TOKEN in .env prüfen.",
            }
        if not target_chat:
            return {
                "ok": False,
                "error": "Keine TELEGRAM_CHAT_ID in .env angegeben.",
            }

        try:
            report = await asyncio.to_thread(generate_full_report_data, hours, False)
            await self._send_with_optional_banner(
                bot=self._app.bot,
                chat_id=target_chat,
                html_text=report["telegram_html"],
                banner_path=DAILY_REPORT_BANNER_PATH,
            )
            self._last_report_sent_at = datetime.now().isoformat(timespec="seconds")
            self._log("INFO", f"{hours}h-Report manuell an Chat {target_chat} gesendet.")
            return {"ok": True, "chat_id": target_chat, "hours": hours}
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"Fehler beim Senden des Reports: {err}")
            return {"ok": False, "error": err}

    async def _scheduled_24h_report_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        target_chat = (config.telegram_chat_id or "").strip()
        if not target_chat:
            return
        try:
            report = await asyncio.to_thread(generate_full_report_data, 24, False)
            await self._send_with_optional_banner(
                bot=context.bot,
                chat_id=target_chat,
                html_text=report["telegram_html"],
                banner_path=DAILY_REPORT_BANNER_PATH,
            )
            self._last_report_sent_at = datetime.now().isoformat(timespec="seconds")
            self._log("INFO", f"Automatischer 24h-Bericht an Chat {target_chat} gesendet.")
        except Exception as exc:
            self._log("ERROR", f"Fehler im 24h-Report-Job: {exc}")

    async def _scheduled_disk_health_monitor_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            from system_service import get_system_health

            health = await asyncio.to_thread(get_system_health, True)
            disk = health.get("host", {}).get("disk", {})
            pct = float(disk.get("percent", 0.0))
            free_gb = float(disk.get("free_gb", 0.0))
            total_gb = float(disk.get("total_gb", 0.0))
            used_gb = float(disk.get("used_gb", 0.0))

            now = time.time()
            if pct >= 85.0:
                if not self._disk_alert_active or (now - self._last_disk_alert_time) > 14400:
                    self._disk_alert_active = True
                    self._last_disk_alert_time = now

                    docker_info = health.get("docker", {})
                    containers = docker_info.get("containers", [])
                    volumes = docker_info.get("volumes", [])

                    cont_lines = []
                    for c in containers:
                        cont_lines.append(
                            f"• <b>{html.escape(c.get('name', ''))}</b>: "
                            f"<code>{c.get('memory_used_mb', 0)} MB</code> RAM "
                            f"({c.get('memory_percent', 0)}%) | "
                            f"<code>{c.get('cpu_percent', 0)}%</code> CPU"
                        )

                    vol_lines = []
                    for v in volumes[:4]:
                        vol_lines.append(
                            f"• <code>{html.escape(v.get('name', ''))}</code>: <b>{v.get('size_mb', 0):.1f} MB</b>"
                        )

                    msg = (
                        "🚨 <b>WARNUNG: Kritischer Speicherplatz auf dem Server!</b>\n\n"
                        f"💾 <b>Festplatte:</b> <code>{used_gb:.1f} GB</code> von <code>{total_gb:.1f} GB</code> (<b>{pct:.1f}%</b> belegt)\n"
                        f"Freier Speicher: <b>{free_gb:.1f} GB</b>\n\n"
                        "🐳 <b>Docker Container:</b>\n"
                        + ("\n".join(cont_lines) if cont_lines else "<i>Keine Container erfasst</i>")
                        + "\n\n"
                        "📦 <b>Wichtige Volumes:</b>\n"
                        + ("\n".join(vol_lines) if vol_lines else "<i>Keine Volumes</i>")
                        + "\n\n"
                        "⚠️ <i>Tipp: Bereinige ungenutzte Docker-Images/Build-Caches mit <code>docker system prune -a</code>.</i>"
                    )

                    admin_ids = self._resolve_admin_chat_ids()
                    if config.telegram_chat_id:
                        try:
                            admin_ids.append(int(config.telegram_chat_id))
                        except Exception:
                            pass
                    for target_id in set(admin_ids):
                        try:
                            await context.bot.send_message(
                                chat_id=target_id,
                                text=msg,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as send_err:
                            self._log("DEBUG", f"Konnte Speicher-Warnung nicht an {target_id} senden: {send_err}")

            elif pct < 78.0 and self._disk_alert_active:
                self._disk_alert_active = False
                recovery_msg = (
                    "✅ <b>Entwarnung: Speicherplatz wieder normalisiert</b>\n\n"
                    f"💾 Festplatte ist jetzt zu <b>{pct:.1f}%</b> belegt (Frei: <b>{free_gb:.1f} GB</b> von <code>{total_gb:.1f} GB</code>)."
                )
                admin_ids = self._resolve_admin_chat_ids()
                if config.telegram_chat_id:
                    try:
                        admin_ids.append(int(config.telegram_chat_id))
                    except Exception:
                        pass
                for target_id in set(admin_ids):
                    try:
                        await context.bot.send_message(
                            chat_id=target_id,
                            text=recovery_msg,
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

        except Exception as exc:
            self._log("DEBUG", f"Fehler im Disk-Health-Job: {exc}")

    # =================================================================
    # GRUPPEN-MENTION HANDLER
    # =================================================================

    async def _on_group_mention(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_message.text:
            return

        text = update.effective_message.text.strip()
        bot_tag = f"@{self._bot_username_raw}" if self._bot_username_raw else ""

        is_mentioned = bool(bot_tag and bot_tag in text.lower())
        is_reply_to_bot = bool(
            update.effective_message.reply_to_message
            and update.effective_message.reply_to_message.from_user
            and self._bot_username_raw
            and (update.effective_message.reply_to_message.from_user.username or "").lower()
            == self._bot_username_raw
        )

        if not is_mentioned and not is_reply_to_bot:
            return

        cleaned = text
        if bot_tag:
            for token in text.split():
                if token.lower().lstrip("/") == bot_tag:
                    cleaned = cleaned.replace(token, "", 1).strip()

        parts = cleaned.split()
        cmd = parts[0].lower().lstrip("/") if parts else "help"
        context.args = parts[1:] if len(parts) > 1 else []

        if cmd in ("report", "bericht"):
            await self._cmd_report(update, context)
        elif cmd in ("overview", "dashboard", "finanzen"):
            await self._cmd_overview(update, context)
        elif cmd in ("radar", "whales_radar"):
            await self._cmd_radar(update, context)
        elif cmd in ("whales", "whale_alerts", "alerts"):
            await self._cmd_whales(update, context)
        elif cmd in ("donor", "dossier", "spender"):
            await self._cmd_donor(update, context)
        elif cmd in ("vip", "vips"):
            await self._cmd_vip(update, context)
        elif cmd in ("loyalty", "switchers", "frontenwechsler"):
            await self._cmd_loyalty(update, context)
        elif cmd in ("streamers", "channels", "liste"):
            await self._cmd_streamers(update, context)
        elif cmd in ("gifts", "geschenke", "ticker"):
            await self._cmd_gifts(update, context)
        elif cmd in ("streamer", "stats", "info"):
            await self._cmd_streamer(update, context)
        elif cmd in ("status", "worker", "workers"):
            await self._cmd_status(update, context)
        elif cmd in ("id", "whoami"):
            await self._cmd_whoami(update, context)
        elif cmd == "login":
            await self._cmd_admin_login(update, context)
        elif cmd == "allow":
            await self._cmd_allow_user(update, context)
        elif cmd == "revoke":
            await self._cmd_revoke_user(update, context)
        elif cmd == "users":
            await self._cmd_list_users(update, context)
        elif cmd == "mode":
            await self._cmd_access_mode(update, context)
        else:
            await self._cmd_help(update, context)

    # =================================================================
    # TELEGRAM BOT COMMAND HANDLERS
    # =================================================================

    async def _cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message or not update.effective_chat:
            return
        tg_user = update.effective_user
        tg_chat = update.effective_chat
        access = await asyncio.to_thread(
            check_telegram_user_access,
            int(tg_user.id),
            tg_user.username or "",
            tg_user.first_name or "",
            int(tg_chat.id),
            tg_chat.title or "",
        )
        role_label = (
            "Admin"
            if access["is_admin"]
            else ("Freigeschaltet" if access["allowed"] else "Gesperrt / Wartend")
        )
        group_line = ""
        if self._is_group_chat(update):
            group_line = (
                f"• <b>Gruppen-Name:</b> <code>{html.escape(tg_chat.title or '-')}</code>\n"
                f"• <b>Gruppen Chat-ID:</b> <code>{tg_chat.id}</code> <i>(für 24h-Report in Gruppe)</i>\n"
            )

        await update.effective_message.reply_text(
            f"👤 <b>toktracker · Telegram-Profil &amp; Chat-Info</b>\n"
            f"• <b>Deine User-ID:</b> <code>{tg_user.id}</code>\n"
            f"• <b>Dein Username:</b> <code>@{html.escape(tg_user.username or '-')}</code>\n"
            f"{group_line}"
            f"• <b>Dein Status:</b> <b>{role_label}</b> (Bot-Modus: <code>{access['mode']}</code>)",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat or not update.effective_user:
            return
        access = await self._authorize(update, require_admin=False)
        if not access:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        is_grp = self._is_group_chat(update)

        id_info = (
            f"📌 <b>User-ID:</b> <code>{user_id}</code> | <b>Gruppen-ID:</b> <code>{chat_id}</code>"
            if is_grp
            else f"📌 <b>Deine ID:</b> <code>{user_id}</code>"
        )

        admin_section = ""
        if access["is_admin"]:
            admin_section = (
                "\n\n<b>🔐 Admin-Befehle:</b>\n"
                "• <code>/login</code> – Einmal-Code für das Web-Admin-Panel (<code>/admin</code>)\n"
                "• <code>/add &lt;username&gt;</code> – Streamer hinzufügen &amp; Worker starten\n"
                "• <code>/track &lt;username&gt;</code> | <code>/stop &lt;username&gt;</code> – Worker steuern\n"
                "• <code>/allow &lt;id|@user&gt; [admin|user]</code> – Nutzer freischalten (oder per Reply!)\n"
                "• <code>/revoke &lt;id|@user&gt;</code> – Zugriff entziehen (oder per Reply!)\n"
                "• <code>/users</code> – Nutzerliste &amp; Anfragen anzeigen\n"
                "• <code>/mode &lt;whitelist|public&gt;</code> – Zugriffsmodus umschalten"
            )

        text = (
            "<b>🚀 Willkommen bei toktracker!</b>\n\n"
            "toktracker überwacht TikTok-Streamer in Echtzeit, erfasst alle Live-Geschenke "
            "(Coins &amp; Diamonds) und berechnet die Netto-Creator-Auszahlung in <b>EUR (€)</b> und <b>USD ($)</b>.\n\n"
            f"{id_info} | <b>Modus:</b> <code>{access['mode'].upper()}</code>\n\n"
            "<b>📋 Abfrage-Befehle (Privatchat &amp; Gruppe):</b>\n"
            "• <code>/overview [heute|24h|7d|30d|all]</code> – 📊 Finanz- &amp; Performance-Dashboard\n"
            "• <code>/radar [all|whale|shark|dolphin|vip]</code> – 🐋 Spender-Radar &amp; Rangliste\n"
            "• <code>/whales [min_coins]</code> – ⚡ Live-Feed der neuesten Großspenden\n"
            "• <code>/donor &lt;username&gt;</code> – 👤 Spender-Dossier, Stammkanal &amp; 1-Klick VIP\n"
            "• <code>/vip [add|remove &lt;user&gt;]</code> – ⭐ VIP-Watchlist &amp; Spender-Markierungen\n"
            "• <code>/loyalty [tage]</code> – 🔀 Frontenwechsler &amp; Streamer-Überschneidungen\n"
            "• <code>/streamers</code> – 👥 Alle Kanäle mit Live-Status (🟢/⚪) &amp; Zuschauern\n"
            "• <code>/gifts [streamer]</code> – 🎁 Live Gift-Ticker der letzten Geschenke\n"
            "• <code>/streamer &lt;username&gt; [stunden]</code> – 📊 Detail-Bericht pro Streamer\n"
            "• <code>/report [stunden]</code> – 📑 24h-Gesamtbericht aller Streamer\n"
            "• <code>/status</code> – ⚙️ Laufzeit-Status aller TikTokLive-Worker\n"
            "• <code>/app</code> – 📱 toktracker Mini-App öffnen\n"
            "• <code>/id</code> – 📌 Eigene User-ID &amp; Gruppen-ID prüfen"
            f"{admin_section}"
        )

        buttons = []
        webapp_url = (config.telegram_webapp_url or "").strip()
        if webapp_url:
            if webapp_url.lower().startswith("https://"):
                buttons.append([
                    InlineKeyboardButton(
                        text="📱 toktracker Mini-App öffnen",
                        web_app=WebAppInfo(url=webapp_url),
                    )
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(
                        text="🌐 toktracker Web-App öffnen",
                        url=webapp_url,
                    )
                ])

        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

        await self._send_with_optional_banner(
            bot=context.bot,
            chat_id=chat_id,
            html_text=text,
            banner_path=WELCOME_BANNER_PATH,
            reply_to_message_id=update.effective_message.message_id if is_grp else None,
            reply_markup=reply_markup,
        )

    async def _cmd_miniapp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        access = await self._authorize(update, require_admin=False)
        if not access:
            return

        is_grp = self._is_group_chat(update)
        webapp_url = (config.telegram_webapp_url or "").strip()

        buttons = []
        if webapp_url:
            if webapp_url.lower().startswith("https://"):
                buttons.append([
                    InlineKeyboardButton(
                        text="⚡ toktracker Mini-App öffnen",
                        web_app=WebAppInfo(url=webapp_url),
                    )
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(
                        text="🌐 toktracker Web-App im Browser öffnen",
                        url=webapp_url,
                    )
                ])

        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

        text = (
            "📱 <b>toktracker Telegram Mini-App</b>\n\n"
            "Verfolge alle Live-Battles (1v1, 2v2, 3er &amp; 4er-Matches), Punktestände "
            "und Einnahmen in Echtzeit direkt in der Telegram Mini-App!\n\n"
            "✨ <b>Features:</b>\n"
            "• ⚔️ <b>Live-Matches:</b> Punktestände, MVPs &amp; Ranglisten in Echtzeit\n"
            "• 💰 <b>Einnahmen:</b> Netto-Creator-Auszahlung (€/$), Coins &amp; Top-Gifter\n"
            "• 👥 <b>Streamer-Monitor:</b> Live-Zuschauer, Status &amp; Tagesverdienst\n\n"
        )
        if webapp_url:
            text += f"🔗 <b>URL:</b> <code>{html.escape(webapp_url)}</code>\n\nTippe auf den Button unten, um die Mini-App zu starten:"
        else:
            text += (
                "ℹ️ <i>Hinweis:</i> Es ist noch keine <code>TELEGRAM_WEBAPP_URL</code> in der <code>.env</code> hinterlegt.\n"
                "Du erreichst die Mini-App im Browser unter:\n"
                f"👉 <code>http://localhost:{config.app_port}/miniapp</code>\n\n"
                "<i>Tipp für native Telegram WebApps:</i>\n"
                "Hinterlege deine öffentliche HTTPS-Adresse in der <code>.env</code>:\n"
                "<code>TELEGRAM_WEBAPP_URL=https://deine-domain.de/miniapp</code>"
            )

        await self._send_with_optional_banner(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            html_text=text,
            banner_path=WELCOME_BANNER_PATH,
            reply_to_message_id=update.effective_message.message_id if is_grp else None,
            reply_markup=reply_markup,
        )

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        if not await self._authorize(update, require_admin=False):
            return

        hours = 24
        if context.args and context.args[0].isdigit():
            hours = max(1, min(720, int(context.args[0])))

        is_grp = self._is_group_chat(update)
        report = await asyncio.to_thread(generate_full_report_data, hours, False)
        await self._send_with_optional_banner(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            html_text=report["telegram_html"],
            banner_path=DAILY_REPORT_BANNER_PATH,
            reply_to_message_id=update.effective_message.message_id if is_grp else None,
        )

    async def _cmd_streamer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        if not context.args:
            await update.effective_message.reply_text(
                "Verwendung: <code>/streamer &lt;username&gt; [stunden]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        identifier = context.args[0].strip().lstrip("@")
        hours = 24
        if len(context.args) > 1 and context.args[1].isdigit():
            hours = max(1, min(8760, int(context.args[1])))

        s_row = await asyncio.to_thread(get_streamer_by_id_or_username, identifier)
        if not s_row:
            await update.effective_message.reply_text(
                f"❌ Streamer <code>@{html.escape(identifier)}</code> wurde nicht in der Datenbank gefunden.",
                parse_mode=ParseMode.HTML,
            )
            return

        uname = s_row["username"]
        is_tracking = monitor_manager.is_tracking(uname)
        diag = monitor_manager.get_worker_diagnostics(uname)
        worker_state = diag["state"] if diag else ("live" if s_row.get("is_live") else "stopped")

        card = await asyncio.to_thread(
            build_streamer_bot_report,
            s_row,
            hours,
            is_tracking,
            worker_state,
        )

        live_str = "🟢 LIVE" if card["is_live"] else f"⚪ {card['worker_state'].upper()}"
        top_g = (
            ", ".join(
                f"{g['gift_name']} ({g['total_coins']:,})" for g in card["top_gifts"][:3]
            )
            or "Keine"
        )
        top_u = (
            ", ".join(
                f"@{u['sender_username']} ({u['total_coins']:,})"
                for u in card["top_gifters"][:3]
            )
            or "Keine"
        )

        msg = (
            f"<b>📊 toktracker · Streamer-Einzelabfrage: {html.escape(card['display_name'])}</b> (@{html.escape(uname)})\n"
            f"Status: <b>{live_str}</b> | Aktuelle Zuschauer: <code>{card['current_viewers']:,}</code>\n"
            "────────────────────────\n"
            f"<b>Zeitraum ({hours}h):</b>\n"
            f"• Netto-Auszahlung: <b>{card['period_eur_net']:,.2f} €</b> (<code>${card['period_usd_net']:,.2f}</code>)\n"
            f"• Brutto-Kaufwert: <code>{card['period_eur_gross']:,.2f} €</code>\n"
            f"• Coins / Diamonds: <b>{card['period_coins']:,}</b> ({card['period_gifts_sent']:,} Gifts von {card['period_unique_gifters']} Giftern)\n"
            f"• Peak Zuschauer ({hours}h): <code>{card['period_peak_viewers']:,}</code>\n"
            f"• Top Gifts: {html.escape(top_g)}\n"
            f"• Top Spender: {html.escape(top_u)}\n"
            "────────────────────────\n"
            f"<b>Gesamt (All-Time):</b> <code>{card['alltime_eur_net']:,.2f} €</code> (<code>{card['alltime_coins']:,}</code> Coins)"
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    @staticmethod
    def _format_relative_time(dt_val: Any) -> str:
        if not dt_val:
            return "nie"
        try:
            now = datetime.utcnow()
            if isinstance(dt_val, (int, float)):
                dt = datetime.utcfromtimestamp(dt_val)
            elif isinstance(dt_val, datetime):
                dt = dt_val.replace(tzinfo=None) if dt_val.tzinfo else dt_val
            elif isinstance(dt_val, str):
                clean = dt_val.replace("T", " ").replace("Z", "").split("+")[0].split(".")[0]
                dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
            else:
                return str(dt_val)

            diff = now - dt
            secs = max(0, int(diff.total_seconds()))
            if secs < 60:
                return f"vor {secs}s"
            mins = secs // 60
            if mins < 60:
                return f"vor {mins}m"
            hours = mins // 60
            if hours < 24:
                return f"vor {hours}h"
            days = hours // 24
            return f"vor {days}d"
        except Exception:
            return str(dt_val)

    # -----------------------------------------------------------------
    # INTERAKTIVE DASHBOARD & FINANZ-ÜBERSICHT (/overview)
    # -----------------------------------------------------------------
    def _format_overview_view(self, period: str = "today") -> Tuple[str, InlineKeyboardMarkup]:
        norm_period = period if period in ("today", "24h", "7d", "30d", "all") else "today"
        data = get_dashboard_overview(period=norm_period, force_refresh=True)
        totals = data.get("totals", {})
        streamers = data.get("streamers", [])

        period_labels = {
            "today": "Heute (seit 00:00 Uhr)",
            "24h": "Letzte 24 Stunden",
            "7d": "Letzte 7 Tage",
            "30d": "Letzte 30 Tage",
            "all": "Gesamt (All-Time)",
        }
        p_label = period_labels.get(norm_period, norm_period)

        eur_net = totals.get("eur_net", 0.0)
        usd_net = totals.get("usd_net", 0.0)
        eur_gross = totals.get("eur_gross", 0.0)
        usd_gross = totals.get("usd_gross", 0.0)
        total_coins = totals.get("total_coins", 0)
        live_count = totals.get("live_count", 0)
        streamer_count = totals.get("streamer_count", 0)
        total_gifts = totals.get("total_gifts_sent", 0)

        lines = [
            "<b>📊 toktracker · Finanzübersicht</b>",
            f"Zeitraum: <b>{html.escape(p_label)}</b>",
            "────────────────────────",
            f"💰 <b>Creator Netto-Auszahlung:</b> <b>{eur_net:,.2f} €</b> (<code>${usd_net:,.2f}</code>)",
            f"🛒 <b>Viewer Brutto-Kaufwert:</b> <code>{eur_gross:,.2f} €</code> (<code>${usd_gross:,.2f}</code>)",
            f"💎 <b>Coins / Diamonds:</b> <b>{total_coins:,}</b> ({total_gifts:,} Geschenke)",
            f"📡 <b>Streamer:</b> <b>{live_count} live</b> von {streamer_count} überwachten",
            "────────────────────────",
            f"<b>🏆 Top Streamer ({p_label}):</b>",
        ]

        sorted_streamers = sorted(streamers, key=lambda s: int(s.get("total_coins") or 0), reverse=True)
        active_sorted = [s for s in sorted_streamers if int(s.get("total_coins") or 0) > 0]
        if not active_sorted:
            lines.append("<i>Keine Einnahmen in diesem Zeitraum verzeichnet.</i>")
        else:
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            for idx, s in enumerate(active_sorted[:5]):
                medal = medals[idx] if idx < len(medals) else f"{idx+1}."
                live_badge = " 🟢" if s.get("is_live") else ""
                s_coins = int(s.get("total_coins") or 0)
                s_eur = float(s.get("eur_net") or 0.0)
                lines.append(
                    f"{medal} <b>{html.escape(s.get('display_name') or s['username'])}</b>{live_badge}: "
                    f"<b>{s_eur:,.2f} €</b> (<code>{s_coins:,} Coins</code>)"
                )

        lines.append("\n💡 <i>Wechsle den Zeitraum mit den Buttons unten:</i>")
        text = "\n".join(lines)

        buttons = [
            [
                InlineKeyboardButton(f"{'✓ ' if norm_period == 'today' else ''}Heute", callback_data="overview_period:today"),
                InlineKeyboardButton(f"{'✓ ' if norm_period == '24h' else ''}24h", callback_data="overview_period:24h"),
                InlineKeyboardButton(f"{'✓ ' if norm_period == '7d' else ''}7 Tage", callback_data="overview_period:7d"),
            ],
            [
                InlineKeyboardButton(f"{'✓ ' if norm_period == '30d' else ''}30 Tage", callback_data="overview_period:30d"),
                InlineKeyboardButton(f"{'✓ ' if norm_period == 'all' else ''}Gesamt", callback_data="overview_period:all"),
                InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"overview_period:{norm_period}"),
            ],
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_overview(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        period = "today"
        if context.args:
            arg = context.args[0].lower().strip()
            if arg in ("today", "24h", "7d", "30d", "all"):
                period = arg
            elif arg in ("heute", "day"):
                period = "today"
            elif arg in ("woche", "week"):
                period = "7d"
            elif arg in ("monat", "month"):
                period = "30d"
            elif arg in ("gesamt", "total"):
                period = "all"

        text, markup = await asyncio.to_thread(self._format_overview_view, period)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # SPENDER-RADAR & CRM (/radar)
    # -----------------------------------------------------------------
    def _format_radar_view(self, tier: str = "all", period: str = "all") -> Tuple[str, InlineKeyboardMarkup]:
        norm_tier = tier if tier in ("all", "whale", "shark", "dolphin") else "all"
        min_tier_param = None if norm_tier == "all" else norm_tier
        data = get_donor_radar_data(period=period, min_tier=min_tier_param, limit=10, offset=0)
        kpis = data.get("kpis", {})
        donors = data.get("donors", [])

        tier_labels = {
            "all": "Alle Spender",
            "whale": "🐋 Whales (≥ 10k Coins)",
            "shark": "🦈 Sharks (≥ 2.5k Coins)",
            "dolphin": "🐬 Dolphins (≥ 500 Coins)",
        }
        tier_title = tier_labels.get(norm_tier, "Alle Spender")
        tier_icons = {"whale": "🐋", "shark": "🦈", "dolphin": "🐬", "fish": "🐟"}

        lines = [
            "<b>🎯 toktracker · Spender-Radar</b>",
            f"Filter: <b>{html.escape(tier_title)}</b>",
            "────────────────────────",
            "📊 <b>Netzwerk-Metriken:</b>",
            f"• 🐋 Whales: <b>{kpis.get('whales', 0)}</b> | 🦈 Sharks: <b>{kpis.get('sharks', 0)}</b> | 🐬 Dolphins: <b>{kpis.get('dolphins', 0)}</b>",
            f"• 🔀 Multi-Streamer Spender: <b>{kpis.get('cross_streamer_whales', 0)}</b>",
            f"• ⭐ VIP Spender: <b>{kpis.get('vip_donors', 0)}</b>",
            f"• 💰 Wal-Volumen: <b>{kpis.get('whale_eur_net', 0.0):,.2f} €</b> (<code>{kpis.get('whale_coins', 0):,} Coins</code>)",
            "────────────────────────",
            "<b>Top Spender:</b>",
        ]

        if not donors:
            lines.append("<i>Keine Spender in dieser Kategorie erfasst.</i>")
        else:
            for idx, d in enumerate(donors[:8], 1):
                icon = tier_icons.get(d.get("tier"), "🐟")
                vip_star = " ⭐VIP" if d.get("is_vip") else ""
                d_coins = int(d.get("total_coins") or 0)
                d_eur = float(d.get("total_net_eur") or 0.0)
                s_count = int(d.get("streamer_count") or 1)
                lines.append(
                    f"{idx}. {icon} <b>@{html.escape(d['username'])}</b>{vip_star}\n"
                    f"   └ <b>{d_coins:,} Coins</b> (<code>{d_eur:,.2f} €</code> Netto) · {s_count} Streamer"
                )

        lines.append("\n💡 <i>Dossier anzeigen:</i> <code>/donor &lt;username&gt;</code>")
        text = "\n".join(lines)

        buttons = [
            [
                InlineKeyboardButton(f"{'✓ ' if norm_tier == 'all' else ''}Alle", callback_data="radar_tier:all"),
                InlineKeyboardButton(f"{'✓ ' if norm_tier == 'whale' else ''}🐋 Whales", callback_data="radar_tier:whale"),
                InlineKeyboardButton(f"{'✓ ' if norm_tier == 'shark' else ''}🦈 Sharks", callback_data="radar_tier:shark"),
            ],
            [
                InlineKeyboardButton(f"{'✓ ' if norm_tier == 'dolphin' else ''}🐬 Dolphins", callback_data="radar_tier:dolphin"),
                InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"radar_tier:{norm_tier}"),
            ],
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_radar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        tier = "all"
        if context.args:
            arg = context.args[0].lower().strip()
            if arg in ("whale", "whales", "wal", "wale"):
                tier = "whale"
            elif arg in ("shark", "sharks", "hai", "haie"):
                tier = "shark"
            elif arg in ("dolphin", "dolphins", "delfin", "delfine"):
                tier = "dolphin"

        text, markup = await asyncio.to_thread(self._format_radar_view, tier)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # WHALE GIFT TICKER (/whales)
    # -----------------------------------------------------------------
    def _format_whales_view(self, min_coins: int = 1000) -> Tuple[str, InlineKeyboardMarkup]:
        val_min = min_coins if min_coins in (1000, 5000, 10000, 30000) else min_coins
        alerts = get_recent_whale_gifts(min_coins=val_min, limit=10)
        tier_icons = {"whale": "🐋", "shark": "🦈", "dolphin": "🐬", "fish": "🐟"}

        lines = [
            f"<b>🚨 toktracker · Whale Gift Ticker (≥ {val_min:,} Coins)</b>",
            "────────────────────────",
        ]

        if not alerts:
            lines.append(f"<i>Keine Geschenke ab {val_min:,} Coins in der Historie verzeichnet.</i>")
        else:
            for a in alerts[:8]:
                rel = self._format_relative_time(a.get("created_at"))
                vip_star = " ⭐VIP" if a.get("is_vip") else ""
                icon = tier_icons.get(a.get("tier"), "🐋")
                a_coins = int(a.get("total_coins") or 0)
                a_eur = float(a.get("eur_net") or 0.0)
                repeat = f" x{a['repeat_count']}" if int(a.get("repeat_count") or 1) > 1 else ""
                lines.append(
                    f"{icon} <b>@{html.escape(a['sender_username'])}</b>{vip_star} ➔ <b>@{html.escape(a['streamer_username'])}</b>\n"
                    f"   └ <b>{html.escape(a['gift_name'])}</b>{repeat} · <b>{a_coins:,} Coins</b> (<code>{a_eur:,.2f} €</code>) · <i>{rel}</i>"
                )

        lines.append("\n💡 <i>Spender analysieren:</i> <code>/donor &lt;username&gt;</code>")
        text = "\n".join(lines)

        buttons = [
            [
                InlineKeyboardButton(f"{'✓ ' if val_min == 1000 else ''}≥ 1k", callback_data="whales_min:1000"),
                InlineKeyboardButton(f"{'✓ ' if val_min == 5000 else ''}≥ 5k", callback_data="whales_min:5000"),
                InlineKeyboardButton(f"{'✓ ' if val_min == 10000 else ''}≥ 10k", callback_data="whales_min:10000"),
                InlineKeyboardButton(f"{'✓ ' if val_min == 30000 else ''}≥ 30k", callback_data="whales_min:30000"),
            ],
            [
                InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"whales_min:{val_min}"),
            ],
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_whales(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        min_coins = 1000
        if context.args:
            try:
                val = int(context.args[0].strip().replace(".", "").replace(",", ""))
                if val > 0:
                    min_coins = val
            except Exception:
                pass

        text, markup = await asyncio.to_thread(self._format_whales_view, min_coins)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # SPENDER-DOSSIER & CRM (/donor)
    # -----------------------------------------------------------------
    def _format_donor_view(self, username: str) -> Tuple[str, InlineKeyboardMarkup]:
        clean_uname = username.strip().lstrip("@").lower()
        data = get_donor_details(clean_uname)
        if not data or not data.get("profile"):
            text = (
                f"❌ <b>Spender nicht gefunden</b>\n\n"
                f"Für <code>@{html.escape(clean_uname)}</code> liegen noch keine Geschenke-Daten vor.\n"
                "Sobald dieser Nutzer in einem überwachten Livestream schenkt, wird sein Dossier automatisch angelegt."
            )
            buttons = [
                [InlineKeyboardButton("🎯 Zum Spender-Radar", callback_data="radar_tier:all")]
            ]
            return text, InlineKeyboardMarkup(buttons)

        p = data["profile"]
        tier = p.get("tier", "fish")
        tier_icons = {"whale": "🐋", "shark": "🦈", "dolphin": "🐬", "fish": "🐟"}
        tier_icon = tier_icons.get(tier, "🐟")
        is_vip = bool(p.get("is_vip"))
        vip_str = "⭐ VIP Spender" if is_vip else "Standard-Spender"

        rel_first = self._format_relative_time(p.get("first_seen"))
        rel_last = self._format_relative_time(p.get("last_seen"))

        loyalty = data.get("loyalty") or {}
        loyalty_badge = loyalty.get("badge", "Keine Einstufung")
        loyalty_label = loyalty.get("label", "")

        lines = [
            "<b>👤 toktracker · Spender-Dossier</b>",
            f"Name: <b>{html.escape(p.get('display_name') or p['username'])}</b> (@{html.escape(p['username'])})",
            f"Status: {tier_icon} <b>{tier.upper()}</b> | <b>{vip_str}</b>",
            "────────────────────────",
            "💰 <b>Finanz- & Spenden-Volumen:</b>",
            f"• Coins: <b>{int(p.get('total_coins') or 0):,}</b>",
            f"• Creator-Netto: <b>{float(p.get('total_eur_net') or 0.0):,.2f} €</b> (<code>${float(p.get('total_usd_net') or 0.0):,.2f}</code>)",
            f"• Geschenke gesamt: <code>{int(p.get('total_gifts') or 0):,}</code>",
            f"• Aktivität: Zuerst {rel_first} · Zuletzt {rel_last}",
            "────────────────────────",
            f"🔀 <b>Netzwerk-Loyalität:</b> {loyalty_badge}",
        ]
        if loyalty_label:
            lines.append(f"<i>{html.escape(loyalty_label)}</i>")

        streamers = data.get("streamers", [])
        if streamers:
            lines.append(f"\n<b>Unterstützte Streamer ({len(streamers)} Kanäle):</b>")
            total_coins = max(1, int(p.get("total_coins") or 0))
            for s in streamers[:4]:
                c_given = int(s.get("coins_given") or 0)
                pct = round((c_given / total_coins) * 100, 1)
                lines.append(
                    f"• <b>@{html.escape(s['streamer_username'])}</b>: <b>{c_given:,} Coins</b> ({pct}%) · <code>{float(s.get('eur_net') or 0.0):,.2f} €</code>"
                )

        top_gifts = data.get("top_gifts", [])
        if top_gifts:
            lines.append("\n<b>Lieblings-Geschenke:</b>")
            g_strs = [
                f"{html.escape(g['gift_name'])} ({int(g.get('total_coins') or 0):,})"
                for g in top_gifts[:3]
            ]
            lines.append("• " + ", ".join(g_strs))

        if p.get("notes"):
            lines.append(f"\n📝 <b>CRM-Notiz:</b> <i>{html.escape(p['notes'])}</i>")

        text = "\n".join(lines)

        vip_btn_label = "⭐ VIP-Status entfernen" if is_vip else "⭐ Als VIP markieren"
        buttons = [
            [
                InlineKeyboardButton(vip_btn_label, callback_data=f"donor_vip_toggle:{p['username']}"),
                InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"donor_refresh:{p['username']}"),
            ],
            [
                InlineKeyboardButton("🎯 Zum Spender-Radar", callback_data="radar_tier:all"),
            ],
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_donor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        if not context.args:
            await update.effective_message.reply_text(
                "Verwendung: <code>/donor &lt;username&gt;</code>\n"
                "Beispiel: <code>/donor montanablack</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        uname = context.args[0].strip().lstrip("@").lower()
        text, markup = await asyncio.to_thread(self._format_donor_view, uname)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # VIP MANAGEMENT (/vip)
    # -----------------------------------------------------------------
    async def _cmd_vip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        args = context.args or []
        if not args:
            radar_data = await asyncio.to_thread(
                get_donor_radar_data,
                vip_only=True,
                limit=25,
            )
            vips = radar_data.get("donors", [])
            if not vips:
                msg = (
                    "<b>⭐ toktracker · VIP Spender-Liste</b>\n"
                    "────────────────────────\n"
                    "Es sind aktuell keine Spender als VIP markiert.\n\n"
                    "<i>Spender als VIP hinzufügen:</i> <code>/vip add &lt;username&gt;</code>"
                )
            else:
                lines = [
                    "<b>⭐ toktracker · VIP Spender-Liste</b>",
                    "────────────────────────",
                    f"Gesamtanzahl: <b>{len(vips)} VIPs</b>\n",
                ]
                for idx, v in enumerate(vips, 1):
                    lines.append(
                        f"{idx}. ⭐ <b>@{html.escape(v['username'])}</b> · <b>{v['total_coins']:,} Coins</b> "
                        f"(<code>{v['total_net_eur']:,.2f} €</code> Netto, {v['streamer_count']} Streamer)"
                    )
                lines.append("\n💡 <i>Dossier anzeigen:</i> <code>/donor &lt;username&gt;</code>")
                msg = "\n".join(lines)
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
            return

        if not await self._authorize(update, require_admin=True):
            return

        first = args[0].lower().strip()
        if first in ("add", "set") and len(args) > 1:
            target_uname = args[1].strip().lstrip("@").lower()
            await asyncio.to_thread(save_donor_crm, target_uname, is_vip=True)
            await update.effective_message.reply_text(
                f"✅ Spender <b>@{html.escape(target_uname)}</b> wurde als <b>⭐ VIP</b> markiert.",
                parse_mode=ParseMode.HTML,
            )
        elif first in ("remove", "delete", "del") and len(args) > 1:
            target_uname = args[1].strip().lstrip("@").lower()
            await asyncio.to_thread(save_donor_crm, target_uname, is_vip=False)
            await update.effective_message.reply_text(
                f"ℹ️ VIP-Status für <b>@{html.escape(target_uname)}</b> wurde <b>entfernt</b>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            target_uname = first.lstrip("@").lower()
            new_status = await asyncio.to_thread(toggle_donor_vip, target_uname)
            st_str = "⭐ VIP" if new_status else "Standard"
            await update.effective_message.reply_text(
                f"Status für <b>@{html.escape(target_uname)}</b> geändert auf: <b>{st_str}</b>.",
                parse_mode=ParseMode.HTML,
            )

    # -----------------------------------------------------------------
    # LOYALITÄT & FRONTENWECHSLER (/loyalty)
    # -----------------------------------------------------------------
    def _format_loyalty_view(self, days: int = 14) -> Tuple[str, InlineKeyboardMarkup]:
        norm_days = days if days in (7, 14, 30) else 14
        data = get_network_loyalty_and_overlap(days_recent=norm_days)
        shifters = data.get("frontenwechsler", [])
        overlaps = data.get("overlap_matrix", [])

        lines = [
            f"<b>🔀 toktracker · Loyalität & Frontenwechsler ({norm_days} Tage)</b>",
            "────────────────────────",
            f"Erfasste Wechsel: <b>{len(shifters)}</b> | Streamer-Overlaps: <b>{len(overlaps)}</b>\n",
            "<b>⚡ Top Frontenwechsler (Streamer-Wechsel):</b>",
        ]

        if not shifters:
            lines.append("<i>Keine markanten Wechsel im gewählten Zeitraum festgestellt.</i>")
        else:
            for s in shifters[:5]:
                fw = s.get("frontenwechsel", {})
                old_s = fw.get("old_streamer", "?")
                new_s = fw.get("new_streamer", "?")
                vol_coins = int(s.get("shift_volume_coins") or 0)
                vol_eur = float(s.get("shift_volume_eur") or 0.0)
                vip = " ⭐VIP" if s.get("is_vip") else ""
                lines.append(
                    f"• <b>@{html.escape(s['username'])}</b>{vip}: <code>{s.get('migration_type', 'Wechsel')}</code>\n"
                    f"   @{html.escape(old_s)} ➔ <b>@{html.escape(new_s)}</b> (<b>{vol_coins:,} Coins</b> · <code>{vol_eur:,.2f} €</code>)"
                )

        lines.append("\n<b>🤝 Top Streamer-Overlaps (Geteilte Großspender):</b>")
        if not overlaps:
            lines.append("<i>Keine geteilten Spender verzeichnet.</i>")
        else:
            for o in overlaps[:4]:
                s1 = o.get("s1_username", "?")
                s2 = o.get("s2_username", "?")
                count = int(o.get("shared_donors_count") or 0)
                vol = float(o.get("shared_volume_eur") or 0.0)
                lines.append(
                    f"• <b>@{html.escape(s1)}</b> & <b>@{html.escape(s2)}</b>: <b>{count} gemeinsame Spender</b> (<code>{vol:,.2f} €</code>)"
                )

        text = "\n".join(lines)

        buttons = [
            [
                InlineKeyboardButton(f"{'✓ ' if norm_days == 7 else ''}7 Tage", callback_data="loyalty_days:7"),
                InlineKeyboardButton(f"{'✓ ' if norm_days == 14 else ''}14 Tage", callback_data="loyalty_days:14"),
                InlineKeyboardButton(f"{'✓ ' if norm_days == 30 else ''}30 Tage", callback_data="loyalty_days:30"),
            ],
            [
                InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"loyalty_days:{norm_days}"),
            ],
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_loyalty(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        days = 14
        if context.args:
            try:
                val = int(context.args[0].strip())
                if val in (7, 14, 30, 60, 90):
                    days = val
            except Exception:
                pass

        text, markup = await asyncio.to_thread(self._format_loyalty_view, days)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # STREAMER-MONITOR & LISTE (/streamers)
    # -----------------------------------------------------------------
    def _format_streamers_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        streamers = get_all_streamers_summary()
        lines = [
            f"<b>📺 toktracker · Streamer-Monitor ({len(streamers)} Kanäle)</b>",
            "────────────────────────",
        ]

        if not streamers:
            lines.append("<i>Keine Streamer hinterlegt. Füge einen mit <code>/add &lt;username&gt;</code> hinzu.</i>")
        else:
            for s in streamers[:12]:
                is_live = bool(s.get("is_live"))
                status_icon = "🟢 LIVE" if is_live else "⚪ OFFLINE"
                viewers = f" ({int(s.get('current_viewers') or 0):,} Viewers)" if is_live else ""
                coins = int(s.get("today_coins") or 0)
                eur = float(s.get("today_eur_net") or 0.0)
                lines.append(
                    f"• {status_icon}{viewers} <b>{html.escape(s.get('display_name') or s['username'])}</b> (@{html.escape(s['username'])})\n"
                    f"   └ Heute: <b>{coins:,} Coins</b> · <code>{eur:,.2f} €</code> Netto"
                )

        lines.append("\n💡 <i>Einzelabfrage:</i> <code>/streamer &lt;username&gt;</code>")
        text = "\n".join(lines)

        buttons = [
            [InlineKeyboardButton("🔄 Aktualisieren", callback_data="streamers_list:all")]
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_streamers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        text, markup = await asyncio.to_thread(self._format_streamers_view)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # GESCHENKE-FEED (/gifts)
    # -----------------------------------------------------------------
    def _format_gifts_view(self, streamer_ident: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
        streamer_row = None
        if streamer_ident:
            streamer_row = get_streamer_by_id_or_username(streamer_ident)

        if streamer_row:
            s_id = streamer_row["id"]
            gifts = get_recent_whale_gifts(min_coins=1, streamer_id=s_id, limit=10)
            header = f"<b>🎁 toktracker · Geschenke-Feed: {html.escape(streamer_row.get('display_name') or streamer_row['username'])}</b> (@{html.escape(streamer_row['username'])})"
        else:
            gifts = get_recent_whale_gifts(min_coins=100, limit=10)
            header = "<b>🎁 toktracker · Geschenke-Feed (Netzwerkweit ≥ 100 Coins)</b>"

        lines = [
            header,
            "────────────────────────",
        ]

        if not gifts:
            lines.append("<i>Noch keine Geschenke im Erfassungszeitraum eingetroffen.</i>")
        else:
            for g in gifts[:8]:
                rel = self._format_relative_time(g.get("created_at"))
                vip = " ⭐VIP" if g.get("is_vip") else ""
                coins = int(g.get("total_coins") or 0)
                eur = float(g.get("eur_net") or 0.0)
                repeat = f" x{g['repeat_count']}" if int(g.get("repeat_count") or 1) > 1 else ""
                lines.append(
                    f"• <b>@{html.escape(g['sender_username'])}</b>{vip} ➔ <b>@{html.escape(g['streamer_username'])}</b>\n"
                    f"   └ <b>{html.escape(g['gift_name'])}</b>{repeat} · <b>{coins:,} Coins</b> (<code>{eur:,.2f} €</code>) · <i>{rel}</i>"
                )

        lines.append("\n💡 <i>Spender analysieren:</i> <code>/donor &lt;username&gt;</code>")
        text = "\n".join(lines)

        val = streamer_row["username"] if streamer_row else "all"
        buttons = [
            [InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"gifts_refresh:{val}")]
        ]
        return text, InlineKeyboardMarkup(buttons)

    async def _cmd_gifts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return

        streamer_ident = context.args[0].strip().lstrip("@") if context.args else None
        text, markup = await asyncio.to_thread(self._format_gifts_view, streamer_ident)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    # -----------------------------------------------------------------
    # INTERAKTIVE CALLBACK DISPATCHER
    # -----------------------------------------------------------------
    async def _on_interactive_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return

        tg_user = update.effective_user
        access = await asyncio.to_thread(
            check_telegram_user_access,
            int(tg_user.id),
            tg_user.username or "",
            tg_user.first_name or "",
            int(update.effective_chat.id) if update.effective_chat else int(tg_user.id),
            update.effective_chat.title if update.effective_chat else "",
        )
        if not access.get("allowed"):
            await query.answer("⛔ Kein Zugriff (Whitelist aktiv).", show_alert=True)
            return

        data = query.data
        parts = data.split(":", 1)
        action = parts[0]
        val = parts[1] if len(parts) > 1 else ""

        text = None
        markup = None

        if action == "overview_period":
            await query.answer()
            period = val or "today"
            text, markup = await asyncio.to_thread(self._format_overview_view, period)
        elif action == "radar_tier":
            await query.answer()
            tier = val or "all"
            text, markup = await asyncio.to_thread(self._format_radar_view, tier)
        elif action == "whales_min":
            await query.answer()
            try:
                min_coins = int(val)
            except Exception:
                min_coins = 1000
            text, markup = await asyncio.to_thread(self._format_whales_view, min_coins)
        elif action == "donor_vip_toggle":
            uname = val.strip().lower()
            new_vip = await asyncio.to_thread(toggle_donor_vip, uname)
            alert_msg = f"⭐ @{uname} ist jetzt VIP!" if new_vip else f"ℹ️ VIP-Status für @{uname} entfernt."
            await query.answer(alert_msg)
            text, markup = await asyncio.to_thread(self._format_donor_view, uname)
        elif action == "donor_refresh":
            await query.answer("Aktualisiert")
            uname = val.strip().lower()
            text, markup = await asyncio.to_thread(self._format_donor_view, uname)
        elif action == "loyalty_days":
            await query.answer()
            try:
                days = int(val)
            except Exception:
                days = 14
            text, markup = await asyncio.to_thread(self._format_loyalty_view, days)
        elif action == "streamers_list":
            await query.answer("Aktualisiert")
            text, markup = await asyncio.to_thread(self._format_streamers_view)
        elif action == "gifts_refresh":
            await query.answer("Aktualisiert")
            ident = None if val == "all" else val
            text, markup = await asyncio.to_thread(self._format_gifts_view, ident)

        if text and markup:
            try:
                await query.edit_message_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                if "not modified" not in str(exc).lower():
                    logger.warning(f"Fehler beim Aktualisieren der Telegram-Nachricht: {exc}")

    async def _cmd_add_streamer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return

        if not context.args:
            await update.effective_message.reply_text(
                "Verwendung: <code>/add &lt;username&gt;</code>", parse_mode=ParseMode.HTML
            )
            return

        username = context.args[0].strip().lstrip("@").lower()
        profile = await asyncio.to_thread(fetch_tiktok_user_info, username)
        streamer = await asyncio.to_thread(
            upsert_streamer,
            username,
            profile.get("display_name") or username,
            profile.get("avatar_url"),
            profile.get("room_id"),
            profile.get("follower_count"),
            profile.get("is_live"),
            True,
        )
        monitor_manager.start_tracking(streamer["id"], streamer["username"], auto_reconnect=True)
        await update.effective_message.reply_text(
            f"✅ Streamer <b>@{html.escape(streamer['username'])}</b> gespeichert und Live-Worker gestartet!",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_track_streamer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return
        if not context.args:
            await update.effective_message.reply_text(
                "Verwendung: <code>/track &lt;username&gt;</code>", parse_mode=ParseMode.HTML
            )
            return
        s_row = await asyncio.to_thread(get_streamer_by_id_or_username, context.args[0])
        if not s_row:
            await update.effective_message.reply_text("❌ Streamer nicht gefunden.")
            return
        res = monitor_manager.start_tracking(s_row["id"], s_row["username"], auto_reconnect=True)
        await update.effective_message.reply_text(f"▶️ {res.get('message')}")

    async def _cmd_stop_streamer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return
        if not context.args:
            await update.effective_message.reply_text(
                "Verwendung: <code>/stop &lt;username&gt;</code>", parse_mode=ParseMode.HTML
            )
            return
        s_row = await asyncio.to_thread(get_streamer_by_id_or_username, context.args[0])
        if not s_row:
            await update.effective_message.reply_text("❌ Streamer nicht gefunden.")
            return
        res = monitor_manager.stop_tracking(s_row["username"])
        await update.effective_message.reply_text(f"⏹️ {res.get('message')}")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=False):
            return
        diag = monitor_manager.get_all_diagnostics()
        workers = diag.get("workers", [])
        lines = [
            "<b>⚙️ toktracker · Backend &amp; Worker Status</b>",
            f"Zugriffsmodus: <code>{get_telegram_access_mode().upper()}</code>",
            f"Aktive Worker: <b>{diag['workers_running']}</b> ({diag['workers_connected']} im Stream verbunden)",
        ]
        for w in workers:
            lines.append(
                f"• <b>@{html.escape(w['username'])}</b>: <code>{w['state']}</code> "
                f"(Viewers: {w['current_viewers']}, Session-Coins: {w['coins_received_session']:,})"
            )

        try:
            from system_service import get_system_health

            h = get_system_health()
            d = h.get("host", {}).get("disk", {})
            m = h.get("host", {}).get("memory", {})
            c = h.get("host", {}).get("cpu", {})
            lines.append(
                "\n🖥️ <b>Server-Ressourcen:</b>\n"
                f"• Festplatte: <code>{d.get('used_gb', 0):.1f}/{d.get('total_gb', 0):.1f} GB</code> (<b>{d.get('percent', 0)}%</b>)\n"
                f"• RAM: <code>{m.get('used_mb', 0):.0f}/{m.get('total_mb', 0):.0f} MB</code> (<b>{m.get('percent', 0)}%</b>)\n"
                f"• CPU: <b>{c.get('percent', 0)}%</b> (Load: {c.get('load_1m', 0)})"
            )
        except Exception:
            pass

        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_allow_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return

        reply_msg = update.effective_message.reply_to_message
        ident: Optional[str] = None
        tg_id_override: Optional[int] = None
        uname_override: Optional[str] = None
        fname_override: Optional[str] = None
        role = "user"

        if reply_msg and reply_msg.from_user and not reply_msg.from_user.is_bot:
            target_u = reply_msg.from_user
            ident = str(target_u.id)
            tg_id_override = int(target_u.id)
            uname_override = target_u.username or None
            fname_override = target_u.first_name or None
            if context.args and context.args[0].lower() in ("user", "admin"):
                role = context.args[0].lower()
        elif context.args:
            ident = context.args[0].strip()
            if len(context.args) > 1 and context.args[1].lower() in ("user", "admin"):
                role = context.args[1].lower()

        if not ident:
            await update.effective_message.reply_text(
                "Verwendung:\n"
                "• <code>/allow &lt;telegram_id_oder_@username&gt; [user|admin]</code>\n"
                "• Oder in einer Gruppe einfach auf die Nachricht einer Person mit <code>/allow</code> antworten!",
                parse_mode=ParseMode.HTML,
            )
            return

        row = await asyncio.to_thread(
            upsert_telegram_user_permission,
            ident,
            1,
            role,
            fname_override,
            "Freigeschaltet via /allow",
            tg_id_override,
            uname_override,
        )
        label = f"@{row['username']}" if row.get("username") else str(row.get("telegram_id"))
        self._log("INFO", f"Telegram-Nutzer {label} als {role.upper()} freigeschaltet.")
        await update.effective_message.reply_text(
            f"✅ Nutzer <b>{html.escape(label)}</b> (ID: <code>{row.get('telegram_id') or 'wird bei Nachricht verknüpft'}</code>) "
            f"wurde mit Rolle <code>{role.upper()}</code> für Privatchat &amp; Gruppen freigeschaltet!",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_revoke_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return

        reply_msg = update.effective_message.reply_to_message
        ident: Optional[str] = None
        tg_id_override: Optional[int] = None
        uname_override: Optional[str] = None

        if reply_msg and reply_msg.from_user and not reply_msg.from_user.is_bot:
            target_u = reply_msg.from_user
            ident = str(target_u.id)
            tg_id_override = int(target_u.id)
            uname_override = target_u.username or None
        elif context.args:
            ident = context.args[0].strip()

        if not ident:
            await update.effective_message.reply_text(
                "Verwendung:\n"
                "• <code>/revoke &lt;telegram_id_oder_@username&gt;</code>\n"
                "• Oder in einer Gruppe auf die Nachricht einer Person mit <code>/revoke</code> antworten.",
                parse_mode=ParseMode.HTML,
            )
            return

        row = await asyncio.to_thread(
            upsert_telegram_user_permission,
            ident,
            -1,
            "user",
            None,
            "Gesperrt via /revoke",
            tg_id_override,
            uname_override,
        )
        label = f"@{row['username']}" if row.get("username") else str(row.get("telegram_id"))
        self._log("INFO", f"Telegram-Nutzer {label} wurde gesperrt.")
        await update.effective_message.reply_text(
            f"🚫 Zugriff für <b>{html.escape(label)}</b> wurde entzogen.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_list_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return

        users = await asyncio.to_thread(list_telegram_users)
        mode = await asyncio.to_thread(get_telegram_access_mode)
        lines = [
            f"<b>👥 toktracker · Telegram-Nutzerverwaltung (Modus: {mode.upper()})</b>",
            f"Gesamt erfasst: <b>{len(users)}</b>",
            "────────────────────────",
        ]
        for u in users[:30]:
            st = int(u.get("is_allowed", 0))
            badge = "✅" if st == 1 else ("⏳" if st == 0 else "🚫")
            role_tag = " [ADMIN]" if u.get("role") == "admin" else ""
            uname = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "-")
            tg_id = u.get("telegram_id") or "-"
            lines.append(
                f"{badge} <code>{tg_id}</code> – <b>{html.escape(uname)}</b>{role_tag}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_access_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not await self._authorize(update, require_admin=True):
            return

        if not context.args or context.args[0].lower() not in ("whitelist", "public"):
            curr = await asyncio.to_thread(get_telegram_access_mode)
            await update.effective_message.reply_text(
                f"🔐 Aktueller Zugriffsmodus: <b>{curr.upper()}</b>\n\n"
                "Umschalten mit:\n"
                "• <code>/mode whitelist</code> – Nur freigeschaltete Personen (@username oder ID) dürfen den Bot im Privatchat &amp; in Gruppen nutzen\n"
                "• <code>/mode public</code> – Alle Telegram-Nutzer dürfen Berichte abrufen (Admins verwalten weiterhin)",
                parse_mode=ParseMode.HTML,
            )
            return

        new_mode = await asyncio.to_thread(set_telegram_access_mode, context.args[0].lower())
        self._log("INFO", f"Telegram-Zugriffsmodus geändert auf: {new_mode.upper()}")
        await update.effective_message.reply_text(
            f"✅ Bot-Zugriffsmodus wurde auf <b>{new_mode.upper()}</b> umgestellt!",
            parse_mode=ParseMode.HTML,
        )


telegram_bot_service = TelegramBotService()
