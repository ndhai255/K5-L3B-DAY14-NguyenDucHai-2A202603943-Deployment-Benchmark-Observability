# Day 14 Lab - Mission Answers

> Ghi chú môi trường: máy làm bài đã có process khác chiếm cổng 8000, 8080 và 8081, nên các app mẫu được chạy ở cổng khác (ghi rõ ở từng bài). Python 3.11 trong venv, Docker 29.1.3.

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns trong `01-localhost-vs-production/develop/app.py`

1. **Hardcode secret** (dòng 17–18): `OPENAI_API_KEY = "sk-hardcoded-..."` và `DATABASE_URL` chứa `admin:password123`. Push lên GitHub là lộ key; muốn đổi key phải sửa code và deploy lại.
2. **Log ra secret** (dòng 34): `print(f"[DEBUG] Using key: {OPENAI_API_KEY}")`. Khi chạy thật, log in đúng dòng `[DEBUG] Using key: sk-hardcoded-fake-key-never-do-this` — ai đọc được log là có key.
3. **`print()` thay vì logging có cấu trúc** (dòng 33, 38): không có level, timestamp, không parse được bằng log aggregator; còn in nguyên câu hỏi và câu trả lời của user.
4. **Config cứng trong code, không theo environment** (dòng 21–22): `DEBUG = True`, `MAX_TOKENS = 500` — không đổi được giữa dev/staging/production.
5. **Không có health check** (dòng 42): gọi `GET /health` trả **404**. Platform không biết agent còn sống để restart.
6. **Bind `localhost`** (dòng 51): trong container, `localhost` là chính container, nên bên ngoài không gọi vào được.
7. **Port cố định 8000** (dòng 52): Railway/Render cấp port qua biến `PORT`, app không đọc → deploy không lên.
8. **`reload=True`** (dòng 53): chế độ dev, tốn tài nguyên và không dùng cho production.
9. **Không xử lý shutdown / readiness**: không có lifespan, không có `/ready`.

### Exercise 1.2: Chạy bản develop

Chạy `uvicorn app:app --host localhost --port 8101` (cổng 8000 đang bận):

```text
$ curl -i -X POST "http://localhost:8101/ask?question=Hello"
HTTP/1.1 200 OK
{"answer":"Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé."}

GET /health                         → 404 (không có health check)
POST /ask với body JSON             → 422 (chỉ nhận question qua query)
```

Log server in ra secret: `[DEBUG] Using key: sk-hardcoded-fake-key-never-do-this`. App **chạy được nhưng không production-ready**.

### Exercise 1.3: So sánh develop vs production

Chạy bản production bằng `PORT=8102 python app.py` → `/ask` (body JSON) 200, `/health` 200 (`uptime_seconds`, `version`, `environment`), `/ready` 200 `{"ready":true}`. Gửi SIGTERM → uvicorn tắt êm, exit code 0.

| Feature | Develop | Production | Tại sao quan trọng? |
|---|---|---|---|
| Config | Hardcode trong `app.py` | `config.py` đọc env (`os.getenv`), `validate()` fail-fast nếu thiếu `AGENT_API_KEY` khi `ENVIRONMENT=production` | Cùng một image chạy được ở dev/staging/prod chỉ bằng đổi env; secret không nằm trong git; thiếu config quan trọng thì crash ngay lúc start thay vì lỗi lúc có user |
| Secret | Key nằm trong code và bị in ra log | Đọc từ env, log chỉ ghi `question_length`, không log secret | Log thường được gửi ra nhiều hệ thống (Loki, Datadog) và nhiều người đọc được |
| Health check | Không có (404) | `/health` (liveness) + `/ready` (readiness) | Platform restart container khi `/health` fail; load balancer chỉ gửi traffic khi `/ready` 200 → không route vào instance đang khởi động / đang tắt |
| Logging | `print()` | Logging có level, message dạng JSON | Parse, filter, search được theo field (event, status...) |
| Shutdown | Đột ngột | Lifespan: `is_ready=False` rồi dọn dẹp | Không cắt ngang request đang chạy khi deploy/scale down |
| Host / Port | `localhost:8000` cố định | `HOST=0.0.0.0`, `PORT` từ env | Container nhận được kết nối từ ngoài; platform tự cấp port |
| Reload | `reload=True` luôn bật | Chỉ khi `DEBUG=true` | Reload là tính năng dev, tốn CPU và có thể restart app bất ngờ |
| CORS | Không cấu hình | `ALLOWED_ORIGINS` từ env | Chỉ frontend được phép mới gọi được API từ trình duyệt |

**Hai điểm mình quan sát được khi chạy thật bản production:**
- **Không có dòng log JSON nào xuất hiện.** Nguyên nhân: `config.py` gọi `logging.warning("OPENAI_API_KEY not set...")` lúc import, *trước* khi `app.py` gọi `logging.basicConfig(...)`. Gọi `logging.warning` khi root logger chưa có handler sẽ tự `basicConfig()` với format mặc định và level WARNING → `basicConfig` của app không còn tác dụng, mọi log INFO bị bỏ. Bằng chứng: dòng duy nhất là `WARNING:root:OPENAI_API_KEY not set — using mock LLM` (format mặc định). Sửa: gọi `basicConfig(..., force=True)` hoặc cấu hình logging trước khi import config.
- **`handle_sigterm` không chạy trong lúc uvicorn đang phục vụ**: uvicorn 0.30 tự đăng ký handler SIGTERM khi `uvicorn.run()` bắt đầu (`capture_signals`), ghi đè handler mà app đăng ký lúc import. Graceful shutdown vẫn đúng vì chính uvicorn xử lý.

## Part 2: Docker

### Exercise 2.1: `02-docker/develop/Dockerfile`

1. **Base image:** `python:3.11` — bản Debian đầy đủ có Python 3.11 (kèm compiler, header, nhiều package hệ thống) nên rất nặng.
2. **Working directory:** `/app` — mọi lệnh `COPY`, `RUN`, `CMD` sau đó chạy tương đối từ thư mục này.
3. **Vì sao `COPY requirements.txt` trước:** Docker cache theo từng layer; một layer bị build lại thì mọi layer sau nó cũng build lại. Copy `requirements.txt` rồi `pip install` trước, copy code sau → sửa code chỉ invalidate layer `COPY app.py`, bước `pip install` (chậm nhất) dùng lại cache. Nếu `COPY . .` trước thì mỗi lần sửa một dòng code đều phải cài lại toàn bộ dependencies.
4. **CMD vs ENTRYPOINT:** `ENTRYPOINT` là chương trình chính của container, tham số `docker run <image> <args>` được *nối thêm* vào sau nó (muốn đổi phải dùng `--entrypoint`). `CMD` là lệnh/tham số mặc định, bị *thay thế* hoàn toàn khi truyền lệnh vào `docker run <image> <cmd>`. Hay dùng chung: `ENTRYPOINT ["uvicorn"]` + `CMD ["app:app", "--port", "8000"]`. Dùng exec form (`["..."]`) để process là PID 1 và nhận được SIGTERM.

### Exercise 2.2: Build và run

```text
$ docker build -f 02-docker/develop/Dockerfile -t my-agent:develop .
$ docker run -p 8201:8000 my-agent:develop        # cổng 8000 trên host đang bận
$ curl -X POST "http://localhost:8201/ask?question=What%20is%20Docker%3F"
{"answer":"Container là cách đóng gói app để chạy ở mọi nơi. Build once, run anywhere!"}
$ curl http://localhost:8201/health
{"status":"ok","uptime_seconds":2.8,"container":true}
```

### Exercise 2.3: Multi-stage build (`02-docker/production/Dockerfile`)

- **Stage 1 (`builder`)**: từ `python:3.11-slim`, cài `gcc`, `libpq-dev` để build được package có phần C, rồi `pip install --user` dependencies vào `/root/.local`. Stage này không được deploy.
- **Stage 2 (`runtime`)**: lại từ `python:3.11-slim` sạch, tạo user non-root `appuser`, chỉ `COPY --from=builder /root/.local` (package đã cài) + `main.py` + `utils/`. Có `HEALTHCHECK`, chạy `uvicorn --workers 2` bằng user `appuser`.
- **Vì sao nhỏ hơn:** image cuối không chứa compiler, header, apt cache, pip cache của stage builder; base là `slim` thay vì bản full.

Kích thước đo bằng `docker images my-agent`:

| Image | DISK USAGE | CONTENT SIZE |
|---|---:|---:|
| `my-agent:develop` (`python:3.11`, single-stage) | 1.67 GB | 426 MB |
| `my-agent:advanced` (`python:3.11-slim`, multi-stage) | 240 MB | 57.4 MB |
| **Chênh lệch** | **giảm ~85,6%** | **giảm ~86,5%** |

(`DISK USAGE` là dung lượng giải nén trên đĩa; `CONTENT SIZE` là dung lượng nén của các layer — theo `docker image inspect`: 426 320 101 B vs 57 408 423 B.)

### Exercise 2.4: Docker Compose stack

```text
                     host :80 / :443
                            │
                   ┌────────▼────────┐
  Client ────────▶ │ nginx (alpine)  │  reverse proxy, rate limit 10 r/s/IP,
                   │                 │  security headers
                   └────────┬────────┘
                            │ proxy_pass http://agent:8000
              network "internal" (bridge)
                   ┌────────▼────────┐
                   │ agent (FastAPI) │  không expose port ra host
                   └───┬─────────┬───┘
       REDIS_URL       │         │      QDRANT_URL
   redis://redis:6379  │         │  http://qdrant:6333
              ┌────────▼───┐ ┌───▼──────────┐
              │ redis:7    │ │ qdrant v1.9  │
              │ (volume)   │ │ (volume)     │
              └────────────┘ └──────────────┘
```

- **4 service** được start: `redis`, `qdrant` (start trước, phải `healthy`), rồi `agent` (`depends_on: condition: service_healthy`), cuối cùng `nginx`.
- **Giao tiếp:** tất cả trong network `internal`; gọi nhau bằng tên service (Docker DNS: `agent`, `redis`, `qdrant`). Chỉ `nginx` mở port ra host; client không gọi thẳng agent được.
- Quan sát thêm: `main.py` của agent không dùng Redis/Qdrant (chỉ nhận env), nên trong bài này 2 service đó mới là "khung" cho các bài sau.

Kết quả test qua Nginx (`docker compose -p k5-day14-docker up -d --build`):

```text
$ curl -i http://localhost/health
HTTP/1.1 200 OK
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
{"status":"ok","uptime_seconds":11.4,"version":"2.0.0","timestamp":"2026-11-26T09:45:50.834245"}

$ curl -X POST http://localhost/ask -H "Content-Type: application/json" -d '{"question": "Explain microservices"}'
{"answer":"Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic."}
```

`docker compose ps`: `agent (healthy)`, `redis (healthy)`, `qdrant (healthy)`, `nginx 0.0.0.0:80->80, 443->443`.

## Part 3: Cloud Deployment

### Exercise 3.1 / 3.2: Deploy agent Part 6

Nền tảng chính: **Railway** (Exercise 3.1). Deploy chính agent Part 6 (`06-lab-complete/`) kèm một Redis trên Railway; chi tiết trong [DEPLOYMENT.md](DEPLOYMENT.md).

- URL: https://agent-production-bfae1a95.up.railway.app
- Kết quả 4 lệnh tự kiểm ở CP6: `/health` **200**, `/ready` **200** (`{"status":"ready","redis":"ok"}`), `/ask` không key **401**, `/ask` có key **200**. Rate limit trên cloud: request thứ 10 trong 60 s trả **429** (`retry-after: 44`).
- Biến môi trường đặt trên Railway: `AGENT_API_KEY` (ngẫu nhiên, không ghi trong repo), `REDIS_URL=${{Redis.REDIS_URL}}`, `ENVIRONMENT=production`, `RATE_LIMIT_PER_MINUTE=10`, `DAILY_BUDGET_USD=1.0`, `MONTHLY_BUDGET_USD=10.0`, `LOG_LEVEL=INFO`.
- Screenshot: `screenshots/dashboard.png`, `screenshots/running.png`, `screenshots/test.png`
- Ghi chú: Railway CLI 5.75 cảnh báo `railway.toml` (Config as Code) đã bị deprecate, còn chạy tới 2026-12-01; bản mới khuyến nghị Infrastructure as Code (`.railway/railway.ts`). Bài vẫn giữ `railway.toml` vì script chấm yêu cầu.

### So sánh `railway.toml` với `render.yaml`

| | `railway.toml` | `render.yaml` |
|---|---|---|
| Định dạng / phạm vi | TOML, cấu hình **một service** (build + deploy) | YAML Blueprint, khai báo **cả hạ tầng**: nhiều service (web + Redis/Key Value) trong một file |
| Build | `[build] builder = "NIXPACKS"` (tự nhận Python) hoặc `"DOCKERFILE"` | `runtime: python` + `buildCommand: pip install ...`, hoặc `runtime: docker` |
| Start | `startCommand = "uvicorn ... --port $PORT"` | `startCommand: uvicorn ... --port $PORT` |
| Health check | `healthcheckPath = "/health"`, `healthcheckTimeout` | `healthCheckPath: /health` |
| Restart | `restartPolicyType = "ON_FAILURE"`, `restartPolicyMaxRetries = 3` | Render tự quản lý, không cấu hình trong file |
| Biến môi trường / secret | **Không** khai báo trong file — đặt bằng `railway variables set` hoặc dashboard | Khai báo trong `envVars`: giá trị thường ghi thẳng, secret dùng `sync: false` (điền tay trên dashboard) hoặc `generateValue: true` (Render tự sinh) |
| Redis | Thêm Redis bằng dashboard, tham chiếu `${{Redis.REDIS_URL}}` | Khai báo luôn service `type: redis` (Render mới gọi là `keyvalue`), lấy URL bằng `fromService` |
| Region / plan / auto deploy | Chọn trên dashboard | Ghi trong file: `region: singapore`, `plan: free`, `autoDeploy: true` |

**Vì sao khác:** Railway thiên về "deploy nhanh từ CLI" — file chỉ mô tả cách build/chạy một service, còn biến môi trường và các service khác quản lý trên platform. Render dùng Blueprint kiểu Infrastructure-as-Code: commit một file là tạo lại được cả stack, nên phải có cú pháp riêng (`sync: false`, `generateValue`) để khai báo secret mà không ghi giá trị vào git.

Trong `06-lab-complete/` mình dùng cả hai: `railway.toml` với `builder = "DOCKERFILE"` (dùng lại Dockerfile multi-stage, port đọc `${PORT:-8000}` trong CMD), `render.yaml` với `runtime: docker` + Key Value Redis + `AGENT_API_KEY: generateValue: true`.

### Exercise 3.3: Pipeline Cloud Run (`cloudbuild.yaml` + `service.yaml`)

- Cloud Build chạy 4 bước nối tiếp nhau (`waitFor`): **test** (`pytest tests/` trong `python:3.11-slim`) → **build** image Docker, gắn 2 tag `:$COMMIT_SHA` và `:latest`, dùng `--cache-from :latest` để tái dùng layer → **push** lên Container Registry `gcr.io/$PROJECT_ID/ai-agent` → **deploy** bằng `gcloud run deploy`. Test fail thì dừng luôn, không build và không deploy.
- Deploy dùng đúng tag `:$COMMIT_SHA` (không phải `latest`), nên mỗi bản chạy trên Cloud Run truy ngược được về một commit và rollback được. Region `asia-southeast1`, public (`--allow-unauthenticated`), 1–10 instance, 512 Mi / 1 CPU, timeout 60 s. `OPENAI_API_KEY` lấy từ Secret Manager (`--set-secrets`), không ghi trong pipeline.
- `service.yaml` khai báo cùng service đó theo kiểu IaC (Knative): `minScale: 1` để tránh cold start, `maxScale: 10`, `containerConcurrency: 80`, cả 2 secret lấy qua `secretKeyRef`, `livenessProbe` gọi `/health`, `startupProbe` gọi `/ready`.
- Ghi chú: thư mục `production-cloud-run/` không có `tests/`, `requirements.txt` hay `Dockerfile`, nên nếu chạy y nguyên thì pipeline sẽ dừng ngay ở bước test. Đây là mẫu cấu hình, phải đặt vào repo có code thì mới chạy được.

## Part 4: API Security

Bản develop chạy bằng `AGENT_API_KEY=secret-key-123 uvicorn app:app --port 8401`; bản production chạy `uvicorn app:app --port 8402` (cổng 8000 đang bận).

### Exercise 4.1: API key (`04-api-gateway/develop/app.py`)

- **Key được check ở đâu:** hàm dependency `verify_api_key()` (dòng 39–54), đọc header bằng `APIKeyHeader(name="X-API-Key", auto_error=False)`, gắn vào `/ask` bằng `Depends(verify_api_key)`. `/` và `/health` không gắn dependency nên vẫn public.
- **Sai key:** thiếu header → **401**; có header nhưng sai → **403** `"Invalid API key."`. (Trong Part 6 mình trả 401 cho cả hai trường hợp theo đúng yêu cầu.)
- **Rotate key:** key đọc từ env `AGENT_API_KEY` → đổi giá trị env trên platform rồi restart/redeploy, không phải sửa code. Muốn không có downtime thì cho phép 2 key cùng lúc (`AGENT_API_KEYS=old,new`), báo client chuyển sang key mới, rồi gỡ key cũ. Lưu ý: mặc định `"demo-key-change-in-production"` khi thiếu env là rủi ro — quên đặt env thì key mặc định (đã công khai trong code) vẫn dùng được.

```text
$ curl -i -X POST http://localhost:8401/ask -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 401 Unauthorized
{"detail":"Missing API key. Include header: X-API-Key: <your-key>"}

$ curl -i -X POST http://localhost:8401/ask -H "X-API-Key: wrong" -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 403 Forbidden
{"detail":"Invalid API key."}

$ curl -i -X POST http://localhost:8401/ask -H "X-API-Key: secret-key-123" -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 200 OK
{"question":"Hello","answer":"Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận."}
```

### Exercise 4.2: JWT (`04-api-gateway/production/auth.py`)

**Flow:** `POST /auth/token` với username/password → `authenticate_user()` kiểm tra `DEMO_USERS` → `create_token()` ký JWT HS256 bằng `JWT_SECRET`, payload `{sub, role, iat, exp = +60 phút}` → client gửi `Authorization: Bearer <token>` → `verify_token()` (`HTTPBearer`) gọi `jwt.decode` kiểm tra chữ ký và hạn → trả `{username, role}` cho endpoint. Server không phải tra DB mỗi request (stateless), nhưng cũng không thu hồi được token trước khi hết hạn.

```text
$ curl -X POST http://localhost:8402/auth/token -H "Content-Type: application/json" -d '{"username": "student", "password": "demo123"}'
{"access_token":"<TOKEN-REDACTED>","token_type":"bearer","expires_in_minutes":60,"hint":"Include in header: Authorization: Bearer <REDACTED>..."}

Sai mật khẩu                 → 401 {"detail":"Invalid credentials"}
/ask không có token          → 401 {"detail":"Authentication required. Include: Authorization: Bearer <token>"}
/ask token giả "abc.def.ghi" → 403 {"detail":"Invalid token."}

$ curl -i -X POST http://localhost:8402/ask -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"question": "Explain JWT"}'
HTTP/1.1 200 OK
x-content-type-options: nosniff
x-frame-options: DENY
{"question":"Explain JWT","answer":"Đây là câu trả lời từ AI agent (mock). ...","usage":{"requests_remaining":9,"budget_remaining_usd":2.1e-05}}
```

Bug nhỏ thấy được: `budget_remaining_usd` trong response `/ask` thực ra là `usage.total_cost_usd` (số tiền **đã dùng**), `/me/usage` mới trả đúng số còn lại (`0.999821`).

### Exercise 4.3: Rate limiting (`04-api-gateway/production/rate_limiter.py`)

- **Algorithm:** sliding window log — mỗi user một `deque` timestamp; mỗi request bỏ các timestamp cũ hơn 60 s rồi đếm. (Không phải token bucket.)
- **Limit:** user thường `rate_limiter_user = 10 req / 60 s`; admin `rate_limiter_admin = 100 req / 60 s`.
- **Bypass cho admin:** không bypass hẳn — `app.py` dòng 141 chọn `rate_limiter_admin` khi `role == "admin"` (tài khoản `teacher`), tức là hạn mức cao hơn. Muốn bypass hoàn toàn thì bỏ qua `limiter.check()` khi `role == "admin"`.
- **Hạn chế:** state nằm trong RAM của process → chạy nhiều worker/instance thì mỗi cái đếm riêng, restart là mất. Part 6 chuyển sang Redis sorted set.

Vòng lặp 20 request với token của `student` (request `/ask` ở Ex 4.2 đã tính là request thứ nhất):

```text
req  1: 200 ... "requests_remaining":8
req  2: 200 ... "requests_remaining":7
...
req  9: 200 ... (request thứ 10 trong cửa sổ 60 s)
req 10: 429 {"detail":{"error":"Rate limit exceeded","limit":10,"window_seconds":60,"retry_after_seconds":59}}
...
req 20: 429

$ curl -i ... (request tiếp theo)
HTTP/1.1 429 Too Many Requests
x-ratelimit-limit: 10
x-ratelimit-remaining: 0
x-ratelimit-reset: 1795686444
retry-after: 59
```

### Exercise 4.4: Cost guard

Cài đặt (chạy thử với Redis thật trong container `redis:7-alpine`):

```python
from datetime import datetime, timezone
import redis

MONTHLY_BUDGET_USD = 10.0
r = redis.from_url(REDIS_URL, decode_responses=True)

# Kiểm tra + cộng trong MỘT lệnh Lua (nguyên tử) → 2 instance gọi cùng lúc không vượt budget
_RESERVE = r.register_script("""
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local cost = tonumber(ARGV[1])
if current + cost > tonumber(ARGV[2]) then return 0 end
redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
""")

def check_budget(user_id: str, estimated_cost: float) -> bool:
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    key = f"budget:{user_id}:{month_key}"
    return bool(_RESERVE(keys=[key], args=[estimated_cost, MONTHLY_BUDGET_USD, 32 * 24 * 3600]))
```

**Cách làm:**
- Mỗi user một key theo tháng `budget:<user>:<YYYY-MM>`. Sang tháng mới thì key mới bắt đầu từ 0, tức là tự "reset đầu tháng" mà không cần cron. TTL 32 ngày để key cũ tự xoá.
- State nằm trong Redis nên mọi instance dùng chung, restart không mất.
- Khác bản solution trong CODE_LAB (`GET` rồi `INCRBYFLOAT` là 2 lệnh riêng): bản đó có race condition. Hai request song song cùng đọc `$9.5` thì cả hai đều thấy "còn budget" và cộng vào, tổng thành `$10.5`. Đưa so sánh và cộng vào một script Lua thì Redis chạy nó nguyên tử.
- Dùng múi giờ UTC để mọi instance cùng hiểu một "tháng", không phụ thuộc timezone của server.

Kết quả chạy thử:

```text
4 lần × $3.0 → [True, True, True, False]     ($9 + $3 > $10 → chặn)
thêm $1.0  → True                            ($9 + $1 = $10, vẫn trong budget)
thêm $0.01 → False
đã dùng: 10 USD · TTL: 2764800 s · key: budget:test-user:2026-11
```

**Rate limit và cost guard, vì sao cần cả hai:** rate limit chặn *tốc độ* (spam, DoS, một client chiếm hết tài nguyên). Cost guard chặn *tổng tiền*, vì 10 req/phút × 60 × 24 vẫn là 14 400 request/ngày, đủ làm cháy budget LLM nếu mỗi request dài hay tốn nhiều token.

## Part 5: Scaling & Reliability

### Exercise 5.1: Health checks (`05-scaling-reliability/develop/app.py`)

Bản develop đã có đủ 2 endpoint; mình chạy `PORT=8501 python app.py` để kiểm chứng:

```text
GET /health → 200 {"status":"ok","uptime_seconds":2.7,"version":"1.0.0","environment":"development",
                   "timestamp":"...","checks":{"memory":{"status":"ok","used_percent":86.4}}}
GET /ready  → 200 {"ready":true,"in_flight_requests":1}
```

- **Liveness (`/health`)**: process còn sống thì trả 200; nếu fail thì platform **restart** container. Không nên phụ thuộc Redis/DB, nếu không thì Redis chết sẽ kéo theo mọi container bị restart liên tục mà không sửa được gì.
- **Readiness (`/ready`)**: trả 503 khi đang khởi động, đang tắt, hoặc khi dependency chưa sẵn sàng. Lúc đó load balancer **ngừng gửi traffic** vào instance này nhưng không restart nó.
- `in_flight_requests: 1` là vì middleware đếm luôn chính request `/ready` đang chạy.

### Exercise 5.2: Graceful shutdown

Test: gửi `POST /ask` rồi 30 ms sau `kill -TERM <pid>`:

```text
in-flight request → 200 {"answer":"Agent đang hoạt động tốt! (mock response) ..."}
agent exit code   → 0

INFO:     Shutting down
INFO:     127.0.0.1:33432 - "POST /ask?question=Long%20task HTTP/1.1" 200 OK   ← request vẫn được trả xong
INFO:     Waiting for application shutdown.
INFO 🔄 Graceful shutdown initiated...
INFO ✅ Shutdown complete
INFO:     Finished server process [706919]
INFO Received signal 15 — uvicorn will handle graceful shutdown
```

- Trình tự khi nhận SIGTERM: uvicorn ngừng nhận kết nối mới → chờ request đang chạy xong (tối đa `timeout_graceful_shutdown=30`) → chạy phần shutdown của lifespan (`_is_ready=False`, chờ `_in_flight_requests` về 0, đóng kết nối) → thoát với exit code 0.
- Quan sát: dòng `Received signal 15` của `handle_sigterm` in **sau cùng**. Lý do: trong lúc phục vụ, uvicorn tạm thay handler SIGTERM bằng handler của nó (`capture_signals`), tắt xong mới trả lại handler cũ rồi phát lại signal. Vì vậy trong Part 6, nếu muốn làm gì lúc nhận SIGTERM thì mình bọc handler của uvicorn *bên trong lifespan* và gọi lại handler cũ, không đăng ký lúc import.

### Exercise 5.3: Stateless design (`05-scaling-reliability/production/app.py`)

Bản production đã stateless: conversation history lưu ở Redis key `session:<id>` (`SETEX`, TTL 1 giờ, giữ tối đa 20 message), không có dict trong RAM. Instance nào cũng đọc được session của user nên load balancer gửi request đi đâu cũng được.

```python
# ❌ Stateful: mỗi instance có dict riêng → request 2 rơi vào instance khác là mất history
conversation_history = {}

# ✅ Stateless: mọi instance đọc/ghi cùng một Redis
def append_to_history(session_id, role, content):
    session = load_session(session_id)               # _redis.get(f"session:{session_id}")
    session.setdefault("history", []).append({...})
    save_session(session_id, session)                # _redis.setex(..., 3600, json.dumps(session))
```

Còn 2 điểm chưa thật stateless/an toàn:
1. Nếu lúc import không ping được Redis thì app **lặng lẽ fallback về `_memory_store` trong RAM**, và vẫn báo `/health` "ok". Khi scale thì đây chính là anti-pattern; production nên fail (hoặc `/ready` 503) thay vì fallback.
2. `append_to_history` là đọc → sửa → ghi cả JSON, nên 2 request cùng session chạy song song có thể ghi đè mất message của nhau. Dùng `RPUSH` + `LTRIM` trên Redis list (thao tác nguyên tử) sẽ an toàn hơn.

### Exercise 5.4: Load balancing

Chạy `docker compose -p k5-day14-scaling up -d --build --scale agent=3`. Cổng 8080 trên máy đã bị container khác chiếm (cả 8081 cũng bận), nên mình dùng một file override tạm, chỉ đổi port của Nginx thành `18080:80`; mọi thứ khác giữ nguyên.

```text
k5-day14-scaling-agent-1   Up (healthy)   8000/tcp
k5-day14-scaling-agent-2   Up (healthy)   8000/tcp
k5-day14-scaling-agent-3   Up (healthy)   8000/tcp
k5-day14-scaling-nginx-1   Up             0.0.0.0:18080->80/tcp
k5-day14-scaling-redis-1   Up (healthy)   6379/tcp

10 request POST /chat:
Request 1 -> instance-b7d86f | redis      Request 6  -> instance-223af8 | redis
Request 2 -> instance-e203b0 | redis      Request 7  -> instance-b7d86f | redis
Request 3 -> instance-223af8 | redis      Request 8  -> instance-e203b0 | redis
Request 4 -> instance-b7d86f | redis      Request 9  -> instance-223af8 | redis
Request 5 -> instance-e203b0 | redis      Request 10 -> instance-b7d86f | redis
```

Nginx `upstream agent_cluster { server agent:8000; }`: tên `agent` được Docker DNS phân giải ra 3 IP, Nginx chia **round-robin** đúng thứ tự b7d86f → e203b0 → 223af8.

### Exercise 5.5: Test stateless

`python test_stateless.py` (bản sao trỏ tới `localhost:18080`):

```text
Request 1: [instance-e203b0]  Q: What is Docker?
Request 2: [instance-223af8]  Q: Why do we need containers?
Request 3: [instance-b7d86f]  Q: What is Kubernetes?
Request 4: [instance-e203b0]  Q: How does load balancing work?
Request 5: [instance-223af8]  Q: What is Redis used for?
------------------------------------------------------------
Total requests: 5
Instances used: {'instance-223af8', 'instance-b7d86f', 'instance-e203b0'}
✅ All requests served despite different instances!

--- Conversation History ---
Total messages: 10
✅ Session history preserved across all instances via Redis!
```

**Giải thích:** 5 câu của cùng một session đi qua cả 3 instance khác nhau, nhưng history vẫn đủ 10 message (5 user + 5 assistant), vì không instance nào giữ history trong RAM; tất cả đọc/ghi chung Redis.

**Thử thêm, kill 1 instance giữa chừng** (script gốc không tự kill): gửi 3 lượt, `docker kill k5-day14-scaling-agent-2`, gửi tiếp 6 lượt trong cùng session:

```text
[200] served_by=instance-e203b0
[200] served_by=instance-223af8
[200] served_by=instance-b7d86f
>>> docker kill k5-day14-scaling-agent-2
[200] served_by=instance-e203b0
[200] served_by=instance-b7d86f
[200] served_by=instance-e203b0      ← chỉ còn 2 instance phục vụ, không request nào lỗi
[200] served_by=instance-b7d86f
[200] served_by=instance-e203b0
[200] served_by=instance-b7d86f
history: 18 messages; users: ['Turn 1', ..., 'Turn 9']       ← không mất lượt nào

nginx: [error] upstream timed out (110: Operation timed out) while connecting to upstream,
       upstream: "http://172.25.0.5:8000/chat"
```

Nginx thử gửi vào IP của instance đã chết, bị timeout, rồi nhờ `proxy_next_upstream error timeout http_503` chuyển request sang instance khác. Client vẫn nhận 200 và session vẫn liên tục.

Bug nhỏ thấy được: trường `turn` bị lệch 1 (request đầu tiên của session đã báo `turn=2`), vì code đếm số câu user trong history *sau khi* đã append câu hiện tại rồi còn `+ 1`.

## Part 6: Final Project

Code trong `06-lab-complete/`. Output `python check_production_ready.py --public-url https://agent-production-bfae1a95.up.railway.app` (chạy đầy đủ, có Docker, sau khi deploy Railway, ngày 2026-11-26):

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
  Result: 12/12 tiêu chí đạt · 44/44 checks passed
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
| 2 | Part 1, 2, 3 (so sánh config + Cloud Run), 4, 5 | Dùng Claude Code chạy các ví dụ mẫu, đo số liệu và viết câu trả lời. | AI chạy từng app/stack, ghi lại output thật (mã HTTP, kích thước image từ `docker images`, log), viết câu trả lời và chỉ ra các điểm lạ khi chạy (log JSON bị mất ở Part 1, handler SIGTERM bị uvicorn ghi đè, `budget_remaining_usd` sai, `turn` lệch 1). Ex 4.4 viết bản dùng Lua để tránh race condition và chạy thử với Redis thật. | Mọi số liệu lấy từ lệnh đã chạy trên máy ngày 2026-11-26. Do cổng 8000/8080/8081 bận nên chạy ở cổng khác (đã ghi rõ). _(Bổ sung: phần bạn tự chạy lại / tự kiểm tra)_ |
| 3 | Part 7 (CP7 + CP8), `BENCHMARK_REPORT.md` | Dùng Claude Code chạy load test, eval, sửa `KNOWLEDGE_BASE`, viết job CI, chạy stack Prometheus/Grafana/Jaeger/Locust, làm thí nghiệm alert và viết báo cáo. | AI chạy toàn bộ lệnh, lấy số từ file JSON/CSV và Prometheus/Jaeger API, chụp ảnh bằng Chrome headless. AI phát hiện và sửa bug `PYTHONUSERBASE` trong `production/Dockerfile`, phát hiện ngưỡng cost alert $2/giờ đã firing ở tải 30 user (nên chạy thí nghiệm C ở 10 user), và tìm ra nút thắt ở c=100 là load generator. Lần đầu, eval v2 bị chạy *sau* khi KB đã được sửa (do phiên làm việc bị ngắt), nên đã trả KB về bản gốc và chạy lại đúng thứ tự. | Lần chạy C đầu tiên bị loại vì máy hết RAM (Prometheus mất mẫu), chạy lại khi máy ổn định. Mọi số trong báo cáo được đối chiếu với `reports/*.json` và `benchmark-results/*.csv`. _(Bổ sung: phần bạn tự kiểm tra lại)_ |
