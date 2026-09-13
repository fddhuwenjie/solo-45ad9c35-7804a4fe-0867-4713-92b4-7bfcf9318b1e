"""调节阀全行程诊断服务 API。"""

import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from . import calibration as calchain, schemas
from .db import Database
from .failsafe import failsafe_analysis, failsafe_report, trend as fs_trend
from .flowcurve import compare as fc_compare, fc_analysis, fc_report
from .fullstroke import analysis, pairing, report
from .limitswitch import compare as ls_compare, ls_analysis, ls_report
from .seatleak import compare as sl_compare, seat_analysis, seat_report
from .thrustsignature import (
    compare as ts_compare, thrust_analysis, thrust_report)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
DEFAULT_DB = os.environ.get("VALVE_DB_PATH", str(Path(__file__).resolve().parent.parent / "valve_diag.db"))


def create_app(db_path=DEFAULT_DB):
    app = FastAPI(title="调节阀全行程诊断服务", version="1.0.0")
    db = Database(db_path)

    def get_db():
        return db

    @app.on_event("shutdown")
    def _shutdown():
        db.close()

    # ---- 阀门档案 ----
    @app.post("/valves", status_code=201)
    def create_valve(body: dict, db=Depends(get_db)):
        tag = (body.get("tag") or "").strip()
        if not tag:
            raise HTTPException(422, "tag 不能为空")
        if db.get_valve_by_tag(tag):
            raise HTTPException(409, f"阀门 {tag} 已存在")
        vid = db.create_valve(tag, body.get("description", ""))
        return db.get_valve(vid)

    @app.get("/valves")
    def list_valves(db=Depends(get_db)):
        return db.list_valves()

    @app.get("/valves/{valve_id}")
    def get_valve(valve_id: int, db=Depends(get_db)):
        v = db.get_valve(valve_id)
        if not v:
            raise HTTPException(404, "阀门不存在")
        v["tests"] = db.list_tests(valve_id)
        return v

    # ---- 仪器校准版本（逐通道校准链；版本不可变） ----
    @app.post("/calibration-versions", status_code=201)
    def create_calibration_version(body: schemas.CalibrationVersionCreate,
                                   db=Depends(get_db)):
        data = body.model_dump()
        problems = calchain.validate_version_create(data)
        if body.supersedes_id is not None \
                and db.get_calibration_version(body.supersedes_id) is None:
            problems.append(f"supersedes_id={body.supersedes_id} 指向不存在的版本")
        if problems:
            raise HTTPException(422, "；".join(problems))
        digest = calchain.certificate_digest(data)
        vid = db.create_calibration_version(
            instrument_serial=body.instrument_serial,
            measurement_type=body.measurement_type,
            unit=body.unit,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            range_min=body.range_min,
            range_max=body.range_max,
            points=data["points"],
            standard_uncertainty=body.standard_uncertainty,
            certificate_summary=body.certificate_summary,
            certificate_digest=digest,
            supersedes_id=body.supersedes_id,
            note=body.note)
        return db.get_calibration_version(vid)

    @app.get("/calibration-versions")
    def list_calibration_versions(instrument_serial: str | None = None,
                                  measurement_type: str | None = None,
                                  db=Depends(get_db)):
        return db.list_calibration_versions(instrument_serial, measurement_type)

    @app.get("/calibration-versions/{version_id}")
    def get_calibration_version(version_id: int, db=Depends(get_db)):
        v = db.get_calibration_version(version_id)
        if not v:
            raise HTTPException(404, "校准版本不存在")
        return v

    # ---- 测试提交 ----
    @app.post("/tests", status_code=201)
    def submit_test(body: schemas.TestSubmission, db=Depends(get_db)):
        valve = db.get_valve_by_tag(body.valve_tag)
        if valve is None:
            vid = db.create_valve(body.valve_tag, body.valve_description)
            valve = db.get_valve(vid)
        payload = body.model_dump()
        test_id = db.create_test(
            valve_id=valve["id"],
            phase=body.phase,
            test_started_at=body.test_started_at,
            range_min=body.range.min,
            range_max=body.range.max,
            range_unit=body.range.unit,
            conditions=body.conditions.model_dump(),
            thresholds=body.thresholds.model_dump(),
            calibration_valid_until=body.calibration_valid_until,
            payload=payload,
        )
        return {"test_id": test_id, "valve_id": valve["id"]}

    @app.get("/tests/{test_id}")
    def get_test(test_id: int, db=Depends(get_db)):
        t = db.get_test(test_id)
        if not t:
            raise HTTPException(404, "测试不存在")
        t["analyses"] = db.list_analyses(test_id)
        return t

    # ---- 分析与版本 ----
    @app.post("/tests/{test_id}/analyze", status_code=201)
    def analyze(test_id: int, body: schemas.AnalyzeRequest | None = None, db=Depends(get_db)):
        if db.get_test(test_id) is None:
            raise HTTPException(404, "测试不存在")
        author = body.author if body else "auto"
        override = body.uncertainty.model_dump() if body and body.uncertainty else None
        bindings = body.calibration_bindings if body else None
        return analysis.run_analysis(db, test_id, author=author,
                                     uncertainty_override=override,
                                     bindings_override=bindings)

    @app.get("/tests/{test_id}/analyses")
    def list_analyses(test_id: int, db=Depends(get_db)):
        return db.list_analyses(test_id)

    @app.get("/analyses/{analysis_id}")
    def get_analysis(analysis_id: int, db=Depends(get_db)):
        a = db.get_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "分析不存在")
        return a

    @app.post("/analyses/{analysis_id}/adjust", status_code=201)
    def adjust(analysis_id: int, body: schemas.AdjustRequest, db=Depends(get_db)):
        base = db.get_analysis(analysis_id)
        if not base:
            raise HTTPException(404, "分析不存在")
        if not body.boundary_moves and not body.exclusions \
                and body.calibration_bindings is None:
            raise HTTPException(422, "调整请求为空：需包含 boundary_moves、exclusions 或 calibration_bindings")
        override = body.uncertainty.model_dump() if body.uncertainty is not None else None
        return analysis.run_analysis(
            db, base["test_id"], author=body.author,
            new_adjustments={
                "boundary_moves": [m.model_dump() for m in body.boundary_moves],
                "exclusions": [e.model_dump() for e in body.exclusions],
                "calibration_bindings": body.calibration_bindings,
            },
            uncertainty_override=override,
            bindings_override=body.calibration_bindings)

    # ---- 检修前后配对 ----
    @app.post("/pairings", status_code=201)
    def create_pairing(body: schemas.PairRequest, db=Depends(get_db)):
        try:
            result = pairing.compare(db, body.pre_analysis_id, body.post_analysis_id)
        except KeyError as e:
            raise HTTPException(404, str(e))
        pid = db.create_pairing(body.pre_analysis_id, body.post_analysis_id, result)
        return {"pairing_id": pid, **result}

    @app.get("/pairings/{pairing_id}")
    def get_pairing(pairing_id: int, db=Depends(get_db)):
        p = db.get_pairing(pairing_id)
        if not p:
            raise HTTPException(404, "配对不存在")
        return p

    # ---- 导出与报告 ----
    @app.get("/analyses/{analysis_id}/export")
    def export_json(analysis_id: int, db=Depends(get_db)):
        a = db.get_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "分析不存在")
        return JSONResponse(
            content=a,
            headers={"Content-Disposition":
                     f'attachment; filename="analysis_{analysis_id}_v{a["version"]}.json"'})

    @app.get("/analyses/{analysis_id}/report", response_class=HTMLResponse)
    def html_report(analysis_id: int, db=Depends(get_db)):
        a = db.get_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "分析不存在")
        test = db.get_test(a["test_id"])
        valve = db.get_valve(test["valve_id"])
        return report.render_report(a, test, valve)

    # ===================== 故障安全动作测试 =====================

    @app.post("/failsafe/tests", status_code=201)
    def submit_failsafe_test(body: schemas.FSTestSubmission, db=Depends(get_db)):
        valve = db.get_valve_by_tag(body.valve_tag)
        if valve is None:
            vid = db.create_valve(body.valve_tag, body.valve_description)
            valve = db.get_valve(vid)
        payload = body.model_dump()
        fs_test_id = db.create_failsafe_test(
            valve_id=valve["id"],
            fail_mode=body.fail_mode,
            phase=body.phase,
            test_started_at=body.test_started_at,
            range_min=body.range.min,
            range_max=body.range.max,
            range_unit=body.range.unit,
            conditions=body.conditions.model_dump(),
            thresholds=body.thresholds.model_dump(),
            observation_window_s=body.observation_window_s,
            calibration_valid_until=body.calibration_valid_until,
            payload=payload,
        )
        return {"failsafe_test_id": fs_test_id, "valve_id": valve["id"]}

    @app.get("/failsafe/tests/{fs_test_id}")
    def get_failsafe_test(fs_test_id: int, db=Depends(get_db)):
        t = db.get_failsafe_test(fs_test_id)
        if not t:
            raise HTTPException(404, "故障安全测试不存在")
        t["analyses"] = db.list_failsafe_analyses(fs_test_id)
        return t

    @app.post("/failsafe/tests/{fs_test_id}/analyze", status_code=201)
    def analyze_failsafe(fs_test_id: int, body: schemas.AnalyzeRequest | None = None,
                         db=Depends(get_db)):
        if db.get_failsafe_test(fs_test_id) is None:
            raise HTTPException(404, "故障安全测试不存在")
        author = body.author if body else "auto"
        bindings = body.calibration_bindings if body else None
        return failsafe_analysis.run_failsafe_analysis(
            db, fs_test_id, author=author, bindings_override=bindings)

    @app.get("/failsafe/tests/{fs_test_id}/analyses")
    def list_failsafe_analyses(fs_test_id: int, db=Depends(get_db)):
        return db.list_failsafe_analyses(fs_test_id)

    @app.get("/failsafe/analyses/{analysis_id}")
    def get_failsafe_analysis(analysis_id: int, db=Depends(get_db)):
        a = db.get_failsafe_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "故障安全分析不存在")
        return a

    @app.post("/failsafe/analyses/{analysis_id}/adjust", status_code=201)
    def adjust_failsafe(analysis_id: int, body: schemas.FSAdjustRequest, db=Depends(get_db)):
        base = db.get_failsafe_analysis(analysis_id)
        if not base:
            raise HTTPException(404, "故障安全分析不存在")
        if body.trip_move is None and body.window_move is None \
                and body.calibration_bindings is None:
            raise HTTPException(422, "调整请求为空：需包含 trip_move、window_move 或 calibration_bindings")
        new_adj = {}
        if body.trip_move is not None:
            new_adj["trip_move"] = body.trip_move.model_dump()
        if body.window_move is not None:
            if body.window_move.window_end_s <= 0:
                raise HTTPException(422, "观察窗长度必须为正数（跳闸后秒）")
            new_adj["window_move"] = body.window_move.model_dump()
        new_adj["calibration_bindings"] = body.calibration_bindings
        try:
            return failsafe_analysis.run_failsafe_analysis(
                db, base["failsafe_test_id"], author=body.author,
                new_adjustments=new_adj,
                bindings_override=body.calibration_bindings)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/failsafe/trends", status_code=201)
    def create_failsafe_trend(body: schemas.FSTrendRequest, db=Depends(get_db)):
        try:
            if body.analysis_ids:
                data = fs_trend.build_trend(db, analysis_ids=body.analysis_ids)
                valve_id = data.get("valve_id")
                mode = data.get("fail_mode")
            else:
                if not body.valve_tag or not body.fail_mode:
                    raise HTTPException(422, "未指定 analysis_ids 时必须提供 valve_tag 与 fail_mode")
                valve = db.get_valve_by_tag(body.valve_tag)
                if valve is None:
                    raise HTTPException(404, "阀门不存在")
                data = fs_trend.build_trend(
                    db, valve_id=valve["id"], fail_mode=body.fail_mode)
                valve_id, mode = valve["id"], body.fail_mode
        except KeyError as e:
            raise HTTPException(404, str(e))
        trend_id = db.create_failsafe_trend(valve_id, mode, data)
        return {"trend_id": trend_id, **data}

    @app.get("/failsafe/trends/{trend_id}")
    def get_failsafe_trend(trend_id: int, db=Depends(get_db)):
        t = db.get_failsafe_trend(trend_id)
        if not t:
            raise HTTPException(404, "趋势记录不存在")
        return t

    @app.get("/failsafe/analyses/{analysis_id}/export")
    def export_failsafe_json(analysis_id: int, db=Depends(get_db)):
        a = db.get_failsafe_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "故障安全分析不存在")
        return JSONResponse(
            content=a,
            headers={"Content-Disposition":
                     f'attachment; filename="failsafe_{analysis_id}_v{a["version"]}.json"'})

    @app.get("/failsafe/analyses/{analysis_id}/report", response_class=HTMLResponse)
    def fs_html_report(analysis_id: int, db=Depends(get_db)):
        a = db.get_failsafe_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "故障安全分析不存在")
        t = db.get_failsafe_test(a["failsafe_test_id"])
        valve = db.get_valve(t["valve_id"])
        return failsafe_report.render_fs_report(a, t, valve)

    @app.get("/failsafe/trends/{trend_id}/report", response_class=HTMLResponse)
    def fs_trend_report(trend_id: int, db=Depends(get_db)):
        rec = db.get_failsafe_trend(trend_id)
        if not rec:
            raise HTTPException(404, "趋势记录不存在")
        valve = db.get_valve(rec["valve_id"])
        return failsafe_report.render_fs_trend_report(trend_id, rec["result"], valve)

    # ===================== 气体阀座密封保持试验 =====================

    @app.post("/seatleak/tests", status_code=201)
    def submit_seatleak_test(body: schemas.SLTestSubmission, db=Depends(get_db)):
        valve = db.get_valve_by_tag(body.valve_tag)
        if valve is None:
            vid = db.create_valve(body.valve_tag, body.valve_description)
            valve = db.get_valve(vid)
        payload = body.model_dump()
        sl_test_id = db.create_seatleak_test(
            valve_id=valve["id"],
            phase=body.phase,
            test_started_at=body.test_started_at,
            flow_direction=body.flow_direction,
            conditions=body.conditions.model_dump(),
            thresholds=body.thresholds.model_dump(),
            calibration_valid_until=body.calibration_valid_until,
            payload=payload,
        )
        return {"seatleak_test_id": sl_test_id, "valve_id": valve["id"]}

    @app.get("/seatleak/tests/{sl_test_id}")
    def get_seatleak_test(sl_test_id: int, db=Depends(get_db)):
        t = db.get_seatleak_test(sl_test_id)
        if not t:
            raise HTTPException(404, "阀座密封试验不存在")
        t["analyses"] = db.list_seatleak_analyses(sl_test_id)
        return t

    @app.post("/seatleak/tests/{sl_test_id}/analyze", status_code=201)
    def analyze_seatleak(sl_test_id: int, body: schemas.AnalyzeRequest | None = None,
                         db=Depends(get_db)):
        if db.get_seatleak_test(sl_test_id) is None:
            raise HTTPException(404, "阀座密封试验不存在")
        author = body.author if body else "auto"
        bindings = body.calibration_bindings if body else None
        return seat_analysis.run_seat_analysis(
            db, sl_test_id, author=author, bindings_override=bindings)

    @app.get("/seatleak/tests/{sl_test_id}/analyses")
    def list_seatleak_analyses(sl_test_id: int, db=Depends(get_db)):
        return db.list_seatleak_analyses(sl_test_id)

    @app.get("/seatleak/analyses/{analysis_id}")
    def get_seatleak_analysis(analysis_id: int, db=Depends(get_db)):
        a = db.get_seatleak_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "阀座密封分析不存在")
        return a

    @app.post("/seatleak/analyses/{analysis_id}/adjust", status_code=201)
    def adjust_seatleak(analysis_id: int, body: schemas.SLAdjustRequest, db=Depends(get_db)):
        base = db.get_seatleak_analysis(analysis_id)
        if not base:
            raise HTTPException(404, "阀座密封分析不存在")
        if not body.segment_moves and not body.exclusions \
                and body.calibration_bindings is None:
            raise HTTPException(422, "调整请求为空：需包含 segment_moves、exclusions 或 calibration_bindings")
        return seat_analysis.run_seat_analysis(
            db, base["seatleak_test_id"], author=body.author,
            new_adjustments={
                "segment_moves": [m.model_dump() for m in body.segment_moves],
                "exclusions": [e.model_dump() for e in body.exclusions],
                "calibration_bindings": body.calibration_bindings,
            },
            bindings_override=body.calibration_bindings)

    @app.post("/seatleak/comparisons", status_code=201)
    def create_seatleak_comparison(body: schemas.SLCompareRequest, db=Depends(get_db)):
        try:
            if body.analysis_ids:
                result = sl_compare.compare_seat_tests(db, analysis_ids=body.analysis_ids)
            else:
                if not body.valve_tag:
                    raise HTTPException(422, "未指定 analysis_ids 时必须提供 valve_tag")
                result = sl_compare.compare_seat_tests(db, valve_tag=body.valve_tag)
        except KeyError as e:
            raise HTTPException(404, str(e))
        cid = db.create_seatleak_comparison(result.get("valve_id"), result)
        return {"comparison_id": cid, **result}

    @app.get("/seatleak/comparisons/{comparison_id}")
    def get_seatleak_comparison(comparison_id: int, db=Depends(get_db)):
        c = db.get_seatleak_comparison(comparison_id)
        if not c:
            raise HTTPException(404, "比较记录不存在")
        return c

    @app.get("/seatleak/analyses/{analysis_id}/export")
    def export_seatleak_json(analysis_id: int, db=Depends(get_db)):
        a = db.get_seatleak_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "阀座密封分析不存在")
        return JSONResponse(
            content=a,
            headers={"Content-Disposition":
                     f'attachment; filename="seatleak_{analysis_id}_v{a["version"]}.json"'})

    @app.get("/seatleak/analyses/{analysis_id}/report", response_class=HTMLResponse)
    def seatleak_html_report(analysis_id: int, db=Depends(get_db)):
        a = db.get_seatleak_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "阀座密封分析不存在")
        t = db.get_seatleak_test(a["seatleak_test_id"])
        valve = db.get_valve(t["valve_id"])
        return seat_report.render_seat_report(a, t, valve)

    # ===================== 阀杆推力签名测试 =====================

    @app.post("/stemthrust/tests", status_code=201)
    def submit_thrust_test(body: schemas.TSTestSubmission, db=Depends(get_db)):
        valve = db.get_valve_by_tag(body.valve_tag)
        if valve is None:
            vid = db.create_valve(body.valve_tag, body.valve_description)
            valve = db.get_valve(vid)
        payload = body.model_dump()
        ts_test_id = db.create_thrust_test(
            valve_id=valve["id"],
            phase=body.phase,
            test_started_at=body.test_started_at,
            actuator_type=body.actuator.actuator_type,
            conditions=body.conditions.model_dump(),
            thresholds=body.thresholds.model_dump(),
            calibration_valid_until=body.calibration_valid_until,
            payload=payload,
        )
        return {"stemthrust_test_id": ts_test_id, "valve_id": valve["id"]}

    @app.get("/stemthrust/tests/{ts_test_id}")
    def get_thrust_test(ts_test_id: int, db=Depends(get_db)):
        t = db.get_thrust_test(ts_test_id)
        if not t:
            raise HTTPException(404, "阀杆推力测试不存在")
        t["analyses"] = db.list_thrust_analyses(ts_test_id)
        return t

    @app.post("/stemthrust/tests/{ts_test_id}/analyze", status_code=201)
    def analyze_thrust(ts_test_id: int, body: schemas.AnalyzeRequest | None = None,
                       db=Depends(get_db)):
        if db.get_thrust_test(ts_test_id) is None:
            raise HTTPException(404, "阀杆推力测试不存在")
        author = body.author if body else "auto"
        bindings = body.calibration_bindings if body else None
        return thrust_analysis.run_thrust_analysis(
            db, ts_test_id, author=author, bindings_override=bindings)

    @app.get("/stemthrust/tests/{ts_test_id}/analyses")
    def list_thrust_analyses(ts_test_id: int, db=Depends(get_db)):
        return db.list_thrust_analyses(ts_test_id)

    @app.get("/stemthrust/analyses/{analysis_id}")
    def get_thrust_analysis(analysis_id: int, db=Depends(get_db)):
        a = db.get_thrust_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "阀杆推力分析不存在")
        return a

    @app.post("/stemthrust/analyses/{analysis_id}/adjust", status_code=201)
    def adjust_thrust(analysis_id: int, body: schemas.TSAdjustRequest, db=Depends(get_db)):
        base = db.get_thrust_analysis(analysis_id)
        if not base:
            raise HTTPException(404, "阀杆推力分析不存在")
        if not body.segment_moves and not body.exclusions \
                and body.calibration_bindings is None:
            raise HTTPException(422, "调整请求为空：需包含 segment_moves、exclusions 或 calibration_bindings")
        for m in body.segment_moves:
            if not m.reason.strip():
                raise HTTPException(422, "人工移动相位边界必须填写理由")
        for e in body.exclusions:
            if not e.reason.strip():
                raise HTTPException(422, "剔除坏点必须填写理由")
        return thrust_analysis.run_thrust_analysis(
            db, base["thrust_test_id"], author=body.author,
            new_adjustments={
                "segment_moves": [m.model_dump() for m in body.segment_moves],
                "exclusions": [e.model_dump() for e in body.exclusions],
                "calibration_bindings": body.calibration_bindings,
            },
            bindings_override=body.calibration_bindings)

    @app.post("/stemthrust/comparisons", status_code=201)
    def create_thrust_comparison(body: schemas.TSCompareRequest, db=Depends(get_db)):
        try:
            if body.analysis_ids:
                result = ts_compare.compare_thrust_tests(db, analysis_ids=body.analysis_ids)
            else:
                if not body.valve_tag:
                    raise HTTPException(422, "未指定 analysis_ids 时必须提供 valve_tag")
                result = ts_compare.compare_thrust_tests(db, valve_tag=body.valve_tag)
        except KeyError as e:
            raise HTTPException(404, str(e))
        cid = db.create_thrust_comparison(result.get("valve_id"), result)
        return {"comparison_id": cid, **result}

    @app.get("/stemthrust/comparisons/{comparison_id}")
    def get_thrust_comparison(comparison_id: int, db=Depends(get_db)):
        c = db.get_thrust_comparison(comparison_id)
        if not c:
            raise HTTPException(404, "比较记录不存在")
        return c

    @app.get("/stemthrust/analyses/{analysis_id}/export")
    def export_thrust_json(analysis_id: int, db=Depends(get_db)):
        a = db.get_thrust_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "阀杆推力分析不存在")
        return JSONResponse(
            content=a,
            headers={"Content-Disposition":
                     f'attachment; filename="stemthrust_{analysis_id}_v{a["version"]}.json"'})

    @app.get("/stemthrust/analyses/{analysis_id}/report", response_class=HTMLResponse)
    def thrust_html_report(analysis_id: int, db=Depends(get_db)):
        a = db.get_thrust_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "阀杆推力分析不存在")
        t = db.get_thrust_test(a["thrust_test_id"])
        valve = db.get_valve(t["valve_id"])
        return thrust_report.render_thrust_report(a, t, valve)

    # ===================== 单相液体流量曲线校核 =====================

    @app.post("/flowcurve/tests", status_code=201)
    def submit_flowcurve_test(body: schemas.FCTestSubmission, db=Depends(get_db)):
        valve = db.get_valve_by_tag(body.valve_tag)
        if valve is None:
            vid = db.create_valve(body.valve_tag, body.valve_description)
            valve = db.get_valve(vid)
        payload = body.model_dump()
        fc_test_id = db.create_flowcurve_test(
            valve_id=valve["id"],
            phase=body.phase,
            test_started_at=body.test_started_at,
            flow_direction=body.flow_direction,
            characteristic=body.valve.characteristic,
            conditions=body.conditions.model_dump(),
            thresholds=body.thresholds.model_dump(),
            calibration_valid_until=body.calibration_valid_until,
            payload=payload,
        )
        return {"flowcurve_test_id": fc_test_id, "valve_id": valve["id"]}

    @app.get("/flowcurve/tests/{fc_test_id}")
    def get_flowcurve_test(fc_test_id: int, db=Depends(get_db)):
        t = db.get_flowcurve_test(fc_test_id)
        if not t:
            raise HTTPException(404, "流量曲线校核测试不存在")
        t["analyses"] = db.list_flowcurve_analyses(fc_test_id)
        return t

    @app.post("/flowcurve/tests/{fc_test_id}/analyze", status_code=201)
    def analyze_flowcurve(fc_test_id: int, body: schemas.AnalyzeRequest | None = None,
                          db=Depends(get_db)):
        if db.get_flowcurve_test(fc_test_id) is None:
            raise HTTPException(404, "流量曲线校核测试不存在")
        author = body.author if body else "auto"
        bindings = body.calibration_bindings if body else None
        return fc_analysis.run_flowcurve_analysis(
            db, fc_test_id, author=author, bindings_override=bindings)

    @app.get("/flowcurve/tests/{fc_test_id}/analyses")
    def list_flowcurve_analyses(fc_test_id: int, db=Depends(get_db)):
        return db.list_flowcurve_analyses(fc_test_id)

    @app.get("/flowcurve/analyses/{analysis_id}")
    def get_flowcurve_analysis(analysis_id: int, db=Depends(get_db)):
        a = db.get_flowcurve_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "流量曲线分析不存在")
        return a

    @app.post("/flowcurve/analyses/{analysis_id}/adjust", status_code=201)
    def adjust_flowcurve(analysis_id: int, body: schemas.FCAdjustRequest,
                         db=Depends(get_db)):
        base = db.get_flowcurve_analysis(analysis_id)
        if not base:
            raise HTTPException(404, "流量曲线分析不存在")
        if not body.plateau_moves and not body.disabled_points \
                and body.calibration_bindings is None:
            raise HTTPException(422, "调整请求为空：需包含 plateau_moves、disabled_points 或 calibration_bindings")
        for m in body.plateau_moves:
            if not m.reason.strip():
                raise HTTPException(422, "人工移动平台边界必须填写理由")
        for d in body.disabled_points:
            if not d.reason.strip():
                raise HTTPException(422, "停用测点必须填写理由")
        return fc_analysis.run_flowcurve_analysis(
            db, base["fc_test_id"], author=body.author,
            new_adjustments={
                "plateau_moves": [m.model_dump() for m in body.plateau_moves],
                "disabled_points": [d.model_dump() for d in body.disabled_points],
                "calibration_bindings": body.calibration_bindings,
            },
            bindings_override=body.calibration_bindings)

    @app.post("/flowcurve/comparisons", status_code=201)
    def create_flowcurve_comparison(body: schemas.FCCompareRequest, db=Depends(get_db)):
        try:
            if body.analysis_ids:
                result = fc_compare.compare_flowcurve_tests(
                    db, analysis_ids=body.analysis_ids)
            else:
                if not body.valve_tag:
                    raise HTTPException(422, "未指定 analysis_ids 时必须提供 valve_tag")
                result = fc_compare.compare_flowcurve_tests(db, valve_tag=body.valve_tag)
        except KeyError as e:
            raise HTTPException(404, str(e))
        cid = db.create_flowcurve_comparison(result.get("valve_id"), result)
        return {"comparison_id": cid, **result}

    @app.get("/flowcurve/comparisons/{comparison_id}")
    def get_flowcurve_comparison(comparison_id: int, db=Depends(get_db)):
        c = db.get_flowcurve_comparison(comparison_id)
        if not c:
            raise HTTPException(404, "比较记录不存在")
        return c

    @app.get("/flowcurve/analyses/{analysis_id}/export")
    def export_flowcurve_json(analysis_id: int, db=Depends(get_db)):
        a = db.get_flowcurve_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "流量曲线分析不存在")
        return JSONResponse(
            content=a,
            headers={"Content-Disposition":
                     f'attachment; filename="flowcurve_{analysis_id}_v{a["version"]}.json"'})

    @app.get("/flowcurve/analyses/{analysis_id}/report", response_class=HTMLResponse)
    def flowcurve_html_report(analysis_id: int, db=Depends(get_db)):
        a = db.get_flowcurve_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "流量曲线分析不存在")
        t = db.get_flowcurve_test(a["fc_test_id"])
        valve = db.get_valve(t["valve_id"])
        return fc_report.render_fc_report(a, t, valve)

    # ===================== 限位开关（开/关到位接点）诊断 =====================

    @app.post("/limitswitch/tests", status_code=201)
    def submit_limitswitch_test(body: schemas.LSTestSubmission, db=Depends(get_db)):
        valve = db.get_valve_by_tag(body.valve_tag)
        if valve is None:
            vid = db.create_valve(body.valve_tag, body.valve_description)
            valve = db.get_valve(vid)
        payload = body.model_dump()
        ls_test_id = db.create_limitswitch_test(
            valve_id=valve["id"],
            phase=body.phase,
            test_started_at=body.test_started_at,
            range_min=body.range.min,
            range_max=body.range.max,
            range_unit=body.range.unit,
            conditions=body.conditions.model_dump(),
            thresholds=body.thresholds.model_dump(),
            calibration_valid_until=body.calibration_valid_until,
            payload=payload,
        )
        return {"limitswitch_test_id": ls_test_id, "valve_id": valve["id"]}

    @app.get("/limitswitch/tests/{ls_test_id}")
    def get_limitswitch_test(ls_test_id: int, db=Depends(get_db)):
        t = db.get_limitswitch_test(ls_test_id)
        if not t:
            raise HTTPException(404, "限位开关测试不存在")
        t["analyses"] = db.list_limitswitch_analyses(ls_test_id)
        return t

    @app.post("/limitswitch/tests/{ls_test_id}/analyze", status_code=201)
    def analyze_limitswitch(ls_test_id: int, body: schemas.AnalyzeRequest | None = None,
                            db=Depends(get_db)):
        if db.get_limitswitch_test(ls_test_id) is None:
            raise HTTPException(404, "限位开关测试不存在")
        author = body.author if body else "auto"
        bindings = body.calibration_bindings if body else None
        return ls_analysis.run_limitswitch_analysis(
            db, ls_test_id, author=author, bindings_override=bindings)

    @app.get("/limitswitch/tests/{ls_test_id}/analyses")
    def list_limitswitch_analyses(ls_test_id: int, db=Depends(get_db)):
        return db.list_limitswitch_analyses(ls_test_id)

    @app.get("/limitswitch/analyses/{analysis_id}")
    def get_limitswitch_analysis(analysis_id: int, db=Depends(get_db)):
        a = db.get_limitswitch_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "限位开关分析不存在")
        return a

    @app.post("/limitswitch/analyses/{analysis_id}/adjust", status_code=201)
    def adjust_limitswitch(analysis_id: int, body: schemas.LSAdjustRequest,
                           db=Depends(get_db)):
        base = db.get_limitswitch_analysis(analysis_id)
        if not base:
            raise HTTPException(404, "限位开关分析不存在")
        if body.channel_rebind is None and body.polarity_override is None \
                and not body.ignore_glitches and body.calibration_bindings is None:
            raise HTTPException(422, "调整请求为空：需包含 channel_rebind、"
                                     "polarity_override、ignore_glitches 或 calibration_bindings")
        new_adj = {}
        if body.channel_rebind is not None:
            if not body.channel_rebind.reason.strip():
                raise HTTPException(422, "改绑通道必须填写理由")
            cmap = body.channel_rebind.channel_map
            if set(cmap.keys()) != {"open", "closed"} \
                    or set(cmap.values()) != {"open", "closed"}:
                raise HTTPException(422, "channel_map 须为 open/closed 的一一置换")
            new_adj["channel_rebind"] = body.channel_rebind.model_dump()
        if body.polarity_override is not None:
            if not body.polarity_override.reason.strip():
                raise HTTPException(422, "纠正极性必须填写理由")
            new_adj["polarity_override"] = body.polarity_override.model_dump()
        for ig in body.ignore_glitches:
            if not ig.reason.strip():
                raise HTTPException(422, "忽略毛刺必须填写理由")
            if ig.t_end < ig.t_start:
                raise HTTPException(422, "忽略毛刺区间终点不得早于起点")
        new_adj["ignore_glitches"] = [g.model_dump() for g in body.ignore_glitches]
        new_adj["calibration_bindings"] = body.calibration_bindings
        return ls_analysis.run_limitswitch_analysis(
            db, base["ls_test_id"], author=body.author,
            new_adjustments=new_adj,
            bindings_override=body.calibration_bindings)

    @app.post("/limitswitch/comparisons", status_code=201)
    def create_limitswitch_comparison(body: schemas.LSCompareRequest,
                                      db=Depends(get_db)):
        try:
            if body.analysis_ids:
                result = ls_compare.compare_limitswitch_tests(
                    db, analysis_ids=body.analysis_ids)
            else:
                if not body.valve_tag:
                    raise HTTPException(422, "未指定 analysis_ids 时必须提供 valve_tag")
                result = ls_compare.compare_limitswitch_tests(
                    db, valve_tag=body.valve_tag)
        except KeyError as e:
            raise HTTPException(404, str(e))
        cid = db.create_limitswitch_comparison(result.get("valve_id"), result)
        return {"comparison_id": cid, **result}

    @app.get("/limitswitch/comparisons/{comparison_id}")
    def get_limitswitch_comparison(comparison_id: int, db=Depends(get_db)):
        c = db.get_limitswitch_comparison(comparison_id)
        if not c:
            raise HTTPException(404, "比较记录不存在")
        return c

    @app.get("/limitswitch/analyses/{analysis_id}/export")
    def export_limitswitch_json(analysis_id: int, db=Depends(get_db)):
        a = db.get_limitswitch_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "限位开关分析不存在")
        return JSONResponse(
            content=a,
            headers={"Content-Disposition":
                     f'attachment; filename="limitswitch_{analysis_id}_v{a["version"]}.json"'})

    @app.get("/limitswitch/analyses/{analysis_id}/report", response_class=HTMLResponse)
    def limitswitch_html_report(analysis_id: int, db=Depends(get_db)):
        a = db.get_limitswitch_analysis(analysis_id)
        if not a:
            raise HTTPException(404, "限位开关分析不存在")
        t = db.get_limitswitch_test(a["ls_test_id"])
        valve = db.get_valve(t["valve_id"])
        return ls_report.render_ls_report(a, t, valve)

    # ---- 请求样例 ----
    @app.get("/samples")
    def list_samples():
        if not SAMPLES_DIR.is_dir():
            return []
        return sorted(p.stem for p in SAMPLES_DIR.glob("*.json"))

    @app.get("/samples/{name}")
    def get_sample(name: str):
        p = SAMPLES_DIR / f"{name}.json"
        if not p.is_file():
            raise HTTPException(404, "样例不存在")
        return json.loads(p.read_text(encoding="utf-8"))

    return app


app = create_app()
