import email
from collections.abc import Callable
from pathlib import Path

import pytest

from leadh.mailbox import MailboxError, MailboxSettings, Reply, build_message, find_drafts_folder, save_drafts
from leadh.models import Contact, Draft, Lead, Scores
from leadh.store import Store


class FakeImap:
    def __init__(self, folders: list[bytes], refuse_login: bool = False) -> None:
        self.folders = folders
        self.refuse_login = refuse_login
        self.appended: list[tuple[str, str, bytes]] = []
        self.auth: list[str] = []

    def login(self, user: str, password: str) -> Reply:
        import imaplib

        if self.refuse_login:
            raise imaplib.IMAP4.error("AUTHENTICATE failed")
        self.auth.append(f"login {user}")
        return "OK", [None]

    def authenticate(self, mechanism: str, authobject: Callable[[bytes], bytes | None]) -> Reply:
        self.auth.append(f"{mechanism} {(authobject(b'') or b'').decode()}")
        return "OK", [None]

    def list(self) -> Reply:
        return "OK", list(self.folders)

    def append(self, mailbox: str, flags: str, date_time: object, message: bytes) -> Reply:
        self.appended.append((mailbox, flags, message))
        return "OK", [None]

    def logout(self) -> Reply:
        return "BYE", [None]


OUTLOOK = [b'(\\HasNoChildren) "/" Inbox', b'(\\HasNoChildren \\Drafts) "/" Drafts', b'(\\Sent) "/" "Sent Items"']


def lead(n: int, status: str = "ready", need: int = 60) -> Lead:
    return Lead(lead_id=f"L-{n:06d}", siren=f"81234567{n}", name="EXEMPLE", campaign="c1",
                contact=Contact(email=f"contact@a{n}.fr", kind="generic", source_url=f"https://a{n}.fr/contact"),
                scores=Scores(need=need, fit=80),
                draft=Draft.model_validate({"variant": "B", "template": "t", "subject": "hello", "body": "Bonjour\n",
                                            "status": status}))


def settings(host: str = "outlook.office365.com") -> MailboxSettings:
    return MailboxSettings(host=host, user="me@outlook.fr", ms_client_id="cid", sender_name="Walid")


def test_saves_ready_drafts_once_best_first_and_never_sends(store: Store) -> None:
    for x in [lead(1, need=40), lead(2, need=90), lead(3, status="needs_edit")]:
        store.save(x)
    store.add_contacted("812345674")
    store.save(lead(4))
    imap = FakeImap(OUTLOOK)
    r = save_drafts(store, settings(), token=lambda: "tok", connect=lambda host: imap)
    assert (r.saved, r.needs_edit, r.no_contact, r.folder) == (2, 1, 1, "Drafts")
    assert [m for m, _, _ in imap.appended] == ["Drafts", "Drafts"]
    assert all(f == "(\\Draft \\Seen)" for _, f, _ in imap.appended)
    first = email.message_from_bytes(imap.appended[0][2])
    assert first["To"] == "contact@a2.fr" and first["X-LeadH-Lead"] == "L-000002 c1 B"
    assert imap.auth == ["XOAUTH2 user=me@outlook.fr\x01auth=Bearer tok\x01\x01"]
    assert not hasattr(imap, "send") and not hasattr(imap, "sendmail")

    again = FakeImap(OUTLOOK)
    r2 = save_drafts(store, settings(), token=lambda: "tok", connect=lambda host: again)
    assert r2.saved == 0 and r2.already == 2 and again.appended == []


def test_no_connection_when_nothing_to_save(store: Store) -> None:
    store.save(lead(1, status="needs_edit"))

    def boom(host: str) -> FakeImap:
        raise AssertionError("should not connect")

    assert save_drafts(store, settings(), token=lambda: "tok", connect=boom).needs_edit == 1


def test_password_accounts_and_refused_login(store: Store) -> None:
    store.save(lead(1))
    s = MailboxSettings(host="ssl0.ovh.net", user="me@espa-studio.fr", password="app-pw")
    imap = FakeImap([b'(\\HasNoChildren) "." INBOX.Drafts'])
    assert save_drafts(store, s, token=lambda: "unused", connect=lambda h: imap).folder == "INBOX.Drafts"
    assert imap.auth == ["login me@espa-studio.fr"]
    store.save(lead(2))
    with pytest.raises(MailboxError, match="app password"):
        save_drafts(store, s, token=lambda: "x", connect=lambda h: FakeImap([], refuse_login=True))


def test_drafts_folder_lookup() -> None:
    assert find_drafts_folder(FakeImap([b'(\\HasNoChildren) "/" "[Gmail]/Brouillons"'])) == "[Gmail]/Brouillons"
    with pytest.raises(MailboxError, match="LEADH_MAIL_DRAFTS_FOLDER"):
        find_drafts_folder(FakeImap([b'(\\HasNoChildren) "/" Inbox']))


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ["LEADH_MAIL_IMAP_HOST", "LEADH_MAIL_APP_PASSWORD", "LEADH_MAIL_MS_CLIENT_ID"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LEADH_MAIL_USER", "me@hotmail.fr")
    with pytest.raises(MailboxError, match="LEADH_MAIL_MS_CLIENT_ID"):
        MailboxSettings.from_env()
    monkeypatch.setenv("LEADH_MAIL_MS_CLIENT_ID", "cid")
    s = MailboxSettings.from_env()
    assert s.host == "outlook.office365.com" and s.microsoft
    monkeypatch.setenv("LEADH_MAIL_USER", "me@espa-studio.fr")
    monkeypatch.setenv("LEADH_MAIL_IMAP_HOST", "ovh")
    with pytest.raises(MailboxError, match="LEADH_MAIL_APP_PASSWORD"):
        MailboxSettings.from_env()


def test_message_headers(tmp_path: Path) -> None:
    msg = email.message_from_bytes(build_message(lead(1), settings()))
    assert msg["From"] == "Walid <me@outlook.fr>" and msg["Subject"] == "hello"
    assert msg.get_payload(decode=True) == b"Bonjour\n"
