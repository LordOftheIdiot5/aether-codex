"""Fuzz Rig — a standalone, long-running invariant-fuzzing campaign driver.

This is "swing #1": point it at a fresh Foundry/Solidity repo dropped into
audit_target/ and walk away. It runs the 4-phase loop that plays to the real
edge of automated auditing — the expensive-but-smart LLM writes the properties
and triages breaks; the free-but-tireless `forge` compute does the searching:

    1. PLAN   (LLM)     the invariant_hunter reads the code, is SEEDED with the
                        reusable invariant library, and writes a Foundry
                        invariant suite (faithful target copy + handler +
                        invariant_* contract) into poc_workspace/.
    2. FUZZ   (compute) the rig grinds `forge` invariant campaigns for a
                        wall-clock budget — many rounds, escalating depth/runs,
                        a fresh seed each round — far past the 600s the in-agent
                        tool allows. This explores call-sequence space a human
                        never could.
    3. TRIAGE (LLM)     on a broken invariant, the counterexample sequence is
                        handed back to the LLM to minimise, explain, and judge
                        real-bug-or-artifact; the rig writes the finding to
                        reports/.
    4. LEARN            every invariant written is merged into a persistent,
                        growing library (data/invariant_library.json) so each
                        new target starts smarter than the last. That compounding
                        is the moat.

Usage:
    python -m aether_codex.fuzz_rig                 # fuzz all of audit_target/, ~2h
    python -m aether_codex.fuzz_rig src --hours 4   # fuzz audit_target/src/ for 4h
    python -m aether_codex.fuzz_rig --skip-plan     # reuse the suite already in
                                                    # poc_workspace/, just grind

Honesty note: most campaigns find nothing — that is a real, useful result, not a
failure. A clean run of strong invariants is evidence, not a bug. The rig never
invents a finding; a break is only reported after the LLM triage judges it a
genuine, attacker-reachable, harmful sequence.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import DATA_DIR, POC_DIR, REPORTS_DIR
from .llm import get_llm

# The reusable, compounding invariant library — the rig's long-term moat.
LIBRARY_PATH = DATA_DIR / "invariant_library.json"
AUDIT_AGENTS_PATH = Path(__file__).resolve().parent / "audit_agents.json"

_ICON = {"plan": "🧠", "fuzz": "🔎", "break": "💥", "triage": "🩺",
         "learn": "📚", "ok": "✅", "warn": "⚠️", "info": "•"}


def _say(kind: str, msg: str) -> None:
    print(f"  {_ICON.get(kind, '•')} {msg}", flush=True)


# --------------------------------------------------------------------- library
def _load_library() -> list[dict]:
    if LIBRARY_PATH.exists():
        try:
            return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_library(entries: list[dict]) -> None:
    LIBRARY_PATH.write_text(json.dumps(entries, indent=1, ensure_ascii=False),
                            encoding="utf-8")


def _merge_into_library(new: list[dict], target: str) -> int:
    """Merge freshly-written invariants into the library, deduping by property
    text. Returns how many were newly added."""
    lib = _load_library()
    seen = {(e.get("property") or "").strip().lower() for e in lib}
    added = 0
    now = time.strftime("%Y-%m-%d")
    for inv in new:
        prop = (inv.get("property") or "").strip()
        if not prop or prop.lower() in seen:
            continue
        seen.add(prop.lower())
        lib.append({
            "name": inv.get("name", ""),
            "property": prop,
            "category": inv.get("category", ""),
            "rationale": inv.get("rationale", ""),
            "first_seen_on": target,
            "date": now,
        })
        added += 1
    if added:
        _save_library(lib)
    return added


def _library_seed(limit: int = 30) -> str:
    """A compact digest of the library to seed the planner. Keeps the strongest
    (most reused / economic) properties in front of the LLM so it reuses and
    refines them instead of starting cold."""
    lib = _load_library()
    if not lib:
        return "(The invariant library is empty — this is the first campaign.)"
    # Prefer economic/solvency categories, then most recent.
    priority = ("solvency", "conservation", "accounting", "no-free-money",
                "monotonic", "rounding")

    def rank(e: dict) -> tuple:
        cat = (e.get("category") or "").lower()
        return (0 if any(p in cat for p in priority) else 1, )

    picked = sorted(lib, key=rank)[:limit]
    lines = [f"- [{e.get('category', '?')}] {e.get('property')}"
             + (f"  — {e['rationale']}" if e.get("rationale") else "")
             for e in picked]
    return "\n".join(lines)


# ------------------------------------------------------------------- json parse
def _extract_json(text: str) -> dict | None:
    """Pull the last JSON object out of an LLM message, tolerant of fences and
    surrounding prose."""
    candidates: list[str] = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates += re.findall(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in reversed(candidates):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass
    # Fallback: the last brace-balanced span.
    for m in reversed(list(re.finditer(r"\{.*\}", text, re.DOTALL))):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    return None


# ------------------------------------------------------------------- the agent
def _invariant_hunter(llm):
    """Build just the invariant_hunter specialist (lean — no full roster)."""
    from .agents.base import SubAgent
    from .tools import TOOLBOX

    specs = json.loads(AUDIT_AGENTS_PATH.read_text(encoding="utf-8"))
    spec = next(s for s in specs if s["name"] == "invariant_hunter")
    tools = [TOOLBOX[t] for t in spec.get("tools", []) if t in TOOLBOX]
    return SubAgent(spec["name"], spec.get("description", ""),
                    spec["system_prompt"], tools, llm)


# --------------------------------------------------------------------- forge run
def _forge() -> str | None:
    return shutil.which("forge")


def _classify(returncode: int, out: str) -> str:
    """Interpret a forge run: 'build' (won't compile), 'break' (a real
    counterexample), or 'clean' (no violation this round)."""
    low = out.lower()
    if "compiler run failed" in low or "failed to compile" in low or "error[" in low:
        return "build"
    if returncode != 0 and ("failing tests:" in low or "[fail" in low):
        return "break"
    return "clean"


def _run_round(forge: str, contract: str, runs: int, depth: int,
               seed: int, timeout_s: int) -> tuple[str, str]:
    """One forge invariant round. Escalation is passed via FOUNDRY_ env
    overrides so we don't have to rewrite foundry.toml between rounds."""
    import os

    env = dict(os.environ)
    env["FOUNDRY_INVARIANT_RUNS"] = str(runs)
    env["FOUNDRY_INVARIANT_DEPTH"] = str(depth)
    env["FOUNDRY_INVARIANT_FAIL_ON_REVERT"] = "false"

    cmd = [forge, "test", "--match-test", "invariant", "-vvv", "--fuzz-seed", str(seed)]
    if contract:
        cmd += ["--match-contract", contract]
    try:
        proc = subprocess.run(cmd, cwd=str(POC_DIR), capture_output=True,
                              text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if hasattr(exc, "stdout") else ""
        return "clean", out  # a timed-out round simply found nothing yet
    except Exception as exc:  # pragma: no cover
        return "build", f"Could not run forge: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return _classify(proc.returncode, out), out


def _campaign(contract: str, hours: float) -> tuple[str, str]:
    """Grind escalating invariant rounds until a break is found or time runs
    out. Returns (result, forge_output) where result is break/build/clean."""
    forge = _forge()
    if forge is None:
        return "build", ("Foundry's `forge` is not on PATH. Install it from "
                         "https://getfoundry.sh and re-run.")

    deadline = time.time() + hours * 3600
    runs, depth = 500, 150
    round_no = 0
    last_out = ""
    while time.time() < deadline:
        round_no += 1
        remaining = deadline - time.time()
        per_round = int(min(remaining, 1500))  # ≤25 min/round → periodic checkpoints
        if per_round < 20:
            break
        seed = random.randint(1, 2**31 - 1)
        _say("fuzz", f"round {round_no}: runs={runs} depth={depth} seed={seed} "
                     f"(≤{per_round//60}m, {remaining/3600:.1f}h left)")
        result, out = _run_round(forge, contract, runs, depth, seed, per_round)
        last_out = out or last_out
        if result == "build":
            _say("warn", "suite did not compile — see output; fix the suite or re-plan.")
            return "build", out
        if result == "break":
            _say("break", f"invariant BROKEN on round {round_no} — capturing counterexample.")
            return "break", out
        _say("ok", f"round {round_no}: no violation (properties held).")
        # Escalate the search for the next round.
        depth = min(depth + 75, 1000)
        runs = min(int(runs * 1.6), 6000)
    return "clean", last_out


# ---------------------------------------------------------------------- phases
_PLAN_DIRECTIVE = """CAMPAIGN — PHASE 1 (PLAN). Target to fuzz: audit_target/{subdir}

You are seeding a LONG automated invariant-fuzzing campaign. A separate rig will
then grind `forge` for hours, so your ONLY job now is to produce a strong,
COMPILING invariant suite — not to find the bug yourself.

REUSE THE LIBRARY. These invariants from past campaigns are proven worth
checking — adapt the ones that fit this protocol, then add new protocol-specific
ones (especially economic/accounting properties, which is where surviving bugs
hide):
{library}

DO THIS:
1. list_source_files + read_source_file to map the target's core assets,
   accounting and value-flow functions.
2. Write into poc_workspace/: a faithful copy of the target under src/; a
   Handler under test/ with BOUNDED actions (bound()/vm.assume), a few actors
   (vm.prank) and GHOST VARIABLES; and ONE invariant test contract under test/
   (is Test) that deploys the system, registers the handler via targetContract()
   and defines several strong invariant_* functions. Prefer solvency /
   conservation / no-free-money / monotonicity / rounding-in-protocol-favour
   properties over trivial ones.
3. run_forge_test once with a SHORT check only to confirm it COMPILES and the
   invariants run (they need not break here — the rig does the deep search).
   Iterate only until it compiles cleanly.

Then FINISH your message with exactly one fenced json block:
```json
{{"invariant_contract": "<the invariant test contract name>",
  "compiles": true,
  "invariants": [
    {{"name": "invariant_...", "property": "one-line property in plain words",
      "category": "solvency|conservation|accounting|no-free-money|monotonic|rounding|access|state-machine",
      "rationale": "why it matters / what bug it would catch"}}
  ],
  "notes": "anything the deep fuzzer should know (bounds, assumptions)"}}
```
"""

_TRIAGE_DIRECTIVE = """CAMPAIGN — PHASE 3 (TRIAGE). An invariant BROKE during the
deep fuzzing campaign. Here is the forge counterexample output:

--- FORGE OUTPUT (truncated) ---
{output}
--- END ---

DO THIS, rigorously and honestly:
1. Read the failing invariant and the exact call sequence forge reported.
2. VERIFY it is a REAL bug, not a test artifact: is every call something a real
   attacker could actually do (bounds realistic, no cheat-only powers), and is
   the end state genuinely harmful (value lost/stuck/stolen, accounting broken)?
   If it is only a test-harness artifact or a property the protocol never
   promised, SAY SO — do not manufacture a finding.
3. If real: minimise the sequence, identify the composed root cause, and write a
   complete audit finding (title, severity, the exact multi-step sequence,
   root cause, impact, and a concrete fix). Put the full finding as markdown in
   your message body.

FINISH with exactly one fenced json block:
```json
{{"real_bug": true, "severity": "Critical|High|Medium|Low",
  "title": "short finding title", "one_line": "what breaks, in one line"}}
```
(set real_bug=false if triage shows it is an artifact.)
"""


def _write_report(name: str, body: str) -> Path:
    safe = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "finding"
    path = REPORTS_DIR / f"fuzz_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.md"
    path.write_text(body, encoding="utf-8")
    return path


def run_campaign(subdir: str = "", hours: float = 2.0, skip_plan: bool = False,
                 provider: str | None = None, model: str | None = None) -> None:
    print("=" * 66)
    print("  AETHER CODEX — FUZZ RIG   (invariant campaign)")
    print(f"  target: audit_target/{subdir or '(all)'}   budget: {hours:.1f}h")
    print("=" * 66)

    llm = get_llm(provider=provider, model=model)
    agent = _invariant_hunter(llm)

    contract = ""
    # ---- PHASE 1: PLAN ---------------------------------------------------
    if not skip_plan:
        _say("plan", "planning invariants (reading code, seeding from library)…")
        directive = _PLAN_DIRECTIVE.format(subdir=subdir or "",
                                           library=_library_seed())
        try:
            plan_msg = agent.run(directive)
        except Exception as exc:
            _say("warn", f"planning failed: {exc}")
            return
        plan = _extract_json(plan_msg) or {}
        contract = (plan.get("invariant_contract") or "").strip()
        invs = plan.get("invariants") or []
        if not plan.get("compiles", False):
            _say("warn", "planner did not confirm a compiling suite. Output below:")
            print(plan_msg[-2000:])
            _say("info", "fix the suite in poc_workspace/ and re-run with --skip-plan, "
                         "or just re-run to let it try again.")
            return
        _say("ok", f"suite compiles: contract='{contract or '?'}', "
                   f"{len(invs)} invariants proposed.")
        added = _merge_into_library(invs, subdir or "audit_target")
        if added:
            _say("learn", f"library grew by {added} new invariant(s) "
                          f"→ {LIBRARY_PATH.name} (now {len(_load_library())} total).")
    else:
        _say("info", "skip-plan: grinding the invariant suite already in poc_workspace/.")

    # ---- PHASE 2: FUZZ ---------------------------------------------------
    _say("fuzz", f"starting deep campaign (escalating runs/depth, fresh seed/round)…")
    result, out = _campaign(contract, hours)

    if result == "build":
        _say("warn", "campaign stopped: the suite would not compile. Forge tail:")
        print(out[-2000:])
        return
    if result == "clean":
        print()
        _say("ok", "CAMPAIGN CLEAN — no invariant broke within the budget.")
        _say("info", "That is a real result: these properties held across the search. "
                     "To go deeper, add stronger economic invariants (differential / "
                     "reference-model) and raise the hours, then re-run.")
        return

    # ---- PHASE 3: TRIAGE (a break was found) -----------------------------
    _say("triage", "a property broke — triaging the counterexample with the LLM…")
    tail = out[-8000:]
    try:
        triage_msg = agent.run(_TRIAGE_DIRECTIVE.format(output=tail))
    except Exception as exc:
        _say("warn", f"triage call failed: {exc}. Raw counterexample:")
        print(tail)
        return
    verdict = _extract_json(triage_msg) or {}
    if verdict.get("real_bug"):
        title = verdict.get("title", "invariant violation")
        sev = verdict.get("severity", "?")
        body = (f"# FUZZ FINDING — {title}\n\n"
                f"**Severity:** {sev}  \n"
                f"**Summary:** {verdict.get('one_line', '')}\n\n"
                f"_Found by Aether Codex Fuzz Rig on `audit_target/{subdir or '(all)'}`, "
                f"{time.strftime('%Y-%m-%d %H:%M')}._\n\n---\n\n"
                f"{triage_msg}\n\n---\n\n## Raw forge counterexample\n\n```\n{tail}\n```\n")
        path = _write_report(title, body)
        print()
        _say("break", f"CONFIRMED {sev} FINDING: {title}")
        _say("ok", f"written → {path}")
        _say("info", "VERIFY IT YOURSELF before submitting anywhere — this is a lead, "
                     "not a guarantee.")
    else:
        _say("ok", "triage judged the break a test artifact, not a real bug (honest null).")
        _say("info", "Tighten the handler bounds/assumptions and re-run to keep the "
                     "search honest.")


def main() -> None:
    p = argparse.ArgumentParser(description="Long-running invariant-fuzzing campaign.")
    p.add_argument("subdir", nargs="?", default="",
                   help="subfolder under audit_target/ to fuzz (default: all)")
    p.add_argument("--hours", type=float, default=2.0,
                   help="wall-clock budget for the deep fuzzing phase (default 2)")
    p.add_argument("--skip-plan", action="store_true",
                   help="reuse the invariant suite already in poc_workspace/")
    p.add_argument("--provider", default=None, help="anthropic | grok | local")
    p.add_argument("--model", default=None, help="model name override")
    args = p.parse_args()
    run_campaign(args.subdir, hours=args.hours, skip_plan=args.skip_plan,
                 provider=args.provider, model=args.model)


if __name__ == "__main__":
    main()
