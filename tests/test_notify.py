import asyncio
import json
import subprocess
import urllib.error
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import pytest

from app import notify, telegram_card


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


def test_telegram_routes_to_profile_chat(telegram_calls):
    assert asyncio.run(notify.telegram("Test", chat_id="456"))
    assert telegram_calls[0][1]["chat_id"] == 456


def test_profile_chat_works_without_a_default_chat(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_CHAT", "")
    assert notify.enabled()   # a profile can still carry its own chat
    assert not asyncio.run(notify.telegram("Test"))   # no destination: skipped, not an error
    assert telegram_calls == []
    assert asyncio.run(notify.telegram("Test", chat_id="456"))
    assert telegram_calls[0][1]["chat_id"] == 456


def test_live_routes_drafts_to_profile_chat(telegram_calls, monkeypatch):
    monkeypatch.setattr(notify, "DRAFT_INTERVAL", 0.01, raising=False)

    async def run():
        live = notify.TelegramLive("456")
        live.update("Draft")
        for _ in range(50):
            if telegram_calls:
                break
            await asyncio.sleep(0.01)
        await live.close()

    asyncio.run(run())
    assert telegram_calls and all(p["chat_id"] == 456 for _, p in telegram_calls)


def test_long_analysis_is_shortened_only_for_telegram():
    a = {"week_start": "2026-10-04", "week_end": "2026-10-10", "lang": "it", "text": "Long text " * 1000}
    rich, classic = notify.telegram_content("plain", profile={"name": "Alex"}, analysis=a)
    assert len(classic) < 4096 and "…" in rich
    assert len(a["text"]) == 10000


def test_graphic_card_uses_geopieno_palette_and_safe_text(measurement):
    fonts = {"manrope": Path("Manrope.woff"), "sora": Path("Sora.woff")}
    profile = {"name": 'Alex </text><image href="https://bad.local"/>', "height_cm": 178, "birth": "1990-01-01"}
    source = telegram_card.svg(measurement, profile, lang="it", fonts=fonts)
    root = ET.fromstring(source)
    assert root.attrib["width"] == "960" and int(root.attrib["height"]) < 3500
    assert "#0f7a63" in source and "#ed6a3b" in source and "#fbf6ef" in source
    assert "Manrope.woff" in source and "Sora.woff" in source
    assert "&lt;/text&gt;" in source and not root.findall(".//{http://www.w3.org/2000/svg}image")
    assert "80,00" in source and "Grasso corporeo" in source and "500 ohm" in source


def test_graphic_card_handles_missing_metrics_and_analysis(measurement):
    fonts = {"manrope": Path("Manrope.woff"), "sora": Path("Sora.woff")}
    measurement["metrics"] = {}
    measurement["impedance"] = None
    source = telegram_card.svg(measurement, {"name": "Alex"}, fonts=fonts, lang="it")
    assert "Nessuna impedenza" in source and "Imposta altezza" in source
    a = {"week_start": "2026-10-04", "week_end": "2026-10-10", "lang": "it",
         "text": "Cosa è migliorato\nPeso & muscoli <test>", "model": "test-model"}
    source = telegram_card.svg(profile={"name": "Alex"}, analysis=a, fonts=fonts)
    ET.fromstring(source)
    assert "ANALISI SETTIMANALE" in source and "class=\"section\"" in source
    assert "&amp;" in source and "&lt;test&gt;" in source


def test_font_cache_downloads_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_card, "FONT_DIR", tmp_path / "fonts")
    calls = []
    font = b"wOFF" + b"\0" * 4 + (120).to_bytes(4, "big") + b"\0" * 4 + (1000).to_bytes(4, "big") + b"\0" * 100

    def open_url(url, **kwargs):
        calls.append(url)
        return BytesIO(font)

    monkeypatch.setattr(telegram_card.urllib.request, "urlopen", open_url)
    paths = telegram_card.prepare_fonts()
    assert all(path.read_bytes() == font for path in paths.values())
    assert telegram_card.prepare_fonts() == paths and len(calls) == 2


def test_font_cache_rejects_non_fonts(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_card, "FONT_DIR", tmp_path / "fonts")
    monkeypatch.setattr(telegram_card.urllib.request, "urlopen", lambda *a, **k: BytesIO(b"<html>error</html>"))
    with pytest.raises(ValueError, match="Invalid card font"):
        telegram_card.prepare_fonts()
    assert not list((tmp_path / "fonts").glob("*.woff"))


def test_renderer_is_bounded_and_does_not_inherit_tokens(monkeypatch):
    monkeypatch.setattr(telegram_card.shutil, "which", lambda *_: "/usr/bin/convert")
    monkeypatch.setattr(telegram_card, "svg", lambda **kwargs: "<svg/>")
    monkeypatch.setenv("MISCALE_TELEGRAM_TOKEN", "private-test-token")
    calls = []
    image = telegram_card.PNG + b"test"

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, image, b"")

    monkeypatch.setattr(telegram_card.subprocess, "run", run)
    assert telegram_card.render() == image
    assert calls[0][1]["timeout"] == 20 and "MSVG:-" in calls[0][0]
    assert "MISCALE_TELEGRAM_TOKEN" not in calls[0][1]["env"]


def test_photo_mode_sends_final_photo_but_keeps_text_drafts(telegram_calls, monkeypatch, measurement):
    monkeypatch.setattr(notify, "TELEGRAM_FORMAT", "photo")
    monkeypatch.setattr(telegram_card, "render", lambda **kwargs: telegram_card.PNG + b"example")
    profile = {"name": "Alex", "height_cm": 178, "birth": "1990-01-01"}
    assert asyncio.run(notify.telegram("Final", measurement=measurement, profile=profile))
    assert telegram_calls[0][0] == "sendPhoto" and isinstance(telegram_calls[0][1]["photo"], bytes)
    assert "reply_markup" in telegram_calls[0][1]
    assert asyncio.run(notify.telegram("Pending", draft_id=17, measurement=measurement, profile=profile, pending=True))
    assert telegram_calls[-1][0] == "sendRichMessageDraft" and "photo" not in telegram_calls[-1][1]


def test_photo_renderer_failure_falls_back_to_text(telegram_calls, monkeypatch, measurement, caplog):
    monkeypatch.setattr(notify, "TELEGRAM_FORMAT", "photo")

    def render(**kwargs):
        raise OSError("private-test-token")

    monkeypatch.setattr(telegram_card, "render", render)
    assert asyncio.run(notify.telegram("Final", measurement=measurement, profile={"name": "Alex"}))
    assert telegram_calls[0][0] == "sendRichMessage"
    assert "private-test-token" not in caplog.text


def test_photo_rejection_falls_back_without_duplicate(telegram_calls, monkeypatch, measurement):
    monkeypatch.setattr(notify, "TELEGRAM_FORMAT", "photo")
    monkeypatch.setattr(telegram_card, "render", lambda **kwargs: telegram_card.PNG + b"example")
    original = notify._post

    def post(url, payload):
        if url.endswith("sendPhoto"):
            telegram_calls.append(("sendPhoto", payload))
            return b'{"ok": false, "error_code": 400}'
        return original(url, payload)

    monkeypatch.setattr(notify, "_post", post)
    assert asyncio.run(notify.telegram("Final", measurement=measurement, profile={"name": "Alex"}))
    assert [method for method, _ in telegram_calls] == ["sendPhoto", "sendRichMessage"]


def test_photo_upload_uses_multipart_without_external_request(monkeypatch):
    requests = []

    def open_url(request, **kwargs):
        requests.append(request)
        return BytesIO(b'{"ok": true}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", open_url)
    image = telegram_card.PNG + b"image"
    response = notify._post("https://example.local/sendPhoto", {"chat_id": 123, "photo": image,
                            "caption": "Test", "reply_markup": {"inline_keyboard": []}})
    assert json.loads(response)["ok"]
    request = requests[0]
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert image in request.data and b'filename="card.png"' in request.data
    assert b'"inline_keyboard": []' in request.data
