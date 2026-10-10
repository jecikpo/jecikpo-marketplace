# jecikpo-marketplace

Plugins by jecikpo for Claude Code and Codex.

## Add the marketplace

Claude Code (`.claude-plugin/marketplace.json`):

```
/plugin marketplace add jecikpo/jecikpo-marketplace
```

For a local copy: `/plugin marketplace add /path/to/jecikpo-marketplace`.

Codex (`.agents/plugins/marketplace.json`): add this repository with the Codex plugin commands.

## Plugins

| Plugin | Description | Tools | Install (Claude Code) |
|---|---|---|---|
| [subagent-usage](plugins/subagent-usage/README.md) | Records token usage, active time and git line counts per subagent, and renders static HTML pages with per-role trend charts | Claude Code, Codex (not tested yet) | `/plugin install subagent-usage@jecikpo-marketplace` |

## Add a plugin

1. Create `plugins/<name>/` with `.claude-plugin/plugin.json` and its parts (`skills/`, `commands/`, `hooks/`, `agents/`).
   For Codex, also add `.codex-plugin/plugin.json`. Skills and `hooks/hooks.json` work in both tools.
2. Add an entry to `.claude-plugin/marketplace.json` with `"source": "./plugins/<name>"`.
   For Codex, also add an entry to `.agents/plugins/marketplace.json`.
3. Add a row to the table above.
4. Run `claude plugin validate .` and `claude plugin validate plugins/<name>`.

## License

MIT, see [LICENSE](LICENSE).
