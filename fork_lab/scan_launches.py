"""Live multi-chain new-launch scanner (MVP).

Watches Uniswap-V2-style factories across EVM chains for freshly created pairs,
pulls the new token out of each, and runs the fork-based safety check on it —
producing a risk card per launch. "See new launches -> auto-check them", now
across Ethereum + BSC (where the honeypots actually are).

Honest scope: surfaces FACTS (verified source? sellable? hidden tax?), not price
predictions. Stdlib only — no pip installs.

Run:  python scan_launches.py [span] [max_per_chain] [chain1,chain2]
Ex:   python scan_launches.py 400 2 bsc
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

FORK_LAB = Path(__file__).resolve().parent
# keccak256("PairCreated(address,address,address,uint256)") — same on every V2 fork.
PAIRCREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

CHAINS = {
    "ethereum": {
        "chainid": 1,
        "rpcs": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"],
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",   # Uniswap V2
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "wnative": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",   # WETH
        "fork": "mainnet",
    },
    "bsc": {
        "chainid": 56,
        "rpcs": [
            "https://bsc-dataseed.binance.org",
            "https://bsc-dataseed1.defibit.io",
            "https://bsc-dataseed1.ninicoin.io",
            "https://bsc-rpc.publicnode.com",
            "https://bsc.drpc.org",
        ],
        "factory": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",   # PancakeSwap V2
        "router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "wnative": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",   # WBNB
        "fork": "bsc",
    },
}


def rpc(rpcs, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    last = None
    for url in rpcs:
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as r:
                out = json.loads(r.read())
            if "error" in out:
                last = out["error"]
                continue
            return out["result"]
        except Exception as exc:
            last = exc
            continue
    raise RuntimeError(f"all RPCs failed for {method}: {last}")


def etherscan_key():
    envf = FORK_LAB.parent / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if line.startswith("ETHERSCAN_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def is_verified(token, chainid, key):
    if not key:
        return None
    url = (f"https://api.etherscan.io/v2/api?chainid={chainid}&module=contract"
           f"&action=getsourcecode&address={token}&apikey={key}")
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            d = json.loads(r.read())
        res = (d.get("result") or [{}])[0]
        return bool(res.get("SourceCode"))
    except Exception:
        return None


def honeypot_check(token, chain):
    env = dict(os.environ)
    env["CHECK_TOKEN"] = token
    env["CHECK_FORK"] = chain["fork"]
    env["CHECK_ROUTER"] = chain["router"]
    env["CHECK_WNATIVE"] = chain["wnative"]
    try:
        p = subprocess.run(
            ["forge", "test", "--match-test", "test_checkEnvToken", "-vv"],
            cwd=str(FORK_LAB), capture_output=True, text=True, timeout=180, env=env,
        )
    except Exception as exc:
        return ("ERROR", str(exc))
    for line in (p.stdout + p.stderr).splitlines():
        if "SCANRESULT:" in line:
            payload = line.split("SCANRESULT:", 1)[1].strip().split()
            if len(payload) >= 3:
                return (payload[1], payload[2])
    return ("UNKNOWN", "?")


def iter_new_tokens(chain, span, chunk=150):
    """Yield freshly-launched wrapped-native-paired tokens, newest first, fetching
    logs in small chunks to stay under public-RPC getLogs range/rate limits."""
    import time
    wnative = chain["wnative"].lower()
    latest = int(rpc(chain["rpcs"], "eth_blockNumber", []), 16)
    frm = latest - span
    print("\n" + "=" * 64)
    print(f"  {chain_label}  |  scanning blocks {frm}..{latest}")
    print("=" * 64)
    b = latest
    while b > frm:
        lo = max(frm, b - chunk + 1)
        try:
            part = rpc(chain["rpcs"], "eth_getLogs", [{
                "address": chain["factory"], "topics": [PAIRCREATED],
                "fromBlock": hex(lo), "toBlock": hex(b),
            }])
        except Exception:
            time.sleep(1.0)
            b = lo - 1
            continue
        for lg in reversed(part):
            token0 = "0x" + lg["topics"][1][-40:]
            token1 = "0x" + lg["topics"][2][-40:]
            if token1.lower() == wnative:
                yield token0
            elif token0.lower() == wnative:
                yield token1
        b = lo - 1
        time.sleep(0.3)


chain_label = ""


def scan_chain(name, chain, span, max_tokens, key):
    global chain_label
    chain_label = name.upper()
    checked = 0
    for token in iter_new_tokens(chain, span):
        checked += 1
        if checked > max_tokens:
            break
        print(f"\n[{name} {checked}] {token}")
        v = is_verified(token, chain["chainid"], key)
        print(f"    verified source : {'yes' if v else ('NO' if v is False else '?')}")
        verdict, loss = honeypot_check(token, chain)
        print(f"    live sell test  : {verdict}  (round-trip loss bps: {loss})")

        flags = []
        if v is False:
            flags.append("UNVERIFIED")
        if verdict == "HONEYPOT":
            flags.append("HONEYPOT-CANT-SELL")
        try:
            if int(loss) >= 2000:
                flags.append(f"HIGH-TAX~{int(loss) // 100}%")
        except ValueError:
            pass
        card = "AVOID" if flags else "no obvious traps (still DYOR)"
        print(f"    >>> {card}" + (f"   [{', '.join(flags)}]" if flags else ""))

    if checked == 0:
        print("  (no wrapped-native pairs found in this window — widen the span)")


def main():
    span = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    which = sys.argv[3].split(",") if len(sys.argv) > 3 else list(CHAINS)

    key = etherscan_key()
    for name in which:
        chain = CHAINS.get(name.strip().lower())
        if not chain:
            print(f"(unknown chain '{name}', skipping)")
            continue
        try:
            scan_chain(name.strip().lower(), chain, span, max_tokens, key)
        except Exception as exc:
            print(f"\n[{name}] scan failed: {exc}")


if __name__ == "__main__":
    main()
