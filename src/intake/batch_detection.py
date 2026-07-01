from config.settings import settings


async def check_batch(redis, platform: str, project: str, now: float) -> str:
    """
    Sliding-window batch detector. Tracks per-platform failure counts using a Redis
    sorted set (score = unix timestamp). Returns one of:

    - "individual"     : fewer than batch_threshold events in the window → run normally
    - "batch_alert"    : exactly the threshold-th event → caller should send aggregated alert
    - "batch_suppress" : beyond the threshold → silently suppress individual investigation
    """
    key = f"batch:{platform}"
    window_start = now - settings.batch_window_seconds
    member = f"{project}:{int(now)}"

    # Add current event, purge expired entries, refresh TTL
    await redis.zadd(key, {member: now})
    await redis.zremrangebyscore(key, "-inf", window_start)
    await redis.expire(key, settings.batch_window_seconds * 2)

    count = await redis.zcard(key)

    if count < settings.batch_threshold:
        return "individual"
    if count == settings.batch_threshold:
        return "batch_alert"
    return "batch_suppress"


async def get_batch_members(redis, platform: str, now: float) -> list[str]:
    """Return the project:timestamp members currently in the batch window for a platform."""
    key = f"batch:{platform}"
    window_start = now - settings.batch_window_seconds
    return await redis.zrangebyscore(key, window_start, "+inf")
