"""TaskTracker — lightweight wrapper for per-cycle task state in NXFH02.

Integrates with SkillContext to track what the G.A.M.E. agent has done
in the current decision cycle. Feeds into _get_trading_state so the
LLM planner has structured visibility into completion requirements.

NXFH02 allowed markets used as known symbols.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from src.control.task_state import (
    TaskState, TaskStatus, WaitReason,
    StepEvent, StepEventType, PlanStep,
)
from src.control.task_logic import (
    record_step_progress, can_mark_task_completed,
    handle_wait_action, check_wait_ready,
    check_step_budget, attempt_task_completion,
    emit_final_log,
)

logger = logging.getLogger("nxfh02.task_tracker")

KNOWN_SYMBOLS = {
    "BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "POL", "HYPE", "LINK"
}


def _parse_symbols(text: str) -> list[str]:
    """Extract known trading symbols mentioned in task description."""
    upper = text.upper()
    found = sorted(sym for sym in KNOWN_SYMBOLS if sym in upper)
    return found if found else []


def _build_plan_steps(task_description: str, symbols: list[str]) -> list[PlanStep]:
    """Derive required plan steps from task description."""
    desc = task_description.lower()
    steps = []

    if "scan" in desc or "opportunities" in desc:
        steps.append(PlanStep(label="scan_opportunities"))
        for sym in symbols:
            steps.append(PlanStep(label=f"evaluate_{sym}"))
        if symbols:
            steps.append(PlanStep(label="execute_trades"))

    elif "evaluate" in desc:
        for sym in symbols:
            steps.append(PlanStep(label=f"evaluate_{sym}"))
        steps.append(PlanStep(label="take_actions"))

    elif "execute" in desc or "trade" in desc:
        for sym in symbols:
            steps.append(PlanStep(label=f"execute_{sym}"))

    elif "monitor" in desc or "performance" in desc:
        steps.append(PlanStep(label="get_performance"))
        steps.append(PlanStep(label="assess_positions"))

    if not steps:
        steps.append(PlanStep(label="complete_task"))

    return steps


class NoOpTaskTracker:
    """Silent no-op replacement used when G.A.M.E. is not active (e.g. Senpi mode).

    Every method is a no-op so scan cycles don't burn fake step budgets and
    'step budget exhausted' noise never appears in non-GAME logs.
    """

    current = None
    _cycle_count = 0

    def start_task(self, task_description: str):
        return None

    def end_task(self):
        return None

    def record_scan(self, coin=None, detail: str = "") -> None:
        pass

    def record_evaluation(self, coin: str, decision: dict) -> None:
        pass

    def record_action(self, coin: str, action_detail: str = "") -> None:
        pass

    def record_plan_step(self, label: str, detail: str = "") -> None:
        pass

    def record_wait(self, reason: str = "unspecified", retry_after_seconds: float = 30.0, retry_condition: str = "") -> None:
        pass

    def try_complete(self) -> bool:
        return False

    def get_status_for_agent(self) -> dict:
        return {"task_active": False, "game_mode": False}

    def recent_history(self, n: int = 5) -> list:
        return []


class TaskTracker:
    """Manages the current task state for one G.A.M.E. decision cycle."""

    def __init__(self, allowed_symbols: Optional[list[str]] = None):
        self._allowed_symbols = set(allowed_symbols or KNOWN_SYMBOLS)
        self._current: Optional[TaskState] = None
        self._history: list[TaskState] = []
        self._cycle_count = 0

    # ── Task lifecycle ────────────────────────────────────────────────────

    def start_task(self, task_description: str) -> TaskState:
        """Create and start a new task. Call at beginning of each G.A.M.E. step."""
        if self._current and self._current.status == TaskStatus.RUNNING:
            logger.warning(
                "start_task called while task %s still RUNNING — archiving",
                self._current.task_id,
            )
            self._archive_current()

        self._cycle_count += 1
        task_id = f"task-{self._cycle_count}-{uuid.uuid4().hex[:6]}"
        symbols = _parse_symbols(task_description)
        steps = _build_plan_steps(task_description, symbols)

        self._current = TaskState(
            task_id=task_id,
            task_description=task_description,
            required_symbols=symbols,
            required_plan_steps=steps,
            max_steps=15,
        )

        logger.info(
            "TASK_CREATED task=%s symbols=%s steps=%s",
            task_id, symbols, [s.label for s in steps],
        )
        return self._current

    def end_task(self) -> Optional[TaskState]:
        """Call at end of G.A.M.E. step. Emits canonical final log."""
        if self._current is None:
            return None
        emit_final_log(self._current)
        self._archive_current()
        return self._history[-1] if self._history else None

    def _archive_current(self) -> None:
        if self._current:
            self._history.append(self._current)
            if len(self._history) > 50:
                self._history = self._history[-50:]
            self._current = None

    # ── Progress recording ────────────────────────────────────────────────

    def record_scan(self, coin: Optional[str] = None, detail: str = "") -> None:
        if not self._current:
            return
        events = []
        if coin and coin.upper() in self._allowed_symbols:
            events.append(StepEvent(
                StepEventType.SYMBOL_SCANNED,
                symbol=coin.upper(), detail=detail,
            ))
        else:
            events.append(StepEvent(
                StepEventType.PLAN_STEP_DONE,
                plan_step_label="scan_opportunities",
                detail=detail,
            ))
        for ev in events:
            record_step_progress(self._current, ev)
        check_step_budget(self._current)

    def record_evaluation(self, coin: str, decision: dict) -> None:
        if not self._current:
            return
        sym = coin.upper()
        record_step_progress(self._current, StepEvent(
            StepEventType.SYMBOL_EVALUATED,
            symbol=sym,
            plan_step_label=f"evaluate_{sym}",
            detail=str(decision),
        ))
        record_step_progress(self._current, StepEvent(
            StepEventType.PLAN_STEP_DONE,
            symbol=sym,
            plan_step_label=f"evaluate_{sym}",
        ))
        self._current.result_payload[sym] = decision
        check_step_budget(self._current)

    def record_action(self, coin: str, action_detail: str = "") -> None:
        if not self._current:
            return
        sym = coin.upper()
        record_step_progress(self._current, StepEvent(
            StepEventType.ACTION_TAKEN,
            symbol=sym,
            plan_step_label=f"execute_{sym}",
            detail=action_detail,
        ))
        # Check if all symbols actioned → mark execute_trades done
        if self._current.required_symbols:
            all_actioned = all(
                self._current.symbol_progress.get(s, type('', (), {'actioned': False})()).actioned
                for s in self._current.required_symbols
            )
            if all_actioned:
                record_step_progress(self._current, StepEvent(
                    StepEventType.PLAN_STEP_DONE,
                    plan_step_label="execute_trades",
                ))
        check_step_budget(self._current)

    def record_plan_step(self, label: str, detail: str = "") -> None:
        if not self._current:
            return
        record_step_progress(self._current, StepEvent(
            StepEventType.PLAN_STEP_DONE,
            plan_step_label=label,
            detail=detail,
        ))
        check_step_budget(self._current)

    def record_wait(
        self,
        reason: str = "unspecified",
        retry_after_seconds: float = 30.0,
        retry_condition: str = "retry window elapsed",
    ) -> None:
        """Record WAIT — task stays RUNNING after retry window, NOT terminal."""
        if not self._current:
            return
        try:
            wait_reason = WaitReason(reason)
        except ValueError:
            wait_reason = WaitReason.UNSPECIFIED
        handle_wait_action(
            self._current, wait_reason, retry_after_seconds, retry_condition,
        )
        # CRITICAL: WAIT does NOT end the task. Loop must continue.

    def try_complete(self) -> bool:
        """Attempt to mark task complete. Returns True if succeeded."""
        if not self._current:
            return False
        attempt_task_completion(self._current)
        return self._current.status == TaskStatus.COMPLETED

    # ── State inspection ──────────────────────────────────────────────────

    @property
    def current(self) -> Optional[TaskState]:
        return self._current

    def get_status_for_agent(self) -> dict:
        """Structured state to inject into _get_trading_state.

        The LLM planner reads this and knows what still needs to be done.
        """
        if self._current is None:
            return {
                "task_active": False,
                "cycle": self._cycle_count,
            }

        ts = self._current
        ok, incomplete_reason = can_mark_task_completed(ts)

        return {
            "task_active": True,
            "task_id": ts.task_id,
            "status": ts.status.name,
            "step": ts.current_step,
            "max_steps": ts.max_steps,
            "steps_remaining": ts.max_steps - ts.current_step,
            "can_complete": ok,
            "incomplete_reason": incomplete_reason if not ok else None,
            "symbols_required": ts.required_symbols,
            "symbols_evaluated": [
                sym for sym, sp in ts.symbol_progress.items() if sp.evaluated
            ],
            "symbols_remaining": [
                sym for sym, sp in ts.symbol_progress.items() if not sp.evaluated
            ],
            "plan_steps_done": [s.label for s in ts.required_plan_steps if s.done],
            "plan_steps_remaining": [s.label for s in ts.required_plan_steps if not s.done],
            "is_waiting": ts.status == TaskStatus.WAITING,
            "wait_reason": ts.wait_state.reason.value if ts.wait_state else None,
            "cycle": self._cycle_count,
        }

    def recent_history(self, n: int = 5) -> list[dict]:
        """Summary of recent task outcomes."""
        return [
            {
                "task_id": t.task_id,
                "status": t.status.name,
                "steps": t.current_step,
                "symbols_done": [
                    sym for sym, sp in t.symbol_progress.items() if sp.evaluated
                ],
            }
            for t in self._history[-n:]
        ]
