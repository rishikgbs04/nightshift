"""Push notifications through ntfy (https://ntfy.sh): free, no account needed.

Install the ntfy app on your phone, subscribe to your topic, and nightshift
tells you each morning how many applications are waiting.
"""

from __future__ import annotations

from typing import Optional

import httpx

from .config import NotifyConfig


def send(cfg: NotifyConfig, title: str, message: str, client: Optional[httpx.Client] = None,
         priority: str = "default") -> bool:
    if not cfg.ntfy_topic:
        return False
    headers = {"Title": title, "Tags": "briefcase", "Priority": priority}
    if cfg.app_url:
        headers["Click"] = cfg.app_url
    client = client or httpx.Client(timeout=15)
    try:
        r = client.post(f"{cfg.ntfy_server.rstrip('/')}/{cfg.ntfy_topic}",
                        content=message.encode(), headers=headers)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def morning_message(waiting: int, auto_approved: int, submitted: int, needs_you: int) -> tuple[str, str]:
    if waiting == 0 and submitted == 0 and needs_you == 0:
        return "nightshift", "No new matches last night. Nothing to do."
    title = f"{waiting} job{'s' if waiting != 1 else ''} to review" if waiting else "Applications sent"
    parts = []
    if submitted:
        parts.append(f"{submitted} application{'s' if submitted != 1 else ''} sent")
    if auto_approved and not submitted:
        parts.append(f"{auto_approved} auto-approved")
    if needs_you:
        parts.append(f"{needs_you} need{'s' if needs_you == 1 else ''} you to finish")
    if waiting:
        parts.append("tap to review")
    return title, ", ".join(parts).capitalize() + "."
