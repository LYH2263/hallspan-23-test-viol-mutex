"""API 级测例：生成接口、违规列表接口、方案表行数三方互证。

判定口径一律来自 app.services.seat_engine（线上同一套引擎），
本文件不另写任何放宽版引擎来装绿。
"""
import json
from dataclasses import asdict
from itertools import combinations
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import Candidate, Hall, SeatPlan
from app.services.seat_engine import (
    SeatAssign,
    find_violations,
    manhattan,
    neighbors4,
    place_candidates,
    plan_to_dict,
)
from app.services.seed import seed_if_empty

# 种子十二人（H101，5 行 6 列，min_manhattan=2，试卷套 1/2/3 循环）
# 手工演算的贪心落座结果：全部落座、零违规
EXPECTED_SEATS = [
    (0, 0), (0, 2), (0, 4), (1, 1), (1, 3), (1, 5),
    (2, 0), (2, 2), (2, 4), (3, 1), (3, 3), (3, 5),
]


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    with Session() as db:
        seed_if_empty(db)  # 真实种子：考室 H101 + 十二名考生 + 三套卷

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    client = TestClient(app, raise_server_exceptions=False)
    yield SimpleNamespace(client=client, Session=Session)
    app.dependency_overrides.clear()
    engine.dispose()


def count_plans(env, hall_id=None):
    with env.Session() as db:
        q = select(func.count()).select_from(SeatPlan)
        if hall_id is not None:
            q = q.where(SeatPlan.hall_id == hall_id)
        return db.scalar(q) or 0


def seed_candidates(env):
    with env.Session() as db:
        hall = db.get(Hall, 1)
        dims = (hall.rows, hall.cols, hall.min_manhattan)
        cands = [
            {"id": c.id, "name": c.name, "ticket_no": c.ticket_no, "paper_id": c.paper_id}
            for c in db.scalars(select(Candidate).where(Candidate.hall_id == 1)).all()
        ]
    return dims, cands


def test_run_violations_and_db_latest_share_one_conclusion(env):
    client = env.client
    assert count_plans(env) == 0

    resp = client.post("/api/seating/run", params={"hall_id": 1})
    assert resp.status_code == 200
    run = resp.json()
    assert count_plans(env) == 1  # 生成接口恰好新增一行方案

    viols_resp = client.get("/api/seating/violations", params={"hall_id": 1})
    latest_resp = client.get("/api/seating/latest", params={"hall_id": 1})
    stats_resp = client.get("/api/seating/stats", params={"hall_id": 1})
    assert viols_resp.status_code == 200
    assert latest_resp.status_code == 200
    assert stats_resp.status_code == 200
    assert count_plans(env) == 1  # 读接口一律不得插新行

    with env.Session() as db:
        plan = db.scalars(
            select(SeatPlan).where(SeatPlan.hall_id == 1).order_by(SeatPlan.id.desc())
        ).first()
        plan_id, stored = plan.id, json.loads(plan.result_json)

    # 同一批座位：生成回包、违规回包、库内最新方案必须同一套结论
    assert latest_resp.json()["id"] == run["id"] == plan_id
    assert run["violations"] == viols_resp.json()["violations"] == stored["violations"]
    assert run["unplaced"] == viols_resp.json()["unplaced"] == stored["unplaced"]
    assert run["assignments"] == latest_resp.json()["assignments"] == stored["assignments"]
    for key, val in run["stats"].items():
        assert stats_resp.json()[key] == val == stored["stats"][key]

    # 与线上判定口径互证：用 app 内同一套引擎对同一批考生重算
    (rows, cols, min_dist), cands = seed_candidates(env)
    exp_assigns, exp_unplaced = place_candidates(rows, cols, min_dist, cands)
    exp_viols = find_violations(rows, cols, min_dist, exp_assigns)
    assert [(a.candidate_id, a.row, a.col) for a in exp_assigns] == [
        (a["candidate_id"], a["row"], a["col"]) for a in run["assignments"]
    ]
    assert [asdict(v) for v in exp_viols] == run["violations"]
    assert exp_unplaced == [] == run["unplaced"]

    # 种子十二人，手工演算：落座位置、间距、同卷四邻逐条可核
    assert len(cands) == 12
    assert [(a["row"], a["col"]) for a in run["assignments"]] == EXPECTED_SEATS
    assert run["stats"] == {"seated": 12, "unplaced": 0, "violations": 0, "capacity": 30}

    seats = [(a["row"], a["col"]) for a in run["assignments"]]
    for p, q in combinations(seats, 2):
        assert manhattan(p, q) >= min_dist  # 任意两人间距达标

    same_paper = [
        (a, b) for a, b in combinations(run["assignments"], 2)
        if a["paper_id"] == b["paper_id"]
    ]
    assert same_paper  # 种子里确有同卷考生
    for a, b in same_paper:
        assert (b["row"], b["col"]) not in neighbors4(a["row"], a["col"], rows, cols)

    # 对角同套在这批座位上真实存在，三处结论都不得记成四邻相邻
    diagonal_same = [
        (a, b) for a, b in same_paper
        if abs(a["row"] - b["row"]) == 1 and abs(a["col"] - b["col"]) == 1
    ]
    assert diagonal_same
    assert run["violations"] == viols_resp.json()["violations"] == stored["violations"] == []


def inject_plan(env, assigns, viols, rows=5, cols=6):
    result = plan_to_dict(assigns, [], viols, rows, cols)
    with env.Session() as db:
        db.add(SeatPlan(hall_id=1, result_json=json.dumps(result, ensure_ascii=False)))
        db.commit()


def test_violation_kinds_stay_separate_through_api(env):
    # 同卷四邻且间距不足：线上引擎判两条独立违规
    both = [SeatAssign(1, "甲", "T1", 1, 0, 0), SeatAssign(2, "乙", "T2", 1, 0, 1)]
    both_viols = find_violations(5, 6, 2, both)
    assert sorted(v.kind for v in both_viols) == ["distance", "same_paper_adjacent"]
    inject_plan(env, both, both_viols)

    resp = env.client.get("/api/seating/violations", params={"hall_id": 1})
    assert resp.status_code == 200
    kinds = [v["kind"] for v in resp.json()["violations"]]
    # 曼哈顿间距违规与同卷四邻违规各自成条，不得并成一句
    assert kinds == [v.kind for v in both_viols]
    assert len(kinds) == 2
    assert "distance" in kinds
    assert "same_paper_adjacent" in kinds

    # 对角同套：线上口径不算四邻，违规列表接口也不得读出四邻违规
    diag = [SeatAssign(3, "丙", "T3", 2, 0, 0), SeatAssign(4, "丁", "T4", 2, 1, 1)]
    diag_viols = find_violations(5, 6, 2, diag)
    assert diag_viols == []
    inject_plan(env, diag, diag_viols)
    resp = env.client.get("/api/seating/violations", params={"hall_id": 1})
    assert resp.status_code == 200
    assert resp.json()["violations"] == []


def test_latest_without_plan_inserts_no_row(env):
    assert count_plans(env) == 0
    for path in ("/api/seating/latest", "/api/seating/violations", "/api/seating/stats"):
        resp = env.client.get(path, params={"hall_id": 1})
        assert resp.status_code == 404, path
    assert count_plans(env) == 0  # 无方案时读最新不得插入新行


def test_illegal_hall_reads_insert_nothing(env):
    assert count_plans(env) == 0
    for path in ("/api/seating/latest", "/api/seating/violations", "/api/seating/stats"):
        resp = env.client.get(path, params={"hall_id": 9999})
        assert resp.status_code == 404, path
    resp = env.client.post("/api/seating/run", params={"hall_id": 9999})
    assert resp.status_code == 404
    assert count_plans(env) == 0  # 非法考室读取/生成一律零增行
    assert count_plans(env, hall_id=9999) == 0


def test_failed_generation_leaves_no_dirty_plan(env, monkeypatch):
    from app.api import seating as seating_api

    def boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(seating_api, "place_candidates", boom)
    resp = env.client.post("/api/seating/run", params={"hall_id": 1})
    assert resp.status_code == 500
    assert count_plans(env) == 0  # 生成失败不得留下脏方案

    monkeypatch.undo()
    resp = env.client.post("/api/seating/run", params={"hall_id": 1})
    assert resp.status_code == 200
    assert count_plans(env) == 1  # 失败那次没留脏数据，恢复后恰好新增一行
