"""Live new-launch scanner (MVP).

Watches the Uniswap V2 factory for freshly created pairs, pulls the new token out
of each, and runs the fork-based safety check on it — producing a risk card per
launch. This is the "see new launches -> auto-check them" pipeline.

Honest scope: surfaces FACTS (verified source? sellable? hidden tax?), not price
predictions. Stdlib only — no pip installs.

Run:  python scan_launches.py [block_span] [max_tokens]
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

# JSON-RPC endpoints for the monitor (the fork check uses drpc via foundry.toml).
# Tried in order; browser UA avoids 403s from gateways that block default urllib.
RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
    "https://rpc.mevblocker.io",
]
FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower()
# keccak256("PairCreated(address,address,address,uint256)")
PAIRCREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
FORK_LAB = Path(__file__).resolve().parent


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    last = None
    for url in RPCS:
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


def is_verified(token, key):
    if not key:
        return None
    url = ("https://api.etherscan.io/v2/api?chainid=1&module=contract&action=getsourcecode"
           f"&address={token}&apikey={key}")
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            d = json.loads(r.read())
        res = (d.get("result") or [{}])[0]
        return bool(res.get("SourceCode"))
    except Exception:
        return None


def honeypot_check(token):
    """Invoke the fork-based checker for one token; return (verdict, loss_bps)."""
    env = dict(os.environ)
    env["CHECK_TOKEN"] = token
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
                return (payload[1], payload[2])  # verdict, lossBps
    return ("UNKNOWN", "?")


def main():
    span = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    latest = int(rpc("eth_blockNumber", []), 16)
    frm = latest - span
    logs = rpc("eth_getLogs", [{
        "address": FACTORY, "topics": [PAIRCREATED],
        "fromBlock": hex(frm), "toBlock": "latest",
    }])
    print("=" * 64)
    print(f"  LAUNCH SCANNER  |  Uniswap V2 pairs, blocks {frm}..{latest}")
    print(f"  {len(logs)} new pair(s) in the last {span} blocks")
    print("=" * 64)

    key = etherscan_key()
    checked = 0
    for lg in reversed(logs):  # newest first
        token0 = "0x" + lg["topics"][1][-40:]
        token1 = "0x" + lg["topics"][2][-40:]
        if token1.lower() == WETH:
            token = token0
        elif token0.lower() == WETH:
            token = token1
        else:
            continue  # not a WETH pair — skip
        checked += 1
        if checked > max_tokens:
            break

        print(f"\n[{checked}] New launch: {token}")
        v = is_verified(token, key)
        print(f"    verified source : {'yes' if v else ('NO' if v is False else '?')}")
        verdict, loss = honeypot_check(token)
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
        print("\n(No WETH-paired launches in this window — widen the block span.)")


if __name__ == "__main__":
    main()
