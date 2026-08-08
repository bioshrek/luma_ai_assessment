"""`TERMINATION_CONSISTENCY` — the end-of-episode marker disagrees with where the episode ends.

This rule catches two errors no other rule can see, both of them *our* errors rather than
upstream's: two episodes concatenated during normalization (an end signal appears mid-episode,
with ordinary frames after it), and one episode split in half (nothing marks the final frame).

It reads the column named by `meta.termination_column` — never a guessed name. Sources spell it
`raw.next.done` and `raw.is_terminal`, and a source that trimmed its markers away during
normalization must report `has_termination_signal=False` rather than offer a column that no
longer contains the fact.

**Measured, and the reason `max_terminal_run` exists.** LeRobot's pusht sets `next.done` on the
*last two* frames of all 80 episodes, not just the last — the terminal transition is recorded
twice. A trailing run of markers is not a concatenation error, so the FAIL condition is
specifically "a marker with ordinary frames after it", and the length of the trailing run is a
separate, softer complaint with a threshold taken from that measurement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "TERMINATION_CONSISTENCY"


def _no_levels() -> Mapping[str, SignalLevel]:
    return {}


@dataclass(frozen=True)
class TerminationConsistency:
    max_terminal_run: int = 2
    """How many trailing frames may carry the marker before it looks like a stuck flag."""

    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset({"has_termination_signal"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_no_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        column = meta.termination_column
        if column is None:
            return RuleResult(
                self.rule_id,
                Verdict.SKIPPED,
                {},
                "the source declares an end signal but names no column carrying it",
            )
        values = np.asarray(frames.column(column), dtype=np.float64)
        marked = np.isfinite(values) & (values != 0.0)
        if marked.size == 0:
            return RuleResult(self.rule_id, Verdict.SKIPPED, {}, "no frames")

        unmarked = np.flatnonzero(~marked)
        terminal_run = marked.size if unmarked.size == 0 else int(marked.size - unmarked[-1] - 1)
        n_marked = int(np.count_nonzero(marked))
        # An end signal that has ordinary frames after it is the concatenation signature.
        n_interior = int(np.count_nonzero(marked[: marked.size - terminal_run]))
        metrics = {
            "n_end_signals": float(n_marked),
            "terminal_run": float(terminal_run),
            "n_interior_signals": float(n_interior),
        }

        if n_interior:
            return RuleResult(
                self.rule_id,
                Verdict.FAIL,
                metrics,
                f"{n_interior} end signal(s) with ordinary frames after them: "
                f"episodes look concatenated ({meta.boundary.termination_source})",
            )
        if not terminal_run:
            return RuleResult(
                self.rule_id,
                Verdict.REVIEW,
                metrics,
                "no end signal on the final frame: the episode was cut without being marked",
            )
        if terminal_run > self.max_terminal_run:
            return RuleResult(
                self.rule_id,
                Verdict.REVIEW,
                metrics,
                f"the end signal is set on the last {terminal_run} frames "
                f"(> {self.max_terminal_run}): it reads like a stuck flag",
            )
        return RuleResult(
            self.rule_id,
            Verdict.PASS,
            metrics,
            f"end signal on the last {terminal_run} frame(s) and nowhere else",
        )
