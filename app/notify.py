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
        "unknown": "Pesata di {w} senza proprietario: il peso non corrisponde a nessun profilo.\n"
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
    return bool(WEBHOOK or (TELEGRAM_TOKEN and TELEGRAM_CHAT))


def _num(v, d=1, lang=LANG):
    s = f"{v:,.{d}f}"
    if lang.startswith(("pt", "it")):
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def _mass(kg, unit, d=1, lang=LANG):
    return f"{_num(kg / LB, d, lang)} lb" if unit == "lb" else f"{_num(kg, d, lang)} kg"


def _trend(m: dict, history: list[dict], unit: str, lang: str) -> list[str]:
    t = TEXT.get(lang, TEXT["en"])
    when = datetime.fromisoformat(m["measured_at"])
    prev = next((h for h in history if when - datetime.fromisoformat(h["measured_at"]) > timedelta(minutes=3)), None)
    trend = []
    if prev:
        d = m["weight_kg"] - prev["weight_kg"]
        trend.append(t["since"].format(d=("+" if d >= 0 else "−") + _mass(abs(d), unit, 2, lang),
                                       when=datetime.fromisoformat(prev["measured_at"]).strftime(t["date"])))
    week = [h["weight_kg"] for h in [m, *history] if when - datetime.fromisoformat(h["measured_at"]) < timedelta(days=7)]
    if len(week) > 1:
        trend.append(t["avg"].format(w=_mass(sum(week) / len(week), unit, 1, lang)))
    return trend


def message(m: dict, profile: dict | None, history: list[dict], unit: str = "kg", lang: str = LANG) -> str:
    """`history` holds the same person's previous measurements, newest first."""
    t = TEXT.get(lang, TEXT["en"])
    x = m.get("metrics") or {}
    if not profile:
        lines = [t["unknown"].format(w=_mass(m["weight_kg"], unit, 2, lang))]
        if PUBLIC_URL:
            lines.append(PUBLIC_URL)
        return "\n".join(lines)
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
    if PUBLIC_URL:
        lines.append(PUBLIC_URL)
    return "\n".join(lines)


def analysis_message(a: dict, profile: dict, lang: str = LANG) -> str:
    t = TEXT.get(lang, TEXT["en"])
    start, end = (datetime.fromisoformat(a[k]).strftime(t["day"]) for k in ("week_start", "week_end"))
    lines = [t["analysis"].format(name=profile["name"], start=start, end=end), "", a["text"]]
    if PUBLIC_URL:
        lines += ["", PUBLIC_URL]
    return "\n".join(lines)


TELEGRAM_TEXT = {
    "en": {
        "measurement": "Weigh-in", "pending": "Stable weight · measuring body composition…",
        "details": "Measurement details", "dashboard": "Open dashboard",
        "labels": ("Body fat", "Muscle", "Water", "BMI", "Protein", "Bone mass", "Fat mass", "Visceral fat", "BMR"),
        "sections": ("What improved", "What needs attention", "For next week"),
    },
    "pt-BR": {
        "measurement": "Pesagem", "pending": "Peso estável · medindo a composição corporal…",
        "details": "Detalhes da medição", "dashboard": "Abrir painel",
        "labels": ("Gordura", "Músculo", "Água", "IMC", "Proteína", "Ossos", "Massa gorda",
                   "Gordura visceral", "Metabolismo basal"),
        "sections": ("O que melhorou", "O que pede atenção", "Para a próxima semana"),
    },
    "it": {
        "measurement": "Pesata", "pending": "Peso stabile · composizione corporea in corso…",
        "details": "Dettagli della misurazione", "dashboard": "Apri dashboard",
        "labels": ("Grasso corporeo", "Muscoli", "Acqua", "IMC", "Proteine", "Massa ossea", "Massa grassa",
                   "Grasso viscerale", "Metabolismo basale"),
        "sections": ("Cosa è migliorato", "Cosa richiede attenzione", "Per la prossima settimana"),
    },
}


def telegram_content(text: str, measurement: dict | None = None, profile: dict | None = None,
                     analysis: dict | None = None, unit: str = "kg", history: list[dict] | None = None,
                     pending: bool = False, lang: str | None = None) -> tuple[str, str]:
    lang = lang or (analysis or {}).get("lang") or LANG
    labels = TELEGRAM_TEXT.get(lang, TELEGRAM_TEXT["en"])
    t = TEXT.get(lang, TEXT["en"])
    rich, classic = [], []

    def heading(value: str, size: int = 2):
        safe = escape(value)
        rich.append(f"<h{size}>{safe}</h{size}>")
        classic.append(f"<b>{safe}</b>")

    def paragraph(value: str):
        safe = escape(value)
        rich.append("<p>" + safe.replace("\n", "<br>") + "</p>")
        classic.append(safe)

    if measurement and profile:
        heading(f"{labels['measurement']} · {profile['name']}")
        weight = escape(_mass(measurement["weight_kg"], unit, 2, lang))
        rich.append(f"<p><strong>{weight}</strong></p>")
        classic.append(f"<b>{weight}</b>")
        paragraph(datetime.fromisoformat(measurement["measured_at"]).strftime("%d/%m/%Y %H:%M"))
        for trend in _trend(measurement, history or [], unit, lang):
            paragraph(trend)
        if pending:
            paragraph(labels["pending"])
        else:
            fields = (("body_fat", "%", 1), ("muscle_mass", "mass", 1), ("water", "%", 1), ("bmi", "", 1),
                      ("protein", "%", 1), ("bone_mass", "mass", 1), ("fat_mass", "mass", 1),
                      ("visceral_fat", "", 0), ("bmr", " kcal", 0))
            rows, details = [], []
            metrics = measurement.get("metrics") or {}
            for index, ((field, suffix, decimals), label) in enumerate(zip(fields, labels["labels"], strict=True)):
                value = metrics.get(field)
                if value is None:
                    continue
                value = _mass(value, unit, decimals, lang) if suffix == "mass" else _num(value, decimals, lang) + suffix
                safe_label, safe_value = escape(label), escape(value)
                classic.append(f"<b>{safe_label}</b>  {safe_value}")
                if index < 4:
                    rows.append(f"<tr><td>{safe_label}</td><td>{safe_value}</td></tr>")
                else:
                    details.append(f"<p><b>{safe_label}</b> {safe_value}</p>")
            if rows:
                rich.append("<table striped compact>" + "".join(rows) + "</table>")
            if measurement.get("impedance"):
                value = t["impedance"].format(imp=measurement["impedance"])
                details.append(f"<p>{escape(value)}</p>")
                classic.append(escape(value))
            else:
                paragraph(t["no_impedance"])
            if not profile.get("height_cm") or not profile.get("birth"):
                paragraph(t["no_height"])
            if details:
                rich.append(f"<details><summary>{escape(labels['details'])}</summary>{''.join(details)}</details>")
    elif analysis and profile:
        start, end = (datetime.fromisoformat(analysis[k]).strftime(t["day"]) for k in ("week_start", "week_end"))
        heading(t["analysis"].format(name=profile["name"], start=start, end=end))
        body = analysis.get("text", "")
        if len(body) > 2400:
            body = body[:2400] + "…"
        for line in body.splitlines():
            if line.strip() in labels["sections"]:
                heading(line.strip(), 3)
            elif line.strip():
                paragraph(line)
        if analysis.get("model"):
            rich.append(f"<footer>{escape(analysis['model'])}</footer>")
            classic.append(f"<i>{escape(analysis['model'])}</i>")
    else:
        paragraph(text[:3500])
    return "".join(rich), "\n\n".join(classic)


def telegram_enabled() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT)


def _private_chat() -> bool:
    return TELEGRAM_CHAT.isdecimal() and int(TELEGRAM_CHAT) > 0


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API status {code}")


def _post(url: str, payload: dict):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
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


async def telegram(text: str, draft_id: int | None = None, **content) -> bool:
    global _rich_available, _telegram_after
    if not telegram_enabled() or (draft_id is not None and not _private_chat()):
        return False
    delay = max(0, _telegram_after - time.monotonic())
    if delay:
        if draft_id is not None:
            return False
        await asyncio.sleep(delay)
    rich, classic = telegram_content(text, **content)
    base = {"chat_id": int(TELEGRAM_CHAT) if _private_chat() else TELEGRAM_CHAT}
    if draft_id is not None:
        base["draft_id"] = draft_id
    else:
        url = urllib.parse.urlsplit(PUBLIC_URL)
        if url.scheme in ("http", "https") and url.netloc:
            lang = content.get("lang") or (content.get("analysis") or {}).get("lang") or LANG
            label = TELEGRAM_TEXT.get(lang, TELEGRAM_TEXT["en"])["dashboard"]
            base["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": PUBLIC_URL}]]}
    methods = []
    if TELEGRAM_FORMAT == "rich" and _rich_available:
        methods.append(("sendRichMessageDraft" if draft_id is not None else "sendRichMessage",
                        {**base, "rich_message": {"html": rich}}))
    methods.append(("sendMessageDraft" if draft_id is not None else "sendMessage",
                    {**base, "text": classic, "parse_mode": "HTML",
                     **({"link_preview_options": {"is_disabled": True}} if draft_id is None else {})}))
    for method, payload in methods:
        try:
            await asyncio.to_thread(_telegram_call, method, payload)
            return True
        except TelegramError as exc:
            if method.startswith("sendRich") and exc.code in (400, 404, 501):
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
    def __init__(self):
        self.draft_id = secrets.randbits(52) + 1
        self.changed = asyncio.Event()
        self.pending: tuple[str, dict] | None = None
        self.task: asyncio.Task | None = None
        self.closed = False

    def update(self, text: str, **content):
        if self.closed or not TELEGRAM_LIVE or not telegram_enabled() or not _private_chat():
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
            if not await telegram(text, draft_id=self.draft_id, **content):
                return
            next_update = time.monotonic() + DRAFT_INTERVAL

    async def close(self):
        self.closed = True
        self.changed.set()
        if self.task:
            await self.task


async def send(text: str, measurement: dict | None = None, profile: dict | None = None,
               analysis: dict | None = None, unit: str = "kg", history: list[dict] | None = None) -> bool:
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
    if telegram_enabled():
        ok = await telegram(text, measurement=measurement, profile=profile, analysis=analysis,
                            unit=unit, history=history) and ok
    return ok
