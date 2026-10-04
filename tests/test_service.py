"""端到端测试：覆盖双时间维查询、更正裁剪与四类整体拒绝。

运行：python -m unittest discover -s tests -v
（需先安装 requirements-dev.txt，仅 API 测试需要 fastapi/httpx）
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# 每个测试用独立临时库，必须在导入 app.config 之前设置
_tmpdir = tempfile.mkdtemp(prefix="sop-test-")
os.environ["SOP_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["SOP_PORT"] = "8099"

from app import service  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.errors import DomainError  # noqa: E402
from app.time_utils import parse_canonical  # noqa: E402

init_db()


def canon(value: str) -> str:
    """测试辅助：把无小数秒的时间转成与存储一致的规范形式。"""
    return parse_canonical(value)


def reset_db() -> None:
    """每个用例重建空库，保证用例彼此独立、可重复运行。"""
    import sqlite3
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            "DELETE FROM versions; DELETE FROM procedures;")
        conn.commit()
    finally:
        conn.close()

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_us(dt: datetime) -> str:
    """带微秒的 UTC 串（用于服务端发布时刻边界）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


t0 = iso(_T - timedelta(days=1))      # 知悉时刻早于发布
t1 = iso(_T)                          # 区间起点
t2 = iso(_T + timedelta(days=30))
t3 = iso(_T + timedelta(days=60))
t4 = iso(_T + timedelta(days=90))
t5 = iso(_T + timedelta(days=365))    # 区间终点（不含）
future = iso(_T + timedelta(days=400))


def item(number: str, body: str = "v0", start: str = t1, end: str = t5,
         base: int | None = None):
    data = {"number": number, "body": body,
            "valid_start": start, "valid_end": end}
    if base is not None:
        data["base_revision"] = base
    return SimpleNamespace(**data)


def release(number: str, start: str = t1, end: str = t5, body: str = "初版"):
    return service.publish_releases([item(number, body, start, end)])


def correct(number: str, base: int, start: str, end: str, body: str = "更正"):
    return service.submit_corrections([item(number, body, start, end, base)])


def numbers_at(work: str, known: str) -> dict[str, dict]:
    return {x["number"]: x for x in service.query_effective(work, known)["items"]}


def us_before(published: str) -> str:
    """发布时刻前 1 微秒的规范串：该知悉时刻刚好还看不到这次发布。"""
    dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)
    return iso_us(dt - timedelta(microseconds=1))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        release("DUP-1")
        with self.assertRaises(DomainError) as ctx:
            release("DUP-1")
        self.assertEqual(ctx.exception.code, "duplicate_number")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_duplicate_in_same_batch(self):
        with self.assertRaises(DomainError) as ctx:
            service.publish_releases([item("DUP-BATCH"), item("DUP-BATCH")])
        self.assertEqual(ctx.exception.code, "duplicate_number")
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_invalid_intervals(self):
        # 起 >= 止
        with self.assertRaises(DomainError) as ctx:
            release("BAD-IV-1", start=t3, end=t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        # 空区间 [t1, t1)
        with self.assertRaises(DomainError):
            release("BAD-IV-2", start=t1, end=t1)

    def test_naive_time_rejected(self):
        with self.assertRaises(DomainError):
            service.publish_releases([item("BAD-TZ", start="2026-01-01T00:00:00",
                                           end=t5)])


class QueryTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_basic_visibility(self):
        release("Q-1")
        # 作业时刻在区间外 -> 无
        self.assertEqual(numbers_at(t5, future), {})
        self.assertEqual(numbers_at(iso(_T - timedelta(seconds=1)), future), {})
        # 区间内可见
        row = numbers_at(t2, future)["Q-1"]
        self.assertEqual(row["revision"], 0)
        self.assertEqual(row["body"], "初版")
        # 知悉时刻早于发布 -> 空
        self.assertEqual(numbers_at(t2, t0), {})

    def test_known_as_of_defaults_now(self):
        release("Q-2")  # 发布时间 = 现在，作业区间在 2026
        row = numbers_at(t2, None)["Q-2"]
        self.assertEqual(row["revision"], 0)

    def test_offset_timezone_normalized_to_utc(self):
        service.publish_releases([item("Q-TZ", start=t1, end=t5)])
        # +08:00 的 08:00 == UTC 00:00，即区间起点（含），可见
        row = numbers_at("2026-01-01T08:00:00+08:00", future)["Q-TZ"]
        self.assertEqual(row["revision"], 0)
        # 终点（不含）：UTC 2027-01-01 00:00 == +08:00 08:00
        self.assertNotIn("Q-TZ", numbers_at("2027-01-01T08:00:00+08:00", future))


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        release("C-1", body="初版")

    def test_correction_covers_only_subinterval(self):
        correct("C-1", 0, t2, t3, "三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=29)), future)["C-1"]["body"], "初版")
        self.assertEqual(numbers_at(t2, future)["C-1"]["body"], "三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=59)), future)["C-1"]["body"], "三月版")
        self.assertEqual(numbers_at(t3, future)["C-1"]["body"], "初版")  # 止不含

    def test_query_before_correction_published_unchanged(self):
        published = correct("C-1", 0, t2, t3, "三月版")["published_at"]
        # 用更正发布时刻本身（published_at <= known 含等号）刚可见；前 1 微秒不可见
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        # 作业时刻同样规范化到 UTC 微秒串，避免与初版发布时刻误比
        self.assertEqual(numbers_at(canon(t2), before)["C-1"]["body"], "初版")
        self.assertEqual(numbers_at(canon(t2), published)["C-1"]["body"], "三月版")

    def test_concurrent_overlap_rejected(self):
        # 顺序提交且与"已发布"的更正重叠：客户依据的是旧版 r0 的当前生效区间，
        # 属于过期基版（应改以 r1 为被替代版本），历史保持原样。
        correct("C-1", 0, t2, t3, "三月版")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T + timedelta(days=40)),
                    iso(_T + timedelta(days=50)), "冲突")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        # 历史不变：该日期仍是第一条更正正文
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["C-1"]["body"], "三月版")

    def test_overlap_inside_same_batch_is_conflict(self):
        # 同一批次（并发提交）内两条更正相互冲突 -> concurrent_correction_conflict
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections([
                item("C-1", "A", t2,
                     iso(_T + timedelta(days=50)), base=0),
                item("C-1", "B", iso(_T + timedelta(days=40)),
                     iso(_T + timedelta(days=55)), base=0),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)
        # 整批回滚：均不存在
        self.assertEqual(numbers_at(t2, future)["C-1"]["revision"], 0)

    def test_adjacent_intervals_allowed(self):
        correct("C-1", 0, t2, t3, "二月")
        correct("C-1", 0, t3, t4, "三月")  # 首尾相接不算重叠
        self.assertEqual(numbers_at(t3, future)["C-1"]["body"], "三月")

    def test_interval_outside_base_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T - timedelta(days=1)), t2, "越界")
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, t4, iso(_T + timedelta(days=400)), "越界")
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_expired_base_version(self):
        # 基版区间被全部覆盖后再以其为基版 -> expired_base_version
        correct("C-1", 0, t1, t3, "上半段")
        correct("C-1", 0, t3, t5, "下半段")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, t2, t3, "基版已全覆盖")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["remaining_intervals"], [])

    def test_expired_base_when_partial_overlap_existing_child(self):
        correct("C-1", 0, t2, t4, "长更正")
        # 与其部分重叠（依据了旧版 r0 的当前生效区间之外的部分）
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T + timedelta(days=20)),
                    iso(_T + timedelta(days=40)), "重叠")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        remaining = ctx.exception.details["remaining_intervals"]
        self.assertEqual([(r["start"], r["end"]) for r in remaining],
                         [(canon(t1), canon(t2)), (canon(t4), canon(t5))])

    def test_missing_procedure_and_base(self):
        with self.assertRaises(DomainError) as ctx:
            correct("NOPE", 0, t1, t2)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 9, t1, t2)
        self.assertEqual(ctx.exception.code, "base_version_not_found")

    def test_chained_correction_and_depth(self):
        correct("C-1", 0, t2, t4, "r1")
        correct("C-1", 1, t3, t4, "r2")          # r1 内的子区间
        correct("C-1", 1, t2, t3, "r3")          # r1 剩余的另一半
        # t2..t3 -> r3（最深链 v0<-v1<-v3）
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["C-1"]["body"], "r3")
        # t3..t4 -> r2
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["C-1"]["body"], "r2")
        # r1 已被 r2/r3 完整覆盖，再以 r1 为基版 -> 过期
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 1, iso(_T + timedelta(days=40)),
                    iso(_T + timedelta(days=45)), "x")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        # r0 在 t4..t5 仍生效，可继续更正
        correct("C-1", 0, t4, t5, "r4")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=100)),
                                    future)["C-1"]["body"], "r4")

    def test_batch_atomic_rollback(self):
        # 第二条与第一条区间重叠（同为对 r0 的并发更正）-> 第一条也不得入库
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections([
                item("C-1", "五月", iso(_T + timedelta(days=120)),
                     iso(_T + timedelta(days=150)), base=0),
                item("C-1", "冲突", iso(_T + timedelta(days=130)),
                     iso(_T + timedelta(days=140)), base=0),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)
        # 第一条不存在：该时间段回到初版
        self.assertEqual(numbers_at(iso(_T + timedelta(days=130)),
                                    future)["C-1"]["body"], "初版")

    def test_batch_with_chain_inside(self):
        # 同批次：先更正 r0 得到 r1，再以 r1 为基版
        res = service.submit_corrections([
            item("C-1", "r1", t2, t4, base=0),
            item("C-1", "r2", t3, t4, base=1),
        ])
        self.assertEqual([x["revision"] for x in res["items"]], [1, 2])
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["C-1"]["body"], "r2")

    def test_published_at_is_server_defined_and_returned(self):
        res = correct("C-1", 0, t2, t3)
        self.assertTrue(res["published_at"].endswith("Z"))
        history = service.list_history("C-1")
        self.assertEqual(history["count"], 2)
        self.assertEqual([v["revision"] for v in history["items"]], [0, 1])
        self.assertEqual(history["items"][1]["supersedes_revision"], 0)

    def test_multiple_procedures_in_one_query(self):
        release("C-2", body="另一个规程")
        correct("C-1", 0, t2, t3, "C1三月")
        rows = numbers_at(t2, future)
        self.assertEqual(rows["C-1"]["body"], "C1三月")
        self.assertEqual(rows["C-2"]["body"], "另一个规程")


class DiffTests(unittest.TestCase):
    """双知悉时刻对比：service.diff_procedure。"""

    def setUp(self):
        reset_db()

    def test_change_segment_between_two_known_moments(self):
        release("D-1", body="初版")
        pub1 = correct("D-1", 0, t2, t3, "三月版")["published_at"]
        res = service.diff_procedure("D-1", us_before(pub1), pub1, t1, t5)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"], {"revision": 0, "body": "初版"})
        self.assertEqual(seg["after"], {"revision": 1, "body": "三月版"})
        # 回显规范化后的入参
        self.assertEqual(res["number"], "D-1")
        self.assertEqual(res["work_start"], canon(t1))
        self.assertEqual(res["work_end"], canon(t5))

    def test_side_without_applicable_version_is_null(self):
        release("D-2", body="初版")
        # 知悉时刻一早于任何发布 -> before 侧无适用版本（null）；
        # 作业区间超出规程区间之外的部分两侧均无版本 -> 不返回。
        day = iso(_T + timedelta(days=366))
        res = service.diff_procedure("D-2", t0, future, t0, day)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t1), canon(t5)))
        self.assertIsNone(seg["before"])
        self.assertEqual(seg["after"], {"revision": 0, "body": "初版"})

    def test_both_sides_without_version_not_returned(self):
        release("D-3")
        # 两个知悉时刻都早于首次发布（且二者相等，不算顺序颠倒）
        res = service.diff_procedure("D-3", t0, t0, t1, t5)
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["segments"], [])

    def test_same_body_new_revision_still_counts_as_change(self):
        release("D-4", body="同文")
        pub1 = correct("D-4", 0, t2, t3, "同文")["published_at"]
        res = service.diff_procedure("D-4", us_before(pub1), pub1, t2, t3)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual(seg["before"], {"revision": 0, "body": "同文"})
        self.assertEqual(seg["after"], {"revision": 1, "body": "同文"})

    def test_chained_correction_resolves_deepest_on_each_side(self):
        release("D-5", body="r0")
        correct("D-5", 0, t2, t4, "r1")
        pub2 = correct("D-5", 1, t3, t4, "r2")["published_at"]
        # before 侧只见 r0/r1，after 侧可见到链式更正 r2
        res = service.diff_procedure("D-5", us_before(pub2), pub2, t1, t5)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t3), canon(t4)))
        self.assertEqual(seg["before"]["revision"], 1)
        self.assertEqual(seg["after"]["revision"], 2)

    def test_work_interval_left_closed_right_open(self):
        release("D-6", body="初版")
        pub1 = correct("D-6", 0, t2, t3, "三月版")["published_at"]
        before = us_before(pub1)
        # 作业区间从更正终点开始（右开）：不含更正 -> 无变化
        res = service.diff_procedure("D-6", before, pub1, t3, t5)
        self.assertEqual(res["segments"], [])
        # 作业区间起于更正起点（左闭）：片段起点恰为更正起点
        res = service.diff_procedure("D-6", before, pub1, t2,
                                     iso(_T + timedelta(days=31)))
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["segments"][0]["start"], canon(t2))
        self.assertEqual(res["segments"][0]["end"],
                         canon(iso(_T + timedelta(days=31))))

    def test_unaffected_and_out_of_range_parts_dropped(self):
        release("D-7", body="初版")
        correct("D-7", 0, t2, t3, "三月版")
        t4b = iso(_T + timedelta(days=100))
        pub2 = correct("D-7", 0, t4, t4b, "百日版")["published_at"]
        # before 侧已含三月版：三月区间两侧相同不返回；只剩百日版这一段
        res = service.diff_procedure("D-7", us_before(pub2), pub2, t1, t5)
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(t4), canon(t4b))])
        # 作业区间只罩住百日版右半边：片段被裁剪到作业区间内
        mid = iso(_T + timedelta(days=95))
        res = service.diff_procedure("D-7", us_before(pub2), pub2, mid, t5)
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(mid), canon(t4b))])

    def test_adjacent_segments_with_same_change_merge(self):
        # 合并逻辑：相邻且两侧版本均相同的片段并为一段（最大片段）
        r0 = {"revision": 0, "body": "x"}
        r1 = {"revision": 1, "body": "y"}
        a, b, c = canon(t1), canon(t2), canon(t3)
        before_cov = [(a, b, r0), (b, c, r0)]
        after_cov = [(a, c, r1)]
        segs = service._diff_coverages(before_cov, after_cov)
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0]["start"], segs[0]["end"]), (a, c))
        self.assertEqual(segs[0]["before"]["revision"], 0)
        self.assertEqual(segs[0]["after"]["revision"], 1)

    def test_diff_errors(self):
        release("D-ERR")
        # 编号不存在 -> 404 not_found
        with self.assertRaises(DomainError) as ctx:
            service.diff_procedure("NOPE", t0, future, t1, t5)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        # 时间缺时区 -> 422 invalid_time
        with self.assertRaises(DomainError) as ctx:
            service.diff_procedure("D-ERR", "2026-01-01T00:00:00",
                                   future, t1, t5)
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "known_as_of_before")
        # 知悉顺序颠倒 -> 422 invalid_known_order
        with self.assertRaises(DomainError) as ctx:
            service.diff_procedure("D-ERR", future, t0, t1, t5)
        self.assertEqual(ctx.exception.code, "invalid_known_order")
        self.assertEqual(ctx.exception.http_status, 422)
        # 作业区间为空（起 == 止 或 起 > 止）-> 422 invalid_interval
        for ws, we in ((t2, t2), (t3, t2)):
            with self.assertRaises(DomainError) as ctx:
                service.diff_procedure("D-ERR", t0, future, ws, we)
            self.assertEqual(ctx.exception.code, "invalid_interval")
            self.assertEqual(ctx.exception.http_status, 422)

    def test_diff_does_not_write_history(self):
        release("D-H", body="初版")
        pub1 = correct("D-H", 0, t2, t3, "三月版")["published_at"]
        before = service.list_history("D-H")
        service.diff_procedure("D-H", us_before(pub1), pub1, t1, t5)
        service.diff_procedure("D-H", t0, future, t1, t5)
        self.assertEqual(service.list_history("D-H"), before)


class ApiTests(unittest.TestCase):
    """FastAPI 层：仅在依赖可用时运行。"""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            from app.main import app
        except Exception:
            raise unittest.SkipTest("fastapi/httpx 未安装，跳过 API 测试")
        cls.client = TestClient(app)

    def _n(self):
        # 编号全库唯一
        return f"API-{id(self)}-{self._seq}"

    def setUp(self):
        reset_db()
        self._seq = ApiTests._seq = getattr(ApiTests, "_seq", 0) + 1

    def test_full_flow_over_http(self):
        c = self.client
        n = self._n()
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "A", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["items"][0]["revision"], 0)

        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "B"})
        self.assertEqual(r.status_code, 201)

        r = c.get("/api/v1/effective", params={"work_time": t2})
        self.assertEqual(r.status_code, 200)
        found = [i for i in r.json()["items"] if i["number"] == n]
        self.assertEqual(found[0]["body"], "B")

        # 顺序提交且与已发布更正重叠 -> 409 过期基版
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": iso(_T + timedelta(days=40)),
            "valid_end": iso(_T + timedelta(days=50)), "body": "X"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "expired_base_version")

        # 同批冲突 -> 409 并发更正冲突
        r = c.post("/api/v1/corrections", json={"items": [
            {"number": n, "base_revision": 0,
             "valid_start": iso(_T + timedelta(days=120)),
             "valid_end": iso(_T + timedelta(days=140)), "body": "P"},
            {"number": n, "base_revision": 0,
             "valid_start": iso(_T + timedelta(days=130)),
             "valid_end": iso(_T + timedelta(days=150)), "body": "Q"},
        ]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"],
                         "concurrent_correction_conflict")

        # 重复编号 409
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "A2", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 409)

        # 历史
        r = c.get(f"/api/v1/procedures/{n}/history")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        r = c.get("/api/v1/procedures/NO-SUCH/history")
        self.assertEqual(r.status_code, 404)

    def test_extra_field_published_at_forbidden(self):
        r = self.client.post("/api/v1/releases", json={
            "number": self._n(), "body": "A",
            "valid_start": t1, "valid_end": t5,
            "published_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

    def test_health_and_empty_result(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        r = self.client.get("/api/v1/effective",
                            params={"work_time": "2001-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)
        self.assertEqual(r.json()["items"], [])

    def test_diff_over_http(self):
        c = self.client
        n = self._n()
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 201)
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})
        self.assertEqual(r.status_code, 201)
        pub1 = r.json()["published_at"]

        # 更正发布前 1 微秒 vs 发布时刻：仅更正子区间发生变化
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_as_of_before": us_before(pub1),
            "known_as_of_after": pub1,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        seg = body["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"], {"revision": 0, "body": "初版"})
        self.assertEqual(seg["after"], {"revision": 1, "body": "三月版"})

        # 知悉时刻早于任何发布：before 侧为 null
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_as_of_before": t0, "known_as_of_after": pub1,
            "work_start": t2, "work_end": t3})
        self.assertEqual(r.status_code, 200)
        seg = r.json()["segments"][0]
        self.assertIsNone(seg["before"])
        self.assertEqual(seg["after"]["revision"], 1)

        # 对比是只读的：历史不变
        r = c.get(f"/api/v1/procedures/{n}/history")
        self.assertEqual(r.json()["count"], 2)

        # 编号不存在 -> 404 not_found
        r = c.get("/api/v1/procedures/NO-SUCH/diff", params={
            "known_as_of_before": t0, "known_as_of_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")
        # 知悉顺序颠倒 -> 422 invalid_known_order
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_as_of_before": future, "known_as_of_after": t0,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_known_order")
        # 作业区间为空 -> 422 invalid_interval
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_as_of_before": t0, "known_as_of_after": future,
            "work_start": t2, "work_end": t2})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")
        # 时间缺时区 -> 422 invalid_time
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_as_of_before": "2026-01-01T00:00:00",
            "known_as_of_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")


if __name__ == "__main__":
    unittest.main(verbosity=2)
