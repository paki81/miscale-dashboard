import asyncio
import json
import urllib.error
from io import BytesIO

import pytest

from app import notify


def test_message_kg_and_lb():
    m = {"measured_at": "2026-10-12T07:30:00", "weight_kg": 80.0, "impedance": 500,
         "metrics": {"body_fat": 20.0, "fat_mass": 16.0, "muscle_mass": 60.0, "water": 55.0, "protein": 18.0,
                     "bone_mass": 3.0, "bmi": 25.2, "visceral_fat": 9, "bmr": 1700}}
    prev = {"measured_at": "2026-10-11T07:30:00", "weight_kg": 80.5}
    profile = {"name": "Alex", "height_cm": 178, "birth": "1990-01-01"}
    text = notify.message(m, profile, [prev], "kg", "en")
    assert text.startswith("⚖️ Alex · 80.00 kg")
    assert "−0.50 kg since" in text and "Impedance 500 Ω" in text
    assert "176.37 lb" in notify.message(m, profile, [], "lb", "en")
    assert "80,00 kg" in notify.message(m, profile, [], "kg", "pt-BR")
    italian = notify.message(m, profile, [prev], "kg", "it")
    assert "80,00 kg" in italian and "−0,50 kg rispetto al 11/10 07:30" in italian
    assert "Grasso corporeo 20,0%" in italian and "Impedenza 500 Ω" in italian
    assert "Metabolismo basale 1.700 kcal" in italian


def test_unknown_person():
    m = {"measured_at": "2026-10-12T07:30:00", "weight_kg": 80.0, "metrics": {}}
    assert "doesn't match any profile" in notify.message(m, None, [], "kg", "en")
    assert "non corrisponde a nessun profilo" in notify.message(m, None, [], "kg", "it")


def test_italian_analysis_message():
    a = {"week_start": "2026-10-04", "week_end": "2026-10-10", "text": "Il peso medio è diminuito."}
    text = notify.analysis_message(a, {"name": "Alex"}, "it")
    assert "Analisi settimanale · Alex (04/10 – 10/10)" in text
    assert a["text"] in text


@pytest.fixture
def telegram_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(notify, "TELEGRAM_CHAT", "123")
    monkeypatch.setattr(notify, "TELEGRAM_FORMAT", "rich", raising=False)
    monkeypatch.setattr(notify, "TELEGRAM_LIVE", True, raising=False)
    monkeypatch.setattr(notify, "_rich_available", True, raising=False)
    monkeypatch.setattr(notify, "_telegram_after", 0, raising=False)
    monkeypatch.setattr(notify, "PUBLIC_URL", "http://dashboard.local:8800")
    monkeypatch.setattr(notify, "WEBHOOK", "")

    def post(url, payload):
        calls.append((url.rsplit("/", 1)[-1], payload))
        return b'{"ok": true, "result": {"message_id": 42}}'

    monkeypatch.setattr(notify, "_post", post)
    return calls


@pytest.fixture
def measurement():
    return {"id": 1, "measured_at": "2026-10-12T07:30:00", "weight_kg": 80.0, "impedance": 500,
            "metrics": {"body_fat": 20.0, "fat_mass": 16.0, "muscle_mass": 60.0, "water": 55.0,
                        "protein": 18.0, "bone_mass": 3.0, "bmi": 25.2, "visceral_fat": 9, "bmr": 1700}}


def test_rich_measurement_escapes_names_and_formats_metrics(measurement, monkeypatch):
    monkeypatch.setattr(notify, "PUBLIC_URL", "")
    profile = {"name": "Alex <admin> & friends", "height_cm": 178, "birth": "1990-01-01"}
    history = [{"measured_at": "2026-10-11T07:30:00", "weight_kg": 80.5}]
    rich, classic = notify.telegram_content("plain", measurement, profile, unit="kg", history=history, lang="it")
    assert "Alex &lt;admin&gt; &amp; friends" in rich and "<admin>" not in rich
    assert "80,00 kg" in rich and "−0,50 kg" in rich
    assert "<table striped compact>" in rich and "<details>" in rich
    assert "Grasso corporeo" in rich and "20,0%" in rich and "500 Ω" in rich
    assert "<table" not in classic and "<h2>" not in classic
    assert "<b>" in classic and "80,00 kg" in classic
    lb, _ = notify.telegram_content("plain", measurement, profile, unit="lb", lang="en")
    assert "176.37 lb" in lb


def test_rich_pending_and_missing_impedance(measurement):
    measurement["impedance"] = None
    measurement["metrics"] = {"bmi": 25.2}
    profile = {"name": "Alex", "height_cm": 178, "birth": "1990-01-01"}
    pending, _ = notify.telegram_content("plain", measurement, profile, pending=True, lang="it")
    final, _ = notify.telegram_content("plain", measurement, profile, lang="it")
    assert "composizione corporea in corso" in pending
    assert "Nessuna impedenza" in final and "Grasso corporeo" not in final


def test_rich_analysis_escapes_provider_text():
    a = {"week_start": "2026-10-04", "week_end": "2026-10-10", "lang": "it",
         "text": "Cosa è migliorato\n• Peso medio in calo.\n\n<bad> & testo", "model": "test-model"}
    rich, classic = notify.telegram_content("plain", profile={"name": "Alex"}, analysis=a)
    assert "Analisi settimanale" in rich and "04/10" in rich and "10/10" in rich
    assert "<h3>Cosa è migliorato</h3>" in rich
    assert "&lt;bad&gt; &amp; testo" in rich and "<bad>" not in rich
    assert "test-model" in rich and "&lt;bad&gt;" in classic


def test_telegram_rich_final_and_draft(telegram_calls):
    assert asyncio.run(notify.telegram("Test <unsafe>"))
    assert asyncio.run(notify.telegram("Draft", draft_id=7))
    assert [method for method, _ in telegram_calls] == ["sendRichMessage", "sendRichMessageDraft"]
    assert "&lt;unsafe&gt;" in telegram_calls[0][1]["rich_message"]["html"]
    assert telegram_calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["url"] == "http://dashboard.local:8800"
    assert telegram_calls[1][1]["draft_id"] == 7 and "reply_markup" not in telegram_calls[1][1]


def test_telegram_html_mode_and_private_chat_only(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_FORMAT", "html")
    assert asyncio.run(notify.telegram("Test"))
    assert telegram_calls[0][0] == "sendMessage"
    assert telegram_calls[0][1]["parse_mode"] == "HTML"
    assert telegram_calls[0][1]["link_preview_options"] == {"is_disabled": True}
    monkeypatch.setattr(notify, "TELEGRAM_CHAT", "-123")
    assert not asyncio.run(notify.telegram("Draft", draft_id=7))
    assert len(telegram_calls) == 1


def test_telegram_falls_back_when_rich_is_unavailable(telegram_calls, monkeypatch):
    original = notify._post

    def post(url, payload):
        if url.endswith("sendRichMessage"):
            telegram_calls.append(("sendRichMessage", payload))
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, BytesIO(b'{"ok": false, "error_code": 404}'))
        return original(url, payload)

    monkeypatch.setattr(notify, "_post", post)
    assert asyncio.run(notify.telegram("Test"))
    assert asyncio.run(notify.telegram("Next"))
    assert [method for method, _ in telegram_calls] == ["sendRichMessage", "sendMessage", "sendMessage"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_telegram_errors_do_not_leak_token_or_send_twice(telegram_calls, monkeypatch, caplog, status):
    def post(url, payload):
        telegram_calls.append((url.rsplit("/", 1)[-1], payload))
        body = json.dumps({"ok": False, "error_code": status, "parameters": {"retry_after": 0}}).encode()
        raise urllib.error.HTTPError(url, status, "secret test-token", {}, BytesIO(body))

    monkeypatch.setattr(notify, "_post", post)
    assert not asyncio.run(notify.telegram("Test"))
    assert len(telegram_calls) == 1
    assert "test-token" not in caplog.text and "api.telegram.org/bot" not in caplog.text


def test_telegram_checks_json_error_responses(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "_post", lambda *_: b'{"ok": false, "error_code": 401}')
    assert not asyncio.run(notify.telegram("Test"))


def test_webhook_keeps_plain_text_and_original_payload(telegram_calls, monkeypatch, measurement):
    calls = []
    monkeypatch.setattr(notify, "WEBHOOK", "http://webhook.local")
    original = notify._post

    def post(url, payload):
        if url == "http://webhook.local":
            calls.append(payload)
            return b"ok"
        return original(url, payload)

    monkeypatch.setattr(notify, "_post", post)
    profile = {"id": 1, "name": "Alex", "height_cm": 178, "birth": "1990-01-01"}
    assert asyncio.run(notify.send("Plain text", measurement, profile))
    assert calls == [{"event": "measurement", "text": "Plain text", "message": "Plain text",
                      "profile": {"id": 1, "name": "Alex"}, "measurement": measurement}]
    assert telegram_calls[0][0] == "sendRichMessage"


def test_telegram_live_coalesces_updates_and_closes(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "DRAFT_INTERVAL", 0.01, raising=False)

    async def run():
        sent = []
        first = asyncio.Event()
        last = asyncio.Event()

        async def telegram(text, **kwargs):
            sent.append((text, kwargs["draft_id"]))
            first.set()
            if text == "Last":
                last.set()
            return True

        monkeypatch.setattr(notify, "telegram", telegram)
        live = notify.TelegramLive()
        live.update("First")
        await asyncio.wait_for(first.wait(), 1)
        for _ in range(20):
            live.update("Intermediate")
        live.update("Last")
        await asyncio.wait_for(last.wait(), 1)
        await live.close()
        live.update("Ignored")
        assert [text for text, _ in sent] == ["First", "Last"]
        assert len({draft for _, draft in sent}) == 1 and sent[0][1] > 0

    asyncio.run(run())


def test_live_close_waits_for_inflight_draft(telegram_calls, monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def telegram(*args, **kwargs):
            started.set()
            await release.wait()
            return True

        monkeypatch.setattr(notify, "telegram", telegram)
        live = notify.TelegramLive()
        live.update("Draft")
        await asyncio.wait_for(started.wait(), 1)
        closing = asyncio.create_task(live.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 1)

    asyncio.run(run())


def test_live_disabled_does_not_send(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_LIVE", False)

    async def run():
        live = notify.TelegramLive()
        live.update("Ignored")
        await live.close()

    asyncio.run(run())
    assert telegram_calls == []


def test_rich_draft_falls_back_to_html_draft(telegram_calls, monkeypatch):
    original = notify._post

    def post(url, payload):
        if url.endswith("sendRichMessageDraft"):
            telegram_calls.append(("sendRichMessageDraft", payload))
            return b'{"ok": false, "error_code": 404}'
        return original(url, payload)

    monkeypatch.setattr(notify, "_post", post)
    assert asyncio.run(notify.telegram("Draft", draft_id=9))
    assert [method for method, _ in telegram_calls] == ["sendRichMessageDraft", "sendMessageDraft"]
    assert telegram_calls[-1][1]["draft_id"] == 9


def test_transport_failure_is_not_retried(telegram_calls, monkeypatch, caplog):
    calls = []

    def post(url, payload):
        calls.append(True)
        raise urllib.error.URLError("test-token secret")

    monkeypatch.setattr(notify, "_post", post)
    assert not asyncio.run(notify.telegram("Test"))
    assert calls == [True] and "test-token" not in caplog.text


def test_retry_after_blocks_drafts_and_delays_next_final(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify.time, "monotonic", lambda: 10)
    monkeypatch.setattr(notify, "_telegram_after", 15)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(notify.asyncio, "sleep", sleep)
    assert not asyncio.run(notify.telegram("Draft", draft_id=1))
    assert telegram_calls == [] and delays == []
    assert asyncio.run(notify.telegram("Final"))
    assert delays == [5] and len(telegram_calls) == 1


def test_no_dashboard_button_for_invalid_url(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "PUBLIC_URL", "javascript:alert(1)")
    assert asyncio.run(notify.telegram("Test"))
    assert "reply_markup" not in telegram_calls[0][1]


def test_long_analysis_is_shortened_only_for_telegram():
    a = {"week_start": "2026-10-04", "week_end": "2026-10-10", "lang": "it", "text": "Long text " * 1000}
    rich, classic = notify.telegram_content("plain", profile={"name": "Alex"}, analysis=a)
    assert len(classic) < 4096 and "…" in rich
    assert len(a["text"]) == 10000
