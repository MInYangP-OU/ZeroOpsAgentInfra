"""Sample metric time-series used by AnomalyDetector when no Prometheus is connected.

Mirrors PrometheusMCP.DEFAULT_SERIES so the detector can run offline / in stub mode.
"""
SAMPLE_METRICS = {
    "order-service": {
        "error_rate": [0.02, 0.025, 0.03, 0.08, 0.12, 0.15, 0.18],
        "latency_ms": [80, 85, 90, 300, 600, 850, 1200],
    },
    "payment-service": {
        "error_rate": [0.005, 0.008, 0.01, 0.05, 0.15, 0.30, 0.32],
        "latency_ms": [30, 35, 40, 200, 700, 1200, 1400],
    },
    "inventory-service": {
        "error_rate": [0.01, 0.015, 0.02, 0.10, 0.25, 0.40, 0.45],
        "latency_ms": [50, 60, 70, 400, 1200, 2000, 2500],
    },
    "api-gateway": {
        "error_rate": [0.005, 0.008, 0.01, 0.03, 0.06, 0.10, 0.11],
        "latency_ms": [40, 42, 45, 120, 300, 500, 600],
    },
}
