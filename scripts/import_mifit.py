import argparse
import csv
import json
import math
import os
import re
import secrets
import sqlite3
import zipfile
import zlib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.db import CSV_IMPORT

ADDRESS = CSV_IMPORT
METRICS = {"bmi": "bmi", "fatRate": "body_fat", "bodyWaterRate": "water", "boneMass": "bone_mass",
           "metabolism": "bmr", "muscleRate": "muscle_mass", "visceralFat": "visceral_fat", "proteinRatio": "protein"}


def _tnet(data: bytes, offset: int = 0, depth: int = 0):
    colon = data.find(b":", offset, offset + 12)
    if depth > 64 or colon < 0 or not data[offset:colon].isdigit():
        raise ValueError("Invalid flow framing")
    length = int(data[offset:colon])
    start, end = colon + 1, colon + 1 + length
    if length > 50 * 1024 * 1024 or end >= len(data):
        raise ValueError("Invalid flow length")
    kind, payload = data[end:end + 1], data[start:end]
    if kind in (b"}", b"]"):
        values, cursor = [], start
        while cursor < end:
            value, cursor = _tnet(data, cursor, depth + 1)
            values.append(value)
        if cursor != end or (kind == b"}" and len(values) % 2):
            raise ValueError("Invalid flow container")
        value = dict(zip(values[::2], values[1::2], strict=True)) if kind == b"}" else values
    elif kind == b",":
        value = payload
    elif kind == b";":
        value = payload.decode("utf-8")
    elif kind == b"#":
        value = int(payload)
    elif kind == b"^":
        value = float(payload)
    elif kind == b"!" and payload in (b"true", b"false"):
        value = payload == b"true"
    elif kind == b"~" and not payload:
        value = None
    else:
        raise ValueError("Invalid flow type")
    return value, end + 1


def _get(obj: dict, key: str, default=None):
    return obj.get(key, obj.get(key.encode(), default))


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def flows(path: Path):
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("Export exceeds 50 MB")
    data = path.read_bytes()
    cursor = 0
    while cursor < len(data):
        record, cursor = _tnet(data, cursor)
        if not isinstance(record, dict):
            raise ValueError("Invalid flow record")
        yield record


def weight_payloads(path: Path):
    for record in flows(path):
        request, response = _get(record, "request"), _get(record, "response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            continue
        route = urlsplit(_text(_get(request, "path", ""))).path
        match = re.fullmatch(r"/users/\d+/members/(-?\d+)/weightRecords", route)
        host = _text(_get(request, "host", ""))
        if not match or not host.startswith("api-mifit-") or not host.endswith((".zepp.com", ".huami.com")):
            continue
        if _get(response, "status_code") != 200:
            continue
        content = _get(response, "content") or b""
        if content.startswith(b"\x1f\x8b"):
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            content = decoder.decompress(content, 20 * 1024 * 1024)
            if not decoder.eof:
                raise ValueError("Weight response exceeds decoding limit")
        yield match[1], json.loads(content)


def inspect_export(path: Path) -> dict:
    with path.open("rb") as file:
        header = file.read(32)
    result = {"size": path.stat().st_size}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            result.update(format="zip", files=[{"name": entry.filename, "size": entry.file_size,
                                               "encrypted": bool(entry.flag_bits & 1)}
                                              for entry in archive.infolist() if not entry.is_dir()])
    elif header.startswith(b"SQLite format 3\x00"):
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
            result.update(format="sqlite", tables=[row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")])
    elif header.split(b":", 1)[0].isdigit():
        responses = []
        for record in flows(path):
            request, response = _get(record, "request"), _get(record, "response")
            if not isinstance(request, dict) or not isinstance(response, dict):
                continue
            route = urlsplit(_text(_get(request, "path", ""))).path
            match = re.fullmatch(r"/users/\d+/members/(-?\d+)/weightRecords", route)
            host = _text(_get(request, "host", ""))
            if not match or not host.startswith("api-mifit-") or not host.endswith((".zepp.com", ".huami.com")):
                continue
            content = _get(response, "content") or b""
            responses.append({"host": host, "member": match[1],
                              "status": _get(response, "status_code"), "body_bytes": len(content)})
        result.update(format="mitmproxy", responses=responses)
    else:
        result["format"] = "unidentified"
    return result


def _number(value):
    if value is None or str(value).strip().lower() in ("", "null", "none"):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite measurement")
    return number


def load_csv(path: Path, timezone: str = "Europe/Rome") -> list[dict]:
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("Export exceeds 50 MB")
    zone = ZoneInfo(timezone)
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not {"time", "weight", "height"}.issubset(reader.fieldnames or []):
            raise ValueError("BODY CSV must include time, weight and height")
        for line, row in enumerate(reader, 2):
            try:
                if None in row:
                    raise ValueError("Malformed CSV")
                at = datetime.fromisoformat(row["time"])
                if at.tzinfo is None:
                    raise ValueError("Timestamp must include an offset")
                weight, height = _number(row["weight"]), _number(row["height"])
                if weight is None or not 0 < weight <= 500:
                    raise ValueError("Invalid weight")
                metrics = {}
                for field, name in METRICS.items():
                    value = _number(row.get(field))
                    if value is not None and value > 0:
                        metrics[name] = value
                for name in ("body_fat", "water", "protein"):
                    if metrics.get(name, 0) > 100:
                        raise ValueError("Invalid percentage")
                if metrics.get("muscle_mass", 0) > weight or metrics.get("bone_mass", 0) > weight:
                    raise ValueError("Invalid mass")
                if height and metrics.get("bmi") and not math.isclose(
                        metrics["bmi"], weight / (height / 100) ** 2, rel_tol=0.02, abs_tol=0.3):
                    raise ValueError("Weight, height and BMI units disagree")
                if "body_fat" in metrics:
                    metrics["fat_mass"] = round(weight * metrics["body_fat"] / 100, 2)
                impedance = _number(row.get("impedance"))
                if impedance is not None:
                    if not impedance.is_integer() or not 0 < impedance < 3000:
                        raise ValueError("Invalid impedance")
                    impedance = int(impedance)
            except (ValueError, TypeError, KeyError, ZeroDivisionError):
                raise ValueError(f"Invalid measurement at CSV line {line}") from None
            rows.append({"measured_at": at.astimezone(zone).replace(tzinfo=None).isoformat(),
                         "scale_time": at.astimezone(UTC).isoformat(), "weight_kg": round(weight, 2),
                         "height_cm": height, "impedance": impedance, "metrics": metrics,
                         "raw": json.dumps({"source": "zepp_life_csv", "height_cm": height, "metrics": metrics}),
                         "address": ADDRESS})
    return rows


def _profile(db: sqlite3.Connection, profile_id: int) -> dict:
    row = db.execute("SELECT id, name, height_cm FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise ValueError("Profile not found")
    return dict(row)


def _plan(db: sqlite3.Connection, rows: list[dict], profile_id: int, timezone: str):
    imported, native = set(), set()
    for row in db.execute("SELECT measured_at, scale_time, weight_kg, address FROM measurements WHERE profile_id = ?",
                          (profile_id,)):
        if row["address"] == ADDRESS and row["scale_time"]:
            imported.add((row["scale_time"], round(row["weight_kg"], 2)))
        else:
            at = datetime.fromisoformat(row["measured_at"])
            if at.tzinfo:
                at = at.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
            native.add((at.isoformat(), round(row["weight_kg"], 2)))
    seen, new = set(), []
    source_duplicates, existing_duplicates = 0, 0
    for row in sorted(rows, key=lambda row: row["scale_time"]):
        key = (row["scale_time"], row["weight_kg"])
        if key in seen:
            source_duplicates += 1
            continue
        seen.add(key)
        if key in imported or (row["measured_at"], row["weight_kg"]) in native:
            existing_duplicates += 1
        else:
            new.append(row)
    return new, source_duplicates, existing_duplicates


def _backup(db: sqlite3.Connection, path: Path) -> Path:
    backup = path.parent / f"before-import-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(4)}.db"
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(backup)) as destination:
        db.backup(destination)
    return backup


def _insert_rows(db: sqlite3.Connection, rows: list[dict], profile_id: int):
    db.executemany("INSERT INTO measurements "
                   "(measured_at, scale_time, weight_kg, impedance, profile_id, metrics, raw, address) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(row["measured_at"], row["scale_time"], row["weight_kg"], row["impedance"], profile_id,
                     json.dumps(row["metrics"], allow_nan=False), row["raw"], row["address"]) for row in rows])


def import_csv(source: Path, database: Path, profile_id: int, timezone: str = "Europe/Rome",
               height_cm: float | None = None, apply: bool = False) -> dict:
    all_rows = load_csv(source, timezone)
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode={mode}", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        profile = _profile(db, profile_id)
        height = height_cm if height_cm is not None else profile["height_cm"]
        if height is None or not math.isfinite(height) or height <= 0:
            raise ValueError("Profile height or explicit height filter is required")
        rows = [row for row in all_rows if row["height_cm"] is not None
                and math.isclose(row["height_cm"], height, abs_tol=0.001)]
        new, source_duplicates, existing_duplicates = _plan(db, rows, profile_id, timezone)
        report = {"profile": profile, "timezone": timezone, "height_filter_cm": height,
                  "source_rows": len(all_rows), "selected_rows": len(rows), "excluded_rows": len(all_rows) - len(rows),
                  "source_duplicates": source_duplicates, "existing_duplicates": existing_duplicates,
                  "to_insert": len(new), "inserted": 0, "backup": None,
                  "first": min((row["measured_at"] for row in rows), default=None),
                  "last": max((row["measured_at"] for row in rows), default=None),
                  "metric_counts": {name: sum(name in row["metrics"] for row in rows)
                                    for name in (*METRICS.values(), "fat_mass")}}
        if apply and new:
            report["backup"] = str(_backup(db, database))
            with db:
                db.execute("BEGIN IMMEDIATE")
                _profile(db, profile_id)
                new, source_duplicates, existing_duplicates = _plan(db, rows, profile_id, timezone)
                _insert_rows(db, new, profile_id)
            report.update(inserted=len(new), to_insert=len(new), source_duplicates=source_duplicates,
                          existing_duplicates=existing_duplicates)
    return report


def csv_summary(path: Path) -> dict:
    groups = {}
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames
        for row in reader:
            height = row.get("height", "unknown")
            group = groups.setdefault(height, {"count": 0, "first": row.get("time"), "last": row.get("time")})
            group["count"] += 1
            group["first"] = min(group["first"], row["time"])
            group["last"] = max(group["last"], row["time"])
    return {"columns": fields, "heights": groups, "total": sum(g["count"] for g in groups.values())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--weights", action="store_true")
    mode.add_argument("--csv-summary", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--profile-id", type=int)
    parser.add_argument("--height-cm", type=float)
    parser.add_argument("--timezone", default="Europe/Rome")
    args = parser.parse_args()
    if args.dry_run or args.apply:
        if args.db is None or args.profile_id is None:
            parser.error("--db and --profile-id are required for CSV import")
        result = import_csv(args.source, args.db, args.profile_id, args.timezone, args.height_cm, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    result = csv_summary(args.source) if args.csv_summary else inspect_export(args.source)
    if args.weights:
        unique = {}
        fields = ("weight", "height", "age", "bmi", "fatRate", "bodyWaterRate", "boneMass", "metabolism",
                  "muscleRate", "proteinRatio", "visceralFat", "impedance")
        for member, payload in weight_payloads(args.source):
            for item in payload.get("items", []):
                summary = item.get("summary") or {}
                entry = {"member": member, "generatedTime": item.get("generatedTime"),
                         "createTime": item.get("createTime"), "weightType": item.get("weightType"),
                         "summary": {key: summary[key] for key in fields if key in summary}}
                unique[(member, entry["generatedTime"], summary.get("weight"))] = entry
        result = list(unique.values())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
