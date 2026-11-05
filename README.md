# atlas — Projekt-Doku als Index, nicht als Kontextfresse

Ein lokaler MCP-Server für Coding-Agenten. Der Agent fragt „Wo steht das
Kampfsystem?" und bekommt **Datei, Zeile und Abschnitt** zurück — nicht das
ganze Dokument.

Ein 5-Tab-Google-Doc mit 124 Abschnitten kostet **20.364 Tokens**, wenn man es
liest. Die Antwort auf dieselbe Frage kostet **875**. Gemessen, nicht geschätzt.

```
Frage:   "Wo finde ich das Kampfsystem?"
Antwort: gdd/02-kampfsystem.md:10  § 2. Kampfsystem
         284 Zeichen ≈ 84 Tokens        (statt ~2.000 für das Dokument)
```

Kein Vektor-Index, kein Embedding-Modell, kein API-Key. SQLite-FTS5 mit
BM25-Ranking, deutschen Stoppwörtern, Umlaut-Folding und Alias-Map.

---

## Was es kann

**1. Finden statt lesen.** `atlas_where` → Anker, `atlas_read` → Abschnitt.
Ein Abschnitt statt eines Dokuments. Antworten sind hart budgetiert: 400 bis
3.000 Zeichen, nie „hier ist alles".

**2. Briefs statt Aufgabenbeschreibungen.** `atlas_brief("Skill-System
implementieren")` liefert die relevanten Abschnitte, die betroffenen
Code-Dateien und die Projektregeln als ein Dokument — der Prompt für einen
Subagenten, statt ihn raten zu lassen.

**3. Abnahme mit Nachweis.** `atlas_publish` schreibt eine Statuszeile ins
Dokument und liest sie zurück, bevor es Erfolg meldet. Und es **verweigert** die
Abnahme, wenn ein Symbol zwar definiert, aber nirgends aufgerufen ist:

```json
{
  "written": false,
  "refused": true,
  "refusal": "Abnahme verweigert — Anbindungs-Nachweis fehlgeschlagen:\n  ✗ '_dispatch_unit_class_skill' ist in scripts/turn_manager.gd:3774 definiert, wird aber nirgends aufgerufen oder gebunden (0 Aufrufe, 0 Referenzen)\n\nEin ✅ ohne Aufrufer ist die Dokumentation einer Absicht, nicht eines Ergebnisses."
}
```

Das ist der Grund, warum es dieses Tool gibt. Ein Agent hatte 1.070 Zeilen
implementiert, den Aufrufer vergessen und „fertig" gemeldet. Der nächste
Compiler-Hotfix sah die Warnung „unused function" und löschte alles. Die Arbeit
war weg, weil niemand gemerkt hatte, dass eine Funktion ohne Aufrufer kein
Feature ist.

---

## Quickstart (offline, ohne Google)

```bash
git clone https://github.com/<user>/atlas-brief-mcp
cd atlas-brief-mcp
uv sync

# Mitgeliefertes Demo-Repo indizieren
uv run atlas --root examples/demo doctor
uv run atlas --root examples/demo where "Wo finde ich das Kampfsystem?"
uv run atlas --root examples/demo brief "Schadensberechnung"
```

Läuft mit **jedem Ordner voller Markdown**. `docs/`, `specs/`, `atlas/` oder
`gdd/` — alles, was `.md` ist, wird indiziert. Der Index landet in
`<root>/.atlas/index.sqlite` und ist jederzeit löschbar; er wird aus dem
Markdown neu gebaut. Das Markdown bleibt die Wahrheit.

---

## Bestehendes Repo erfassen

```bash
cd dein-projekt
uv run atlas init
```

Scannt das Repository (kein LLM, kein Netzwerk, unter 2 Sekunden), schreibt
`docs/ARCHITECTURE.md` mit:

- **Domänen**: jeder Quellordner mit Dateien, Zeilen, Tokenschätzung und
  Kern-Symbolen
- **Kontext-Hotspots**: Dateien über 10.000 Tokens, die man nie am Stück lesen darf
- **Unverdrahtete Symbole**: Funktionen über 15 Zeilen, deren Name außerhalb
  der Definition nirgends vorkommt — die Kandidaten für „implementiert, aber
  nie angeschlossen"

Dazu `AGENTS.md`, `.antigravityrules`, `.mcp.json` und `.atlas/` in
`.gitignore` — **aber nur, wenn sie nicht existieren.** `atlas init` überschreibt
nichts. Ein Bootstrap, der deine handgeschriebenen Regeln plattmacht, wird genau
einmal benutzt.

```bash
atlas init --out docs/ARCH.md   # anderer Pfad
atlas init --force              # Doku bewusst neu erzeugen
```

---

## Google Docs & Sheets (optional)

Wenn die Doku in Google Docs lebt statt in Markdown, indiziert `atlas` sie über
die Docs-API — inklusive mehrerer Dokument-Tabs.

```bash
atlas auth                     # einmalig: OAuth-Consent im Browser
atlas sync --query GOREQUEST   # Dokumente in den Cache holen
atlas status                   # Work Orders mit Status
atlas publish --wo 7 --title "Mission 1" --desc "Räume 1–9"
```

Drei Details, die den Unterschied machen:

- **Gelesen wird exportiert, geurteilt wird direkt.** Der Suchindex wird aus dem
  Drive-Export gefüllt; die Verifikation nach einem Write läuft über
  `documents.get`, weil die Docs-API nach `batchUpdate` sofort konsistent ist und
  der Drive-Export 0,5–2 Sekunden nachhinkt. Wer über den Export verifiziert,
  baut sich ein `verified: false` ein, das keines ist.
- **Schreiben ist append-only mit Revisionssperre.** `requiredRevisionId`
  verhindert, dass ein Write auf einer veralteten Revision landet.
- **Statuswechsel per `tabsCriteria`.** Google Docs hat Tabs; `replaceAllText`
  ohne Tab-Sperre ändert denselben String auch in Archiv-Kopien.

Balancing-Werte gehen über `atlas_sheet_update` — mit `row_key` und
**Spaltenname**, nie mit Zellkoordinate. Ein Agent, der `C14` schreibt, verzählt
sich um eine Zeile und überschreibt den Nachbarn.

---

## MCP andocken

**Claude Code** — `.mcp.json` im Projektroot:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "C:/Pfad/zu/atlas-brief-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "atlas", "serve"],
      "env": { "ATLAS_ROOT": "." }
    }
  }
}
```

`ATLAS_ROOT=.` wird relativ zum Arbeitsverzeichnis des Clients aufgelöst — eine
Installation genügt für alle Projekte.

**Antigravity** — Eintrag in `~/.gemini/config/mcp_config.json`, gleiche Struktur.

Der Server bringt eigene `instructions` mit. Der Client liest sie beim Verbinden
und weiß dadurch, dass Doku-Fragen über `atlas_where` laufen — nicht über
`Read`/`Grep`, und dass ein Statusaufruf keiner Bestätigung durch die CLI bedarf.

### Die 15 Tools

| Tool | Zweck |
|---|---|
| `atlas_where` / `atlas_search` | Thema → Anker (BM25, deutsche Stoppwörter) |
| `atlas_read` | Anker → Abschnitt im Volltext, Budget-kontrolliert |
| `atlas_outline` | Gliederung eines Dokuments |
| `atlas_tasks` | offene Checkbox-Tasks |
| `atlas_links` | Querverweise eines Abschnitts |
| `atlas_brief` | Task-Brief: Abschnitte + Dateien + Projektregeln |
| `atlas_code_find` | Symbol → Datei, Zeile, Signatur **und Anbindung** |
| `atlas_status` | Work Orders, mit Zeitstempel der letzten Synchronisation |
| `atlas_publish` / `atlas_set_status` | Statuszeile schreiben / abhaken, verifiziert |
| `atlas_sheets` / `atlas_sheet_update` / `atlas_sheet_append` | Tabellen lesen/schreiben |
| `atlas_lint` | Struktur- und Formatprüfung |

---

## Wie es funktioniert

- **Index**: SQLite mit FTS5 (`external content`), BM25-Ranking, Präfix-Suche.
  Section-Anker statt Datei-Granularität: ~60 Tokens Treffer statt 2.000.
- **Deutsch**: Umlaut-Folding, Stoppwörter, Komposita-Zerlegung — aber nur, wenn
  beide Teile im Vokabular stehen. Lieber null Treffer als ein falscher.
- **Kein Embedding**: Synonyme kommen aus `.atlas/aliases.yml` im Projekt, nicht
  aus einem Modell. Nachvollziehbar, offline, in Millisekunden.
- **Schreiben**: atomar, verifiziert, protokolliert in `.atlas/mutations.log`.

---

## Was es nicht ist

- **Kein Dokumenteneditor.** Es schreibt Statuszeilen und Tabellenzellen, keine
  Design-Prosa.
- **Kein Crawler.** Es indiziert, was in den Doku-Ordnern liegt.
- **Kein Ersatz für Tests.** Das Anbindungs-Gate prüft, ob eine Funktion
  aufgerufen wird — nicht, ob sie das Richtige tut.
- **Nicht für Windows-only gebaut**, aber dort entwickelt und getestet.

---

## Tests

```bash
uv run pytest -q      # 155 Tests, keine Netzwerkzugriffe
uv run python scripts/smoke_mcp.py --root examples/demo   # JSON-RPC-Smoke: 15 Tools
```

Die Tests laufen ohne Google-Zugang. Alles, was die Docs-API braucht, wird
gefaked — inklusive der Fälle, in denen der Fake einen Write verschluckt, weil
genau das in Produktion passiert ist.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
