# Benchmark & Observability Report — Day 14

> Mọi con số dưới đây lấy từ file đã nộp: `reports/*.json` (load test, eval), `benchmark-results/*.csv` (Locust), Prometheus / Jaeger API và log của agent trong lần chạy ngày 2026-11-26.

**Họ tên:** Nguyen Duc Hai
**Ngày chạy:** 2026-11-26
**Môi trường:** Laptop Linux (Intel Core i5-12500H, 16 luồng CPU, 14 GB RAM), Docker 29.1.3, Python 3.11. Load test chạy trên cùng máy với agent. Mỗi container agent bị giới hạn `cpus: 0.5`, `memory: 256M` (theo `docker-compose.yml`).

> Ghi chú: cổng 8080 trên máy đã bị một container khác chiếm, nên Nginx của stack production được map ra `18080` bằng một file compose override (chỉ đổi port, mọi thứ khác giữ nguyên). Locust gọi `http://nginx` trong mạng nội bộ nên không bị ảnh hưởng.

---

## 1. SLO của agent

| SLI | SLO | Lý do chọn ngưỡng này |
|---|---|---|
| p95 latency `/ask` | < 500 ms | Mock LLM mất ~150–250 ms mỗi call (p95 đo được ở tải vừa là ~220 ms), nên 500 ms còn ~2× dư địa cho mạng và retrieval. Với chatbot, người dùng bắt đầu thấy "chậm" quanh 0,5–1 s. Đây cũng là ngưỡng mặc định của `load_test.py`; alert production đặt lỏng hơn (p95 > 1 s trong 2 phút) để tránh báo động vì spike ngắn |
| Error rate `/ask` | < 2 % | Upstream LLM tự lỗi ~1 % (`MOCK_ERROR_RATE=0.01`), nên SLO phải lớn hơn mức nền này; 2 % chừa error budget cho deploy và sự cố nhỏ. Alert đặt ở 5 % trong 1 phút (lỏng hơn SLO) |
| Accuracy trên golden dataset | ≥ 85 % (CI), mục tiêu 100 % | 23 câu thì mỗi câu ≈ 4,3 điểm %, 85 % nghĩa là sai tối đa 3 câu. CI chặn merge khi < 85 % **hoặc** có câu bị regression so với baseline; sau khi sửa KB, v2 đạt 23/23 nên giữ 100 % làm mục tiêu |

---

## 2. Benchmark hiệu năng (develop, `load_test.py`)

### 2.1 Concurrency vs latency (agent v2)

`python load_test.py --url http://localhost:8002 --requests 300 --concurrency <c> --out reports/load_v2_c<c>.json`

| Concurrency | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | SLO |
|---|---|---|---|---|---|---|
| 1 | 4.26 | 190.9 | 220.7 | 1702.2 | 0.67 % | ✅ PASS |
| 10 | 38.71 | 188.9 | 219.2 | 1692.2 | 1.67 % | ✅ PASS |
| 50 | 106.73 | 192.0 | 233.5 | 1700.7 | 0.67 % | ✅ PASS |
| 100 | 66.77 | 311.4 | 2669.4 | 3179.4 | 0.33 % | ❌ FAIL (p95) |

**Nhận xét:** RPS **không** tăng tuyến tính, và còn giảm khi lên 100.

- **Từ 1 tới 10:** RPS tăng gần tuyến tính (4,26 → 38,71, tức ×9,1). Agent dùng `async` (`asyncio.sleep` cho LLM call) nên trong lúc chờ LLM, event loop vẫn phục vụ được request khác. p50/p95 gần như không đổi (~190/220 ms). Đúng định luật Little: RPS ≈ concurrency / latency, với c=1 thì 1 / 0,2355 s ≈ 4,2.
- **Ở 50:** RPS chỉ ×2,8 so với 10, chứ không phải ×5. Một lần chạy 300 request ở c=50 chỉ kéo dài 2,8 s, nên phần khởi động và phần "đuôi" (vài worker cuối chờ request chậm 1,7 s) chiếm tỉ trọng lớn. p95 vẫn đạt SLO.
- **Ở 100:** RPS giảm còn 66,77 và p95 nhảy lên 2,7 s. Mình đo thêm (lần đo thăm dò, không nằm trong file nộp) để tìm nguyên nhân:
  - Agent chỉ dùng ~8 % một core CPU, và latency phía server (field `ms` trong log JSON của agent) cao nhất chỉ ~1,7 s, trong khi client thấy tới 6 s.
  - Process `load_test.py` thì dùng 86 % CPU ở c=100, so với 25 % ở c=50, mà lại đạt RPS thấp hơn.

  → Nút thắt nằm ở **công cụ tạo tải**: một event loop Python phải quản lý pool 100 kết nối `httpx` trên cùng máy, chứ không phải ở agent. Muốn đo agent ở mức concurrency này phải tách load generator ra máy hoặc process riêng (Locust với nhiều worker, `k6`, `wrk`...).

### 2.2 Tail latency

Khởi động lại v2 với từng giá trị `MOCK_SLOW_RATE`, chạy `--requests 300 --concurrency 20` (`reports/load_v2_slow_<rate>.json`):

| `MOCK_SLOW_RATE` | avg (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| 0 | 190.5 | 220.8 | 233.6 |
| 0.03 (mặc định) | 238.5 | 221.8 | 1682.5 |
| 0.1 | 403.4 | 1698.5 | 1712.9 |

**Nhận xét:**

- **p99 nhạy nhất.** Khi 3 % request bị chậm thêm 1,5 s, p99 nhảy từ 234 lên 1683 ms (×7,2), trong khi avg chỉ tăng 25 % (190 → 239 ms) và p95 gần như không đổi (221 → 222 ms), vì 3 % < 5 % nên request chậm chưa chạm tới p95. Lên 10 % thì request chậm vượt qua mốc 5 %, nên p95 cũng nhảy lên 1,7 s (×7,7).
- **Average che giấu tail latency.** Ở mức 0.03, avg 238 ms trông hoàn toàn ổn, nhưng cứ 100 người thì có 1 người chờ ~1,7 s. Average trộn 97 request nhanh với 3 request chậm thành một số "trung bình" mà không người dùng nào thực sự trải qua.
- **Vì vậy SLO đặt theo p95/p99**, tức là cam kết về trải nghiệm của phần lớn người dùng, kể cả nhóm chịu thiệt nhất. Average chỉ dùng để ước lượng tổng tài nguyên.
- Ghi chú thêm: RPS giảm từ 101 xuống 42 req/s dù agent không quá tải. Concurrency cố định 20 là tải vòng kín, nên latency tăng thì RPS giảm theo định luật Little. Đây là một lý do nữa để không đánh giá hệ thống chỉ bằng RPS.

---

## 3. Benchmark chất lượng (develop, `eval/run_eval.py`)

| Chỉ số | Agent v1 | Agent v2 | Agent v2 (sau khi sửa KB) |
|---|---|---|---|
| Accuracy | 4.3 % (1/23) | 91.3 % (21/23) | 100.0 % (23/23) |
| Avg tokens / câu | 56.2 | 109.0 | 116.9 |
| Tổng cost (USD) | 0.000391 | 0.000760 | 0.000818 |
| Cost / câu đúng (USD) | 0.00039135 | 0.00003619 | 0.00003557 |
| Quality gate | ❌ FAIL (accuracy < 80 %) | ✅ PASS (≥ 80 %, 0 regression so với v1) | ✅ PASS (`--min-accuracy 1.0`, 0 regression so với v2) |

**Những câu v2 trả lời sai ban đầu và cách bạn sửa:**

| ID | Keyword bị thiếu | Bạn đã sửa gì |
|---|---|---|
| `scaling-03` ("Kubernetes HPA scale dựa trên những chỉ số nào?") | `cpu`, `memory`, `custom metrics` | KB không có tài liệu nào khớp nên v2 trả câu chung chung. Thêm entry kích hoạt bởi `hpa`, `horizontal pod autoscaler`, `autoscal`, `kubernetes`: *"Kubernetes HPA (Horizontal Pod Autoscaler) tự tăng/giảm số pod theo chỉ số đo được: mức dùng CPU, memory, hoặc custom metrics từ ứng dụng như RPS, độ dài hàng đợi…"* |
| `security-02` ("JWT khác gì API key trong xác thực?") | `token`, `hết hạn`, `chữ ký` | Thêm entry kích hoạt bởi `jwt`, `json web token`: so sánh API key (chuỗi bí mật cố định, dùng tới khi bị thu hồi) với JWT (token do server ký, chứa user/role/`exp`; server chỉ kiểm tra chữ ký, không tra DB; tự hết hạn) |

- Chỉ sửa `KNOWLEDGE_BASE` trong `develop/utils/mock_llm.py` (đã chép sang `production/utils/mock_llm.py`), không đụng `golden_dataset.jsonl`.
- Trigger keyword được chọn để không "cướp" câu khác: không câu nào khác trong dataset chứa `hpa`/`kubernetes`/`jwt`. Kết quả eval cuối xác nhận **0 regression**.
- Lệnh: `python eval/run_eval.py --url http://localhost:8002 --min-accuracy 1.0 --baseline reports/eval_v2.json --out reports/eval_v2_fixed.json` → `Quality gate ... ✅ PASS`.

**Quality gate trong CI** (`.github/workflows/quality-gate.yml`): job chạy khi có PR đụng tới `benchmark-observability/`. Job bật agent v2 với `MOCK_ERROR_RATE=0` (để gate không fail vì lỗi ngẫu nhiên), rồi chạy `run_eval.py --min-accuracy 0.85 --baseline reports/eval_v2_fixed.json`. `run_eval.py` trả **exit code 1** khi accuracy < ngưỡng hoặc có regression, nên step fail, job fail, và PR không merge được khi bật branch protection "Require status checks". Đã thử trên máy: eval v1 với `--baseline reports/eval_v2.json` ra **exit code 1** (20 câu regression); chạy các bước của job CI với v2 đã sửa ra exit code 0.

**Nhận xét về trade-off chất lượng / chi phí / latency giữa v1 và v2:**

- **Chi phí mỗi câu:** v2 tốn gấp ~1,9–2,1 lần token (109–117 so với 56) và gấp 2,1 lần tổng cost. Lý do là retrieval nhét tài liệu vào prompt, làm input dài hơn.
- **Chi phí mỗi câu đúng:** v2 rẻ hơn **~10,8 lần** (0,0000362 so với 0,000391 USD), vì v1 gần như không trả lời đúng câu nào. "Cost / câu" khiến v1 trông rẻ hơn, nhưng "cost / câu đúng" mới phản ánh giá trị thật người dùng nhận được, nên dùng chỉ số này để so 2 phiên bản.
- **Latency:** v2 chậm hơn một chút (p50 158 → ~200 ms) vì có thêm bước retrieval ~20 ms và prompt dài hơn (latency mock tăng theo số input token).
- p95 của eval v2 ban đầu (1546,9 ms) **không** nói lên v2 chậm. Với 23 mẫu, chỉ cần 1–2 request rơi vào nhóm "slow" 3 % là p95 nhảy vọt; lần chạy sau ra 216,8 ms. Đúng như README cảnh báo, latency phải đo bằng load test, không đo bằng eval.
- Sửa KB làm token trung bình tăng nhẹ (+7,9/câu) nhưng cost / câu đúng còn giảm thêm, nên đáng làm.

---

## 4. Observability stack (production)

Stack: `docker compose -p k5-day14-observability up -d --build` (agent ×2 + Nginx + Prometheus + Grafana + Jaeger + Locust). Unit test alert: `promtool test rules /p/alert_rules_test.yml` → `SUCCESS`.

> **Sửa 1 lỗi để stack chạy được:** lần đầu `agent` restart liên tục với `ModuleNotFoundError: No module named 'uvicorn'`. Nguyên nhân: `production/Dockerfile` cài package vào `/root/.local` rồi copy sang `/home/agent/.local`, nhưng user `agent` lại có home là `/app` (`useradd -d /app`), nên Python tìm user site-packages ở `/app/.local`. Sửa bằng cách thêm `ENV PYTHONUSERBASE=/home/agent/.local`, giống Dockerfile của Part 5.

### 4.1 Screenshot

- [x] Dashboard Grafana khi đang có tải, thấy rõ Golden Signals: [`screenshots/grafana-dashboard.png`](screenshots/grafana-dashboard.png) (30 user, sau 2 lượt benchmark scaling 100 user)
- [x] Một trace chậm trong Jaeger, thấy span nào chiếm thời gian: [`screenshots/jaeger-slow-trace.png`](screenshots/jaeger-slow-trace.png)
- [x] Một trace lỗi trong Jaeger, có event `exception`: [`screenshots/jaeger-error-trace.png`](screenshots/jaeger-error-trace.png)
- [x] Trang Alerts của Prometheus với **2 alert cùng firing** (`AgentInstanceDown` + `LLMCostBurnRateHigh`): [`screenshots/prometheus-alert-firing.png`](screenshots/prometheus-alert-firing.png)
- Thêm: dashboard trong lúc thí nghiệm A, panel Error rate đỏ: [`screenshots/exp-A-error-rate.png`](screenshots/exp-A-error-rate.png)

### 4.2 Thí nghiệm "phá hệ thống"

Thời gian được đo bằng script: ghi mốc lúc gọi `docker compose up -d` (hoặc `docker pause`), rồi cứ 5 giây hỏi `GET /api/v1/alerts` một lần cho tới khi alert chuyển sang `firing`. Mỗi thí nghiệm bắt đầu lại từ `.env.example` (mặc định), nên thay đổi của thí nghiệm trước tự được hoàn tác.

| # | Thay đổi | Alert nào firing | Thời gian từ lúc đổi → firing | Panel nào phản ánh rõ nhất |
|---|---|---|---|---|
| A | `MOCK_ERROR_RATE=0.2` (30 user) | `AgentHighErrorRate` | **93 s** (pending sau 32 s + `for: 1m`) | *Error rate*: lên 19,9 % (nền 1,28 %); *LLM calls theo kết quả* (đường `error`) |
| B | `MOCK_SLOW_RATE=0.3` (30 user) | `AgentHighLatencyP95` | **147 s** (pending sau 26 s + `for: 2m`) | *Latency p50/p95/p99*: p95 1,91 s, p99 1,98 s nhưng avg chỉ 0,61 s |
| C | `LLM_MODEL=mock-llm-large` (**10 user**, xem ghi chú) | `LLMCostBurnRateHigh` | **177 s** (pending sau 62 s + `for: 2m`) | *Cost burn rate*: $0,81 → **$12,19/giờ** (×15); *LLM latency p95 theo model*: p50 LLM ~0,18 → 0,40 s |
| D | `docker pause k5-day14-observability-agent-1` | `AgentInstanceDown` | **50 s** (pending sau 20 s + `for: 30s`) | *Instances up*: 2 → 1 (`up{instance="172.25.0.8:8000"} = 0`) |
| E | `AGENT_VERSION=v1` (10 user) | **Không có** (theo dõi 202 s) | — | *Retrieval hit ratio* → "No data"; *Tokens/s* giảm; bảng *Phiên bản đang chạy* đổi sang `v1` |

**Ghi chú từ từng thí nghiệm:**

- **Baseline 30 user đã làm `LLMCostBurnRateHigh` firing.** Ở 30 user (~18,5 req/s `/ask`), chi phí đo được là **$2,47/giờ**, vượt ngưỡng $2/giờ, nên alert này firing ngay cả khi hệ thống bình thường. Vì vậy thí nghiệm C được chạy ở 10 user (baseline $0,81/giờ, không alert) để đo đúng thời gian "đổi model → firing". Ngưỡng $2/giờ quá sát với tải bình thường; production nên đặt theo budget thật (vd 2× mức chi tiêu bình thường), hoặc so với cùng giờ tuần trước.
- **A và B làm chi phí giảm.** Khi 20 % call lỗi (A), call lỗi không tốn token. Khi request chậm (B), mỗi user Locust gửi ít request hơn vì đây là tải vòng kín. Kết quả là `LLMCostBurnRateHigh` tự tắt trong lúc chạy A/B.
- **B: `avg` tăng ít, p95/p99 tăng vọt**, đúng như mục 2.2: 30 % request chậm thêm 1,5 s thì p95 lên 1,9 s trong khi avg chỉ 0,61 s. Alert đặt theo p95 bắt được, còn đặt theo avg (ngưỡng 1 s) thì sẽ không bao giờ kêu.
- **D: client không thấy lỗi 504 nào.** README dự đoán sẽ có 504 ở Nginx, nhưng trong lần chạy của mình, log Nginx suốt thời gian pause chỉ có `connect() failed (111: Connection refused) ... upstream: "http://172.25.0.8:8000/ask"` khoảng 11 giây một lần. Nhờ `proxy_next_upstream error timeout`, request được chuyển sang instance còn lại: 462 × 200, chỉ có 5 × 503 (lỗi mock LLM bình thường). Lý do nhận "refused" thay vì bị treo: mình pause `agent-1` ngay sau khi compose vừa tạo lại container đó, nên uvicorn chưa kịp mở socket. Dù vậy, kết luận của README vẫn đúng: phía agent không thấy gì cả (error rate panel xanh, 1,84 %), muốn thấy lỗi phải nhìn từ Nginx hoặc từ ngoài.
- **Đổi cấu hình bằng `docker compose up -d` gây downtime ngắn.** Lúc hoàn tác C, compose tạo lại **cả 2 replica cùng lúc**, nên Nginx trả **89 lỗi 502** ("no live upstreams") trong khoảng 12 giây. Compose không làm rolling update; production cần rolling deploy (thay từng instance, chờ `/ready` rồi mới thay cái tiếp theo).
- **Sự cố về tài nguyên máy:** trong lần chạy C đầu tiên (30 user), laptop bị thiếu RAM (swap 5,5/8 GB, load average lên ~380, kernel OOM killer giết một process Chrome không thuộc bài lab). Prometheus chỉ scrape được 68/300 mẫu trong 25 phút, nên kết quả lần đó bị loại và C được chạy lại sau khi máy ổn định. Bài học: chính hệ thống giám sát cũng có thể "mù" khi máy chủ của nó quá tải, nên cần cảnh báo khi scrape bị trễ hoặc thiếu dữ liệu (vd alert `absent()` / `up` của chính Prometheus), và đặt stack giám sát trên hạ tầng tách khỏi ứng dụng.

**Thí nghiệm E không làm alert nào firing. Bạn sẽ phát hiện loại sự cố này trong production bằng cách nào?**

E là lỗi **chất lượng**, không phải lỗi hạ tầng. Khi đổi về v1, mọi metric hạ tầng thậm chí còn "đẹp" hơn: p95 giảm 291 → 199 ms, input token/request giảm 79,6 → 36,3, error rate 0,9 %. Nhưng eval cho thấy accuracy rơi từ 100 % xuống 4,3 %. Cách phát hiện:

1. **Chặn trước khi deploy:** quality gate trong CI (`run_eval.py` + `--baseline`, mục 3). Bản v1 sẽ bị chặn với exit code 1 và 20 câu regression.
2. **Giám sát chất lượng sau khi deploy:**
   - Chạy eval định kỳ trên production (synthetic, vd mỗi 15 phút với golden dataset) và đẩy accuracy thành metric `agent_eval_accuracy`, có alert khi < 85 %.
   - Theo dõi các metric "đại diện" cho chất lượng: retrieval hit ratio (về 0 trong E, có thể alert khi giảm mạnh), số token output, độ dài câu trả lời.
   - Thu tín hiệu từ người dùng: 👍/👎, tỉ lệ hỏi lại, và LLM-as-judge chấm một mẫu nhỏ (sampling) câu trả lời thật.
3. **Alert khi phiên bản thay đổi:** dùng `agent_info{version}` để đánh dấu deploy trên dashboard (annotation), rồi đối chiếu với các tín hiệu chất lượng ở trên.

### 4.3 Debug bằng trace

Chọn trace chậm nhất trong 1500 trace của 10 phút gần nhất (lấy qua Jaeger API v3, sắp theo thời lượng):

- `trace_id`: `2e5617ab6e96bb61cd78f8055b34a4a4`
- Tổng thời gian: **1739.4 ms** (`POST /ask`, HTTP 200)
- Span chiếm nhiều thời gian nhất: **`chat mock-llm-small`** (**1692.7 ms, 97.3 %**). Các span còn lại: `agent.run` 1711.8 ms (span cha), `agent.retrieve_context` chỉ 18.9 ms (1.1 %). Attributes: `gen_ai.request.model = mock-llm-small`, `gen_ai.usage.input_tokens = 83`, `gen_ai.usage.output_tokens = 44`.
- Kết luận: request chậm vì **LLM upstream** (rơi vào nhóm `MOCK_SLOW_RATE` +1,5 s), không phải vì code agent hay retrieval.
- Dòng log tương ứng (`docker compose logs agent | grep 2e5617ab6e96bb61cd78f8055b34a4a4`):

```json
{"ts": "2026-11-26T10:18:39.683999+00:00", "level": "INFO", "service": "observable-agent", "instance": "c25887b80924", "event": "llm_call", "trace_id": "2e5617ab6e96bb61cd78f8055b34a4a4", "span_id": "48737cecce63497d", "request_id": "38cfa01f8477", "model": "mock-llm-small", "agent_version": "v2", "input_tokens": 83, "output_tokens": 44, "cost_usd": 3.9e-05, "llm_ms": 1692.8}
{"ts": "2026-11-26T10:18:39.684273+00:00", "level": "INFO", "service": "observable-agent", "instance": "c25887b80924", "event": "http_request", "trace_id": "2e5617ab6e96bb61cd78f8055b34a4a4", "span_id": "48737cecce63497d", "request_id": "38cfa01f8477", "method": "POST", "route": "/ask", "status": 200, "ms": 1738.9}
```

`llm_ms = 1692.8` trong log khớp với span `chat mock-llm-small` (1692.7 ms), và `ms = 1738.9` khớp với span gốc (1739.4 ms).

**Trace lỗi** (`1dbff8aba5bc10128d83d0640bfa2c5c`, 94.4 ms, HTTP 503): span `chat mock-llm-small` (71.5 ms) có `status = ERROR`, `error.type = upstream_unavailable` và event `exception` với `exception.type = utils.mock_llm.LLMError`, `exception.message = "Upstream LLM error: 503 Service Unavailable (mock)"`. Log cùng `trace_id`:

```json
{"ts": "2026-11-26T10:18:37.526525+00:00", "level": "INFO", "service": "observable-agent", "instance": "c25887b80924", "event": "llm_error", "trace_id": "1dbff8aba5bc10128d83d0640bfa2c5c", "span_id": "d4943b42a7f48611", "request_id": "9439e19d97af", "error": "Upstream LLM error: 503 Service Unavailable (mock)"}
```

(Nhận xét: sự kiện `llm_error` đang được log ở level `INFO`, đáng lẽ phải là `ERROR` hoặc `WARNING` để lọc log theo mức độ được.)

### 4.4 Trả lời câu hỏi Exercise 7.1 và 7.4

**7.1. Ví dụ các loại metric** (từ `curl localhost:8002/metrics` của agent develop):
- Counter: `http_requests_total{method="POST",route="/ask",status="200"} 1.0`, `llm_tokens_total{model="mock-llm-small",type="input"} 91.0`. Chỉ tăng, đọc bằng `rate()`.
- Gauge: `agent_in_flight_requests 0.0`, `agent_info{model="mock-llm-small",version="v2"} 1.0`. Lên xuống được.
- Histogram: `http_request_duration_seconds_bucket{le="0.3",method="POST",route="/ask"} 1.0` (+ `_sum`, `_count`). Tính percentile bằng `histogram_quantile()`.
- Metric mới thêm theo yêu cầu: `agent_question_length_chars` (Histogram, bucket 20/50/100/200/500/1000/2000). Câu hỏi 40 ký tự rơi vào `le="50.0"`. Dùng Histogram chứ không dùng label `length=...` vì độ dài có vô số giá trị.

**7.1. Nếu label `route` dùng URL thật:** mỗi URL lạ mà bot quét (`/wp-admin`, `/.env`, `/a1b2c3`…) sẽ tạo ra một time series mới cho mọi tổ hợp `method × status`, và với Histogram còn nhân thêm 13 bucket. Bot có thể sinh hàng nghìn tới hàng triệu series, làm Prometheus hết RAM; đây cũng là một kiểu tấn công DoS vào hệ thống giám sát. Đã thử: gọi 4 URL rác thì chỉ sinh **1** series `http_requests_total{method="GET",route="unmatched",status="404"} 4.0`.

**7.4** (30 user, 2 instance, lấy bằng PromQL của dashboard):
1. **p95 = 294 ms, avg = 236 ms** (p50 177 ms, **p99 1841 ms**). p95 và avg gần nhau vì 97 % request nằm quanh 180–220 ms; khoảng cách thật nằm ở **p99 ≈ 7,8× avg**, tức là 3 % request chậm bất thường (tail latency). Nhìn riêng avg hay p95 thì không thấy nhóm này.
2. **Có chia đều:** 9,27 và 9,20 req/s cho 2 instance (round-robin của Nginx; lúc 3 instance thì 9 request chia đúng 3/3/3).
3. **Input tokens/s (1442) > output tokens/s (769)**, tức là 79 so với 42 token mỗi request. Prompt v2 gồm system prompt + tài liệu retrieve được + câu hỏi, còn output chỉ là câu trả lời ngắn (mock trả nguyên 1 tài liệu). Đây là đặc trưng của RAG: context làm input phình to. Tuy vậy giá output đắt gấp 4 lần input, nên tính theo tiền thì output vẫn chiếm ~2/3 chi phí mỗi request (42 × 0,6 so với 79 × 0,15 trên mỗi triệu token).
4. **`curl http://localhost:18080/metrics` → `404 Not Found`**, vì `nginx.conf` có `location = /metrics { return 404; }`. Metrics lộ thông tin nội bộ (route, lưu lượng, version, chi phí), nên chỉ Prometheus được scrape trực tiếp từng container trong mạng `obs_net`; Nginx (cổng vào duy nhất từ bên ngoài) chặn đường này.

---

## 5. Benchmark scaling (production, Locust headless, 100 users, 2 phút)

`docker compose run --rm locust ... --headless -u 100 -r 10 -t 2m --csv /mnt/results/scale_<n>`. Số liệu là hàng `POST /ask` trong `benchmark-results/scale_<n>_stats.csv`:

| Số instance | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---|---|---|---|---|---|
| 1 | 59.80 | 190 | 220 | 1700 | 87 / 7126 (1.22 %) |
| 3 | 60.06 | 190 | 230 | 1700 | 68 / 7157 (0.95 %) |

(Hàng `Aggregated`, gồm cả `GET /health`: 65.82 so với 65.88 req/s.) Trước khi chạy scale_3 đã kiểm tra Prometheus thấy đủ 3 target `up`, và 9 request thử chia đều 3/3/3 cho 3 IP (`X-Served-By`).

**Giải thích kết quả:** Scale từ 1 lên 3 instance **không cải thiện** RPS (59,80 → 60,06) lẫn p95 (220 → 230 ms; khác biệt nằm trong sai số vì Locust làm tròn percentile theo bucket 10 ms).

1. **Tải bị giới hạn ở phía client, không ở phía server.** Locust là tải vòng kín: 100 user, mỗi user `wait_time = between(0.5, 2)` (trung bình 1,25 s) giữa hai request. Throughput tối đa ≈ 100 / (1,25 + 0,23) ≈ 67 req/s, đúng bằng số đo (65,8). Thêm instance cũng không có thêm request để xử lý.
2. **Một instance chưa hề bão hoà.** Trong lúc chạy, tổng `agent_in_flight_requests` cao nhất chỉ 23, CPU mỗi container dưới 1 %. Agent là async và LLM call là `asyncio.sleep`, nên một process chờ được rất nhiều request cùng lúc.
3. **Latency bị chặn dưới bởi LLM.** p50 ~190 ms gần như toàn bộ là thời gian mock LLM (trace ở mục 4.3: span `chat mock-llm-small` chiếm 97,3 % request). p99 ~1,7 s là request rơi vào nhóm `MOCK_SLOW_RATE` 3 %. Thêm instance không làm LLM trả lời nhanh hơn.

→ **Nút thắt là upstream LLM** (và cách tạo tải), không phải số instance. Scale ngang chỉ có ích khi agent thật sự bão hoà (CPU cao, in-flight tăng, p95 tăng theo tải) hoặc để chịu lỗi (mất 1 instance vẫn còn instance khác). Muốn giảm latency thì phải xử lý ở phía LLM: model nhỏ/nhanh hơn, cache câu trả lời, streaming, đặt timeout và retry/hedging cho request chậm.

---

## 6. Kết luận

- 3 điều bạn học được về benchmark & observability:
  1. **Percentile, không phải average, và phải biết mình đang đo cái gì.** Chỉ 3 % request chậm đã làm p99 tăng ×7 trong khi avg chỉ +25 %. Nhưng số đo cũng có thể sai vì công cụ đo: ở concurrency 100, chính `load_test.py` (86 % CPU) mới là nút thắt chứ không phải agent; còn Locust vòng kín có think time thì khoá RPS ở ~67 req/s dù có 1 hay 3 instance. Phải kiểm tra phía server (log `ms`, CPU, in-flight) trước khi kết luận.
  2. **Metric hạ tầng không nói gì về chất lượng.** Thí nghiệm E (quay về v1) làm accuracy rơi từ 100 % xuống 4,3 % mà dashboard còn đẹp hơn: p95 thấp hơn, ít token hơn, không alert nào. Chất lượng phải được đo riêng: eval làm quality gate trong CI, cộng với eval định kỳ và tín hiệu người dùng trên production.
  3. **Metrics → traces → logs nối với nhau qua `trace_id`.** Dashboard cho biết p99 cao, Jaeger chỉ ra 97,3 % thời gian nằm ở span LLM, còn log cùng `trace_id` cho đúng `llm_ms = 1692.8` và token của request đó. Mỗi trụ cột trả lời một câu hỏi khác nhau, và chỉ khi nối được với nhau thì debug mới nhanh.
- Nếu đưa agent này lên production thật, bạn sẽ thêm metric / alert nào nữa?
  - **Chất lượng:** `agent_eval_accuracy` từ eval định kỳ (alert khi < 85 %), retrieval hit ratio (alert khi giảm > 50 % so với 1 giờ trước), tỉ lệ 👎 của người dùng.
  - **Tầng load balancer / bên ngoài:** metric Nginx (5xx, 502/504, upstream response time), và synthetic check gọi `/ask` từ ngoài, vì như thí nghiệm D và lần đổi cấu hình cho thấy, lỗi ở tầng Nginx (89 × 502) agent hoàn toàn không thấy.
  - **SLO theo error budget burn rate** (multi-window, vd 5 phút và 1 giờ) thay cho ngưỡng cố định, để vừa báo nhanh khi sự cố lớn vừa không kêu vì spike ngắn.
  - **Cost:** ngưỡng theo budget thật hoặc so với cùng giờ tuần trước (ngưỡng $2/giờ hiện tại đã firing ở tải bình thường 30 user), cộng cost theo tenant/API key (tính ở tầng log hoặc billing, không làm label Prometheus để tránh cardinality).
  - **Giám sát chính hệ thống giám sát:** alert khi scrape thiếu hoặc trễ (`absent(up{job="agent"})`, `prometheus_rule_evaluation_duration`...). Trong buổi lab, máy bị quá tải đã làm Prometheus mất ~75 % mẫu trong 25 phút mà không ai biết.
  - **Saturation thật:** CPU/RAM của container so với limit (`cpus: 0.5`), độ sâu hàng đợi event loop, và rate-limit 429 từ LLM provider.
