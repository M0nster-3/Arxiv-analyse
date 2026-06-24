"""
SQLite 数据库管理。

- papers 表：论文元数据 + PDF 下载状态
- sync_state 表：同步状态
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "arxiv_analyse.db"


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id         TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            authors          TEXT NOT NULL,
            summary          TEXT NOT NULL,
            primary_category TEXT NOT NULL,
            categories       TEXT NOT NULL,
            published        TEXT,
            updated          TEXT,
            year             INTEGER,
            arxiv_url        TEXT NOT NULL,
            pdf_url          TEXT NOT NULL,
            pdf_path         TEXT,
            pdf_downloaded   INTEGER NOT NULL DEFAULT 0,
            pdf_size         INTEGER,
            status           TEXT NOT NULL DEFAULT 'pending',
            error            TEXT,
            source           TEXT,
            fetched_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
        CREATE INDEX IF NOT EXISTS idx_papers_category ON papers(primary_category);
        CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
        CREATE INDEX IF NOT EXISTS idx_papers_year_status ON papers(year, status);
        CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);
    """)
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_year(arxiv_id: str, published: str | None = None) -> int | None:
    if published:
        try:
            return int(published[:4])
        except (ValueError, IndexError):
            pass
    try:
        if "/" in arxiv_id:
            yy = int(arxiv_id.split("/")[1][:2])
        else:
            yy = int(arxiv_id[:2])
        return (1900 + yy) if yy >= 91 else (2000 + yy)
    except (ValueError, IndexError):
        return None


def safe_id(arxiv_id: str) -> str:
    return arxiv_id.replace("/", "_")


def upsert_paper(conn: sqlite3.Connection, meta: dict):
    existing = conn.execute(
        "SELECT pdf_downloaded FROM papers WHERE arxiv_id = ?",
        (meta["arxiv_id"],),
    ).fetchone()
    if existing and existing["pdf_downloaded"] == 1:
        return

    year = extract_year(meta["arxiv_id"], meta.get("published"))
    authors = meta["authors"]
    if isinstance(authors, list):
        authors = json.dumps(authors, ensure_ascii=False)
    categories = meta["categories"]
    if isinstance(categories, list):
        categories = json.dumps(categories, ensure_ascii=False)

    sid = safe_id(meta["arxiv_id"])
    year_str = str(year) if year else "unknown"
    pdf_path = f"{year_str}/{sid}/paper.pdf"

    conn.execute("""
        INSERT INTO papers (arxiv_id, title, authors, summary, primary_category,
                           categories, published, updated, year,
                           arxiv_url, pdf_url, pdf_path, fetched_at, source, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        ON CONFLICT(arxiv_id) DO UPDATE SET
            title=excluded.title, authors=excluded.authors, summary=excluded.summary,
            primary_category=excluded.primary_category, categories=excluded.categories,
            published=excluded.published, updated=excluded.updated, year=excluded.year,
            arxiv_url=excluded.arxiv_url, pdf_url=excluded.pdf_url,
            pdf_path=excluded.pdf_path, fetched_at=excluded.fetched_at, source=excluded.source
        WHERE pdf_downloaded != 1
    """, (
        meta["arxiv_id"], meta["title"], authors, meta["summary"],
        meta["primary_category"], categories,
        meta.get("published"), meta.get("updated"), year,
        meta.get("arxiv_url", f"https://arxiv.org/abs/{meta['arxiv_id']}"),
        meta.get("pdf_url", f"https://arxiv.org/pdf/{meta['arxiv_id']}"),
        pdf_path, now_iso(), meta.get("source", "unknown"),
    ))


def upsert_papers_batch(conn: sqlite3.Connection, papers: list[dict]):
    for meta in papers:
        upsert_paper(conn, meta)


def mark_downloaded(conn: sqlite3.Connection, arxiv_id: str,
                    pdf_path: str, pdf_size: int):
    conn.execute(
        "UPDATE papers SET status='downloaded', pdf_downloaded=1, "
        "pdf_path=?, pdf_size=?, error=NULL WHERE arxiv_id=?",
        (pdf_path, pdf_size, arxiv_id),
    )


def mark_failed(conn: sqlite3.Connection, arxiv_id: str, error: str):
    conn.execute(
        "UPDATE papers SET status='failed', error=? WHERE arxiv_id=?",
        (error, arxiv_id),
    )


def get_pending_papers(conn: sqlite3.Connection, limit: int = 500,
                       year: int | None = None) -> list[dict]:
    if year is not None:
        rows = conn.execute(
            "SELECT * FROM papers WHERE status='pending' AND year=? "
            "ORDER BY published LIMIT ?", (year, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM papers WHERE status='pending' "
            "ORDER BY published LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_published(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(published) as latest FROM papers"
    ).fetchone()
    if row and row["latest"]:
        return row["latest"][:10]
    return None


def count_by_status(conn: sqlite3.Connection, status: str,
                    year: int | None = None) -> int:
    if year is not None:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM papers WHERE status=? AND year=?",
            (status, year),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM papers WHERE status=?", (status,),
        ).fetchone()
    return row["c"]


def reset_failed_to_pending(conn: sqlite3.Connection,
                            year: int | None = None) -> int:
    if year is not None:
        cur = conn.execute(
            "UPDATE papers SET status='pending', error=NULL "
            "WHERE status='failed' AND year=?", (year,),
        )
    else:
        cur = conn.execute(
            "UPDATE papers SET status='pending', error=NULL WHERE status='failed'"
        )
    return cur.rowcount


def get_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) as c FROM papers").fetchone()["c"]
    downloaded = count_by_status(conn, "downloaded")
    failed = count_by_status(conn, "failed")
    pending = count_by_status(conn, "pending")
    return {"total": total, "downloaded": downloaded,
            "failed": failed, "pending": pending}


def get_year_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT year,
               COUNT(*) as total,
               SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as downloaded,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
        FROM papers GROUP BY year ORDER BY year
    """).fetchall()
    return [dict(r) for r in rows]


def get_category_stats(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT primary_category, COUNT(*) as c FROM papers "
        "GROUP BY primary_category ORDER BY c DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_sync_state(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
