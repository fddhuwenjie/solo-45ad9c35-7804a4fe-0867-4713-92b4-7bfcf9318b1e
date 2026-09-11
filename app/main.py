"""调节阀全行程诊断服务 API。"""

import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from . import analysis, pairing, report, schemas
from .db import Database

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
        return analysis.run_analysis(db, test_id, author=author)

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
        if not body.boundary_moves and not body.exclusions:
            raise HTTPException(422, "调整请求为空：需包含 boundary_moves 或 exclusions")
        return analysis.run_analysis(
            db, base["test_id"], author=body.author,
            new_adjustments={
                "boundary_moves": [m.model_dump() for m in body.boundary_moves],
                "exclusions": [e.model_dump() for e in body.exclusions],
            })

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
