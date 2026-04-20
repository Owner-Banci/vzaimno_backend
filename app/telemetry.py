from __future__ import annotations

from fastapi import FastAPI

from app.config import get_env, get_float
from app.logging_utils import logger


_TELEMETRY_INITIALIZED = False


def _load_otel_components():
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    psycopg_instrumentor = None
    for module_name, class_name in (
        ("opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
        ("opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor"),
    ):
        try:
            module = __import__(module_name, fromlist=[class_name])
            psycopg_instrumentor = getattr(module, class_name)
            break
        except Exception:
            continue

    return {
        "trace": trace,
        "OTLPSpanExporter": OTLPSpanExporter,
        "FastAPIInstrumentor": FastAPIInstrumentor,
        "Resource": Resource,
        "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "psycopg_instrumentor": psycopg_instrumentor,
    }


def init_telemetry(app: FastAPI) -> None:
    global _TELEMETRY_INITIALIZED

    if _TELEMETRY_INITIALIZED:
        return

    endpoint = (get_env("OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip()
    if not endpoint:
        logger.info("otel_disabled", extra={"status_code": 0})
        _TELEMETRY_INITIALIZED = True
        return

    try:
        components = _load_otel_components()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "otel_dependencies_missing",
            extra={"status_code": 0, "error": str(exc)},
        )
        _TELEMETRY_INITIALIZED = True
        return

    trace = components["trace"]
    resource = components["Resource"].create(
        {
            "service.name": (get_env("OTEL_SERVICE_NAME", "vzaimno-backend") or "vzaimno-backend"),
            "service.version": (get_env("APP_GIT_SHA", "unknown") or "unknown"),
            "deployment.environment": (get_env("ENV", "dev") or "dev"),
        }
    )
    tracer_provider = components["TracerProvider"](resource=resource)
    exporter = components["OTLPSpanExporter"](
        endpoint=endpoint,
        timeout=max(1.0, get_float("OTEL_EXPORTER_OTLP_TIMEOUT_S", 5.0)),
    )
    tracer_provider.add_span_processor(components["BatchSpanProcessor"](exporter))
    trace.set_tracer_provider(tracer_provider)

    try:
        components["FastAPIInstrumentor"].instrument_app(app)
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel_fastapi_instrumentation_failed", extra={"status_code": 0, "error": str(exc)})

    psycopg_instrumentor = components["psycopg_instrumentor"]
    if psycopg_instrumentor is not None:
        try:
            psycopg_instrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel_psycopg_instrumentation_failed", extra={"status_code": 0, "error": str(exc)})

    logger.info("otel_enabled", extra={"status_code": 0})
    _TELEMETRY_INITIALIZED = True

