"""Run with python main.py. Secrets and mutable data stay on this machine."""
import asyncio
import contextlib
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import KnowledgeAgent, ProviderError
from knowledge import Knowledge

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")


def create_app(db_path=None, api_key=None, agent_override=None):
    store = Knowledge(db_path or os.getenv("KNOWLEDGE_DB", ROOT / "runtime/knowledge.sqlite3"), ROOT / "data/knowledge.json")
    key = os.getenv("DEEPSEEK_API_KEY", "") if api_key is None else api_key
    agent = agent_override or KnowledgeAgent(store, key, os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                             os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    app = FastAPI(title="小小信息", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.agent = store, agent
    active_sessions = set()
    access_token = os.getenv("APP_ACCESS_TOKEN", "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not access_token and request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
            return JSONResponse({"detail": "本机模式只允许通过本机地址访问"}, status_code=400)
        # Same-origin writes prevent another website from spending the local API key.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
        if request.url.path.startswith("/api/") and access_token:
            import hmac
            if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + access_token):
                return JSONResponse({"detail": "请输入访问口令"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.IntegrityError)
    async def duplicate(request, exc):
        return JSONResponse({"detail": "同一天已存在同名资料，请编辑已有条目。"}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)[:300]}, status_code=400)

    @app.get("/api/status")
    def status():
        return {"configured": bool(key), "model": agent.model, "knowledge": store.stats()}

    @app.get("/api/knowledge")
    def knowledge(q: str = "", start_date: str = "", end_date: str = "", category: str = "", limit: int = 20, offset: int = 0):
        from datetime import date
        for value in (start_date, end_date):
            if value:
                date.fromisoformat(value)
        if start_date and end_date and start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return store.search(q[:500], start_date, end_date, category[:80], min(max(limit, 1), 100), max(offset, 0))

    @app.get("/api/export")
    def export():
        rows = store.search(limit=1_000_000)["items"]
        return StreamingResponse(iter([json.dumps(rows, ensure_ascii=False, indent=2)]), media_type="application/json",
                                 headers={"Content-Disposition": 'attachment; filename="knowledge-backup.json"'})

    @app.get("/api/knowledge/{entry_id}")
    def entry(entry_id: str):
        item = store.get(entry_id)
        if not item:
            raise HTTPException(404, "资料不存在")
        return item

    @app.get("/api/knowledge/{entry_id}/revisions")
    def revisions(entry_id: str):
        if not store.get(entry_id):
            raise HTTPException(404, "资料不存在")
        return store.revisions(entry_id)

    @app.post("/api/knowledge", status_code=201)
    async def add(request: Request):
        raw = await limited_json(request)
        return store.add(raw)

    @app.put("/api/knowledge/{entry_id}")
    async def edit(entry_id: str, request: Request):
        raw = await limited_json(request)
        version = raw.pop("version", None)
        if not isinstance(version, int):
            raise ValueError("请提供资料版本号")
        reason = str(raw.pop("reason", "手动修订"))
        try:
            return store.update(entry_id, raw, version, reason)
        except KeyError:
            raise HTTPException(404, "资料不存在")

    @app.post("/api/import")
    async def import_file(request: Request):
        raw = await limited_json(request, 3_000_000)
        return store.import_text(str(raw.get("filename", "")), str(raw.get("content", "")))

    @app.get("/api/sessions")
    def sessions():
        return store.sessions()

    @app.post("/api/sessions", status_code=201)
    def new_session():
        return store.new_session()

    def ensure_session(session_id):
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise HTTPException(404, "对话不存在，请新建对话")

    @app.get("/api/sessions/{session_id}/messages")
    def messages(session_id: str):
        ensure_session(session_id)
        return store.messages(session_id)

    class ChatInput(BaseModel):
        session_id: str = Field(min_length=1, max_length=64)
        question: str = Field(min_length=1, max_length=5000)

    @app.post("/api/chat")
    async def chat(payload: ChatInput):
        ensure_session(payload.session_id)
        question = payload.question.strip()
        if not question:
            raise ValueError("请输入问题")
        if payload.session_id in active_sessions:
            raise HTTPException(409, "这段对话正在回答，请稍候")
        if len(active_sessions) >= 3:
            raise HTTPException(429, "当前正在处理多个问题，请稍后重试")
        active_sessions.add(payload.session_id)

        async def stream():
            queue = asyncio.Queue()

            async def work():
                try:
                    result = await agent.answer(question, store.messages(payload.session_id), queue.put)
                    store.append_turn(payload.session_id, question, result["answer"], result["sources"], result["steps"])
                    await queue.put({"type": "done", **result})
                except ProviderError as exc:
                    await queue.put({"type": "error", "message": str(exc)})
                except Exception:
                    # Never send provider payloads or local configuration to the browser.
                    await queue.put({"type": "error", "message": "本轮处理失败，未保存这次问答。请重试。"})

            task = asyncio.create_task(work())
            try:
                yield json.dumps({"type": "start", "label": "正在理解问题并检索知识库…"}, ensure_ascii=False) + "\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        yield '{"type":"heartbeat"}\n'
                        continue
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event["type"] in ("done", "error"):
                        break
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                active_sessions.discard(payload.session_id)

        return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})

    @app.get("/")
    def home():
        return FileResponse(ROOT / "web/index.html")

    archives = {"timeline": "产品君_AI周报时间线.html", "markdown": "产品君_AI周报_2026-07-12至11-01.md", "csv": "信息清单.csv"}

    @app.get("/archive/{name}")
    def archive(name: str):
        if name not in archives:
            raise HTTPException(404)
        # Downloads preserve the original archive's own layout and inline styles.
        return FileResponse(ROOT / archives[name], filename=archives[name])

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app


async def limited_json(request, max_bytes=1_000_000):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HTTPException(413, "文件过大，请分批导入")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("请求须为有效的 JSON")
    if not isinstance(result, dict):
        raise ValueError("请求须为一个 JSON 对象")
    return result


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("APP_ACCESS_TOKEN"):
        raise SystemExit("对外提供服务前，请设置 APP_ACCESS_TOKEN 访问口令。")
    uvicorn.run(create_app(), host=host, port=int(os.getenv("PORT", "8765")), access_log=False)
