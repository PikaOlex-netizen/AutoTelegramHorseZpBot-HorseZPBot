from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .config import AppConfig
from .database import Database
from .models import OutboundMessage, utcnow
from .schemas import (
    AccountCreateRequest, AddChatRequest, ChatAssignmentRequest, CodeLoginRequest, ManualMessageRequest,
    FarmSettingsRequest, GeminiSettingsRequest, PasswordLoginRequest, PhoneLoginRequest, PSSettingsRequest, SettingsRequest,
    StartSchedulerRequest, TemplateRequest,
)
from .services.audit import AuditLog
from .services.accounts import AccountService
from .services.chats import ChatManager
from .services.messages import MessageManager
from .services.gemini import GeminiAssistant
from .services.keyword_reply import KeywordReplyAssistant
from .services.farm import FarmRegistry
from .services.ps import PSRegistry
from .services.scheduler import SchedulerRegistry
from .services.settings import SettingsService
from .services.telegram_client import TelegramService, TelegramUnavailable


def application_services(request: Request) -> dict:
    return request.app.state.services


def selected_account_id(services: dict) -> int:
    return services["accounts"].selected_id()


async def terminal_heartbeat(
    stop_event: asyncio.Event, accounts: AccountService, scheduler: SchedulerRegistry,
    messages: MessageManager, audit: AuditLog, interval: int,
) -> None:
    """Give the terminal a useful confirmation during otherwise quiet periods."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            continue
        except asyncio.TimeoutError:
            pass
        summaries: list[str] = []
        active_sends = False
        for account in accounts.list():
            states = scheduler.for_account(account["id"]).all_states()
            pch, pb = states["PCH"]["status"], states["PB"]["status"]
            active_sends = active_sends or pch == "SENDING" or pb == "SENDING"
            summaries.append(f"{account['label']}: ПЧ={pch}, ПБ={pb}")
        if not active_sends and not messages.is_processing_deletions:
            audit.write("INFO", "terminal", "idle_report", "No send/delete action right now; " + "; ".join(summaries))


def as_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    if isinstance(exc, (ValueError, TelegramUnavailable)):
        return HTTPException(400, str(exc))
    return HTTPException(500, "Internal server error; see Logs for the non-sensitive error details")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = AppConfig.load()
    db = Database(config.database_url)
    db.initialize()
    settings = SettingsService(db, config)
    settings.initialize()
    audit = AuditLog(db, settings)
    accounts = AccountService(db, settings, audit)
    accounts.initialize()
    telegram = TelegramService(config, audit, settings, accounts)
    chats = ChatManager(db, telegram, audit)
    messages = MessageManager(db, telegram, settings, audit)
    scheduler = SchedulerRegistry(db, chats, messages, settings, audit, accounts)
    farm = FarmRegistry(db, chats, messages, audit, accounts)
    ps = PSRegistry(db, chats, messages, audit, accounts)
    gemini = GeminiAssistant(config, db, settings, accounts, telegram, chats, messages, audit)
    keyword_reply = KeywordReplyAssistant(db, accounts, telegram, chats, messages, audit)

    async def synchronize_authorized_account(account_id: int) -> None:
        # The first successful authorization imports every dialog automatically.
        # Existing assignments are preserved by ChatManager.refresh_from_telegram.
        await chats.refresh_from_telegram(account_id)
        await gemini.ensure_listener(account_id)
        await keyword_reply.ensure_listener(account_id)

    telegram.add_authorized_callback(synchronize_authorized_account)
    scheduler.initialize()
    farm.initialize()
    ps.initialize()
    app.state.services = {
        "config": config, "db": db, "audit": audit, "settings": settings,
        "accounts": accounts, "telegram": telegram, "chats": chats, "messages": messages, "scheduler": scheduler, "farm": farm, "ps": ps,
        "gemini": gemini, "keyword_reply": keyword_reply,
    }
    # A session can already be authorized when the application is opened again.
    # Import its dialogs once if this account has no local chat inventory yet.
    for account in accounts.list():
        account_id = account["id"]
        try:
            status = await telegram.status(account_id)
            if status["authorized"] and not chats.list(account_id):
                await synchronize_authorized_account(account_id)
        except Exception as exc:
            audit.write("WARNING", "chats", "startup_sync_failed", f"Initial chat import failed: {type(exc).__name__}", account_id=account_id)
    audit.write("INFO", "system", "startup", "Telegram Scheduler started")
    await messages.start()
    await scheduler.restore()
    await farm.restore()
    await ps.restore()
    await gemini.start()
    await keyword_reply.start()
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        terminal_heartbeat(heartbeat_stop, accounts, scheduler, messages, audit, config.terminal_idle_report_seconds),
        name="terminal-heartbeat",
    )
    try:
        yield
    finally:
        audit.write("INFO", "system", "shutdown", "Graceful shutdown started")
        heartbeat_stop.set()
        await heartbeat_task
        await chats.shutdown()
        await farm.shutdown()
        await ps.shutdown()
        await scheduler.shutdown()
        await gemini.shutdown()
        await keyword_reply.shutdown()
        await messages.stop()
        await telegram.disconnect()
        db.close()


app = FastAPI(title="Telegram Scheduler", version="1.0.0", lifespan=lifespan)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status(request: Request):
    services = application_services(request)
    account_id = selected_account_id(services)
    telegram_status = await services["telegram"].status(account_id)
    chats = services["chats"].list(account_id)
    messages = services["messages"].list(account_id, 1000)
    schedulers = services["scheduler"].for_account(account_id).all_states()
    farm = services["farm"].for_account(account_id).get_state()
    pch_assigned = sum(chat["pch_enabled"] for chat in chats)
    pb_assigned = sum(chat["pb_enabled"] for chat in chats)
    selected_for_mailings = sum(chat["pch_enabled"] or chat["pb_enabled"] for chat in chats)

    # Keep the dashboard useful without introducing a browser-side data store:
    # seven compact daily buckets are calculated from the durable message ledger.
    today = utcnow().date()
    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    activity_by_day = {day.isoformat(): {"sent": 0, "pch": 0, "pb": 0, "reply": 0, "ps": 0, "farm": 0} for day in days}
    start_of_period = datetime.combine(days[0], time.min)
    with services["db"].session() as session:
        recent_messages = session.scalars(
            select(OutboundMessage).where(
                OutboundMessage.account_id == account_id,
                OutboundMessage.sent_at >= start_of_period,
            )
        ).all()
    for message in recent_messages:
        sent_at = message.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        # Convert to local time for bucket assignment
        bucket = activity_by_day.get(sent_at.astimezone().date().isoformat())
        if bucket is not None:
            bucket["sent"] += 1
            if message.message_type == "PCH":
                bucket["pch"] += 1
            elif message.message_type == "PB":
                bucket["pb"] += 1
            elif message.message_type == "REPLY":
                bucket["reply"] += 1
            elif message.message_type == "PS":
                bucket["ps"] += 1
            elif message.message_type == "FARM":
                bucket["farm"] += 1

    def workload(assigned: int, active: bool) -> dict[str, int | bool]:
        total = len(chats)
        return {
            "assigned": assigned,
            "total": total,
            "percent": round(assigned * 100 / total) if total else 0,
            "active": active,
        }

    return {
        "account_id": account_id,
        "accounts": services["accounts"].list(),
        "telegram": telegram_status,
        "chats": {
            "total": len(chats), "manual": len(chats),
            "pch": pch_assigned, "pb": pb_assigned, "selected": selected_for_mailings,
        },
        "schedulers": schedulers,
        "farm": farm,
        "messages": {
            "sent": len(messages), "deleted": sum(m["status"] == "DELETED" for m in messages),
            "pending_deletion": sum(m["status"] in {"DELETE_PENDING", "CHECKING", "DELETING"} for m in messages),
            "errors": sum(m["status"] == "FAILED" for m in messages),
        },
        "dashboard": {
            "activity": {
                "days": [
                    {
                        "date": day.isoformat(),
                        "sent": activity_by_day[day.isoformat()]["sent"],
                        "pch": activity_by_day[day.isoformat()]["pch"],
                        "pb": activity_by_day[day.isoformat()]["pb"],
                        "reply": activity_by_day[day.isoformat()]["reply"],
                        "ps": activity_by_day[day.isoformat()]["ps"],
                        "farm": activity_by_day[day.isoformat()]["farm"]
                    }
                    for day in days
                ],
                "total_7d": sum(bucket["sent"] for bucket in activity_by_day.values()),
                "today": activity_by_day[today.isoformat()]["sent"],
            },
            "workload": {
                "pch": workload(pch_assigned, schedulers["PCH"]["enabled"]),
                "pb": workload(pb_assigned, schedulers["PB"]["enabled"]),
                "farm": {"active": farm["enabled"], "percent": 100 if farm["enabled"] else 0},
            },
        },
    }


@app.get("/api/accounts")
async def list_accounts(request: Request):
    services = application_services(request)
    selected = selected_account_id(services)
    items = []
    for account in services["accounts"].list():
        items.append({**account, "selected": account["id"] == selected, "telegram": await services["telegram"].status(account["id"])})
    return {"accounts": items, "selected_account_id": selected, "max_accounts": 3}


@app.post("/api/accounts")
async def create_account(payload: AccountCreateRequest, request: Request):
    try:
        services = application_services(request)
        account = services["accounts"].create(payload.label)
        services["scheduler"].for_account(account["id"])
        services["farm"].for_account(account["id"])
        services["accounts"].select(account["id"])
        return account
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/accounts/{account_id}/select")
async def select_account(account_id: int, request: Request):
    try:
        return application_services(request)["accounts"].select(account_id)
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.put("/api/accounts/{account_id}")
async def rename_account(account_id: int, payload: AccountCreateRequest, request: Request):
    try:
        return application_services(request)["accounts"].rename(account_id, payload.label)
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/auth/status")
async def auth_status(request: Request):
    services = application_services(request)
    account_id = selected_account_id(services)
    await services["gemini"].ensure_listener(account_id)
    await services["keyword_reply"].ensure_listener(account_id)
    return await services["telegram"].status(account_id)


@app.post("/api/auth/phone")
async def auth_phone(payload: PhoneLoginRequest, request: Request):
    try:
        services = application_services(request)
        return await services["telegram"].begin_phone_login(selected_account_id(services), payload.phone)
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/auth/code")
async def auth_code(payload: CodeLoginRequest, request: Request):
    try:
        services = application_services(request)
        account_id = selected_account_id(services)
        result = await services["telegram"].submit_code(account_id, payload.code)
        if result["status"] == "AUTHORIZED":
            await services["gemini"].ensure_listener(account_id)
            await services["keyword_reply"].ensure_listener(account_id)
        return result
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/auth/2fa")
async def auth_two_factor(payload: PasswordLoginRequest, request: Request):
    try:
        services = application_services(request)
        account_id = selected_account_id(services)
        result = await services["telegram"].submit_password(account_id, payload.password)
        if result["status"] == "AUTHORIZED":
            await services["gemini"].ensure_listener(account_id)
            await services["keyword_reply"].ensure_listener(account_id)
        return result
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/auth/qr")
async def auth_qr(request: Request):
    try:
        services = application_services(request)
        return await services["telegram"].begin_qr_login(selected_account_id(services))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    try:
        services = application_services(request)
        await services["telegram"].logout(selected_account_id(services))
        return {"status": "LOGGED_OUT"}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/chats")
async def list_chats(request: Request, filter: str | None = Query(default=None), include_archived: bool = False):
    services = application_services(request)
    return services["chats"].list(selected_account_id(services), filter, include_archived=include_archived)


@app.post("/api/chats/refresh")
async def refresh_chats(request: Request):
    try:
        services = application_services(request)
        return await services["chats"].refresh_from_telegram(selected_account_id(services))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/chats")
async def add_chat(payload: AddChatRequest, request: Request):
    try:
        services = application_services(request)
        return await services["chats"].add_from_telegram(selected_account_id(services), payload.chat_id)
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.put("/api/chats/{chat_id}")
async def update_chat(chat_id: int, payload: ChatAssignmentRequest, request: Request):
    try:
        services = application_services(request)
        return services["chats"].update_assignment(selected_account_id(services), chat_id, **payload.model_dump(exclude_none=True))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: int, request: Request):
    try:
        services = application_services(request)
        services["chats"].remove_from_mailings(selected_account_id(services), chat_id)
        return {"status": "ARCHIVED"}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/messages")
async def list_messages(request: Request, limit: int = Query(200, ge=1, le=1000)):
    services = application_services(request)
    return services["messages"].list(selected_account_id(services), limit)


@app.post("/api/messages/send")
async def send_message(payload: ManualMessageRequest, request: Request):
    services = application_services(request)
    account_id = selected_account_id(services)
    # PS and Farm are single-chat selections, so they only require the chat to be enabled
    if payload.message_type in {"PCH", "PB"}:
        if not services["chats"].is_currently_eligible(account_id, payload.chat_id, payload.message_type):
            raise HTTPException(400, "Select this enabled chat in the corresponding manual mailing list first")
    else:
        if not services["chats"].is_currently_eligible(account_id, payload.chat_id):
            raise HTTPException(400, "Select this enabled chat first")
    try:
        message_id = await services["messages"].send(account_id, payload.chat_id, payload.text, payload.message_type)
        return {"telegram_message_id": message_id, "status": "DELETE_PENDING"}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/scheduler/status")
async def scheduler_status(request: Request):
    services = application_services(request)
    return services["scheduler"].for_account(selected_account_id(services)).all_states()


@app.get("/api/farm")
async def farm_status(request: Request):
    services = application_services(request)
    return services["farm"].for_account(selected_account_id(services)).get_state()


@app.put("/api/farm")
async def farm_update(payload: FarmSettingsRequest, request: Request):
    try:
        services = application_services(request)
        return await services["farm"].for_account(selected_account_id(services)).configure(
            chat_id=payload.chat_id, text=payload.text, enabled=payload.enabled,
        )
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/ps")
async def ps_status(request: Request):
    services = application_services(request)
    return services["ps"].for_account(selected_account_id(services)).get_state()


@app.put("/api/ps")
async def ps_update(payload: PSSettingsRequest, request: Request):
    try:
        services = application_services(request)
        return await services["ps"].for_account(selected_account_id(services)).configure(
            chat_id=payload.chat_id, text=payload.text, enabled=payload.enabled,
        )
    except Exception as exc:
        raise as_http_error(exc) from exc


def normalized_kind(kind: str) -> str:
    kind = kind.upper()
    if kind not in {"PCH", "PB"}:
        raise HTTPException(404, "Scheduler must be pch or pb")
    return kind


@app.get("/api/scheduler/{kind}")
async def one_scheduler(kind: str, request: Request):
    try:
        services = application_services(request)
        return services["scheduler"].for_account(selected_account_id(services)).get_state(normalized_kind(kind))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/scheduler/{kind}/start")
async def scheduler_start(kind: str, payload: StartSchedulerRequest, request: Request):
    try:
        services = application_services(request)
        return await services["scheduler"].for_account(selected_account_id(services)).start(normalized_kind(kind), payload.text)
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/scheduler/{kind}/pause")
async def scheduler_pause(kind: str, request: Request):
    try:
        services = application_services(request)
        return await services["scheduler"].for_account(selected_account_id(services)).pause(normalized_kind(kind))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/scheduler/{kind}/resume")
async def scheduler_resume(kind: str, request: Request):
    try:
        services = application_services(request)
        return await services["scheduler"].for_account(selected_account_id(services)).resume(normalized_kind(kind))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/scheduler/{kind}/stop")
async def scheduler_stop(kind: str, request: Request):
    try:
        services = application_services(request)
        return await services["scheduler"].for_account(selected_account_id(services)).stop(normalized_kind(kind))
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/scheduler/{kind}/template")
async def get_template(kind: str, request: Request):
    try:
        services = application_services(request)
        return {"text": services["scheduler"].for_account(selected_account_id(services)).get_template(normalized_kind(kind))}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.put("/api/scheduler/{kind}/template")
async def put_template(kind: str, payload: TemplateRequest, request: Request):
    try:
        services = application_services(request)
        services["scheduler"].for_account(selected_account_id(services)).set_template(normalized_kind(kind), payload.text)
        return {"status": "SAVED"}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/settings")
async def get_settings(request: Request):
    return application_services(request)["settings"].get_all()


@app.put("/api/settings")
async def put_settings(payload: SettingsRequest, request: Request):
    return application_services(request)["settings"].update(payload.model_dump())


@app.get("/api/gemini/settings")
async def get_gemini_settings(request: Request):
    services = application_services(request)
    return services["gemini"].settings_for(selected_account_id(services))


@app.put("/api/gemini/settings")
async def put_gemini_settings(payload: GeminiSettingsRequest, request: Request):
    try:
        services = application_services(request)
        account_id = selected_account_id(services)
        result = services["gemini"].update_settings(account_id, **payload.model_dump())
        if result["enabled"]:
            await services["gemini"].ensure_listener(account_id)
        return result
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/logs")
async def get_logs(
    request: Request, limit: int = Query(200, ge=1, le=1000), level: str | None = None,
    module: str | None = None, mailing_type: str | None = None, chat_id: int | None = None,
    message_id: int | None = None, q: str | None = None,
):
    services = application_services(request)
    return services["audit"].list(
        limit=limit, level=level, module=module, mailing_type=mailing_type, chat_id=chat_id,
        message_id=message_id, query=q, account_id=selected_account_id(services),
    )


@app.get("/api/events")
async def events(request: Request):
    services = application_services(request)
    audit = services["audit"]
    account_id = selected_account_id(services)

    async def generate():
        async for payload in audit.stream():
            if payload.get("account_id") not in {None, account_id}:
                continue
            event_name = payload.pop("event", "log.created")
            yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/system")
async def system(request: Request):
    services = application_services(request)
    return {
        "data_directory": str(services["config"].data_dir),
        "session_persistent": Path(services["telegram"]._session_path(selected_account_id(services))).exists(),
        "selected_account_id": selected_account_id(services),
        "max_accounts": 3,
        "scheduler_recovery": True,
        "delete_task_recovery": True,
    }


def run() -> None:
    import uvicorn
    config = AppConfig.load()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    uvicorn.run("app.main:app", host=config.host, port=config.port, reload=False)


if __name__ == "__main__":
    run()
