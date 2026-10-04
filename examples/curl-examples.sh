#!/bin/sh
# 调用示例（curl）。前置：服务已在本机启动。
# 约定时间均为 UTC、ISO-8601；区间语义为 [起, 止)。
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"

# --- 1. 首次发布 ---------------------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 2. 更正（只覆盖 3 月子区间；published_at 由服务端记录，不可回填） ---
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。"
      }'; echo

# --- 3. 回溯查询：作业时刻在 2 月（更正区间外） -> 初版 -----------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-02-15T08:00:00Z"; echo

# --- 4. 回溯查询：作业时刻在 3 月（更正区间内） -> 更正正文 -------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z"; echo

# --- 5. 知悉时刻早于任何发布 -> 空结果 ----------------------------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z&known_as_of=2025-06-01T00:00:00Z"; echo

# --- 6a. 顺序请求却与"已发布"更正重叠 -> 409 过期基版，历史不变 ----------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-20T00:00:00Z",
        "valid_end":   "2026-03-25T00:00:00Z",
        "body": "依据了旧版 r0 的已失效部分，应被拒绝"
      }'; echo

# --- 6b. 同一批次内并发更正相互冲突 -> 409，整批回滚 --------------------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z", "valid_end": "2026-05-20T00:00:00Z",
         "body": "并发更正 A"},
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-10T00:00:00Z", "valid_end": "2026-05-30T00:00:00Z",
         "body": "并发更正 B（与 A 冲突，整批拒绝）"}
      ]}'; echo

# --- 6c. 回填发布时间：published_at 为额外字段，422 拒绝 -----------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "body": "重复编号且回填发布时间",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "published_at": "2000-01-01T00:00:00Z"
      }'; echo

# --- 7. 批量更正：整批原子提交（任一条非法则全部不入库） -----------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z", "valid_end": "2026-06-01T00:00:00Z",
         "body": "5 月更正"},
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-06-01T00:00:00Z", "valid_end": "2026-07-01T00:00:00Z",
         "body": "6 月更正（与上一条首尾相接，允许）"}
      ]}'; echo

# --- 8. 版本历史（审计视图） --------------------------------------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CURL-001/history"; echo

# --- 9a. 两知悉时刻对比：前侧取发布之前，作业区间覆盖全年 ---------------
#       只有更正子区间（3 月）两侧版本不同；before 为 null（当时无任何版本）
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 9b. 收窄作业区间到 2-5 月：null→r0 与 null→r1 两段 -----------------
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-02-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-05-01T00:00:00Z'; echo

# --- 9c. 知悉顺序颠倒 -> 422 invalid_known_order（不写历史） -------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2030-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2025-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 9d. 空作业区间（起=止） -> 422 invalid_interval ---------------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-03-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-03-01T00:00:00Z'; echo
