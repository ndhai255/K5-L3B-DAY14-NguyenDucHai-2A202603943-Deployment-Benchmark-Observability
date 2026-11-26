"""
Production AI Agent — Part 6 Final Project

Chạy:   uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2
Chấm:   python check_production_ready.py
"""
import json
import logging
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, record_cost
from app.rate_limiter import check_rate_limit
from app.redis_client import r, redis_healthy
from utils.mock_llm import ask as llm_ask

APP_VERSION = "1.0.0"


# ─────────────────────────────────────────────────────────
# Structured logging (tiêu chí 10): mỗi dòng log là 1 JSON object
# ─────────────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(entry, ensure_ascii=False, default=str)


def _build_logger() -> logging.Logger:
    log = logging.getLogger("agent")
    log.setLevel(settings.log_level)
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.propagate = False   # không để root logger in lại dòng không phải JSON
    return log


logger = _build_logger()


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={"fields": fields})


# ─────────────────────────────────────────────────────────
# Lifespan + graceful shutdown (tiêu chí 11)
# ─────────────────────────────────────────────────────────
class State:
    started_at = time.time()
    shutting_down = False


def _install_sigterm_hook() -> None:
    """
    Khi nhận SIGTERM: đánh dấu shutting_down để /ready trả 503 (load balancer ngừng gửi request mới),
    rồi GỌI LẠI handler cũ của uvicorn. Uvicorn sẽ ngừng nhận kết nối mới, chờ request đang chạy
    xong rồi thoát với exit code 0.
    """
    previous = signal.getsignal(signal.SIGTERM)

    def on_sigterm(signum, frame):
        State.shutting_down = True
        log_event("sigterm_received", draining=True)
        if callable(previous):
            previous(signum, frame)

    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except ValueError:      # không ở main thread (vd chạy trong test) → để uvicorn tự xử lý
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _install_sigterm_hook()
    log_event("startup", version=APP_VERSION, environment=settings.environment,
              rate_limit_per_minute=settings.rate_limit_per_minute,
              daily_budget_usd=settings.daily_budget_usd,
              monthly_budget_usd=settings.monthly_budget_usd)
    yield
    # Tới đây uvicorn đã xử lý xong mọi request đang chạy
    log_event("shutdown", uptime_s=round(time.time() - State.started_at, 1))
    try:
        r.close()
    except Exception:
        pass


app = FastAPI(title="Production AI Agent", version=APP_VERSION, lifespan=lifespan,
              docs_url=None if settings.environment == "production" else "/docs",
              redoc_url=None)


# ─────────────────────────────────────────────────────────
# Middleware: request id + log mỗi request
# ─────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    log_event("request", request_id=request_id, method=request.method, path=request.url.path,
              status=response.status_code, ms=ms)
    return response


# ─────────────────────────────────────────────────────────
# Xử lý lỗi: mã lỗi rõ ràng, không lộ stack trace
# ─────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "msg": e.get("msg")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "invalid_request", "details": errors})


@app.exception_handler(redis.RedisError)
async def redis_error(_request: Request, exc: redis.RedisError):
    log_event("redis_unavailable", logging.ERROR, error=type(exc).__name__)
    return JSONResponse(status_code=503, content={"error": "service_unavailable",
                                                  "detail": "Backing store unavailable, retry later"},
                        headers={"Retry-After": "5"})


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception):
    log_event("unhandled_error", logging.ERROR, error=type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": "internal_error"})


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: str | None = Field(default=None, max_length=128)

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v.strip()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)      # ~4 ký tự / token


@app.get("/health")
def health():
    """Liveness (tiêu chí 4): process còn sống → 200. Không phụ thuộc Redis."""
    return {"status": "ok", "version": APP_VERSION,
            "uptime_s": round(time.time() - State.started_at, 1)}


@app.get("/ready")
def ready():
    """Readiness (tiêu chí 5): 200 khi nhận được traffic, 503 khi Redis mất kết nối hoặc đang tắt."""
    if State.shutting_down:
        return JSONResponse(status_code=503, content={"status": "shutting_down"})
    if not redis_healthy():
        return JSONResponse(status_code=503, content={"status": "not_ready", "redis": "down"})
    return {"status": "ready", "redis": "ok"}


@app.post("/ask")
def ask(body: AskRequest, api_key: str = Depends(verify_api_key)):
    check_rate_limit(api_key)          # 429 khi vượt RATE_LIMIT_PER_MINUTE
    check_budget()                     # 402 khi hết budget

    answer = llm_ask(body.question)

    input_tokens, output_tokens = _estimate_tokens(body.question), _estimate_tokens(answer)
    cost = record_cost(input_tokens, output_tokens)
    log_event("llm_call", user_id=body.user_id, input_tokens=input_tokens,
              output_tokens=output_tokens, cost_usd=round(cost, 8))
    return {"question": body.question, "answer": answer,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cost_usd": round(cost, 8)}}
