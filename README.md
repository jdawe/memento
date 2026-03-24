# Memento

Persistent two-layer memory for [OpenClaw](https://openclaw.ai). Fully local, zero vendor dependency.

- **Layer 1: Semantic Memory** — LanceDB + Ollama. Auto-captures facts, preferences, and decisions. Auto-recalls relevant context before each response.
- **Layer 2: Verbatim Search** — SQLite FTS5 over session transcripts. Exact-match search across your entire conversation history.

```
┌─────────────────────────────────────────────────────┐
│                    Memento                           │
│                                                     │
│  ┌──────────────────┐    ┌───────────────────────┐  │
│  │  Layer 1:        │    │  Layer 2:             │  │
│  │  Semantic Memory │    │  Verbatim Search      │  │
│  │                  │    │                       │  │
│  │  LanceDB         │    │  SQLite + FTS5        │  │
│  │  + Ollama        │    │  + JSONL indexer      │  │
│  │  (nomic-embed)   │    │                       │  │
│  │                  │    │                       │  │
│  │  "What was that  │    │  "Find every message  │  │
│  │   thing about    │    │   containing the word │  │
│  │   budgets?"      │    │   'migration'"        │  │
│  └──────────────────┘    └───────────────────────┘  │
│                                                     │
│  Semantic ≈ fuzzy recall    Verbatim ≈ exact search │
└─────────────────────────────────────────────────────┘
```

---

## Prerequisites

- [OpenClaw](https://openclaw.ai) installed and running
- Python 3.9+
- macOS or Linux

---

## Layer 1: Semantic Memory (LanceDB)

### Install Ollama

```bash
brew install ollama        # macOS
# or: curl -fsSL https://ollama.com/install.sh | sh   # Linux

ollama serve &             # starts local API on 127.0.0.1:11434
ollama pull nomic-embed-text   # 274MB, 768-dim, fast on Apple Silicon
```

### Enable the plugin

Copy `examples/openclaw-plugin-config.json` into your `~/.openclaw/openclaw.json` (merge with existing config):

```json
{
  "plugins": {
    "memory": "memory-lancedb",
    "entries": {
      "memory-lancedb": {
        "enabled": true,
        "config": {
          "embedding": {
            "apiKey": "ollama",
            "model": "nomic-embed-text",
            "baseURL": "http://127.0.0.1:11434/v1"
          },
          "autoCapture": true,
          "autoRecall": true
        }
      }
    }
  }
}
```

> **⚠️ Use `127.0.0.1`, not `localhost`.** Node.js may resolve `localhost` to IPv6 (`::1`), but Ollama only listens on IPv4. This causes silent connection failures.

### Restart and verify

```bash
openclaw gateway restart
openclaw status
# Look for: Memory │ enabled (plugin memory-lancedb)
```

### How it works

| Feature | Description |
|---|---|
| **Auto-capture** | Extracts facts, preferences, decisions from every conversation and embeds them |
| **Auto-recall** | Searches stored memories before each response, injects relevant context |
| **Manual tools** | `memory_store` — save explicitly. `memory_recall` — search explicitly |

Data lives at `~/.openclaw/memory/lancedb/` (~1-5 MB per thousand memories).

---

## Layer 2: Verbatim Search (SQLite FTS5)

### Setup

```bash
# Clone this repo (or just grab scripts/transcript-search.py)
git clone https://github.com/jdawe/memento.git
cd memento

# Run initial index
python3 scripts/transcript-search.py index
python3 scripts/transcript-search.py stats
```

### Configuration

The script reads two environment variables (with sensible defaults):

| Variable | Default | Description |
|---|---|---|
| `MEMENTO_SESSIONS_DIR` | `~/.openclaw/agents/main/sessions` | Path to OpenClaw JSONL session files |
| `MEMENTO_DB_PATH` | `~/.openclaw/workspace/data/transcripts.db` | Path to the SQLite database |

Override them if your setup differs:

```bash
MEMENTO_SESSIONS_DIR=/custom/path MEMENTO_DB_PATH=/custom/db.sqlite python3 scripts/transcript-search.py index
```

### Usage

```bash
# Search all conversations
python3 scripts/transcript-search.py search "budget spreadsheet"

# Filter by role and date range
python3 scripts/transcript-search.py search "API migration" --role assistant --after 2026-03-01

# Full message content (no truncation)
python3 scripts/transcript-search.py search "architecture" --full --limit 5

# Rebuild from scratch
python3 scripts/transcript-search.py reindex

# Show stats
python3 scripts/transcript-search.py stats
```

### Auto-indexing

Keep the index fresh with a cron job:

```bash
# Via OpenClaw cron (recommended):
openclaw cron add transcript-indexer --every 30m \
  --message "Run: bash /path/to/memento/scripts/transcript-index-cron.sh"

# Or via system crontab:
*/30 * * * * /path/to/memento/scripts/transcript-index-cron.sh
```

### How it works

| Component | What it does |
|---|---|
| **JSONL parser** | Reads OpenClaw session transcripts (user, assistant, tool messages) |
| **SQLite FTS5** | Full-text search index with BM25 ranking |
| **Incremental indexing** | Only processes new/changed files (tracks by file size) |
| **Triggers** | FTS index auto-syncs on insert/update/delete |

---

## When to use which layer

| Question | Layer | Why |
|---|---|---|
| "What did we decide about the API?" | Semantic (LanceDB) | Fuzzy match, context-aware |
| "Find the exact message where I said 'migration'" | Verbatim (SQLite) | Precise, grep-like |
| "Do I prefer dark mode?" | Semantic (LanceDB) | Preference recall |
| "Show me everything from last Tuesday" | Verbatim (SQLite) | Date-filtered exact search |

Semantic memory is **automatic** — it captures and recalls without you doing anything. Verbatim search is **on-demand** — run it when you need exact quotes or historical context.

---

## Storage Footprint

| Component | Size |
|---|---|
| Ollama binary | ~32 MB |
| nomic-embed-text model | ~274 MB |
| LanceDB data | ~1-5 MB per 1K memories |
| SQLite transcript DB | ~5-15 MB per 10K messages |

Fully local. No API calls. No cloud dependency.

---

## License

MIT
