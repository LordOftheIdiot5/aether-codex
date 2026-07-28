"""Telegram launch-safety alert bot.

Continuously watches new EVM launches and posts a risk card to a Telegram channel
as each new token appears — running the same ungameable fork sell-test. Dry-run
(no token set) prints cards to the console so you can see it work first.

Setup to go live:
  1. On Telegram, message @BotFather -> /newbot -> copy the bot token.
  2. Create a channel, add the bot as an admin. Get the channel's chat_id
     (e.g. message @userinfobot, or use @your_channel as the id).
  3. Add to .env:
        TELEGRAM_BOT_TOKEN=123456:ABC...
        TELEGRAM_CHAT_ID=@your_channel
  4. python telegram_bot.py [chains] [interval_secs]     e.g. python telegram_bot.py ethereum,bsc 180

Quick preview (no token needed):
        python telegram_bot.py demo
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import scan_launches as S  # reuse the multi-chain scanning + check engine


def cfg(key, default=""):
    if os.environ.get(key):
        return os.environ[key]
    envf = Path(__file__).resolve().parent.parent / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return default


BOT_TOKEN = cfg("TELEGRAM_BOT_TOKEN")
CHAT_ID = cfg("TELEGRAM_CHAT_ID")
EXPLORERS = {"ethereum": "https://etherscan.io/token/", "bsc": "https://bscscan.com/token/"}


def tg_send(text):
    if not (BOT_TOKEN and CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as exc:
        print("  telegram send failed:", exc)
        return False


def build_card(name, token, chain, key):
    verified = S.is_verified(token, chain["chainid"], key)
    verdict, loss = S.honeypot_check(token, chain)

    # Hard flags = a real, proven trap -> AVOID. Soft flags = caution, not a dealbreaker.
    hard, soft = [], []
    if verdict == "HONEYPOT":
        hard.append("HONEYPOT — CANNOT SELL")
    try:
        if int(loss) >= 3000:
            hard.append(f"HIGH TAX ~{int(loss) // 100}%")
        elif int(loss) >= 1000:
            soft.append(f"elevated tax ~{int(loss) // 100}%")
    except ValueError:
        pass
    if verified is False:
        soft.append("source not verified yet")

    if hard:
        head = "\U0001F6A9 <b>AVOID</b>"
    elif soft:
        head = "⚠️ <b>CAUTION</b>"
    else:
        head = "✅ <b>Looks clean</b>"

    src = "verified" if verified else ("not verified" if verified is False else "unknown")
    sell = f"{verdict}" + (f" (round-trip {loss} bps)" if verdict not in ("HONEYPOT", "ERROR", "UNKNOWN") else "")
    lines = [
        f"{head}  —  new {name.upper()} launch",
        f"<code>{token}</code>",
        f"• sell test: <b>{sell}</b>",
        f"• source: {src}",
    ]
    if hard or soft:
        lines.append("• flags: " + ", ".join(hard + soft))
    lines.append(f'<a href="{EXPLORERS.get(name, "") + token}">explorer</a>  •  <i>DYOR — not financial advice</i>')
    return "\n".join(lines)


def recent_tokens(chain, span, cap):
    """Newest-first wrapped-native-paired tokens from the last `span` blocks (chunked)."""
    out = []
    wnative = chain["wnative"].lower()
    latest = int(S.rpc(chain["rpcs"], "eth_blockNumber", []), 16)
    frm = latest - span
    b = latest
    while b > frm and len(out) < cap:
        lo = max(frm, b - 120 + 1)
        try:
            part = S.rpc(chain["rpcs"], "eth_getLogs", [{
                "address": chain["factory"], "topics": [S.PAIRCREATED],
                "fromBlock": hex(lo), "toBlock": hex(b),
            }])
        except Exception:
            time.sleep(1.0)
            b = lo - 1
            continue
        for lg in reversed(part):
            t0 = "0x" + lg["topics"][1][-40:]
            t1 = "0x" + lg["topics"][2][-40:]
            tok = t0 if t1.lower() == wnative else (t1 if t0.lower() == wnative else None)
            if tok:
                out.append(tok)
            if len(out) >= cap:
                break
        b = lo - 1
        time.sleep(0.2)
    return out


def demo():
    """Preview a single risk card for the latest Ethereum launch (no Telegram needed)."""
    key = S.etherscan_key()
    chain = S.CHAINS["ethereum"]
    toks = recent_tokens(chain, 1500, 1)
    if not toks:
        print("no recent ETH launches found; try again shortly.")
        return
    print("\n--- preview of a card the bot would post ---\n")
    print(build_card("ethereum", toks[0], chain, key))


def run(chains, interval):
    span = 200
    key = S.etherscan_key()
    mode = "LIVE (posting to Telegram)" if (BOT_TOKEN and CHAT_ID) else \
           "DRY-RUN (printing cards; set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to go live)"
    print(f"Launch-safety bot | chains={chains} | every {interval}s | {mode}")

    seen = set()
    first = True
    while True:
        batch = []
        for name in chains:
            chain = S.CHAINS.get(name.strip().lower())
            if not chain:
                continue
            try:
                for tok in recent_tokens(chain, span, 3):
                    if tok not in seen:
                        seen.add(tok)
                        batch.append((name.strip().lower(), chain, tok))
            except Exception as exc:
                print(f"[{name}] fetch failed: {exc}")

        if first:
            print(f"primed {len(seen)} recent launches (silent). watching for new ones...")
            tg_send("\U0001F7E2 Launch-safety bot online — watching new launches.")
            first = False
        else:
            for name, chain, tok in batch:
                card = build_card(name, tok, chain, key)
                print("\n" + card)
                tg_send(card)
        time.sleep(interval)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
        return
    chains = sys.argv[1].split(",") if len(sys.argv) > 1 else ["ethereum", "bsc"]
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    run(chains, interval)


if __name__ == "__main__":
    main()
