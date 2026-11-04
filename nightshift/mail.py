"""Reading and sending email over IMAP/SMTP, kept behind two tiny interfaces
(Mailbox, Sender) so tests and other providers can swap in."""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional, Protocol

from .config import EmailConfig


class MailError(RuntimeError):
    pass


@dataclass
class Mail:
    message_id: str
    at: datetime
    from_addr: str
    from_name: str
    subject: str
    text: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    uid: int = 0


class Mailbox(Protocol):
    def fetch_since(self, since: datetime, after_uid: int = 0) -> list[Mail]: ...


class Sender(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


# ---------------------------------------------------------------------- parsing


class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []

    def handle_data(self, d):
        self.out.append(d)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li", "tr"):
            self.out.append("\n")


def _body_text(msg: email.message.Message) -> str:
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_content()
        except (KeyError, LookupError, UnicodeDecodeError):
            raw = part.get_payload(decode=True) or b""
            payload = raw.decode("utf-8", "replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = payload
        elif part.get_content_type() == "text/html" and html is None:
            html = payload
    if plain:
        text = plain
    elif html:
        p = _Strip()
        p.feed(html)
        text = "".join(p.out)
    else:
        text = ""
    # drop quoted history so we classify what *they* wrote
    text = re.split(r"\n(?:On .{5,80}wrote:|-{2,}\s*Original Message|From: .+\nSent: )", text)[0]
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse(raw: bytes, uid: int = 0) -> Mail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    try:
        at = parsedate_to_datetime(str(msg.get("Date")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        at = datetime.now(timezone.utc)
    mid = str(msg.get("Message-ID", "")).strip() or f"<uid-{uid}@local>"
    return Mail(message_id=mid, at=at, from_addr=addr.lower(), from_name=name, subject=str(msg.get("Subject", "")),
                text=_body_text(msg)[:6000], in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
                references=str(msg.get("References", "")).split(), uid=uid)


# ---------------------------------------------------------------------- IMAP / SMTP


class ImapMailbox:
    def __init__(self, cfg: EmailConfig):
        if not cfg.ready():
            raise MailError(f"Email isn't set up: add email.address in config.yaml and the {cfg.password_env} secret")
        self.cfg = cfg

    def fetch_since(self, since: datetime, after_uid: int = 0) -> list[Mail]:
        try:
            box = imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port, ssl_context=ssl.create_default_context(),
                                    timeout=60)
        except OSError as e:
            raise MailError(f"Couldn't reach {self.cfg.imap_host}: {e}") from e
        try:
            try:
                box.login(self.cfg.address, self.cfg.password())
            except imaplib.IMAP4.error as e:
                raise MailError("Email sign-in failed. Use an app password (not your normal password); "
                                "some school accounts don't allow them.") from e
            box.select("INBOX", readonly=True)
            crit = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
            typ, data = box.uid("SEARCH", None, crit)
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > after_uid][-300:]
            out: list[Mail] = []
            for uid in uids:
                typ, parts = box.uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK: don't mark as read
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    out.append(parse(raw, uid))
            return out
        finally:
            try:
                box.logout()
            except Exception:  # noqa: BLE001
                pass


class SmtpSender:
    def __init__(self, cfg: EmailConfig):
        if not cfg.ready():
            raise MailError(f"Email isn't set up: add email.address in config.yaml and the {cfg.password_env} secret")
        self.cfg = cfg

    def send(self, msg: EmailMessage) -> None:
        try:
            if self.cfg.smtp_port == 465:
                s = smtplib.SMTP_SSL(self.cfg.smtp_host, 465, context=ssl.create_default_context(), timeout=60)
            else:
                s = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=60)
                s.starttls(context=ssl.create_default_context())
            with s:
                s.login(self.cfg.address, self.cfg.password())
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise MailError("Email sign-in failed. Use an app password (not your normal password).") from e
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"Couldn't send: {e}") from e


def compose(cfg: EmailConfig, to_addr: str, to_name: str, subject: str, body: str,
            in_reply_to: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name or cfg.address, cfg.address))
    msg["To"] = formataddr((to_name, to_addr)) if to_name else to_addr
    msg["Subject"] = subject
    domain = cfg.address.split("@")[-1] or "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


def default_since(cfg: EmailConfig) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=cfg.lookback_days)
