"""领域错误：带稳定的机器可读 code，路由层统一转成 HTTP 响应。"""
from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def duplicate_number(number: str, at_index: int | None = None) -> DomainError:
    details = {"number": number}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("duplicate_number", f"规程编号 {number} 已存在，禁止重复发布",
                       http_status=409, details=details)


def invalid_interval(start: str, end: str, reason: str,
                     at_index: int | None = None) -> DomainError:
    details = {"start": start, "end": end, "reason": reason}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("invalid_interval", reason, http_status=422, details=details)


def procedure_not_found(number: str, at_index: int | None = None) -> DomainError:
    details = {"number": number}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("not_found", f"规程编号 {number} 不存在，无法更正",
                       http_status=404, details=details)


def base_version_not_found(number: str, revision: int,
                           at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("base_version_not_found",
                       f"规程 {number} 的版本 r{revision} 不存在",
                       http_status=404, details=details)


def expired_base(number: str, revision: int, remaining: list[tuple[str, str]],
                 at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision,
               "remaining_intervals": [{"start": s, "end": e} for s, e in remaining]}
    if at_index is not None:
        details["index"] = at_index
    if remaining:
        msg = (f"规程 {number} r{revision} 在请求子区间处已被既有更正覆盖，"
               f"请改以最新正文版本为被替代版本（过期基版）")
    else:
        msg = (f"规程 {number} r{revision} 的生效区间已被既有更正全部覆盖，"
               f"不能再以其为被替代版本（过期基版）")
    return DomainError("expired_base_version", msg, http_status=409, details=details)


def concurrent_conflict(number: str, revision: int,
                        conflict_with: tuple[str, str],
                        at_index: int | None = None) -> DomainError:
    cs, ce = conflict_with
    details = {"number": number, "revision": revision,
               "conflict_interval": {"start": cs, "end": ce}}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "concurrent_correction_conflict",
        f"对规程 {number} r{revision} 的并发更正区间与既有更正 [{cs}, {ce}) 重叠，"
        f"同一版本的更正子区间不得相互冲突",
        http_status=409, details=details)
