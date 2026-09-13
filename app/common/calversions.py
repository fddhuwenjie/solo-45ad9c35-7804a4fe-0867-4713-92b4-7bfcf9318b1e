"""仪器校准链 API：逐通道校准版本（版本不可变，续证派生新版本）。"""

from fastapi import APIRouter, Depends, HTTPException

from . import calibration as calchain
from . import schemas
from .deps import get_db

router = APIRouter()


@router.post("/calibration-versions", status_code=201)
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


@router.get("/calibration-versions")
def list_calibration_versions(instrument_serial: str | None = None,
                              measurement_type: str | None = None,
                              db=Depends(get_db)):
    return db.list_calibration_versions(instrument_serial, measurement_type)


@router.get("/calibration-versions/{version_id}")
def get_calibration_version(version_id: int, db=Depends(get_db)):
    v = db.get_calibration_version(version_id)
    if not v:
        raise HTTPException(404, "校准版本不存在")
    return v
