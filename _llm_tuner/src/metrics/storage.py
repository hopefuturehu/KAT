"""Metrics storage backends."""

import json
import sqlite3
from pathlib import Path
from datetime import datetime
from src.metrics.models import MetricsBundle, DataPoint
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SQLiteMetricsStorage:
    """Store metrics in SQLite for simple deployment."""

    def __init__(self, db_path: str = "data/metrics.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    benchmark_name TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    unit TEXT,
                    tags_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    cpu_percent REAL,
                    memory_percent REAL,
                    disk_iops_read REAL,
                    disk_iops_write REAL
                )
            """)
            conn.commit()

    def save_bundle(self, bundle: MetricsBundle) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            ts = bundle.timestamp.isoformat()
            # Save benchmark metrics
            for op in bundle.benchmark_metrics.operations:
                conn.execute(
                    "INSERT INTO metrics (trial_id, benchmark_name, timestamp, metric_name, metric_value, unit, tags_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        bundle.trial_id,
                        bundle.benchmark_name,
                        ts,
                        op.get("name", "unknown"),
                        float(op.get("value", 0)),
                        op.get("unit", ""),
                        json.dumps(op.get("tags", {})),
                    ),
                )
            # Save aggregate metrics
            agg = bundle.benchmark_metrics.aggregate
            for name, value in agg.items():
                conn.execute(
                    "INSERT INTO metrics (trial_id, benchmark_name, timestamp, metric_name, metric_value, unit) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (bundle.trial_id, bundle.benchmark_name, ts, f"aggregate.{name}", float(value), ""),
                )
            # Save system metrics
            if bundle.system_metrics:
                sm = bundle.system_metrics
                conn.execute(
                    "INSERT INTO system_metrics (trial_id, timestamp, cpu_percent, memory_percent, disk_iops_read, disk_iops_write) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (bundle.trial_id, ts, sm.cpu_percent, sm.memory_percent, sm.disk_iops_read, sm.disk_iops_write),
                )
            conn.commit()
        logger.debug("metrics saved", trial_id=bundle.trial_id, benchmark=bundle.benchmark_name)

    def query_trial(self, trial_id: str) -> list[dict]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM metrics WHERE trial_id = ? ORDER BY timestamp",
                (trial_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_best_metric(self, experiment_id: str, metric_name: str, maximize: bool = True) -> dict | None:
        """Get the best (max/min) value for a metric across all trials of an experiment."""
        # Join via trial's experiment_id
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            agg_fn = "MAX" if maximize else "MIN"
            row = conn.execute(
                f"""
                SELECT m.metric_value, m.trial_id, m.timestamp
                FROM metrics m
                JOIN trials t ON m.trial_id = t.id
                WHERE t.experiment_id = ? AND m.metric_name = ?
                ORDER BY m.metric_value {'DESC' if maximize else 'ASC'}
                LIMIT 1
                """,
                (experiment_id, metric_name),
            ).fetchone()
            return dict(row) if row else None
