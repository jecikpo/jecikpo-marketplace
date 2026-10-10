---
description: Regenerate the subagent usage pages, or record a past session with "backfill <session-id>"
argument-hint: "[backfill <session-id>]"
allowed-tools: Bash(python3 -I *)
---
Run the matching command and report its output in 1 or 2 lines.

- If the arguments are empty, run:
  `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/subagent_usage.py" --html`
- If the arguments are `backfill <session>`, run:
  `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/subagent_usage.py" --backfill <session>`
  For "this session" or "current", use the current session id.

The last line of the output is the path of index.html. Give it to the user as a file:// link.

Arguments: $ARGUMENTS
