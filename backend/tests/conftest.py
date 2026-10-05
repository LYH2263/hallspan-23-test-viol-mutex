"""接口级集成测试固件：内存 SQLite + 依赖覆盖 get_db。

必须在导入 app 之前固定环境变量（app.config 在导入时读取 settings，
app.database 在导入时按 settings 创建全局 engine）。
"""
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_ON_EMPTY"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import Candidate, Hall, PaperSet, SeatPlan


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=True)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session):
    # 请求结束不关闭 session：测试要在同一 session/连接上直接核对库内真实行数与方案内容。
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    # 关闭 raise_server_exceptions：生成失败必须是可断言的 500 JSON，而不是测试进程里抛异常。
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def make_seed_hall(db, rows: int = 5, cols: int = 6, min_dist: int = 2) -> Hall:
    """按 services/seed.py 的同一批基础数据造 5x6 考室 + A/B/C 三卷 + 12 名考生。"""
    hall = Hall(code="H101", name="一号考室", rows=rows, cols=cols, min_manhattan=min_dist)
    db.add(hall)
    papers = [
        PaperSet(code="P-A", title="语文 A 卷"),
        PaperSet(code="P-B", title="语文 B 卷"),
        PaperSet(code="P-C", title="语文 C 卷"),
    ]
    db.add_all(papers)
    db.flush()
    names = ["陈一", "李二", "张三", "赵四", "钱五", "孙六",
             "周七", "吴八", "郑九", "王十", "冯十一", "陈十二"]
    for i, name in enumerate(names):
        db.add(Candidate(hall_id=hall.id, name=name, ticket_no=f"T{2026001+i}",
                         paper_id=papers[i % len(papers)].id))
    db.commit()
    db.refresh(hall)
    return hall


def seed_layout() -> dict[int, tuple[int, int]]:
    """种子 12 人在 5x6 / min_dist=2 下贪心排座的手工演算坐标（candidate_id -> (row, col)）。

    逐人按行优先试座可手算：行 0 落 1/2/3 于列 0/2/4；行 1 的 4/5/6 只能落列 1/3/5；
    行 2 重复 0/2/4；行 3 重复 1/3/5。12 人全部落座。
    """
    return {
        1: (0, 0), 2: (0, 2), 3: (0, 4),
        4: (1, 1), 5: (1, 3), 6: (1, 5),
        7: (2, 0), 8: (2, 2), 9: (2, 4),
        10: (3, 1), 11: (3, 3), 12: (3, 5),
    }


def paper_of(candidate_id: int) -> int:
    return 1 + (candidate_id - 1) % 3


def reference_violations(assignments: list[dict], min_dist: int) -> list[dict]:
    """线上判定口径的独立重述（不调用 seat_engine，只按规则重新演算一遍）。

    两条规则各自独立成条，不许并成一句：
      1) 任意两人曼哈顿距离 d < min_dist        -> kind=distance
      2) 同试卷套且坐标互为上下左右四邻（d==1）   -> kind=same_paper_adjacent
    对角（dr=1,dc=1，d==2）不是四邻，不得记 same_paper_adjacent。
    """
    out: list[dict] = []
    four = ((0, 1), (0, -1), (1, 0), (-1, 0))
    for i, a in enumerate(assignments):
        for b in assignments[i + 1:]:
            d = abs(a["row"] - b["row"]) + abs(a["col"] - b["col"])
            if d < min_dist:
                out.append({"kind": "distance", "a_id": a["candidate_id"], "b_id": b["candidate_id"],
                            "detail": f"曼哈顿距离 {d} < 最小要求 {min_dist}"})
            is_four_neighbor = (
                a["paper_id"] == b["paper_id"]
                and (b["row"] - a["row"], b["col"] - a["col"]) in four
            )
            if is_four_neighbor:
                out.append({"kind": "same_paper_adjacent", "a_id": a["candidate_id"],
                            "b_id": b["candidate_id"],
                            "detail": f"同试卷套 {a['paper_id']} 四邻相邻"})
    return out


def norm(viols: list[dict]) -> list[tuple]:
    return sorted((v["kind"], v["a_id"], v["b_id"], v["detail"]) for v in viols)
