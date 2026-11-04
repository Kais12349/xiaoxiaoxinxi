"""Local, versioned knowledge store. Search does not need a model or embedding API."""
import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def terms(text, unique=True):
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text)
    for block in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(block[i:i + 2] for i in range(len(block) - 1))
        if len(block) == 1:
            tokens.append(block)
    return list(dict.fromkeys(tokens)) if unique else tokens


ALIASES = [
    ("视频", "短片", "剪辑", "video"), ("图片", "图像", "绘图", "image"),
    ("抠图", "背景移除", "前景分离", "matting"),
    ("转写", "转录", "语音识别", "字幕", "录音转文字", "录音变成文字", "音频转文字", "transcribe", "asr"),
    ("三维", "3d", "建模", "重建"), ("编程", "代码", "coding"),
    ("智能体", "agent", "助手"), ("音乐", "作曲", "midi"),
    ("配音", "语音合成", "tts"), ("网页", "网站", "前端"),
]
TEXT_FIELDS = ("name", "organization", "category", "type", "function", "scenario",
               "workflow", "limitations", "status", "reported_claim", "verification", "notes")


def normalize(raw):
    if not isinstance(raw, dict):
        raise ValueError("每条资料必须是一个对象")
    aliases = {"名称": "name", "项目": "name", "日期": "date", "具体用途": "function",
               "使用场景（示例）": "scenario", "使用场景": "scenario", "使用方式": "workflow",
               "限制与注意": "limitations", "作者原始摘要（未独立确认）": "reported_claim",
               "机构": "organization", "类别": "category", "类型": "type", "项目链接": "links",
               "当期状态": "status", "核对说明": "verification", "补充来源": "additional_links",
               "同步视频": "video", "作者总结": "comment", "期次日期": "date", "技术类型": "type",
               "使用场景（应用示例）": "scenario", "作者原始摘要（未核实）": "reported_claim",
               "作者引用链接": "links", "补充链接": "additional_links", "作者总结评论": "comment", "核验说明": "verification"}
    raw = {aliases.get(k, k): v for k, v in raw.items()}
    item = {k: str(raw.get(k) or "").strip() for k in TEXT_FIELDS}
    item["name"] = item["name"] or str(raw.get("title") or "").strip()
    item["function"] = item["function"] or str(raw.get("content") or "").strip()
    if not item["name"] or not item["function"]:
        raise ValueError("项目名称和具体用途不能为空")
    if len(item["name"]) > 200 or any(len(item[k]) > 30000 for k in TEXT_FIELDS):
        raise ValueError("名称最多200字，单个说明字段最多30000字")
    item["date"] = date.fromisoformat(str(raw.get("date") or date.today())).isoformat()
    item["category"] = item["category"] or "补充资料"
    item["verification"] = item["verification"] or "用户补充，尚未独立核实"
    for key in ("links", "additional_links"):
        urls = raw.get(key) or []
        if isinstance(urls, str):
            urls = re.split(r"[\s,，]+", urls)
        if not isinstance(urls, list):
            raise ValueError("链接应为数组或按行分隔的 URL")
        clean = []
        for url in urls:
            url = str(url).strip()
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
                raise ValueError("来源链接必须是有效的 http 或 https 地址")
            if url not in clean:
                clean.append(url)
        item[key] = clean[:30]
    for key in ("video", "comment"):
        value = str(raw.get(key) or "").strip()
        if value and (urlparse(value).scheme not in ("https", "http") or not urlparse(value).hostname):
            raise ValueError("视频和作者总结链接须使用 http 或 https")
        item[key] = value
    return item


class Knowledge:
    def __init__(self, db_path, seed_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                    date TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
                    body TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(id UNINDEXED, tokens);
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY, entry_id TEXT NOT NULL, version INTEGER NOT NULL,
                    body TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, refs TEXT NOT NULL,
                    steps TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            if not db.execute("SELECT 1 FROM settings WHERE key='seed_imported'").fetchone():
                seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
                for i, raw in enumerate(seed):
                    self._insert(db, raw, f"K{i+1:04d}", "初始资料导入")
                db.execute("INSERT INTO settings VALUES ('seed_imported',?)", (now(),))
            # Published batches also reach existing databases, without replacing edits.
            # Filenames are immutable batch identifiers; corrections use a new batch.
            for batch in sorted((Path(seed_path).parent / "collections").glob("*.json")):
                marker = "collection:" + batch.name
                if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
                    continue
                rows = json.loads(batch.read_text(encoding="utf-8"))
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError("收录批次须为最多1000条的数组：" + batch.name)
                items = [normalize(raw) for raw in rows]
                for item in items:
                    fingerprint = self.fingerprint(item)
                    if db.execute("SELECT 1 FROM entries WHERE fingerprint=?", (fingerprint,)).fetchone():
                        continue
                    key = "U" + hashlib.sha256((batch.name + "|" + fingerprint).encode()).hexdigest()[:12]
                    self._insert(db, item, key, "精选批次导入：" + batch.name)
                db.execute("INSERT INTO settings VALUES (?,?)", (marker, now()))
            # Rebuild only the derived index, preserving records and user revisions.
            index_version = db.execute("SELECT value FROM settings WHERE key='search_index_version'").fetchone()
            if not index_version or index_version[0] != '2':
                for row in db.execute("SELECT * FROM entries").fetchall():
                    self._index(db, row['id'], self.decode(row))
                db.execute("INSERT OR REPLACE INTO settings VALUES ('search_index_version','2')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def fingerprint(item):
        return hashlib.sha256((item["date"] + "|" + item["name"].casefold()).encode()).hexdigest()

    def _index(self, db, key, item):
        db.execute("DELETE FROM search_index WHERE id=?", (key,))
        text = " ".join([item["name"]] * 6 + [item["category"], item["type"]] * 3 +
                        [item["function"], item["scenario"]] * 2 +
                        [item.get(k, "") for k in TEXT_FIELDS])
        db.execute("INSERT INTO search_index VALUES (?,?)", (key, " ".join(terms(text, unique=False))))

    def _insert(self, db, raw, key=None, reason="手动新增"):
        item = normalize(raw)
        key = key or "U" + uuid.uuid4().hex[:12]
        body = json.dumps(item, ensure_ascii=False)
        stamp = now()
        db.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,1,?)",
                   (key, self.fingerprint(item), item["date"], item["name"], item["category"], body, stamp))
        self._index(db, key, item)
        db.execute("INSERT INTO revisions(entry_id,version,body,reason,created_at) VALUES (?,1,?,?,?)",
                   (key, body, reason, stamp))
        return key

    @staticmethod
    def decode(row):
        return dict(json.loads(row["body"]), id=row["id"], version=row["version"], updated_at=row["updated_at"])

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (key,)).fetchone()
            return self.decode(row) if row else None

    def stats(self):
        with self.connect() as db:
            count, first, last = db.execute("SELECT count(*), min(date), max(date) FROM entries").fetchone()
            categories = [dict(r) for r in db.execute("SELECT category, count(*) AS count FROM entries GROUP BY category ORDER BY count DESC")]
            dates = [dict(r) for r in db.execute("SELECT date, count(*) AS count FROM entries GROUP BY date ORDER BY date")]
        return {"count": count, "first_date": first, "last_date": last, "categories": categories, "dates": dates}

    def search(self, query="", start_date="", end_date="", category="", limit=12, offset=0):
        where, params = ["1=1"], []
        for field, op, value in [("date", ">=", start_date), ("date", "<=", end_date), ("category", "=", category)]:
            if value:
                where.append(f"e.{field}{op}?")
                params.append(value)
        clause = " AND ".join(where)
        with self.connect() as db:
            if query.strip():
                # A named lookup should not expand into hundreds of installation
                # documents just because the query also contains common words.
                named = []
                lowered = query.casefold().strip()
                if not re.search(r"替代|类似|同类|alternative|similar", lowered):
                    for row in db.execute("SELECT DISTINCT name FROM entries"):
                        name = row['name'].casefold()
                        if re.search(r"[a-z0-9]", name):
                            found = bool(re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", lowered))
                        else:
                            found = len(name) >= 3 and name in lowered
                        if found:
                            named.append(row['name'])
                if named:
                    marks = ','.join('?' for _ in named)
                    records = db.execute(f"SELECT e.* FROM entries e WHERE {clause} AND name IN ({marks}) ORDER BY date DESC, name",
                                         params + named).fetchall()
                    records = sorted(records, key=lambda r: (r['name'].casefold() == lowered, len(r['name'])), reverse=True)
                    return {"items": [self.decode(r) for r in records[offset:offset+limit]], "total": len(records)}
                expanded = query.lower()
                for group in ALIASES:
                    if any(word in expanded for word in group):
                        expanded += " " + " ".join(group)
                ts = terms(expanded)[:180]
                if not ts:
                    return {"items": [], "total": 0}
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in ts)
                records = db.execute(f"""SELECT e.*, bm25(search_index) AS relevance FROM entries e
                    JOIN search_index s ON e.id=s.id WHERE search_index MATCH ? AND {clause}
                    ORDER BY relevance""", [match] + params).fetchall()
                query_latin = re.findall(r"[a-z0-9]+", query.lower())
                def score(r):
                    name = r["name"].lower()
                    return (100 if name in query.lower() or query.lower() in name else 0) + sum(8 for t in query_latin if t in name) - r["relevance"]
                records = sorted(records, key=score, reverse=True)
            else:
                records = db.execute(f"SELECT e.* FROM entries e WHERE {clause} ORDER BY date DESC, name", params).fetchall()
            return {"items": [self.decode(r) for r in records[offset:offset+limit]], "total": len(records)}

    def add(self, raw):
        with self.connect() as db:
            key = self._insert(db, raw)
        return self.get(key)

    def update(self, key, raw, version, reason="手动修订"):
        item = normalize(raw)
        with self.connect() as db:
            existing = db.execute("SELECT version FROM entries WHERE id=?", (key,)).fetchone()
            if not existing:
                raise KeyError(key)
            if existing[0] != version:
                raise ValueError("这条资料已被更新，请重新打开后再保存")
            body = json.dumps(item, ensure_ascii=False)
            stamp = now()
            changed = db.execute("""UPDATE entries SET fingerprint=?,date=?,name=?,category=?,body=?,version=?,updated_at=?
                WHERE id=? AND version=?""", (self.fingerprint(item), item["date"], item["name"], item["category"], body, version+1, stamp, key, version)).rowcount
            if changed != 1:
                raise ValueError("资料版本冲突，请重新打开后再保存")
            self._index(db, key, item)
            db.execute("INSERT INTO revisions(entry_id,version,body,reason,created_at) VALUES (?,?,?,?,?)",
                       (key, version+1, body, reason[:500], stamp))
        return self.get(key)

    def revisions(self, key):
        with self.connect() as db:
            return [dict(r, body=json.loads(r["body"])) for r in db.execute(
                "SELECT version,body,reason,created_at FROM revisions WHERE entry_id=? ORDER BY version DESC", (key,))]

    def import_text(self, filename, content):
        if len(content.encode()) > 2_000_000:
            raise ValueError("单次导入最多2MB")
        ext = Path(filename).suffix.lower()
        if ext == ".json":
            rows = json.loads(content.lstrip("\ufeff"))
            if isinstance(rows, dict):
                rows = rows.get("items", [rows])
        elif ext == ".csv":
            rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
        elif ext in (".md", ".txt"):
            rows = [{"name": next((x.lstrip("# ") for x in content.splitlines() if x.strip()), Path(filename).stem)[:200],
                     "function": content, "category": "补充文档", "notes": "导入文档；原有结构化项目仍需逐条整理"}]
        else:
            raise ValueError("支持 JSON、CSV、Markdown 和 TXT")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("单次须为最多1000条的数组")
        # Validate the complete batch before changing the database.
        items = [normalize(row) for row in rows]
        added, skipped = 0, 0
        with self.connect() as db:
            for item in items:
                if db.execute("SELECT 1 FROM entries WHERE fingerprint=?", (self.fingerprint(item),)).fetchone():
                    skipped += 1
                    continue
                self._insert(db, item, reason="文件导入：" + Path(filename).name)
                added += 1
        return {"added": added, "skipped": skipped}

    def new_session(self, title="新对话"):
        key, stamp = uuid.uuid4().hex, now()
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (key, title[:80], stamp, stamp))
        return {"id": key, "title": title[:80]}

    def sessions(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT 100")]

    def delete_session(self, session_id):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))

    def messages(self, session_id):
        with self.connect() as db:
            return [dict(r, refs=json.loads(r["refs"]), steps=json.loads(r["steps"])) for r in db.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (session_id,))]

    def append_turn(self, session_id, question, answer, refs, steps):
        with self.connect() as db:
            stamp = now()
            updated = db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (stamp, session_id))
            if not updated.rowcount:
                raise KeyError(session_id)
            for role, content, sources, trace in [("user", question, [], []), ("assistant", answer, refs, steps)]:
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex, session_id, role,
                    content, json.dumps(sources, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), stamp))
            db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新对话'", (question[:40], session_id))
