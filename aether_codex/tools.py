"""Shared tools that agents can call.

Each tool is a plain LangChain `@tool` function. `TOOLBOX` maps tool names to
implementations so the Director can hand tools to dynamically spawned agents
by name.
"""

from __future__ import annotations

import contextlib
import io
import math
import traceback
from pathlib import Path

from langchain_core.tools import tool

from .config import AUDIT_DIR, POC_DIR, REPORTS_DIR, settings

# ddgs is the maintained successor of the duckduckgo_search package;
# accept either so older installs keep working.
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web (DuckDuckGo) and return the top results with titles,
    URLs and snippets. Use focused queries; call multiple times for
    different angles of a topic."""
    if DDGS is None:
        return "Web search unavailable: the 'ddgs' package is not installed."
    try:
        with DDGS() as ddg:
            results = list(ddg.text(query, max_results=max_results))
    except Exception as exc:
        return f"Search failed ({exc}). Try again with a simpler query."
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        url = r.get("href") or r.get("link") or ""
        lines.append(f"[{i}] {r.get('title', '')}\n{url}\n{r.get('body', '')}")
    return "\n\n".join(lines)


@tool
def run_python(code: str) -> str:
    """Execute Python code for calculations and quick simulations and return
    what it prints. `math` and `numpy` (as `np`) are pre-imported. Always
    print() your results, including units. Example:
    print(f"Heat loss: {U * A * dT:.0f} W")"""
    # NOTE: this executes in-process and is NOT a full sandbox. It is fine for
    # a local prototype driven by your own prompts. Do not expose the app
    # publicly without further isolation.
    safe_builtins = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "float": float, "int": int, "len": len,
        "list": list, "max": max, "min": min, "pow": pow, "print": print,
        "range": range, "round": round, "sorted": sorted, "str": str,
        "sum": sum, "tuple": tuple, "zip": zip,
    }
    env: dict = {"__builtins__": safe_builtins, "math": math}
    try:
        import numpy as np
        env["np"] = np
    except ImportError:
        pass

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exec(code, env)  # noqa: S102 — deliberate, see note above
    except Exception:
        return "Error while running code:\n" + traceback.format_exc(limit=3)
    output = buffer.getvalue().strip()
    if not output and "result" in env:
        output = repr(env["result"])
    return output or "(The code ran but printed nothing — use print() to return values.)"


@tool
def write_report_file(filename: str, content: str) -> str:
    """Write a markdown/text file into the reports/ directory. `filename`
    must be a plain file name like 'thermal_storage_report.md' (no paths)."""
    safe_name = Path(filename).name  # strips any directory components
    if not safe_name:
        return "Invalid filename."
    path = REPORTS_DIR / safe_name
    path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


@tool
def read_report_file(filename: str) -> str:
    """Read a previously written file from the reports/ directory."""
    path = REPORTS_DIR / Path(filename).name
    if not path.exists():
        existing = ", ".join(p.name for p in REPORTS_DIR.iterdir()) or "(none)"
        return f"No such file. Existing report files: {existing}"
    return path.read_text(encoding="utf-8")


def _resolve_in_audit_dir(rel_path: str) -> Path | None:
    """Resolve rel_path under AUDIT_DIR, refusing anything that escapes it
    (path traversal, absolute paths, symlinks pointing outside)."""
    base = AUDIT_DIR.resolve()
    candidate = (base / rel_path).resolve()
    if candidate == base or base in candidate.parents:
        return candidate
    return None


# Text/code file extensions worth listing to an audit agent.
_SOURCE_EXTS = {".sol", ".vy", ".rs", ".move", ".cairo", ".ts", ".js", ".py",
                ".go", ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".cfg"}


@tool
def list_source_files(subdir: str = "") -> str:
    """List source files available to audit, under the audit_target/ directory.
    `subdir` optionally narrows to a folder. Use this to discover the codebase
    before reading individual files with read_source_file."""
    root = _resolve_in_audit_dir(subdir)
    if root is None or not root.exists():
        return (f"No such folder under audit_target/. Put the code to audit in "
                f"{AUDIT_DIR} (or a subfolder) first.")
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _SOURCE_EXTS
        and ".git" not in p.parts and "node_modules" not in p.parts
    )
    if not files:
        return f"No source files found under audit_target/{subdir}."
    lines = []
    for p in files[:400]:
        rel = p.relative_to(AUDIT_DIR.resolve())
        lines.append(f"{rel.as_posix()} ({p.stat().st_size} bytes)")
    note = "" if len(files) <= 400 else f"\n(+{len(files) - 400} more, narrow with subdir)"
    return "\n".join(lines) + note


@tool
def read_source_file(path: str) -> str:
    """Read a source file from the audit_target/ directory (the code under
    audit). `path` is relative to audit_target/, e.g.
    'src/Vault.sol'. Returns the file with line numbers so you can cite exact
    lines in findings."""
    resolved = _resolve_in_audit_dir(path)
    if resolved is None:
        return "Invalid path: reads are confined to the audit_target/ directory."
    if not resolved.is_file():
        return f"No such file: audit_target/{path}. Use list_source_files first."
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read file ({exc})."
    lines = text.splitlines()
    if len(lines) > 1500:
        body = "\n".join(f"{i:>4} | {ln}" for i, ln in enumerate(lines[:1500], 1))
        return body + f"\n... (truncated at 1500 of {len(lines)} lines)"
    return "\n".join(f"{i:>4} | {ln}" for i, ln in enumerate(lines, 1))


def _resolve_in(base: Path, rel_path: str) -> Path | None:
    """Resolve rel_path under base, refusing anything that escapes it."""
    root = base.resolve()
    candidate = (root / rel_path).resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


@tool
def write_poc_file(path: str, content: str) -> str:
    """Write a Solidity file into the isolated PoC Foundry project
    (poc_workspace/). Put the vulnerable target (a faithful copy or minimal
    reproduction) under 'src/…' and your exploit test under 'test/…' with a
    .t.sol name, e.g. path='test/ReentrancyExploit.t.sol'. Then run it with
    run_forge_test. Overwrites on repeat so you can iterate."""
    resolved = _resolve_in(POC_DIR, path)
    if resolved is None:
        return "Invalid path: writes are confined to the poc_workspace/ directory."
    if resolved.suffix.lower() != ".sol":
        return "Only .sol files can be written here."
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to poc_workspace/{Path(path).as_posix()}"


@tool
def run_forge_test(match: str = "") -> str:
    """Compile and RUN the exploit PoCs in poc_workspace/ with Foundry and
    return the result (pass/fail, assertions, and call traces on failure).
    `match` optionally filters to a test name or contract (forge
    --match-test / --match-contract). Use this to PROVE an exploit works and
    to iterate on compile errors and failing assertions until it passes.
    A finding's PoC is only credible once this reports the exploit test
    PASSING."""
    import shutil
    import subprocess

    forge = shutil.which("forge")
    if forge is None:
        return ("Foundry's `forge` was not found on PATH. Install it "
                "(https://getfoundry.sh) or ensure ~/.foundry/bin is on PATH.")

    cmd = [forge, "test"]
    if match.strip():
        # A capitalized token is likely a contract name; otherwise a test name.
        flag = "--match-contract" if match.strip()[0].isupper() else "--match-test"
        cmd += [flag, match.strip()]
    try:
        proc = subprocess.run(
            cmd, cwd=str(POC_DIR), capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return ("forge test timed out after 600s. If this was an invariant/fuzz "
                "run, lower the invariant runs/depth or narrow the handler.")
    except Exception as exc:
        return f"Could not run forge ({exc})."

    output = (proc.stdout or "") + (proc.stderr or "")
    verdict = "PASSED ✅" if proc.returncode == 0 else "FAILED / did not compile ❌"
    if len(output) > 12000:
        output = output[:12000] + "\n... (output truncated)"
    return f"forge test {verdict} (exit {proc.returncode})\n\n{output}"


# Etherscan V2 uses one key across chains; map friendly names to chain IDs.
_EXPLORER_CHAINS = {
    "ethereum": 1, "eth": 1, "mainnet": 1,
    "bsc": 56, "bnb": 56, "binance": 56,
    "polygon": 137, "matic": 137,
    "arbitrum": 42161, "arb": 42161,
    "base": 8453,
    "optimism": 10, "op": 10,
    "avalanche": 43114, "avax": 43114,
}


def _decode_verified_source(raw: str) -> str:
    """Etherscan returns single-file source as plain Solidity, and multi-file
    source as a double-brace-wrapped standard-JSON object. Normalize both to a
    single concatenated, line-numbered string for the agent to read/cite."""
    import json as _json

    text = raw
    if raw.startswith("{{") and raw.endswith("}}"):
        try:
            meta = _json.loads(raw[1:-1])  # strip one brace layer
            sources = meta.get("sources", {})
            parts = []
            for path, entry in sources.items():
                content = entry.get("content", "")
                parts.append(f"// ===== FILE: {path} =====\n{content}")
            text = "\n\n".join(parts) if parts else raw
        except (ValueError, AttributeError):
            text = raw

    lines = text.splitlines()
    if len(lines) > 1800:
        body = "\n".join(f"{i:>4} | {ln}" for i, ln in enumerate(lines[:1800], 1))
        return body + f"\n... (truncated at 1800 of {len(lines)} lines)"
    return "\n".join(f"{i:>4} | {ln}" for i, ln in enumerate(lines, 1))


@tool
def fetch_contract_source(address: str, chain: str = "ethereum") -> str:
    """Fetch a DEPLOYED smart contract's verified source code and metadata from
    a block explorer, by on-chain address. Use this to audit or rug-screen any
    live token/contract. `chain` is one of: ethereum, bsc, polygon, arbitrum,
    base, optimism, avalanche. Returns contract name, compiler, proxy status
    (and implementation address if it is a proxy), deployer address, and the
    full source with line numbers for exact citations."""
    import json as _json
    import re
    import urllib.parse
    import urllib.request

    addr = address.strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return f"Invalid address '{address}'. Expected a 0x-prefixed 40-hex address."
    chain_id = _EXPLORER_CHAINS.get(chain.strip().lower())
    if chain_id is None:
        return (f"Unknown chain '{chain}'. Supported: "
                f"{', '.join(sorted(set(_EXPLORER_CHAINS)))}.")
    if not settings.etherscan_api_key:
        return ("No block-explorer API key configured. Get a free key at "
                "https://etherscan.io/apis and add ETHERSCAN_API_KEY=... to your "
                ".env file (one Etherscan key works across all supported chains).")

    def _call(params: dict) -> dict:
        url = "https://api.etherscan.io/v2/api?" + urllib.parse.urlencode({
            "chainid": chain_id, "apikey": settings.etherscan_api_key, **params,
        })
        with urllib.request.urlopen(url, timeout=25) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    try:
        data = _call({"module": "contract", "action": "getsourcecode", "address": addr})
    except Exception as exc:
        return f"Explorer request failed ({exc}). Check the address, chain and key."

    if str(data.get("status")) != "1" or not data.get("result"):
        return (f"Explorer error: {data.get('result') or data.get('message')}. "
                f"The key may be invalid or rate-limited.")

    info = data["result"][0]
    if not info.get("SourceCode"):
        return (f"Contract {addr} on {chain} is NOT verified — no source available. "
                f"Treat unverified contracts as high-risk; only bytecode analysis "
                f"is possible.")

    # Best-effort deployer lookup (non-fatal if it fails).
    deployer = "unknown"
    try:
        created = _call({"module": "contract", "action": "getcontractcreation",
                         "contractaddresses": addr})
        if str(created.get("status")) == "1" and created.get("result"):
            deployer = created["result"][0].get("contractCreator", "unknown")
    except Exception:
        pass

    is_proxy = str(info.get("Proxy", "0")) == "1"
    header = [
        f"# Contract: {info.get('ContractName') or '(unnamed)'}",
        f"Address:  {addr}  ({chain}, chainid {chain_id})",
        f"Compiler: {info.get('CompilerVersion', '?')}",
        f"Deployer: {deployer}",
        f"Proxy:    {'YES — logic can be swapped by admin' if is_proxy else 'no'}",
    ]
    if is_proxy and info.get("Implementation"):
        header.append(f"Implementation: {info['Implementation']}  "
                      f"(fetch this address too for the real logic)")
    header.append("")
    return "\n".join(header) + "\n" + _decode_verified_source(info["SourceCode"])


@tool
def crypto_price(coins: str, currency: str = "usd") -> str:
    """Get live price, 24h change and market cap for cryptocurrencies from
    CoinGecko. `coins` is a comma-separated list of CoinGecko IDs, e.g.
    'bitcoin,ethereum,solana' (lowercase full names, not ticker symbols)."""
    import json as _json
    import urllib.parse
    import urllib.request

    ids = ",".join(c.strip().lower() for c in coins.split(",") if c.strip())
    if not ids:
        return "No coin IDs given."
    url = (
        "https://api.coingecko.com/api/v3/simple/price?"
        + urllib.parse.urlencode({
            "ids": ids,
            "vs_currencies": currency,
            "include_24hr_change": "true",
            "include_market_cap": "true",
        })
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return f"Price lookup failed ({exc}). Check the CoinGecko IDs and try again."
    if not data:
        return f"No data for '{ids}' — use CoinGecko IDs like 'bitcoin', not tickers like 'BTC'."
    lines = []
    for coin, values in data.items():
        price = values.get(currency)
        change = values.get(f"{currency}_24h_change")
        mcap = values.get(f"{currency}_market_cap")
        lines.append(
            f"{coin}: {price:,} {currency.upper()}"
            + (f" ({change:+.2f}% 24h)" if change is not None else "")
            + (f", mcap {mcap:,.0f}" if mcap else "")
        )
    return "\n".join(lines)


# Name -> tool mapping used when spawning agents dynamically.
TOOLBOX = {
    "web_search": web_search,
    "run_python": run_python,
    "write_report_file": write_report_file,
    "read_report_file": read_report_file,
    "crypto_price": crypto_price,
    "list_source_files": list_source_files,
    "read_source_file": read_source_file,
    "fetch_contract_source": fetch_contract_source,
    "write_poc_file": write_poc_file,
    "run_forge_test": run_forge_test,
}
