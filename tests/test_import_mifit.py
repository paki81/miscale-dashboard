import csv
import json
import sqlite3
from pathlib import Path

import pytest

from app.db import DB
from scripts import import_mifit

FIELDS = "time,weight,height,bmi,fatRate,bodyWaterRate,boneMass,metabolism,muscleRate,visceralFat".split(",")
ROW = {"time": "2024-01-01 07:00:00+0000", "weight": "80", "height": "178", "bmi": "25.2",
       "fatRate": "20", "bodyWaterRate": "55", "boneMass": "3", "metabolism": "1700",
       "muscleRate": "60", "visceralFat": "9"}


def source(tmp_path, rows):
    path = tmp_path / "BODY.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def database(tmp_path):
    path = tmp_path / "test.db"
    db = DB(str(path))
    pid = db.save_profile({"name": "Alex", "height_cm": 178, "male": True})
    db.conn.close()
    return path, pid


def count(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]


def test_csv_preserves_metrics_and_converts_timezone(tmp_path):
    path = source(tmp_path, [ROW, {**ROW, "time": "2024-07-01 07:00:00+0000", "fatRate": "null"}])
    rows = import_mifit.load_csv(path, "Europe/Rome")
    assert rows[0]["measured_at"] == "2024-01-01T08:00:00"
    assert rows[1]["measured_at"] == "2024-07-01T09:00:00"
    assert rows[0]["scale_time"] == "2024-01-01T07:00:00+00:00"
    assert rows[0]["metrics"]["muscle_mass"] == 60
    assert rows[0]["metrics"]["fat_mass"] == 16
    assert rows[0]["metrics"]["bone_mass"] == 3
    assert rows[0]["impedance"] is None and "protein" not in rows[0]["metrics"]
    assert "body_fat" not in rows[1]["metrics"] and "fat_mass" not in rows[1]["metrics"]


def test_dry_run_filters_profiles_without_writing(tmp_path):
    path = source(tmp_path, [ROW, ROW, {**ROW, "height": "165", "weight": "60", "bmi": "22.0"}])
    db, pid = database(tmp_path)
    before = db.read_bytes()
    result = import_mifit.import_csv(path, db, pid)
    assert result["source_rows"] == 3 and result["selected_rows"] == 2
    assert result["excluded_rows"] == 1 and result["source_duplicates"] == 1
    assert result["to_insert"] == 1 and result["inserted"] == 0 and result["backup"] is None
    assert db.read_bytes() == before and count(db) == 0


def test_import_backs_up_and_is_idempotent(tmp_path):
    path = source(tmp_path, [ROW, {**ROW, "time": "2024-01-02 07:00:00+0000"}])
    db, pid = database(tmp_path)
    result = import_mifit.import_csv(path, db, pid, apply=True)
    assert result["inserted"] == 2 and count(db) == 2
    assert count(Path(result["backup"])) == 0
    with sqlite3.connect(db) as conn:
        data = conn.execute("SELECT profile_id, impedance, metrics, address FROM measurements LIMIT 1").fetchone()
    assert data[0] == pid and data[1] is None
    assert json.loads(data[2])["body_fat"] == 20 and data[3] == import_mifit.ADDRESS
    again = import_mifit.import_csv(path, db, pid, apply=True)
    assert again["existing_duplicates"] == 2 and again["inserted"] == 0
    assert again["backup"] is None and count(db) == 2


@pytest.mark.parametrize("change", [{"weight": "nan"}, {"weight": "-1"}, {"fatRate": "inf"},
                                    {"time": "2024-01-01 07:00:00"}])
def test_invalid_input_never_changes_database(tmp_path, change):
    path = source(tmp_path, [ROW, {**ROW, **change}])
    db, pid = database(tmp_path)
    with pytest.raises(ValueError):
        import_mifit.import_csv(path, db, pid, apply=True)
    assert count(db) == 0 and not list(tmp_path.glob("before-import-*.db"))


def test_unknown_profile_never_changes_database(tmp_path):
    path = source(tmp_path, [ROW])
    db, _ = database(tmp_path)
    with pytest.raises(ValueError, match="Profile"):
        import_mifit.import_csv(path, db, 999, apply=True)
    assert count(db) == 0


def test_failed_batch_rolls_back_all_insertions(tmp_path, monkeypatch):
    path = source(tmp_path, [ROW, {**ROW, "time": "2024-01-02 07:00:00+0000"}])
    db, pid = database(tmp_path)
    original = import_mifit._insert_rows

    def fail(conn, rows, profile_id):
        original(conn, rows[:1], profile_id)
        raise RuntimeError("test failure")

    monkeypatch.setattr(import_mifit, "_insert_rows", fail)
    with pytest.raises(RuntimeError):
        import_mifit.import_csv(path, db, pid, apply=True)
    assert count(db) == 0


def test_dst_fold_keeps_distinct_instants_and_repeat_import_skips_them(tmp_path):
    path = source(tmp_path, [{**ROW, "time": "2024-10-27 00:30:00+0000"},
                             {**ROW, "time": "2024-10-27 01:30:00+0000"}])
    db, pid = database(tmp_path)
    result = import_mifit.import_csv(path, db, pid, apply=True)
    assert result["inserted"] == 2
    assert import_mifit.import_csv(path, db, pid, apply=True)["inserted"] == 0


def test_existing_native_measurement_is_not_duplicated(tmp_path):
    path = source(tmp_path, [ROW])
    db, pid = database(tmp_path)
    app_db = DB(str(db))
    app_db.insert({"measured_at": "2024-01-01T08:00:00", "weight_kg": 80, "profile_id": pid,
                   "impedance": 500, "metrics": {"body_fat": 21}, "address": "AA:BB:CC:DD:EE:FF"})
    app_db.conn.close()
    result = import_mifit.import_csv(path, db, pid, apply=True)
    assert result["inserted"] == 0 and result["existing_duplicates"] == 1
    assert count(db) == 1
