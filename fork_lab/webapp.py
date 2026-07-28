"""Local dashboard server for the launch-safety scanner.

Serves index.html and a tiny JSON API that reuses the scan engine:
  GET /api/check?chain=ethereum&token=0x...   -> risk assessment for one token
  GET /api/scan?chain=ethereum&count=3        -> assess the N latest launches

Stdlib only. Run:  python webapp.py [port]   then open http://localhost:8000
"""
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scan_launches as S

HERE = Path(__file__).resolve().parent
EXPLORERS = {"ethereum": "https://etherscan.io/token/", "bsc": "https://bscscan.com/token/"}
KEY = S.etherscan_key()


def recent_tokens(chain, span, cap):
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


def _eth_call(chain, to, data):
    try:
        return S.rpc(chain["rpcs"], "eth_call", [{"to": to, "data": data}, "latest"])
    except Exception:
        return "0x"


def _pad(addr):
    return addr.lower().replace("0x", "").rjust(64, "0")


def _dec_string(hexdata):
    if not hexdata or hexdata == "0x":
        return ""
    try:
        b = bytes.fromhex(hexdata[2:])
        if len(b) >= 64 and int.from_bytes(b[:32], "big") == 32:
            length = int.from_bytes(b[32:64], "big")
            s = b[64:64 + length].decode("utf-8", "replace").strip("\x00")
            if s:
                return s
        return b[:32].rstrip(b"\x00").decode("utf-8", "replace").strip("\x00")  # bytes32 fallback
    except Exception:
        return ""


def token_meta(token, chain):
    name = _dec_string(_eth_call(chain, token, "0x06fdde03"))     # name()
    symbol = _dec_string(_eth_call(chain, token, "0x95d89b41"))   # symbol()
    return name.strip()[:40], symbol.strip()[:16]


def liquidity_native(token, chain):
    """Wrapped-native reserve in the token's DEX pair (in ETH/BNB units)."""
    try:
        pairhex = _eth_call(chain, chain["factory"], "0xe6a43905" + _pad(token) + _pad(chain["wnative"]))
        pair = "0x" + pairhex[-40:]
        if int(pair, 16) == 0:
            return None
        res = _eth_call(chain, pair, "0x0902f1ac")  # getReserves()
        b = bytes.fromhex(res[2:])
        if len(b) < 64:
            return None
        r0 = int.from_bytes(b[0:32], "big")
        r1 = int.from_bytes(b[32:64], "big")
        token0 = "0x" + _eth_call(chain, pair, "0x0dfe1681")[-40:]  # token0()
        native = r0 if token0.lower() == chain["wnative"].lower() else r1
        return native / 1e18
    except Exception:
        return None


def assess(token, chain_name):
    chain = S.CHAINS[chain_name]
    native_sym = "BNB" if chain_name == "bsc" else "ETH"
    verified = S.is_verified(token, chain["chainid"], KEY)
    name, symbol = token_meta(token, chain)
    liq = liquidity_native(token, chain)
    verdict, loss = S.honeypot_check(token, chain)

    hard, soft = [], []
    if verdict == "HONEYPOT":
        hard.append("Honeypot — cannot sell")
    elif verdict == "NOBUY":
        soft.append("No liquidity / not tradeable yet")
    try:
        lv = int(loss)
        if lv >= 3000:
            hard.append(f"High tax ~{lv // 100}%")
        elif lv >= 1000:
            soft.append(f"Elevated tax ~{lv // 100}%")
    except (TypeError, ValueError):
        pass
    if liq is not None and liq < (5 if chain_name == "bsc" else 1):
        soft.append(f"Low liquidity (~{liq:.2f} {native_sym})")
    if verified is False:
        soft.append("Source not verified")

    tier = "avoid" if hard else ("caution" if soft else "clean")
    return {
        "token": token, "chain": chain_name, "name": name, "symbol": symbol,
        "verified": verified, "verdict": verdict, "loss": loss,
        "liquidity": liq, "native": native_sym, "tier": tier,
        "flags": hard + soft, "explorer": EXPLORERS.get(chain_name, "") + token,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                self._send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            except Exception as exc:
                self._send(f"index.html missing: {exc}".encode(), "text/plain", 500)
            return
        if u.path == "/api/check":
            chain = q.get("chain", ["ethereum"])[0]
            token = q.get("token", [""])[0].strip()
            if chain not in S.CHAINS or not token.startswith("0x") or len(token) != 42:
                return self._json({"error": "give a valid 0x token address + chain"}, 400)
            try:
                return self._json(assess(token, chain))
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        if u.path == "/api/scan":
            chain = q.get("chain", ["ethereum"])[0]
            count = max(1, min(5, int(q.get("count", ["3"])[0])))
            if chain not in S.CHAINS:
                return self._json({"error": "unknown chain"}, 400)
            try:
                toks = recent_tokens(S.CHAINS[chain], 3000, count)
                return self._json([assess(t, chain) for t in toks])
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Launch-safety dashboard running:  http://localhost:{port}")
    print("(each check runs a real fork sell-test, so results take a few seconds)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
