
"""
Project A.R.G.U.S. — Dedicated Personal Memory Store
database.py
"""

from __future__ import annotations

import logging
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from config import BASE_DIR, DATA_DIR, env

# Keep the original filename/location unless explicitly configured. No implicit DB move.
DB_PATH = Path(env('ARGUS_DB_PATH', str(BASE_DIR / 'argus_user_profile.db')))

logger = logging.getLogger("argus.database")

_SELF_REF_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmy\s+name\s+is\s+([A-Za-z]+)", re.IGNORECASE), "identity"),
    (re.compile(r"\bcall\s+me\s+([A-Za-z]+)", re.IGNORECASE), "identity"),
    (re.compile(r"\bi\s+usually\s+(.+)", re.IGNORECASE), "habit"),
    (re.compile(r"\bi\s+prefer\s+(.+)", re.IGNORECASE), "preference"),
    (re.compile(r"\bi\s+like\s+(.+)", re.IGNORECASE), "preference"),
    (re.compile(r"\bmy\s+ip\s+(?:address\s+)?is\s+([\d.:\w/]+)", re.IGNORECASE), "network"),
    (re.compile(r"\bremember\s+that\s+(.+)", re.IGNORECASE), "direct_fact"),
]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _sections(text: str):
    """Partition on printed section headings and retain every line, including preambles."""
    parts, lines = [], []
    for line in text.splitlines():
        stripped = line.strip()
        numbered = re.match(r'^(?:#{1,6}\s+|(?:unit|chapter|module|topic|section)\s+\w+\b|(?:[IVXLCDM]+\.|\d+(?:\.\d+)*[.)])\s+)', stripped, re.I)
        heading = bool(stripped and len(stripped) <= 110 and (numbered or (
            stripped.isupper() and len(stripped.split()) >= 2 and not stripped.endswith(('.', '?', '!')))))
        if heading and lines and any(s.strip() for s in lines):
            parts.append('\n'.join(lines).strip())
            lines = []
        lines.append(line)
    parts.append('\n'.join(lines).strip())
    return parts


@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bootstrap_schema() -> None:
    ddl_statements = [
        """
        CREATE TABLE IF NOT EXISTS user_profile (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT    NOT NULL,
            key         TEXT    NOT NULL UNIQUE,
            value       TEXT    NOT NULL,
            updated_at  TIMESTAMP NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            role      TEXT      NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
            content   TEXT      NOT NULL,
            timestamp TIMESTAMP NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS system_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_text  TEXT    NOT NULL,
            is_active  BOOLEAN NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT    NOT NULL,
            success    BOOLEAN NOT NULL,
            detail     TEXT,
            timestamp  TIMESTAMP NOT NULL
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_profile_category ON user_profile(category);",
        "CREATE INDEX IF NOT EXISTS idx_history_timestamp ON conversation_history(timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_rules_active ON system_rules(is_active);",
        '''CREATE TABLE IF NOT EXISTS user_rules (
            id INTEGER PRIMARY KEY, rule_text TEXT NOT NULL UNIQUE,
            trigger_text TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS search_cache (
            query_key TEXT PRIMARY KEY, query TEXT NOT NULL, payload TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS ntfy_inbox (
            id TEXT PRIMARY KEY, message TEXT NOT NULL, attachment TEXT,
            payload TEXT NOT NULL, received_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS pdf_documents (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS pdf_chunks (
            id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES pdf_documents(id),
            page INTEGER NOT NULL, text TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS idx_pdf_document ON pdf_chunks(document_id)',
        'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS episodic_memory(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
            content TEXT NOT NULL, importance REAL NOT NULL, timestamp REAL NOT NULL, consolidated INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS semantic_memory(id INTEGER PRIMARY KEY, content TEXT UNIQUE NOT NULL,
            importance REAL NOT NULL, timestamp REAL NOT NULL, evidence_count INTEGER NOT NULL DEFAULT 1)''',
        'CREATE TABLE IF NOT EXISTS study_metadata(document_id TEXT PRIMARY KEY, title TEXT NOT NULL, page_count INTEGER NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS study_page_coverage(
            document_id TEXT NOT NULL REFERENCES pdf_documents(id), page INTEGER NOT NULL,
            label TEXT NOT NULL, explicitly_reviewed INTEGER NOT NULL DEFAULT 0,
            reviewed_at REAL, PRIMARY KEY(document_id,page))''',
        '''CREATE TABLE IF NOT EXISTS study_chunk_coverage(
            chunk_id INTEGER PRIMARY KEY REFERENCES pdf_chunks(id), reviewed_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS study_chunk_topics(
            chunk_id INTEGER NOT NULL REFERENCES pdf_chunks(id), ordinal INTEGER NOT NULL,
            label TEXT NOT NULL, PRIMARY KEY(chunk_id,ordinal))''',
        'CREATE VIEW IF NOT EXISTS study_documents AS SELECT d.*,m.title,m.page_count FROM pdf_documents d LEFT JOIN study_metadata m ON m.document_id=d.id',
        'CREATE VIEW IF NOT EXISTS study_chunks AS SELECT * FROM pdf_chunks',
    ]

    with _get_conn() as conn:
        for stmt in ddl_statements:
            conn.execute(stmt)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(study_metadata)')}
        for name, declaration in [('original_path', "TEXT NOT NULL DEFAULT ''"),
                                  ('source_sha256', "TEXT NOT NULL DEFAULT ''"),
                                  ('text_page_count', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                conn.execute(f'ALTER TABLE study_metadata ADD COLUMN {name} {declaration}')

    logger.info("Schema bootstrap complete — WAL mode active on %s", DB_PATH)


class ArgusDatabase:
    def __init__(self) -> None:
        _bootstrap_schema()

    def commit_personal_fact(self, category: str, key: str, value: str) -> None:
        sql = """
            INSERT INTO user_profile (category, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                category   = excluded.category,
                value      = excluded.value,
                updated_at = excluded.updated_at;
        """
        with _get_conn() as conn:
            conn.execute(sql, (category.strip(), key.strip(), value.strip(), _utc_now()))
        logger.debug("Persisted a user fact in category %s", category)

    def extract_and_memorize(self, conversation_turn: str) -> list[tuple[str, str, str]]:
        committed: list[tuple[str, str, str]] = []
        for pattern, category in _SELF_REF_PATTERNS:
            match = pattern.search(conversation_turn)
            if not match:
                continue
            try:
                raw_value = match.group(1).strip().rstrip(".")
                key = "user_name" if category == "identity" else f"{category}:{raw_value[:32].lower().replace(' ', '_')}"
                self.commit_personal_fact(category, key, raw_value)
                committed.append((category, key, raw_value))
            except Exception as exc:
                logger.warning("extract_and_memorize error: %s", exc)
        return committed

    def get_user_name(self) -> str:
        sql = "SELECT value FROM user_profile WHERE category = 'identity' ORDER BY updated_at DESC LIMIT 1;"
        with _get_conn() as conn:
            row = conn.execute(sql).fetchone()
            if row and row["value"]:
                return row["value"]
        return "Hari"

    def get_personalized_context(self) -> str:
        sql = "SELECT category, key, value FROM user_profile ORDER BY category ASC, updated_at DESC;"
        with _get_conn() as conn:
            rows = conn.execute(sql).fetchall()

        if not rows:
            return "[USER ATTRIBUTES]\n  • IDENTITY: Hari"

        lines: list[str] = ["[USER KNOWLEDGE ATTRIBUTES]"]
        for row in rows:
            lines.append(f"  • {row['category'].upper()} ({row['key']}): {row['value']}")
        return "\n".join(lines)

    def log_conversation_turn(self, role: str, content: str) -> None:
        self.add_episode(role, content, .8 if role == 'user' else .3)
        sql = "INSERT INTO conversation_history (role, content, timestamp) VALUES (?, ?, ?);"
        with _get_conn() as conn:
            conn.execute(sql, (role, content, _utc_now()))

    def get_recent_history(self, limit: int = 10) -> list[dict[str, str]]:
        sql = "SELECT role, content, timestamp FROM conversation_history ORDER BY id DESC LIMIT ?;"
        with _get_conn() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_system_rule(self, rule: str) -> int:
        sql = "INSERT INTO system_rules (rule_text, is_active, created_at) VALUES (?, 1, ?);"
        with _get_conn() as conn:
            cursor = conn.execute(sql, (rule.strip(), _utc_now()))
            rowid: int = cursor.lastrowid
        return rowid

    def get_active_rules(self) -> list[str]:
        sql = "SELECT rule_text FROM system_rules WHERE is_active = 1 ORDER BY id ASC;"
        with _get_conn() as conn:
            rows = conn.execute(sql).fetchall()
            newer = conn.execute('SELECT rule_text FROM user_rules WHERE is_active=1 ORDER BY id').fetchall()
        return list(dict.fromkeys(row['rule_text'] for row in [*rows, *newer]))

    def add_user_rule(self, text: str, trigger: str = '', action: str = '') -> int:
        text = text.strip()
        if not text or len(text) > 4000:
            raise ValueError('A rule must contain 1–4000 characters')
        with _get_conn() as conn:
            conn.execute('''INSERT INTO user_rules(rule_text,trigger_text,action,created_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(rule_text) DO UPDATE SET is_active=1,
                trigger_text=excluded.trigger_text,action=excluded.action,updated_at=excluded.updated_at''',
                (text, trigger, action or text, _utc_now(), _utc_now()))
            rule_id = conn.execute('SELECT id FROM user_rules WHERE rule_text=?', (text,)).fetchone()[0]
        self.export_rules()
        return rule_id

    def extract_rule(self, text: str) -> dict | None:
        cleaned = re.sub(r'^(?:hey\s+)?argus[,!: ]*', '', text.strip(), flags=re.I)
        match = re.match(r'(?:new rule\s*:\s*|remember to\s+)(.+)', cleaned, re.I | re.S)
        if not match and not re.match(r'^(?:always|never)\s+', cleaned, re.I):
            return None
        rule = match.group(1) if match else cleaned
        parts = re.split(r'\s+when\s+', rule, maxsplit=1, flags=re.I)
        return {'text': rule.strip(), 'action': parts[0].strip(), 'trigger': parts[1].strip() if len(parts) > 1 else ''}

    def export_rules(self, path: Path | None = None) -> Path:
        target = path or DATA_DIR / 'user_rules.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        with _get_conn() as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM user_rules ORDER BY id')]
        # Unique temporary files avoid concurrent writers sharing a staging filename.
        import tempfile
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent, delete=False) as handle:
            json.dump({'version': 1, 'rules': rows}, handle, ensure_ascii=False, indent=2)
            temporary = Path(handle.name)
        temporary.replace(target)
        return target

    def import_rules(self, path: Path) -> int:
        data = json.loads(path.read_text(encoding='utf-8'))
        rules = data.get('rules', [])
        if not isinstance(rules, list) or len(rules) > 10000:
            raise ValueError('Invalid rules export')
        for row in rules:
            self.add_user_rule(row['rule_text'], row.get('trigger_text', ''), row.get('action', ''))
            if not row.get('is_active', True):
                with _get_conn() as conn:
                    conn.execute('UPDATE user_rules SET is_active=0 WHERE rule_text=?', (row['rule_text'],))
        self.export_rules()
        return len(rules)

    def cache_get(self, query: str) -> dict | None:
        key = ' '.join(query.casefold().split())
        with _get_conn() as conn:
            row = conn.execute('SELECT payload FROM search_cache WHERE query_key=? AND expires_at>?', (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def cache_set(self, query: str, result: dict, ttl: int = 43200) -> None:
        key = ' '.join(query.casefold().split())
        now = time.time()
        with _get_conn() as conn:
            conn.execute('INSERT OR REPLACE INTO search_cache VALUES(?,?,?,?,?)', (key, query, json.dumps(result), now, now + ttl))
            conn.execute('DELETE FROM search_cache WHERE expires_at<?', (now,))

    def add_inbox(self, event: dict) -> bool:
        if event.get('event', 'message') != 'message':
            return False
        payload = json.dumps(event, ensure_ascii=False)
        event_id = str(event.get('id') or hashlib.sha256(payload.encode()).hexdigest())
        with _get_conn() as conn:
            cursor = conn.execute('INSERT OR IGNORE INTO ntfy_inbox VALUES(?,?,?,?,?)',
                (event_id, str(event.get('message', '')), json.dumps(event.get('attachment')), payload, time.time()))
            conn.execute('DELETE FROM ntfy_inbox WHERE id NOT IN (SELECT id FROM ntfy_inbox ORDER BY received_at DESC LIMIT 256)')
        return cursor.rowcount > 0

    def get_latest_inbox(self) -> dict | None:
        with _get_conn() as conn:
            row = conn.execute('SELECT payload FROM ntfy_inbox ORDER BY received_at DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def store_pdf(self, filename: str, pages: list[str], title: str = '', source_sha256: str = '') -> dict:
        if not pages or len(pages) > 600 or any(not isinstance(page, str) for page in pages):
            raise ValueError('A PDF must contain between 1 and 600 pages')
        if source_sha256 and not re.fullmatch(r'[0-9a-f]{64}', source_sha256):
            raise ValueError('Invalid PDF source digest')
        digest = source_sha256 or hashlib.sha256(json.dumps(pages, ensure_ascii=False).encode()).hexdigest()
        chunks = []
        for page, text in enumerate(pages, 1):
            text = re.sub(r'[ \t]+', ' ', text).strip()
            for ordinal, section in enumerate(_sections(text), 1):
                label = next((s.strip()[:120] for s in section.splitlines() if s.strip()),'')
                for start in range(0, max(1, len(section)), 1000):
                    chunks.append((page, section[start:start + 1200], ordinal, label))
        text_pages = sum(bool(page.strip()) for page in pages)
        with _get_conn() as conn:
            exists = conn.execute('SELECT 1 FROM pdf_documents WHERE id=?', (digest,)).fetchone()
            if not exists:
                conn.execute('INSERT INTO pdf_documents VALUES(?,?,?)', (digest, Path(filename).name, _utc_now()))
                for page, body, ordinal, label in chunks:
                    inserted = conn.execute('INSERT INTO pdf_chunks(document_id,page,text) VALUES(?,?,?)', (digest,page,body))
                    conn.execute('INSERT INTO study_chunk_topics VALUES(?,?,?)',(inserted.lastrowid,ordinal,label))
            conn.execute('''INSERT INTO study_metadata(document_id,title,page_count,source_sha256,text_page_count)
                VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,
                page_count=excluded.page_count,source_sha256=excluded.source_sha256,text_page_count=excluded.text_page_count''',
                (digest, title or Path(filename).stem, len(pages), source_sha256, text_pages))
        self.ensure_study_coverage(digest, len(pages))
        return {'id': digest, 'filename': Path(filename).name, 'title': title or Path(filename).stem,
                'page_count': len(pages), 'text_page_count': text_pages, 'chunks': len(chunks), 'sha256': source_sha256}

    def set_pdf_original(self, document_id: str, path: str, sha256: str) -> None:
        with _get_conn() as conn:
            conn.execute('UPDATE study_metadata SET original_path=?,source_sha256=? WHERE document_id=?',
                         (path, sha256, document_id))

    def tag_material(self, document_id: str, origin: str, unit: str):
        self.set_setting('material:' + document_id, {'origin': origin, 'unit': unit})

    def material_info(self, document_id: str):
        return self.get_setting('material:' + document_id, {'origin': 'upload', 'unit': 'page'})

    def ensure_study_coverage(self, document_id: str, page_count: int) -> None:
        """Every physical page is a coverage topic, including pages without text."""
        with _get_conn() as conn:
            if conn.execute('SELECT COUNT(*) FROM study_page_coverage WHERE document_id=?',(document_id,)).fetchone()[0] == page_count:
                return
            for page in range(1, int(page_count) + 1):
                row = conn.execute('SELECT text FROM pdf_chunks WHERE document_id=? AND page=? ORDER BY id LIMIT 1',
                                   (document_id, page)).fetchone()
                if row is None:
                    conn.execute('INSERT INTO pdf_chunks(document_id,page,text) VALUES(?,?,?)', (document_id, page, ''))
                excerpt = re.sub(r'\s+', ' ', row['text']).strip() if row else ''
                label = f'Page {page}: ' + (excerpt[:120] if excerpt else 'No extractable text; inspect the original page')
                conn.execute('INSERT OR IGNORE INTO study_page_coverage(document_id,page,label) VALUES(?,?,?)',
                             (document_id, page, label))

    def mark_study_covered(self, chunk_ids: list[int], page_refs: list[dict] | None = None) -> int:
        """Record only delivered excerpts; explicit page refs apply to no-text pages."""
        changed = 0
        with _get_conn() as conn:
            for chunk_id in set(chunk_ids):
                if isinstance(chunk_id, bool) or not isinstance(chunk_id, int):
                    continue
                cursor = conn.execute('''INSERT OR IGNORE INTO study_chunk_coverage(chunk_id,reviewed_at)
                    SELECT id,? FROM pdf_chunks WHERE id=?''', (time.time(), chunk_id))
                changed += cursor.rowcount
            for ref in page_refs or []:
                # A page with real text is complete only after every chunk was delivered.
                cursor = conn.execute('''UPDATE study_page_coverage SET explicitly_reviewed=1,reviewed_at=?
                    WHERE document_id=? AND page=? AND explicitly_reviewed=0 AND NOT EXISTS(
                    SELECT 1 FROM pdf_chunks c WHERE c.document_id=study_page_coverage.document_id
                    AND c.page=study_page_coverage.page AND trim(c.text)<>'')''',
                    (time.time(), str(ref['document_id']), int(ref['page'])))
                changed += cursor.rowcount
        return changed

    def study_coverage(self, document_ids: list[str]) -> dict:
        """Measure each indexed heading; unstructured and legacy materials use physical units."""
        ids = list(dict.fromkeys(document_ids))
        if ids:
            with _get_conn() as conn:
                docs = [dict(row) for row in conn.execute('''SELECT d.id,d.filename,COALESCE(m.page_count,
                    (SELECT MAX(page) FROM pdf_chunks WHERE document_id=d.id),0) page_count
                    FROM pdf_documents d LEFT JOIN study_metadata m ON m.document_id=d.id
                    WHERE d.id IN (''' + ','.join('?' for _ in ids) + ')', ids)]
            for doc in docs:
                self.ensure_study_coverage(doc['id'], doc['page_count'])
        else:
            docs = []
        topics = []
        with _get_conn() as conn:
            for doc in docs:
                grouped = {}
                for row in conn.execute('''SELECT p.page,c.id,COALESCE(t.ordinal,1) ordinal,
                    COALESCE(NULLIF(t.label,''),p.label) label,p.explicitly_reviewed,
                    CASE WHEN v.chunk_id IS NULL THEN 0 ELSE 1 END reviewed
                    FROM study_page_coverage p JOIN pdf_chunks c ON c.document_id=p.document_id AND c.page=p.page
                    LEFT JOIN study_chunk_coverage v ON v.chunk_id=c.id LEFT JOIN study_chunk_topics t ON t.chunk_id=c.id
                    WHERE p.document_id=? ORDER BY p.page,ordinal,c.id''', (doc['id'],)):
                    key = (row['page'], row['ordinal'])
                    topic = grouped.setdefault(key, {'id': f"{doc['id']}:{row['page']}:{row['ordinal']}",
                        'document_id':doc['id'],'filename':doc['filename'],'page':row['page'],
                        'label':row['label'],'title':row['label'],'covered':True,'covered_chunks':0,'total_chunks':0,'chunk_ids':[]})
                    topic['chunk_ids'].append(row['id'])
                    topic['total_chunks'] += 1
                    topic['covered_chunks'] += row['reviewed']
                    topic['covered'] = topic['covered'] and bool(row['explicitly_reviewed'] or row['reviewed'])
                topics.extend(grouped.values())
        total, covered = len(topics), sum(topic['covered'] for topic in topics)
        percent = covered * 100 / total if total else 0.0
        return {'covered': covered, 'total': total, 'remaining': total-covered, 'percent': percent,
                'covered_percent': percent, 'remaining_percent': 100-percent if total else 0.0,
                'basis': 'Discussed topics / all indexed topics. Headingless pages, slides and text sections count as one topic.', 'topics': topics}

    def add_episode(self, kind: str, content: str, importance: float = .5):
        with _get_conn() as conn:
            conn.execute('INSERT INTO episodic_memory(kind,content,importance,timestamp) VALUES(?,?,?,?)',
                (kind, content, max(0, min(1, importance)), time.time()))

    def relevant_memories(self, query: str, limit: int = 6) -> str:
        terms = set(re.findall(r'\w{3,}', query.lower()))
        with _get_conn() as conn:
            rows = [dict(r) for r in conn.execute('SELECT content,importance,timestamp FROM semantic_memory ORDER BY importance DESC,timestamp DESC LIMIT 500')]
            rows += [dict(r) for r in conn.execute('SELECT content,importance,timestamp FROM episodic_memory WHERE kind="user" ORDER BY id DESC LIMIT 200')]
        rows.sort(key=lambda r: (sum(t in r['content'].lower() for t in terms), r['importance'], r['timestamp']), reverse=True)
        return '\n'.join(dict.fromkeys(r['content'][:700] for r in rows[:limit]))

    def consolidate_memories(self):
        promoted = 0
        rules = []
        with _get_conn() as conn:
            rows = conn.execute('SELECT * FROM episodic_memory WHERE consolidated=0 ORDER BY id LIMIT 2000').fetchall()
            for row in rows:
                # Only explicit user statements become durable facts; never promote assistant/tool guesses.
                if row['kind'] == 'user' and re.search(r'\b(i prefer|i like|my |i am working|i.m building|always |never |new rule:|remember )', row['content'], re.I):
                    rule = self.extract_rule(row['content'])
                    if rule:
                        rules.append(rule)
                    content = re.sub(r'\s+', ' ', row['content']).strip()
                    existing = conn.execute('SELECT id FROM semantic_memory WHERE content=?', (content,)).fetchone()
                    conn.execute('''INSERT INTO semantic_memory(content,importance,timestamp) VALUES(?,?,?)
                        ON CONFLICT(content) DO UPDATE SET evidence_count=evidence_count+1,timestamp=excluded.timestamp''',
                        (content, row['importance'], row['timestamp']))
                    promoted += not bool(existing)
                conn.execute('UPDATE episodic_memory SET consolidated=1 WHERE id=?', (row['id'],))
            conn.execute('DELETE FROM episodic_memory WHERE consolidated=1 AND importance<.4 AND timestamp<?', (time.time()-30*86400,))
        for rule in rules:
            self.add_user_rule(**rule)
        return {'reviewed': len(rows), 'promoted': promoted}

    def study_passages(self, document_ids: list[str]) -> list[dict]:
        if not document_ids:
            return []
        with _get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT c.*,d.filename,
                CASE WHEN v.chunk_id IS NULL THEN 0 ELSE 1 END covered FROM pdf_chunks c JOIN pdf_documents d
                ON d.id=c.document_id LEFT JOIN study_chunk_coverage v ON v.chunk_id=c.id
                WHERE c.document_id IN (''' + ','.join('?' for _ in document_ids) + ') ORDER BY c.document_id,c.page,c.id', document_ids)]

    def search_pdf(self, query: str, document_id: str | None = None, limit: int = 6) -> list[dict]:
        terms = set(re.findall(r'\w{3,}', query.casefold())) - {'the', 'and', 'this', 'that', 'what', 'from', 'with', 'explain', 'please'}
        sql = '''SELECT c.id,c.document_id,d.filename,c.page,c.text FROM pdf_chunks c
            JOIN pdf_documents d ON d.id=c.document_id'''
        with _get_conn() as conn:
            rows = conn.execute(sql + (' WHERE c.document_id=?' if document_id else ''), (document_id,) if document_id else ()).fetchall()
        scored = [(sum(min(8, row['text'].casefold().count(t)) for t in terms), index, dict(row)) for index, row in enumerate(rows)]
        scored.sort(key=lambda item: (-item[0], item[1]))
        # An explicit document with a generic 'quiz me' request can use the opening pages.
        generic = bool(re.search(r'\b(quiz|summari[sz]e|overview|study|next|continue)\b', query, re.I))
        return [row for score, _, row in scored if score > 0 or (document_id and generic)][:max(1, min(limit, 20))]

    def list_documents(self) -> list[dict]:
        with _get_conn() as conn:
            return [dict(r) for r in conn.execute('''SELECT d.*,m.title,m.page_count,m.text_page_count,m.original_path,m.source_sha256
                FROM pdf_documents d LEFT JOIN study_metadata m ON m.document_id=d.id ORDER BY d.created_at DESC''')]

    def get_setting(self, key: str, default=None):
        with _get_conn() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value) -> None:
        with _get_conn() as conn:
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def log_auth_event(self, event_type: str, success: bool, detail: str = "") -> None:
        sql = "INSERT INTO auth_events (event_type, success, detail, timestamp) VALUES (?, ?, ?, ?);"
        with _get_conn() as conn:
            conn.execute(sql, (event_type, int(success), detail, _utc_now()))

    def profile_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        with _get_conn() as conn:
            for table in ("user_profile", "conversation_history", "system_rules", "auth_events"):
                row = conn.execute(f"SELECT COUNT(*) as n FROM {table};").fetchone()
                summary[table] = row["n"]
        return summary


db = ArgusDatabase()
