import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Candidate, Hall, SeatPlan
from app.services.seat_engine import find_violations, place_candidates, plan_to_dict
router = APIRouter(prefix="/seating", tags=["seating"])


def _load_hall(db: Session, hall_id: int) -> Hall:
    hall = db.get(Hall, hall_id)
    if not hall:
        raise HTTPException(404, "考室不存在")
    return hall


def _latest_plan(db: Session, hall_id: int) -> SeatPlan | None:
    return db.scalars(
        select(SeatPlan).where(SeatPlan.hall_id == hall_id).order_by(SeatPlan.id.desc())
    ).first()


@router.post("/run")
def run_seating(hall_id: int = 1, db: Session = Depends(get_db)):
    hall = _load_hall(db, hall_id)
    try:
        cands = [{"id": c.id, "name": c.name, "ticket_no": c.ticket_no, "paper_id": c.paper_id}
                 for c in db.scalars(select(Candidate).where(Candidate.hall_id == hall_id)).all()]
        assigns, unplaced = place_candidates(hall.rows, hall.cols, hall.min_manhattan, cands)
        viols = find_violations(hall.rows, hall.cols, hall.min_manhattan, assigns)
        result = plan_to_dict(assigns, unplaced, viols, hall.rows, hall.cols)
        result["hall"] = {"id": hall.id, "name": hall.name, "min_manhattan": hall.min_manhattan}
        plan = SeatPlan(hall_id=hall_id, created_at=datetime.utcnow(),
                        result_json=json.dumps(result, ensure_ascii=False))
        db.add(plan)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        # 任何失败都必须回滚，不得留下待写入的脏方案
        db.rollback()
        raise HTTPException(500, "排座生成失败")
    db.refresh(plan)
    return {"id": plan.id, **result}

@router.get("/latest")
def latest(hall_id: int = 1, db: Session = Depends(get_db)):
    _load_hall(db, hall_id)  # 非法考室 404，且不产生任何方案行
    plan = _latest_plan(db, hall_id)
    if not plan:
        # 只读语义：无方案时返回 404，绝不隐式触发生成、不得插入新行
        raise HTTPException(404, "暂无排座方案")
    data = json.loads(plan.result_json)
    return {"id": plan.id, **data}

@router.get("/violations")
def violations(hall_id: int = 1, db: Session = Depends(get_db)):
    data = latest(hall_id=hall_id, db=db)
    return {"hall_id": hall_id, "violations": data.get("violations", []), "unplaced": data.get("unplaced", [])}

@router.get("/stats")
def stats(hall_id: int = 1, db: Session = Depends(get_db)):
    data = latest(hall_id=hall_id, db=db)
    return {"hall_id": hall_id, **data.get("stats", {})}
