#!/usr/bin/env python3
"""
Transcript Search — SQLite FTS5 index over OpenClaw session JSONL files.

Commands:
  index              Incrementally index new/changed session files
  reindex            Drop and rebuild the full index
  search "query"     Full-text search across all indexed messages
  stats              Show index statistics

Usage:
  python3 transcript-search.py index
  python3 transcript-search.py search "Loyalty Lite" --role assistant --limit 20
  python3 transcript-search.py search "migration" --after 2026-03-01 --full
  python3 transcript-search.py reindex
  python3 transcript-search.py stats
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

WORKSPACE_DIR = os.environ.get("MEMENTO_WORKSPACE_DIR", os.path.expanduser("~/.openclaw/workspace"))

SESSIONS_DIR = os.environ.get("MEMENTO_SESSIONS_DIR", os.path.expanduser("~/.openclaw/agents/main/sessions"))
DB_PATH = os.environ.get("MEMENTO_DB_PATH", os.path.join(WORKSPACE_DIR, "data/transcripts.db"))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    session_date TEXT,
    message_id TEXT,
    timestamp TEXT,
    role TEXT,
    content TEXT,
    tool_name TEXT,
    content_hash TEXT,
    UNIQUE(session_id, message_id)
);

CREATE TABLE IF NOT EXISTS indexed_files (
    filename TEXT PRIMARY KEY,
    file_size INTEGER,
    indexed_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    role,
    content='messages',
    content_rowid='id'
);

-- Triggers for FTS sync
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, role)
    VALUES (new.id, new.content, new.role);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, role)
    VALUES ('delete', old.id, old.content, old.role);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, role)
    VALUES ('delete', old.id, old.content, old.role);
    INSERT INTO messages_fts(rowid, content, role)
    VALUES (new.id, new.content, new.role);
END;
"""


def content_hash(role, content, timestamp):
    """Hash of role + content + 5-second timestamp bucket for cross-session dedup."""
    ts_bucket = timestamp[:18] if len(timestamp) >= 18 else timestamp
    # Round seconds to nearest 5s bucket
    if len(ts_bucket) >= 18:
        try:
            sec = int(ts_bucket[17:19]) if len(ts_bucket) >= 19 else int(ts_bucket[17:18])
            ts_bucket = ts_bucket[:17] + str((sec // 5) * 5).zfill(2)
        except ValueError:
            pass
    key = f"{role}:{ts_bucket}:{content[:500]}"
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    _migrate_content_hash(conn)
    return conn


def _migrate_content_hash(conn):
    """Add content_hash column if missing (one-time migration)."""
    cur = conn.cursor()
    cols = [row[1] for row in cur.execute("PRAGMA table_info(messages)")]
    if "content_hash" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")
        conn.commit()


def extract_content(content_field):
    """Extract text from content that may be a string or list of {type, text} objects."""
    if content_field is None:
        return None
    if isinstance(content_field, str):
        return content_field if content_field.strip() else None
    if isinstance(content_field, list):
        parts = []
        for item in content_field:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(item["text"])
                elif item.get("type") == "image":
                    continue  # skip binary/media
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(parts)
        return text if text.strip() else None
    return None


def parse_session_file(filepath):
    """Parse an OpenClaw JSONL session file and yield message records."""
    session_id = os.path.basename(filepath).replace(".jsonl", "")
    session_date = None

    with open(filepath, "r", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(f"  Warning: malformed JSON at {session_id} line {line_num}, skipping", file=sys.stderr)
                continue

            entry_type = entry.get("type", "")

            # OpenClaw: extract session date from the session header
            if entry_type == "session" and not session_date:
                ts = entry.get("timestamp", "")
                if ts:
                    session_date = ts[:10]

            if entry_type != "message":
                continue

            msg = entry.get("message")
            if not msg:
                continue
            role = msg.get("role", "")
            content = extract_content(msg.get("content"))
            timestamp = entry.get("timestamp", "")
            message_id = entry.get("id", f"line-{line_num}")
            tool_name = msg.get("toolName", None)

            if content is None:
                continue

            # Skip very short content (likely just whitespace or empty tool results)
            if len(content.strip()) < 2:
                continue

            if not session_date and timestamp:
                session_date = timestamp[:10]

            yield {
                "session_id": session_id,
                "session_date": session_date or "",
                "message_id": message_id,
                "timestamp": timestamp,
                "role": role,
                "content": content,
                "tool_name": tool_name,
            }


def cmd_index(conn, verbose=True):
    """Incrementally index new/changed session files."""
    cur = conn.cursor()
    
    # Get already-indexed files
    cur.execute("SELECT filename, file_size FROM indexed_files")
    indexed = {row[0]: row[1] for row in cur.fetchall()}

    # Find all JSONL session files
    files = []
    for f in os.listdir(SESSIONS_DIR):
        if f.endswith(".jsonl"):
            files.append((SESSIONS_DIR, f))

    new_count = 0
    msg_count = 0

    for fdir, fname in sorted(files, key=lambda x: x[1]):
        fpath = os.path.join(fdir, fname)
        fsize = os.path.getsize(fpath)

        if fname in indexed and indexed[fname] == fsize:
            continue  # already indexed and unchanged

        if verbose:
            print(f"  Indexing {fname} ({fsize:,} bytes)...")

        # If file changed, remove old entries first
        if fname in indexed:
            session_id = fname.replace(".jsonl", "")
            cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

        batch = []
        for rec in parse_session_file(fpath):
            ch = content_hash(rec["role"], rec["content"], rec["timestamp"])
            batch.append((
                rec["session_id"], rec["session_date"], rec["message_id"],
                rec["timestamp"], rec["role"], rec["content"], rec["tool_name"], ch
            ))

        inserted = 0
        for row in batch:
            ch = row[7]
            # Skip if a message with the same content hash already exists (cross-session dedup)
            existing = cur.execute(
                "SELECT 1 FROM messages WHERE content_hash = ? LIMIT 1", (ch,)
            ).fetchone()
            if existing:
                continue
            cur.execute(
                """INSERT OR IGNORE INTO messages
                   (session_id, session_date, message_id, timestamp, role, content, tool_name, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                row
            )
            inserted += 1
        msg_count += inserted

        cur.execute(
            "INSERT OR REPLACE INTO indexed_files (filename, file_size, indexed_at) VALUES (?, ?, ?)",
            (fname, fsize, datetime.now(timezone.utc).isoformat())
        )
        new_count += 1

    conn.commit()

    if verbose:
        if new_count == 0:
            print("Index is up to date — no new or changed files.")
        else:
            print(f"Indexed {new_count} file(s), {msg_count} message(s) added.")


def cmd_reindex(conn):
    """Drop and rebuild the full index."""
    cur = conn.cursor()
    # Drop FTS and content tables, then recreate
    cur.execute("DROP TABLE IF EXISTS messages_fts")
    cur.execute("DROP TABLE IF EXISTS messages")
    cur.execute("DROP TABLE IF EXISTS indexed_files")
    conn.commit()
    conn.executescript(SCHEMA_SQL)
    print("Tables dropped and recreated. Re-indexing all files...")
    cmd_index(conn, verbose=True)


def cmd_dedup(conn):
    """Remove cross-session duplicate messages using content hashing."""
    cur = conn.cursor()

    # First, backfill content_hash for rows that don't have one
    rows = cur.execute(
        "SELECT id, role, content, timestamp FROM messages WHERE content_hash IS NULL"
    ).fetchall()
    if rows:
        print(f"Backfilling content_hash for {len(rows)} messages...")
        for row_id, role, content, ts in rows:
            ch = content_hash(role, content or "", ts or "")
            cur.execute("UPDATE messages SET content_hash = ? WHERE id = ?", (ch, row_id))
        conn.commit()

    # Find and remove duplicates: keep the earliest id for each content_hash
    dupes = cur.execute("""
        SELECT COUNT(*) FROM messages WHERE id NOT IN (
            SELECT MIN(id) FROM messages WHERE content_hash IS NOT NULL GROUP BY content_hash
        ) AND content_hash IS NOT NULL
    """).fetchone()[0]

    if dupes == 0:
        print("No duplicates found.")
        return

    print(f"Removing {dupes} duplicate messages...")
    cur.execute("""
        DELETE FROM messages WHERE id NOT IN (
            SELECT MIN(id) FROM messages WHERE content_hash IS NOT NULL GROUP BY content_hash
        ) AND content_hash IS NOT NULL
    """)
    conn.commit()

    # Rebuild FTS index
    print("Rebuilding FTS index...")
    cur.execute("DELETE FROM messages_fts")
    cur.execute("INSERT INTO messages_fts(rowid, content, role) SELECT id, content, role FROM messages")
    conn.commit()
    print(f"Done. Removed {dupes} duplicates.")


def format_timestamp(iso_ts):
    """Convert ISO timestamp to readable format."""
    if not iso_ts:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        # Convert to US/Eastern
        from datetime import timedelta
        eastern_offset = timedelta(hours=-4)  # EDT
        dt_eastern = dt + eastern_offset
        return dt_eastern.strftime("%Y-%m-%d %H:%M EDT")
    except (ValueError, TypeError):
        return iso_ts[:16] if len(iso_ts) >= 16 else iso_ts


def cmd_search(conn, query, role=None, after=None, before=None, limit=10, full=False):
    """Full-text search across indexed messages."""
    cur = conn.cursor()

    # Build the query
    where_clauses = ["messages_fts MATCH ?"]
    params = [query]

    if role:
        where_clauses.append("m.role = ?")
        params.append(role)
    if after:
        where_clauses.append("m.session_date >= ?")
        params.append(after)
    if before:
        where_clauses.append("m.session_date <= ?")
        params.append(before)

    where = " AND ".join(where_clauses)
    params.append(limit)

    sql = f"""
        SELECT m.timestamp, m.role, m.session_id, m.content, m.tool_name,
               rank
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE {where}
        ORDER BY rank
        LIMIT ?
    """

    try:
        results = cur.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Search error: {e}", file=sys.stderr)
        print("Tip: Use double quotes for exact phrases, e.g.: search '\"exact phrase\"'", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No results found.")
        return

    print(f"Found {len(results)} result(s):\n")

    for ts, role_val, session_id, content, tool_name, rank in results:
        ts_fmt = format_timestamp(ts)
        role_label = role_val
        if tool_name:
            role_label = f"toolResult:{tool_name}"

        display = content if full else (content[:500] + "..." if len(content) > 500 else content)

        print(f"[{ts_fmt}] [{role_label}] in session {session_id[:8]}:")
        print(display)
        print("---")


def cmd_stats(conn):
    """Show index statistics."""
    cur = conn.cursor()

    total_msgs = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_sessions = cur.execute("SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0]
    total_files = cur.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]

    if total_msgs == 0:
        print("Index is empty. Run 'index' first.")
        return

    date_range = cur.execute(
        "SELECT MIN(session_date), MAX(session_date) FROM messages WHERE session_date != ''"
    ).fetchone()

    role_counts = cur.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY COUNT(*) DESC"
    ).fetchall()

    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0

    print(f"Transcript Search Index Stats")
    print(f"{'='*40}")
    print(f"Total messages:  {total_msgs:,}")
    print(f"Total sessions:  {total_sessions}")
    print(f"Files indexed:   {total_files}")
    print(f"Date range:      {date_range[0] or '?'} → {date_range[1] or '?'}")
    print(f"Database size:   {db_size / 1024 / 1024:.1f} MB")
    print()
    print("Messages by role:")
    for role, count in role_counts:
        print(f"  {role:20s} {count:,}")


def main():
    parser = argparse.ArgumentParser(description="Search OpenClaw session transcripts")
    sub = parser.add_subparsers(dest="command")

    index_p = sub.add_parser("index", help="Incrementally index new/changed sessions")
    index_p.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    sub.add_parser("reindex", help="Drop and rebuild the full index")
    sub.add_parser("dedup", help="Remove cross-session duplicate messages")
    sub.add_parser("stats", help="Show index statistics")

    search_p = sub.add_parser("search", help="Full-text search")
    search_p.add_argument("query", help="Search query (FTS5 syntax)")
    search_p.add_argument("--role", help="Filter by role (user, assistant, toolResult, system)")
    search_p.add_argument("--after", help="Only results after YYYY-MM-DD")
    search_p.add_argument("--before", help="Only results before YYYY-MM-DD")
    search_p.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    search_p.add_argument("--full", action="store_true", help="Show full message content")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    conn = get_db()

    try:
        if args.command == "index":
            cmd_index(conn, verbose=not getattr(args, 'quiet', False))
        elif args.command == "reindex":
            cmd_reindex(conn)
        elif args.command == "dedup":
            cmd_dedup(conn)
        elif args.command == "stats":
            cmd_stats(conn)
        elif args.command == "search":
            cmd_search(conn, args.query, role=args.role, after=args.after,
                       before=args.before, limit=args.limit, full=args.full)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
