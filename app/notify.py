"""Optional notification for every new weigh-in (and the weekly analysis): a generic webhook and/or Telegram.

- MISCALE_WEBHOOK_URL: receives a JSON POST with the text and the measurement (works with
  n8n, Node-RED, Home Assistant webhooks, Gotify bridges...).
- MISCALE_TELEGRAM_TOKEN + MISCALE_TELEGRAM_CHAT_ID: sends the text with your own bot.
"""

import asyncio
import json
import logging
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html import escape

log = logging.getLogger("miscale.notify")

WEBHOOK = os.environ.get("MISCALE_WEBHOOK_URL", "")
TELEGRAM_TOKEN = os.environ.get("MISCALE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("MISCALE_TELEGRAM_CHAT_ID", "")
TELEGRAM_FORMAT = os.environ.get("MISCALE_TELEGRAM_FORMAT", "rich").lower()
TELEGRAM_LIVE = os.environ.get("MISCALE_TELEGRAM_LIVE", "on").lower() not in ("off", "0", "false", "no")
DRAFT_INTERVAL = 2.0
_rich_available = True
_telegram_after = 0.0
PUBLIC_URL = os.environ.get("MISCALE_PUBLIC_URL", "")
LANG = os.environ.get("MISCALE_LANG", "en")
LB = 0.45359237

TEXT = {
    "en": {
        "unknown": "⚖️ {w} without an owner: the weight doesn't match any profile.\nPick who it was in the history.",
        "since": "{d} since {when}", "avg": "7-day average {w}",
        "fat": "Body fat {fat}% ({mass}) · Muscle {muscle}",
        "water": "Water {water}% · Protein {protein}% · Bone {bone}",
        "bmi": "BMI {bmi} · Visceral fat {visceral} · BMR {bmr} kcal",
        "impedance": "Impedance {imp} Ω",
        "no_impedance": "No impedance (step on barefoot and wait for the measurement to finish).",
        "no_height": "Set height and birth date in the profile to see body composition.",
        "date": "%b %d %H:%M",
        "analysis": "🧠 Weekly analysis · {name} ({start} – {end})", "day": "%b %d",
    },
    "pt-BR": {
        "unknown": "⚖️ Pesagem de {w} sem dono: o peso não bate com nenhum perfil.\nEscolha a pessoa no histórico.",
        "since": "{d} desde {when}", "avg": "média 7 dias {w}",
        "fat": "Gordura {fat}% ({mass}) · Músculo {muscle}",
        "water": "Água {water}% · Proteína {protein}% · Ossos {bone}",
        "bmi": "IMC {bmi} · Gordura visceral {visceral} · Metabolismo basal {bmr} kcal",
        "impedance": "Impedância {imp} Ω",
        "no_impedance": "Sem impedância (suba descalço e espere o fim da medição).",
        "no_height": "Cadastre altura e nascimento no perfil para ver a composição corporal.",
        "date": "%d/%m %H:%M",
        "analysis": "🧠 Análise da semana · {name} ({start} a {end})", "day": "%d/%m",
    },
    "it": {
        "unknown": "Rilevazione di {w} senza proprietario: il peso non corrisponde a nessun profilo.\n"
                   "Scegli la persona nella cronologia.",
        "since": "{d} rispetto al {when}", "avg": "media di 7 giorni {w}",
        "fat": "Grasso corporeo {fat}% ({mass}) · Muscoli {muscle}",
        "water": "Acqua {water}% · Proteine {protein}% · Massa ossea {bone}",
        "bmi": "IMC {bmi} · Grasso viscerale {visceral} · Metabolismo basale {bmr} kcal",
        "impedance": "Impedenza {imp} Ω",
        "no_impedance": "Nessuna impedenza (sali a piedi nudi e attendi la fine della misurazione).",
        "no_height": "Imposta altezza e data di nascita nel profilo per vedere la composizione corporea.",
        "date": "%d/%m %H:%M",
        "analysis": "Analisi settimanale · {name} ({start} – {end})", "day": "%d/%m",
    },
}


def enabled() -> bool:
    # A bot token alone is enough: a profile can carry its own chat_id.
    return bool(WEBHOOK or TELEGRAM_TOKEN)


def _num(v, d=1, lang=LANG):
    s = f"{v:,.{d}f}"
    if lang.startswith(("pt", "it")):
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def _mass(kg, unit, d=1, lang=LANG):
    return f"{_num(kg / LB, d, lang)} lb" if unit == "lb" else f"{_num(kg, d, lang)} kg"


def _link(m: dict | None) -> str:
    """The public dashboard URL, deep-linked to a measurement's results view when possible."""
    if not PUBLIC_URL or not m or not isinstance(m.get("id"), int) or m["id"] < 0:
        return PUBLIC_URL
    return f"{PUBLIC_URL}{'&' if '?' in PUBLIC_URL else '?'}m={m['id']}"


def _prev(m: dict, history: list[dict]) -> dict | None:
    when = datetime.fromisoformat(m["measured_at"])
    return next((h for h in history if when - datetime.fromisoformat(h["measured_at"]) > timedelta(minutes=3)), None)


def _trend(m: dict, history: list[dict], unit: str, lang: str) -> list[str]:
    t = TEXT.get(lang, TEXT["en"])
    when = datetime.fromisoformat(m["measured_at"])
    prev = _prev(m, history)
    trend = []
    if prev:
        d = m["weight_kg"] - prev["weight_kg"]
        trend.append(t["since"].format(d=("+" if d >= 0 else "−") + _mass(abs(d), unit, 2, lang),
                                       when=datetime.fromisoformat(prev["measured_at"]).strftime(t["date"])))
    week = [h["weight_kg"] for h in [m, *history] if when - datetime.fromisoformat(h["measured_at"]) < timedelta(days=7)]
    if len(week) > 1:
        trend.append(t["avg"].format(w=_mass(sum(week) / len(week), unit, 1, lang)))
    return trend


def _delta(cur, prev, suffix: str, decimals: int, unit: str, lang: str) -> str:
    if cur is None or prev is None:
        return "—"
    raw_diff = (cur - prev) / LB if suffix == "mass" and unit == "lb" else cur - prev
    eps = 0.5 * 10 ** -decimals
    if abs(raw_diff) < max(eps, 1e-9):
        return "="
    arrow = "▲" if raw_diff > 0 else "▼"
    val = _mass(abs(cur - prev), unit, decimals, lang) if suffix == "mass" else _num(abs(raw_diff), decimals, lang) + suffix
    return f"{arrow} {val}"


def message(m: dict, profile: dict | None, history: list[dict], unit: str = "kg", lang: str = LANG) -> str:
    """`history` holds the same person's previous measurements, newest first."""
    t = TEXT.get(lang, TEXT["en"])
    x = m.get("metrics") or {}
    if not profile:
        return t["unknown"].format(w=_mass(m["weight_kg"], unit, 2, lang))
    lines = [f"⚖️ {profile['name']} · {_mass(m['weight_kg'], unit, 2, lang)}"]

    trend = _trend(m, history, unit, lang)
    if trend:
        lines.append(" · ".join(trend))

    if x.get("body_fat") is not None:
        lines.append(t["fat"].format(fat=_num(x["body_fat"], 1, lang), mass=_mass(x["fat_mass"], unit, 1, lang),
                                     muscle=_mass(x["muscle_mass"], unit, 1, lang)))
        lines.append(t["water"].format(water=_num(x["water"], 1, lang), protein=_num(x["protein"], 1, lang),
                                       bone=_mass(x["bone_mass"], unit, 1, lang)))
    if x.get("bmi") is not None:
        lines.append(t["bmi"].format(bmi=_num(x["bmi"], 1, lang), visceral=_num(x["visceral_fat"], 0, lang),
                                     bmr=_num(x["bmr"], 0, lang)))
    if m.get("impedance"):
        lines.append(t["impedance"].format(imp=m["impedance"]))
    elif x.get("bmi") is not None:
        lines.append(t["no_impedance"])
    if not profile.get("height_cm") or not profile.get("birth"):
        lines.append(t["no_height"])
    return "\n".join(lines)


def analysis_message(a: dict, profile: dict, lang: str = LANG) -> str:
    t = TEXT.get(lang, TEXT["en"])
    start, end = (datetime.fromisoformat(a[k]).strftime(t["day"]) for k in ("week_start", "week_end"))
    lines = [t["analysis"].format(name=profile["name"], start=start, end=end), "", a["text"]]
    return "\n".join(lines)


METRIC_FIELDS = (("body_fat", "%", 1), ("muscle_mass", "mass", 1), ("water", "%", 1), ("bmi", "", 1),
                 ("protein", "%", 1), ("bone_mass", "mass", 1), ("fat_mass", "mass", 1),
                 ("visceral_fat", "", 0), ("bmr", " kcal", 0))


TELEGRAM_TEXT = {
    "en": {
        "measurement": "Weigh-in", "pending": "Stable weight · measuring body composition…",
        "details": "Measurement details", "dashboard": "Open dashboard",
        "col_metric": "Metric", "col_value": "Value",
        "labels": ("Body fat", "Muscle", "Water", "BMI", "Protein", "Bone mass", "Fat mass", "Visceral fat", "BMR"),
        "sections": ("What improved", "What needs attention", "For next week"),
    },
    "pt-BR": {
        "measurement": "Pesagem", "pending": "Peso estável · medindo a composição corporal…",
        "details": "Detalhes da medição", "dashboard": "Abrir painel",
        "col_metric": "Métrica", "col_value": "Valor",
        "labels": ("Gordura", "Músculo", "Água", "IMC", "Proteína", "Ossos", "Massa gorda",
                   "Gordura visceral", "Metabolismo basal"),
        "sections": ("O que melhorou", "O que pede atenção", "Para a próxima semana"),
    },
    "it": {
        "measurement": "Rilevazione", "pending": "Peso stabile · composizione corporea in corso…",
        "details": "Dettagli della misurazione", "dashboard": "Apri dashboard",
        "col_metric": "Parametro", "col_value": "Valore",
        "labels": ("Grasso corporeo", "Muscoli", "Acqua", "IMC", "Proteine", "Massa ossea", "Massa grassa",
                   "Grasso viscerale", "Metabolismo basale"),
        "sections": ("Cosa è migliorato", "Cosa richiede attenzione", "Per la prossima settimana"),
    },
}


def _md(value: str) -> str:
    """Escape dynamic text for Telegram Rich Markdown (which also parses inline HTML)."""
    text = escape(value, quote=False)
    for ch in ("\\", "*", "_", "~", "`", "[", "]", "|"):
        text = text.replace(ch, f"\\{ch}")
    return text


def telegram_content(text: str, measurement: dict | None = None, profile: dict | None = None,
                     analysis: dict | None = None, unit: str = "kg", history: list[dict] | None = None,
                     pending: bool = False, lang: str | None = None) -> tuple[str, str]:
    lang = lang or (analysis or {}).get("lang") or LANG
    labels = TELEGRAM_TEXT.get(lang, TELEGRAM_TEXT["en"])
    t = TEXT.get(lang, TEXT["en"])
    rich, classic = [], []

    def heading(value: str, size: int = 2):
        rich.append(f"{'#' * size} {_md(value)}")
        classic.append(f"<b>{escape(value)}</b>")

    def paragraph(value: str):
        rich.append(_md(value))
        classic.append(escape(value))

    if measurement and profile:
        heading(f"⚖️ {labels['measurement']} · {profile['name']}")
        prev = _prev(measurement, history or [])
        weight_raw = _mass(measurement["weight_kg"], unit, 2, lang)
        weight_delta = _delta(measurement["weight_kg"], prev["weight_kg"], "mass", 2, unit, lang) if prev else ""
        weight_head = f"### **{_md(weight_raw)}**  {_md(weight_delta)}" if weight_delta else f"### **{_md(weight_raw)}**"
        rich.append(weight_head)
        classic.append(f"<b>{escape(weight_raw)}</b>")
        trends = _trend(measurement, history or [], unit, lang)
        if trends:
            rich_trends = []
            for idx, line in enumerate(trends):
                if idx == 0 and prev:
                    d = measurement["weight_kg"] - prev["weight_kg"]
                    prefix = "▲ " if d > 0.005 else "▼ " if d < -0.005 else "= "
                    rich_trends.append(f"> {prefix}{_md(line)}")
                else:
                    rich_trends.append(f"> {_md(line)}")
            rich.append("\n".join(rich_trends))
            classic.extend(escape(line) for line in trends)
        when_str = datetime.fromisoformat(measurement["measured_at"]).strftime("%d/%m/%Y %H:%M")
        classic.append(escape(when_str))
        if pending:
            rich.append(f"_{_md(labels['pending'])}_")
            classic.append(escape(labels["pending"]))
        else:
            rows, detail_rows = [], []
            metrics = measurement.get("metrics") or {}
            prev_metrics = (prev.get("metrics") or {}) if prev else {}
            show_delta = prev is not None
            for index, ((field, suffix, decimals), label) in enumerate(zip(METRIC_FIELDS, labels["labels"], strict=True)):
                raw_val = metrics.get(field)
                if raw_val is None:
                    continue
                value = _mass(raw_val, unit, decimals, lang) if suffix == "mass" else _num(raw_val, decimals, lang) + suffix
                delta = _delta(raw_val, prev_metrics.get(field), suffix, decimals, unit, lang) if show_delta else ""
                classic.append(f"<b>{escape(label)}</b>  {escape(value)}" + (f" ({escape(delta)})" if delta and delta != "—" else ""))
                if index < 4:
                    row = f"| {_md(label)} | **{_md(value)}** | {_md(delta)} |" if show_delta else f"| {_md(label)} | **{_md(value)}** |"
                    rows.append(row)
                else:
                    row = f"| {_md(label)} | {_md(value)} | {_md(delta)} |" if show_delta else f"| {_md(label)} | {_md(value)} |"
                    detail_rows.append(row)
            table_head = (f"| {_md(labels['col_metric'])} | {_md(labels['col_value'])} | Δ |\n|:--|--:|:--|"
                          if show_delta else f"| {_md(labels['col_metric'])} | {_md(labels['col_value'])} |\n|:--|--:|")
            if rows:
                rich.append("\n".join([table_head, *rows]))
            if measurement.get("impedance"):
                imp_label = t["impedance"].format(imp=measurement["impedance"])
                imp_val = f"{measurement['impedance']} Ω"
                if show_delta:
                    imp_delta = _delta(measurement["impedance"], prev.get("impedance"), " Ω", 0, unit, lang)
                    detail_rows.append(f"| {_md(imp_label.split()[0])} | {_md(imp_val)} | {_md(imp_delta)} |")
                else:
                    detail_rows.append(f"| {_md(imp_label.split()[0])} | {_md(imp_val)} |")
                classic.append(escape(imp_label))
            else:
                paragraph(t["no_impedance"])
            if not profile.get("height_cm") or not profile.get("birth"):
                paragraph(t["no_height"])
            if detail_rows:
                detail_table = "\n".join([table_head, *detail_rows])
                rich.append(f"<details><summary>{_md(labels['details'])}</summary>\n\n{detail_table}\n\n</details>")
        rich.append(f"_{_md(when_str)}_")
    elif analysis and profile:
        start, end = (datetime.fromisoformat(analysis[k]).strftime(t["day"]) for k in ("week_start", "week_end"))
        title = t["analysis"].format(name=profile["name"], start=start, end=end)
        heading(title if title.startswith("🧠") else f"🧠 {title}")
        body = analysis.get("text", "")
        if len(body) > 2400:
            body = body[:2400] + "…"
        bullets: list[str] = []

        def flush_bullets():
            if bullets:
                rich.append("\n".join(bullets))
                bullets.clear()

        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                flush_bullets()
                continue
            if stripped in labels["sections"]:
                flush_bullets()
                heading(stripped, 3)
            elif stripped.startswith(("• ", "- ")):
                bullets.append(f"- {_md(stripped[2:].strip())}")
                classic.append(escape(line))
            else:
                flush_bullets()
                paragraph(line)
        flush_bullets()
        if analysis.get("model"):
            rich.append(f"_{_md(analysis['model'])}_")
            classic.append(f"<i>{escape(analysis['model'])}</i>")
    else:
        paragraph(text[:3500])
    return "\n\n".join(rich), "\n\n".join(classic)


def telegram_enabled(chat_id: str | None = None) -> bool:
    return bool(TELEGRAM_TOKEN and (chat_id or TELEGRAM_CHAT))


def _private_chat(chat: str | None = None) -> bool:
    chat = chat or TELEGRAM_CHAT
    return chat.isdecimal() and int(chat) > 0


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API status {code}")


def _post(url: str, payload: dict):
    if isinstance(payload.get("photo"), bytes):
        boundary = "miscale-" + secrets.token_hex(16)
        parts = []
        for name, value in payload.items():
            parts.append(f"--{boundary}\r\n".encode())
            if name == "photo":
                parts.append(b'Content-Disposition: form-data; name="photo"; filename="card.png"\r\n'
                             b'Content-Type: image/png\r\n\r\n' + value + b"\r\n")
            else:
                value = json.dumps(value) if isinstance(value, dict | list | bool) else str(value)
                parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
    else:
        body = json.dumps(payload).encode()
        content_type = "application/json"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read()


def _telegram_call(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{urllib.parse.quote(TELEGRAM_TOKEN, safe=':')}/{method}"
    try:
        result = json.loads(_post(url, payload))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read())
        except ValueError:
            result = {}
        raise TelegramError(exc.code, int((result.get("parameters") or {}).get("retry_after", 0))) from None
    if not result.get("ok"):
        raise TelegramError(int(result.get("error_code", 500)), int((result.get("parameters") or {}).get("retry_after", 0)))


async def telegram(text: str, draft_id: int | None = None, chat_id: str | None = None, **content) -> bool:
    global _rich_available, _telegram_after
    chat = chat_id or TELEGRAM_CHAT
    if not telegram_enabled(chat) or (draft_id is not None and not _private_chat(chat)):
        return False
    delay = max(0, _telegram_after - time.monotonic())
    if delay:
        if draft_id is not None:
            return False
        await asyncio.sleep(delay)
    rich, classic = telegram_content(text, **content)
    base = {"chat_id": int(chat) if chat.isdecimal() else chat}
    if draft_id is not None:
        base["draft_id"] = draft_id
    else:
        url = urllib.parse.urlsplit(PUBLIC_URL)
        if url.scheme in ("http", "https") and url.netloc:
            lang = content.get("lang") or (content.get("analysis") or {}).get("lang") or LANG
            label = TELEGRAM_TEXT.get(lang, TELEGRAM_TEXT["en"])["dashboard"]
            base["reply_markup"] = {"inline_keyboard": [[{"text": label,
                                                          "url": _link(content.get("measurement"))}]]}
    methods = []
    if TELEGRAM_FORMAT == "photo" and draft_id is None and content.get("profile") and (
            content.get("measurement") or content.get("analysis")):
        from .telegram_card import render

        try:
            image_content = {k: v for k, v in content.items() if k != "pending"}
            image = await asyncio.to_thread(render, **image_content)
            caption = text if len(text) <= 900 else text[:900] + "…"
            methods.append(("sendPhoto", {**base, "photo": image, "caption": escape(caption), "parse_mode": "HTML"}))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("Telegram card rendering failed (%s); using text", type(exc).__name__)
    if TELEGRAM_FORMAT in ("rich", "photo") and _rich_available:
        methods.append(("sendRichMessageDraft" if draft_id is not None else "sendRichMessage",
                        {**base, "rich_message": {"markdown": rich}}))
    methods.append(("sendMessageDraft" if draft_id is not None else "sendMessage",
                    {**base, "text": classic, "parse_mode": "HTML",
                     **({"link_preview_options": {"is_disabled": True}} if draft_id is None else {})}))
    for method, payload in methods:
        try:
            await asyncio.to_thread(_telegram_call, method, payload)
            return True
        except TelegramError as exc:
            if method == "sendPhoto" and exc.code in (400, 404, 413, 501):
                continue
            if method.startswith("sendRich") and exc.code in (400, 404, 501):
                log.warning("Telegram %s rejected (API status %s); falling back to HTML", method, exc.code)
                _rich_available = False
                continue
            if exc.code == 429:
                _telegram_after = time.monotonic() + max(0, exc.retry_after)
            log.warning("Telegram %s failed (API status %s)", method, exc.code)
            return False
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("Telegram %s failed (transport or invalid response)", method)
            return False
    return False


class TelegramLive:
    def __init__(self, chat_id: str | None = None):
        self.chat_id = chat_id
        self.draft_id = secrets.randbits(52) + 1
        self.changed = asyncio.Event()
        self.pending: tuple[str, dict] | None = None
        self.task: asyncio.Task | None = None
        self.closed = False

    def update(self, text: str, **content):
        if (self.closed or not TELEGRAM_LIVE or not telegram_enabled(self.chat_id)
                or not _private_chat(self.chat_id)):
            return
        self.pending = (text, content)
        self.changed.set()
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        next_update = 0.0
        while not self.closed:
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=10)
            except TimeoutError:
                pass
            if self.closed:
                return
            await asyncio.sleep(max(0, next_update - time.monotonic()))
            if self.closed:
                return
            self.changed.clear()
            text, content = self.pending
            if not await telegram(text, draft_id=self.draft_id, chat_id=self.chat_id, **content):
                return
            next_update = time.monotonic() + DRAFT_INTERVAL

    async def close(self):
        self.closed = True
        self.changed.set()
        if self.task:
            await self.task


async def send(text: str, measurement: dict | None = None, profile: dict | None = None,
               analysis: dict | None = None, unit: str = "kg", history: list[dict] | None = None,
               chat_id: str | None = None) -> bool:
    """Sends `text`. The webhook also gets the measurement, or the weekly analysis."""
    if not enabled():
        log.info("no notifier configured; message only logged:\n%s", text)
        return False
    ok = True
    if WEBHOOK:
        payload = {"event": "analysis" if analysis else "measurement", "text": text, "message": text,
                   "profile": {k: profile.get(k) for k in ("id", "name")} if profile else None}
        if analysis:
            payload["analysis"] = analysis
        else:
            payload["measurement"] = measurement
        try:
            await asyncio.to_thread(_post, WEBHOOK, payload)
        except OSError as exc:
            log.warning("webhook failed (%s)", getattr(exc, "code", type(exc).__name__))
            ok = False
    if telegram_enabled(chat_id):
        ok = await telegram(text, measurement=measurement, profile=profile, analysis=analysis,
                            unit=unit, history=history, chat_id=chat_id) and ok
    return ok
