# Day 14 Lab - Mission Answers

## Part 1: Localhost vs Production

_(chưa làm — CP1)_

## Part 2: Docker

_(chưa làm — CP2)_

## Part 3: Cloud Deployment

_(chưa làm — CP6)_

## Part 4: API Security

_(chưa làm — CP3)_

## Part 5: Scaling & Reliability

_(chưa làm — CP4)_

## Part 6: Final Project

Code trong `06-lab-complete/`. Output `python check_production_ready.py` (chạy đầy đủ, có Docker, ngày 2026-11-26):

```text
================================================================
  📋 Checklist Deliverable
     ✅ Dockerfile (multi-stage, < 500 MB)
     ✅ docker-compose.yml (agent + redis)
     ✅ .dockerignore
     ✅ Health check endpoint (GET /health)
     ✅ Readiness endpoint (GET /ready)
     ✅ API Key authentication
     ✅ Rate limiting
     ✅ Cost guard
     ✅ Config từ environment variables
     ✅ Structured logging
     ✅ Graceful shutdown
     ✅ Public URL ready (Railway / Render config)
----------------------------------------------------------------
  Result: 12/12 tiêu chí đạt · 43/43 checks passed
  🎉 PASS 12/12 — PRODUCTION READY! Deploy nào!
================================================================
```

Image build ra 63 MB (`python:3.11-slim`, multi-stage). `docker stop` tắt trong 0,7 s, exit code 0.

### Cách làm từng phần

| Tiêu chí | File | Cách làm |
|---|---|---|
| Config (9) | `app/config.py` | Dataclass đọc `os.getenv` cho `PORT`, `ENVIRONMENT`, `AGENT_API_KEY`, `REDIS_URL`, `RATE_LIMIT_PER_MINUTE`, `DAILY_BUDGET_USD`, `MONTHLY_BUDGET_USD`, `LOG_LEVEL`. `AGENT_API_KEY` mặc định rỗng → mọi `/ask` bị 401 tới khi đặt key. |
| API key (6) | `app/auth.py` | `APIKeyHeader("X-API-Key", auto_error=False)`; thiếu/sai → 401. So sánh bằng `secrets.compare_digest` để tránh timing attack. |
| Redis (5) | `app/redis_client.py` | `redis.from_url(...)` chỉ tạo pool, không kết nối lúc import → app khởi động được khi chưa có Redis. `socket_connect_timeout=2` để `/ready` trả 503 nhanh. |
| Rate limit (7) | `app/rate_limiter.py` | Sliding window bằng sorted set, key theo hash SHA-256 của API key: `ZREMRANGEBYSCORE` → `ZADD` → `ZCARD` → `EXPIRE` trong một transaction `MULTI/EXEC`, nên 2 worker đếm chung. Vượt → 429 + `Retry-After`; request bị từ chối được `ZREM` để không ăn vào hạn mức. |
| Cost guard (8) | `app/cost_guard.py` | `INCRBYFLOAT` vào `cost:daily:YYYY-MM-DD` và `cost:monthly:YYYY-MM` (có TTL). `check_budget()` trước khi gọi LLM, vượt ngân sách ngày hoặc tháng → 402. Token ước lượng ~4 ký tự/token, giá gpt-4o-mini. |
| Health/Ready (4, 5) | `app/main.py` | `/health` không chạm Redis, luôn 200. `/ready` ping Redis → 503 khi mất kết nối hoặc khi đang tắt. |
| Logging (10) | `app/main.py` | `JsonFormatter` (`json.dumps`) cho logger `agent`, `propagate=False`. Middleware log mỗi request: `request_id`, `method`, `path`, `status`, `ms`. |
| Xử lý lỗi | `app/main.py` | Handler cho `RequestValidationError` (422 có field + msg), `RedisError` (503 + `Retry-After`), `Exception` (500 `internal_error`) — không trả stack trace cho client. |
| Graceful shutdown (11) | `app/main.py`, `Dockerfile` | Lifespan bọc handler SIGTERM của uvicorn: bật cờ `shutting_down` (→ `/ready` 503) rồi gọi lại handler cũ để uvicorn ngừng nhận request mới và chờ request đang chạy xong. `CMD sh -c "exec uvicorn ..."` để uvicorn là PID 1. |
| Docker (1, 3) | `Dockerfile`, `.dockerignore` | Stage builder cài vào `/opt/venv`; stage runtime `python:3.11-slim`, user `app` (uid 10001), `HEALTHCHECK` gọi `/health`, lắng nghe `${PORT:-8000}`. `.dockerignore` loại `.env*`, `__pycache__`, `venv`, `.git`, `benchmark-observability/`. |
| Compose (2) | `docker-compose.yml` | `agent` build từ Dockerfile, `REDIS_URL=redis://redis:6379/0`, `depends_on: redis (service_healthy)`, đọc `.env` với `required: false`. `redis:7-alpine` có healthcheck + volume. |
| Deploy (12) | `railway.toml`, `render.yaml` | Railway: `builder = "DOCKERFILE"`, `healthcheckPath = "/health"`. Render: web service Docker + Key Value Redis, `AGENT_API_KEY` dùng `generateValue: true`, `REDIS_URL` lấy `fromService`. |

### Test thủ công bằng docker compose

```text
GET /health                          → 200 {"status":"ok",...}
GET /ready                           → 200 {"status":"ready","redis":"ok"}
POST /ask không key                  → 401 {"detail":"Missing or invalid API key"}
POST /ask có key (RATE_LIMIT=3) ×4   → 200, 200, 200, 429
POST /ask body JSON hỏng             → 422 {"error":"invalid_request",...}
POST /ask question toàn khoảng trắng → 422
Dừng redis: GET /ready               → 503 {"status":"not_ready","redis":"down"}
Dừng redis: GET /health              → 200
Dừng redis: POST /ask                → 503 {"error":"service_unavailable",...}
```

---

## AI log

| # | Phần | Hỏi AI gì | Nhận được gì | Tự sửa / tự kiểm tra thế nào |
|---|---|---|---|---|
| 1 | Part 6 — toàn bộ `06-lab-complete/` | Dùng Claude Code (Claude Opus 5.5) để hoàn thành các TODO của bản khung cho đạt 12 tiêu chí của `check_production_ready.py`. | Code cho `app/*.py`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `railway.toml`, `render.yaml`. AI đọc mã nguồn uvicorn 0.30 để xác nhận uvicorn đăng ký SIGTERM bằng `signal.signal` trước lifespan, nên hook trong lifespan bọc lại được handler cũ. | Chạy `check_production_ready.py` đầy đủ → PASS 12/12. Test tay qua `docker compose` (401 / 200 / 429 / 422 / Redis down → 503). _(Bổ sung: phần bạn tự đọc lại, tự giải thích được, và chỗ đã tự sửa)_ |
