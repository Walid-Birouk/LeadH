"""Saves drafts into your mailbox's Drafts folder over IMAP, for you to read, edit and send yourself. There is no
sending code here: IMAP can only store messages, and a stored draft goes nowhere until you press send."""

import contextlib
import imaplib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Protocol, cast

from leadh.models import Lead, now
from leadh.store import Store

# Usual Drafts folder names, for servers that do not flag theirs with the \Drafts attribute (RFC 6154).
DRAFT_FOLDERS = ["Drafts", "[Gmail]/Drafts", "[Gmail]/Brouillons", "Brouillons", "INBOX.Drafts", "Draft"]
PRESETS = {"gmail": "imap.gmail.com", "outlook": "outlook.office365.com", "ovh": "ssl0.ovh.net",
           "icloud": "imap.mail.me.com", "yahoo": "imap.mail.yahoo.com"}


class MailboxError(Exception):
    pass


Reply = tuple[str, list[bytes | None]]


class Imap(Protocol):
    """The part of imaplib.IMAP4 this module uses."""

    def login(self, user: str, password: str) -> Reply: ...
    def authenticate(self, mechanism: str, authobject: Callable[[bytes], bytes | None]) -> Reply: ...
    def list(self) -> Reply: ...
    def append(self, mailbox: str, flags: str, date_time: object, message: bytes) -> Reply: ...
    def logout(self) -> Reply: ...


@dataclass
class MailboxSettings:
    host: str
    user: str
    # An app password, or empty for a Microsoft account: Outlook.com and Microsoft 365 refuse passwords over
    # IMAP since September 2024 and take only an OAuth sign-in.
    password: str = ""
    ms_client_id: str = ""
    sender_name: str = ""
    folder: str = ""  # empty: found from the server's folder list

    @property
    def microsoft(self) -> bool:
        return self.host == PRESETS["outlook"]

    @classmethod
    def from_env(cls) -> "MailboxSettings":
        user = os.environ.get("LEADH_MAIL_USER", "")
        host = os.environ.get("LEADH_MAIL_IMAP_HOST", "")
        host = PRESETS.get(host.lower(), host) or PRESETS.get(_provider_of(user), "")
        s = cls(host=host, user=user, password=os.environ.get("LEADH_MAIL_APP_PASSWORD", ""),
                ms_client_id=os.environ.get("LEADH_MAIL_MS_CLIENT_ID", ""),
                sender_name=os.environ.get("LEADH_MAIL_FROM_NAME", ""),
                folder=os.environ.get("LEADH_MAIL_DRAFTS_FOLDER", ""))
        needed = [("LEADH_MAIL_USER", user), ("LEADH_MAIL_IMAP_HOST", host)]
        needed += [("LEADH_MAIL_MS_CLIENT_ID", s.ms_client_id)] if s.microsoft else \
            [("LEADH_MAIL_APP_PASSWORD", s.password)]
        missing = [name for name, value in needed if not value]
        if missing:
            raise MailboxError(f"set {', '.join(missing)} in .env (see .env.example)")
        return s


MS_SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]
PERSONAL_MICROSOFT = frozenset({"outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr", "live.com", "live.fr",
                                "msn.com"})


def microsoft_token(settings: MailboxSettings, cache_path: Path, show: Callable[[str], None] = print) -> str:
    """An IMAP access token for a Microsoft account. The first time, it prints a code to enter at
    microsoft.com/devicelogin; after that the cached refresh token signs in silently (the cache stays in data/)."""
    import msal

    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())
    # Personal addresses sign in through the consumers endpoint; a work address through the common one.
    personal = settings.user.rpartition("@")[2].lower() in PERSONAL_MICROSOFT
    app = msal.PublicClientApplication(
        settings.ms_client_id, token_cache=cache,
        authority=f"https://login.microsoftonline.com/{'consumers' if personal else 'common'}")
    accounts = app.get_accounts(username=settings.user)
    result = app.acquire_token_silent(MS_SCOPES, account=accounts[0]) if accounts else None
    if not result:
        flow = app.initiate_device_flow(scopes=MS_SCOPES)
        if "user_code" not in flow:
            raise MailboxError(f"Microsoft sign-in could not start: {flow.get('error_description', flow)}")
        show(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise MailboxError(f"Microsoft sign-in failed: {result.get('error_description', result.get('error'))}")
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize())
        cache_path.chmod(0o600)
    return str(result["access_token"])


def sign_in(imap: Imap, settings: MailboxSettings, token: Callable[[], str]) -> None:
    try:
        if settings.microsoft:
            bearer = token()
            imap.authenticate("XOAUTH2", lambda _: f"user={settings.user}\x01auth=Bearer {bearer}\x01\x01".encode())
        else:
            imap.login(settings.user, settings.password)
    except imaplib.IMAP4.error as e:
        hint = "check the account allows IMAP" if settings.microsoft else "use an app password, not your usual one"
        raise MailboxError(f"sign-in refused by {settings.host}: {hint} ({e})") from e


def _provider_of(address: str) -> str:
    domain = address.rpartition("@")[2].lower()
    if domain in PERSONAL_MICROSOFT:
        return "outlook"
    return {"gmail.com": "gmail", "googlemail.com": "gmail", "icloud.com": "icloud", "me.com": "icloud",
            "yahoo.fr": "yahoo", "yahoo.com": "yahoo"}.get(domain, "")


def find_drafts_folder(imap: Imap) -> str:
    status, rows = imap.list()
    if status != "OK":
        raise MailboxError("could not list mailbox folders")
    names: list[tuple[str, str]] = []
    for raw in rows:
        if not raw:
            continue
        line = raw.decode(errors="replace")
        m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
        if m:
            names.append((m.group("flags"), m.group("name").strip().strip('"')))
    for flags, name in names:
        if "\\Drafts" in flags:
            return name
    for wanted in DRAFT_FOLDERS:
        for _, name in names:
            if name.lower() == wanted.lower():
                return name
    raise MailboxError("no Drafts folder found; set LEADH_MAIL_DRAFTS_FOLDER")


def build_message(lead: Lead, settings: MailboxSettings) -> bytes:
    assert lead.draft and lead.contact
    msg = EmailMessage()
    msg["From"] = f"{settings.sender_name} <{settings.user}>" if settings.sender_name else settings.user
    msg["To"] = lead.contact.email
    msg["Subject"] = lead.draft.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.user.rpartition("@")[2] or None)
    # Lets you find, in the CSV or the database, which lead and variant a draft belongs to.
    msg["X-LeadH-Lead"] = f"{lead.lead_id} {lead.campaign or '-'} {lead.draft.variant}"
    msg.set_content(lead.draft.body)
    return msg.as_bytes()


@dataclass
class SaveResult:
    saved: int = 0
    needs_edit: int = 0
    no_contact: int = 0
    already: int = 0
    folder: str = ""


def save_drafts(store: Store, settings: MailboxSettings, token: Callable[[], str],
                connect: Callable[[str], Imap] | None = None, limit: int | None = None) -> SaveResult:
    """Ready drafts not yet in the mailbox, best leads first. Drafts that need your edit stay in LeadH."""
    leads = [x for x in store.all() if x.draft]
    leads.sort(key=lambda x: -(x.scores.need * x.scores.fit if x.scores else 0))
    result = SaveResult()
    todo = []
    for lead in leads:
        assert lead.draft
        if lead.draft.in_mailbox_at:
            result.already += 1
        elif lead.draft.status != "ready":
            result.needs_edit += 1
        elif not lead.contact or store.contacted(lead.siren) or store.opted_out(
                lead.siren, lead.contact.email.rpartition("@")[2]):
            result.no_contact += 1
        else:
            todo.append(lead)
    todo = todo[:limit] if limit is not None else todo
    if not todo:
        return result
    imap = connect(settings.host) if connect else cast(Imap, imaplib.IMAP4_SSL(settings.host))
    try:
        sign_in(imap, settings, token)
        result.folder = settings.folder or find_drafts_folder(imap)
        folder = f'"{result.folder}"' if " " in result.folder else result.folder
        for lead in todo:
            assert lead.draft
            status, _ = imap.append(folder, "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()),
                                    build_message(lead, settings))
            if status != "OK":
                raise MailboxError(f"the server refused to store the draft for {lead.lead_id}")
            lead.draft.in_mailbox_at = now()
            store.save(lead)
            result.saved += 1
    finally:
        with contextlib.suppress(Exception):  # a failed logout after the work is done changes nothing
            imap.logout()
    return result
