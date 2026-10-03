"""Reporting: агрегаты фактов отправки и проблем."""

from __future__ import annotations

from app.reporting.classify import (
    ProblemSpec,
    classify_error,
    classify_health_status,
    classify_or_unknown,
    severity_rank,
)
from app.reporting.dashboard import build_dashboard, format_dashboard_html
from app.reporting.density import avg_gap_minutes, build_density_report
from app.reporting.stats import estimate_schedule_sent, expected_per_chat_24h

__all__ = [
    "ProblemSpec",
    "avg_gap_minutes",
    "build_dashboard",
    "build_density_report",
    "classify_error",
    "classify_health_status",
    "classify_or_unknown",
    "estimate_schedule_sent",
    "expected_per_chat_24h",
    "format_dashboard_html",
    "severity_rank",
]
