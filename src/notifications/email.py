"""
Outlook email delivery via Microsoft Graph's sendMail API.

Deliberately not RabbitMQ or any other message broker — at this volume (one email per
investigation, not a high-throughput stream) there's no queue to build; Redis already covers
this codebase's actual queueing/coordination needs (credential cache, rerun idempotency, the
distributed concurrency semaphore), and adding a second broker just to send an email would be
new infrastructure to operate for no real gain. Delivery here is fire-and-forget
(asyncio.create_task from notifier.py), the same pattern rerun.py already uses for its
outcome-check poll.

Reuses the same Entra ID app registration as SSO (need_to_implement.txt item 17) — just add a
Mail.Send application permission to it, rather than standing up a separate credential. Sends
"as" a configured mailbox (NOTIFICATION_SENDER_UPN), not per-recipient delegated auth.

Not yet configured — GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET/
NOTIFICATION_SENDER_UPN all need that app registration's Mail.Send permission granted first.
Until then, sends are skipped with a logged warning: this is a delivery convenience layer, not
a security gate, so missing config degrades gracefully (the audit trail already has
`notification_ready` regardless) rather than failing the whole diagnosis the way a missing
`hmac_secret` correctly does.
"""
import logging

import httpx
from azure.identity import ClientSecretCredential

from config.settings import settings

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_credential: ClientSecretCredential | None = None


def _get_credential() -> ClientSecretCredential | None:
    global _credential
    if not (settings.graph_tenant_id and settings.graph_client_id and settings.graph_client_secret):
        return None
    if _credential is None:
        _credential = ClientSecretCredential(
            tenant_id=settings.graph_tenant_id,
            client_id=settings.graph_client_id,
            client_secret=settings.graph_client_secret,
        )
    return _credential


async def send_investigation_email(to_email: str, subject: str, body: str) -> None:
    """Best-effort — logs and returns rather than raising. Called fire-and-forget from
    notifier.py; a failed send should never fail the investigation it's reporting on."""
    credential = _get_credential()
    if credential is None or not settings.notification_sender_upn:
        logger.warning(
            "Graph email not configured (GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET/"
            "NOTIFICATION_SENDER_UPN) — skipping send to %s", to_email,
        )
        return

    try:
        token = credential.get_token(_GRAPH_SCOPE).token
    except Exception:
        logger.exception("Failed to acquire Graph token — skipping email to %s", to_email)
        return

    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": "false",
    }

    url = f"https://graph.microsoft.com/v1.0/users/{settings.notification_sender_upn}/sendMail"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=message)
            response.raise_for_status()
    except Exception:
        logger.exception("Failed to send investigation email to %s", to_email)
