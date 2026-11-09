"""Wiring verification: does a symbol exist *and* is it actually used?

Background (WO 8): 1,070 lines implemented, the caller was forgotten, and the
next compiler hotfix saw "unused function" and deleted it all. A `✅` status in
the doc was true at the time — and yet the feature was dead.

Two questions, two answers, one verdict:

    defined?     no  → symbol does not exist (typo or removed)
    used?        no  → dead code, refuse acceptance
    both yes         → wired

The Work Order → symbols mapping lives in `.atlas/wo-symbols.yml` (in the
project, not the tool). It is the only way to monitor a completed WO later:
without recorded symbols, nobody notices when a hotfix severs the caller.
"""

from __future__ import annotations

from .config import cache_dir

from pathlib import Path

from . import code

SYMBOLS_FILE = "wo-symbols.yml"


def symbols_path(root) -> Path:
    return cache_dir(root) / SYMBOLS_FILE


def load_wo_symbols(root) -> dict[int, list[str]]:
    """Return `{8: ['_dispatch_unit_class_skill'], 12: [...]}` — missing file = empty dict."""
    from .config import CACHE_DIR, LEGACY_CACHE_DIR

    root_path = Path(root)
    candidates = [
        root_path / LEGACY_CACHE_DIR / SYMBOLS_FILE,
        root_path / CACHE_DIR / SYMBOLS_FILE,
    ]
    existing_paths = [p for p in candidates if p.exists()]
    if not existing_paths:
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return {}

    mapping: dict[int, list[str]] = {}
    for path in existing_paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            try:
                wo = int(key)
            except (TypeError, ValueError):
                continue
            items: list[str] = []
            if isinstance(value, str):
                items = [value]
            elif isinstance(value, list):
                items = [str(item) for item in value if str(item).strip()]
            if items:
                prior = mapping.get(wo, [])
                mapping[wo] = list(dict.fromkeys(prior + items))
    return mapping


def record_wo_symbols(root, wo: int, symbols: list[str] | None) -> Path | None:
    """Persist verified symbols for a completed Work Order into `.atlas/wo-symbols.yml`."""
    cleaned = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not cleaned:
        return None
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return None
    mapping = load_wo_symbols(root)
    existing = mapping.get(int(wo), [])
    merged = list(dict.fromkeys(existing + cleaned))
    mapping[int(wo)] = merged
    path = symbols_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {int(k): v for k, v in sorted(mapping.items())}
    path.write_text(
        yaml.safe_dump(ordered, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    return path


def check(root, symbols: list[str]) -> dict:
    """Check each symbol for definition and wiring. No network, no index."""
    report = []
    for raw in symbols:
        symbol = str(raw).strip()
        if not symbol:
            continue
        # exact=True: otherwise `limit` is consumed by substring matches and the
        # real definition falls past the cutoff (can_vault_a/b/c before `func vault()`).
        definitions = code.find(root, symbol, limit=3, exact=True)
        # Exact match, not substring: `vault` must not be satisfied by `can_vault`.
        exact = [d for d in definitions if d["symbol"] == symbol]
        usage = code.references(root, symbol, limit=3)
        bound = usage["call_count"] + usage["ref_count"]

        entry = {
            "symbol": symbol,
            "defined": bool(exact),
            "definition": f"{exact[0]['path']}:{exact[0]['line']}" if exact else "",
            "signature": exact[0]["signature"] if exact else "",
            "calls": usage["call_count"],
            "refs": usage["ref_count"],
            "ok": bool(exact) and bound > 0,
        }
        if not exact:
            entry["reason"] = "not defined"
        elif bound == 0:
            entry["reason"] = "defined, but never called or bound"
        report.append(entry)

    failed = [item for item in report if not item["ok"]]
    return {"checked": len(report), "ok": not failed, "symbols": report,
            "failed": failed}


def _verdict(entry: dict) -> str:
    if not entry["defined"]:
        return f"'{entry['symbol']}' is not defined in the project"
    return (
        f"'{entry['symbol']}' is defined in {entry['definition']} but "
        "never called or bound (0 calls, 0 references)"
    )


def refusal(result: dict) -> str:
    """Format a plain-text refusal message — with evidence, not blame."""
    lines = ["Acceptance refused — wiring check failed:"]
    for entry in result["failed"]:
        lines.append(f"  ✗ {_verdict(entry)}")
    lines.append("")
    lines.append(
        "A ✅ without a caller documents an intention, not a "
        "result (see WO 8). Wire it up, then publish again."
    )
    return "\n".join(lines)


def summary(result: dict) -> str:
    lines = []
    for entry in result["symbols"]:
        mark = "✓" if entry["ok"] else "✗"
        where = entry["definition"] or "—"
        lines.append(
            f"  {mark} {entry['symbol']}  {where}  "
            f"{entry['calls']} call(s), {entry['refs']} reference(s)"
        )
    return "\n".join(lines)


def regressions(root, work_orders: list[dict]) -> list[dict]:
    """Check the symbols of **completed** work orders against the current codebase.

    `work_orders` comes from `status.status()` — `[{wo, status, title}, …]`.
    WOs without recorded symbols are skipped, and open WOs are never checked:
    an open WO is allowed to be unfinished, a completed one is not allowed to be broken.
    """
    mapping = load_wo_symbols(root)
    done = {item["wo"]: item for item in work_orders
            if str(item.get("status", "")).lower() in {"done", "erledigt"}}

    findings: list[dict] = []
    for wo, symbols in sorted(mapping.items()):
        if wo not in done:
            continue
        result = check(root, symbols)
        for entry in result["failed"]:
            findings.append({
                "wo": wo,
                "title": done[wo].get("title", ""),
                "symbol": entry["symbol"],
                "reason": entry.get("reason", ""),
                "verdict": _verdict(entry),
            })
    return findings


def check_project(root, conn) -> dict:
    """Run CI / pre-commit wiring & link integrity checks across the repository.

    1. Verifies that all symbols recorded in `.atlas/wo-symbols.yml` for completed
       Work Orders are still defined and still have >= 1 caller/reference.
    2. Verifies that all relative file paths declared in document `implements`
       frontmatter still exist on disk.
    """
    import json
    from . import status as status_mod

    root_path = Path(root)
    items = json.loads(status_mod.status(conn)).get("work_orders", [])
    mapping = load_wo_symbols(root_path)
    reg = regressions(root_path, items)

    broken_implements: list[dict] = []
    rows = conn.execute("SELECT id, path, implements FROM docs").fetchall()
    for row in rows:
        raw_impl = row["implements"] or "[]"
        try:
            impl_list = json.loads(raw_impl) if raw_impl.startswith("[") else [
                p.strip() for p in raw_impl.split(",") if p.strip()
            ]
        except Exception:
            impl_list = []
        for rel_file in impl_list:
            rel_str = str(rel_file).strip()
            if not rel_str:
                continue
            if not (root_path / rel_str).exists():
                broken_implements.append({
                    "doc": row["id"],
                    "doc_path": row["path"],
                    "missing_file": rel_str,
                })

    ok = not reg and not broken_implements
    return {
        "ok": ok,
        "monitored_work_orders": len(mapping),
        "regressions": reg,
        "broken_implements": broken_implements,
    }


def render_check(report: dict) -> str:
    """Format `check_project` output for terminal / pre-commit hooks."""
    if report["ok"]:
        return (
            f"✓ Wiring & link check passed "
            f"({report['monitored_work_orders']} monitored work order(s), "
            f"0 regressions, 0 broken implements links)."
        )
    lines = ["✗ Wiring / link check failed:"]
    for item in report.get("regressions", []):
        lines.append(f"  ✗ WO {item['wo']} ({item['title']}): {item['verdict']}")
    for item in report.get("broken_implements", []):
        lines.append(
            f"  ✗ {item['doc_path']}: implements '{item['missing_file']}' does not exist"
        )
    return "\n".join(lines)
