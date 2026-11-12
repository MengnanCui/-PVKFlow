"""「当前值」两种写法逐行相同（schema.sql 的 key_result_current / key_result_current_all，C078）。

一个是逐行找最大 id（按样品取、按样品关联时快），一个是先分组再连回来（整表汇总时快）。
口径只能有一个：最后写入、且没被标作废的那一条；作废了最新的就退回上一条。
"""
from __future__ import annotations

import random

from app.storage import db


def _seed():
    rnd = random.Random(7)
    with db.tx() as c:
        for sid in ("smp_a", "smp_b", "smp_c"):
            c.execute("INSERT INTO sample(sample_id, name, created_at) VALUES(?,?,?)", (sid, sid, db.now()))
        for _ in range(400):
            sid = rnd.choice(["smp_a", "smp_b", "smp_c", None])    # 没有样品的结果也要算对（IS 比较）
            c.execute("INSERT INTO key_result(sample_id, field_name, value_num, quality, source, created_at)"
                      " VALUES(?,?,?,?,'skill',?)",
                      (sid, rnd.choice(["ot", "pce", "voc"]), rnd.random(),
                       rnd.choice(["validated", "validated", "review", "reject"]), db.now()))


def test_the_two_current_views_agree_row_for_row(workspace):
    _seed()
    a = db.query("SELECT id FROM key_result_current ORDER BY id")
    b = db.query("SELECT id FROM key_result_current_all ORDER BY id")
    assert a == b and a
    # 每个 (样品, 字段) 最多一条、而且不是作废的
    rows = db.query("SELECT sample_id, field_name, quality FROM key_result_current_all")
    assert len({(r["sample_id"], r["field_name"]) for r in rows}) == len(rows)
    assert all(r["quality"] != "reject" for r in rows)


def test_per_sample_count_shortcut_matches_the_view(workspace):
    _seed()
    for sid in ("smp_a", "smp_b", "smp_c"):
        fast = db.scalar("SELECT COUNT(DISTINCT field_name) FROM key_result"
                         " WHERE sample_id = ? AND quality != 'reject'", (sid,))
        assert fast == db.scalar("SELECT COUNT(*) FROM key_result_current WHERE sample_id = ?", (sid,))
