"""
Rate limiting (tiêu chí 7).

  • Mỗi API key tối đa RATE_LIMIT_PER_MINUTE request trong 60 giây (sliding window)
  • Vượt giới hạn → HTTP 429 kèm header Retry-After
  • State lưu trong Redis (sorted set) nên mọi worker / instance dùng chung và vẫn còn
    sau khi restart agent
"""
import hashlib
import time
import uuid

from fastapi import HTTPException

from app.config import settings
from app.redis_client import r

WINDOW_SECONDS = 60


def _bucket(key: str) -> str:
    # Không lưu API key thô trong Redis — chỉ lưu hash.
    return "ratelimit:" + hashlib.sha256(key.encode()).hexdigest()[:32]


def check_rate_limit(key: str) -> None:
    limit = settings.rate_limit_per_minute
    bucket = _bucket(key)
    now = time.time()
    member = f"{now}:{uuid.uuid4().hex}"

    # Chạy trong 1 transaction (MULTI/EXEC) để 2 worker không đếm lệch nhau.
    pipe = r.pipeline(transaction=True)
    pipe.zremrangebyscore(bucket, 0, now - WINDOW_SECONDS)   # bỏ request ngoài cửa sổ 60s
    pipe.zadd(bucket, {member: now})                          # ghi request hiện tại
    pipe.zcard(bucket)                                        # đếm request trong cửa sổ
    pipe.zrange(bucket, 0, 0, withscores=True)                # request cũ nhất → tính Retry-After
    pipe.expire(bucket, WINDOW_SECONDS + 1)
    _, _, count, oldest, _ = pipe.execute()

    if count > limit:
        # Request bị từ chối không được tính vào hạn mức
        r.zrem(bucket, member)
        oldest_ts = oldest[0][1] if oldest else now
        retry_after = max(1, int(oldest_ts + WINDOW_SECONDS - now) + 1)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} requests per {WINDOW_SECONDS}s",
            headers={"Retry-After": str(retry_after)},
        )
