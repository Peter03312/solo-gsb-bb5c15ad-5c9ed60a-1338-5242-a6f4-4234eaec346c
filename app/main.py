"""FastAPI 入口：HTTP API 与统一错误处理。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import service
from .config import settings
from .db import init_db
from .errors import DomainError
from .schemas import CorrectionBatch, CorrectionItem, ReleaseBatch, ReleaseItem

app = FastAPI(
    title="安全作业规程历史回溯服务",
    version="1.0.0",
    description=("双时间维（适用时间 / 发布时间）规程版本库：首次发布、更正、"
                 "按作业时刻+知悉时刻回溯查询。"),
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.exception_handler(DomainError)
async def _domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status,
                        content={"error": {"code": exc.code,
                                           "message": exc.message,
                                           "details": exc.details}})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request,
                              exc: RequestValidationError) -> JSONResponse:
    # 包含额外字段（如试图回填的 published_at）、类型错误等
    return JSONResponse(status_code=422,
                        content={"error": {"code": "validation_error",
                                           "message": "请求参数校验失败",
                                           "details": {"errors": exc.errors()}}})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {"service": "sop-history",
            "docs": "/docs",
            "health": "/health",
            "endpoints": [
                "POST /api/v1/releases",
                "POST /api/v1/corrections",
                "GET  /api/v1/effective",
                "GET  /api/v1/procedures/{number}/history",
            ]}


def _coerce_items(payload: dict, single_cls, batch_cls):
    """允许单条对象直接提交，或 {"items": [...]} 批量提交。"""
    try:
        if isinstance(payload, dict) and "items" in payload:
            return batch_cls(**payload).items
        return [single_cls(**payload)]
    except ValidationError as exc:
        # 手动校验与 FastAPI 入参校验走同一套错误响应
        raise RequestValidationError(exc.errors())


@app.post("/api/v1/releases", status_code=201)
async def create_release(request: Request) -> dict:
    """首次发布规程：编号、正文、UTC 适用区间 [起, 止)。"""
    payload = await request.json()
    items = _coerce_items(payload, ReleaseItem, ReleaseBatch)
    return service.publish_releases(items)


@app.post("/api/v1/corrections", status_code=201)
async def create_correction(request: Request) -> dict:
    """提交更正：编号、被替代版本、该版本当前生效区间内的子区间、新正文。

    服务端记录发布时间；批量提交时任一条目非法则整批拒绝。
    """
    payload = await request.json()
    items = _coerce_items(payload, CorrectionItem, CorrectionBatch)
    return service.submit_corrections(items)


@app.get("/api/v1/effective")
def effective(work_time: str, known_as_of: str | None = None) -> dict:
    """回溯查询：截至 known_as_of 已发布、且在 work_time 有效的正文与版本。"""
    return service.query_effective(work_time, known_as_of)


@app.get("/api/v1/procedures/{number}/history")
def procedure_history(number: str) -> dict:
    """审计视图：某规程的全部版本（含尚未对旧知悉时刻可见的版本）。"""
    return service.list_history(number)
