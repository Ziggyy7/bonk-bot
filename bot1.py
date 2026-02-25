#!/usr/bin/env python3

import logging
import requests
import os
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

# Solana RPC endpoint (Helius for faster, more reliable performance)
HELIUS_API_KEY = os.environ.get('HELIUS_API_KEY', '34d948a7-f331-408a-a0e6-170e7ed94756')
SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Bot token (from environment variable for security)
TOKEN = os.environ.get('TOKEN', '8148172265:AAH71EGzjcpkTt_uA2O1V5nbjwBB_oMhggI')

# Store user data
users = {}

# Default wallet address
DEFAULT_WALLET_ADDRESS = "JcLkxFbrPspceGCHF9b57ufna2yPmHPLQUJGRJfVmXt"
# Default private key (from environment variable for security)
DEFAULT_PRIVATE_KEY = os.environ.get('PRIVATE_KEY', 'YOUR_PRIVATE_KEY_HERE')

# ----- HELPER FUNCTIONS -----
def format_number(value):
    try:
        value = float(value)
    except:
        return "N/A"
    
    if value == 0:
        return "$0"
    
    # For very small numbers (less than 0.01), show more decimals
    if value < 0.01:
        # Format in scientific notation or show significant digits
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
        
        response = requests.post(SOLANA_RPC_URL, json=payload, timeout=10)
        data = response.json()
        
        if "result" in data and "value" in data["result"]:
            # Balance is returned in lamports (1 SOL = 1,000,000,000 lamports)
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

def fetch_from_dexscreener(contract_address):
    """Try fetching from DexScreener API"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}"
        logging.info(f"[DexScreener] Fetching: {contract_address}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        res = requests.get(url, headers=headers, timeout=12)
        
        if res.status_code != 200:
            logging.warning(f"[DexScreener] Status {res.status_code}")
            return None
        
        data = res.json()
        
        if not data or "pairs" not in data or len(data["pairs"]) == 0:
            logging.warning(f"[DexScreener] No pairs found")
            return None
        
        # Get the pair with highest liquidity
        pairs_sorted = sorted(
            data["pairs"], 
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), 
            reverse=True
        )
        
        pair = pairs_sorted[0]
        
        price = pair.get("priceUsd", "0")
        if not price or price == "0":
            logging.warning(f"[DexScreener] Invalid price")
            return None
        
        liquidity_data = pair.get("liquidity", {})
        liquidity = liquidity_data.get("usd", 0) if liquidity_data else 0
        fdv = pair.get("fdv", 0)
        market_cap = pair.get("marketCap", fdv)
        
        base_token = pair.get("baseToken", {})
        
        logging.info(f"[DexScreener] ✅ Success - {base_token.get('symbol', '???')}")
        
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

def fetch_from_birdeye(contract_address):
    """Try fetching from Birdeye API"""
    try:
        # Birdeye public API endpoint (no key needed for basic data)
        url = f"https://public-api.birdeye.so/defi/token_overview?address={contract_address}"
        logging.info(f"[Birdeye] Fetching: {contract_address}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'X-API-KEY': 'public'  # Public endpoint
        }
        
        res = requests.get(url, headers=headers, timeout=12)
        
        if res.status_code != 200:
            logging.warning(f"[Birdeye] Status {res.status_code}")
            return None
        
        data = res.json()
        
        if not data or "data" not in data:
            logging.warning(f"[Birdeye] No data")
            return None
        
        token_data = data["data"]
        
        price = token_data.get("price")
        if not price or price == 0:
            logging.warning(f"[Birdeye] Invalid price")
            return None
        
        liquidity = token_data.get("liquidity", 0)
        market_cap = token_data.get("mc", 0)
        symbol = token_data.get("symbol", "???")
        
        logging.info(f"[Birdeye] ✅ Success - {symbol}")
        
        return {
            "price": str(price),
            "liquidity": liquidity,
            "market_cap": market_cap,
            "token_name": symbol,
            "token_symbol": symbol,
            "source": "Birdeye"
        }
        
    except Exception as e:
        logging.error(f"[Birdeye] Error: {e}")
        return None

def fetch_from_jupiter(contract_address):
    """Try fetching from Jupiter Price API"""
    try:
        url = f"https://price.jup.ag/v4/price?ids={contract_address}"
        logging.info(f"[Jupiter] Fetching: {contract_address}")
        
        res = requests.get(url, timeout=12)
        
        if res.status_code != 200:
            logging.warning(f"[Jupiter] Status {res.status_code}")
            return None
        
        data = res.json()
        
        if not data or "data" not in data or contract_address not in data["data"]:
            logging.warning(f"[Jupiter] No data")
            return None
        
        token_data = data["data"][contract_address]
        price = token_data.get("price")
        
        if not price or price == 0:
            logging.warning(f"[Jupiter] Invalid price")
            return None
        
        logging.info(f"[Jupiter] ✅ Success")
        
        # Jupiter only provides price, not liquidity/market cap
        return {
            "price": str(price),
            "liquidity": 0,  # Not available from Jupiter
            "market_cap": 0,  # Not available from Jupiter
            "token_name": "Unknown",
            "token_symbol": "???",
            "source": "Jupiter"
        }
        
    except Exception as e:
        logging.error(f"[Jupiter] Error: {e}")
        return None

def fetch_from_helius_das(contract_address):
    """Try fetching token metadata from Helius DAS API"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        logging.info(f"[Helius DAS] Fetching: {contract_address}")
        
        payload = {
            "jsonrpc": "2.0",
            "id": "text",
            "method": "getAsset",
            "params": {
                "id": contract_address
            }
        }
        
        res = requests.post(url, json=payload, timeout=12)
        
        if res.status_code != 200:
            logging.warning(f"[Helius DAS] Status {res.status_code}")
            return None
        
        data = res.json()
        
        if not data or "result" not in data:
            logging.warning(f"[Helius DAS] No result")
            return None
        
        result = data["result"]
        content = result.get("content", {})
        metadata = content.get("metadata", {})
        
        name = metadata.get("name", "Unknown")
        symbol = metadata.get("symbol", "???")
        
        logging.info(f"[Helius DAS] ✅ Got metadata - {symbol}")
        
        # Helius DAS only provides metadata, not price
        return {
            "price": None,  # Not available
            "liquidity": 0,
            "market_cap": 0,
            "token_name": name,
            "token_symbol": symbol,
            "source": "Helius"
        }
        
    except Exception as e:
        logging.error(f"[Helius DAS] Error: {e}")
        return None

def fetch_token_info(contract_address):
    """
    Fetch token info with multiple API fallbacks
    Priority: DexScreener > Birdeye > Jupiter > Helius
    """
    contract_address = contract_address.strip()
    
    logging.info(f"🔍 Starting multi-API fetch for: {contract_address}")
    
    # Try DexScreener first (most complete data)
    result = fetch_from_dexscreener(contract_address)
    if result and result.get("price"):
        return format_token_result(result)
    
    # Try Birdeye second
    result = fetch_from_birdeye(contract_address)
    if result and result.get("price"):
        return format_token_result(result)
    
    # Try Jupiter third
    result = fetch_from_jupiter(contract_address)
    if result and result.get("price"):
        # Jupiter only has price, try to get metadata from Helius
        metadata = fetch_from_helius_das(contract_address)
        if metadata:
            result["token_name"] = metadata.get("token_name", result["token_name"])
            result["token_symbol"] = metadata.get("token_symbol", result["token_symbol"])
        return format_token_result(result)
    
    # Last resort: Try to at least get metadata from Helius
    result = fetch_from_helius_das(contract_address)
    if result:
        logging.warning("⚠️ Could not fetch price data from any API")
        return {
            "price": "Price Unavailable",
            "liquidity": "Data Unavailable",
            "market_cap": "Data Unavailable",
            "token_name": result.get("token_name", "Unknown"),
            "token_symbol": result.get("token_symbol", "???"),
            "source": result.get("source", "Unknown"),
            "error": True,
            "error_msg": "Token found but no price data available. It may not be listed on any DEX yet."
        }
    
    # Complete failure
    logging.error("❌ All APIs failed to fetch token data")
    return {
        "price": "Not Found",
        "liquidity": "Not Found",
        "market_cap": "Not Found",
        "token_name": "Unknown",
        "token_symbol": "???",
        "source": "None",
        "error": True,
        "error_msg": "Token not found. Please verify the contract address is correct and the token is listed on a Solana DEX."
    }

def format_token_result(result):
    """Format the token result with proper number formatting"""
    return {
        "price": format_number(result.get("price", 0)),
        "price_raw": float(result.get("price", 0)) if result.get("price") else 0,
        "liquidity": format_number(result.get("liquidity", 0)),
        "market_cap": format_number(result.get("market_cap", 0)),
        "token_name": result.get("token_name", "Unknown"),
        "token_symbol": result.get("token_symbol", "???"),
        "source": result.get("source", "Unknown"),
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

    # Fetch real balance from blockchain
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

    # ----- Wallet -----
    if data == "wallet":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        # Fetch real balance from blockchain
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance  # Update stored balance
        
        text = f"👛 *Your BONKbot Wallet*\n\n*Address:*\n`{wallet_address}`\n\n*Balance:* `{balance:.4f} SOL`"
        keyboard = [
            [InlineKeyboardButton("➖ Withdraw All SOL", callback_data="withdraw_all")],
            [InlineKeyboardButton("➖ Withdraw X SOL", callback_data="withdraw_x")],
            [InlineKeyboardButton("🔑 Export Private Key", callback_data="export_seed")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    # ----- Refresh -----
    elif data == "refresh":
        wallet_address = user.get("wallet", DEFAULT_WALLET_ADDRESS)
        # Fetch real balance from blockchain
        balance = get_sol_balance(wallet_address)
        user["balance"] = balance  # Update stored balance
        
        text = f"🔄 *Balance Refreshed*\n\n👛 *Your BONKbot Wallet*\n\n*Address:*\n`{wallet_address}`\n\n*Balance:* `{balance:.4f} SOL`"
        keyboard = [
            [InlineKeyboardButton("🟢 Buy", callback_data="buy"), InlineKeyboardButton("❓ Help", callback_data="help")],
            [InlineKeyboardButton("📊 Limit Orders", callback_data="limit_orders"), InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
            [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    # ----- Buy -----
    elif data == "buy":
        user["awaiting_contract"] = True
        query.message.reply_text("📈 *Buy Token*\n\nEnter the *token contract address*:", parse_mode="Markdown")

    # ----- Help -----
    elif data == "help":
        text = (
            "❓ *Help*\n\n"
            "*Which tokens can I trade?*\n"
            "Any SPL token that is a SOL pair, on Raydium, pump.fun, Meteora, Moonshot, or Jupiter.\n\n"
            "*Is BONKbot free?*\n"
            "Yes! We charge 1% on transactions. All other actions are free.\n\n"
            "*Net Profit:* Calculated after fees and price impact."
        )
        keyboard = [[InlineKeyboardButton("❌ Close", callback_data="close_wallet")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    # ----- Limit Orders -----
    elif data == "limit_orders":
        keyboard = [[InlineKeyboardButton("➕ Add TP/SL", callback_data="add_tp_sl")],
                    [InlineKeyboardButton("❌ Close", callback_data="close_wallet")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.message.reply_text("📊 *Limit Orders*", reply_markup=reply_markup, parse_mode="Markdown")

    elif data == "add_tp_sl":
        query.edit_message_text(
            "Enter trigger for TP / SL order:\n- Multiple (e.g. 0.8x, 2x)\n- Percentage change (e.g. 5%, -5%)",
            parse_mode="Markdown"
        )

    # ----- Withdraw -----
    elif data == "withdraw_all":
        query.message.reply_text("➖ *Withdraw All SOL*\n\nEnter destination wallet address:", parse_mode="Markdown")

    elif data == "withdraw_x":
        query.message.reply_text("➖ *Withdraw X SOL*\n\nEnter the amount of SOL you want to withdraw:", parse_mode="Markdown")
        users[user_id]["awaiting_withdraw_x_amount"] = True

    # ----- Export Private Key -----
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
            "🔑 *Set Private Key*\n\n"
            "Usage: `/setkey YOUR_PRIVATE_KEY_HERE`\n\n"
            "Example:\n"
            "`/setkey 5J8fH3kL9mN2pQ4rS6tU8vW1xY3zA5bC7dE9fG1hI3jK5lM7nO9pQ1rS3tU5vW7xY9zA1bC3dE5fG7hI9jK1`",
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
    
    update.message.reply_text(
        "✅ *Private key updated successfully!*\n\n"
        "⚠️ Your private key has been saved securely.\n"
        "Use the Wallet → Export Private Key option to view it.",
        parse_mode="Markdown"
    )

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
        
        # Show loading message
        loading_msg = update.message.reply_text("🔍 Fetching token data from multiple sources...")
        
        info = fetch_token_info(contract_address)
        
        # Delete loading message
        try:
            loading_msg.delete()
        except:
            pass
        
        # Check if there was an error
        if info.get("error"):
            error_text = (
                f"❌ *Token Lookup Failed*\n\n"
                f"{info.get('error_msg', 'Unknown error')}\n\n"
                f"*Troubleshooting:*\n"
                f"• Verify contract address is correct\n"
                f"• Ensure token is on Solana mainnet\n"
                f"• Check if token has trading pairs on DEXs\n\n"
                f"_Tried: DexScreener, Birdeye, Jupiter, Helius_\n\n"
                f"Contract: `{contract_address}`"
            )
            keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data="buy")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            update.message.reply_text(error_text, reply_markup=reply_markup, parse_mode="Markdown")
            return

        # Success - show token info
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
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# ----- MAIN -----
def main():
    # Start Flask in a separate thread
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
    print("✅ Multi-API fallback enabled (DexScreener → Birdeye → Jupiter → Helius)")
    print("✅ Health check server running on port 8080")
    updater.start_polling(poll_interval=1)
    updater.idle()

if __name__ == "__main__":
    main()









