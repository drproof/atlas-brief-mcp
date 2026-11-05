"""Anbindungs-Nachweis: existiert ein Symbol *und* wird es benutzt?

Hintergrund (WO 8): 1.070 Zeilen implementiert, der Aufrufer fehlte, der nächste
Compiler-Hotfix sah „unused function" und löschte alles. Ein Status `✅` im Doc
war zu diesem Zeitpunkt wahr — und trotzdem war das Feature tot.

Zwei Fragen, zwei Antworten, ein Urteil:

    definiert?   nein → das Symbol gibt es nicht (Tippfehler oder weg)
    benutzt?     nein → toter Code, Abnahme verweigern
    beides ja    → angebunden

Die Zuordnung Work Order → Symbole steht in `.atlas/wo-symbols.yml` (im Projekt,
nicht im Tool). Sie ist der einzige Weg, eine erledigte WO später zu überwachen:
Ohne hinterlegte Symbole kann niemand merken, dass ein Hotfix sie gekappt hat.
"""

from __future__ import annotations

from .config import cache_dir

from pathlib import Path

from . import code

SYMBOLS_FILE = "wo-symbols.yml"


def symbols_path(root) -> Path:
    return cache_dir(root) / SYMBOLS_FILE


def load_wo_symbols(root) -> dict[int, list[str]]:
    """`{8: ['_dispatch_unit_class_skill'], 12: [...]}` — fehlende Datei = leer."""
    path = symbols_path(root)
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

    mapping: dict[int, list[str]] = {}
    for key, value in data.items():
        try:
            wo = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            mapping[wo] = [value]
        elif isinstance(value, list):
            mapping[wo] = [str(item) for item in value if str(item).strip()]
    return mapping


def check(root, symbols: list[str]) -> dict:
    """Prüft jedes Symbol auf Definition und Anbindung. Ohne Netz, ohne Index."""
    report = []
    for raw in symbols:
        symbol = str(raw).strip()
        if not symbol:
            continue
        # exact=True: sonst frisst `limit` die Substring-Treffer und die echte
        # Definition liegt hinter dem Abbruch (can_vault_a/b/c vor `func vault()`).
        definitions = code.find(root, symbol, limit=3, exact=True)
        # Exakter Treffer, nicht Substring: `vault` soll nicht `can_vault` bestätigen.
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
            entry["reason"] = "nicht definiert"
        elif bound == 0:
            entry["reason"] = "definiert, aber nirgends aufgerufen oder gebunden"
        report.append(entry)

    failed = [item for item in report if not item["ok"]]
    return {"checked": len(report), "ok": not failed, "symbols": report,
            "failed": failed}


def _verdict(entry: dict) -> str:
    if not entry["defined"]:
        return f"'{entry['symbol']}' ist im Projekt nicht definiert"
    return (
        f"'{entry['symbol']}' ist in {entry['definition']} definiert, wird aber "
        "nirgends aufgerufen oder gebunden (0 Aufrufe, 0 Referenzen)"
    )


def refusal(result: dict) -> str:
    """Klartext für die verweigerte Abnahme — mit Beleg, nicht mit Vorwurf."""
    lines = ["Abnahme verweigert — Anbindungs-Nachweis fehlgeschlagen:"]
    for entry in result["failed"]:
        lines.append(f"  ✗ {_verdict(entry)}")
    lines.append("")
    lines.append(
        "Ein ✅ ohne Aufrufer ist die Dokumentation einer Absicht, nicht eines "
        "Ergebnisses (vgl. WO 8). Verdrahten, dann erneut veröffentlichen."
    )
    return "\n".join(lines)


def summary(result: dict) -> str:
    lines = []
    for entry in result["symbols"]:
        mark = "✓" if entry["ok"] else "✗"
        where = entry["definition"] or "—"
        lines.append(
            f"  {mark} {entry['symbol']}  {where}  "
            f"{entry['calls']} Aufruf(e), {entry['refs']} Referenz(en)"
        )
    return "\n".join(lines)


def regressions(root, work_orders: list[dict]) -> list[dict]:
    """Prüft die Symbole **erledigter** Work Orders gegen den aktuellen Code.

    `work_orders` kommt aus `status.status()` — `[{wo, status, title}, …]`.
    Nicht hinterlegte WOs werden übersprungen, offene nie geprüft: Eine offene
    WO darf unfertig sein, eine erledigte nicht kaputt.
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
