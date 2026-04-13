"""Task State Model for NXFH02 control-loop hardening.

Fixes the root cause where WAIT is treated as an implicit terminal action.
WAIT is a pause signal, never a completion signal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class TaskStatus(Enum):
    RUNNING   = auto()
    COMPLETED = auto()   # all required work done and validated
    STALLED   = auto()   # step budget exhausted without completion
    ABORTED   = auto()   # unrecoverable error or explicit abort
    WAITING   = auto()   # paused — NOT terminal, resume after retry


class WaitReason(Enum):
    RATE_LIMIT     = "rate_limit"
    AWAITING_FILL  = "awaiting_fill"
    AWAITING_DATA  = "awaiting_data"
    RETRY_BACKOFF  = "retry_backoff"
    UNSPECIFIED    = "unspecified"


class StepEventType(Enum):
    SYMBOL_SCANNED   = "symbol_scanned"
    SYMBOL_EVALUATED = "symbol_evaluated"
    ACTION_TAKEN     = "action_taken"
    PLAN_STEP_DONE   = "plan_step_done"
    WAIT_ISSUED      = "wait_issued"
    RETRY_TRIGGERED  = "retry_triggered"
    ABORT_REQUESTED  = "abort_requested"


@dataclass
class PlanStep:
    label: str
    done: bool = False
    completed_at: Optional[float] = None

    def mark_done(self) -> None:
        self.done = True
        self.completed_at = time.time()


@dataclass
class SymbolProgress:
    symbol: str
    scanned: bool   = False
    evaluated: bool = False
    actioned: bool  = False   # trade executed or explicitly skipped


@dataclass
class WaitState:
    reason: WaitReason
    retry_after: float
    retry_condition: str
    issued_at: float = field(default_factory=time.time)
    retries_remaining: int = 3


@dataclass
class StepEvent:
    event_type: StepEventType
    symbol: Optional[str]          = None
    plan_step_label: Optional[str] = None
    detail: str                    = ""
    timestamp: float               = field(default_factory=time.time)


@dataclass
class TaskState:
    task_id: str
    task_description: str
    required_symbols: list[str]
    required_plan_steps: list[PlanStep]
    symbol_progress: dict[str, SymbolProgress] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.RUNNING
    current_step: int  = 0
    max_steps: int     = 15
    events: list[StepEvent] = field(default_factory=list)
    wait_state: Optional[WaitState] = None
    result_payload: dict = field(default_factory=dict)
    created_at: float  = field(default_factory=time.time)
    ended_at: Optional[float] = None
    terminal_reason: str = ""

    def __post_init__(self) -> None:
        for sym in self.required_symbols:
            if sym not in self.symbol_progress:
                self.symbol_progress[sym] = SymbolProgress(symbol=sym)

    def to_summary(self) -> dict:
        """Compact summary for injection into _get_trading_state."""
        return {
            "task_id": self.task_id,
            "status": self.status.name,
            "step": f"{self.current_step}/{self.max_steps}",
            "symbols_done": {
                sym: {
                    "scanned": sp.scanned,
                    "evaluated": sp.evaluated,
                    "actioned": sp.actioned,
                }
                for sym, sp in self.symbol_progress.items()
            },
            "steps_done": [s.label for s in self.required_plan_steps if s.done],
            "steps_remaining": [s.label for s in self.required_plan_steps if not s.done],
            "waiting": self.status == TaskStatus.WAITING,
            "wait_reason": self.wait_state.reason.value if self.wait_state else None,
        }
