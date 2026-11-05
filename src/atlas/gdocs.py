"""Google-Docs/Sheets-Sync: OAuth, Export nach Markdown, Cache.

Google Docs liefert über die Drive-API direkt Markdown (`files.export`),
deshalb braucht es keinen eigenen Docs-API-Parser. Sheets werden über die
Sheets-API als Markdown-Tabellen gerendert.
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

# Schreiben nur für `atlas publish` (Work-Order-Zeilen) und `atlas sheet-update`.
# `spreadsheets` (ohne .readonly) ist der Schreib-Scope; `spreadsheets.readonly`
# bleibt zusätzlich drin, damit `sync` weiterhin mit demselben Token läuft.
WRITE_SCOPES = READ_SCOPES + [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

# Rückwärtskompatibel: Standard ist Lesen.
SCOPES = READ_SCOPES

def global_dir() -> Path:
    """`~/.atlas`, sonst das alte `~/.gdd` (Token/Client-Secret von vor der Umbenennung)."""
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

# Das Datenformat der Statuszeilen. Englisch, weil die Dokumente oeffentlich
# lesbar sind — aber die deutschen Varianten bleiben lesbar, damit bestehende
# Dokumente weiter funktionieren.
STATUS_MARK = {"done": "✅ DONE", "open": "⬜ OPEN"}
MARKS_BY_STATUS = {
    "done": ("✅ DONE", "✅ ERLEDIGT", "☑ ERLEDIGT", "☑ DONE"),
    "open": ("⬜ OPEN", "⬜ OFFEN", "☐ OFFEN", "☐ OPEN"),
}


class SyncError(RuntimeError):
    """Fehler mit klarer Handlungsanweisung für den Nutzer."""


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
    """Scopes aus der Token-Datei — nicht die angefragten.

    `Credentials.from_authorized_user_file(path, scopes)` überschreibt die
    gespeicherten Scopes mit dem Parameter. Ein Vergleich darüber ist daher
    immer wahr und ein fehlender Schreib-Scope fällt nicht auf.
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
            # Mit den GESPEICHERTEN Scopes laden, nicht mit den angeforderten:
            # `from_authorized_user_file` überschreibt `creds.scopes` mit dem
            # Parameter, und `to_json()` beim Refresh schreibt genau die zurück.
            # Mit READ_SCOPES würde ein Refresh die Schreib-Scopes aus der
            # Token-Datei löschen — ein `atlas where` hätte dann `publish` kaputt
            # gemacht (am 05.11. genau so passiert).
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
    """Baut den Drive-q-Ausdruck. Klammern sind zwingend (siehe list_documents)."""
    clauses = [
        f"(mimeType = '{DOC_MIME}' or mimeType = '{SHEET_MIME}')",
        "trashed = false",
    ]
    if name_filter:
        safe = name_filter.replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"name contains '{safe}'")
    return " and ".join(clauses)


def list_documents(drive, query: str | None = None, limit: int = 50) -> list[dict]:
    """Sucht Google-Docs und -Sheets, deren Name den Suchbegriff enthält."""
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
    """Google Doc -> Markdown (Google konvertiert selbst)."""
    try:
        data = drive.files().export(fileId=file_id, mimeType="text/markdown").execute()
    except Exception as error:
        raise SyncError(_explain(error)) from error
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def export_sheet(sheets, file_id: str, max_rows: int = 2000, max_cols: str = "Z") -> str:
    """Alle Blätter eines Spreadsheets als Markdown-Tabellen."""
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


def _cache_path(root: Path, name: str) -> Path:
    cache = cache_dir(root) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{_slug(name)}.md"


def assert_writable(conn, file_id: str) -> None:
    """Schreibzugriff nur auf Quellen, die dieses Projekt kennt.

    Das Token hat `documents`-Scope für das gesamte Konto. Ohne diese Prüfung
    könnte ein Agent theoretisch jedes Google Doc überschreiben.
    """
    allowed = {source["id"] for source in known_sources(conn)}
    if file_id not in allowed:
        raise SyncError(
            f"Write access denied: '{file_id}' is not a known source of this "
            "project. Run `atlas sync --query <Name>` first."
        )


def known_sources(conn) -> list[dict]:
    """Die bereits gesyncten Dokumente — Basis für den automatischen Sync."""
    return [
        {"id": row["drive_id"], "name": row["name"], "mimeType": row["mime"]}
        for row in conn.execute("SELECT drive_id, name, mime FROM sync_state")
    ]


def auto_sync(root: Path, conn, max_age_seconds: int = 300, quiet: bool = True):
    """Holt den Google-Stand, aber höchstens alle `max_age_seconds`.

    Wird vor MCP-Tool-Calls und vor status/brief aufgerufen. Ohne Token oder ohne
    bekannte Quellen passiert nichts — der Aufruf ist dann kostenlos.
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

    # Versuch sofort markieren, damit parallele Aufrufe nicht mehrfach syncen.
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_sync_attempt', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(now),),
    )
    conn.commit()

    try:
        return sync(root, sources=sources, conn=conn)
    except Exception as error:  # Netz, Scope, abgelaufenes Token — nie den Tool-Call killen
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
    """Holt Google-Docs/Sheets, schreibt Markdown-Cache und indiziert neu."""
    creds = load_credentials(interactive=False, client_secret=client_secret)
    drive, sheets, _ = build_services(creds)

    # Ohne explizite Quellen: die bereits bekannten Dokumente nachziehen.
    # Erst wenn auch die fehlen, ist ein Namensfilter zwingend.
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
