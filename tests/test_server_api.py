"""Tests for peerport.server.api (REST stub routes)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from peerport.db import NewMail, insert_mail, open_db
from peerport.server.app import create_app

if TYPE_CHECKING:
    from pathlib import Path

ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/chat"),
    ("POST", "/api/board"),
    ("POST", "/api/mail/mail-1/reply"),
    ("POST", "/api/mail/mail-1/read"),
    ("GET", "/api/mail"),
    ("POST", "/api/world"),
    ("GET", "/api/notes"),
    ("POST", "/api/notes"),
    ("GET", "/api/logbook"),
    ("GET", "/api/usage"),
    ("POST", "/api/settings"),
    ("GET", "/api/peer/beacon"),
    ("GET", "/api/onboarding"),
]


class TestApiStubs:
    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_stub_route_responds_without_404(self, method: str, path: str) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.request(method, path)

        assert response.status_code != 404
        assert response.status_code == 501
        assert "detail" in response.json()

    def test_path_param_routes_echo_the_id_in_the_stub_body(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/api/peer/beacon")

        assert response.json()["peer_id"] == "beacon"


class FakeLogbookService:
    def read_logbook(self) -> dict[str, object]:
        return {
            "while_away": [{"text": "Tug tidied the pier.", "ts_world": 10}],
            "chronicle": [{"day": 1, "entries": ["Tug tidied the pier."]}],
        }


class TestGetLogbook:
    def test_returns_read_logbook_data_when_service_wired(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            app.state.logbook_service = FakeLogbookService()
            response = client.get("/api/logbook")

        assert response.status_code == 200
        assert response.json() == {
            "while_away": [{"text": "Tug tidied the pier.", "ts_world": 10}],
            "chronicle": [{"day": 1, "entries": ["Tug tidied the pier."]}],
        }


class TestGetMail:
    def test_lists_mails_newest_first(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "mail_api.db")
        insert_mail(
            conn,
            NewMail(friend_id="kai", direction="in", subject="Hi", body="body"),
        )
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.get("/api/mail")
        conn.close()

        assert response.status_code == 200
        mails = response.json()["mails"]
        assert len(mails) == 1
        assert mails[0]["friend_id"] == "kai"
        assert mails[0]["read"] is False


class TestMarkMailRead:
    def test_marks_read_and_clears_dot(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "mail_read.db")
        mail_id = insert_mail(
            conn,
            NewMail(friend_id="kai", direction="in", subject="Hi", body="body"),
        )
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.post(f"/api/mail/{mail_id}/read")
        conn.close()

        assert response.status_code == 200

    def test_non_numeric_id_rejected(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "mail_read2.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.post("/api/mail/not-a-number/read")
        conn.close()

        assert response.status_code == 422


class FakeMailService:
    def __init__(self) -> None:
        self.replies: list[tuple[int, str]] = []

    async def reply(self, mail_id: int, text: str) -> bool:
        self.replies.append((mail_id, text))
        return True


class TestPostMailReply:
    def test_reply_calls_service_and_returns_ok(self) -> None:
        app = create_app()
        service = FakeMailService()
        with TestClient(app) as client:
            app.state.mail_service = service
            response = client.post("/api/mail/7/reply", json={"text": "Glad to hear!"})

        assert response.status_code == 200
        assert service.replies == [(7, "Glad to hear!")]

    def test_empty_reply_rejected(self) -> None:
        app = create_app()
        service = FakeMailService()
        with TestClient(app) as client:
            app.state.mail_service = service
            response = client.post("/api/mail/7/reply", json={"text": "   "})

        assert response.status_code == 422
        assert service.replies == []
