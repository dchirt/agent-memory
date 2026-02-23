import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec
from mcp.server.fastmcp import FastMCP

# --- Config ---
DB_PATH = Path("/data/agent_memory.db")
EMBED_MODEL = "all-MiniLM-L6-v2"
EMBED_DIM = 384
VALID_CATEGORIES = {"corrections", "preferences", "patterns", "decisions", "facts"}

# --- Init ---
mcp = FastMCP("kiro-memory")
model = None
_initialized = False


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, category, content=memories, content_rowid=rowid
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            id TEXT PRIMARY KEY,
            embedding float[{EMBED_DIM}]
        );
        CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL
        );
    """)
    # FTS triggers for sync
    for op, body in [
        ("INSERT", "INSERT INTO memories_fts(rowid, content, category) VALUES (new.rowid, new.content, new.category)"),
        ("DELETE", "INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.rowid, old.content, old.category)"),
        ("UPDATE", "INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.rowid, old.content, old.category); INSERT INTO memories_fts(rowid, content, category) VALUES (new.rowid, new.content, new.category)"),
    ]:
        db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS memories_fts_{op.lower()}
            AFTER {op} ON memories BEGIN {body}; END
        """)
    db.commit()
    db.close()


def embed(text: str) -> bytes:
    global model
    if model is None:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBED_MODEL)
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tobytes()


def ensure_init():
    global _initialized
    if not _initialized:
        init_db()
        _initialized = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_category(category: str):
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")


# --- Tools ---

@mcp.tool()
def memory_store(category: str, content: str, source: str = "") -> str:
    """Store a confirmed memory. Categories: corrections, preferences, patterns, decisions, facts."""
    validate_category(category)
    ensure_init()
    mid = str(uuid.uuid4())
    ts = now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO memories (id, category, content, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (mid, category, content, source, ts, ts),
    )
    db.execute(
        "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
        (mid, embed(content)),
    )
    db.commit()
    db.close()
    return json.dumps({"id": mid, "status": "stored"})


@mcp.tool()
def memory_query(query: str, category: str = "", limit: int = 5) -> str:
    """Hybrid semantic + keyword search over memories. Optionally filter by category."""
    if category:
        validate_category(category)
    ensure_init()

    db = get_db()
    query_vec = embed(query)

    # Vector search
    vec_sql = """
        SELECT v.id, v.distance
        FROM memories_vec v
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
    """
    vec_rows = db.execute(vec_sql, (query_vec, limit * 2)).fetchall()
    vec_scores = {r["id"]: 1.0 - r["distance"] for r in vec_rows}

    # FTS search
    fts_sql = """
        SELECT m.id, fts.rank
        FROM memories_fts fts
        JOIN memories m ON m.rowid = fts.rowid
        WHERE memories_fts MATCH ?
        ORDER BY fts.rank
        LIMIT ?
    """
    try:
        fts_rows = db.execute(fts_sql, (query, limit * 2)).fetchall()
        fts_scores = {r["id"]: 1.0 / (1.0 - r["rank"]) for r in fts_rows}
    except sqlite3.OperationalError:
        fts_scores = {}

    # Merge scores (0.7 semantic + 0.3 keyword)
    all_ids = set(vec_scores) | set(fts_scores)
    scored = []
    for mid in all_ids:
        score = 0.7 * vec_scores.get(mid, 0.0) + 0.3 * fts_scores.get(mid, 0.0)
        scored.append((mid, score))
    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for mid, score in scored[:limit]:
        cat_filter = " AND category = ?" if category else ""
        params = [mid] + ([category] if category else [])
        row = db.execute(f"SELECT * FROM memories WHERE id = ?{cat_filter}", params).fetchone()
        if row:
            results.append({**dict(row), "score": round(score, 4)})

    db.close()
    return json.dumps(results)


@mcp.tool()
def memory_update(id: str, content: str = "", category: str = "") -> str:
    """Update an existing memory's content and/or category by ID."""
    if category:
        validate_category(category)
    ensure_init()

    db = get_db()
    row = db.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
    if not row:
        db.close()
        return json.dumps({"error": "Memory not found"})

    new_content = content or row["content"]
    new_category = category or row["category"]
    ts = now_iso()

    db.execute(
        "UPDATE memories SET content = ?, category = ?, updated_at = ? WHERE id = ?",
        (new_content, new_category, ts, id),
    )
    if content:
        db.execute("DELETE FROM memories_vec WHERE id = ?", (id,))
        db.execute("INSERT INTO memories_vec (id, embedding) VALUES (?, ?)", (id, embed(new_content)))
    db.commit()
    db.close()
    return json.dumps({"id": id, "status": "updated"})


@mcp.tool()
def memory_delete(id: str) -> str:
    """Delete a memory by ID."""
    ensure_init()
    db = get_db()
    row = db.execute("SELECT id FROM memories WHERE id = ?", (id,)).fetchone()
    if not row:
        db.close()
        return json.dumps({"error": "Memory not found"})
    db.execute("DELETE FROM memories WHERE id = ?", (id,))
    db.execute("DELETE FROM memories_vec WHERE id = ?", (id,))
    db.commit()
    db.close()
    return json.dumps({"id": id, "status": "deleted"})


@mcp.tool()
def memory_propose(category: str, content: str, source: str = "") -> str:
    """Queue a proposed memory for end-of-session review."""
    validate_category(category)
    ensure_init()
    pid = str(uuid.uuid4())
    db = get_db()
    db.execute(
        "INSERT INTO proposals (id, category, content, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (pid, category, content, source, now_iso()),
    )
    db.commit()
    db.close()
    return json.dumps({"id": pid, "status": "proposed"})


@mcp.tool()
def memory_review() -> str:
    """Return all pending memory proposals for user review."""
    ensure_init()
    db = get_db()
    rows = db.execute("SELECT * FROM proposals ORDER BY created_at").fetchall()
    db.close()
    return json.dumps([dict(r) for r in rows])


@mcp.tool()
def memory_confirm(accepted_ids: list[str] = [], rejected_ids: list[str] = []) -> str:
    """Accept or reject proposals. Accepted ones become confirmed memories."""
    ensure_init()
    db = get_db()
    stored = []
    for pid in accepted_ids:
        row = db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if not row:
            continue
        mid = str(uuid.uuid4())
        ts = now_iso()
        db.execute(
            "INSERT INTO memories (id, category, content, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, row["category"], row["content"], row["source"], ts, ts),
        )
        db.execute("INSERT INTO memories_vec (id, embedding) VALUES (?, ?)", (mid, embed(row["content"])))
        db.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        stored.append(mid)

    rejected = 0
    for pid in rejected_ids:
        cur = db.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        rejected += cur.rowcount

    db.commit()
    db.close()
    return json.dumps({"stored": stored, "rejected": rejected})


@mcp.tool()
def memory_stats() -> str:
    """Return memory counts by category and last updated timestamp."""
    ensure_init()
    db = get_db()
    rows = db.execute(
        "SELECT category, COUNT(*) as count, MAX(updated_at) as last_updated FROM memories GROUP BY category"
    ).fetchall()
    total = db.execute("SELECT COUNT(*) as n FROM memories").fetchone()["n"]
    pending = db.execute("SELECT COUNT(*) as n FROM proposals").fetchone()["n"]
    db.close()
    return json.dumps({
        "total": total,
        "pending_proposals": pending,
        "by_category": {r["category"]: {"count": r["count"], "last_updated": r["last_updated"]} for r in rows},
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
