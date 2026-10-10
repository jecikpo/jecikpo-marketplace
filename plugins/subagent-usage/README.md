# subagent-usage

A plugin for Claude Code and Codex that records the usage of each subagent and shows it on
static HTML pages.

For each subagent that stops, a `SubagentStop` hook reads the agent transcript and stores 1 row:
tokens, API calls, tool calls, peak context, active time, the longest turn and, optionally, the
lines of the git commits made while the agent was active. The pages show the data per session,
per model and per role, with 1 trend chart for each role.

## Requirements

- Claude Code or Codex with plugin support.
- Python 3.11 or later (`tomllib`). No other packages.
- `git`, only for the line counts.

## Install in Claude Code

```
/plugin marketplace add jecikpo/jecikpo-marketplace
/plugin install subagent-usage@jecikpo-marketplace
```

For a local copy: `/plugin marketplace add /path/to/jecikpo-marketplace`.

The hook runs in every project where the plugin is enabled. To record only some projects,
install the plugin with the project scope, or disable it in the user scope and enable it in
`<project>/.claude/settings.local.json`:

```json
{ "enabledPlugins": { "subagent-usage@jecikpo-marketplace": true } }
```

## Install in Codex

The repository has a Codex marketplace file (`.agents/plugins/marketplace.json`) and a Codex
manifest (`plugins/subagent-usage/.codex-plugin/plugin.json`). Add the marketplace and install
the plugin with the Codex plugin commands.

**Codex does not run plugin hooks until you trust them.** After the install, run `/hooks` in
Codex, review the `SubagentStop` hook of this plugin and trust it. If the hook changes in a new
version, trust it again.

**The Codex support is not tested with real Codex sessions yet.** It is built from the Codex
source code and from the bug reports of other usage tools. See "Codex data" below.

## Use

The plugin has 1 skill, `subagent-usage`. Ask for it in plain words, or call it directly:

| Request | Result |
|---|---|
| "Show the subagent usage pages" | Generates all pages again and gives the path of `index.html` |
| "Record the subagents of session `<id>`" | Records all subagents of a past session (backfill) |

In Claude Code, the direct call is `/subagent-usage:subagent-usage`.

The same commands without Claude:

```
python3 -I plugins/subagent-usage/scripts/subagent_usage.py --html
python3 -I plugins/subagent-usage/scripts/subagent_usage.py --backfill <session-id | path/to/session.jsonl>
# Codex: <session-id> is the id of the main session; the path is its rollout-*.jsonl file
python3 -I plugins/subagent-usage/scripts/subagent_usage.py --help
```

A row is keyed by the agent id. If an agent is resumed and stops again, or if you run a backfill
again, the row is replaced, not added again.

## Files

| Path | Content |
|---|---|
| `~/.claude/subagent-usage.csv` | The data, 1 row per subagent |
| `~/.claude/subagent-usage/index.html` | All sessions |
| `~/.claude/subagent-usage/sessions/<id>.html` | 1 page per session |
| `~/.claude/subagent-usage/roles.toml` | Your role rules and settings (copied from `defaults/roles.toml` on the first run) |
| `<project>/.claude/subagent-roles.toml` | Optional project rules and settings |

Environment variables change the paths: `SUBAGENT_USAGE_DIR` (pages and `roles.toml`),
`SUBAGENT_USAGE_CSV` (the data file), `CLAUDE_CONFIG_DIR` (the Claude folder) and `CODEX_HOME`
(the Codex folder, default `~/.codex`). Claude Code and Codex use the same data file and pages;
the `tool` column and the "Tool" column on the index page show which tool ran the session.

## Roles

A role groups agents that do the same type of work, for example "implementer" or
"task reviewer". The rules are in TOML:

```toml
[[role]]
name = "implementer"                 # required
description = '^implement task'      # regex on the lowercased agent description
type = "general-purpose"             # optional: exact subagent type
extra_metrics = ["code_added"]       # optional: second line on the role chart
```

- The first rule that matches gives the role. Project rules are tried first.
- `[default] name = "other"` is the role for agents that match no rule.
- The pages calculate the roles each time, so a rule change also applies to old agents.
- A bad rule does not stop the hook. The pages show a warning box with the file and the rule.

The default rules fit the subagent descriptions of the superpowers skills. Add your own rules for
other workflows.

## Settings

```toml
[settings]
git_stats = true
```

`git_stats` counts the lines of the git commits made in the active time of each agent. Set it
to `false` for a project where the agents do not commit. The hook then does not run git, and
the pages hide the code columns and the code chart lines.

## Codex data

- **Transcripts.** Codex writes 1 rollout file per agent in `~/.codex/sessions/`. A subagent file
  has `source.subagent.thread_spawn.parent_thread_id` in its first record.
- **Tokens.** Codex records running totals. The plugin uses the last total minus the total at
  the start of the agent. A forked subagent file starts with a copy of the parent history and
  the parent's total; the plugin skips records in the first 1 s after the file starts.
  **This rule is an assumption.** Check it with real files before you trust the Codex numbers.
- **Input.** In Codex data, the total input includes the cached input. There is no cache write
  value in most records.
- **Output.** Codex records correct output tokens, so Codex agents have an "Output" column.
- **Description.** Codex has no short task label. The plugin uses `agent_path`, then
  `agent_nickname`, then the first line of the task message. Add role rules that fit these
  values, for example `description = '^implement[ _]task'`.
- **Model colors.** The pages color only the Claude model families. GPT models are gray.

## Limits

- **Claude Code output tokens are not recorded.** The transcripts store `output_tokens` at the
  start of the stream, so the value is wrong.
- **Token counts are not costs.** Cache reads have a lower price per token.
- **Active time.** A gap of more than 30 min between 2 transcript records counts as an idle gap,
  not as work. Active times in the summaries are sums, so parallel agents count separately.
- **Line counts use time windows.** A commit counts if its time is in an active period of the
  agent. A commit by the main agent or by a parallel agent in the same period also counts.
  Lock files and folders such as `target/`, `node_modules/` and `idl/` do not count.
- **Pages are generated by the hook.** A large CSV makes each hook run slower. The hook has a
  timeout of 30 s.

## Tests

```
python3 -m unittest discover -s plugins/subagent-usage/tests
```

## License

MIT, see the [LICENSE](../../LICENSE) of the marketplace.
