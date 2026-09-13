"""SQLite 存储核心：单连接、共享建表与阀门档案/校准版本数据访问。

连接与锁由 ``DatabaseCore`` 持有；六套诊断能力各自的数据访问层
（``app.<能力>.store``）以 mixin 形式共享同一连接，由 ``app.db.Database``
按能力清单组合，不额外打开连接。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

# 公共表：校准版本（含查询索引）与阀门档案。
# 各诊断能力的建表语句见对应包的 store 模块（app.<能力>.store.SCHEMA），
# 由装配层按能力清单依次执行；全部为 IF NOT EXISTS，旧库无需迁移。
CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_serial TEXT NOT NULL,
  measurement_type TEXT NOT NULL,
  unit TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_until TEXT NOT NULL,
  range_min REAL NOT NULL,
  range_max REAL NOT NULL,
  points_json TEXT NOT NULL,
  standard_uncertainty REAL NOT NULL,
  certificate_summary TEXT NOT NULL DEFAULT '',
  certificate_digest TEXT NOT NULL,
  supersedes_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calibration_lookup
  ON calibration_versions(instrument_serial, measurement_type);
CREATE TABLE IF NOT EXISTS valves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def decode_analysis_row(row):
    """分析版本行 → dict（adjustments/result 反序列化）；空行返回 None。"""
    if not row:
        return None
    d = dict(row)
    d["adjustments"] = json.loads(d.pop("adjustments_json"))
    d["result"] = json.loads(d.pop("result_json"))
    return d


def decode_test_row(row):
    """测试行 → dict（conditions/thresholds/payload 反序列化）；空行返回 None。"""
    if not row:
        return None
    d = dict(row)
    d["conditions"] = json.loads(d.pop("conditions_json"))
    d["thresholds"] = json.loads(d.pop("thresholds_json"))
    d["payload"] = json.loads(d.pop("payload_json"))
    return d


def decode_result_row(row):
    """单 JSON 结果列的行（配对/比较/趋势）→ dict；空行返回 None。"""
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json"))
    return d


class DatabaseCore:
    """连接、锁与公共数据访问（阀门档案、校准版本）。

    ``extra_schemas`` 为各诊断能力的建表语句，按能力清单顺序在
    公共表之后执行（全部 IF NOT EXISTS，幂等）。
    """

    def __init__(self, path, extra_schemas=()):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(CORE_SCHEMA)
            for ddl in extra_schemas:
                self._conn.executescript(ddl)

    def close(self):
        self._conn.close()

    # ---- 仪器校准版本（不可变；续证/纠错派生新版本） ----
    def create_calibration_version(self, instrument_serial, measurement_type, unit,
                                   valid_from, valid_until, range_min, range_max,
                                   points, standard_uncertainty, certificate_summary,
                                   certificate_digest, supersedes_id, note):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO calibration_versions(instrument_serial, measurement_type,
                   unit, valid_from, valid_until, range_min, range_max, points_json,
                   standard_uncertainty, certificate_summary, certificate_digest,
                   supersedes_id, note, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (instrument_serial, measurement_type, unit, valid_from, valid_until,
                 range_min, range_max, json.dumps(points), standard_uncertainty,
                 certificate_summary, certificate_digest, supersedes_id, note, utc_now()))
            return cur.lastrowid

    def get_calibration_version(self, version_id):
        row = self._conn.execute(
            "SELECT * FROM calibration_versions WHERE id=?", (version_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["points"] = json.loads(d.pop("points_json"))
        return d

    def list_calibration_versions(self, instrument_serial=None, measurement_type=None):
        q = "SELECT * FROM calibration_versions WHERE 1=1"
        args = []
        if instrument_serial:
            q += " AND instrument_serial=?"
            args.append(instrument_serial)
        if measurement_type:
            q += " AND measurement_type=?"
            args.append(measurement_type)
        q += " ORDER BY id"
        rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["points"] = json.loads(d.pop("points_json"))
            out.append(d)
        return out

    # ---- 阀门档案 ----
    def create_valve(self, tag, description=""):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO valves(tag, description, created_at) VALUES(?,?,?)",
                (tag, description, utc_now()))
            return cur.lastrowid

    def get_valve(self, valve_id):
        row = self._conn.execute("SELECT * FROM valves WHERE id=?", (valve_id,)).fetchone()
        return dict(row) if row else None

    def get_valve_by_tag(self, tag):
        row = self._conn.execute("SELECT * FROM valves WHERE tag=?", (tag,)).fetchone()
        return dict(row) if row else None

    def list_valves(self):
        return [dict(r) for r in self._conn.execute("SELECT * FROM valves ORDER BY id")]
