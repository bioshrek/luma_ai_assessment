"""The run's statistical vocabulary (M7). Pure domain — no store, no presenter.

These are the definitions every presenter reads. A test here is what stops the markdown, the
JSON and the console table from quietly answering the same question three ways.
"""

from __future__ import annotations

from rdp.domain.qc.rule import RuleResult, Verdict
from rdp.domain.run import (
    COMMIT,
    FETCH,
    NORMALIZE,
    QC,
    STAGES,
    IngestionRun,
    failure_reason,
    rule_rates,
)


def _run() -> IngestionRun:
    return IngestionRun(run_id="run_1", started_at="2026-01-01T00:00:00Z")


def test_a_failure_is_bucketed_by_exception_type_not_by_message() -> None:
    """Otherwise every episode gets its own bucket and "top reasons" names nothing."""
    a = "FetchError: pusht:episode_000007 timed out"
    b = "FetchError: pusht:episode_000031 timed out"
    assert failure_reason(a) == failure_reason(b) == "FetchError"


def test_a_message_with_no_type_prefix_is_still_countable() -> None:
    assert failure_reason("something went wrong") == "something went wrong"
    assert failure_reason("") == "unknown"
    assert failure_reason(":  ") == "unknown"


def test_top_failure_reasons_ranks_by_count_then_name() -> None:
    run = _run()
    for i in range(3):
        run.record_failure(f"s:ep{i}", "FetchError: gone")
    run.record_failure("s:ep9", "SchemaError: bad column")
    run.record_failure("s:ep8", "AdapterError: no clock")
    assert run.top_failure_reasons(2) == [("FetchError", 3), ("AdapterError", 1)]
    assert run.counters["failed"] == 5


def test_skip_reasons_never_collapse_two_rules_into_one_key() -> None:
    """"There is no action" and "the action is an episode label" are different findings."""
    run = _run()
    run.record_rule(
        RuleResult(rule_id="TS_MONOTONIC", verdict=Verdict.SKIPPED, reason="synthetic_timestamp")
    )
    run.record_rule(
        RuleResult(rule_id="ACTION_JERK", verdict=Verdict.SKIPPED, reason="episode_label_action")
    )
    run.record_rule(
        RuleResult(rule_id="ACTION_JERK", verdict=Verdict.SKIPPED, reason="no_action")
    )
    assert run.stats()["skip_reasons"] == {
        "ACTION_JERK": {"episode_label_action": 1, "no_action": 1},
        "TS_MONOTONIC": {"synthetic_timestamp": 1},
    }


def test_hit_rate_divides_by_the_episodes_a_rule_ran_on() -> None:
    """Dividing by the corpus would flatter a rule that skipped most of it."""
    rates = rule_rates(
        {
            "ACTION_JERK": {
                Verdict.PASS.value: 6,
                Verdict.REVIEW.value: 1,
                Verdict.FAIL.value: 1,
                Verdict.SKIPPED.value: 2,
            }
        }
    )
    (rate,) = rates
    assert (rate.evaluated, rate.hits, rate.skipped, rate.total) == (8, 2, 2, 10)
    assert rate.hit_rate == 0.25
    assert rate.skip_rate == 0.2


def test_a_rule_that_only_ever_skipped_has_a_defined_hit_rate() -> None:
    (rate,) = rule_rates({"TS_MONOTONIC": {Verdict.SKIPPED.value: 4}})
    assert rate.evaluated == 0
    assert rate.hit_rate == 0.0
    assert rate.skip_rate == 1.0


def test_errors_are_counted_but_are_not_hits() -> None:
    (rate,) = rule_rates(
        {"R": {Verdict.PASS.value: 1, Verdict.ERROR.value: 2, Verdict.FAIL.value: 1}}
    )
    assert (rate.evaluated, rate.hits, rate.errors) == (4, 1, 2)


def test_stage_seconds_accumulate_across_episodes() -> None:
    run = _run()
    run.record_stage(FETCH, 1.5)
    run.record_stage(FETCH, 0.25)
    run.record_stage(QC, 0.125)
    stats = run.stats()
    assert stats["stage_seconds"][FETCH] == 1.75
    assert stats["stage_calls"][FETCH] == 2
    assert stats["stage_seconds"][QC] == 0.125


def test_every_stage_appears_even_when_it_never_ran() -> None:
    """A resumed run that only commits still has to show fetch as zero, not as absent."""
    stats = _run().stats()
    assert list(stats["stage_seconds"]) == list(STAGES) == [FETCH, NORMALIZE, QC, COMMIT]
    assert set(stats["stage_calls"].values()) == {0}


def test_as_payload_matches_the_shape_the_repository_returns() -> None:
    """One renderer serves both the live run and a run read back days later."""
    run = _run()
    run.finish("2026-01-01T00:01:00Z")
    payload = run.as_payload()
    assert set(payload) == {
        "run_id",
        "started_at",
        "finished_at",
        "status",
        "resumed_from",
        "args",
        "stats",
    }
    assert payload["stats"] == run.stats()
