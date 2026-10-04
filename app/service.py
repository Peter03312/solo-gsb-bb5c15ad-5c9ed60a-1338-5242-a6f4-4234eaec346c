"""领域服务：首次发布、更正提交、双时间维回溯查询。

双时间维语义
------------
一条 version 行同时携带两个互不相同的时间维：

* 适用时间（valid time）：``[valid_start, valid_end)``，正文在作业现场的生效区间；
* 发布时间（transaction time）：``published_at``，服务端落库时刻，不可回填。

更正只新增行（子版本），不修改任何旧行，因此"更正发布前的查询结果"天然保持原样：
查询时以知悉时刻 ``known_as_of`` 过滤 ``published_at <= known_as_of`` 即可。

版本树
------
- 首版：``revision=0``、``supersedes_id IS NULL``；
- 更正：指向被替代版本 ``supersedes_id``，编号为该规程下递增的 revision；
- 父版本的"仍生效区间" = 自身区间 − 其全部子更正区间（同父更正区间两两不重叠）；
- 查询时在所有可见候选中沿 supersedes 链取"最深"版本，即为作业时刻有效正文。
"""
from __future__ import annotations

import sqlite3

from . import errors
from .db import read_conn, write_tx
from .errors import DomainError
from .time_utils import TimeParseError, now_canonical, parse_canonical

# ---------------------------------------------------------------- 服务端时钟

# 写操作由全局锁串行化；时钟同样保证单调递增，避免连续两次发布落在同一微秒
# 而破坏 published_at 的先后可分辨性（客户端禁止回填，只允许使用该时钟）。
_last_published_at = ""


def _server_now() -> str:
    global _last_published_at
    ts = now_canonical()
    if ts <= _last_published_at:
        # 极端情况下同一微秒内连续发布：向前推进 1 微秒
        from datetime import datetime, timedelta
        dt = datetime.strptime(_last_published_at,
                               "%Y-%m-%dT%H:%M:%S.%fZ") + timedelta(microseconds=1)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _last_published_at = ts
    return ts

# ---------------------------------------------------------------- 区间工具


def _subtract_intervals(base: tuple[str, str],
                        holes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``[base)`` 减去 holes 的并集，返回若干不相交区间（按起点排序）。"""
    pieces = [base]
    for hs, he in sorted(holes):
        next_pieces: list[tuple[str, str]] = []
        for ps, pe in pieces:
            if he <= ps or hs >= pe:
                next_pieces.append((ps, pe))          # 不相交
            else:
                if hs > ps:
                    next_pieces.append((ps, hs))      # 左侧残余
                if he < pe:
                    next_pieces.append((he, pe))      # 右侧残余
        pieces = next_pieces
    return pieces


def _covered_by(interval: tuple[str, str],
                pieces: list[tuple[str, str]]) -> bool:
    s, e = interval
    for ps, pe in pieces:
        if ps <= s and e <= pe:
            return True
    return False


def _first_overlap(interval: tuple[str, str],
                   others: list[tuple[str, str]]) -> tuple[str, str] | None:
    s, e = interval
    for os_, oe in others:
        if s < oe and os_ < e:
            return os_, oe
    return None


def _validated_interval(start_raw: str, end_raw: str,
                        index: int | None) -> tuple[str, str]:
    """规范化区间并校验 [start, end) 非空；时间不合法抛 invalid_interval。"""
    try:
        start = parse_canonical(start_raw, "valid_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "valid_start", **({"index": index} if index is not None else {})})
    try:
        end = parse_canonical(end_raw, "valid_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "valid_end", **({"index": index} if index is not None else {})})
    if not start < end:
        raise errors.invalid_interval(
            start, end, "适用区间非法：起点必须严格早于终点（区间为 [起, 止) 且长度大于 0）",
            index)
    return start, end


def _clean_text(value: str, field: str, index: int | None) -> str:
    text = value.strip()
    if not text:
        details = {"field": field}
        if index is not None:
            details["index"] = index
        raise DomainError("invalid_body", f"{field} 不能为空字符串", 422, details)
    return text


def _clean_number(value: str, index: int | None) -> str:
    number = value.strip()
    if not number:
        details: dict = {"field": "number"}
        if index is not None:
            details["index"] = index
        raise DomainError("invalid_number", "规程编号不能为空字符串", 422, details)
    return number

# ---------------------------------------------------------------- 行读取


def _fetch_procedure(conn: sqlite3.Connection,
                     number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM procedures WHERE number = ?", (number,)).fetchone()


def _fetch_version(conn: sqlite3.Connection, proc_id: int,
                   revision: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE procedure_id = ? AND revision = ?",
        (proc_id, revision)).fetchone()


def _children(conn: sqlite3.Connection,
              version_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE supersedes_id = ? ORDER BY valid_start",
        (version_id,)).fetchall()


def _remaining(conn: sqlite3.Connection,
               base: sqlite3.Row) -> list[tuple[str, str]]:
    """基版在其当前生效区间内尚未被子更正覆盖的子区间集合。"""
    holes = [(c["valid_start"], c["valid_end"])
             for c in _children(conn, base["id"])]
    return _subtract_intervals((base["valid_start"], base["valid_end"]), holes)

# ---------------------------------------------------------------- 首次发布


def publish_releases(items: list) -> dict:
    """首次发布一个或多个规程；整批原子提交。"""
    normalized: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        number = _clean_number(item.number, i)
        body = _clean_text(item.body, "body", i)
        start, end = _validated_interval(item.valid_start, item.valid_end, i)
        if number in seen:
            raise errors.duplicate_number(number, i)
        seen.add(number)
        normalized.append((number, body, start, end))

    with write_tx() as conn:
        published_at = _server_now()
        created: list[dict] = []
        for i, (number, body, start, end) in enumerate(normalized):
            if _fetch_procedure(conn, number) is not None:
                raise errors.duplicate_number(number, i)
        for number, body, start, end in normalized:
            cur = conn.execute(
                "INSERT INTO procedures (number, created_at) VALUES (?, ?)",
                (number, published_at))
            proc_id = cur.lastrowid
            conn.execute(
                "INSERT INTO versions (procedure_id, revision, body, "
                "valid_start, valid_end, supersedes_id, published_at) "
                "VALUES (?, 0, ?, ?, ?, NULL, ?)",
                (proc_id, body, start, end, published_at))
            created.append({"number": number, "revision": 0, "body": body,
                            "valid_start": start, "valid_end": end,
                            "published_at": published_at})
    return {"published_at": published_at, "items": created}

# ---------------------------------------------------------------- 更正提交


def submit_corrections(items: list) -> dict:
    """提交一条或多条更正；任一非法则整批回滚。

    同批次内条目按顺序处理，因此第 k 条可以以同批次更早插入的新版本为基版
    （此时 revision 号为可预知的该规程当前最大号 +1）。
    """
    with write_tx() as conn:
        published_at = _server_now()
        # (proc_id, base_revision) -> 本批次内已占用的子区间
        pending: dict[tuple[int, int], list[tuple[str, str]]] = {}
        created: list[dict] = []

        for i, item in enumerate(items):
            number = _clean_number(item.number, i)
            body = _clean_text(item.body, "body", i)
            sub = _validated_interval(item.valid_start, item.valid_end, i)

            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.procedure_not_found(number, i)
            proc_id = proc["id"]
            base = _fetch_version(conn, proc_id, item.base_revision)
            if base is None:
                raise errors.base_version_not_found(number, item.base_revision, i)

            # 1) 子区间必须整体落在基版区间内（非法区间优先于冲突判定）。
            base_iv = (base["valid_start"], base["valid_end"])
            if not (base_iv[0] <= sub[0] and sub[1] <= base_iv[1]):
                raise errors.invalid_interval(
                    sub[0], sub[1],
                    f"非法区间：更正子区间 [{sub[0]}, {sub[1]}) 超出被替代版本 "
                    f"r{item.base_revision} 的适用区间 [{base_iv[0]}, {base_iv[1]})",
                    i)

            # 2) 同一被替代版本的并发更正不得在生效区间上相互冲突。
            #    先于"剩余区间"检查：同批次更早条目虽已插入当前事务、
            #    尚非已发布历史，对它们的重叠属于并发更正，而非过期基版。
            key = (proc_id, item.base_revision)
            clash = _first_overlap(sub, pending.get(key, []))
            if clash is not None:
                raise errors.concurrent_conflict(number, item.base_revision,
                                                 clash, i)

            # 3) 扣除已发布子更正后的剩余生效区间；子区间不落在其中 -> 过期基版。
            #    （步骤 1 已保证子区间在基版区间内，故未被剩余区间覆盖的部分
            #    必然与某个已发布子更正重叠，即客户依据的是已被替代的旧版。）
            remaining = _remaining(conn, base)
            if not _covered_by(sub, remaining):
                raise errors.expired_base(number, item.base_revision, remaining, i)

            row = conn.execute(
                "SELECT COALESCE(MAX(revision), -1) + 1 AS next_revision "
                "FROM versions WHERE procedure_id = ?", (proc_id,)).fetchone()
            new_revision = row["next_revision"]

            conn.execute(
                "INSERT INTO versions (procedure_id, revision, body, "
                "valid_start, valid_end, supersedes_id, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (proc_id, new_revision, body, sub[0], sub[1],
                 base["id"], published_at))
            pending.setdefault(key, []).append(sub)
            created.append({"number": number, "revision": new_revision,
                            "supersedes_revision": item.base_revision,
                            "body": body, "valid_start": sub[0],
                            "valid_end": sub[1], "published_at": published_at})

    return {"published_at": published_at, "items": created}

# ---------------------------------------------------------------- 回溯查询


def _serialize(row: sqlite3.Row,
               parent_rev: dict[int, int]) -> dict:
    pid = row["supersedes_id"]
    return {
        "number": row["number"],
        "revision": row["revision"],
        "body": row["body"],
        "valid_start": row["valid_start"],
        "valid_end": row["valid_end"],
        "supersedes_revision": parent_rev.get(pid) if pid is not None else None,
        "published_at": row["published_at"],
    }


def query_effective(work_time_raw: str, known_as_of_raw: str | None) -> dict:
    """返回截至知悉时刻已发布、且在作业时刻有效的各规程正文与版本。"""
    try:
        work_time = parse_canonical(work_time_raw, "work_time")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422, {"field": "work_time"})
    if known_as_of_raw is None or not str(known_as_of_raw).strip():
        known_as_of = now_canonical()
    else:
        try:
            known_as_of = parse_canonical(known_as_of_raw, "known_as_of")
        except TimeParseError as exc:
            raise DomainError("invalid_time", str(exc), 422,
                              {"field": "known_as_of"})

    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT v.*, p.number AS number
              FROM versions v
              JOIN procedures p ON p.id = v.procedure_id
             WHERE v.published_at <= ?
               AND v.valid_start <= ?
               AND ? < v.valid_end
             ORDER BY p.number
            """,
            (known_as_of, work_time, work_time)).fetchall()
        if not rows:
            return {"work_time": work_time, "known_as_of": known_as_of,
                    "count": 0, "items": []}

        # 取候选涉及的全部版本，构建父链并计算深度。
        proc_ids = {r["procedure_id"] for r in rows}
        all_versions: list[sqlite3.Row] = []
        for proc_id in proc_ids:
            all_versions.extend(conn.execute(
                "SELECT * FROM versions WHERE procedure_id = ?",
                (proc_id,)).fetchall())

        by_id = {v["id"]: v for v in all_versions}
        parent_rev = {v["id"]: v["revision"] for v in all_versions}
        depth_cache: dict[int, int] = {}

        def depth(v: sqlite3.Row) -> int:
            chain: list[int] = []
            cur: sqlite3.Row | None = v
            while cur is not None and cur["id"] not in depth_cache:
                chain.append(cur["id"])
                cur = by_id.get(cur["supersedes_id"]) if cur["supersedes_id"] is not None else None
            base = depth_cache[cur["id"]] if cur is not None else 0
            for vid in reversed(chain):
                base += 1
                depth_cache[vid] = base
            return depth_cache[v["id"]]

        best: dict[int, sqlite3.Row] = {}
        best_depth: dict[int, int] = {}
        for r in rows:
            pid = r["procedure_id"]
            d = depth(r)
            cur = best.get(pid)
            if (cur is None or d > best_depth[pid]
                    # 同深度理论上不会出现（覆盖互不重叠）；以下仅为确定性兜底
                    or (d == best_depth[pid]
                        and (r["revision"], r["published_at"])
                        > (cur["revision"], cur["published_at"]))):
                best[pid] = r
                best_depth[pid] = d

        items = sorted((_serialize(r, parent_rev) for r in best.values()),
                       key=lambda x: x["number"])

    return {"work_time": work_time, "known_as_of": known_as_of,
            "count": len(items), "items": items}

# ---------------------------------------------------------------- 历史查看


def list_history(number_raw: str) -> dict:
    """返回某规程全部版本（管理用审计视图，不做知悉时刻裁剪）。"""
    number = number_raw.strip()
    with read_conn() as conn:
        proc = _fetch_procedure(conn, number) if number else None
        if proc is None:
            raise DomainError("not_found", f"规程编号 {number or '(空)'} 不存在",
                              404, {"number": number})
        rows = conn.execute(
            """
            SELECT v.*, p.number AS number
              FROM versions v
              JOIN procedures p ON p.id = v.procedure_id
             WHERE v.procedure_id = ?
             ORDER BY v.revision
            """, (proc["id"],)).fetchall()
        parent_rev: dict[int, int] = {}
        if rows:
            ids = [r["supersedes_id"] for r in rows if r["supersedes_id"] is not None]
            if ids:
                placeholders = ",".join("?" * len(ids))
                for prow in conn.execute(
                        f"SELECT id, revision FROM versions WHERE id IN ({placeholders})",
                        ids):
                    parent_rev[prow["id"]] = prow["revision"]
        items = [_serialize(r, parent_rev) for r in rows]
    return {"number": number, "count": len(items), "items": items}
