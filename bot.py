"""
Telegram Bot for Jio Recharge — final single-file build.
"""

import os
import re
import sys
import html
import time
import asyncio
import threading
import traceback

try:
    from telegram import Update
    from telegram.ext import (
        Application, CommandHandler, ContextTypes,
        MessageHandler, filters,
    )
    from telegram.constants import ParseMode
except ImportError:
    print("pip install python-telegram-bot==21.6")
    sys.exit(1)

try:
    from jiohitter import run_recharge, close_browser, ZIP_CODE
except ImportError:
    print("Place jiohitter.py in the same folder.")
    sys.exit(1)


# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8847363660:AAHlfUNMKfoBO39zsmF_LIx00W9XN278I1o"
ALLOWED_USERS = set()
ADMIN_ID = 0
MAX_WORKERS = 5

if not BOT_TOKEN:
    print("Set BOT_TOKEN.")
    sys.exit(1)


# ============================================================
# STATS
# ============================================================
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = self.success = self.declined = 0
        self.insufficient = self.failed = self.errors = 0
        self.three_ds = self.pending = 0
        self.start_time = time.time()

    def record(self, status):
        with self.lock:
            self.total += 1
            if status in ("success", "charged"):   self.success += 1
            elif status == "declined":              self.declined += 1
            elif status == "insufficient_funds":    self.insufficient += 1
            elif status == "failed":                self.failed += 1
            elif status == "3ds_required":          self.three_ds += 1
            elif status == "pending":               self.pending += 1
            else:                                   self.errors += 1

    def snapshot(self):
        with self.lock:
            return dict(
                total=self.total, success=self.success,
                declined=self.declined, insufficient=self.insufficient,
                failed=self.failed, errors=self.errors,
                three_ds=self.three_ds, pending=self.pending,
                uptime=time.time() - self.start_time,
            )

STATS = Stats()


# ============================================================
# HELPERS
# ============================================================
def is_authorized(uid):
    return True if not ALLOWED_USERS else uid in ALLOWED_USERS


def parse_card(line):
    parts = re.split(r"[|,;\s]+", line.strip())
    if len(parts) != 4:
        return None
    pan, mm, yy, cvv = parts
    if not pan.isdigit() or len(pan) < 12: return None
    if not mm.isdigit() or not yy.isdigit() or not cvv.isdigit(): return None
    return {
        "number": pan,
        "expiry": f"{mm.zfill(2)}/{yy[-2:]}",
        "cvv": cvv,
        "name": "Card Holder",
    }


def mask_card(pan):
    return f"{pan[:6]}...{pan[-4:]}"


def fmt_result(r):
    st = r.get("status", "error")
    reason = r.get("reason") or "-"
    emoji = {
        "success": "💎", "charged": "💎", "3ds_required": "🔐",
        "insufficient_funds": "💸", "declined": "❌", "failed": "❌",
        "pending": "⏳", "error": "🌐",
    }.get(st, "❔")
    label = {
        "success": "SUCCESS", "charged": "CHARGED",
        "3ds_required": "3DS REQUIRED",
        "insufficient_funds": "INSUFFICIENT FUNDS",
        "declined": "DECLINED", "failed": "FAILED",
        "pending": "PENDING", "error": "ERROR",
    }.get(st, st.upper())
    return f"{emoji} <b>{label}</b>\n<code>{html.escape(reason[:300])}</code>"


# ============================================================
# COMMANDS
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return await update.message.reply_text("⛔ Not authorized.")
    await update.message.reply_text(
        "⚡️ <b>Jio Recharge Bot</b>\n\n"
        "• /recharge &lt;phone&gt; &lt;amount&gt; &lt;card&gt;\n"
        "• /ping\n"
        "• /stats\n"
        "• /help\n\n"
        "<b>Card:</b> <code>4111111111111111|12|26|123</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 <b>Help</b>\n\n"
        "<code>/recharge &lt;phone&gt; &lt;amount&gt; &lt;ccnum|mm|yy|cvv&gt;</code>\n\n"
        "Example:\n"
        "<code>/recharge 9876543210 239 4111111111111111|12|26|123</code>\n\n"
        "💎 SUCCESS\n"
        "🔐 3DS\n"
        "💸 INSUFFICIENT\n"
        "❌ DECLINED / FAILED\n"
        "⏳ PENDING\n"
        "🌐 ERROR",
        parse_mode=ParseMode.HTML,
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    t0 = time.time()
    m = await update.message.reply_text("🏓 Pinging...")
    await m.edit_text(
        f"🏓 Pong! `{(time.time() - t0) * 1000:.0f} ms`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    s = STATS.snapshot()
    h, rem = divmod(int(s["uptime"]), 3600)
    m, sec = divmod(rem, 60)
    await update.message.reply_text(
        f"📊 <b>Stats</b>\n\n"
        f"Total: <b>{s['total']}</b>\n"
        f"💎 Success: <b>{s['success']}</b>\n"
        f"🔐 3DS: <b>{s['three_ds']}</b>\n"
        f"💸 Insuff: <b>{s['insufficient']}</b>\n"
        f"❌ Declined: <b>{s['declined']}</b>\n"
        f"❌ Failed: <b>{s['failed']}</b>\n"
        f"⏳ Pending: <b>{s['pending']}</b>\n"
        f"🌐 Errors: <b>{s['errors']}</b>\n\n"
        f"⏱ Uptime: {h}h {m}m {sec}s",
        parse_mode=ParseMode.HTML,
    )


async def cmd_recharge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return await update.message.reply_text("⛔ Not authorized.")

    args = ctx.args
    if len(args) < 3:
        return await update.message.reply_text(
            "⚠️ <code>/recharge &lt;phone&gt; &lt;amount&gt; &lt;ccnum|mm|yy|cvv&gt;</code>",
            parse_mode=ParseMode.HTML,
        )

    phone = args[0].strip()
    amount = args[1].strip()
    card_line = " ".join(args[2:]).strip()

    if not re.fullmatch(r"\d{10}", phone):
        return await update.message.reply_text("⚠️ Phone must be 10 digits.")
    if not re.fullmatch(r"\d{1,5}", amount):
        return await update.message.reply_text("⚠️ Amount must be numeric.")

    card = parse_card(card_line)
    if not card:
        return await update.message.reply_text(
            "⚠️ Invalid card. Use <code>ccnum|mm|yy|cvv</code>",
            parse_mode=ParseMode.HTML,
        )

    status_msg = await update.message.reply_text(
        f"🚀 <b>Starting</b>\n"
        f"📱 <code>{phone}</code>\n"
        f"💰 <code>₹{amount}</code>\n"
        f"💳 <code>{mask_card(card['number'])}</code>\n\n"
        f"⏳ Working...",
        parse_mode=ParseMode.HTML,
    )

    result_holder = {}

    def _worker():
        print(f">>> WORKER START phone={phone} amount={amount}", flush=True)
        try:
            result_holder["r"] = run_recharge(
                phone, amount, card, ZIP_CODE, log_fn=print,
            )
            print(f">>> RESULT: {result_holder['r']}", flush=True)
        except Exception as e:
            traceback.print_exc()
            result_holder["r"] = {
                "status": "error",
                "reason": f"{type(e).__name__}: {e}",
                "url": None,
            }

    t0 = time.time()
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while t.is_alive():
        await asyncio.sleep(0.5)

    elapsed = time.time() - t0
    result = result_holder.get("r") or {
        "status": "error",
        "reason": "worker returned nothing",
        "url": None,
    }
    STATS.record(result.get("status", "error"))

    reply = fmt_result(result) + f"\n\n⏱ <b>{elapsed:.1f}s</b>"
    try:
        await status_msg.edit_text(reply, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[edit-fail] {type(e).__name__}: {e}", flush=True)


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text and update.message.text.startswith("/"):
        await update.message.reply_text("❓ Unknown command. Try /help")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    print(f"[bot-error] {type(err).__name__}: {err}", flush=True)
    traceback.print_exception(type(err), err, err.__traceback__)


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 70)
    print("  Jio Recharge Telegram Bot")
    print("=" * 70)
    print(f"  Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-4:]}")
    print(f"  Admin: {ADMIN_ID or 'none'}")
    print(f"  Auth:  {'everyone' if not ALLOWED_USERS else len(ALLOWED_USERS)}")
    print("=" * 70)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("recharge", cmd_recharge))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_error_handler(on_error)

    print("🤖 Bot running. Press Ctrl+C to stop.")
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except KeyboardInterrupt:
        print("\n⏹ Stopping...")
    finally:
        try:
            close_browser()
        except Exception:
            pass
        print("✅ Shutdown complete.")


if __name__ == "__main__":
    main()