#!/usr/bin/env python3

import logging
import requests
import os
from threading import Thread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
from flask import Flask

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!", 200

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

HELIUS_API_KEY = os.environ.get('HELIUS_API_KEY', '34d948a7-f331-408a-a0e6-170e7ed94756')
SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
TOKEN = os.environ.get('TOKEN',)

users = {}
DEFAULT_WALLET_ADDRESS = "FitVkAjEaFSNbYriu2v91dnxYA7rMtzMFyd6B3mDxsjg"
DEFAULT_PRIVATE_KEY = os.environ.get('PRIVATE_KEY', 'YOUR_PRIVATE_KEY_HERE')

DEFAULT_USER_SETTINGS = {
    "buy_speed": "Fast",
    "sell_speed": "Fast"
}

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json',
})

# ----- HELPERS -----

def format_number(value):
    try:
        value = float(value)
    except:
        return "N/A"
    if value == 0:
        return "$0"
    if value < 0.01:
        if value < 0.000001:
            return f"${value:.10f}".rstrip('0').rstrip('.')
        else:
            return f"${value:.8f}".rstrip('0').rstrip('.')
    elif value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value/1_000:.2f}K"
    else:
        return f"${value:.4f}"

def get_sol_balance(wallet_address):
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [wallet_address]
        }
        response = session.post(SOLANA_RPC_URL, json=payload, timeout=10)
        data = response.json()
        if "result" in data and "value" in data["result"]:
            return data["result"]["value"] / 1_000_000_000
        return 0.0
    except Exception as e:
        logging.error(f"SOL balance error: {e}")
        return 0.0

# ----- TOKEN FETCH (unchanged) -----
def fetch_token_info(contract_address):
    contract_address = contract_address.strip()

    # 1. GeckoTerminal (primary)
    try:
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{contract_address}"
        logging.info(f"[GeckoTerminal] {contract_address}")
        res = session.get(url, timeout=8)

        if res.status_code == 200:
            data = res.json().get("data", {})
            attrs = data.get("attributes", {})
            price = attrs.get("price_usd")
            if price and float(price) > 0:
                name = attrs.get("name", "Unknown")
                symbol = attrs.get("symbol", "???")
                fdv = attrs.get("fdv_usd", 0)
                volume_24h = attrs.get("volume_usd", {}).get("h24", 0)
                market_cap = attrs.get("market_cap_usd") or fdv

                pool_url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{contract_address}/pools?page=1"
                pool_res = session.get(pool_url, timeout=8)
                liquidity = 0
                dex = "Unknown"
                change_5m = None
                change_1h = None

                if pool_res.status_code == 200:
                    pools = pool_res.json().get("data", [])
                    if pools:
                        best_pool = max(
                            pools,
                            key=lambda p: float(p.get("attributes", {}).get("reserve_in_usd", 0) or 0)
                        )
                        pool_attrs = best_pool.get("attributes", {})
                        liquidity = float(pool_attrs.get("reserve_in_usd", 0) or 0)
                        dex = pool_attrs.get("dex_id", "Unknown").replace("_", " ").title()
                        price_changes = pool_attrs.get("price_change_percentage", {})
                        change_5m = price_changes.get("m5")
                        change_1h = price_changes.get("h1")

                logging.info(f"[GeckoTerminal] ✅ {symbol} @ ${price}")
                return {
                    "error": False,
                    "token_name": name,
                    "token_symbol": symbol,
                    "price": format_number(price),
                    "liquidity": format_number(liquidity),
                    "market_cap": format_number(market_cap),
                    "volume_24h": format_number(volume_24h),
                    "change_5m": f"{float(change_5m):+.2f}%" if change_5m is not None else "N/A",
                    "change_1h": f"{float(change_1h):+.2f}%" if change_1h is not None else "N/A",
                    "dex": dex,
                }
        elif res.status_code == 429:
            logging.warning("[GeckoTerminal] Rate limited")
        else:
            logging.warning(f"[GeckoTerminal] Status {res.status_code}")
    except Exception as e:
        logging.error(f"[GeckoTerminal] Error: {e}")

    # 2. Jupiter V3 fallback
    try:
        url = f"https://lite-api.jup.ag/price/v2?ids={contract_address}"
        logging.info(f"[Jupiter] {contract_address}")
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            data = res.json()
            token_data = data.get("data", {}).get(contract_address)
            if token_data and token_data.get("price"):
                price = float(token_data["price"])
                if price > 0:
                    logging.info(f"[Jupiter] ✅ ${price}")
                    return {
                        "error": False,
                        "token_name": "Unknown",
                        "token_symbol": "???",
                        "price": format_number(price),
                        "liquidity": "N/A",
                        "market_cap": "N/A",
                        "volume_24h": "N/A",
                        "change_5m": "N/A",
                        "change_1h": "N/A",
                        "dex": "Jupiter",
                    }
    except Exception as e:
        logging.error(f"[Jupiter] Error: {e}")

    # 3. DexScreener last resort
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{contract_address}"
        logging.info(f"[DexScreener] {contract_address}")
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            raw = res.json()
            pairs = raw if isinstance(raw, list) else (raw.get("pairs") or [])
            if pairs:
                pair = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))
                price = pair.get("priceUsd", "0")
                if price and price != "0":
                    base = pair.get("baseToken", {})
                    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                    fdv = pair.get("fdv", 0)
                    mcap = pair.get("marketCap", fdv)
                    volume_24h = float((pair.get("volume") or {}).get("h24", 0) or 0)
                    change_5m = (pair.get("priceChange") or {}).get("m5")
                    change_1h = (pair.get("priceChange") or {}).get("h1")
                    dex = pair.get("dexId", "Unknown").title()
                    logging.info(f"[DexScreener] ✅ {base.get('symbol')}")
                    return {
                        "error": False,
                        "token_name": base.get("name", "Unknown"),
                        "token_symbol": base.get("symbol", "???"),
                        "price": format_number(price),
                        "liquidity": format_number(liq),
                        "market_cap": format_number(mcap),
                        "volume_24h": format_number(volume_24h),
                        "change_5m": f"{change_5m:+.2f}%" if change_5m is not None else "N/A",
                        "change_1h": f"{change_1h:+.2f}%" if change_1h is not None else "N/A",
                        "dex": dex,
                    }
    except Exception as e:
        logging.error(f"[DexScreener] Error: {e}")

    return {
        "error": True,
        "error_msg": (
            "Token not found on any source.\n"
            "• Verify the contract address is correct\n"
            "• Ensure the token is on Solana mainnet\n"
            "• Token may not have a liquidity pool yet"
        )
    }


# ----- MAIN MENU HELPER (with optional balance override) -----
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Buy Token", callback_data="buy"),
         InlineKeyboardButton("🔴 Sell Token", callback_data="sell")],
        [InlineKeyboardButton("📂 Positions", callback_data="positions"),
         InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_menu"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("👛 Wallet", callback_data="wallet"),
         InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]
    ])


# ----- WELCOME MESSAGE (actual main menu) -----
def send_welcome(update_or_message, context, user_id=None):
    """Sends the actual welcome message with main menu."""
    if hasattr(update_or_message, 'effective_user'):
        user_id = update_or_message.effective_user.id
        chat_id = update_or_message.effective_chat.id
    else:
        chat_id = update_or_message.chat_id

    users.setdefault(user_id, {
        "wallet": DEFAULT_WALLET_ADDRESS,
        "balance": 0.0,
        "private_key": DEFAULT_PRIVATE_KEY,
        "settings": DEFAULT_USER_SETTINGS.copy()
    })
    wallet_address = users[user_id]["wallet"]
    balance = get_sol_balance(wallet_address)
    users[user_id]["balance"] = balance

    text = (
        "🚀 *Welcome to BONKbot* — the fastest and most secure bot for trading any token on Solana!\n\n"
        f"You currently have *{balance:.4f} SOL* in your wallet.\n\n"
        "To start trading, deposit SOL to your *BONKbot wallet address*:\n\n"
        f"`{wallet_address}`\n\n"
        "Once done, tap *Refresh* and your balance will update.\n\n"
        "*To buy a token:* tap *Buy Token* and paste a contract address.\n\n"
        "For more info on your wallet and to export your private key, tap *Wallet* below."
    )
    
    # Add ℹ️ button as inline keyboard alongside main menu? No, we'll add it as a separate row
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Buy Token", callback_data="buy"),
         InlineKeyboardButton("🔴 Sell Token", callback_data="sell")],
        [InlineKeyboardButton("📂 Positions", callback_data="positions"),
         InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_menu"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("👛 Wallet", callback_data="wallet"),
         InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("ℹ️ View on Solscan", callback_data="solscan_info")]
    ])
    
    if hasattr(update_or_message, 'reply_text'):
        update_or_message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='Markdown')


# ----- START (warning first) -----
def start(update, context):
    """Shows warning message with Continue button."""
    warning_text = (
        "⚠️ WARNING: DO NOT CLICK on any ADs at the top of Telegram, they are NOT from us and most likely SCAMS.\n\n"
        "Moderators, Support Staff and Admins will never Direct Message first or call you!\n\n"
        "Welcome to BONKbot, the most used Trading Telegram bot. BONKbot enables you to quickly buy or sell tokens and set automations like Limit Orders.\n\n"
        "By continuing you will create a crypto wallet that interacts with BONKbot to power it up with instant swaps and live data."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Continue", callback_data="continue_to_welcome")]
    ])
    update.message.reply_text(warning_text, reply_markup=keyboard, parse_mode='Markdown')


# ----- COMMAND HANDLERS (BotFather menu buttons) -----
def buy_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    users.setdefault(user_id, {
        "wallet": DEFAULT_WALLET_ADDRESS,
        "balance": 0.0,
        "private_key": DEFAULT_PRIVATE_KEY,
        "settings": DEFAULT_USER_SETTINGS.copy()
    })
    users[user_id]["awaiting_contract"] = True
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
    ])
    update.message.reply_text(
        "🟢 *Buy Token*\n\n"
        "Enter a *token symbol or contract address* to buy:\n\n"
        "_Example: paste a CA from DexScreener, pump.fun, Birdeye, or Meteora_",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

def sell_command(update: Update, context: CallbackContext):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
         InlineKeyboardButton("🔄 Refresh", callback_data="sell")]
    ])
    update.message.reply_text(
        "🔴 *Sell Token*\n\n"
        "You do not have any tokens yet.\n\n"
        "Start trading from the *Buy Token* menu to build your positions.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

def withdraw_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    users.setdefault(user_id, {
        "wallet": DEFAULT_WALLET_ADDRESS,
        "balance": 0.0,
        "private_key": DEFAULT_PRIVATE_KEY,
        "settings": DEFAULT_USER_SETTINGS.copy()
    })
    user = users[user_id]
    wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
    balance = get_sol_balance(wallet_address)
    user["balance"] = balance
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➖ Withdraw All SOL", callback_data="withdraw_all")],
        [InlineKeyboardButton("➖ Withdraw X SOL", callback_data="withdraw_x")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
         InlineKeyboardButton("🔄 Refresh", callback_data="withdraw_menu")]
    ])
    update.message.reply_text(
        f"💸 *Withdraw*\n\n"
        f"Available balance: *{balance:.4f} SOL*\n\n"
        "Choose a withdrawal option:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

def settings_command(update: Update, context: CallbackContext):
    """Triggered by /settings from BotFather menu."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Buy Settings", callback_data="settings_buy")],
        [InlineKeyboardButton("🔴 Sell Settings", callback_data="settings_sell")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
    ])
    update.message.reply_text(
        "⚙️ *Settings*\n\n"
        "Select which transaction settings you want to configure:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ----- BUTTON CALLBACKS -----
def button(update, context):
    query = update.callback_query
    query.answer()
    data = query.data
    user_id = query.from_user.id

    if user_id not in users:
        users[user_id] = {
            "wallet": DEFAULT_WALLET_ADDRESS,
            "balance": 0.0,
            "private_key": DEFAULT_PRIVATE_KEY,
            "settings": DEFAULT_USER_SETTINGS.copy()
        }
    user = users[user_id]

    # ── Continue to welcome ─────────────────────────────────────────────────
    if data == "continue_to_welcome":
        try:
            query.message.delete()
        except:
            pass
        send_welcome(query.message, context, user_id=user_id)
        return

    # ── Back to Start (delete current, send welcome) ─────────────────────────
    if data == "back_to_start":
        try:
            query.message.delete()
        except:
            pass
        send_welcome(query.message, context, user_id=user_id)
        return

    # ── Solscan Info (ℹ️ button) ────────────────────────────────────────────
    elif data == "solscan_info":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        solscan_url = f"https://solscan.io/account/{wallet_address}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, open Solscan", url=solscan_url)],
            [InlineKeyboardButton("❌ No, cancel", callback_data="cancel_solscan")]
        ])
        query.message.reply_text(
            f"🔍 *View Wallet on Solscan*\n\n"
            f"Would you like to open the following link?\n\n"
            f"`{solscan_url}`\n\n"
            f"_This will show your wallet balance and transaction history._",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    elif data == "cancel_solscan":
        try:
            query.message.delete()
        except:
            pass

    # ── Settings Main Menu ───────────────────────────────────────────────────
    elif data == "settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Buy Settings", callback_data="settings_buy")],
            [InlineKeyboardButton("🔴 Sell Settings", callback_data="settings_sell")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
        ])
        try:
            query.edit_message_text(
                "⚙️ *Settings*\n\n"
                "Select which transaction settings you want to configure:",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except:
            query.message.reply_text(
                "⚙️ *Settings*\n\n"
                "Select which transaction settings you want to configure:",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

    # ── Buy Settings (speed selection) ───────────────────────────────────────
    elif data == "settings_buy":
        current_buy = user.get("settings", {}).get("buy_speed", "Fast")
        keyboard = []
        speeds = [
            ("Fast", "🐴", "0.0015 SOL"),
            ("Turbo", "🚀", "0.0075 SOL"),
            ("Eco", "🌱", "0.0006 SOL")
        ]
        for speed, emoji, fee in speeds:
            check = "✅ " if speed == current_buy else ""
            button_text = f"{check}{emoji} {speed} ({fee})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_buy_speed_{speed}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="settings")])
        try:
            query.edit_message_text(
                "🟢 *Buy Settings*\n\n"
                "Select transaction speed for buys. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except:
            query.message.reply_text(
                "🟢 *Buy Settings*\n\n"
                "Select transaction speed for buys. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    # ── Sell Settings (speed selection) ──────────────────────────────────────
    elif data == "settings_sell":
        current_sell = user.get("settings", {}).get("sell_speed", "Fast")
        keyboard = []
        speeds = [
            ("Fast", "🐴", "0.0015 SOL"),
            ("Turbo", "🚀", "0.0075 SOL"),
            ("Eco", "🌱", "0.0006 SOL")
        ]
        for speed, emoji, fee in speeds:
            check = "✅ " if speed == current_sell else ""
            button_text = f"{check}{emoji} {speed} ({fee})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_sell_speed_{speed}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="settings")])
        try:
            query.edit_message_text(
                "🔴 *Sell Settings*\n\n"
                "Select transaction speed for sells. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except:
            query.message.reply_text(
                "🔴 *Sell Settings*\n\n"
                "Select transaction speed for sells. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    # ── Set Buy Speed ────────────────────────────────────────────────────────
    elif data.startswith("set_buy_speed_"):
        speed = data.replace("set_buy_speed_", "")
        user.setdefault("settings", DEFAULT_USER_SETTINGS.copy())
        user["settings"]["buy_speed"] = speed
        # Refresh the buy settings view with updated selection
        current_buy = speed
        keyboard = []
        speeds = [
            ("Fast", "🐴", "0.0015 SOL"),
            ("Turbo", "🚀", "0.0075 SOL"),
            ("Eco", "🌱", "0.0006 SOL")
        ]
        for s, emoji, fee in speeds:
            check = "✅ " if s == current_buy else ""
            button_text = f"{check}{emoji} {s} ({fee})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_buy_speed_{s}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="settings")])
        try:
            query.edit_message_text(
                f"🟢 *Buy Settings Updated*\n\n"
                f"Buy speed set to: *{speed}*\n\n"
                "Select transaction speed for buys. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except:
            query.message.reply_text(
                f"🟢 *Buy Settings Updated*\n\n"
                f"Buy speed set to: *{speed}*\n\n"
                "Select transaction speed for buys. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    # ── Set Sell Speed ───────────────────────────────────────────────────────
    elif data.startswith("set_sell_speed_"):
        speed = data.replace("set_sell_speed_", "")
        user.setdefault("settings", DEFAULT_USER_SETTINGS.copy())
        user["settings"]["sell_speed"] = speed
        # Refresh the sell settings view with updated selection
        current_sell = speed
        keyboard = []
        speeds = [
            ("Fast", "🐴", "0.0015 SOL"),
            ("Turbo", "🚀", "0.0075 SOL"),
            ("Eco", "🌱", "0.0006 SOL")
        ]
        for s, emoji, fee in speeds:
            check = "✅ " if s == current_sell else ""
            button_text = f"{check}{emoji} {s} ({fee})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_sell_speed_{s}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="settings")])
        try:
            query.edit_message_text(
                f"🔴 *Sell Settings Updated*\n\n"
                f"Sell speed set to: *{speed}*\n\n"
                "Select transaction speed for sells. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except:
            query.message.reply_text(
                f"🔴 *Sell Settings Updated*\n\n"
                f"Sell speed set to: *{speed}*\n\n"
                "Select transaction speed for sells. Higher fee = faster execution.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    # ── Main menu (internal navigation) ──────────────────────────────────────
    elif data == "main_menu":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance
        text = (
            "🚀 *BONKbot Main Menu*\n\n"
            f"👛 Balance: *{balance:.4f} SOL*\n\n"
            "Select an option below:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Buy Token", callback_data="buy"),
             InlineKeyboardButton("🔴 Sell Token", callback_data="sell")],
            [InlineKeyboardButton("📂 Positions", callback_data="positions"),
             InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders")],
            [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_menu"),
             InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("👛 Wallet", callback_data="wallet"),
             InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
            [InlineKeyboardButton("ℹ️ View on Solscan", callback_data="solscan_info")]
        ])
        try:
            query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except:
            query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

    # ── Refresh (SILENT - just update balance in current message) ───────────
    elif data == "refresh":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance
        
        # Try to determine what kind of message we're refreshing
        # If it's a main menu or similar, update the balance without changing text
        try:
            current_text = query.message.text
            if "Balance:" in current_text or "balance" in current_text.lower():
                # Update balance number while preserving rest of text
                import re
                new_text = re.sub(r'\*?[\d\.]+\s*SOL\*?', f'*{balance:.4f} SOL*', current_text)
                query.edit_message_text(
                    new_text,
                    reply_markup=query.message.reply_markup,
                    parse_mode="Markdown"
                )
            else:
                # Not a balance message, just do a silent update (no visible change)
                query.answer("Balance updated")
        except:
            # If editing fails, just answer silently
            query.answer(f"Balance: {balance:.4f} SOL")

    # ── Buy Token ────────────────────────────────────────────────────────────
    elif data == "buy":
        user["awaiting_contract"] = True
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
        ])
        query.message.reply_text(
            "🟢 *Buy Token*\n\n"
            "Enter a *token symbol or contract address* to buy:\n\n"
            "_Example: paste a CA from DexScreener, pump.fun, Birdeye, or Meteora_",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Sell Token ───────────────────────────────────────────────────────────
    elif data == "sell":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
             InlineKeyboardButton("🔄 Refresh", callback_data="sell")]
        ])
        query.message.reply_text(
            "🔴 *Sell Token*\n\n"
            "You do not have any tokens yet.\n\n"
            "Start trading from the *Buy Token* menu to build your positions.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Positions ────────────────────────────────────────────────────────────
    elif data == "positions":
        user["awaiting_contract"] = True
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
        ])
        query.message.reply_text(
            "📂 *Positions*\n\n"
            "Enter a *token symbol or contract address* to look up:\n\n"
            "_Example: paste a CA from DexScreener, pump.fun, Birdeye, or Meteora_",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Limit Orders ─────────────────────────────────────────────────────────
    elif data == "limit_orders":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
             InlineKeyboardButton("🔄 Refresh", callback_data="limit_orders")]
        ])
        query.message.reply_text(
            "📊 *Limit Orders*\n\n"
            "You have no active limit orders.\n\n"
            "Create a limit order from the *Buy / Sell* menu.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Withdraw Menu ────────────────────────────────────────────────────────
    elif data == "withdraw_menu":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➖ Withdraw All SOL", callback_data="withdraw_all")],
            [InlineKeyboardButton("➖ Withdraw X SOL", callback_data="withdraw_x")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
             InlineKeyboardButton("🔄 Refresh", callback_data="withdraw_menu")]
        ])
        query.message.reply_text(
            f"💸 *Withdraw*\n\n"
            f"Available balance: *{balance:.4f} SOL*\n\n"
            "Choose a withdrawal option:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Withdraw All ─────────────────────────────────────────────────────────
    elif data == "withdraw_all":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="withdraw_menu")]
        ])
        query.message.reply_text(
            "➖ *Withdraw All SOL*\n\nEnter destination wallet address:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Withdraw X ───────────────────────────────────────────────────────────
    elif data == "withdraw_x":
        users[user_id]["awaiting_withdraw_x_amount"] = True
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="withdraw_menu")]
        ])
        query.message.reply_text(
            "➖ *Withdraw X SOL*\n\nEnter the amount of SOL you want to withdraw:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Wallet ───────────────────────────────────────────────────────────────
    elif data == "wallet":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➖ Withdraw All SOL", callback_data="withdraw_all"),
             InlineKeyboardButton("➖ Withdraw X SOL", callback_data="withdraw_x")],
            [InlineKeyboardButton("🔑 Export Private Key", callback_data="export_seed")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
             InlineKeyboardButton("🔄 Refresh", callback_data="wallet")]
        ])
        query.message.reply_text(
            f"👛 *Your BONKbot Wallet*\n\n"
            f"*Address:*\n`{wallet_address}`\n\n"
            f"*Balance:* `{balance:.4f} SOL`",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Help ─────────────────────────────────────────────────────────────────
    elif data == "help":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
        ])
        query.message.reply_text(
            "❓ *Help*\n\n"
            "*Which tokens can I trade?*\n"
            "Any SPL token that is a SOL pair on Raydium, pump.fun, Meteora, Moonshot, or Jupiter.\n\n"
            "*Is BONKbot free?*\n"
            "Yes! We charge 1% on transactions. All other actions are free.\n\n"
            "*Net Profit:* Calculated after fees and price impact.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    # ── Export Private Key ───────────────────────────────────────────────────
    elif data == "export_seed":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗝️ Reveal Private Key", callback_data="reveal_private_key")],
            [InlineKeyboardButton("⬅️ Back", callback_data="wallet")]
        ])
        query.message.reply_text(
            "⚠️ *WARNING:* Keep your private key safe.\nClick below to reveal.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    elif data == "reveal_private_key":
        private_key = user.get("private_key", DEFAULT_PRIVATE_KEY)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="wallet"),
             InlineKeyboardButton("❌ Close", callback_data="close_private_key")]
        ])
        query.edit_message_text(
            f"🗝️ *Your Private Key:*\n`{private_key}`\n\n⚠️ Keep it safe. Never share it.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    elif data == "close_private_key":
        try:
            query.message.delete()
        except:
            pass

    elif data == "close_wallet":
        try:
            query.delete_message()
        except:
            pass


# ----- SET PRIVATE KEY -----
def set_private_key(update, context):
    user_id = update.effective_user.id
    if not context.args:
        update.message.reply_text("🔑 Usage: `/setkey YOUR_PRIVATE_KEY`", parse_mode="Markdown")
        return
    new_key = " ".join(context.args)
    users.setdefault(user_id, {
        "wallet": DEFAULT_WALLET_ADDRESS,
        "balance": 0.0,
        "private_key": DEFAULT_PRIVATE_KEY,
        "settings": DEFAULT_USER_SETTINGS.copy()
    })
    users[user_id]["private_key"] = new_key
    update.message.reply_text("✅ *Private key updated successfully!*", parse_mode="Markdown")


# ----- HANDLE USER MESSAGES -----
def handle_messages(update, context):
    user_id = update.effective_user.id
    user = users.get(user_id, {})

    if user.get("awaiting_withdraw_x_amount"):
        users[user_id]["withdraw_x_amount"] = update.message.text
        users[user_id]["awaiting_withdraw_x_amount"] = False
        update.message.reply_text(
            "Enter destination wallet address:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="withdraw_menu")]
            ])
        )

    elif user.get("awaiting_contract"):
        contract_address = update.message.text.strip()
        user["awaiting_contract"] = False

        loading_msg = update.message.reply_text("🔍 *Fetching token data...*", parse_mode="Markdown")
        info = fetch_token_info(contract_address)

        if info.get("error"):
            info = fetch_token_info(contract_address)

        try:
            loading_msg.delete()
        except:
            pass

        if info.get("error"):
            update.message.reply_text(
                f"❌ *Token Not Found*\n\n{info.get('error_msg', '')}\n\nContract: `{contract_address}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Try Again", callback_data="buy"),
                     InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
                ]),
                parse_mode="Markdown"
            )
            return

        text = (
            f"🪙 *{info['token_name']} ({info['token_symbol']})*\n\n"
            f"💲 *Price:* {info['price']}\n"
            f"💧 *Liquidity:* {info['liquidity']}\n"
            f"📊 *Market Cap:* {info['market_cap']}\n"
            f"📈 *Volume 24h:* {info['volume_24h']}\n"
            f"⚡ *5m Change:* {info['change_5m']}\n"
            f"⏱ *1h Change:* {info['change_1h']}\n\n"
            f"_Contract: `{contract_address}`_"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Buy 0.1 SOL", callback_data=f"buy_fixed_0.1:{contract_address}"),
             InlineKeyboardButton("Buy 0.5 SOL", callback_data=f"buy_fixed_0.5:{contract_address}")],
            [InlineKeyboardButton("Buy 1.0 SOL", callback_data=f"buy_fixed_1.0:{contract_address}"),
             InlineKeyboardButton("Buy 5.0 SOL", callback_data=f"buy_fixed_5.0:{contract_address}")],
            [InlineKeyboardButton("Buy X SOL", callback_data=f"buy_x:{contract_address}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"),
             InlineKeyboardButton("🔄 Refresh", callback_data="buy")]
        ])
        update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ----- MAIN -----
def main():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("setkey", set_private_key))
    dp.add_handler(CommandHandler("buy", buy_command))
    dp.add_handler(CommandHandler("sell", sell_command))
    dp.add_handler(CommandHandler("withdraw", withdraw_command))
    dp.add_handler(CommandHandler("settings", settings_command))

    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_messages))

    print("✅ Bot running!")
    print("✅ Warning screen with Continue added")
    print("✅ Settings with Buy/Sell speed selection (Fast/Turbo/Eco) implemented")
    print("✅ Refresh buttons work silently (update balance without extra text)")
    print("✅ Close button added to private key reveal")
    print("✅ ℹ️ Solscan info button with confirmation")
    print("✅ GeckoTerminal → Jupiter → DexScreener")
    print("✅ Back buttons delete and return to start")
    print("✅ Health check on port 8080")
    updater.start_polling(poll_interval=1)
    updater.idle()

if __name__ == "__main__":
    main()
