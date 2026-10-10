# jecikpo-marketplace

Claude Code plugins by jecikpo.

## Add the marketplace

```
/plugin marketplace add jecikpo/jecikpo-marketplace
```

For a local copy: `/plugin marketplace add /path/to/jecikpo-marketplace`.

## Plugins

| Plugin | Description | Install |
|---|---|---|
| [subagent-usage](plugins/subagent-usage/README.md) | Records token usage, active time and git line counts per subagent, and renders static HTML pages with per-role trend charts | `/plugin install subagent-usage@jecikpo-marketplace` |

## Add a plugin

1. Create `plugins/<name>/` with `.claude-plugin/plugin.json` and its parts (`skills/`, `commands/`, `hooks/`, `agents/`).
2. Add an entry to `.claude-plugin/marketplace.json` with `"source": "./plugins/<name>"`.
3. Add a row to the table above.
4. Run `claude plugin validate .` and `claude plugin validate plugins/<name>`.

## License

MIT, see [LICENSE](LICENSE).
