"""FastAPI request entrance and checked response delivery. Start: python -m src."""
import asyncio
import contextlib
import json
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.api.admission import Admission
from src.api.readiness import Readiness
from src.bootstrap import seed_demo
from src.config import Settings
from src.contracts import Actor, AskRequest, Document, RequestContext, utcnow
from src.ingestion.embeddings import HashEmbeddings, OpenAIEmbeddingAdapter
from src.ingestion.extractors import SUPPORTED_SUFFIXES, extract_file_isolated
from src.ingestion.pipeline import IngestionPipeline
from src.observability.tracing import Telemetry
from src.orchestration.graph import QueryEngine
from src.policy.auth import Authenticator
from src.policy.safety import UnsafeContentError, sanitize_text
from src.retrieval.retriever import SourceRetriever
from src.storage.sessions import SessionDenied, SessionStore
from src.vectorstore.factory import get_vector_store

STATIC = Path(__file__).with_name("static")

def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = get_vector_store(settings)
        embeddings = (OpenAIEmbeddingAdapter(settings.openai_api_key.get_secret_value(),
                      model=settings.embedding_model, dimensions=settings.embedding_dimensions, approved_processing=True)
                      if settings.embedding_mode == "openai" else HashEmbeddings())
        store.assert_embedding_compatible(embeddings.fingerprint)
        app.state.embeddings = embeddings
        if settings.bootstrap_demo:
            await asyncio.to_thread(seed_demo, store, embeddings)
        app.state.store = store
        app.state.sessions = SessionStore(settings.session_ttl_seconds, settings.session_max_turns)
        app.state.telemetry = Telemetry()
        app.state.admission = Admission(settings.max_concurrent_requests, settings.max_user_requests)
        app.state.engine = QueryEngine(settings, store, SourceRetriever(store, embeddings), app.state.sessions, app.state.telemetry)
        app.state.readiness = Readiness(app.state.engine, store)
        app.state.upload_lock = asyncio.Semaphore(2)
        yield
        store.close()

    app = FastAPI(title="Engineering Atlas", version="1.0.0", lifespan=lifespan,
                  description="A policy-gated internal engineering assistant. Synthetic data by default.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    auth = Authenticator(settings)

    @app.middleware("http")
    async def request_boundary(request, call_next):
        # Bound JSON bodies even when clients omit or falsify Content-Length.
        if request.method in {"POST", "PUT", "PATCH"}:
            maximum = settings.max_upload_bytes + 65536 if "multipart/form-data" in request.headers.get("content-type", "") else 40000
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    return JSONResponse({"detail": "request_too_large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic error details can echo user content; only return field and type.
        return JSONResponse(status_code=422, content={"detail": "invalid_request", "errors": [
            {"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]})

    @app.exception_handler(SessionDenied)
    async def session_error(request, exc):
        return JSONResponse(status_code=404, content={"detail": "session_unavailable"})

    async def admin(actor: Actor = Depends(auth)):
        if not actor.admin:
            raise HTTPException(403, "administrator_required")
        return actor

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health/live")
    async def liveness():
        return {"status": "ok"}

    @app.get("/health")
    @app.get("/api/health")
    async def health(actor: Actor = Depends(auth)):
        try:
            counts = app.state.store.counts()
            readiness = await app.state.readiness.check()
            return {"status": readiness["status"], "mode": settings.model_mode,
                    "source_mode": "synthetic", "auth_mode": settings.auth_mode,
                    "services": readiness["services"],
                    "index": counts, "session_ttl_seconds": settings.session_ttl_seconds}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/health/ready")
    async def readiness(actor: Actor = Depends(auth)):
        result = await health(actor)
        if isinstance(result, dict) and result["status"] != "ok":
            return JSONResponse(result, status_code=503)
        return result

    async def prepare(payload, actor):
        try:
            question = sanitize_text(payload.question)
        except UnsafeContentError:
            raise HTTPException(422, "question_failed_content_check") from None
        sid = app.state.sessions.authorize(actor.id, payload.session_id)
        await app.state.engine.load_current_session(actor.id, sid)
        await app.state.admission.acquire(actor.id)
        return question, RequestContext(request_id=uuid4().hex, actor_ref=actor.id, session_ref=sid,
                                        deadline_at=utcnow() + timedelta(seconds=settings.request_timeout))

    @app.post("/ask")
    @app.post("/api/ask")
    async def ask(payload: AskRequest, actor: Actor = Depends(auth)):
        question, context = await prepare(payload, actor)
        async def ignore(stage, message):
            pass
        try:
            return await app.state.engine.run(question, context, ignore)
        except TimeoutError:
            raise HTTPException(504, {"code": "request_deadline", "request_id": context.request_id}) from None
        except Exception:
            raise HTTPException(503, {"code": "answer_unavailable", "request_id": context.request_id}) from None
        finally:
            await app.state.admission.release(actor.id)

    @app.post("/ask/stream")
    @app.post("/api/chat")
    async def chat(payload: AskRequest, request: Request, actor: Actor = Depends(auth)):
        question, context = await prepare(payload, actor)
        async def events():
            queue = asyncio.Queue(maxsize=30)
            async def progress(stage, message):
                await queue.put(("progress", {"stage": stage, "message": message, "request_id": context.request_id}))
            async def work():
                try:
                    result = await app.state.engine.run(question, context, progress)
                    await queue.put(("answer", result.model_dump(mode="json")))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await queue.put(("error", {"code": "answer_unavailable", "message": "The answer could not be completed. Please try again.",
                                                "request_id": context.request_id}))
                finally:
                    await queue.put(("done", {"request_id": context.request_id, "session_id": context.session_ref}))
            task = asyncio.create_task(work())
            try:
                yield format_event("progress", {"stage": "started", "message": "Starting your search", "request_id": context.request_id})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=1)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield format_event(event, data)
                    if event == "done":
                        break
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await app.state.admission.release(actor.id)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @app.get("/api/sessions")
    async def sessions(actor: Actor = Depends(auth)):
        current = []
        for item in app.state.sessions.list(actor.id):
            try:
                await app.state.engine.load_current_session(actor.id, item["id"])
                current.append(item)
            except SessionDenied:
                continue
        return {"sessions": current}

    @app.get("/api/sessions/{sid}")
    async def session(sid: str, actor: Actor = Depends(auth)):
        return await app.state.engine.load_current_session(actor.id, sid)

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str, actor: Actor = Depends(auth)):
        app.state.sessions.delete(actor.id, sid)
        return {"status": "deleted"}

    @app.get("/api/activity")
    async def activity(actor: Actor = Depends(auth)):
        return {"activity": app.state.telemetry.list(actor.id)}

    @app.get("/api/documents")
    async def documents(actor: Actor = Depends(auth)):
        return {"documents": [d for d in app.state.store.list_documents() if not d.get("blocked")]}

    @app.post("/api/documents/upload")
    async def upload(file: UploadFile = File(...), actor: Actor = Depends(admin)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(415, "unsupported_document_format")
        async with app.state.upload_lock:
            path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=settings.data_dir) as target:
                    path = Path(target.name)
                    size = 0
                    while chunk := await file.read(65536):
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            raise HTTPException(413, "document_too_large")
                        target.write(chunk)
                extracted = await asyncio.to_thread(extract_file_isolated, path)
                doc_id = uuid4().hex
                document = Document(id=doc_id, source="local", title=extracted.title or Path(file.filename).stem,
                                    body=extracted.text, url=f"/api/documents/{doc_id}/content",
                                    updated_at=utcnow(), company_wide=True,
                                    metadata={"format": suffix, "blocks": extracted.blocks})
                await asyncio.to_thread(IngestionPipeline(app.state.store, app.state.embeddings).ingest, [document], scope="uploads")
                published = app.state.store.get_document(doc_id)
                if published is None:
                    raise HTTPException(422, "document_quarantined")
                app.state.readiness.invalidate()
                return {"id": doc_id, "title": published.title, "status": "indexed"}
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(422, "document_could_not_be_processed") from None
            finally:
                await file.close()
                if path:
                    path.unlink(missing_ok=True)

    @app.get("/api/documents/{document_id:path}/content")
    async def document_content(document_id: str, actor: Actor = Depends(auth)):
        document = app.state.store.get_document(document_id)
        if document is None:
            raise HTTPException(404, "document_unavailable")
        return PlainTextResponse(document.body)

    @app.delete("/api/documents/{document_id:path}")
    async def delete_document(document_id: str, actor: Actor = Depends(admin)):
        if app.state.store.get_document(document_id) is None:
            raise HTTPException(404, "document_unavailable")
        app.state.store.revoke(document_id, reason="deleted")
        app.state.readiness.invalidate()
        app.state.sessions.invalidate(document_id)
        return {"status": "deleted"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app

def format_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

app = create_app()

