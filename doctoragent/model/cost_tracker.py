"""LLM 调用成本追踪系统。

支持按模型定价、按租户聚合、成本告警、导出报表。
定价表 ``MODEL_PRICING`` 以「每 1K token 美元价格」为单位，未知模型
回退到 ``"default"`` 条目。当 ``storage_path`` 为 ``None`` 时使用
SQLite 内存库，适合测试与临时统计。
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from doctoragent._utils import open_sqlite
from doctoragent.compat import UTC

logger = logging.getLogger(__name__)

# 常见模型的定价表（每 1K token 的美元价格）。
# 未知模型回退到 ``"default"`` 条目。
MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "o1": {"input": 0.015, "output": 0.06},
    "o1-mini": {"input": 0.003, "output": 0.012},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "default": {"input": 0.001, "output": 0.002},
}


class CostRecord(BaseModel):
    """单次 LLM 调用的成本记录。"""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    tenant_id: str = "default"
    timestamp: str


class CostTracker:
    """LLM 成本追踪器，支持可选 SQLite 持久化。

    ``storage_path`` 为 ``None`` 时使用内存 SQLite（进程退出即丢失），
    适合测试与临时统计；传入文件路径则持久化到磁盘。
    """

    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path: Path | None = Path(storage_path) if storage_path is not None else None
        self._write_lock = threading.Lock()
        if self.storage_path is not None:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            # 文件库：复用共享的 open_sqlite（WAL + busy_timeout + timeout）。
            self._conn = open_sqlite(self.storage_path, row_factory=sqlite3.Row)
        else:
            # 内存库必须保持连接存活；WAL 对 :memory: 是 no-op，跳过。
            self._conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    def _init_db(self) -> None:
        """创建 cost_records 表与索引（幂等）。"""
        with self._write_lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cost_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    timestamp TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_tenant ON cost_records(tenant_id)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_model ON cost_records(model)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_timestamp ON cost_records(timestamp)"
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 计费 & 记录
    # ------------------------------------------------------------------

    def calculate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """根据定价表计算单次调用成本（美元）。

        未知模型回退到 ``MODEL_PRICING["default"]``。
        """
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
        input_cost = (prompt_tokens / 1000.0) * pricing["input"]
        output_cost = (completion_tokens / 1000.0) * pricing["output"]
        return round(input_cost + output_cost, 8)

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tenant_id: str = "default",
    ) -> CostRecord:
        """记录一次调用成本，返回 :class:`CostRecord`。"""
        cost = self.calculate_cost(model, prompt_tokens, completion_tokens)
        now = datetime.now(UTC).isoformat()
        record = CostRecord(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            tenant_id=tenant_id,
            timestamp=now,
        )
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO cost_records
                    (model, prompt_tokens, completion_tokens, cost_usd,
                     tenant_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    model,
                    prompt_tokens,
                    completion_tokens,
                    cost,
                    tenant_id,
                    now,
                ),
            )
            self._conn.commit()
        logger.debug(
            "cost_recorded",
            extra={
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "tenant_id": tenant_id,
            },
        )
        return record

    # ------------------------------------------------------------------
    # 汇总统计
    # ------------------------------------------------------------------

    def get_summary(
        self,
        tenant_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict:
        """汇总统计：总成本、总 token、按模型分组、按天分组。"""
        conditions: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("timestamp <= ?")
            params.append(end_time)
        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._write_lock:
            # 总计
            totals = self._conn.execute(  # nosec B608
                f"""
                SELECT
                    COALESCE(SUM(cost_usd), 0.0) AS total_cost,
                    COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                    COALESCE(SUM(completion_tokens), 0) AS total_completion,
                    COUNT(*) AS call_count
                FROM cost_records{where_clause}
                """,
                params,
            ).fetchone()

            # 按模型分组
            model_rows = self._conn.execute(  # nosec B608
                f"""
                SELECT model,
                       SUM(cost_usd) AS cost,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       COUNT(*) AS count
                FROM cost_records{where_clause}
                GROUP BY model
                """,
                params,
            ).fetchall()

            # 按天分组（取 timestamp 的日期部分）
            day_rows = self._conn.execute(  # nosec B608
                f"""
                SELECT substr(timestamp, 1, 10) AS day,
                       SUM(cost_usd) AS cost,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       COUNT(*) AS count
                FROM cost_records{where_clause}
                GROUP BY day
                ORDER BY day
                """,
                params,
            ).fetchall()

        by_model: dict[str, dict] = {}
        for row in model_rows:
            by_model[row["model"]] = {
                "cost_usd": round(row["cost"] or 0.0, 8),
                "prompt_tokens": row["prompt_tokens"] or 0,
                "completion_tokens": row["completion_tokens"] or 0,
                "call_count": row["count"],
            }

        by_day: dict[str, dict] = {}
        for row in day_rows:
            by_day[row["day"]] = {
                "cost_usd": round(row["cost"] or 0.0, 8),
                "prompt_tokens": row["prompt_tokens"] or 0,
                "completion_tokens": row["completion_tokens"] or 0,
                "call_count": row["count"],
            }

        return {
            "total_cost_usd": round(totals["total_cost"] or 0.0, 8),
            "total_prompt_tokens": totals["total_prompt"] or 0,
            "total_completion_tokens": totals["total_completion"] or 0,
            "total_tokens": (totals["total_prompt"] or 0) + (totals["total_completion"] or 0),
            "call_count": totals["call_count"],
            "by_model": by_model,
            "by_day": by_day,
        }

    def get_daily_costs(self, days: int = 7) -> list[dict]:
        """最近 ``days`` 天每日成本（含零成本日期，按日期升序）。"""
        today = datetime.now(UTC).date()
        start_date = today - timedelta(days=days - 1)
        start_str = start_date.isoformat()

        with self._write_lock:
            rows = self._conn.execute(
                """
                SELECT substr(timestamp, 1, 10) AS day,
                       SUM(cost_usd) AS cost,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       COUNT(*) AS count
                FROM cost_records
                WHERE timestamp >= ?
                GROUP BY day
                ORDER BY day
                """,
                (start_str,),
            ).fetchall()

        # 构建日期 -> 数据映射，补齐无记录的日期为 0。
        data_map: dict[str, dict] = {}
        for row in rows:
            data_map[row["day"]] = {
                "date": row["day"],
                "cost_usd": round(row["cost"] or 0.0, 8),
                "prompt_tokens": row["prompt_tokens"] or 0,
                "completion_tokens": row["completion_tokens"] or 0,
                "call_count": row["count"],
            }

        result: list[dict] = []
        for i in range(days):
            day = (start_date + timedelta(days=i)).isoformat()
            result.append(
                data_map.get(
                    day,
                    {
                        "date": day,
                        "cost_usd": 0.0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "call_count": 0,
                    },
                )
            )
        return result

    def check_budget(self, tenant_id: str, daily_budget_usd: float) -> bool:
        """检查今日是否在预算内。

        返回 ``True`` 表示未超预算（可继续调用），``False`` 表示已超预算。
        """
        today_str = datetime.now(UTC).date().isoformat()
        with self._write_lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) AS cost
                FROM cost_records
                WHERE tenant_id = ? AND substr(timestamp, 1, 10) = ?
                """,
                (tenant_id, today_str),
            ).fetchone()
        spent = row["cost"] if row is not None else 0.0
        within_budget = float(spent) < daily_budget_usd
        if not within_budget:
            logger.warning(
                "budget_exceeded",
                extra={
                    "tenant_id": tenant_id,
                    "spent": float(spent),
                    "budget": daily_budget_usd,
                },
            )
        return within_budget

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export_csv(self, output_path: Path, tenant_id: str | None = None) -> int:
        """导出记录到 CSV 文件，返回写入的行数。"""
        conditions: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._write_lock:
            rows = self._conn.execute(  # nosec B608
                f"""
                SELECT model, prompt_tokens, completion_tokens, cost_usd,
                       tenant_id, timestamp
                FROM cost_records{where_clause}
                ORDER BY timestamp
                """,
                params,
            ).fetchall()

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "model",
            "prompt_tokens",
            "completion_tokens",
            "cost_usd",
            "tenant_id",
            "timestamp",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        logger.info(
            "cost_exported",
            extra={"path": str(output_path), "rows": len(rows)},
        )
        return len(rows)

    # ------------------------------------------------------------------
    # 资源清理
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        with self._write_lock:
            self._conn.close()

    def __enter__(self) -> CostTracker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
