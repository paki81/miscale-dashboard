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
