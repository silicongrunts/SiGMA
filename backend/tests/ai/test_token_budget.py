"""TokenBudgetTracker: display vs budget accounting.

A chat turn split across pause/resume cycles reassembles its whole-turn
stats from three buckets: ``base`` (spend persisted by earlier tasks of
the same turn), ``carry`` (subagent spend from before a pause, not yet in
any parent row), and ``usage`` (this task's accrual). Budget enforcement
keeps counting the accrual only — earlier tasks already ran within their
own budgets.
"""

from app.services.token_budget import TokenBudgetTracker, TokenUsage


def test_total_usage_sums_base_carry_and_accrual():
    tracker = TokenBudgetTracker(
        base=TokenUsage(input=100, output=10, cached=80),
        carry=TokenUsage(input=50, output=5, cached=40),
    )
    tracker.add_llm_usage({
        "prompt_tokens": 200, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 150},
    })

    total = tracker.total_usage
    assert total.to_dict() == {"input": 350, "output": 35, "cached": 270}
    # The display buckets never contaminate the accrual the budget reads.
    assert tracker.usage.to_dict() == {"input": 200, "output": 20, "cached": 150}


def test_zero_defaults_keep_legacy_shape():
    tracker = TokenBudgetTracker()
    assert tracker.total_usage.to_dict() == {"input": 0, "output": 0, "cached": 0}


def test_budget_exceeded_counts_accrual_only():
    # base+carry alone above the budget must not trip it: the parked
    # segment's spend was already accepted before the pause.
    tracker = TokenBudgetTracker(budget=100, base=TokenUsage(input=500, output=50))
    assert not tracker.exceeded

    tracker.add_llm_usage({"prompt_tokens": 60, "completion_tokens": 50})
    assert tracker.exceeded


def test_no_budget_never_exceeds():
    tracker = TokenBudgetTracker(base=TokenUsage(input=10**9))
    assert not tracker.exceeded
