"""Tests for peerport.server.api (REST stub routes)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from peerport.db import NewMail, insert_mail, open_db
from peerport.mate.notes import NotesStore
from peerport.memory.stream import MemoryStream
from peerport.peers.personas import load_personas
from peerport.server.api import ONBOARDING_KEEPER_NAME_KEY
from peerport.server.app import create_app
from tests.test_memory import FakeEmbedder

if TYPE_CHECKING:
    import sqlite3

    from fastapi import FastAPI
    from httpx import Response

REPO_ROOT = Path(__file__).parent.parent

ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/chat"),
    ("POST", "/api/board"),
    ("POST", "/api/mail/mail-1/reply"),
    ("POST", "/api/mail/mail-1/read"),
    ("GET", "/api/mail"),
    ("POST", "/api/world"),
    ("GET", "/api/notes"),
    ("GET", "/api/notes/note-1"),
    ("DELETE", "/api/notes/note-1"),
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
            NewMail(
                friend_id="kai",
                direction="in",
                subject="Hi",
                body="body",
                ts_world=7200,
            ),
        )
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.get("/api/mail")
        conn.close()

        assert response.status_code == 200
        mails = response.json()["mails"]
        assert mails[0]["world_day"] == 2
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


class TestNotesRoutes:
    def test_get_notes_lists_from_store(self, tmp_path: Path) -> None:
        store = NotesStore(tmp_path / "notes")
        store.create("Tide Patterns", "body")
        app = create_app()
        with TestClient(app) as client:
            app.state.notes_store = store
            response = client.get("/api/notes")

        assert response.status_code == 200
        notes = response.json()["notes"]
        assert len(notes) == 1
        assert notes[0]["title"] == "Tide Patterns"

    def test_get_note_detail_returns_content(self, tmp_path: Path) -> None:
        store = NotesStore(tmp_path / "notes")
        note_id = store.create("Tide Patterns", "The tides run high.")
        app = create_app()
        with TestClient(app) as client:
            app.state.notes_store = store
            response = client.get(f"/api/notes/{note_id}")

        assert response.status_code == 200
        assert "The tides run high." in response.json()["content_markdown"]

    def test_get_note_detail_unknown_id_404(self, tmp_path: Path) -> None:
        store = NotesStore(tmp_path / "notes")
        app = create_app()
        with TestClient(app) as client:
            app.state.notes_store = store
            response = client.get("/api/notes/does-not-exist")

        assert response.status_code == 404

    def test_delete_note_removes_it(self, tmp_path: Path) -> None:
        store = NotesStore(tmp_path / "notes")
        note_id = store.create("Tide Patterns", "body")
        app = create_app()
        with TestClient(app) as client:
            app.state.notes_store = store
            response = client.delete(f"/api/notes/{note_id}")

        assert response.status_code == 200
        assert store.list_notes() == []


class FakeMateChat:
    """Minimal stand-in exposing only the `.memory` onboarding reads (#29)."""

    def __init__(self, memory: MemoryStream) -> None:
        self.memory = memory


def _wire_onboarding_app(app: FastAPI, conn: sqlite3.Connection) -> None:
    """Wire the bits `/api/onboarding` and `/api/settings` read (#29)."""
    app.state.db_conn = conn
    app.state.personas = dict(load_personas(REPO_ROOT / "personas"))
    app.state.mate_chat = FakeMateChat(MemoryStream(conn, FakeEmbedder()))


class TestOnboardingStepRouting:
    """`GET /api/onboarding`: step order per D-018 (#29)."""

    def test_step_is_api_key_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        conn = open_db(tmp_path / "onboarding1.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.get("/api/onboarding")
        conn.close()

        assert response.status_code == 200
        assert response.json()["step"] == "api_key"

    def test_step_is_locale_once_key_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "onboarding2.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.get("/api/onboarding")
        conn.close()

        assert response.json()["step"] == "locale"

    def test_keeper_name_set_early_does_not_skip_the_locale_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-001 boundary: step 3 never renders before step 2 completes."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "onboarding3.db")
        conn.execute(
            "INSERT INTO world_state (key, value) VALUES (?, ?)",
            (ONBOARDING_KEEPER_NAME_KEY, "Rin"),
        )
        conn.commit()
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.get("/api/onboarding")
        conn.close()

        assert response.json()["step"] == "locale"


class TestPostSettingsOnboardingFields:
    """`POST /api/settings`: validation and step advancement (#29)."""

    def test_locale_then_keeper_name_advance_through_steps_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "settings1.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            assert client.get("/api/onboarding").json()["step"] == "locale"

            response = client.post("/api/settings", json={"locale": "ja"})
            assert response.status_code == 200
            assert response.json()["step"] == "keeper_name"

            response = client.post("/api/settings", json={"keeper_name": "Rin"})
            assert response.json()["step"] == "mate_name"
        conn.close()

    def test_invalid_locale_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "settings2.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            response = client.post("/api/settings", json={"locale": "fr"})
        conn.close()

        assert response.status_code == 422

    def test_empty_keeper_name_rejected_and_does_not_advance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-006 boundary: an empty Keeper name must not advance the flow."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "settings3.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            client.post("/api/settings", json={"locale": "en"})
            response = client.post("/api/settings", json={"keeper_name": "   "})
            assert response.status_code == 422
            assert client.get("/api/onboarding").json()["step"] == "keeper_name"
        conn.close()

    def test_mate_name_rejected_before_locale_and_keeper_name_are_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Premature completion is refused: no persona rename, no seed rows."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "settings4.db")
        app = create_app()
        with TestClient(app) as client:
            _wire_onboarding_app(app, conn)
            response = client.post("/api/settings", json={"mate_name": "Lumen"})

        assert response.status_code == 409
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert app.state.personas["beacon"].name == "Beacon"
        conn.close()


class TestOnboardingCompletion:
    """Completing step 4 renames Mate and seeds memories once (#29)."""

    def _complete_flow(
        self, client: TestClient, app: FastAPI, conn: sqlite3.Connection, mate_name: str
    ) -> Response:
        _wire_onboarding_app(app, conn)
        client.post("/api/settings", json={"locale": "en"})
        client.post("/api/settings", json={"keeper_name": "Rin"})
        response: Response = client.post("/api/settings", json={"mate_name": mate_name})
        return response

    def test_completion_writes_each_map_visible_personas_seed_memories_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-010: beacon/tug/bell/echo each get their 2 seed rows, once."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "complete1.db")
        app = create_app()
        with TestClient(app) as client:
            response = self._complete_flow(client, app, conn, "Lumen")

        assert response.status_code == 200
        assert response.json()["step"] == "done"
        rows = conn.execute("SELECT peer_id, text FROM memories").fetchall()
        conn.close()

        assert {peer_id for peer_id, _ in rows} == {"beacon", "tug", "bell", "echo"}
        assert len(rows) == 8
        assert any("lighthouse first lit" in text for _, text in rows)

    def test_mate_persona_renamed_when_keeper_renames_during_the_conversation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-007: an explicit rename overrides the "Beacon" default."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "complete2.db")
        app = create_app()
        with TestClient(app) as client:
            self._complete_flow(client, app, conn, "Lumen")
        conn.close()

        assert app.state.personas["beacon"].name == "Lumen"

    def test_mate_persona_defaults_to_beacon_when_not_renamed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-007: no rename during the conversation keeps the default name."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "complete3.db")
        app = create_app()
        with TestClient(app) as client:
            self._complete_flow(client, app, conn, "Beacon")
        conn.close()

        assert app.state.personas["beacon"].name == "Beacon"

    def test_seed_memories_skipped_gracefully_without_a_wired_memory_stream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive branch: onboarding still completes without `mate_chat`."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "complete4.db")
        app = create_app()
        with TestClient(app) as client:
            app.state.db_conn = conn
            client.post("/api/settings", json={"locale": "en"})
            client.post("/api/settings", json={"keeper_name": "Rin"})
            response = client.post("/api/settings", json={"mate_name": "Beacon"})

        assert response.status_code == 200
        assert response.json()["step"] == "done"
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        conn.close()

    def test_restart_without_fresh_stays_done_and_never_duplicates_memories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-011: a normal restart against an onboarded world stays put."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        conn = open_db(tmp_path / "complete5.db")
        app = create_app()
        with TestClient(app) as client:
            self._complete_flow(client, app, conn, "Beacon")
        before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        # A normal (non-`--fresh`) restart: a brand-new app wired to the
        # same already-onboarded database.
        app2 = create_app()
        with TestClient(app2) as client2:
            app2.state.db_conn = conn
            status_response = client2.get("/api/onboarding")
            retry_response = client2.post(
                "/api/settings", json={"mate_name": "Someone Else"}
            )

        after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()

        assert status_response.json()["step"] == "done"
        assert retry_response.status_code == 409
        assert after == before
