"""阀门档案 API。"""

from fastapi import APIRouter, Depends, HTTPException

from .deps import get_db

router = APIRouter()


@router.post("/valves", status_code=201)
def create_valve(body: dict, db=Depends(get_db)):
    tag = (body.get("tag") or "").strip()
    if not tag:
        raise HTTPException(422, "tag 不能为空")
    if db.get_valve_by_tag(tag):
        raise HTTPException(409, f"阀门 {tag} 已存在")
    vid = db.create_valve(tag, body.get("description", ""))
    return db.get_valve(vid)


@router.get("/valves")
def list_valves(db=Depends(get_db)):
    return db.list_valves()


@router.get("/valves/{valve_id}")
def get_valve(valve_id: int, db=Depends(get_db)):
    v = db.get_valve(valve_id)
    if not v:
        raise HTTPException(404, "阀门不存在")
    v["tests"] = db.list_tests(valve_id)
    return v
