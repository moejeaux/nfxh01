from src.intelligence.rollback import (
    evaluate_pending_changes_placeholder,
    evaluate_pending_learning_changes,
)


def run_topk_advisory_review(*args, **kwargs):
    from src.intelligence.topk_review import run_topk_advisory_review as _impl

    return _impl(*args, **kwargs)


def run_weekly_llm_summary(*args, **kwargs):
    from src.intelligence.weekly_summary import run_weekly_llm_summary as _impl

    return _impl(*args, **kwargs)

__all__ = [
    "evaluate_pending_learning_changes",
    "evaluate_pending_changes_placeholder",
    "run_topk_advisory_review",
    "run_weekly_llm_summary",
]
