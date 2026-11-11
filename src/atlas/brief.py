"""Task brief: build a ready-to-use delegation prompt from the index.

No model, no hallucination — only real search hits, real code mappings,
and project guardrails. Works both as a CLI (copy-paste into any chat)
and as an MCP tool.
"""

from __future__ import annotations

from .config import cache_dir

import ast
import json
import re
from datetime import date
from pathlib import Path

from . import search as search_mod

MAX_SECTIONS = 5
SECTION_CHARS = 1600
MAX_CHARS = 8000

# Code paths in the GDD/docs usually appear inline in prose ("scripts/camp_hub.gd",
# "src/atlas/search.py"), not only in frontmatter. Extract them from matched sections.
FILE_RE = re.compile(
    r"(?:res://)?[\w.-]+(?:/[\w.-]+)+\.(?:gd|tscn|tres|json|gdshader|glsl|py|ts|tsx|js|jsx|rs|cs|go|java|kt|swift)",
    re.IGNORECASE,
)


def _parse_list(raw: str) -> list[str]:
    """Frontmatter lists are stored via str() — accept both forms."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("["):
        for parse in (json.loads, ast.literal_eval):
            try:
                value = parse(text)
                if isinstance(value, list):
                    return [str(item) for item in value if str(item).strip()]
            except Exception:
                continue
    return [part.strip() for part in text.split(",") if part.strip()]


RULES_LIMIT = 2600


def _load_rules(root: Path) -> str:
    """Load project guardrails — AGENTS.md takes precedence over legacy files.

    An older `rules.md` sitting next to it must not shadow `AGENTS.md`: it
    contained outdated timeouts and no GDD requirement, causing the agent to
    follow rules that had been wrong for weeks.
    """
    for candidate in (root / "AGENTS.md", cache_dir(root) / "rules.md",
                      root / "rules.md", root / "CLAUDE.md"):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                return _trim_rules(text)
    return ""


def _trim_rules(text: str) -> str:
    """Keep only the GDD portion of the guardrails — the rest is in AGENTS.md anyway.

    Cut at the first `## 1.` section, but only when preceded by a `## 0.`
    section (the GDD block). Otherwise the file is flat and is only truncated
    at the length limit — otherwise nothing would remain of it.
    """
    match = re.search(r"\n##\s*1\.", text)
    if match and match.start() > 0 and re.search(r"\n##\s*0\.", text[: match.start()]):
        text = text[: match.start()]
    if len(text) > RULES_LIMIT:
        cut = text.rfind("\n", 0, RULES_LIMIT)
        text = text[: cut if cut > 0 else RULES_LIMIT]
        text += "\n\n_… see AGENTS.md for further rules._"
    return text.strip()


def find_next_work_order(conn, work_order: int | None = None) -> dict | None:
    """Return the requested Work Order, or the next open Work Order (prioritizing in-progress ones)."""
    open_orders = find_open_work_orders(conn, work_order=work_order)
    return open_orders[0] if open_orders else None


def find_open_work_orders(conn, work_order: int | None = None) -> list[dict]:
    """Return all open Work Orders ordered with in-progress (`🚧`) first, then by WO number."""
    from . import status as status_mod

    orders = status_mod.extract_work_orders(conn)
    if work_order is not None:
        matched = [item for item in orders if item["wo"] == int(work_order)]
        return matched
    open_orders = [item for item in orders if item["status"] == "open"]
    in_progress = [
        item for item in open_orders
        if 0 < item.get("steps_done", 0) < item.get("steps_total", 0)
    ]
    not_started = [item for item in open_orders if item not in in_progress]
    return in_progress + not_started


def _symbol_status_lines(
    root: Path,
    symbols: list[str],
    current_wo: int | None = None,
    conn=None,
    guards_map: dict | None = None,
) -> list[str]:
    """Check current definition and caller status for target symbols across the codebase."""
    from . import verify

    result = verify.check(root, symbols[:8])
    if guards_map is None:
        guards_map = verify.symbol_guards(root, conn=conn)
    lines: list[str] = []
    for item in result.get("symbols", []):
        sym = item["symbol"]
        other_guards = [
            g for g in guards_map.get(sym, [])
            if current_wo is None or int(g["wo"]) != int(current_wo)
        ]
        shield = ""
        if other_guards:
            labels = ", ".join(f"WO {g['wo']}" for g in other_guards)
            shield = f" · 🛡️ guarded by {labels}"
        if not item["defined"]:
            lines.append(f"- `{sym}` — **not defined yet** (needs implementation){shield}")
        else:
            calls = item["calls"]
            refs = item["refs"]
            state = f"{calls} call(s), {refs} ref(s)" if (calls + refs) > 0 else "⚠️ 0 callers (unwired)"
            lines.append(f"- `{sym}` — defined in `{item['definition']}` ({state}){shield}")
    return lines


def _handoff_context(
    root: Path,
    work_order: int | None = None,
    files: list[str] | None = None,
    recorded_syms: list[str] | None = None,
    conn=None,
    guards_map: dict | None = None,
) -> list[str]:
    """Build a compact multi-agent handoff block: recent audit log actions, working-tree edits, and protected symbols."""
    import subprocess
    from . import audit, verify

    out: list[str] = []

    # 1. Recent agent activity from `.atlas/mutations.log`
    log_entries = audit.tail(root, limit=15)
    if log_entries:
        recent = log_entries[-3:]
        activity_parts: list[str] = []
        for entry in recent:
            stamp = str(entry.get("ts", ""))[11:16] or str(entry.get("ts", ""))[:10]
            tool = str(entry.get("tool", "?"))
            if entry.get("task"):
                task_txt = str(entry["task"])[:45]
                st = entry.get("status", "done")
                activity_parts.append(f"`{stamp}` {tool} `{task_txt}` -> {st}")
            elif entry.get("wo") is not None:
                wo_n = entry["wo"]
                st = entry.get("status") or entry.get("new") or "updated"
                activity_parts.append(f"`{stamp}` {tool} WO {wo_n} ({st})")
            else:
                activity_parts.append(f"`{stamp}` {tool}")
        if activity_parts:
            out.append("- **Recent agent actions:** " + " · ".join(activity_parts))

    # 2. Uncommitted git working-tree files (excluding .atlas/.gdd cache)
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            dirty: list[str] = []
            for raw in proc.stdout.splitlines():
                if len(raw) < 4:
                    continue
                rel = raw[3:].strip().strip('"')
                if " -> " in rel:
                    rel = rel.split(" -> ", 1)[1].strip()
                if rel.startswith((".atlas/", ".gdd/")) or "__pycache__" in rel:
                    continue
                if rel not in dirty:
                    dirty.append(rel)
            if dirty:
                aff_set = {f.replace("\\", "/") for f in (files or [])}
                overlap = [d for d in dirty if d in aff_set]
                overlap_note = (
                    f" — touches affected file(s): {', '.join(f'`{f}`' for f in overlap[:4])}"
                    if overlap
                    else ""
                )
                sample = ", ".join(f"`{d}`" for d in dirty[:5])
                more = f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else ""
                out.append(
                    f"- **Working tree (uncommitted):** {len(dirty)} file(s): {sample}{more}{overlap_note}"
                )
    except Exception:
        pass

    # 3. Protected symbols from earlier completed Work Orders in affected files
    if guards_map is None:
        guards_map = verify.symbol_guards(root, conn=conn)
    if guards_map and files:
        protected_hits: list[str] = []
        seen_syms: set[str] = set()
        for rel in files[:10]:
            fpath = root / rel
            if not fpath.is_file():
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for sym, g_list in guards_map.items():
                if sym in seen_syms or sym not in text or not re.search(rf"\b{re.escape(sym)}\b", text):
                    continue
                other_wos = [
                    g["wo"] for g in g_list
                    if work_order is None or int(g["wo"]) != int(work_order)
                ]
                if not other_wos:
                    continue
                seen_syms.add(sym)
                wo_labels = ", ".join(f"WO {w}" for w in other_wos[:2])
                protected_hits.append(f"`{sym}` ({wo_labels})")
                if len(protected_hits) >= 6:
                    break
            if len(protected_hits) >= 6:
                break
        if protected_hits:
            out.append(
                "- 🛡️ **Protected symbols in affected files (do not break):** "
                + ", ".join(protected_hits)
            )

    return out


def build(
    conn,
    root: Path,
    goal: str = "",
    max_sections: int = MAX_SECTIONS,
    max_chars: int = MAX_CHARS,
    include_rules: bool = True,
    work_order: int | None = None,
    extra_files: list[str] | None = None,
    all_open: bool = False,
) -> str:
    """Build the task brief as Markdown (for one Work Order/goal, or the full open queue when `all_open=True`)."""
    from . import verify

    if all_open:
        return build_queue(conn, root, max_chars=max_chars, include_rules=include_rules)

    wo_item = find_next_work_order(conn, work_order=work_order) if (work_order is not None or not goal.strip()) else None
    if wo_item is not None and work_order is None:
        work_order = wo_item["wo"]
    if not goal.strip():
        if wo_item is not None:
            goal = wo_item["title"] or f"Work Order {wo_item['wo']}"
        else:
            return (
                "# Task brief: No open Work Orders\n\n"
                "_All documented Work Orders are completed (`✅ DONE`). "
                "Create a new one with `atlas plan \"<topic>\" --step \"...\"`._\n"
            )

    hits = search_mod.run_search(conn, goal, k=max_sections * 3)

    selected: list[dict] = []
    seen: set[str] = set()

    # If the Work Order explicitly links a `spec_anchor`, include that section first.
    spec_anchor = (wo_item or {}).get("spec_anchor") or ""
    if spec_anchor:
        base_spec = spec_anchor.split("~")[0]
        row = conn.execute(
            "SELECT anchor, doc_id, path, line_start, heading, breadcrumb "
            "FROM sections WHERE anchor = ? OR anchor LIKE ? ORDER BY part LIMIT 1",
            (base_spec, base_spec + "~%"),
        ).fetchone()
        if row is not None:
            seen.add(base_spec)
            selected.append({
                "anchor": row["anchor"],
                "doc_id": row["doc_id"],
                "path": row["path"],
                "line": row["line_start"],
                "heading": row["heading"],
                "breadcrumb": row["breadcrumb"] or "",
            })

    for hit in hits:
        base = hit["anchor"].split("~")[0]
        if base in seen or base.endswith("#3-work-orders"):
            continue
        seen.add(base)
        selected.append(hit)
        if len(selected) >= max_sections:
            break

    budget = max_chars
    sections: list[str] = []
    doc_ids: set[str] = set()
    file_hits: list[str] = []
    for hit in selected:
        base = hit["anchor"].split("~")[0]
        payload = json.loads(search_mod.read(conn, base, max_chars=min(SECTION_CHARS, max(300, budget))))
        budget -= payload["chars"]
        doc_ids.add(hit["doc_id"])
        for found in FILE_RE.finditer(payload["body"]):
            path = found.group(0).replace("res://", "")
            if path not in file_hits:
                file_hits.append(path)
        origin = f"{hit['path']}:{hit['line']}"
        trail = f" — {hit['breadcrumb']}" if hit["breadcrumb"] else ""
        sections.append(
            f"### {payload['heading']}\n"
            f"*{origin}{trail}* · anchor: `{base}`\n\n"
            f"{payload['body']}\n"
        )
        if budget <= 0:
            break

    files: list[str] = list(extra_files or [])
    for item in file_hits:
        if item not in files:
            files.append(item)
    for doc_id in sorted(doc_ids):
        row = conn.execute("SELECT implements FROM docs WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            continue
        for item in _parse_list(row["implements"]):
            if item not in files:
                files.append(item)

    lines: list[str] = [
        f"# Task brief: {goal}",
        "",
        f"_Generated from the documentation index on {date.today().isoformat()}. "
        f"Anchors are stable — re-read them any time with `atlas_read(<anchor>)`._",
        "",
        f"## 1. Relevant sections ({len(sections)})",
        "",
    ]
    if sections:
        lines.extend(sections)
    else:
        lines.append(
            "_No match in the documentation. Try different wording, or use `atlas_outline`._\n"
        )

    lines.extend(["## 2. Affected files", ""])
    if files:
        lines.extend(f"- `{item}`" for item in files)
    else:
        lines.append("_No `implements:` mapping in the docs — search the code yourself._")
    lines.append("")

    wo_steps = (wo_item or {}).get("steps") or []
    recorded_syms = verify.load_wo_symbols(root).get(int(work_order), []) if work_order else []
    shared_guards = verify.symbol_guards(root, conn=conn) if (files or recorded_syms) else {}
    handoff_lines = _handoff_context(
        root,
        work_order=work_order,
        files=files,
        recorded_syms=recorded_syms,
        conn=conn,
        guards_map=shared_guards,
    )
    if handoff_lines:
        lines.extend([
            "### Handoff & Regression Context",
            "",
            *handoff_lines,
            "",
        ])

    if include_rules:
        rules = _load_rules(root)
        if rules:
            lines.extend(["## 3. Guardrails (still in force)", "", rules, ""])

    if wo_steps:
        done_cnt = sum(1 for s in wo_steps if s.get("done"))
        lines.extend([
            f"## 4. Work Order {work_order} Checklist ({done_cnt}/{len(wo_steps)} steps done)",
            "",
        ])
        next_step = next((s["text"] for s in wo_steps if not s.get("done")), None)
        if next_step:
            lines.append(f"👉 **Next open step:** `{next_step}`")
            lines.append("")
        for step in wo_steps:
            mark = "x" if step.get("done") else " "
            lines.append(f"- [{mark}] {step['text']}")
        lines.append("")
    else:
        lines.extend([
            "## 4. Work order",
            "",
            "- [ ] Break the goal into checkable steps",
            "- [ ] Read the affected files from section 2 first",
            "- [ ] Verify after the change (test/run) and quote the result",
            "",
        ])

    if recorded_syms:
        sym_lines = _symbol_status_lines(
            root,
            recorded_syms,
            current_wo=work_order,
            conn=conn,
            guards_map=shared_guards,
        )
        if sym_lines:
            lines.extend([
                "### Target symbols (wiring gate)",
                "",
                *sym_lines,
                "",
            ])

    if work_order:
        sym_flag = f" --verify-symbols {','.join(recorded_syms)}" if recorded_syms else ""
        lines.extend([
            f"## 5. Closing (mandatory — work order {work_order})",
            "",
            "Record the status through the tool. Do **not** edit the document yourself:",
            "",
            "```",
            *([f'atlas set-status --task "<step substring>" --status done'] if wo_steps else []),
            f'atlas set-status --wo {work_order} --status done{sym_flag}',
            f'atlas publish --wo {work_order} --title "<title>" '
            '--desc "<one sentence: what was implemented>"',
            "```",
            "",
            "`verified: true` in the response is the proof. If the field is missing,",
            "the task is **not** documented — even if the code is finished.",
            "",
        ])
    else:
        lines.extend([
            "## 5. Closing",
            "",
            "Record the status through the tool (MCP: `atlas_publish`, CLI: `atlas publish --wo <N> …`).",
            "Do **not** edit the document yourself. `verified: true` is the proof.",
            "",
        ])

    lines.extend([
        "> If anything is unclear, re-read the anchor from section 1 with `atlas_read`",
        "> instead of opening the whole documentation file.",
        "",
    ])

    text = "\n".join(lines)
    if len(text) > max_chars * 2:
        text = text[: max_chars * 2].rstrip() + "\n\n_… brief truncated._\n"
    return text


def build_queue(
    conn,
    root: Path,
    max_chars: int = MAX_CHARS * 2,
    include_rules: bool = True,
) -> str:
    """Build a sequential multi-Work-Order execution queue (`atlas next+`) covering all open Work Orders."""
    from . import verify

    open_orders = find_open_work_orders(conn)
    if not open_orders:
        return (
            "# Work Order Queue (`atlas next+`): No open Work Orders\n\n"
            "_All documented Work Orders are completed (`✅ DONE`). "
            "Create new ones with `atlas plan \"<topic>\" --step \"...\"`._\n"
        )

    budget = max(1200, min(int(max_chars), 24000))
    sym_map = verify.load_wo_symbols(root)
    shared_guards = verify.symbol_guards(root, conn=conn) if sym_map else {}

    shown_nums = [f"WO {item['wo']}" for item in open_orders[:12]]
    if len(open_orders) > 12:
        shown_nums.append(f"+{len(open_orders) - 12} more")
    wo_numbers = ", ".join(shown_nums)
    lines: list[str] = [
        f"# Work Order Queue (`atlas next+`): {len(open_orders)} open ({wo_numbers})",
        "",
        f"_Generated on {date.today().isoformat()}. Execute each Work Order sequentially from top to bottom. "
        "Do **not** skip ahead: finish every `- [ ]` step, run tests, check off each step with "
        "`atlas_set_status(task=..., status='done')`, and close the Work Order with "
        "`atlas_set_status(wo=<N>, status='done')` before starting the next Work Order._",
        "",
    ]

    rules = _load_rules(root) if include_rules else ""
    rules_block: list[str] = []
    if rules:
        trimmed_rules = rules[: max(300, budget // 4)].rstrip()
        if len(trimmed_rules) < len(rules):
            trimmed_rules += "\n_… guardrails truncated._"
        rules_block = ["## Guardrails (in force for all Work Orders)", "", trimmed_rules, ""]
    rules_reserve = sum(len(r) + 1 for r in rules_block)

    per_wo_budget = max(250, min(SECTION_CHARS, (budget - rules_reserve) // max(1, min(len(open_orders), 6))))
    overflow: list[tuple[int, dict]] = []

    for idx, item in enumerate(open_orders, start=1):
        current_len = sum(len(line) + 1 for line in lines)
        if idx > 1 and current_len + rules_reserve + 320 >= budget:
            overflow = list(enumerate(open_orders[idx - 1 :], start=idx))
            break

        wo_num = item["wo"]
        title = item["title"] or f"Work Order {wo_num}"
        steps = item.get("steps") or []
        done_cnt = sum(1 for s in steps if s.get("done"))
        state_badge = "🚧 IN PROGRESS" if 0 < done_cnt < len(steps) else "⬜ OPEN"
        card: list[str] = [f"## {idx}. Work Order {wo_num} — {state_badge}: {title}", ""]

        # Locate spec section (either explicit spec_anchor or top BM25 hit)
        spec_anchor = item.get("spec_anchor") or ""
        if not spec_anchor:
            hits = search_mod.run_search(conn, title, k=3)
            hit = next(
                (h for h in hits if not h["anchor"].split("~")[0].endswith("#3-work-orders")),
                None,
            )
            if hit:
                spec_anchor = hit["anchor"].split("~")[0]

        files: list[str] = []
        if spec_anchor:
            payload = json.loads(
                search_mod.read(conn, spec_anchor, max_chars=min(SECTION_CHARS, per_wo_budget))
            )
            if "body" in payload:
                links_data = json.loads(search_mod.links(conn, spec_anchor))
                for f in _parse_list(links_data.get("implements", "")):
                    if f not in files:
                        files.append(f)
                for found in FILE_RE.finditer(payload["body"]):
                    rel = found.group(0).replace("res://", "")
                    if rel not in files:
                        files.append(rel)
                card.append(
                    f"**Spec section:** `{spec_anchor}` (*{payload['path']}:{payload['line']} — {payload['heading']}*)"
                )
                card.append("")
                card.append(payload["body"])
                card.append("")

        if files:
            card.append("**Affected files:** " + ", ".join(f"`{f}`" for f in files))
            card.append("")

        if steps:
            card.append(f"**Steps ({done_cnt}/{len(steps)} done):**")
            for step in steps:
                mark = "x" if step.get("done") else " "
                card.append(f"- [{mark}] {step['text']}")
            card.append("")

        recorded_syms = sym_map.get(int(wo_num), [])
        if recorded_syms:
            card.append("**Target symbols (wiring gate):**")
            card.extend(
                _symbol_status_lines(
                    root,
                    recorded_syms,
                    current_wo=wo_num,
                    conn=conn,
                    guards_map=shared_guards,
                )
            )
            card.append("")

        sym_flag = f" --verify-symbols {','.join(recorded_syms)}" if recorded_syms else ""
        card.extend([
            f"**Checkoff for Work Order {wo_num}:**",
            "```",
            *([f'atlas set-status --task "<step substring>" --status done'] if steps else []),
            f"atlas set-status --wo {wo_num} --status done{sym_flag}",
            "```",
            "",
        ])

        card_len = sum(len(c) + 1 for c in card)
        if idx > 1 and current_len + card_len + rules_reserve + 180 > budget:
            overflow = list(enumerate(open_orders[idx - 1 :], start=idx))
            break
        lines.extend(card)

    if overflow:
        lines.extend(["## Queued Next (compact summary)", ""])
        omitted = 0
        for q_idx, q_item in overflow:
            q_wo = q_item["wo"]
            q_title = (q_item["title"] or f"Work Order {q_wo}")[:70]
            q_steps = q_item.get("steps") or []
            q_done = sum(1 for s in q_steps if s.get("done"))
            q_badge = "🚧" if 0 < q_done < len(q_steps) else "⬜"
            summary_line = f"- {q_idx}. **WO {q_wo}** {q_badge}: {q_title} ({q_done}/{len(q_steps)} steps)"
            cur = sum(len(line) + 1 for line in lines)
            if cur + len(summary_line) + rules_reserve + 110 > budget:
                omitted = len(overflow) - (q_idx - overflow[0][0])
                break
            lines.append(summary_line)
        if omitted > 0:
            lines.append(f"- _… +{omitted} more open Work Order(s) queued; re-run `atlas next+` after completing above._")
        lines.append("")

    if rules_block:
        lines.extend(rules_block)

    text = "\n".join(lines)
    if len(text) > budget:
        suffix = "\n\n_… queue truncated._\n"
        text = text[: max(200, budget - len(suffix))].rstrip() + suffix
    return text

