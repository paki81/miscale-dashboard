import os
import shutil
import subprocess
import tempfile
import textwrap
import threading
import urllib.request
from datetime import datetime
from html import escape
from pathlib import Path

from . import notify

FONT_DIR = Path(os.environ.get("MISCALE_DB", "/data/miscale.db")).parent / "telegram-fonts"
FONTS = {
    "manrope": "https://www.geopieno.it/assets/manrope-latin-500-normal-DMZssgOp.woff",
    "sora": "https://www.geopieno.it/assets/sora-latin-700-normal-BKPfQAnC.woff",
}
FONT_LOCK = threading.Lock()
CSS = Path(__file__).with_name("telegram-card.css")
PNG = b"\x89PNG\r\n\x1a\n"


def prepare_fonts() -> dict[str, Path]:
    with FONT_LOCK:
        FONT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = {}
        for name, url in FONTS.items():
            path = FONT_DIR / f"{name}.woff"
            if not path.exists():
                with urllib.request.urlopen(url, timeout=10) as response:
                    data = response.read(1024 * 1024 + 1)
                if (not data.startswith(b"wOFF") or not 44 <= len(data) <= 1024 * 1024
                        or int.from_bytes(data[8:12], "big") != len(data)
                        or not 0 < int.from_bytes(data[16:20], "big") <= 2 * 1024 * 1024):
                    raise ValueError("Invalid card font")
                with tempfile.NamedTemporaryFile(dir=FONT_DIR, delete=False) as file:
                    temporary = Path(file.name)
                    try:
                        file.write(data)
                        file.flush()
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            result[name] = path
        return result


def svg(measurement: dict | None = None, profile: dict | None = None, analysis: dict | None = None,
        unit: str = "kg", history: list[dict] | None = None, lang: str | None = None,
        fonts: dict[str, Path] | None = None) -> str:
    lang = lang or (analysis or {}).get("lang") or notify.LANG
    labels = notify.TELEGRAM_TEXT.get(lang, notify.TELEGRAM_TEXT["en"])
    t = notify.TEXT.get(lang, notify.TEXT["en"])
    fonts = fonts or prepare_fonts()
    css = CSS.read_text().replace("@manrope@", escape(str(fonts["manrope"]))).replace("@sora@", escape(str(fonts["sora"])))
    parts = []

    def rect(x, y, width, height, cls, radius=20):
        parts.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}"/>')

    def text(value, x, y, cls, limit=80):
        value = "".join(c for c in str(value) if ord(c) >= 32)
        value = value if len(value) <= limit else value[:limit - 1] + "…"
        parts.append(f'<text class="{cls}" x="{x}" y="{y}">{escape(value)}</text>')

    text("Mi Scale", 72, 80, "brand")
    text((profile or {}).get("name", ""), 72, 164, "title", 32)
    if measurement:
        text(labels["measurement"].upper(), 72, 120, "eyebrow")
        text(datetime.fromisoformat(measurement["measured_at"]).strftime("%d/%m/%Y %H:%M"), 72, 198, "date")
        rect(48, 224, 864, 160, "hero", 24)
        value = measurement["weight_kg"] / notify.LB if unit == "lb" else measurement["weight_kg"]
        text(notify._num(value, 2, lang), 72, 340, "weight")
        text(unit, 510, 340, "unit")
        y = 426
        for trend in notify._trend(measurement, history or [], unit, lang):
            text(trend, 72, y, "trend", 68)
            y += 32
        metrics = measurement.get("metrics") or {}
        values = []
        for (field, suffix, decimals), label in zip(notify.METRIC_FIELDS, labels["labels"], strict=True):
            value = metrics.get(field)
            if value is not None:
                formatted = (notify._mass(value, unit, decimals, lang) if suffix == "mass"
                             else notify._num(value, decimals, lang) + suffix)
                values.append((label, formatted))
        if measurement.get("impedance"):
            values.append((t["impedance"].split(" {imp}", 1)[0], f"{measurement['impedance']} ohm"))
        y += 16
        for index, (label, value) in enumerate(values):
            x, row_y = 48 + (index % 2) * 444, y + (index // 2) * 148
            rect(x, row_y, 420, 132, "metric", 18)
            text(label, x + 24, row_y + 40, "metric-label", 28)
            text(value, x + 24, row_y + 100, "metric-value", 22)
        y += ((len(values) + 1) // 2) * 148
        notes = []
        if not measurement.get("impedance"):
            notes.append(t["no_impedance"])
        if not (profile or {}).get("height_cm") or not (profile or {}).get("birth"):
            notes.append(t["no_height"])
        for note in notes:
            for line in textwrap.wrap(note, 60):
                text(line, 72, y + 30, "note")
                y += 34
        y += 28
    elif analysis:
        title = {"it": "Analisi settimanale", "pt-BR": "Análise da semana"}.get(lang, "Weekly analysis")
        text(title.upper(), 72, 120, "eyebrow")
        start, end = (datetime.fromisoformat(analysis[k]).strftime(t["day"]) for k in ("week_start", "week_end"))
        text(f"{start} – {end}", 72, 198, "date")
        y = 248
        body = analysis.get("text", "")
        body = body if len(body) <= 2400 else body[:2400] + "…"
        for paragraph in body.splitlines():
            if not paragraph.strip():
                y += 16
            elif paragraph.strip() in labels["sections"]:
                y += 14
                text(paragraph.strip(), 72, y, "section")
                y += 48
            else:
                for line in textwrap.wrap(paragraph.strip(), 48):
                    text(line, 72, y, "body", 52)
                    y += 42
        if analysis.get("model"):
            y += 22
            text(analysis["model"], 72, y, "note", 70)
        y += 36
    else:
        raise ValueError("A measurement or analysis is required")
    height = max(600, y + 100)
    if height > 3500:
        raise ValueError("Card is too tall")
    text("Mi Body Composition Scale 2", 72, height - 54, "footer")
    background = (f'<rect class="canvas" width="960" height="{height}"/>'
                  f'<rect class="shadow" x="28" y="36" width="904" height="{height - 54}" rx="36"/>'
                  f'<rect class="surface" x="24" y="20" width="912" height="{height - 48}" rx="32"/>'
                  '<rect class="accent" x="48" y="60" width="8" height="24" rx="4"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{height}" viewBox="0 0 960 {height}">'
            f'<style>{css}</style>{background}{"".join(parts)}</svg>')


def render(**content) -> bytes:
    converter = shutil.which("convert")
    if not converter:
        raise OSError("ImageMagick is unavailable")
    source = svg(**content)
    result = subprocess.run(
        [converter, "-limit", "memory", "128MiB", "-limit", "map", "128MiB", "-limit", "disk", "64MiB",
         "-limit", "thread", "1", "-limit", "time", "15", "-background", "#fbf6ef", "MSVG:-", "-strip", "PNG32:-"],
        input=source.encode(), capture_output=True, timeout=20, check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "MAGICK_THREAD_LIMIT": "1"})
    if not result.stdout.startswith(PNG) or len(result.stdout) > 5 * 1024 * 1024:
        raise ValueError("Invalid card image")
    return result.stdout
