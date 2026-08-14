"""Skill: dynamic-baseline -- EWMA-based dynamic threshold for proactive sensing.

启发 1：主动感知 -- 用 EWMA（指数加权移动平均）+ 标准差阈值
在告警阈值未触发前检测指标异常变化率，避免被动等待告警。

算法：
  EWMA(t) = α * x(t) + (1 - α) * EWMA(t-1)
  σ(t)    = sqrt(α * (x(t) - EWMA(t))^2 + (1 - α) * σ(t-1)^2)
  异常 if |x(t) - EWMA(t-1)| > k * σ(t-1)

参数：
  α = 0.3 （对近期样本更敏感）
  k = 2.5 （2.5σ 触发，对应 ~99% 置信度）
"""
from __future__ import annotations
import math


class DynamicBaselineSkill:
    NAME = "dynamic-baseline"
    PURPOSE = "EWMA-based dynamic threshold for proactive anomaly detection"
    INPUT = "series: list[float] -- recent metric values; alpha: float; k: float"
    OUTPUT = "anomalies: list[dict] -- each {index, value, ewma, sigma, is_anomaly}"
    CALL_CONDITION = (
        "AnomalyDetector Agent 在 AlertAggregator 之前运行，"
        "对每个服务的关键指标做主动扫描"
    )
    DEPENDENCIES = ["Prometheus MCP (query_range / query)"]
    FAILURE = "Degrade to no-op; never raise"
    SECURITY = "Read-only analysis"
    REUSE = "通用时序异常检测，可用于任何需要动态阈值的场景"

    DEFAULT_ALPHA = 0.3
    DEFAULT_K = 2.5
    MIN_SAMPLES = 3

    def execute(
        self,
        series: list[float] | None = None,
        alpha: float | None = None,
        k: float | None = None,
        **kw,
    ) -> dict:
        alpha = alpha if alpha is not None else self.DEFAULT_ALPHA
        k = k if k is not None else self.DEFAULT_K
        xs = series or []
        if len(xs) < self.MIN_SAMPLES:
            return {
                "anomalies": [],
                "summary": {
                    "samples": len(xs),
                    "min_required": self.MIN_SAMPLES,
                    "status": "insufficient_data",
                },
            }

        # Warmup: initialize EWMA + sigma from the first `warmup_n` samples
        # so we don't trip on a sigma=0 floor at i=0.
        warmup_n = 3
        warmup = xs[:warmup_n]
        ewma = sum(warmup) / len(warmup)
        sigma = math.sqrt(
            sum((x - ewma) ** 2 for x in warmup) / max(len(warmup) - 1, 1)
        )

        anomalies: list[dict] = []
        for i, x in enumerate(xs):
            # Skip anomaly checks during warmup.
            if i < warmup_n:
                ewma = alpha * x + (1 - alpha) * ewma
                sigma = math.sqrt(
                    alpha * (x - ewma) ** 2 + (1 - alpha) * sigma ** 2
                )
                continue
            deviation = x - ewma
            if abs(deviation) > k * max(sigma, 1e-9):
                anomalies.append({
                    "index": i,
                    "value": x,
                    "ewma_before": round(ewma, 4),
                    "sigma_before": round(sigma, 4),
                    "deviation": round(deviation, 4),
                    "z_score": round(deviation / max(sigma, 1e-9), 2),
                    "is_anomaly": True,
                })
            # Update EWMA + variance.
            ewma = alpha * x + (1 - alpha) * ewma
            sigma = math.sqrt(
                alpha * (x - ewma) ** 2 + (1 - alpha) * sigma ** 2
            )

        return {
            "anomalies": anomalies,
            "summary": {
                "samples": len(xs),
                "alpha": alpha,
                "k": k,
                "final_ewma": round(ewma, 4),
                "final_sigma": round(sigma, 4),
                "anomaly_count": len(anomalies),
                "status": "ok",
            },
        }

    @staticmethod
    def is_proactive_anomaly(anomaly_report: dict) -> bool:
        """True if the report contains anomalies indicating a proactive alert
        is warranted (i.e. before a hard threshold fires)."""
        summary = anomaly_report.get("summary", {})
        return summary.get("status") == "ok" and summary.get("anomaly_count", 0) > 0

    def fallback(self, series=None, **kw):
        return {
            "anomalies": [],
            "summary": {"status": "degraded", "note": "dynamic baseline unavailable"},
        }
