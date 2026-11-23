"""Authenticated local HTTP adapter over the shared application."""

from dataclasses import asdict, replace
import asyncio
import base64
import binascii
from contextlib import contextmanager, suppress
import hmac
import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from anyio import CancelScope
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .app import RagApplication
from .cloud import guard_history_calls
from .auth import AccountStore, AuthenticationError, COOKIE_NAME, SESSION_SECONDS
from .config import Principal, Settings, load_settings
from .domain import ConflictError, NotFoundError, RagError, safe_error
from .file_manager import register_files, stage_document
from .http_inputs import (AccessInput, AccountDomainsInput, AccountInput, AssetIndex, AssetInput, AssetSearch,
                          AssetUpdate, DeleteInput, DocumentInput, LoginInput, PublishInput, QueryInput, RollbackInput)
from .evaluation_reports import ReportStore


def create_api(settings: Settings | None = None, *, mineru_settings=None) -> FastAPI:
    settings = settings or load_settings()
    application = RagApplication(settings)
    admin_tenants = {principal.tenant_id for principal in settings.api_tokens.values() if principal.admin}
    browser_tenant = settings.browser_tenant or (next(iter(admin_tenants)) if len(admin_tenants) == 1 else "demo")
    accounts = AccountStore(settings.data_dir, browser_tenant, settings.admin_password)
    api = FastAPI(title="Adaptive RAG Foundation", version="0.1.0")
    api.state.application = application
    api.state.accounts = accounts

    @api.middleware("http")
    async def browser_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(getattr(request.state, "identity_headers", {}))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    static = Path(__file__).resolve().parent / "static"
    api.mount("/static", StaticFiles(directory=static), name="static")

    @api.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(static / "index.html")

    @api.get("/images", include_in_schema=False)
    def image_workbench():
        return FileResponse(static / "images.html")

    @api.get("/knowledge", include_in_schema=False)
    def knowledge_workbench():
        return FileResponse(static / "knowledge.html")

    @api.get("/evaluation", include_in_schema=False)
    def evaluation_workbench():
        return FileResponse(static / "evaluation.html")

    def authenticate(request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, separator, token = header.partition(" ")
        found = None
        if separator and scheme.lower() == "bearer" and token and not any(c.isspace() for c in token):
            for configured, principal in settings.api_tokens.items():
                if hmac.compare_digest(configured.encode("utf-8"), token.encode("utf-8")):
                    found = principal
                    request.state.username = principal.subject_id
        if not header:
            account = accounts.session(request.cookies.get(COOKIE_NAME))
            if account is not None:
                found = account.principal
                request.state.username = account.username
        if found is None:
            raise AuthenticationError("请先登录工作台。")
        actual = {"X-RAG-Principal": quote(found.subject_id, safe=""), "X-RAG-Tenant": quote(found.tenant_id, safe="")}
        request.state.identity_headers = actual
        if any(request.headers.get(key, value) != value for key, value in actual.items()):
            raise ConflictError("登录账号已切换，请刷新工作台。")
        if not found.admin and found.data_domains is None:
            # Legacy Bearer readers receive the same limited default as
            # browser accounts; an omitted scope is never an HTTP bypass.
            found = replace(found, data_domains=("public",))
        return found

    def current_access(request, principal):
        current = authenticate(request)
        if current != principal:
            raise NotFoundError("conversation not found")
        return current.access

    def administrator(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.admin:
            from .domain import ForbiddenError
            raise ForbiddenError("Administrator permission required")
        return principal

    User = Annotated[Principal, Depends(authenticate)]
    Admin = Annotated[Principal, Depends(administrator)]

    def accessible_trace(principal: Principal, trace_id: str):
        value = application.get_accessible_trace(principal.access, trace_id)
        if not principal.admin and value.get("subject_id") != principal.subject_id:
            from .domain import NotFoundError
            raise NotFoundError("trace not found")
        return value

    @api.exception_handler(RagError)
    async def rag_error(_request: Request, exc: RagError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": str(exc)}})

    @api.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Omit submitted input values, which may contain private source text.
        details = [{"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "Invalid request", "details": details}})

    from starlette.exceptions import HTTPException

    @api.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, headers=exc.headers, content={"error": {"code": "unauthorized" if exc.status_code == 401 else "http_error", "message": str(exc.detail)}})

    @api.exception_handler(Exception)
    async def internal_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Internal application error"}})

    @api.get("/health")
    def health():
        return {"status": "ok", "mode": application.capabilities()["mode"]}

    @api.get("/capabilities")
    def capabilities(principal: User):
        return application.capabilities()

    @api.get("/console/session")
    def console_session(request: Request, principal: User):
        return {"tenant_id": principal.tenant_id, "subject_id": principal.subject_id,
                "username": request.state.username, "admin": principal.admin,
                "data_domains": principal.data_domains,
                "capabilities": application.capabilities()}

    @api.get("/access/domains")
    def data_domains(principal: User):
        from .data_access import DATA_DOMAIN_LABELS
        return {"domains": [{"id": key, "label": label} for key, label in DATA_DOMAIN_LABELS.items()],
                "data_domains": principal.data_domains}

    @api.post("/auth/login")
    def login(body: LoginInput, request: Request):
        token, account = accounts.login(body.username, body.password)
        principal = account.principal
        response = JSONResponse(content={"tenant_id": principal.tenant_id, "subject_id": principal.subject_id,
            "username": account.username, "admin": principal.admin, "data_domains": principal.data_domains,
            "capabilities": application.capabilities()})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                            samesite="lax", path="/")
        return response

    @api.post("/auth/logout", status_code=204)
    def logout(request: Request):
        if "x-rag-principal" in request.headers or "x-rag-tenant" in request.headers:
            authenticate(request)
        accounts.logout(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", path="/")
        return response

    @api.get("/admin/users")
    def users(principal: Admin):
        return {"users": accounts.users(principal)}

    @api.post("/admin/users", status_code=201)
    def create_user(body: AccountInput, principal: Admin):
        return accounts.create(principal, body.username, body.password, tuple(body.data_domains))

    @api.patch("/admin/users/{username}/domains")
    def update_user_domains(username: str, body: AccountDomainsInput, principal: Admin):
        return accounts.set_domains(principal, username, tuple(body.data_domains))

    @api.delete("/admin/users/{username}", status_code=204)
    def delete_user(username: str, principal: Admin):
        accounts.delete(principal, username)
        return Response(status_code=204)

    @api.post("/documents", status_code=201)
    def ingest(body: DocumentInput, principal: Admin):
        return stage_document(application, body, principal)

    @api.get("/documents")
    def documents(principal: Admin):
        snapshot = application.store.open_snapshot(principal.tenant_id)
        return {"documents": application.store.list_documents(principal.tenant_id),
                "generation": snapshot.generation if snapshot is not None else None}

    reports = ReportStore(settings.data_dir)

    @api.get("/evaluation/reports")
    def evaluation_reports(principal: Admin):
        return {"reports": reports.list(principal.tenant_id)}

    @api.get("/evaluation/reports/{report_id}")
    def evaluation_report(report_id: str, principal: Admin):
        # A missing report, a malformed ID, and another tenant's report all look the same.
        report = reports.get(principal.tenant_id, report_id)
        if report is None:
            raise NotFoundError("evaluation report not found")
        return report

    @api.post("/publish")
    def publish(body: PublishInput, principal: Admin):
        return asdict(application.publish(principal.tenant_id, body.job_id, body.expected_generation, principal=principal))

    @contextmanager
    def consistent_access(request, principal):
        # Detect committed ACL/trash changes anywhere during a multi-read response.
        # An epoch is monotonic even when the original permission is later restored.
        access = current_access(request, principal)
        epoch = application.store.access_epoch(access.tenant_id)
        yield access
        current_access(request, principal)
        if application.store.access_epoch(access.tenant_id) != epoch:
            raise NotFoundError("conversation not found")

    @api.get("/conversations")
    def conversation_list(request: Request, principal: User, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
        with consistent_access(request, principal) as access:
            return application.conversations.page(access, limit, offset)

    @api.get("/conversations/{conversation_id}")
    def conversation_detail(conversation_id: str, request: Request, principal: User):
        history = application.conversations
        with consistent_access(request, principal) as access:
            turns = list(history.turns(access, conversation_id))
            history.recheck(access, conversation_id, [turn["trace_id"] for turn in turns])
        return {"conversation_id": conversation_id, "turns": turns}

    async def run_authenticated_query(query_request, principal, request, observer=None):
        def check():
            with consistent_access(request, principal) as access:
                application.conversations.recheck(access, query_request.conversation_id, query_request.history_trace_ids)

        guard = (lambda: asyncio.to_thread(check)) if query_request.conversation_id else None
        with guard_history_calls(guard):
            return await application.query(query_request, observer)

    def release_trace(request, principal, trace_id):
        with consistent_access(request, principal):
            return accessible_trace(principal, trace_id)

    @api.post("/query")
    async def query(body: QueryInput, request: Request, principal: User):
        query_request = await asyncio.to_thread(application.conversations.request, principal.access, body)
        result = await run_authenticated_query(query_request, principal, request)
        # Account grants can change during remote/model work. Re-authenticate
        # and validate every trace source before returning any answer text.
        await asyncio.to_thread(release_trace, request, principal, result.trace_id)
        payload = asdict(result)
        if query_request.persist_conversation:
            payload["conversation_id"] = query_request.conversation_id or result.trace_id
        return payload

    @api.post("/query/stream")
    async def query_stream(body: QueryInput, request: Request, principal: User):
        query_request = await asyncio.to_thread(application.conversations.request, principal.access, body)

        async def events():
            # One queue belongs to one request; no global run registry or
            # pollable in-progress source data is created.
            queue = asyncio.Queue(maxsize=256)
            trace_id = None

            def observe(event, value):
                nonlocal trace_id
                if event == "trace":
                    trace_id = value["trace_id"]
                queue.put_nowait((event, value))

            async def run_query():
                try:
                    value = await run_authenticated_query(query_request, principal, request, observe)
                    trace = await asyncio.to_thread(release_trace, request, principal, value.trace_id)
                    queue.put_nowait(("result", {"result": asdict(value), "trace": trace}))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Plugins and transports may put credentials in exception
                    # text. Only a stable public code and generic text cross
                    # this streaming transport.
                    code = exc.code if isinstance(exc, RagError) else "internal_error"
                    diagnostic = safe_error(exc)
                    value = {"code": code, "message": diagnostic["message"],
                             "error": diagnostic, "trace_id": trace_id}
                    # A failed query can still have useful validated outputs.
                    # Recheck every source against current permissions before
                    # returning them, exactly as for a successful terminal.
                    if trace_id:
                        try:
                            value["trace"] = await asyncio.to_thread(release_trace, request, principal, trace_id)
                        except Exception:
                            pass
                    queue.put_nowait(("error", value))

            task = asyncio.create_task(run_query())
            try:
                while True:
                    event, value = await queue.get()
                    yield f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False, allow_nan=False)}\n\n"
                    if event in {"result", "error"}:
                        break
            finally:
                if not task.done():
                    task.cancel()
                # Starlette cancels its stream task group on disconnect. Shield
                # cleanup from repeated level cancellation while the pipeline
                # writes its bounded cancellation trace.
                with CancelScope(shield=True):
                    with suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    @api.post("/documents/delete")
    def delete_document(body: DeleteInput, principal: Admin):
        return asdict(application.delete_document(principal.tenant_id, body.document_id, body.expected_generation, principal=principal))

    @api.post("/rollback")
    def rollback(body: RollbackInput, principal: Admin):
        return asdict(application.rollback(principal.tenant_id, body.generation, body.expected_generation))

    @api.post("/access")
    @api.post("/documents/access")
    def access(body: AccessInput, principal: Admin):
        from .domain import ValidationError
        if body.allowed_subjects is None and body.revoked is None and body.data_domains is None:
            raise ValidationError("请选择要修改的资料权限。")
        application.set_access(principal.tenant_id, body.document_id,
            tuple(body.allowed_subjects) if body.allowed_subjects is not None else None,
            body.revoked, tuple(body.data_domains) if body.data_domains is not None else None, principal=principal)
        return {"status": "updated"}

    @api.get("/jobs/{job_id}")
    def job(job_id: str, principal: Admin):
        return application.get_job(principal.tenant_id, job_id)

    @api.get("/traces/{trace_id}")
    def trace(trace_id: str, request: Request, principal: User):
        return release_trace(request, principal, trace_id)

    @api.get("/evidence/{generation}/{evidence_id}")
    def evidence(generation: str, evidence_id: str, principal: User):
        return asdict(application.get_evidence(principal.access, generation, evidence_id))

    @api.get("/evidence/{generation}/{evidence_id}/images/{key}")
    def evidence_image(generation: str, evidence_id: str, key: str, request: Request, principal: User):
        content, media_type = application.get_evidence_image(principal.access, generation, evidence_id, key)
        # Same recheck as the image library: the session and the document may have changed during the read.
        application.get_evidence(authenticate(request).access, generation, evidence_id)
        return Response(content=content, media_type=media_type,
                        headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})

    def asset_view(value):
        return value | {"content_url": f"/assets/{value['asset_id']}/content"}

    @api.get("/assets")
    def list_assets(principal: User):
        return {"assets": [asset_view(value) for value in application.assets.list(principal.access)]}

    @api.post("/assets", status_code=201)
    def create_asset(body: AssetInput, principal: Admin):
        from .domain import ValidationError
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValidationError("图片编码无效，请重新选择图片。") from None
        values = body.model_dump(exclude={"content_base64"})
        values["tags"] = tuple(values["tags"])
        values["allowed_subjects"] = tuple(values["allowed_subjects"])
        values["data_domains"] = tuple(values["data_domains"])
        return asset_view(application.assets.managed_create(principal, content=content, **values))

    @api.post("/assets/search")
    async def search_assets(body: AssetSearch, request: Request, principal: User):
        result = await application.assets.search(principal.access, body.question, body.top_k)
        current = await asyncio.to_thread(authenticate, request)
        result = await asyncio.to_thread(application.assets.get_trace, current.access, result["trace_id"])
        result["hits"] = [hit | {"asset": asset_view(hit["asset"])} for hit in result.get("hits", ())]
        return result

    @api.post("/assets/index")
    async def index_assets(body: AssetIndex, principal: Admin):
        return await application.assets.process_pending(principal.tenant_id, body.limit)

    @api.get("/assets/traces/{trace_id}")
    def asset_trace(trace_id: str, principal: User):
        return application.assets.get_trace(principal.access, trace_id)

    @api.get("/assets/{asset_id}/content")
    def asset_content(asset_id: str, request: Request, principal: User):
        content, media_type = application.assets.content(principal.access, asset_id)
        application.assets.get(authenticate(request).access, asset_id)
        return Response(content=content, media_type=media_type,
                        headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})

    @api.get("/assets/{asset_id}/thumbnail")
    def asset_thumbnail(asset_id: str, request: Request, principal: User):
        content, media_type = application.assets.content(principal.access, asset_id, thumbnail=True)
        application.assets.get(authenticate(request).access, asset_id)
        return Response(content=content, media_type=media_type,
                        headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})

    @api.post("/assets/{asset_id}/reindex")
    def reindex_asset(asset_id: str, principal: Admin):
        return asset_view(application.assets.managed_reindex(principal, asset_id))

    @api.get("/assets/{asset_id}")
    def get_asset(asset_id: str, principal: User):
        return asset_view(application.assets.get(principal.access, asset_id))

    @api.patch("/assets/{asset_id}")
    def update_asset(asset_id: str, body: AssetUpdate, principal: Admin):
        values = body.model_dump()
        values["tags"] = tuple(values["tags"])
        values["allowed_subjects"] = tuple(values["allowed_subjects"])
        if values["data_domains"] is not None:
            values["data_domains"] = tuple(values["data_domains"])
        return asset_view(application.assets.managed_update(principal, asset_id, **values))

    @api.delete("/assets/{asset_id}", status_code=204)
    def delete_asset(asset_id: str, principal: Admin, expected_revision: str | None = None):
        application.assets.delete(principal, asset_id, expected_revision)
        return Response(status_code=204)

    register_files(api, application, authenticate, mineru_settings=mineru_settings)
    return api
