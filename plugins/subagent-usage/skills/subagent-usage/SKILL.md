---
name: subagent-usage
description: Use when the user asks to see, refresh or open the subagent usage pages, or to record the subagents of a past session (backfill). Generates static HTML pages with tokens, active time and code lines per subagent.
---
# Subagent usage pages

The script is `../../scripts/subagent_usage.py`, relative to the folder of this file.
Use its absolute path in the commands below.

1. If the user asks to record a past session, run:
   `python3 -I <script> --backfill <session-id or path to the session .jsonl>`
   For "this session" or "the current session", use the current session id.
2. Otherwise, run: `python3 -I <script> --html`
3. Give the user the path of `index.html` from the last output line, as a `file://` link.
   Report the output in 1 or 2 lines. Do not open or read the HTML files.

If the output says "No subagent transcripts found", tell the user and stop.
