#!/usr/bin/env python3
"""调用示例：仅依赖标准库，直接运行即可走一遍完整业务场景。

前置：服务已在本机启动（docker compose up --build 或 uvicorn app.main:app）。
用法：python examples/python_client.py [BASE_URL]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    number = "SOP-DEMO-001"

    # 1) 首次发布：全年适用的初版正文
    status, res = call("POST", "/api/v1/releases", {
        "number": number,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    show("1. 首次发布", status, res)

    # 2) 更正：仅覆盖 3 月这一子区间；发布时间由服务端记录
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。",
    })
    show("2. 提交更正（子区间）", status, res)
    published_r1 = res["items"][0]["published_at"]

    # 3) 查询：2 月（更正区间之外）-> 仍是初版
    status, res = call("GET", "/api/v1/effective?work_time=2026-02-15T08:00:00Z")
    show("3. 作业时刻 2026-02-15（更正区间外）", status, res)

    # 4) 查询：3 月（更正区间之内）-> 更正正文
    status, res = call("GET", "/api/v1/effective?work_time=2026-03-15T08:00:00Z")
    show("4. 作业时刻 2026-03-15（更正区间内）", status, res)

    # 5) 回溯：知悉时刻取 2025 年（任何版本都尚未发布）-> 空结果
    status, res = call("GET",
                       "/api/v1/effective?work_time=2026-03-15T08:00:00Z"
                       "&known_as_of=2025-06-01T00:00:00Z")
    show("5. 知悉时刻早于发布 -> 空", status, res)

    # 6a) 顺序请求，却与"已发布"更正区间重叠 -> 409 过期基版，历史不变
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-20T00:00:00Z",
        "valid_end": "2026-03-25T00:00:00Z",
        "body": "依据了旧版 r0 的已失效部分，必须被拒绝。",
    })
    show("6a. 过期基版（顺序重叠已发布更正，整体拒绝）", status, res)

    # 6b) 同一批次内两条对 r0 的并发更正相互重叠 -> 409，整批回滚
    status, res = call("POST", "/api/v1/corrections", {"items": [
        {"number": number, "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z",
         "valid_end": "2026-05-20T00:00:00Z", "body": "并发更正 A"},
        {"number": number, "base_revision": 0,
         "valid_start": "2026-05-10T00:00:00Z",
         "valid_end": "2026-05-30T00:00:00Z", "body": "并发更正 B（与 A 冲突）"},
    ]})
    show("6b. 并发更正冲突（同批重叠，整批回滚）", status, res)

    # 6c) 试图回填发布时间：额外字段被拒绝（published_at 只由服务端记录）
    status, res = call("POST", "/api/v1/releases", {
        "number": number,
        "body": "重复编号且回填发布时间",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "published_at": "2000-01-01T00:00:00Z",
    })
    show("6c. 回填发布时间/重复编号被拒绝", status, res)

    # 7) 以 r1 为基版做"更正之更正"（链式）
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 1,
        "valid_start": "2026-03-10T00:00:00Z",
        "valid_end": "2026-03-20T00:00:00Z",
        "body": "再更正：3 月 10-20 日改为全套防割装备。",
    })
    show("7. 链式更正 r1 -> r2", status, res)

    status, res = call("GET",
                       "/api/v1/procedures/" + number + "/history")
    show("8. 版本历史", status, res)

    # 9) 两知悉时刻作业区间对比（见文件下方 _diff_examples）
    _diff_examples(number, published_r1)


def _diff_examples(number: str, published_r1: str) -> None:
    """对比接口示例：known_before / known_after 卡在 r1 发布时刻前后一微秒。"""
    import urllib.parse
    from datetime import datetime, timedelta, timezone

    def us_iso(canonical: str, delta_microseconds: int) -> str:
        dt = datetime.strptime(canonical, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        return (dt + timedelta(microseconds=delta_microseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    # 9a) 全年作业区间：只有 3 月这一段在两侧分别是 r0 / r1；
    #     两侧同为 r0 的相邻部分不返回
    q = urllib.parse.urlencode({
        "known_before": us_iso(published_r1, -1),
        "known_after": us_iso(published_r1, 1),
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9a. 两知悉时刻对比（全年区间，仅 3 月段 r0→r1）", status, res)

    # 9b) 前侧取很早以前（一切版本均未发布）：before=null 逐段列出。
    #     作业区间只问 2-5 月，其中 r1 还被第 7 步的 r2 切成 r1/r2/r1 三段
    q = urllib.parse.urlencode({
        "known_before": "2025-01-01T00:00:00Z",
        "known_after": "2030-01-01T00:00:00Z",
        "work_start": "2026-02-01T00:00:00Z",
        "work_end": "2026-05-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9b. 前侧无任何可见版本（before 为 null 也逐段列出）", status, res)

    # 9c) 知悉顺序颠倒 -> 422 invalid_known_order，不写历史
    q = urllib.parse.urlencode({
        "known_before": "2030-01-01T00:00:00Z",
        "known_after": "2025-01-01T00:00:00Z",
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9c. 知悉顺序颠倒被拒绝", status, res)


if __name__ == "__main__":
    main()
