from app.services.seat_engine import find_violations, manhattan, place_candidates, SeatAssign

def test_manhattan():
    assert manhattan((0, 0), (2, 1)) == 3

def test_min_distance_placement():
    cands = [{"id": i, "name": f"C{i}", "ticket_no": f"T{i}", "paper_id": 1 + (i % 2)} for i in range(4)]
    assigns, unplaced = place_candidates(4, 4, 2, cands)
    assert len(assigns) + len(unplaced) == 4
    for i, a in enumerate(assigns):
        for b in assigns[i+1:]:
            assert manhattan((a.row, a.col), (b.row, b.col)) >= 2

def test_same_paper_not_adjacent_in_result():
    # Force two same paper — engine should avoid 4-neigh
    cands = [
        {"id": 1, "name": "A", "ticket_no": "T1", "paper_id": 1},
        {"id": 2, "name": "B", "ticket_no": "T2", "paper_id": 1},
        {"id": 3, "name": "C", "ticket_no": "T3", "paper_id": 2},
    ]
    assigns, _ = place_candidates(3, 3, 1, cands)
    viols = find_violations(3, 3, 1, assigns)
    assert not any(v.kind == "same_paper_adjacent" for v in viols)

def test_distance_violation_is_its_own_kind():
    # 不同卷、四邻距离 1 < 2：只记曼哈顿间距违规，不得捎带同卷四邻
    assigns = [
        SeatAssign(1, "A", "T1", 1, 0, 0),
        SeatAssign(2, "B", "T2", 2, 0, 1),
    ]
    viols = find_violations(2, 2, 2, assigns)
    assert [v.kind for v in viols] == ["distance"]
    assert "曼哈顿" in viols[0].detail

def test_same_paper_violation_is_its_own_kind():
    # 同卷四邻但间距达标（min_dist=1）：只记同卷四邻违规，不得捎带间距
    assigns = [
        SeatAssign(1, "A", "T1", 1, 0, 0),
        SeatAssign(2, "B", "T2", 1, 0, 1),
    ]
    viols = find_violations(2, 2, 1, assigns)
    assert [v.kind for v in viols] == ["same_paper_adjacent"]
    assert "四邻" in viols[0].detail

def test_diagonal_same_paper_is_not_four_neighbor():
    # 对角同套：间距 2 达标，且对角不是四邻，一条违规都不能有
    assigns = [
        SeatAssign(1, "A", "T1", 1, 0, 0),
        SeatAssign(2, "B", "T2", 1, 1, 1),
    ]
    assert find_violations(2, 2, 2, assigns) == []

def test_two_kinds_stay_two_entries():
    # 同卷且四邻且间距不足：两类违规各自成条，不得并成一句
    assigns = [
        SeatAssign(1, "A", "T1", 1, 0, 0),
        SeatAssign(2, "B", "T2", 1, 0, 1),
    ]
    viols = find_violations(2, 2, 2, assigns)
    assert sorted(v.kind for v in viols) == ["distance", "same_paper_adjacent"]
