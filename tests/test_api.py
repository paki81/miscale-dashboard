import asyncio
import importlib
import os

import pytest
from fastapi.testclient import TestClient

from .packets import packet


def client(tmp_path):
    os.environ["MISCALE_DB"] = str(tmp_path / "test.db")
    os.environ["MISCALE_SCANNER"] = "off"
    import app.main as main
    importlib.reload(main)
    return TestClient(main.app), main


def test_profiles_measurements_and_settings(tmp_path):
    c, main = client(tmp_path)
    with c:
        assert c.get("/api/ping").json() == {"ok": True}
        p = c.post("/api/profiles", json={"name": "Alex", "height_cm": 178, "birth": "1990-01-01", "male": True}).json()
        mid = main.db.insert({"measured_at": "2026-10-12T07:30:00", "weight_kg": 80, "impedance": 500,
                              "profile_id": None, "metrics": {}})
        m = c.patch(f"/api/measurements/{mid}", json={"profile_id": p["id"]}).json()
        assert m["profile_id"] == p["id"] and m["metrics"]["body_fat"] > 0
        assert c.put("/api/settings", json={"unit": "lb"}).json()["unit"] == "lb"
        assert c.put("/api/settings", json={"unit": "stone"}).status_code == 422
        r = c.put(f"/api/profiles/{p['id']}/photo", content=b"\xff\xd8fake", headers={"content-type": "image/jpeg"})
        assert r.status_code == 200 and c.get(f"/api/profiles/{p['id']}/photo").content == b"\xff\xd8fake"
        bad = c.put(f"/api/profiles/{p['id']}/photo", content=b"x", headers={"content-type": "text/plain"})
        assert bad.status_code == 415
        c.delete(f"/api/profiles/{p['id']}")
        assert c.get("/api/measurements").json()[0]["profile_id"] is None


def test_websocket_status(tmp_path):
    c, _ = client(tmp_path)
    with c, c.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "status", "scanner": False, "scanner_error": None, "last_seen": None}


def test_pwa_files(tmp_path):
    c, _ = client(tmp_path)
    with c:
        m = c.get("/manifest.webmanifest")
        assert m.headers["content-type"].startswith("application/manifest+json")
        assert {i["purpose"] for i in m.json()["icons"]} == {"any", "maskable"}
        for icon in m.json()["icons"]:
            assert c.get(icon["src"]).status_code == 200
        assert c.get("/sw.js").headers["content-type"].startswith("text/javascript")
        assert c.get("/favicon.ico").status_code == 200


@pytest.fixture
def ingest_client(tmp_path, monkeypatch):
    monkeypatch.delenv("MISCALE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("MISCALE_SCALE_MAC", raising=False)
    c, main = client(tmp_path)
    with c:
        yield c, main


def test_ingest_records_measurement_and_broadcasts(ingest_client):
    c, main = ingest_client
    p = c.post("/api/profiles", json={"name": "Alex", "height_cm": 178, "birth": "1990-01-01"}).json()
    data = packet(80, impedance=500, stabilized=True).hex()
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["scanner"] is False
        response = c.post("/api/ingest", json={"data": data, "mac": "AA:BB:CC:DD:EE:FF", "rssi": -60})
        assert response.status_code == 200 and response.json() == {"ok": True}
        live = ws.receive_json()
        assert live["type"] == "live" and live["weight_kg"] == 80 and live["rssi"] == -60
        saved = ws.receive_json()
        assert saved["type"] == "measurement" and saved["event"] == "saved"
    measurements = c.get("/api/measurements").json()
    assert len(measurements) == 1
    m = measurements[0]
    assert m == saved["measurement"]
    assert m["weight_kg"] == 80 and m["impedance"] == 500
    assert m["address"] == "AA:BB:CC:DD:EE:FF" and m["raw"] == data
    assert m["profile_id"] == p["id"] and m["metrics"]["body_fat"] > 0
    assert main.listener.last_seen is not None
    logged = c.get("/api/debug/packets").json()
    assert len(logged) == 1 and logged[0]["rssi"] == -60 and logged[0]["raw"] == data


@pytest.mark.parametrize("data", ["zz" * 13, "00" * 12, "00" * 14, " " * 26, "00" * 12 + "  "])
def test_ingest_rejects_malformed_data(ingest_client, data):
    c, main = ingest_client
    assert c.post("/api/ingest", json={"data": data}).status_code == 422
    assert main.db.list() == [] and main.listener.devices == {}


def test_ingest_requires_configured_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MISCALE_INGEST_TOKEN", "test-ingest-token")
    monkeypatch.delenv("MISCALE_SCALE_MAC", raising=False)
    c, main = client(tmp_path)
    data = {"data": packet(80, stabilized=True).hex()}
    with c:
        assert c.post("/api/ingest", json=data).status_code == 401
        assert c.post("/api/ingest", json=data, headers={"X-Ingest-Token": "wrong"}).status_code == 401
        assert main.db.list() == [] and main.listener.devices == {}
        response = c.post("/api/ingest", json=data, headers={"X-Ingest-Token": "test-ingest-token"})
        assert response.status_code == 200 and response.json() == {"ok": True}
        m = c.get("/api/measurements").json()[0]
        assert m["address"] == "remote" and m["weight_kg"] == 80
        assert c.get("/api/debug/packets").json()[0]["rssi"] is None


def test_ingest_deduplicates_packets(ingest_client):
    c, main = ingest_client
    data = {"data": packet(80, impedance=500, stabilized=True).hex()}
    for _ in range(2):
        assert c.post("/api/ingest", json=data).status_code == 200
    assert len(main.db.list()) == 1
    assert len(main.db.packets()) == 1


def test_ingest_preserves_mac_filter(ingest_client, monkeypatch):
    c, main = ingest_client
    monkeypatch.setattr(main.listener, "address", "AA:BB:CC:DD:EE:FF")
    data = {"data": packet(80, stabilized=True).hex(), "mac": "11:22:33:44:55:66"}
    assert c.post("/api/ingest", json=data).status_code == 200
    assert main.db.list() == [] and main.db.packets() == []
    data["mac"] = "aa:bb:cc:dd:ee:ff"
    assert c.post("/api/ingest", json=data).status_code == 200
    assert len(main.db.list()) == 1


def test_ingest_updates_session_with_impedance(ingest_client):
    c, main = ingest_client
    for raw in (packet(80), packet(80, stabilized=True), packet(80, impedance=500, stabilized=True)):
        assert c.post("/api/ingest", json={"data": raw.hex()}).status_code == 200
    measurements = main.db.list()
    assert len(measurements) == 1 and measurements[0]["impedance"] == 500


@pytest.mark.parametrize("impedance", [None, 500])
def test_notifications_send_without_delaying_complete_measurements(ingest_client, monkeypatch, impedance):
    c, main = ingest_client
    calls, done = [], asyncio.Event()

    class Live:
        def __init__(self, *args, **kwargs):
            pass

        def update(self, text, **kwargs):
            calls.append(("draft", kwargs["measurement"]["impedance"]))

        async def close(self):
            calls.append(("closed", None))

    async def send(text, measurement=None, profile=None, **kwargs):
        calls.append(("final", measurement["impedance"]))
        done.set()
        return True

    async def wait():
        await asyncio.wait_for(done.wait(), 1)

    monkeypatch.setattr(main.notify, "enabled", lambda: True)
    monkeypatch.setattr(main.notify, "TelegramLive", Live, raising=False)
    monkeypatch.setattr(main.notify, "send", send)
    monkeypatch.setattr(main, "NOTIFY_WAIT", 60 if impedance else 0)
    c.post("/api/profiles", json={"name": "Alex", "height_cm": 178, "birth": "1990-01-01"})
    data = {"data": packet(80, impedance=impedance, stabilized=True).hex()}
    assert c.post("/api/ingest", json=data).status_code == 200
    c.portal.call(wait)
    assert calls[-2:] == [("closed", None), ("final", impedance)]
    assert ([call for call in calls if call[0] == "draft"] == [("draft", None)]) is (impedance is None)
    assert main.waiting == {}


def test_impedance_update_completes_same_live_notification(ingest_client, monkeypatch):
    c, main = ingest_client
    started, done = asyncio.Event(), asyncio.Event()
    final = []

    class Live:
        def __init__(self, *args, **kwargs):
            pass

        def update(self, *args, **kwargs):
            started.set()

        async def close(self):
            pass

    async def send(text, measurement=None, profile=None, **kwargs):
        final.append(measurement)
        done.set()
        return True

    async def wait(event):
        await asyncio.wait_for(event.wait(), 1)

    monkeypatch.setattr(main.notify, "enabled", lambda: True)
    monkeypatch.setattr(main.notify, "TelegramLive", Live, raising=False)
    monkeypatch.setattr(main.notify, "send", send)
    monkeypatch.setattr(main, "NOTIFY_WAIT", 60)
    c.post("/api/profiles", json={"name": "Alex"})
    assert c.post("/api/ingest", json={"data": packet(80, stabilized=True).hex()}).status_code == 200
    c.portal.call(wait, started)
    complete = {"data": packet(80, impedance=500, stabilized=True).hex()}
    assert c.post("/api/ingest", json=complete).status_code == 200
    c.portal.call(wait, done)
    assert c.post("/api/ingest", json=complete).status_code == 200
    assert len(final) == 1 and final[0]["impedance"] == 500
    assert len(main.db.list()) == 1 and main.waiting == {}


def test_unrecognized_weigh_in_is_not_notified(ingest_client, monkeypatch):
    c, main = ingest_client
    calls = []

    class Live:
        def __init__(self, *args, **kwargs):
            pass

        def update(self, *args, **kwargs):
            calls.append("draft")

        async def close(self):
            pass

    async def send(*args, **kwargs):
        calls.append("final")

    monkeypatch.setattr(main.notify, "enabled", lambda: True)
    monkeypatch.setattr(main.notify, "TelegramLive", Live)
    monkeypatch.setattr(main.notify, "send", send)
    assert c.post("/api/ingest", json={"data": packet(80, stabilized=True).hex()}).status_code == 200
    c.portal.call(asyncio.sleep, 0)
    assert len(main.db.list()) == 1 and main.db.list()[0]["profile_id"] is None
    assert calls == [] and main.waiting == {}


def test_profile_notification_fields(tmp_path):
    c, main = client(tmp_path)
    with c:
        p = c.post("/api/profiles", json={"name": "Alex", "telegram_chat_id": "456", "notify": False}).json()
        assert p["telegram_chat_id"] == "456" and p["notify"] is False
        p = c.put(f"/api/profiles/{p['id']}", json={"name": "Alex", "notify": True}).json()
        assert p["notify"] is True and p["telegram_chat_id"] is None


def test_db_migrates_profiles_without_notification_columns(tmp_path):
    import sqlite3

    from app.db import DB
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE profiles (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "male INTEGER NOT NULL DEFAULT 1)")
    conn.execute("INSERT INTO profiles (name) VALUES ('Alex')")
    conn.commit()
    conn.close()
    db = DB(str(path))
    p = db.profile(1)
    assert p["name"] == "Alex" and p["notify"] is True and p["telegram_chat_id"] is None


def test_notification_goes_to_profile_chat(ingest_client, monkeypatch):
    c, main = ingest_client
    done = asyncio.Event()
    calls = []

    class Live:
        def __init__(self, chat_id=None):
            calls.append(("live", chat_id))

        def update(self, *args, **kwargs):
            pass

        async def close(self):
            pass

    async def send(text, measurement=None, profile=None, **kwargs):
        calls.append(("final", kwargs.get("chat_id")))
        done.set()
        return True

    async def wait():
        await asyncio.wait_for(done.wait(), 1)

    monkeypatch.setattr(main.notify, "enabled", lambda: True)
    monkeypatch.setattr(main.notify, "TelegramLive", Live)
    monkeypatch.setattr(main.notify, "send", send)
    c.post("/api/profiles", json={"name": "Alex", "telegram_chat_id": "456"})
    data = {"data": packet(80, impedance=500, stabilized=True).hex()}
    assert c.post("/api/ingest", json=data).status_code == 200
    c.portal.call(wait)
    assert ("live", "456") in calls and ("final", "456") in calls


def test_notifications_off_for_profile(ingest_client, monkeypatch):
    c, main = ingest_client
    calls = []

    class Live:
        def __init__(self, *args, **kwargs):
            calls.append("live")

        def update(self, *args, **kwargs):
            calls.append("draft")

        async def close(self):
            pass

    async def send(*args, **kwargs):
        calls.append("final")

    monkeypatch.setattr(main.notify, "enabled", lambda: True)
    monkeypatch.setattr(main.notify, "TelegramLive", Live)
    monkeypatch.setattr(main.notify, "send", send)
    c.post("/api/profiles", json={"name": "Alex", "notify": False})
    data = {"data": packet(80, impedance=500, stabilized=True).hex()}
    assert c.post("/api/ingest", json=data).status_code == 200
    c.portal.call(asyncio.sleep, 0.05)
    assert calls == [] and main.waiting == {}


def test_profile_updates_keep_exported_metrics_without_impedance(tmp_path):
    c, main = client(tmp_path)
    with c:
        p = c.post("/api/profiles", json={"name": "Alex", "height_cm": 178, "birth": "1990-01-01"}).json()
        metrics = {"bmi": 25.2, "body_fat": 20, "muscle_mass": 60}
        imported = main.db.insert({"measured_at": "2024-01-01T08:00:00", "weight_kg": 80,
                                   "impedance": None, "profile_id": p["id"], "metrics": metrics,
                                   "address": "IMPORT:ZEPP_LIFE"})
        native = main.db.insert({"measured_at": "2024-01-02T08:00:00", "weight_kg": 80,
                                 "impedance": None, "profile_id": p["id"], "metrics": {"bmi": 25.2}})
        response = c.put(f"/api/profiles/{p['id']}", json={"name": "Alex", "height_cm": 180, "birth": "1990-01-01"})
        assert response.status_code == 200
        assert main.db.get(imported)["metrics"] == metrics
        assert main.db.get(native)["metrics"]["bmi"] != 25.2
