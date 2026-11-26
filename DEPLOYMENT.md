# Deployment Information

## Public URL

https://agent-production-bfae1a95.up.railway.app

## Platform

**Railway** — project `day14-agent`, environment `production`, region `us-east` (iad). Deploy chính agent Part 6 trong `06-lab-complete/` (không phải app mẫu `03-cloud-deployment/`).

| Service | Nguồn | Ghi chú |
|---|---|---|
| `agent` | `railway up` từ `06-lab-complete/`, build bằng `Dockerfile` (multi-stage, `python:3.11-slim`, user non-root) theo `railway.toml` (`builder = "DOCKERFILE"`, `healthcheckPath = "/health"`) | uvicorn 2 worker, lắng nghe `${PORT}` do Railway cấp |
| `Redis` | `railway add --database redis` (image `redis:8.2`, có volume) | Chỉ truy cập qua mạng nội bộ `redis.railway.internal:6379`; lưu state rate limit + cost guard |

Các bước đã làm:

```bash
cd 06-lab-complete
railway init --name day14-agent
railway add --database redis
railway add --service agent
railway service link agent
railway variables --service agent --set "AGENT_API_KEY=<random>" --set 'REDIS_URL=${{Redis.REDIS_URL}}' \
  --set ENVIRONMENT=production --set RATE_LIMIT_PER_MINUTE=10 --set DAILY_BUDGET_USD=1.0 --set MONTHLY_BUDGET_USD=10.0 --set LOG_LEVEL=INFO
railway up --detach --service agent
railway domain --service agent
```

`.railwayignore` loại `.env`, venv, `benchmark-observability/` khỏi bước upload (`.dockerignore` chỉ áp dụng lúc build image).

## Test Commands

Đặt `BASE` là URL public và `AGENT_API_KEY` là key đã đặt trên Railway (không ghi key vào repo). Mỗi lệnh chỉ in mã HTTP:

```bash
BASE=https://agent-production-bfae1a95.up.railway.app

curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/health"     # 200
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/ready"      # 200
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" -H 'Content-Type: application/json' -d '{"question":"Hello"}'   # 401
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" -H 'Content-Type: application/json' -H "X-API-Key: $AGENT_API_KEY" -d '{"question":"Hello"}'   # 200
```

Kết quả chạy ngày 2026-11-26:

```text
200
200
401
200
```

### Health / Readiness

```bash
$ curl $BASE/health
{"status":"ok","version":"1.0.0","uptime_s":30.8}

$ curl $BASE/ready
{"status":"ready","redis":"ok"}
```

### API Test (with authentication)

```bash
curl -X POST "$BASE/ask" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "question": "Hello"}'
```

### Rate limiting (10 req/phút / API key, state trong Redis)

```bash
for i in $(seq 1 12); do
  curl -sS -o /dev/null -w '%{http_code} ' -X POST "$BASE/ask" \
    -H 'Content-Type: application/json' -H "X-API-Key: $AGENT_API_KEY" -d '{"question":"test"}'
done
# 200 200 200 200 200 200 200 200 200 429 429 429
# (request thứ 10 trong cửa sổ 60s tính cả lần gọi /ask ở trên; response 429 có header retry-after: 44)
```

### Script chấm

```bash
python check_production_ready.py --public-url https://agent-production-bfae1a95.up.railway.app
#  ✅ Public URL: GET https://agent-production-bfae1a95.up.railway.app/health → 200
```

## Environment Variables Set

Đặt trên Railway (service `agent` → Variables), **không** ghi trong code hay repo:

| Biến | Giá trị |
|---|---|
| `AGENT_API_KEY` | chuỗi ngẫu nhiên 43 ký tự (`secrets.token_urlsafe(32)`), không công khai |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` — Railway tự thay bằng URL nội bộ `redis://default:***@redis.railway.internal:6379` |
| `ENVIRONMENT` | `production` |
| `RATE_LIMIT_PER_MINUTE` | `10` |
| `DAILY_BUDGET_USD` | `1.0` |
| `MONTHLY_BUDGET_USD` | `10.0` |
| `LOG_LEVEL` | `INFO` |
| `PORT` | do Railway tự cấp; `CMD` trong Dockerfile đọc `${PORT:-8000}` |

## Screenshots

- [Deployment dashboard](screenshots/dashboard.png)
- [Service running](screenshots/running.png)
- [Test results](screenshots/test.png)
