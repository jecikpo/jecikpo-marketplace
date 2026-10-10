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
| `~/.claude/subagent-usage/roles.toml` | Your role rules and settings. See "Configuration file". |
| `<project>/.claude/subagent-roles.toml` | Optional project rules and settings |

Environment variables change the paths: `SUBAGENT_USAGE_DIR` (pages and `roles.toml`),
`SUBAGENT_USAGE_CSV` (the data file), `CLAUDE_CONFIG_DIR` (the Claude folder) and `CODEX_HOME`
(the Codex folder, default `~/.codex`). Claude Code and Codex use the same data file and pages;
the `tool` column and the "Tool" column on the index page show which tool ran the session.

## Configuration file

The configuration file sets 2 things:

- **Role rules.** A role groups agents that do the same type of work, for example
  "implementer" or "task reviewer". The pages show 1 summary row and 1 trend chart per role.
- **Settings.** At this time there is 1 setting, `git_stats`.

The format is [TOML](https://toml.io).

### Where the files are

| File | Scope | Required |
|---|---|---|
| `~/.claude/subagent-usage/roles.toml` | All projects | Yes. The plugin creates it on the first run. |
| `<project>/.claude/subagent-roles.toml` | 1 project | No. You create it. |

- **First run.** The plugin copies `defaults/roles.toml` to the global file. This happens on the
  first hook run, `--html` or `--backfill`. Then the file is yours. An update of the plugin does
  not change it.
- **Plugin updates.** To get the default rules of a new plugin version, compare your file with
  `defaults/roles.toml` and copy the rules that you want. You can also delete your file: the next
  run creates it again from the new template.
- **Project folder.** The plugin finds the project file in the working folder of the session
  (the `project_dir` column). For Codex, `.claude/subagent-roles.toml` is also the path.
- `SUBAGENT_USAGE_DIR` changes the folder of the global file.

### How the 2 files are merged

1. The rules of the project file come first, then the rules of the global file.
2. The first rule that matches an agent gives its role. Thus a project rule can replace a global
   rule.
3. A `[default]` in the project file replaces the global `[default]` for that project.
4. Each key in the project `[settings]` replaces the same global key for that project.

### Format

```toml
[settings]
git_stats = true

[[role]]
name = "implementer"                 # required: the role name on the pages
description = '^implement task'      # a regular expression on the agent description
type = "general-purpose"             # the exact subagent type
extra_metrics = ["code_added"]       # optional: a second line on the role chart

[default]
name = "other"                       # the role of agents that match no rule
```

`[[role]]` keys:

| Key | Required | Meaning |
|---|---|---|
| `name` | Yes | The role name on the pages. 2 or more rules can give the same name. |
| `description` | `description` or `type` or both | A Python regular expression. The plugin tests it with `re.search` against the **lowercased** agent description. Write the pattern in lowercase. |
| `type` | `description` or `type` or both | The subagent type. It must match exactly, including uppercase letters (for example `Explore`). |
| `extra_metrics` | No | A list of extra lines for the role chart. Known values: `"code_added"` (code lines added, from git). |

- If a rule has `description` and `type`, both must match.
- Write regular expressions in single quotes (`'...'`). TOML then keeps each backslash, so
  `'\bspec\b'` works as written. In double quotes, you must write each backslash 2 times.
- `^` matches the start of the description. Without `^`, the pattern can match at any position.
- The agent description is the `description` of the Agent tool call in Claude Code. For Codex,
  see "Codex data" below.

`[settings]` keys:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `git_stats` | true or false | `true` | Count the lines of the git commits made in the active time of each agent. |

- **`git_stats = false` at record time.** The hook does not run git, and the line columns of the
  new rows stay empty. A later change to `true` does not add counts to these rows.
- **`git_stats = false` at page time.** The pages hide the "Code +", "Code -" and "Commits"
  columns and the `code_added` chart lines.

### When a change takes effect

- **Roles.** The plugin calculates the roles each time it generates the pages. It does not store
  them in the CSV. A rule change thus also applies to old agents. To see the change at once, run
  the skill or `subagent_usage.py --html`.
- **`git_stats`.** This setting applies to the agents that stop after the change, and to the
  pages at the next page generation.

### Errors

A configuration error never stops the hook. The pages show a warning box with the file, the rule
number and the problem.

| Error | Result |
|---|---|
| A rule without `name`, or without `description` and `type` | The rule is skipped |
| A regular expression that is not valid | The rule is skipped |
| An unknown value in `extra_metrics` | The value is ignored |
| An unknown key or a wrong value type in `[settings]` | The key is ignored |
| A TOML syntax error in the global file | The rules of `defaults/roles.toml` are used |
| A TOML syntax error in the project file | The project file is ignored |

The "by role" section of each page names the files that the plugin used.

### Examples

A project where the agents do not commit and the work is analysis:

```toml
# <project>/.claude/subagent-roles.toml
[settings]
git_stats = false

[[role]]
name = "analyst"
description = '\blens\b|^(extract|map|synthesize) '

[default]
name = "misc"
```

Codex agents (the description is `agent_path`, the nickname or the first line of the task):

```toml
[[role]]
name = "implementer"
description = '^implement[ _]task|^you are implementing task'
extra_metrics = ["code_added"]

[[role]]
name = "explorer"
type = "explorer"
```

The default rules fit the subagent descriptions of the superpowers skills
(subagent-driven development, spec and plan reviews).

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
