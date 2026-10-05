"""接口级集成测试：同时打 生成接口 / 违规列表接口 / 方案表行数，三源互证。

约束逐条落地：
- 生成回包、违规回包、库内最新方案必须同一套结论（只绿一处不算对）。
- 曼哈顿间距违规与同卷四邻违规是两条独立记录，不得并成一句。
- 对角同套（d==2 的斜对角）不得记成四邻相邻。
- 无方案时读最新不得插入新行（合法空考室、非法考室都覆盖）。
- 生成失败（引擎抛错 / 落库抛错）不得留下脏方案。
- 用种子 12 人手工演算间距与四邻，不接受"另写放宽引擎装绿"。
"""
import json

import pytest
from sqlalchemy import func, select

from app.models.models import SeatPlan
from app.services.seat_engine import SeatAssign, find_violations
from conftest import make_seed_hall, norm, paper_of, reference_violations, seed_layout


def plan_count(db) -> int:
    return db.scalar(select(func.count()).select_from(SeatPlan)) or 0


# ---------------------------------------------------------------------------
# 1) 种子 12 人：生成回包 / 违规回包 / 库内最新方案 / 表行数，同一套结论
# ---------------------------------------------------------------------------

def test_seed_twelve_run_violations_latest_db_same_conclusion(client, db_session):
    hall = make_seed_hall(db_session)  # 5x6, min_dist=2, 12 人, A/B/C 循环

    # —— 手工演算的期望坐标（逐人行优先试座即可推出，见 conftest.seed_layout）——
    expected = seed_layout()

    # 生成接口
    run = client.post(f"/api/seating/run?hall_id={hall.id}")
    assert run.status_code == 200
    run_body = run.json()

    # 方案表恰好新增一行，且回包 id 与库内主键一致
    assert plan_count(db_session) == 1
    db_row = db_session.scalar(select(SeatPlan).where(SeatPlan.hall_id == hall.id))
    assert db_row is not None
    assert run_body["id"] == db_row.id

    # 库内最新方案就是生成回包落库的内容（去掉响应层 id 后必须逐字一致）
    assert json.loads(db_row.result_json) == {k: v for k, v in run_body.items() if k != "id"}

    # 违规列表接口
    viol_resp = client.get(f"/api/seating/violations?hall_id={hall.id}")
    assert viol_resp.status_code == 200
    viol_body = viol_resp.json()

    # 读最新接口
    latest_resp = client.get(f"/api/seating/latest?hall_id={hall.id}")
    assert latest_resp.status_code == 200
    latest_body = latest_resp.json()

    # —— 三源同一套结论：违规集合、未排上、座位分配完全一致 ——
    assert latest_body["id"] == db_row.id == run_body["id"]
    assert norm(viol_body["violations"]) == norm(run_body["violations"]) == norm(latest_body["violations"])
    assert viol_body["unplaced"] == run_body["unplaced"] == latest_body["unplaced"]
    assert latest_body["assignments"] == run_body["assignments"]
    assert latest_body["stats"] == run_body["stats"]

    # —— 与手工演算的布局逐人核对（不是"引擎说啥就是啥"）——
    assert run_body["stats"] == {"seated": 12, "unplaced": 0, "violations": 0, "capacity": 30}
    got = {a["candidate_id"]: (a["row"], a["col"]) for a in run_body["assignments"]}
    assert got == expected
    for a in run_body["assignments"]:
        assert a["paper_id"] == paper_of(a["candidate_id"])

    # —— 用线上判定口径的独立重述再演算一遍：必须与三个接口的结论完全相同 ——
    assert norm(reference_violations(run_body["assignments"], hall.min_manhattan)) == \
        norm(run_body["violations"])

    # 手工再证一遍：任意两人 d>=2；不存在同卷四邻
    assigns = run_body["assignments"]
    for i, a in enumerate(assigns):
        for b in assigns[i + 1:]:
            d = abs(a["row"] - b["row"]) + abs(a["col"] - b["col"])
            assert d >= hall.min_manhattan
            assert not (a["paper_id"] == b["paper_id"] and d == 1)


def test_seed_diagonal_same_paper_is_not_four_neighbor(client, db_session):
    """种子布局里存在多对同卷对角（如 1 号(0,0) 与 4 号(1,1)），距离恰好达标，
    对角不是上下左右四邻，三个数据源都不得记 same_paper_adjacent。"""
    hall = make_seed_hall(db_session)
    run_body = client.post(f"/api/seating/run?hall_id={hall.id}").json()

    by_id = {a["candidate_id"]: a for a in run_body["assignments"]}
    a, b = by_id[1], by_id[4]
    assert a["paper_id"] == b["paper_id"] == 1
    assert abs(a["row"] - b["row"]) == 1 and abs(a["col"] - b["col"]) == 1  # 对角
    assert abs(a["row"] - b["row"]) + abs(a["col"] - b["col"]) == 2          # 间距恰好达标

    viol_body = client.get(f"/api/seating/violations?hall_id={hall.id}").json()
    latest_body = client.get(f"/api/seating/latest?hall_id={hall.id}").json()
    for body in (run_body, latest_body):
        assert [v for v in body["violations"]
                if {v["a_id"], v["b_id"]} == {1, 4}] == []
    assert all(v["kind"] != "same_paper_adjacent" for v in viol_body["violations"])
    assert all(v["kind"] != "distance" for v in viol_body["violations"])
    # 独立重述同样不得产生该违规
    assert not any({v["a_id"], v["b_id"]} == {1, 4}
                   for v in reference_violations(run_body["assignments"], hall.min_manhattan))


# ---------------------------------------------------------------------------
# 2) 两种违规必须各自成条；同卷四邻 + 间距不足同时命中也不得并句
#    （生成器按规则不会产出违规，故构造一条库内历史方案，经 latest/violations
#      接口读出，并与线上口径 find_violations 及独立重述三方对齐）
# ---------------------------------------------------------------------------

def test_distance_and_same_paper_four_neighbor_are_separate_records(client, db_session):
    hall = make_seed_hall(db_session, rows=2, cols=3)
    # 2x3 内手工摆放（min_dist=2）：
    #   1 号 卷1 (0,0)；2 号 卷1 (0,1)：同卷四邻且 d=1<2 —— 同时命中两条
    #   3 号 卷1 (1,1)：与 2 号同卷四邻；与 1 号是同卷对角(d=2) —— 对角不得有记录
    assigns = [
        SeatAssign(1, "陈一", "T2026001", 1, 0, 0),
        SeatAssign(2, "李二", "T2026002", 1, 0, 1),
        SeatAssign(3, "张三", "T2026003", 1, 1, 1),
    ]
    engine_viols = find_violations(hall.rows, hall.cols, hall.min_manhattan, assigns)
    stored = {
        "rows": hall.rows, "cols": hall.cols,
        "assignments": [a.__dict__ for a in assigns],
        "unplaced": [],
        "violations": [v.__dict__ for v in engine_viols],
        "stats": {"seated": 3, "unplaced": 0, "violations": len(engine_viols),
                  "capacity": hall.rows * hall.cols},
        "hall": {"id": hall.id, "name": hall.name, "min_manhattan": hall.min_manhattan},
    }
    db_session.add(SeatPlan(hall_id=hall.id, result_json=json.dumps(stored, ensure_ascii=False)))
    db_session.commit()
    assert plan_count(db_session) == 1

    viol_body = client.get(f"/api/seating/violations?hall_id={hall.id}").json()["violations"]
    latest_body = client.get(f"/api/seating/latest?hall_id={hall.id}").json()

    # 线上口径、独立重述、违规接口、最新方案 —— 四处结论一致
    expected = norm(reference_violations(stored["assignments"], hall.min_manhattan))
    assert norm([v.__dict__ for v in engine_viols]) == expected
    assert norm(viol_body) == expected
    assert norm(latest_body["violations"]) == expected

    # 期望恰好 4 条：(1,2) 与 (2,3) 各自同时产生 distance 与 same_paper_adjacent
    pair_12 = [v for v in viol_body if {v["a_id"], v["b_id"]} == {1, 2}]
    pair_23 = [v for v in viol_body if {v["a_id"], v["b_id"]} == {2, 3}]
    assert {v["kind"] for v in pair_12} == {"distance", "same_paper_adjacent"}
    assert {v["kind"] for v in pair_23} == {"distance", "same_paper_adjacent"}
    assert len(viol_body) == 4

    # 两类是两条独立记录、两句不同的话，严禁并成一句
    details = {(v["kind"], v["detail"]) for v in pair_12}
    assert len(details) == 2
    assert any(v["kind"] == "distance" and "曼哈顿距离" in v["detail"] for v in pair_12)
    assert any(v["kind"] == "same_paper_adjacent" and "四邻相邻" in v["detail"] for v in pair_12)

    # 对角同套 (1,3) 不得出现任何违规
    assert not any({v["a_id"], v["b_id"]} == {1, 3} for v in viol_body)


# ---------------------------------------------------------------------------
# 3) 无方案时读取：合法空考室 / 非法考室都零增行
# ---------------------------------------------------------------------------

def test_read_latest_without_plan_inserts_nothing(client, db_session):
    hall = make_seed_hall(db_session)
    # 删掉考生，制造"考室合法但从未生成"
    from app.models.models import Candidate
    db_session.query(Candidate).delete()
    db_session.commit()
    assert plan_count(db_session) == 0

    for path in ("/api/seating/latest", "/api/seating/violations", "/api/seating/stats"):
        resp = client.get(f"{path}?hall_id={hall.id}")
        assert resp.status_code == 404, path
        assert plan_count(db_session) == 0, f"{path} 不得隐式生成方案行"


def test_illegal_hall_reads_and_run_add_zero_rows(client, db_session):
    assert plan_count(db_session) == 0
    for path in ("/api/seating/latest", "/api/seating/violations", "/api/seating/stats"):
        resp = client.get(f"{path}?hall_id=9999")
        assert resp.status_code == 404, path
        assert plan_count(db_session) == 0, "非法考室读取零增行"
    resp = client.post("/api/seating/run?hall_id=9999")
    assert resp.status_code == 404
    assert plan_count(db_session) == 0


# ---------------------------------------------------------------------------
# 4) 生成失败不得留下脏方案
# ---------------------------------------------------------------------------

def test_run_engine_error_leaves_no_dirty_plan(client, db_session, monkeypatch):
    hall = make_seed_hall(db_session)

    def boom(*_args, **_kwargs):
        raise RuntimeError("排座引擎爆炸")

    monkeypatch.setattr("app.api.seating.place_candidates", boom)
    resp = client.post(f"/api/seating/run?hall_id={hall.id}")
    assert resp.status_code == 500
    assert plan_count(db_session) == 0
    # 失败后库仍可用：读最新是 404（没有残留脏方案），且不新增行
    assert client.get(f"/api/seating/latest?hall_id={hall.id}").status_code == 404
    assert plan_count(db_session) == 0


def test_run_commit_error_rolls_back_no_dirty_plan(client, db_session, monkeypatch):
    hall = make_seed_hall(db_session)

    def failing_commit():
        raise RuntimeError("数据库提交失败")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    resp = client.post(f"/api/seating/run?hall_id={hall.id}")
    assert resp.status_code == 500
    # 接口必须自行回滚：用【同一个 session】直接查（测试不代劳 rollback）。
    # 若接口没回滚，session 处于 inactive 状态，此查询必抛 PendingRollbackError -> 测试红。
    assert plan_count(db_session) == 0
    # 恢复后库仍可用，且读不到残留脏方案
    monkeypatch.undo()
    assert client.get(f"/api/seating/latest?hall_id={hall.id}").status_code == 404
    db_session.rollback()


# ---------------------------------------------------------------------------
# 5) 重复生成：行数递增且三处始终指向库内最新方案
# ---------------------------------------------------------------------------

def test_rerun_row_count_and_latest_consistency(client, db_session):
    hall = make_seed_hall(db_session)
    first = client.post(f"/api/seating/run?hall_id={hall.id}").json()
    second = client.post(f"/api/seating/run?hall_id={hall.id}").json()
    assert plan_count(db_session) == 2
    assert second["id"] > first["id"]

    latest_body = client.get(f"/api/seating/latest?hall_id={hall.id}").json()
    viol_body = client.get(f"/api/seating/violations?hall_id={hall.id}").json()
    newest = db_session.scalar(
        select(SeatPlan).where(SeatPlan.hall_id == hall.id).order_by(SeatPlan.id.desc()))
    assert latest_body["id"] == second["id"] == newest.id
    assert json.loads(newest.result_json)["assignments"] == latest_body["assignments"]
    assert norm(viol_body["violations"]) == norm(second["violations"])


# ---------------------------------------------------------------------------
# 6) 容量不足：未排上名单同样要三源一致
# ---------------------------------------------------------------------------

def test_over_capacity_unplaced_consistent_across_sources(client, db_session):
    hall = make_seed_hall(db_session, rows=2, cols=2, min_dist=3)  # 12 人抢满足 d>=3 的座
    run_body = client.post(f"/api/seating/run?hall_id={hall.id}").json()
    assert run_body["stats"]["seated"] + run_body["stats"]["unplaced"] == 12
    assert run_body["stats"]["capacity"] == 4
    assert len(run_body["unplaced"]) >= 1

    viol_body = client.get(f"/api/seating/violations?hall_id={hall.id}").json()
    latest_body = client.get(f"/api/seating/latest?hall_id={hall.id}").json()
    assert viol_body["unplaced"] == run_body["unplaced"] == latest_body["unplaced"]
    assert plan_count(db_session) == 1
    assert latest_body["stats"]["unplaced"] == run_body["stats"]["unplaced"]
