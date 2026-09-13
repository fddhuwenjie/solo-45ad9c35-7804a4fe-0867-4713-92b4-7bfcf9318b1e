"""请求样例 API。"""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

SAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "samples"

router = APIRouter()


@router.get("/samples")
def list_samples():
    if not SAMPLES_DIR.is_dir():
        return []
    return sorted(p.stem for p in SAMPLES_DIR.glob("*.json"))


@router.get("/samples/{name}")
def get_sample(name: str):
    p = SAMPLES_DIR / f"{name}.json"
    if not p.is_file():
        raise HTTPException(404, "样例不存在")
    return json.loads(p.read_text(encoding="utf-8"))
