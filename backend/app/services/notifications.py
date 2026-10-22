"""Push-notification hook (contract section 11). Best effort: a failure is logged and NEVER blocks the caller.

`notify()` resolves the recipients' device tokens, records the targets in an in-memory outbox (inspected by tests and
useful for debugging; token values are never stored there or logged) and, if FCM is configured, hands the data message
to app.services.fcm in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import DeviceToken
from . import fcm

log = logging.getLogger("app.notify")

OUTBOX: deque[dict[str, Any]] = deque(maxlen=200)
SEND_TIMEOUT_S = 5


async def notify(
    session: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    *,
    type: str,
    entity_id: uuid.UUID | str,
    title: str,
    body: str,
    high_priority: bool = False,
) -> int:
    """Returns the number of device-token targets (0 when nobody has a registered device)."""
    ids = sorted({u for u in user_ids}, key=str)
    if not ids:
        return 0
    try:
        rows = (await session.execute(select(DeviceToken).where(DeviceToken.user_id.in_(ids)))).scalars().all()
        payload = {"type": type, "id": str(entity_id), "title": title, "body": body}
        OUTBOX.append(
            {
                "payload": payload,
                "high_priority": high_priority,
                "user_ids": [str(u) for u in ids],
                "targets": [{"user_id": str(t.user_id), "device_token_id": str(t.id), "platform": t.platform} for t in rows],
            }
        )
        log.info("notify %s id=%s users=%d devices=%d", type, entity_id, len(ids), len(rows))
        if rows and fcm.is_configured():
            invalid = await asyncio.wait_for(
                asyncio.to_thread(fcm.send, [t.fcm_token for t in rows], payload, high_priority),
                SEND_TIMEOUT_S * max(1, len(rows) // 500 + 1),
            )
            if invalid:  # FCM says these registrations are dead: drop them (own session: never commits the caller's work)
                async with async_sessionmaker(session.bind, expire_on_commit=False)() as cleanup:  # type: ignore[arg-type]
                    await cleanup.execute(delete(DeviceToken).where(DeviceToken.fcm_token.in_(invalid)))
                    await cleanup.commit()
        return len(rows)
    except Exception:  # noqa: BLE001 - push must never fail the business operation
        log.exception("push notification failed (ignored)")
        return 0


async def push_account_disabled(user_id: uuid.UUID, fcm_tokens: list[str]) -> None:
    """Data-only `{type: account_disabled}` (no id / title / body) to the tokens an offboarded user had. Called AFTER the
    commit that deleted them. Best effort; the app only reacts by calling GET /auth/me, so a push alone never wipes."""
    if not fcm_tokens:
        return
    payload = {"type": "account_disabled"}
    try:
        OUTBOX.append(
            {
                "payload": payload,
                "high_priority": True,
                "user_ids": [str(user_id)],
                "targets": [{"user_id": str(user_id), "device_token_id": None, "platform": None} for _ in fcm_tokens],
            }
        )
        if fcm.is_configured():
            await asyncio.wait_for(asyncio.to_thread(fcm.send, fcm_tokens, payload, True), SEND_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - push must never fail the business operation
        log.exception("account_disabled push failed (ignored)")
