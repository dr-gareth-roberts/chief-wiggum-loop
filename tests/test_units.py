"""Pytest unit suite for the pure helpers in ``scripts/wiggum_isolated_loop.py``.

These tests exercise the helpers as in-process functions: ``argparse.Namespace``
objects are built directly via the ``make_args`` fixture so tests never go
through ``parse_args`` (which inspects the cwd and can mutate ``--sandbox``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


# --- parse_metrics -----------------------------------------------------------


def test_parse_metrics_prefixed_form_always_matches(wil_module):
    metrics = wil_module.parse_metrics("METRIC score=10\nnoise", metric_name="")
    assert metrics == {"score": 10.0}


def test_parse_metrics_bare_form_requires_configured_name(wil_module):
    text = "score=10\nlatency=42"
    # Without an explicit metric_name, the bare form must NOT match.
    assert wil_module.parse_metrics(text, metric_name="") == {}
    # With a matching metric_name, only the matching bare line is included.
    assert wil_module.parse_metrics(text, metric_name="score") == {"score": 10.0}


def test_parse_metrics_bare_form_ignores_unrelated_kv_lines(wil_module):
    # The bug this guards against: PORT=8080 should not be promoted to a metric
    # just because the agent's output mentions it. Only the configured name wins.
    text = "PORT=8080\nscore=7"
    metrics = wil_module.parse_metrics(text, metric_name="score")
    assert "PORT" not in metrics
    assert metrics == {"score": 7.0}


def test_parse_metrics_prefixed_and_bare_can_coexist(wil_module):
    text = "METRIC latency=99\nscore=12"
    # The prefixed form parses unconditionally; the bare form parses because
    # metric_name matches the bare line name.
    metrics = wil_module.parse_metrics(text, metric_name="score")
    assert metrics == {"latency": 99.0, "score": 12.0}


def test_parse_metrics_handles_negative_and_decimal_values(wil_module):
    text = "METRIC delta=-3.5"
    assert wil_module.parse_metrics(text) == {"delta": -3.5}


# --- classify_stuck_reason ---------------------------------------------------


def test_classify_stuck_reason_stagnation_wins_over_refusal_regex(wil_module):
    """Regression: when the agent says "I cannot" but workspace didn't change,
    the classifier must return ``no_workspace_progress`` rather than
    ``model_refusal_or_give_up`` — otherwise stuck loops mis-self-report."""

    candidate = {
        "output_tail": "I cannot find foo",
        "verifier_output_tail": "",
        "verifier_exit_code": None,
        "agent_timeout": False,
        "timeout": False,
    }
    assert (
        wil_module.classify_stuck_reason(candidate, stagnant=1, previous_failure_hash="")
        == "no_workspace_progress"
    )


def test_classify_stuck_reason_timeout_takes_precedence(wil_module):
    candidate = {
        "output_tail": "I cannot do that",
        "verifier_output_tail": "verifier said no",
        "verifier_exit_code": 1,
        "agent_timeout": True,
    }
    # Even with stagnation and a refusal phrase, timeout dominates because the
    # agent never finished its turn.
    assert (
        wil_module.classify_stuck_reason(candidate, stagnant=2, previous_failure_hash="prev")
        == "agent_timeout"
    )


def test_classify_stuck_reason_same_verifier_failure_detected(wil_module):
    import hashlib

    verifier = "FAIL: assertion error on line 42"
    prev_hash = hashlib.sha256(verifier.encode()).hexdigest()
    candidate = {
        "output_tail": "",
        "verifier_output_tail": verifier,
        "verifier_exit_code": 1,
        "agent_timeout": False,
    }
    assert (
        wil_module.classify_stuck_reason(candidate, stagnant=0, previous_failure_hash=prev_hash)
        == "same_verifier_failure"
    )


def test_classify_stuck_reason_metric_missing_when_required(wil_module):
    candidate = {
        "output_tail": "",
        "verifier_output_tail": "",
        "verifier_exit_code": None,
        "metric_required": True,
        "metric_value": None,
        "agent_timeout": False,
    }
    assert (
        wil_module.classify_stuck_reason(candidate, stagnant=0, previous_failure_hash="")
        == "metric_missing"
    )


def test_classify_stuck_reason_refusal_only_when_no_other_signal(wil_module):
    # Refusal regex should fire only when stagnant=0 AND no verifier failure
    # AND no metric requirement.
    candidate = {
        "output_tail": "Sorry, as an AI I cannot help with that",
        "verifier_output_tail": "",
        "verifier_exit_code": None,
        "agent_timeout": False,
        "timeout": False,
    }
    assert (
        wil_module.classify_stuck_reason(candidate, stagnant=0, previous_failure_hash="")
        == "model_refusal_or_give_up"
    )


def test_classify_stuck_reason_no_signal_returns_empty(wil_module):
    candidate = {
        "output_tail": "All good, made a change",
        "verifier_output_tail": "ok",
        "verifier_exit_code": 0,
        "agent_timeout": False,
    }
    assert wil_module.classify_stuck_reason(candidate, stagnant=0, previous_failure_hash="") == ""


# --- candidate_rank ----------------------------------------------------------


def test_candidate_rank_verifier_success_beats_metric(wil_module, make_args):
    args = make_args(metric_name="score", metric_direction="higher")
    high_metric_no_verifier = {"verifier_exit_code": 1, "metric_value": 999.0, "patch_chars": 100}
    low_metric_with_verifier = {"verifier_exit_code": 0, "metric_value": 1.0, "patch_chars": 100}
    # The tuple ordering means verifier success (1.0) beats any metric score
    # when the other has verifier_exit_code != 0 (verifier_bonus=0.0).
    assert wil_module.candidate_rank(low_metric_with_verifier, args, best_metric=None) > wil_module.candidate_rank(
        high_metric_no_verifier, args, best_metric=None
    )


def test_candidate_rank_metric_breaks_tied_verifier(wil_module, make_args):
    args = make_args(metric_name="score", metric_direction="higher")
    a = {"verifier_exit_code": 0, "metric_value": 5.0, "patch_chars": 100}
    b = {"verifier_exit_code": 0, "metric_value": 9.0, "patch_chars": 100}
    assert wil_module.candidate_rank(b, args, None) > wil_module.candidate_rank(a, args, None)


def test_candidate_rank_smaller_patch_wins_on_tie(wil_module, make_args):
    args = make_args(metric_name="score", metric_direction="higher")
    big = {"verifier_exit_code": 0, "metric_value": 5.0, "patch_chars": 100_000}
    small = {"verifier_exit_code": 0, "metric_value": 5.0, "patch_chars": 100}
    # With verifier_bonus and metric_score equal, the third tuple element
    # rewards smaller patches.
    assert wil_module.candidate_rank(small, args, None) > wil_module.candidate_rank(big, args, None)


def test_candidate_rank_lower_direction_inverts_metric_score(wil_module, make_args):
    args = make_args(metric_name="latency", metric_direction="lower")
    fast = {"verifier_exit_code": 0, "metric_value": 10.0, "patch_chars": 100}
    slow = {"verifier_exit_code": 0, "metric_value": 100.0, "patch_chars": 100}
    assert wil_module.candidate_rank(fast, args, None) > wil_module.candidate_rank(slow, args, None)


# --- candidate_accepted ------------------------------------------------------


def test_candidate_accepted_auto_with_verifier_failure_rejects(wil_module, make_args):
    """REGRESSION: with acceptance=auto, success_command set, no metric, the
    policy resolves to ``verifier`` — a verifier-failing candidate must NOT be
    accepted. (Old behavior incorrectly accepted under this configuration.)"""

    args = make_args(
        acceptance="auto",
        success_command="false",
        metric_name="",
        sandbox="none",
        candidates=1,
    )
    candidate = {"verifier_exit_code": 1, "patch_chars": 12, "metric_value": None}
    assert wil_module.candidate_accepted(candidate, args, state={}, baseline_hash="") is False


def test_candidate_accepted_auto_with_verifier_success_accepts(wil_module, make_args):
    args = make_args(acceptance="auto", success_command="true", metric_name="")
    candidate = {"verifier_exit_code": 0, "patch_chars": 7, "metric_value": None}
    assert wil_module.candidate_accepted(candidate, args, state={}, baseline_hash="") is True


def test_candidate_accepted_always_policy_returns_true(wil_module, make_args):
    args = make_args(acceptance="always")
    candidate = {"verifier_exit_code": 9, "patch_chars": 0, "metric_value": None}
    assert wil_module.candidate_accepted(candidate, args, state={}, baseline_hash="") is True


def test_candidate_accepted_metric_policy_requires_improvement(wil_module, make_args):
    args = make_args(acceptance="metric", metric_name="score", metric_direction="higher")
    worse = {"verifier_exit_code": 0, "metric_value": 1.0}
    better = {"verifier_exit_code": 0, "metric_value": 11.0}
    state = {"best_metric": 10.0}
    assert wil_module.candidate_accepted(worse, args, state=state, baseline_hash="") is False
    assert wil_module.candidate_accepted(better, args, state=state, baseline_hash="") is True


def test_candidate_accepted_auto_without_metric_or_verifier_is_always(wil_module, make_args):
    # No metric_name AND no success_command means the auto policy degrades to
    # "always" — every candidate is accepted.
    args = make_args(acceptance="auto", metric_name="", success_command="")
    candidate = {"verifier_exit_code": None, "metric_value": None, "patch_chars": 0}
    assert wil_module.candidate_accepted(candidate, args, state={}, baseline_hash="") is True


# --- metric_improved ---------------------------------------------------------


def test_metric_improved_higher_direction(wil_module):
    assert wil_module.metric_improved(10.0, 5.0, "higher") is True
    assert wil_module.metric_improved(5.0, 10.0, "higher") is False
    assert wil_module.metric_improved(5.0, 5.0, "higher") is False


def test_metric_improved_lower_direction(wil_module):
    assert wil_module.metric_improved(5.0, 10.0, "lower") is True
    assert wil_module.metric_improved(10.0, 5.0, "lower") is False


def test_metric_improved_none_handling(wil_module):
    # ``new=None`` can never be an improvement, regardless of direction.
    assert wil_module.metric_improved(None, 5.0, "higher") is False
    assert wil_module.metric_improved(None, None, "higher") is False
    # First-ever metric: any value beats ``None``.
    assert wil_module.metric_improved(1.0, None, "higher") is True
    assert wil_module.metric_improved(-1.0, None, "lower") is True


# --- promise_was_met / extract_tagged ---------------------------------------


def test_extract_tagged_normalizes_whitespace_in_tag_body(wil_module):
    # Per the task spec: the extracted text is whitespace-collapsed.
    body = "foo <p>  hello   world  </p> bar"
    assert wil_module.extract_tagged(body, "p") == "hello world"


def test_extract_tagged_returns_none_when_tag_absent(wil_module):
    assert wil_module.extract_tagged("no tags here", "promise") is None


def test_promise_was_met_round_trips_through_extract_tagged(wil_module):
    output = "preamble <promise>  DONE\n</promise> trailer"
    assert wil_module.promise_was_met(output, "DONE") is True


def test_promise_was_met_returns_false_when_promise_differs(wil_module):
    assert wil_module.promise_was_met("<promise>NOT YET</promise>", "DONE") is False


def test_promise_was_met_returns_false_when_promise_unset(wil_module):
    assert wil_module.promise_was_met("<promise>DONE</promise>", None) is False
    assert wil_module.promise_was_met("<promise>DONE</promise>", "") is False


# --- deterministic_summary_update --------------------------------------------


def test_deterministic_summary_update_appends_fields(wil_module):
    previous = ""
    round_info = {
        "iteration": 3,
        "timestamp": "2025-01-01T00:00:00Z",
        "agent_exit_code": 0,
        "agent_timeout": False,
        "accepted_candidate": 1,
        "candidate_count": 1,
        "verifier_exit_code": 0,
        "metric_name": "score",
        "metric_value": 42.0,
        "stuck_reason": "",
        "stagnant_iterations": 0,
        "status_lines": ["M foo.txt"],
        "output_tail": "agent did something useful",
        "verifier_output_tail": "all good",
    }
    summary = wil_module.deterministic_summary_update(previous, round_info, max_chars=4000)
    assert "## Iteration 3 — 2025-01-01T00:00:00Z" in summary
    assert "agent_exit: 0" in summary
    assert "metric: score=42.0" in summary
    assert "M foo.txt" in summary
    assert "agent did something useful" in summary
    assert "all good" in summary


def test_deterministic_summary_update_adds_stagnation_lesson(wil_module):
    previous = "# previous\n"
    round_info = {
        "iteration": 1,
        "timestamp": "T",
        "stagnant_iterations": 2,
        "verifier_exit_code": 1,
        "stuck_reason": "no_workspace_progress",
    }
    summary = wil_module.deterministic_summary_update(previous, round_info, max_chars=4000)
    assert "did not change user-visible workspace state" in summary
    assert "verifier is still failing" in summary
    assert "classified failure mode is `no_workspace_progress`" in summary


def test_deterministic_summary_update_bounded_by_max_chars(wil_module):
    """compact_text trims at ~max_chars + a fixed marker overhead (~30 chars).
    The point of the bound is to keep the summary from growing unbounded
    across iterations — exact size is allowed to overshoot by the trim marker."""

    previous = "x" * 10_000
    round_info = {"iteration": 1, "timestamp": "T"}
    summary = wil_module.deterministic_summary_update(previous, round_info, max_chars=2000)
    # Without the bound, summary length would be 10_000 + entry chars; we
    # allow a small overshoot for the "[middle trimmed]" marker compact_text
    # injects when it cuts the body in half.
    assert len(summary) <= 2100
    assert "[middle trimmed]" in summary


# --- compact_text ------------------------------------------------------------


def test_compact_text_short_passes_through(wil_module):
    short = "hello world"
    assert wil_module.compact_text(short, max_chars=100) == short


def test_compact_text_collapses_excess_blank_lines(wil_module):
    text = "a\n\n\n\nb"
    # Three-plus blank lines collapse to a single blank-line separator.
    assert wil_module.compact_text(text, max_chars=100) == "a\n\nb"


def test_compact_text_long_text_middle_trimmed(wil_module):
    long = "A" * 500 + "MIDDLE" + "B" * 500
    out = wil_module.compact_text(long, max_chars=100)
    assert "[middle trimmed]" in out
    # The first/last fragments survive; the middle marker does not.
    assert "MIDDLE" not in out
    assert out.startswith("A")
    assert out.endswith("B")


# --- variant_for -------------------------------------------------------------


def test_variant_for_returns_stall_breaker_when_stagnant(wil_module):
    last = wil_module.DEFAULT_VARIANTS[-1]
    assert wil_module.variant_for(iteration=1, stagnant=1) == last
    assert wil_module.variant_for(iteration=99, stagnant=5) == last


def test_variant_for_rotates_by_iteration_index(wil_module):
    variants = wil_module.DEFAULT_VARIANTS
    for iteration in range(1, len(variants) + 1):
        expected = variants[(iteration - 1) % len(variants)]
        assert wil_module.variant_for(iteration=iteration, stagnant=0) == expected
    # And it cycles back round.
    assert wil_module.variant_for(iteration=len(variants) + 1, stagnant=0) == variants[0]


# --- select_agent_command ----------------------------------------------------


def test_select_agent_command_batch_rotation_single_candidate(wil_module, make_args):
    args = make_args(agent_command=["cmd0", "cmd1"], agent_switch_every=2)
    # Iterations 1 and 2 → command 0, iterations 3 and 4 → command 1.
    assert wil_module.select_agent_command(args, iteration=1) == ("cmd0", 0)
    assert wil_module.select_agent_command(args, iteration=2) == ("cmd0", 0)
    assert wil_module.select_agent_command(args, iteration=3) == ("cmd1", 1)
    assert wil_module.select_agent_command(args, iteration=4) == ("cmd1", 1)


def test_select_agent_command_candidates_stagger(wil_module, make_args):
    args = make_args(agent_command=["cmd0", "cmd1"], agent_switch_every=2)
    # Candidate fan-out staggers the chosen command so best-of-N compares styles.
    assert wil_module.select_agent_command(args, iteration=1, candidate=1) == ("cmd0", 0)
    assert wil_module.select_agent_command(args, iteration=1, candidate=2) == ("cmd1", 1)
    assert wil_module.select_agent_command(args, iteration=3, candidate=1) == ("cmd1", 1)
    assert wil_module.select_agent_command(args, iteration=3, candidate=2) == ("cmd0", 0)


# --- parse_args preset resolution --------------------------------------------


@pytest.fixture
def in_nongit_dir(tmp_path, monkeypatch):
    """Run parse_args from a non-git dir so --sandbox stays 'none' (no auto-upgrade noise)."""

    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "preset, expected",
    [
        # Regression: these fields have non-sentinel argparse defaults, so the
        # old `getattr(args, key) in (None, "", 0, "reflective")` merge silently
        # dropped them. They must now reflect the preset.
        ("explore", {"agent_switch_every": 2, "candidates": 2, "mode": "variants", "critic_every": 2}),
        ("cheap", {"agent_switch_every": 6, "summary_max_chars": 4000, "mode": "reflective"}),
        ("review-heavy", {"agent_switch_every": 3, "critic_every": 3, "mode": "variants"}),
        ("coding", {"agent_switch_every": 4, "critic_every": 4, "mode": "variants"}),
    ],
)
def test_parse_args_preset_applies_non_sentinel_defaults(wil_module, in_nongit_dir, preset, expected):
    args = wil_module.parse_args(["--preset", preset, "do the thing"])
    for key, value in expected.items():
        assert getattr(args, key) == value, f"{preset}.{key}"


def test_parse_args_explicit_flag_overrides_preset(wil_module, in_nongit_dir):
    args = wil_module.parse_args(
        ["--preset", "explore", "--candidates", "5", "--agent-switch-every", "9", "task"]
    )
    assert args.candidates == 5
    assert args.agent_switch_every == 9


def test_explicitly_passed_distinguishes_user_values_from_defaults(wil_module):
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--alpha", type=int, default=4)
    p.add_argument("--beta", type=int, default=1)
    passed = wil_module.explicitly_passed(p, ["--alpha", "4"])
    assert "alpha" in passed
    assert "beta" not in passed
    # The parser's real defaults must be restored after the probe.
    assert p.parse_args([]).alpha == 4
