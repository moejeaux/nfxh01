"""Task logic — completion predicates, WAIT handling, budget enforcement.

Core fix: WAIT is never a terminal action.
Completion requires explicit validation of all required work.
"""

from __future__ import annotations

import time
import logging
from typing import Optional

from src.control.task_state import (
    TaskState, TaskStatus, WaitState, WaitReason,
    StepEvent, StepEventType, SymbolProgress,
)

logger = logging.getLogger("nxfh02.task_logic")

# Canonical log tags
TASK_CREATED               = "TASK_CREATED"
TASK_COMPLETED             = "TASK_COMPLETED"
TASK_STALLED_STEP_LIMIT    = "TASK_STALLED_STEP_LIMIT"
TASK_ABORTED_INVALID_STATE = "TASK_ABORTED_INVALID_STATE"
TASK_WAITING               = "TASK_WAITING_FOR_CONDITION"
TASK_COMPLETION_BLOCKED    = "TASK_COMPLETION_BLOCKED"
TASK_RETRY                 = "TASK_RETRY"
SYMBOL_SCANNED             = "SYMBOL_SCANNED"
SYMBOL_EVALUATED           = "SYMBOL_EVALUATED"
ACTION_TAKEN               = "ACTION_TAKEN"
PLAN_STEP_DONE             = "PLAN_STEP_DONE"


def record_step_progress(task_state: TaskState, event: StepEvent) -> TaskState:
    """Apply a StepEvent — only mutation point during a step."""
    if task_state.status not in (TaskStatus.RUNNING, TaskStatus.WAITING):
        logger.warning(
            "record_step_progress on non-active task %s (status=%s) — ignoring",
            task_state.task_id, task_state.status.name,
        )
        return task_state

    task_state.events.append(event)
    task_state.current_step += 1

    sym = event.symbol
    etype = event.event_type

    if sym and sym in task_state.symbol_progress:
        sp = task_state.symbol_progress[sym]
        if etype == StepEventType.SYMBOL_SCANNED:
            sp.scanned = True
            logger.info("%s task=%s symbol=%s", SYMBOL_SCANNED, task_state.task_id, sym)
        elif etype == StepEventType.SYMBOL_EVALUATED:
            sp.evaluated = True
            logger.info("%s task=%s symbol=%s", SYMBOL_EVALUATED, task_state.task_id, sym)
        elif etype == StepEventType.ACTION_TAKEN:
            sp.actioned = True
            logger.info("%s task=%s symbol=%s detail=%s",
                        ACTION_TAKEN, task_state.task_id, sym, event.detail)

    if etype == StepEventType.PLAN_STEP_DONE and event.plan_step_label:
        for step in task_state.required_plan_steps:
            if step.label == event.plan_step_label and not step.done:
                step.mark_done()
                logger.info("%s task=%s step=%s",
                            PLAN_STEP_DONE, task_state.task_id, step.label)
                break

    if etype == StepEventType.RETRY_TRIGGERED and task_state.status == TaskStatus.WAITING:
        task_state.status = TaskStatus.RUNNING
        task_state.wait_state = None
        logger.info("%s task=%s", TASK_RETRY, task_state.task_id)

    if etype == StepEventType.ABORT_REQUESTED:
        _mark_terminal(task_state, TaskStatus.ABORTED, event.detail)

    return task_state


def can_mark_task_completed(task_state: TaskState) -> tuple[bool, str]:
    """Authoritative completion gate — LLM cannot bypass this."""
    if task_state.status == TaskStatus.ABORTED:
        return False, "task is ABORTED"

    for sym in task_state.required_symbols:
        sp = task_state.symbol_progress.get(sym)
        if sp is None:
            return False, f"symbol {sym} has no progress record"
        if not sp.evaluated:
            return False, f"symbol {sym} has not been evaluated"

    for step in task_state.required_plan_steps:
        if not step.done:
            return False, f"plan step '{step.label}' not yet completed"

    if not task_state.result_payload:
        return False, "result_payload is empty — no concrete output recorded"

    for sym in task_state.required_symbols:
        if sym not in task_state.result_payload:
            return False, f"symbol {sym} missing from result_payload"

    return True, ""


def handle_wait_action(
    task_state: TaskState,
    reason: WaitReason,
    retry_after_seconds: float,
    retry_condition: str,
) -> TaskState:
    """Transition to WAITING. WAIT is NEVER terminal."""
    if task_state.status in (TaskStatus.COMPLETED, TaskStatus.ABORTED, TaskStatus.STALLED):
        logger.error(
            "handle_wait_action on terminal task %s (status=%s) — ignoring",
            task_state.task_id, task_state.status.name,
        )
        return task_state

    retry_after_seconds = max(5.0, retry_after_seconds)

    task_state.wait_state = WaitState(
        reason=reason,
        retry_after=time.time() + retry_after_seconds,
        retry_condition=retry_condition,
    )
    task_state.status = TaskStatus.WAITING

    record_step_progress(
        task_state,
        StepEvent(
            event_type=StepEventType.WAIT_ISSUED,
            detail=f"reason={reason.value} retry_in={retry_after_seconds}s",
        ),
    )

    logger.info(
        "%s task=%s reason=%s retry_in=%.1fs condition=%s",
        TASK_WAITING, task_state.task_id,
        reason.value, retry_after_seconds, retry_condition,
    )
    return task_state


def check_wait_ready(task_state: TaskState) -> bool:
    if task_state.status != TaskStatus.WAITING:
        return False
    if task_state.wait_state is None:
        return True
    return time.time() >= task_state.wait_state.retry_after


def check_step_budget(task_state: TaskState) -> TaskState:
    """Budget exhaustion → STALLED, never COMPLETED."""
    if task_state.status in (TaskStatus.COMPLETED, TaskStatus.ABORTED, TaskStatus.STALLED):
        return task_state

    if task_state.current_step >= task_state.max_steps:
        ok, reason = can_mark_task_completed(task_state)
        if not ok:
            _mark_terminal(
                task_state,
                TaskStatus.STALLED,
                f"step budget exhausted at {task_state.current_step}/{task_state.max_steps}. "
                f"Incomplete: {reason}",
            )
    return task_state


def attempt_task_completion(task_state: TaskState) -> TaskState:
    """Gate for COMPLETED. Must be called explicitly — never by WAIT or budget."""
    ok, reason = can_mark_task_completed(task_state)
    if ok:
        _mark_terminal(task_state, TaskStatus.COMPLETED, "all conditions satisfied")
        logger.info("%s task=%s steps=%d",
                    TASK_COMPLETED, task_state.task_id, task_state.current_step)
    else:
        logger.warning("%s task=%s reason=%s",
                       TASK_COMPLETION_BLOCKED, task_state.task_id, reason)
    return task_state


def emit_final_log(task_state: TaskState) -> None:
    """Single authoritative exit log — replaces ambiguous 'Task ended' message."""
    tag = {
        TaskStatus.COMPLETED: TASK_COMPLETED,
        TaskStatus.STALLED:   TASK_STALLED_STEP_LIMIT,
        TaskStatus.ABORTED:   TASK_ABORTED_INVALID_STATE,
        TaskStatus.WAITING:   TASK_WAITING,
        TaskStatus.RUNNING:   "TASK_EXITED_WHILE_RUNNING",
    }.get(task_state.status, "TASK_STATUS_UNKNOWN")

    logger.info(
        "%s task=%s steps=%d/%d symbols=%s steps_done=%s steps_remaining=%s reason=%s",
        tag,
        task_state.task_id,
        task_state.current_step,
        task_state.max_steps,
        {sym: vars(task_state.symbol_progress[sym]) for sym in task_state.required_symbols},
        [s.label for s in task_state.required_plan_steps if s.done],
        [s.label for s in task_state.required_plan_steps if not s.done],
        task_state.terminal_reason,
    )


def _mark_terminal(task_state: TaskState, status: TaskStatus, reason: str) -> None:
    task_state.status = status
    task_state.terminal_reason = reason
    task_state.ended_at = time.time()
    level = logging.INFO if status == TaskStatus.COMPLETED else logging.WARNING
    logger.log(level, "%s task=%s reason=%s", status.name, task_state.task_id, reason)
