"""
Observability for GrowthMate (Revision 3) — trace ids, stage latency, audit.

A process-local latency store records per-stage durations so `/metrics/latency`
can expose P50/P95. `write_audit` is the single str:point for AuditLog rows so
every material stage lands the same envelope (trace_id, stage, latency, source,
cache_hit, confidence) without duplicating DB logic.

This is explicitly a documented, process-local cache (ARCHITECTURE §15). No
secrets or raw PII ever pass through here — callers redact first.
"""

import contextvars
import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app import db as db_module
from app.models import AuditLog
from app.pii import redact_text

_LOCK = threading.Lock()
# stage -> list of (timestamp_ms, duration_ms). Bounded lazily on read/reset.
_LATENCIES: dict[str, list[float]] = {}

# Per-trace stage timings (bounded by turn count; drained on read).
_TRACE_TIMES: dict[str, list[float]] = {}
_TRACE: contextvars.ContextVar[str] = contextvars.ContextVar("gm_trace", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time a pipeline stage. Duration is appended to both the global latency
    store (for /metrics/latency) and the active trace (for this turn's audit
    envelope). Uses a process-local context so nested nodes never conflate."""
    trace = _TRACE.get()
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        record_stage_ms(name, duration_ms)
        if trace:
            with _LOCK:
                _TRACE_TIMES.setdefault(trace, []).append(duration_ms)


@contextmanager
def run_turn_time(trace_id: str):
    """Context manager that binds `trace_id` for the whole turn, then drains
    the per-trace timings once the turn completes."""
    token = _TRACE.set(trace_id)
    try:
        yield
    finally:
        _TRACE.reset(token)
        with _LOCK:
            _TRACE_TIMES.pop(trace_id, None)


def total_ms(trace_id: str) -> float:
    """Total measured stage time for a turn (drained on read)."""
    with _LOCK:
        vals = _TRACE_TIMES.get(trace_id) or []
    return round(sum(vals), 3)


def start_stage(trace_id: str, stage: str) -> float:
    return time.perf_counter()


def end_stage(trace_id: str, stage: str, start: float) -> float:
    duration_ms = (time.perf_counter() - start) * 1000.0
    with _LOCK:
        _LATENCIES.setdefault(stage, []).append(duration_ms)
    return round(duration_ms, 3)


def record_stage_ms(stage: str, duration_ms: float) -> None:
    with _LOCK:
        _LATENCIES.setdefault(stage, []).append(float(duration_ms))


def latency_summary() -> dict[str, dict]:
    """{stage: {count, p50, p95, avg}} for /metrics/latency."""
    with _LOCK:
        snapshot = {stage: sorted(vals[-500:]) for stage, vals in _LATENCIES.items()}
    out: dict[str, dict] = {}
    for stage in sorted(snapshot):
        vals = snapshot[stage]
        if not vals:
            continue
        out[stage] = {
            "count": len(vals),
            "p50": round(_percentile(vals, 0.50), 3),
            "p95": round(_percentile(vals, 0.95), 3),
            "avg": round(sum(vals) / len(vals), 3),
        }
    return out


def reset_latency() -> None:
    with _LOCK:
        _LATENCIES.clear()


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[idx]


def write_audit(
    *,
    session_id: str,
    actor: str,
    event_type: str,
    tool_name: Optional[str],
    parameters: Optional[dict],
    outcome: str,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
    error_detail: Optional[str] = None,
    trace_id: Optional[str] = None,
    stage: Optional[str] = None,
    latency_ms: Optional[float] = None,
    source: Optional[str] = None,
    cache_hit: Optional[bool] = None,
    confidence: Optional[float] = None,
    tier_used: Optional[int] = None,
    mandate_signature: Optional[str] = None,
) -> None:
    """Persist one AuditLog row. Redacts parameter payloads before storage."""
    db = db_module.SessionLocal()
    try:
        db.add(
            AuditLog(
                session_id=session_id,
                actor=actor,
                event_type=event_type,
                tool_name=tool_name,
                parameters_json=_safe_redacted_json(parameters),
                agent_reasoning=None,
                decision=decision,
                reason=reason,
                outcome=outcome,
                error_detail=error_detail,
                trace_id=trace_id,
                stage=stage,
                latency_ms=latency_ms,
                source=source,
                cache_hit=cache_hit,
                confidence=confidence,
                tier_used=tier_used,
                mandate_signature=mandate_signature,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 — an audit failure must never crash a turn
        db.rollback()
    finally:
        db.close()


def _safe_redacted_json(parameters: Optional[dict]) -> Optional[str]:
    if parameters is None:
        return None
    try:
        return json.dumps(_redact_value(parameters))
    except Exception:  # noqa: BLE001
        return None


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value