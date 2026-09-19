"""OpenTelemetry 可用性单一权威源（HTTP 导出）

从 app/main.py 保护性导入块抽出（2026-08-31 模块化）。
OTEL 为可选可观测组件，缺失/环境不兼容时 OTEL_AVAILABLE=False，应用降级为无追踪运行（main指点 #4）。
main.py、monitor_requests、observability 路由均从此模块读取，避免多份真值漂移。
"""
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    OTEL_AVAILABLE = True
except Exception as exc:
    import logging
    logging.getLogger(__name__).warning(
        "OpenTelemetry 导入失败，追踪降级: %s", type(exc).__name__
    )
    OTEL_AVAILABLE = False
