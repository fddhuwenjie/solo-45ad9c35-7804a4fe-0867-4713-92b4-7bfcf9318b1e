"""SQLite 持久层：阀门档案、测试（含采样）、分析版本、配对结果。"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS valves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL REFERENCES valves(id),
  phase TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  test_started_at TEXT,
  range_min REAL NOT NULL,
  range_max REAL NOT NULL,
  range_unit TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  calibration_valid_until TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  test_id INTEGER NOT NULL REFERENCES tests(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'auto',
  adjustments_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL,
  UNIQUE(test_id, version)
);
CREATE TABLE IF NOT EXISTS pairings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pre_analysis_id INTEGER NOT NULL,
  post_analysis_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failsafe_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL REFERENCES valves(id),
  fail_mode TEXT NOT NULL,
  phase TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  test_started_at TEXT,
  range_min REAL NOT NULL,
  range_max REAL NOT NULL,
  range_unit TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  observation_window_s REAL NOT NULL,
  calibration_valid_until TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failsafe_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  failsafe_test_id INTEGER NOT NULL REFERENCES failsafe_tests(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'auto',
  adjustments_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL,
  UNIQUE(failsafe_test_id, version)
);
CREATE TABLE IF NOT EXISTS failsafe_trends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL,
  fail_mode TEXT NOT NULL,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seatleak_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL REFERENCES valves(id),
  phase TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  test_started_at TEXT,
  flow_direction TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  calibration_valid_until TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seatleak_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seatleak_test_id INTEGER NOT NULL REFERENCES seatleak_tests(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'auto',
  adjustments_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL,
  UNIQUE(seatleak_test_id, version)
);
CREATE TABLE IF NOT EXISTS seatleak_comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thrust_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL REFERENCES valves(id),
  phase TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  test_started_at TEXT,
  actuator_type TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  calibration_valid_until TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thrust_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thrust_test_id INTEGER NOT NULL REFERENCES thrust_tests(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'auto',
  adjustments_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL,
  UNIQUE(thrust_test_id, version)
);
CREATE TABLE IF NOT EXISTS thrust_comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flowcurve_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL REFERENCES valves(id),
  phase TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  test_started_at TEXT,
  flow_direction TEXT NOT NULL,
  characteristic TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  calibration_valid_until TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flowcurve_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fc_test_id INTEGER NOT NULL REFERENCES flowcurve_tests(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'auto',
  adjustments_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL,
  UNIQUE(fc_test_id, version)
);
CREATE TABLE IF NOT EXISTS flowcurve_comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    # ---- 阀门档案 ----
    def create_valve(self, tag, description=""):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO valves(tag, description, created_at) VALUES(?,?,?)",
                (tag, description, _now()))
            return cur.lastrowid

    def get_valve(self, valve_id):
        row = self._conn.execute("SELECT * FROM valves WHERE id=?", (valve_id,)).fetchone()
        return dict(row) if row else None

    def get_valve_by_tag(self, tag):
        row = self._conn.execute("SELECT * FROM valves WHERE tag=?", (tag,)).fetchone()
        return dict(row) if row else None

    def list_valves(self):
        return [dict(r) for r in self._conn.execute("SELECT * FROM valves ORDER BY id")]

    # ---- 测试 ----
    def create_test(self, valve_id, phase, test_started_at, range_min, range_max,
                    range_unit, conditions, thresholds, calibration_valid_until, payload):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO tests(valve_id, phase, submitted_at, test_started_at,
                   range_min, range_max, range_unit, conditions_json, thresholds_json,
                   calibration_valid_until, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (valve_id, phase, _now(), test_started_at, range_min, range_max,
                 range_unit, json.dumps(conditions), json.dumps(thresholds),
                 calibration_valid_until, json.dumps(payload)))
            return cur.lastrowid

    def get_test(self, test_id):
        row = self._conn.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json"))
        d["thresholds"] = json.loads(d.pop("thresholds_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_tests(self, valve_id=None):
        q = "SELECT id, valve_id, phase, submitted_at, test_started_at FROM tests"
        args = ()
        if valve_id is not None:
            q += " WHERE valve_id=?"
            args = (valve_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    # ---- 分析版本 ----
    def create_analysis(self, test_id, author, adjustments, result):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM analyses WHERE test_id=?",
                (test_id,)).fetchone()
            version = row["v"] + 1
            cur = self._conn.execute(
                """INSERT INTO analyses(test_id, version, created_at, author,
                   adjustments_json, result_json) VALUES(?,?,?,?,?,?)""",
                (test_id, version, _now(), author,
                 json.dumps(adjustments), json.dumps(result)))
            return cur.lastrowid, version

    def get_analysis(self, analysis_id):
        row = self._conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        return self._analysis_row(row)

    def get_analysis_by_version(self, test_id, version):
        row = self._conn.execute(
            "SELECT * FROM analyses WHERE test_id=? AND version=?",
            (test_id, version)).fetchone()
        return self._analysis_row(row)

    def list_analyses(self, test_id):
        rows = self._conn.execute(
            """SELECT id, test_id, version, created_at, author FROM analyses
               WHERE test_id=? ORDER BY version""", (test_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _analysis_row(row):
        if not row:
            return None
        d = dict(row)
        d["adjustments"] = json.loads(d.pop("adjustments_json"))
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 配对 ----
    def create_pairing(self, pre_analysis_id, post_analysis_id, result):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO pairings(pre_analysis_id, post_analysis_id, created_at, result_json)
                   VALUES(?,?,?,?)""",
                (pre_analysis_id, post_analysis_id, _now(), json.dumps(result)))
            return cur.lastrowid

    def get_pairing(self, pairing_id):
        row = self._conn.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 故障安全测试 ----
    def create_failsafe_test(self, valve_id, fail_mode, phase, test_started_at,
                             range_min, range_max, range_unit, conditions, thresholds,
                             observation_window_s, calibration_valid_until, payload):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO failsafe_tests(valve_id, fail_mode, phase, submitted_at,
                   test_started_at, range_min, range_max, range_unit, conditions_json,
                   thresholds_json, observation_window_s, calibration_valid_until, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (valve_id, fail_mode, phase, _now(), test_started_at, range_min, range_max,
                 range_unit, json.dumps(conditions), json.dumps(thresholds),
                 observation_window_s, calibration_valid_until, json.dumps(payload)))
            return cur.lastrowid

    def get_failsafe_test(self, fs_test_id):
        row = self._conn.execute(
            "SELECT * FROM failsafe_tests WHERE id=?", (fs_test_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json"))
        d["thresholds"] = json.loads(d.pop("thresholds_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_failsafe_tests(self, valve_id=None):
        q = ("SELECT id, valve_id, fail_mode, phase, submitted_at, test_started_at "
             "FROM failsafe_tests")
        args = ()
        if valve_id is not None:
            q += " WHERE valve_id=?"
            args = (valve_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    # ---- 故障安全分析版本 ----
    def create_failsafe_analysis(self, fs_test_id, author, adjustments, result):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM failsafe_analyses WHERE failsafe_test_id=?",
                (fs_test_id,)).fetchone()
            version = row["v"] + 1
            cur = self._conn.execute(
                """INSERT INTO failsafe_analyses(failsafe_test_id, version, created_at, author,
                   adjustments_json, result_json) VALUES(?,?,?,?,?,?)""",
                (fs_test_id, version, _now(), author,
                 json.dumps(adjustments), json.dumps(result)))
            return cur.lastrowid, version

    def get_failsafe_analysis(self, analysis_id):
        row = self._conn.execute(
            "SELECT * FROM failsafe_analyses WHERE id=?", (analysis_id,)).fetchone()
        return self._fs_analysis_row(row)

    def list_failsafe_analyses(self, fs_test_id):
        rows = self._conn.execute(
            """SELECT id, failsafe_test_id, version, created_at, author
               FROM failsafe_analyses WHERE failsafe_test_id=? ORDER BY version""",
            (fs_test_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_failsafe_analysis_ids(self, valve_id, fail_mode):
        """同阀门同故障模式每个测试的最新分析版本，按测试时间排序。"""
        rows = self._conn.execute(
            """SELECT t.id AS tid,
                      COALESCE(t.test_started_at, t.submitted_at) AS ttime,
                      (SELECT a.id FROM failsafe_analyses a
                       WHERE a.failsafe_test_id=t.id ORDER BY a.version DESC LIMIT 1) AS aid
               FROM failsafe_tests t WHERE t.valve_id=? AND t.fail_mode=?
               ORDER BY ttime, t.id""", (valve_id, fail_mode)).fetchall()
        return [(r["tid"], r["aid"], r["ttime"]) for r in rows if r["aid"] is not None]

    @staticmethod
    def _fs_analysis_row(row):
        if not row:
            return None
        d = dict(row)
        d["adjustments"] = json.loads(d.pop("adjustments_json"))
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 故障安全退化趋势 ----
    def create_failsafe_trend(self, valve_id, fail_mode, result):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO failsafe_trends(valve_id, fail_mode, created_at, result_json)
                   VALUES(?,?,?,?)""", (valve_id, fail_mode, _now(), json.dumps(result)))
            return cur.lastrowid

    def get_failsafe_trend(self, trend_id):
        row = self._conn.execute(
            "SELECT * FROM failsafe_trends WHERE id=?", (trend_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 阀座密封保持试验 ----
    def create_seatleak_test(self, valve_id, phase, test_started_at, flow_direction,
                             conditions, thresholds, calibration_valid_until, payload):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO seatleak_tests(valve_id, phase, submitted_at, test_started_at,
                   flow_direction, conditions_json, thresholds_json,
                   calibration_valid_until, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (valve_id, phase, _now(), test_started_at, flow_direction,
                 json.dumps(conditions), json.dumps(thresholds),
                 calibration_valid_until, json.dumps(payload)))
            return cur.lastrowid

    def get_seatleak_test(self, sl_test_id):
        row = self._conn.execute(
            "SELECT * FROM seatleak_tests WHERE id=?", (sl_test_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json"))
        d["thresholds"] = json.loads(d.pop("thresholds_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_seatleak_tests(self, valve_id=None):
        q = ("SELECT id, valve_id, phase, submitted_at, test_started_at, flow_direction "
             "FROM seatleak_tests")
        args = ()
        if valve_id is not None:
            q += " WHERE valve_id=?"
            args = (valve_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    # ---- 阀座密封试验分析版本 ----
    def create_seatleak_analysis(self, sl_test_id, author, adjustments, result):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM seatleak_analyses "
                "WHERE seatleak_test_id=?", (sl_test_id,)).fetchone()
            version = row["v"] + 1
            cur = self._conn.execute(
                """INSERT INTO seatleak_analyses(seatleak_test_id, version, created_at, author,
                   adjustments_json, result_json) VALUES(?,?,?,?,?,?)""",
                (sl_test_id, version, _now(), author,
                 json.dumps(adjustments), json.dumps(result)))
            return cur.lastrowid, version

    def get_seatleak_analysis(self, analysis_id):
        row = self._conn.execute(
            "SELECT * FROM seatleak_analyses WHERE id=?", (analysis_id,)).fetchone()
        return self._sl_analysis_row(row)

    def list_seatleak_analyses(self, sl_test_id):
        rows = self._conn.execute(
            """SELECT id, seatleak_test_id, version, created_at, author
               FROM seatleak_analyses WHERE seatleak_test_id=? ORDER BY version""",
            (sl_test_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_seatleak_analysis_ids(self, valve_id):
        """同阀门每个阀座试验的最新分析版本，按试验时间排序。"""
        rows = self._conn.execute(
            """SELECT t.id AS tid,
                      COALESCE(t.test_started_at, t.submitted_at) AS ttime,
                      (SELECT a.id FROM seatleak_analyses a
                       WHERE a.seatleak_test_id=t.id ORDER BY a.version DESC LIMIT 1) AS aid
               FROM seatleak_tests t WHERE t.valve_id=?
               ORDER BY ttime, t.id""", (valve_id,)).fetchall()
        return [(r["tid"], r["aid"], r["ttime"]) for r in rows if r["aid"] is not None]

    @staticmethod
    def _sl_analysis_row(row):
        if not row:
            return None
        d = dict(row)
        d["adjustments"] = json.loads(d.pop("adjustments_json"))
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 阀座密封试验比较 ----
    def create_seatleak_comparison(self, valve_id, result):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO seatleak_comparisons(valve_id, created_at, result_json)
                   VALUES(?,?,?)""", (valve_id, _now(), json.dumps(result)))
            return cur.lastrowid

    def get_seatleak_comparison(self, comparison_id):
        row = self._conn.execute(
            "SELECT * FROM seatleak_comparisons WHERE id=?", (comparison_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 阀杆推力签名测试 ----
    def create_thrust_test(self, valve_id, phase, test_started_at, actuator_type,
                           conditions, thresholds, calibration_valid_until, payload):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO thrust_tests(valve_id, phase, submitted_at, test_started_at,
                   actuator_type, conditions_json, thresholds_json,
                   calibration_valid_until, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (valve_id, phase, _now(), test_started_at, actuator_type,
                 json.dumps(conditions), json.dumps(thresholds),
                 calibration_valid_until, json.dumps(payload)))
            return cur.lastrowid

    def get_thrust_test(self, ts_test_id):
        row = self._conn.execute(
            "SELECT * FROM thrust_tests WHERE id=?", (ts_test_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json"))
        d["thresholds"] = json.loads(d.pop("thresholds_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_thrust_tests(self, valve_id=None):
        q = ("SELECT id, valve_id, phase, actuator_type, submitted_at, test_started_at "
             "FROM thrust_tests")
        args = ()
        if valve_id is not None:
            q += " WHERE valve_id=?"
            args = (valve_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    def create_thrust_analysis(self, ts_test_id, author, adjustments, result):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM thrust_analyses "
                "WHERE thrust_test_id=?", (ts_test_id,)).fetchone()
            version = row["v"] + 1
            cur = self._conn.execute(
                """INSERT INTO thrust_analyses(thrust_test_id, version, created_at, author,
                   adjustments_json, result_json) VALUES(?,?,?,?,?,?)""",
                (ts_test_id, version, _now(), author,
                 json.dumps(adjustments), json.dumps(result)))
            return cur.lastrowid, version

    def get_thrust_analysis(self, analysis_id):
        row = self._conn.execute(
            "SELECT * FROM thrust_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["adjustments"] = json.loads(d.pop("adjustments_json"))
        d["result"] = json.loads(d.pop("result_json"))
        return d

    def list_thrust_analyses(self, ts_test_id):
        rows = self._conn.execute(
            """SELECT id, thrust_test_id, version, created_at, author
               FROM thrust_analyses WHERE thrust_test_id=? ORDER BY version""",
            (ts_test_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_thrust_analysis_ids(self, valve_id):
        """同阀门每个推力测试的最新分析版本，按试验时间排序。"""
        rows = self._conn.execute(
            """SELECT t.id AS tid,
                      COALESCE(t.test_started_at, t.submitted_at) AS ttime,
                      (SELECT a.id FROM thrust_analyses a
                       WHERE a.thrust_test_id=t.id ORDER BY a.version DESC LIMIT 1) AS aid
               FROM thrust_tests t WHERE t.valve_id=?
               ORDER BY ttime, t.id""", (valve_id,)).fetchall()
        return [(r["tid"], r["aid"], r["ttime"]) for r in rows if r["aid"] is not None]

    def create_thrust_comparison(self, valve_id, result):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO thrust_comparisons(valve_id, created_at, result_json)
                   VALUES(?,?,?)""", (valve_id, _now(), json.dumps(result)))
            return cur.lastrowid

    def get_thrust_comparison(self, comparison_id):
        row = self._conn.execute(
            "SELECT * FROM thrust_comparisons WHERE id=?", (comparison_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 单相液体流量曲线校核 ----
    def create_flowcurve_test(self, valve_id, phase, test_started_at, flow_direction,
                              characteristic, conditions, thresholds,
                              calibration_valid_until, payload):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO flowcurve_tests(valve_id, phase, submitted_at,
                   test_started_at, flow_direction, characteristic, conditions_json,
                   thresholds_json, calibration_valid_until, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (valve_id, phase, _now(), test_started_at, flow_direction,
                 characteristic, json.dumps(conditions), json.dumps(thresholds),
                 calibration_valid_until, json.dumps(payload)))
            return cur.lastrowid

    def get_flowcurve_test(self, fc_test_id):
        row = self._conn.execute(
            "SELECT * FROM flowcurve_tests WHERE id=?", (fc_test_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json"))
        d["thresholds"] = json.loads(d.pop("thresholds_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_flowcurve_tests(self, valve_id=None):
        q = ("SELECT id, valve_id, phase, flow_direction, characteristic, "
             "submitted_at, test_started_at FROM flowcurve_tests")
        args = ()
        if valve_id is not None:
            q += " WHERE valve_id=?"
            args = (valve_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    def create_flowcurve_analysis(self, fc_test_id, author, adjustments, result):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM flowcurve_analyses "
                "WHERE fc_test_id=?", (fc_test_id,)).fetchone()
            version = row["v"] + 1
            cur = self._conn.execute(
                """INSERT INTO flowcurve_analyses(fc_test_id, version, created_at,
                   author, adjustments_json, result_json) VALUES(?,?,?,?,?,?)""",
                (fc_test_id, version, _now(), author,
                 json.dumps(adjustments), json.dumps(result)))
            return cur.lastrowid, version

    def get_flowcurve_analysis(self, analysis_id):
        row = self._conn.execute(
            "SELECT * FROM flowcurve_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["adjustments"] = json.loads(d.pop("adjustments_json"))
        d["result"] = json.loads(d.pop("result_json"))
        return d

    def list_flowcurve_analyses(self, fc_test_id):
        rows = self._conn.execute(
            """SELECT id, fc_test_id, version, created_at, author
               FROM flowcurve_analyses WHERE fc_test_id=? ORDER BY version""",
            (fc_test_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_flowcurve_analysis_ids(self, valve_id):
        """同阀门每个流量曲线测次的最新分析版本，按测次时间排序。"""
        rows = self._conn.execute(
            """SELECT t.id AS tid,
                      COALESCE(t.test_started_at, t.submitted_at) AS ttime,
                      (SELECT a.id FROM flowcurve_analyses a
                       WHERE a.fc_test_id=t.id ORDER BY a.version DESC LIMIT 1) AS aid
               FROM flowcurve_tests t WHERE t.valve_id=?
               ORDER BY ttime, t.id""", (valve_id,)).fetchall()
        return [(r["tid"], r["aid"], r["ttime"]) for r in rows if r["aid"] is not None]

    def create_flowcurve_comparison(self, valve_id, result):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO flowcurve_comparisons(valve_id, created_at, result_json)
                   VALUES(?,?,?)""", (valve_id, _now(), json.dumps(result)))
            return cur.lastrowid

    def get_flowcurve_comparison(self, comparison_id):
        row = self._conn.execute(
            "SELECT * FROM flowcurve_comparisons WHERE id=?",
            (comparison_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d
