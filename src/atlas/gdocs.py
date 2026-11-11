"""Google Docs/Sheets sync: OAuth, Markdown export, and local cache.

Google Docs exports Markdown directly via the Drive API (`files.export`), so
no custom Docs API parser is needed. Spreadsheets are rendered as Markdown
tables via the Sheets API.
"""

from __future__ import annotations

from .config import cache_dir

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

READ_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# Default OAuth scopes for `atlas auth`: Google Docs read-only (GDD export) +
# Google Sheets read/write (`atlas_sheet_update` / `atlas_sheet_append`).
SHEET_WRITE_SCOPES = READ_SCOPES + [
    "https://www.googleapis.com/auth/spreadsheets",
]

# Opt-in Google Docs write scopes (`atlas auth --docs-write`), required only
# when mirroring Work Orders to Google Docs via `--gdocs` / `ATLAS_SYNC_GDOCS=1`.
WRITE_SCOPES = SHEET_WRITE_SCOPES + [
    "https://www.googleapis.com/auth/documents",
]

# Backward-compatible default: read-only scopes.
SCOPES = READ_SCOPES

def global_dir() -> Path:
    """Return `~/.atlas`, or legacy `~/.gdd` (token/client secret from before the rename)."""
    primary = Path.home() / ".atlas"
    if primary.is_dir():
        return primary
    legacy = Path.home() / ".gdd"
    if legacy.is_dir():
        return legacy
    return primary
TOKEN_PATH = global_dir() / "token.json"
CLIENT_SECRET_PATH = global_dir() / "client_secret.json"

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# Data format for status lines. English because the documents are publicly
# readable — but German variants remain supported so existing documents continue
# to work.
STATUS_MARK = {"done": "✅ DONE", "open": "⬜ OPEN"}
MARKS_BY_STATUS = {
    "done": (
        "✅ DONE",
        "✅ ERLEDIGT",
        "☑ ERLEDIGT",
        "☑ DONE",
        "✅ ABGESCHLOSSEN",
        "☑ ABGESCHLOSSEN",
    ),
    "open": (
        "⬜ OPEN",
        "⬜ OFFEN",
        "☐ OFFEN",
        "☐ OPEN",
        "⬜ AUSSTEHEND",
        "☐ AUSSTEHEND",
        "🚧 IN PROGRESS",
        "🚧 OFFEN",
        "🚧 OPEN",
    ),
}


class SyncError(RuntimeError):
    """Error with a clear actionable instruction for the user."""


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def find_client_secret(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SyncError(f"Client secret not found: {path}")
        return path
    if CLIENT_SECRET_PATH.exists():
        return CLIENT_SECRET_PATH

    downloads = Path.home() / "Downloads"
    matches = sorted(downloads.rglob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if matches:
        return matches[0]
    raise SyncError(
        "No OAuth client found. Download a desktop client in the Google Cloud Console "
        "and save it as ~/.atlas/client_secret.json."
    )


def token_scopes() -> list[str]:
    """Read scopes from the token file — not the requested ones.

    `Credentials.from_authorized_user_file(path, scopes)` overwrites the stored
    scopes with the passed parameter. Comparing against `creds.scopes` would
    therefore always succeed and hide a missing write scope.
    """
    if not TOKEN_PATH.exists():
        return []
    try:
        raw = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    scopes = raw.get("scopes") or []
    return [str(scope) for scope in scopes]


def load_credentials(interactive: bool = False, client_secret: str | None = None,
                     scopes: list[str] | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    wanted = scopes or READ_SCOPES
    granted = token_scopes()
    missing = [scope for scope in wanted if scope not in granted]

    creds = None
    if TOKEN_PATH.exists() and not missing:
        try:
            # Load with the STORED scopes, not the requested ones:
            # `from_authorized_user_file` overwrites `creds.scopes` with the
            # parameter, and `to_json()` writes those back on refresh.
            # Passing READ_SCOPES would strip write scopes from the token file
            # on refresh — an `atlas where` call would then break `publish`
            # (which happened on Nov 5).
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), granted or wanted)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except Exception:
            creds = None

    if not interactive:
        needed = "write scope (`documents`)" if any("documents" in s for s in missing) else "read scopes"
        raise SyncError(
            f"No valid Google token for {needed}. Run `atlas auth` once "
            "(opens the browser for consent)."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = find_client_secret(client_secret)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), wanted)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save(creds)
    return creds


def _save(creds) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def token_status() -> dict:
    granted = token_scopes()
    return {
        "authorized": bool(granted),
        "scopes": granted,
        "write_access": any(scope.endswith("/auth/documents") for scope in granted),
        "sheet_write_access": any(scope.endswith("/auth/spreadsheets") for scope in granted),
        "token": str(TOKEN_PATH),
    }


# --------------------------------------------------------------------------
# Google-API
# --------------------------------------------------------------------------

def _explain(error: Exception) -> str:
    text = str(error)
    message = text
    activation = ""
    try:
        payload = json.loads(error.content.decode("utf-8"))  # type: ignore[attr-defined]
        message = payload.get("error", {}).get("message", text)
        for detail in payload.get("error", {}).get("details", []):
            activation = detail.get("metadata", {}).get("activationUrl", "") or activation
    except Exception:
        pass

    if "has not been used in project" in message or "accessNotConfigured" in text:
        hint = f" The API is not enabled in the Cloud project: {message}"
        if activation:
            hint += f"\nEnable at: {activation}"
        return hint
    if "insufficient" in message.lower() or "insufficientPermissions" in text:
        return (
            "The token has insufficient permissions. Re-run `atlas auth --force` "
            f"to confirm all scopes. ({message})"
        )
    return message


def build_services(creds):
    from googleapiclient.discovery import build

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    return drive, sheets, docs


def build_query(name_filter: str | None = None) -> str:
    """Build the Drive `q` expression. Parentheses are mandatory (see list_documents)."""
    clauses = [
        f"(mimeType = '{DOC_MIME}' or mimeType = '{SHEET_MIME}')",
        "trashed = false",
    ]
    if name_filter:
        safe = name_filter.replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"name contains '{safe}'")
    return " and ".join(clauses)


def list_documents(drive, query: str | None = None, limit: int = 50) -> list[dict]:
    """Find Google Docs and Sheets whose name contains the search term."""
    params = {
        "q": build_query(query),
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
        "pageSize": limit,
        "orderBy": "modifiedTime desc",
    }
    try:
        response = drive.files().list(**params).execute()
    except Exception as error:
        raise SyncError(_explain(error)) from error
    return response.get("files", [])


def export_document(drive, file_id: str) -> str:
    """Export a Google Doc as Markdown (converted by Google)."""
    try:
        data = drive.files().export(fileId=file_id, mimeType="text/markdown").execute()
    except Exception as error:
        raise SyncError(_explain(error)) from error
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def export_sheet(sheets, file_id: str, max_rows: int = 2000, max_cols: str = "Z") -> str:
    """Render all tabs of a spreadsheet as Markdown tables."""
    try:
        meta = sheets.spreadsheets().get(
            spreadsheetId=file_id, includeGridData=False
        ).execute()
    except Exception as error:
        raise SyncError(_explain(error)) from error

    parts: list[str] = []
    for sheet in meta.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "Sheet")
        escaped = title.replace("'", "''")
        try:
            values = sheets.spreadsheets().values().get(
                spreadsheetId=file_id,
                range=f"'{escaped}'!A1:{max_cols}{max_rows}",
            ).execute().get("values", [])
        except Exception as error:
            parts.append(f"## {title}\n\n_(unreadable: {_explain(error)})_\n")
            continue

        parts.append(f"## {title}\n")
        if not values:
            parts.append("_(empty)_\n")
            continue

        width = max(len(row) for row in values)
        for index, row in enumerate(values):
            cells = [str(cell).replace("|", "\\|").replace("\n", " ").strip() for cell in row]
            cells += [""] * (width - len(cells))
            parts.append("| " + " | ".join(cells) + " |")
            if index == 0:
                parts.append("|" + "|".join(["---"] * width) + "|")
        parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    from .slug import slugify

    return slugify(text, max_len=48)


def _frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


_WO_LINE_RE = re.compile(
    r"^[ \t>*#-]*Work\s*Order\s*(?P<wo>\d+)\s*(?:—|–|-|:)\s*(?:✅|⬜|☑|☐|🚧).*$",
    re.IGNORECASE | re.MULTILINE,
)
_CACHE_STEP_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+\[(?P<mark>[ xX])\][ \t]+(?P<text>.+)$")
_CACHE_DONE_RE = re.compile(r"✅|☑|\bERLEDIGT\b|\bABGESCHLOSSEN\b|\bDONE\b", re.IGNORECASE)
_CACHE_OPEN_MARK_RE = re.compile(
    r"(?:⬜|☐)\s*(?:OFFEN|AUSSTEHEND|OPEN)|🚧\s*(?:IN\s+PROGRESS|OFFEN|OPEN)?|⬜|☐|🚧",
    re.IGNORECASE,
)


def _extract_wo_blocks(text: str) -> dict[int, dict[str, Any]]:
    """Extract `{wo_num: {'header': str, 'block': str, 'done': bool, 'steps': [(done, text, raw), ...]}}`."""
    lines = text.splitlines()
    blocks: dict[int, dict[str, Any]] = {}
    idx = 0
    while idx < len(lines):
        m = _WO_LINE_RE.match(lines[idx])
        if not m:
            idx += 1
            continue
        wo_num = int(m.group("wo"))
        header = lines[idx].strip()
        block_lines = [header]
        steps: list[tuple[bool, str, str]] = []
        j = idx + 1
        while j < len(lines):
            nxt = lines[j]
            stripped = nxt.strip()
            if stripped.startswith("#") or _WO_LINE_RE.match(nxt):
                break
            sm = _CACHE_STEP_RE.match(nxt)
            if sm:
                block_lines.append(nxt.rstrip())
                steps.append((sm.group("mark").lower() == "x", sm.group("text").strip(), nxt.rstrip()))
            elif not stripped and steps:
                # Allow blank line inside or after; stop block capture if next non-blank isn't a step
                if j + 1 < len(lines) and _CACHE_STEP_RE.match(lines[j + 1]):
                    j += 1
                    continue
                break
            elif stripped and not steps:
                # Prose line directly under WO before steps
                block_lines.append(nxt.rstrip())
                j += 1
                continue
            else:
                break
            j += 1
        prefix_window = header[m.end("wo") - (len(lines[idx]) - len(lines[idx].lstrip())):][:36]
        is_done = bool(_CACHE_DONE_RE.search(prefix_window))
        blocks[wo_num] = {
            "header": header,
            "block": "\n".join(block_lines),
            "done": is_done,
            "steps": steps,
        }
        idx = j
    return blocks


def _latest_audit_states(root: Path) -> tuple[dict[int, str], list[tuple[int | None, str, str]]]:
    """Read authoritative recent WO and WO-scoped task statuses from `mutations.log`."""
    from . import audit

    status_map = {
        "done": "done",
        "erledigt": "done",
        "abgeschlossen": "done",
        "open": "open",
        "offen": "open",
        "ausstehend": "open",
        "todo": "open",
        "in_progress": "open",
        "in progress": "open",
    }
    wo_state: dict[int, str] = {}
    task_entries: list[tuple[int | None, str, str]] = []
    for entry in audit.tail(root, limit=60):
        if entry.get("refused") or entry.get("verified") is False:
            continue
        raw_st = str(entry.get("status") or "").strip().lower()
        if not raw_st and entry.get("new"):
            raw_st = "done" if _CACHE_DONE_RE.search(str(entry["new"])) else "open"
        norm_st = status_map.get(raw_st, "")
        if not norm_st:
            continue
        entry_wo: int | None = None
        if entry.get("wo") is not None:
            try:
                entry_wo = int(entry["wo"])
            except (TypeError, ValueError):
                entry_wo = None
        if entry.get("task"):
            task_q = str(entry["task"]).strip().casefold()
            if task_q:
                task_entries.append((entry_wo, task_q, norm_st))
        elif entry_wo is not None:
            wo_state[entry_wo] = norm_st
    return wo_state, task_entries


def _rescue_cached_work_orders(root: Path, filename: str, new_body: str) -> str:
    """Preserve Work Order blocks, DONE transitions, and checked steps from `.atlas/cache` when Drive export lags."""
    from .config import CACHE_DIR, LEGACY_CACHE_DIR

    new_blocks = _extract_wo_blocks(new_body)
    present_wos = set(new_blocks.keys())
    wo_audit, task_entries = _latest_audit_states(root)

    cached_blocks: dict[int, dict[str, Any]] = {}
    for cdir in (root / CACHE_DIR / "cache", root / LEGACY_CACHE_DIR / "cache"):
        candidate = cdir / filename
        if not candidate.exists():
            continue
        try:
            old_text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for wo_num, info in _extract_wo_blocks(old_text).items():
            if wo_num not in cached_blocks:
                cached_blocks[wo_num] = info

    # Count distinct Work Orders containing each normalized step text so legacy
    # unscoped task audit entries never spill over onto duplicate steps in other WOs.
    step_wos: dict[str, set[int]] = {}
    for source_map in (new_blocks, cached_blocks):
        for wo_num, info in source_map.items():
            for _done, s_txt, _raw in (info.get("steps") or []):
                step_wos.setdefault(s_txt.casefold(), set()).add(wo_num)

    # 1. Prevent stale Drive export from downgrading confirmed DONE status or checked steps on present WOs
    patched_lines = new_body.splitlines()
    current_wo: int | None = None
    for i, line in enumerate(patched_lines):
        wm = _WO_LINE_RE.match(line)
        if wm:
            current_wo = int(wm.group("wo"))
            cached_info = cached_blocks.get(current_wo)
            audit_st = wo_audit.get(current_wo)
            should_be_done = (
                audit_st == "done"
                or (cached_info is not None and cached_info["done"] and audit_st != "open")
            )
            if should_be_done and not new_blocks[current_wo]["done"]:
                patched_lines[i] = _CACHE_OPEN_MARK_RE.sub("✅ DONE", line, count=1)
            continue
        if line.strip().startswith("#"):
            current_wo = None
            continue
        if current_wo is not None:
            sm = _CACHE_STEP_RE.match(line)
            if sm and sm.group("mark") == " ":
                step_txt = sm.group("text").strip()
                step_key = step_txt.casefold()
                cached_steps = {
                    t.casefold(): d
                    for d, t, _raw in (cached_blocks.get(current_wo, {}).get("steps") or [])
                }
                is_cached_done = bool(cached_steps.get(step_key))
                is_unique_step = len(step_wos.get(step_key, set())) <= 1
                explicit_task: str | None = None
                for entry_wo, q, st in reversed(task_entries):
                    if not q or q not in step_key:
                        continue
                    if entry_wo is not None:
                        if entry_wo == current_wo:
                            explicit_task = st
                            break
                    elif is_cached_done or is_unique_step:
                        explicit_task = st
                        break
                if explicit_task == "done" or (is_cached_done and explicit_task != "open"):
                    bracket_pos = line.find("[ ]")
                    if bracket_pos != -1:
                        patched_lines[i] = line[:bracket_pos] + "[x]" + line[bracket_pos + 3:]

    result_body = "\n".join(patched_lines)
    if new_body.endswith("\n") and not result_body.endswith("\n"):
        result_body += "\n"

    # 2. Rescue completely missing Work Order blocks (including checklist steps)
    rescued_blocks: list[tuple[int, str]] = []
    for wo_num, info in cached_blocks.items():
        if wo_num not in present_wos:
            present_wos.add(wo_num)
            rescued_blocks.append((wo_num, info["block"]))

    if not rescued_blocks:
        return result_body

    rescued_text = "\n\n".join(block for _, block in sorted(rescued_blocks))
    return f"{result_body.rstrip()}\n\n{rescued_text}\n"


def _cache_path(root: Path, name: str) -> Path:
    cache = cache_dir(root) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{_slug(name)}.md"


def assert_writable(conn, file_id: str) -> None:
    """Allow write access only to sources registered in this project.

    The token has `documents` scope for the entire account. Without this check,
    an agent could theoretically overwrite any Google Doc.
    """
    allowed = {source["id"] for source in known_sources(conn)}
    if file_id not in allowed:
        raise SyncError(
            f"Write access denied: '{file_id}' is not a known source of this "
            "project. Run `atlas sync --query <Name>` first."
        )


def recover_sources_from_cache(conn) -> list[dict]:
    """Re-populate `sync_state` from `.atlas/cache/*.md` or `.gdd/cache/*.md` frontmatter if `sync_state` was cleared."""
    if conn is None:
        return []
    root_path: Path | None = None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if row and row["value"]:
            root_path = Path(row["value"])
    except Exception:
        root_path = None
    if root_path is None:
        from . import status as status_mod
        root_path = status_mod._infer_root(conn)
    if root_path is None:
        return []

    from .config import CACHE_DIR, LEGACY_CACHE_DIR
    cache_dirs = [
        d for d in (root_path / CACHE_DIR / "cache", root_path / LEGACY_CACHE_DIR / "cache")
        if d.is_dir()
    ]
    if not cache_dirs:
        return []

    from .chunker import parse_frontmatter
    from .indexer import set_sync_state

    recovered: list[dict] = []
    seen_ids: set[str] = set()
    for cdir in cache_dirs:
        for path in sorted(cdir.glob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body, _ = parse_frontmatter(raw)
            drive_id = str(meta.get("drive_id") or "").strip()
            if not drive_id or drive_id in seen_ids or str(meta.get("source") or "").strip() != "gdrive":
                continue
            seen_ids.add(drive_id)
            kind = str(meta.get("kind") or "doc").strip()
            mime = SHEET_MIME if kind == "sheet" else DOC_MIME
            name = str(meta.get("title") or path.stem).strip()
            raw_sync = meta.get("synced_at")
            synced_at = (
                raw_sync.isoformat()
                if hasattr(raw_sync, "isoformat")
                else str(raw_sync or "").strip()
            )
            set_sync_state(conn, drive_id, name, mime, "", synced_at, len(body), str(path))
            recovered.append({"id": drive_id, "name": name, "mimeType": mime})
    return recovered


def known_sources(conn) -> list[dict]:
    """Return previously synced documents — basis for automatic sync."""
    if conn is None:
        return []
    rows = [
        {"id": row["drive_id"], "name": row["name"], "mimeType": row["mime"]}
        for row in conn.execute("SELECT drive_id, name, mime FROM sync_state")
    ]
    if rows:
        return rows
    return recover_sources_from_cache(conn)


def auto_sync(root: Path, conn, max_age_seconds: int = 300, quiet: bool = True):
    """Fetch the latest Google state, at most once every `max_age_seconds`.

    Called before MCP tool calls and before status/brief. Without a token or
    known sources this is a no-op with zero overhead.
    """
    import time

    if os.environ.get("GDD_AUTO_SYNC", "1").lower() in ("0", "false", "no", "off"):
        return None
    if not TOKEN_PATH.exists():
        return None

    sources = known_sources(conn)
    if not sources:
        return None

    now = time.time()
    row = conn.execute("SELECT value FROM meta WHERE key='last_sync_attempt'").fetchone()
    if row is not None:
        try:
            if now - float(row["value"]) < max_age_seconds:
                return None
        except (TypeError, ValueError):
            pass

    # Record the attempt immediately so concurrent calls do not sync repeatedly.
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_sync_attempt', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(now),),
    )
    conn.commit()

    try:
        return sync(root, sources=sources, conn=conn)
    except Exception as error:  # network, scope, expired token — never kill the tool call
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_sync_error', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} {error}"[:400],),
        )
        conn.commit()
        if not quiet:
            raise
        return None


def sync(
    root: Path,
    query: str | None = None,
    sources: list[dict] | None = None,
    force: bool = False,
    client_secret: str | None = None,
    conn=None,
) -> dict:
    """Fetch Google Docs/Sheets, write the Markdown cache, and re-index."""
    creds = load_credentials(interactive=False, client_secret=client_secret)
    drive, sheets, _ = build_services(creds)

    # Without explicit sources: refresh the already known documents.
    # A name filter is only required when none are registered yet.
    if not sources and not query and conn is not None:
        sources = known_sources(conn)

    if not sources and not query:
        raise SyncError(
            "No source known yet. Provide a name filter on the first run, "
            "e.g.  atlas sync --query MYPROJECT"
        )

    targets = sources or list_documents(drive, query=query)
    if not targets:
        raise SyncError(
            f"No Google Docs/Sheets found (query: {query!r}). "
            "Check that the name matches and the account has access."
        )

    from .indexer import get_sync_state, refresh, set_sync_state

    written: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for target in targets:
        file_id = target["id"]
        name = target.get("name", file_id)
        mime = target.get("mimeType", "")
        try:
            meta = drive.files().get(
                fileId=file_id, fields="id,name,mimeType,modifiedTime,version"
            ).execute()
        except Exception as error:
            errors.append({"name": name, "error": _explain(error)})
            continue

        state = get_sync_state(conn, file_id) if conn is not None else None
        if (
            not force
            and state is not None
            and state["modified"] == meta.get("modifiedTime")
            and Path(state["cache_path"]).exists()
        ):
            skipped.append({"name": name, "reason": "unchanged"})
            continue

        try:
            if mime == DOC_MIME:
                body = export_document(drive, file_id)
                kind = "doc"
            elif mime == SHEET_MIME:
                body = export_sheet(sheets, file_id)
                kind = "sheet"
            else:
                continue
        except SyncError as error:
            errors.append({"name": name, "error": str(error)})
            continue

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        header = _frontmatter({
            "id": _slug(name),
            "title": name,
            "source": "gdrive",
            "kind": kind,
            "drive_id": file_id,
            "synced_at": stamp,
            "tags": "[atlas, gdrive]",
        })
        path = _cache_path(root, name)
        if kind == "doc":
            body = _rescue_cached_work_orders(root, path.name, body)
        path.write_text(f"{header}\n\n{body.strip()}\n", encoding="utf-8")

        if conn is not None:
            set_sync_state(conn, file_id, name, mime, meta.get("modifiedTime", ""),
                           stamp, len(body), str(path))

        written.append({
            "name": name,
            "kind": kind,
            "chars": len(body),
            "cache": path.relative_to(root).as_posix(),
        })

    if conn is not None and written:
        refresh(conn, root)

    return {
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "cache_dir": str((cache_dir(root) / "cache")),
    }
