# Project guidelines: gdd-mcp

## Documentation

**Documentation is accessed through the `atlas` MCP server only:**

| Question | Tool |
|---|---|
| "Where is ...?" | `atlas_where("topic")` |
| Full section text | `atlas_read("<anchor>")` |
| Which work orders are done? | `atlas_status()` |
| Plan a new work order + steps | `atlas_plan("topic", steps=[...])` |
| Task brief (or next open WO) | `atlas_brief()` |
| Symbol/file in code | `atlas_code_find("name")` |

**Mandatory:**
- For every documentation question use `atlas_where` first — do not open the
  source files with Read/Grep and do not search the codebase.
- `.atlas/` is off-limits to file tools: the cache is overwritten on the next
  sync, and reading it directly costs tens of thousands of tokens.
- Work order status is a tool call, not a text edit: `atlas_publish` (new
  order) or `atlas_set_status` (tick an existing one off).
- No checkoff without green tests. Checkoff is the last step of a turn.
- Pass `verify_symbols=[...]` when closing: functions that are defined but
  never called do not count as delivered.
- A `✅` is a point-in-time acceptance. If a feature breaks later, the old
  order is not reopened — a new one is created.
