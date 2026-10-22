"""Optional FCM sender. Imported lazily; absent `firebase-admin` or FCM_CREDENTIALS_FILE simply disables real push.

Install `firebase-admin` in the api/worker image to enable it (deliberately not in requirements.txt: the app works
without push and the notification service records every target either way).
"""

from __future__ import annotations

import logging
import threading

from ..core.config import get_settings

log = logging.getLogger("app.fcm")
_lock = threading.Lock()
_app = None


def is_configured() -> bool:
    return bool(get_settings().fcm_credentials_file)


def _get_app():
    global _app
    with _lock:
        if _app is None:
            import firebase_admin
            from firebase_admin import credentials

            _app = firebase_admin.initialize_app(credentials.Certificate(get_settings().fcm_credentials_file))
        return _app


FCM_BATCH = 500  # FCM multicast limit


def send(tokens: list[str], payload: dict[str, str], high_priority: bool = False) -> list[str]:
    """Blocking; call through asyncio.to_thread. Data-only message (no sensitive content), sent in batches of 500 tokens.
    Returns the tokens FCM reports as unregistered/invalid so the caller can delete them."""
    if not tokens or not is_configured():
        return []
    try:
        from firebase_admin import messaging
    except ImportError:
        log.warning("FCM_CREDENTIALS_FILE is set but firebase-admin is not installed; push skipped")
        return []
    invalid: list[str] = []
    data = {k: str(v) for k, v in payload.items()}
    for i in range(0, len(tokens), FCM_BATCH):
        chunk = tokens[i : i + FCM_BATCH]
        msg = messaging.MulticastMessage(
            data=data,
            tokens=chunk,
            android=messaging.AndroidConfig(priority="high" if high_priority else "normal"),
            apns=messaging.APNSConfig(headers={"apns-priority": "10" if high_priority else "5"}),
        )
        resp = messaging.send_each_for_multicast(msg, app=_get_app())
        for token, r in zip(chunk, resp.responses, strict=False):
            if not r.success and isinstance(r.exception, messaging.UnregisteredError):
                invalid.append(token)
    return invalid
