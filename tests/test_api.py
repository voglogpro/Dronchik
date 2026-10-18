import importlib

from fastapi.testclient import TestClient


def make_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO", "0")
    monkeypatch.setenv("ACCESS_CODES", "test-code-123")
    monkeypatch.setenv("TG_API_ID", "")
    monkeypatch.setenv("INGEST_TOKEN", "secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TG_CHANNELS_FILE", str(tmp_path / "none.json"))
    import app.main
    return TestClient(importlib.reload(app.main).app)


def test_ingest_and_state(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path) as client:
        r = client.post("/api/ingest", json={"channel": "x", "text": "🛵 2 БПЛА над Курчатовым курсом на Курск"})
        assert r.status_code == 401

        headers = {"Authorization": "Bearer secret"}
        r = client.post("/api/ingest", json={"channel": "x", "text": "🛵 2 БПЛА над Курчатовым курсом на Курск"}, headers=headers)
        assert r.status_code == 200 and r.json()["events"][0]["target"] == "Курск"

        # без кода — публичная карта: тревога по области и значок «!», но ни дронов, ни прогноза
        state = client.get("/api/state").json()
        assert state["mode"] == "public" and state["regions"]["kursk-oblast"]["status"] == "uav"
        assert state["tracks"] == [] and state["launches"] == []
        assert not any(r["status"] == "predicted" for r in state["regions"].values())
        assert any(m["region"] == "kursk-oblast" for m in state["alert_marks"])
        assert client.get("/api/heatmap?days=7").status_code == 403
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert "kursk-oblast" in msg["regions"] and msg["tracks"] == []

        assert client.post("/api/access", json={"code": "wrong"}).status_code == 403
        assert client.post("/api/access", json={"code": "test-code-123"}).status_code == 200
        state = client.get("/api/state").json()
        assert state["mode"] == "full" and state["tracks"][0]["count"] == 2
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["tracks"]

        assert client.get("/api/regions").json()["type"] == "FeatureCollection"
        assert "APP_CONFIG" in client.get("/config.js").text
        assert client.get("/").status_code == 200
        assert "Пользовательское соглашение" in client.get("/terms.html").text
        assert '"termsVersion"' in client.get("/config.js").text
        heat = client.get("/api/heatmap?days=7").json()
        assert heat["total"] >= 1 and heat["points"][0]["place"] == "Курчатов"
        assert client.get("/quotes.json").json()["items"]
        assert client.get("/api/health").json()["mode"] == "none"
