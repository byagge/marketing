"""Отчёты по фактам: что реально отправлено и кто пишет аккаунтам."""

from __future__ import annotations

from app.reporting.dashboard import build_dashboard, format_dashboard_html
from app.reporting.factual import FactReport, build_fact_report

__all__ = ["FactReport", "build_dashboard", "build_fact_report", "format_dashboard_html"]
