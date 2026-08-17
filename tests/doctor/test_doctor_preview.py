"""Tests for ``syncade --doctor``'s run-plan + cost preview (PR-v2-12 check 7, F1/F3).

- **C1 (same plan):** the base doctor resolves and the actor set it lists are what a real
  run for the same flags would diff / dispatch.
- **C4 / F1' / F3 (forward cost):** the cost row is a FORWARD estimate for the planned run,
  scaled by the round budget, built from REVIEWERS + JUDGE cost per round (they run every
  round). The producer is NOT folded into the per-round figure (it runs only on NO-SHIP
  rounds); its extra cost is noted separately. Runs whose reviewer/judge cost is priced from
  INCOMPLETE token data are excluded (never a falsely-precise number). No history -> a coarse
  list-price fallback. C2 — the in-memory backfill never writes the on-disk ``metrics.db``.

Shares the ``healthy_env`` fixture (``conftest.py``) and helpers (``_helpers.py``) with
``test_doctor.py``. The preview checks live in ``syncade.doctor_preview``, so its metrics
reads are patched there.
"""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from syncade import doctor, doctor_preview
from syncade.config import SyncadeConfig
from syncade.doctor_preview import check_plan
from syncade.metrics.schema import ActorStatRow, RunRow
from syncade.pricing_config import PricingConfig
from tests.doctor._helpers import _repo_no_default


class TestPlanCheck:
    """C1 — the plan doctor previews (base, diff, actor set, rounds) is what a real run for
    the same flags would execute."""

    def test_no_base_is_full_head(self, healthy_env):
        plan = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "plan"
        )
        assert plan.status == "ok"
        assert "full HEAD" in plan.detail

    def test_explicit_base_reports_diff_size(self, healthy_env):
        plan = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, base_ref="HEAD")
            if c.name == "plan"
        )
        assert plan.status == "ok"
        # Assert the INTENT (a sized diff against a named base), not the exact
        # phrasing: PR-h-02c item 4 changed "diff vs <ref>" to "diff from <oid>"
        # because the ref and the OID diverge under three-dot semantics.
        assert "file(s)" in plan.detail and "changed line(s)" in plan.detail

    def test_bad_base_is_red(self, healthy_env):
        plan = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, base_ref="no-such-ref-xyz")
            if c.name == "plan"
        )
        assert plan.status == "red"
        assert plan.fix

    def test_unresolvable_scope_is_red(self, tmp_path):
        # Isolated at the unit level: a no-default repo makes scope resolution fail. (In a
        # full run the branch guard would also refuse such a repo, so this targets the plan.)
        repo = _repo_no_default(tmp_path / "solo")
        plan = doctor_preview.check_plan(
            repo, SyncadeConfig(), base_ref=None, scope="everything", max_rounds=3
        )
        assert plan.status == "red"
        assert plan.fix and "--base" in plan.fix

    def test_actor_set_shows_producer_on_noship_in_loop(self, healthy_env):
        plan = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "plan"
        )
        assert "non-final NO-SHIP" in plan.detail  # max_rounds=3 (>1) -> committing

    def test_single_pass_omits_producer(self, healthy_env):
        plan = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=1)
            if c.name == "plan"
        )
        assert "non-final NO-SHIP" not in plan.detail
        assert "reviewer(s) + judge" in plan.detail

    def test_empty_base_string_is_red(self, healthy_env):
        # An explicitly-passed `--base ""` is treated as no-base by take_snapshot, so
        # doctor used to print "diff vs : 0 file(s)" as a plausible but wrong green plan.
        # An empty string is always an accidental value; doctor must reject it as red.
        plan = doctor_preview.check_plan(
            healthy_env, SyncadeConfig(), base_ref="", scope=None, max_rounds=1
        )
        assert plan.status == "red"
        assert "empty" in plan.detail
        assert plan.fix

    def test_no_base_snapshot_error_is_red_not_false_green(self, healthy_env, monkeypatch):
        # Finding 1: a corrupt git index fails take_snapshot. Without a --base, the old code
        # skipped snapshot entirely, producing false-green while the real run exits 60.
        from syncade.snapshot import SnapshotError

        def _raise(*a, **k):
            raise SnapshotError("corrupt index")

        monkeypatch.setattr(doctor_preview, "take_snapshot", _raise)
        plan = doctor_preview.check_plan(
            healthy_env, SyncadeConfig(), base_ref=None, scope=None, max_rounds=1
        )
        assert plan.status == "red"
        assert "corrupt index" in plan.detail or "cannot snapshot" in plan.detail


class TestCostCheck:
    """C4 / F1' — a FORWARD estimate for the planned run: reviewers + judge per-round scaled
    by the round budget, producer NOT folded in, incomplete-cost runs excluded, coarse
    fallback with no history, unpriced models named. C2 — writes no metrics.db."""

    def _history(self, monkeypatch, per_round, *, producer_cost=0.0, incomplete=False):
        # Fake priced history whose per-round (role, model) roster EXACTLY matches the default
        # config (2 reviewers + judge, all their real models) so the identity-match counts it.
        # Each run ran 2 rounds; cost splits equally across the per-round actors so the summed
        # per-run cost = per_round*2 and the per-round estimate = per_round. `incomplete` marks
        # the first reviewer as priced from incomplete tokens, which excludes the run. An
        # optional producer row (large) proves producers are NOT folded in.
        cfg = SyncadeConfig()
        reviewers = list(cfg.reviewers)
        n_actors = len(reviewers) + 1  # reviewers + judge
        cost_each = per_round * 2 / n_actors
        runs = [RunRow(run_id=f"r{i}", rounds_executed=2) for i in range(3)]
        actors = []
        for i in range(3):
            for j, reviewer in enumerate(reviewers):
                actors.append(
                    ActorStatRow(
                        run_id=f"r{i}",
                        role="reviewer",
                        name=reviewer.name,
                        model=reviewer.model,
                        cost_usd=cost_each,
                        cost_incomplete_tokens=5 if (incomplete and j == 0) else 0,
                    )
                )
            actors.append(
                ActorStatRow(
                    run_id=f"r{i}",
                    role="synthesizer",
                    name="judge",
                    model=cfg.synthesizer.model,
                    cost_usd=cost_each,
                )
            )
            if producer_cost:
                actors.append(
                    ActorStatRow(
                        run_id=f"r{i}", role="producer", name="producer", cost_usd=producer_cost
                    )
                )
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: runs)
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: actors)

    def _empty(self, monkeypatch):
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: [])
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: [])

    def test_forward_estimate_from_history(self, healthy_env, monkeypatch):
        self._history(monkeypatch, per_round=3.0)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert cost.status == "ok"
        assert "$3.00/round" in cost.detail
        assert "$9.00" in cost.detail  # 3.0/round x 3 rounds
        assert "reviewers + judge" in cost.detail
        assert "API-equivalent" in cost.detail

    def test_estimate_scales_with_rounds(self, healthy_env, monkeypatch):
        # THE original finding: the estimate must differ for max_rounds=1 vs 3.
        self._history(monkeypatch, per_round=2.0)
        one = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=1)
            if c.name == "cost"
        )
        three = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert "$2.00 for up to 1 round" in one.detail
        assert "$6.00 for up to 3 round" in three.detail
        assert one.detail != three.detail

    def test_producer_cost_is_not_folded_into_per_round(self, healthy_env, monkeypatch):
        # F1' core: a $100 producer cost must NOT inflate the reviewers+judge per-round figure.
        self._history(monkeypatch, per_round=2.0, producer_cost=100.0)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert "$2.00/round" in cost.detail  # producer excluded from the per-round number
        assert "producer adds more" in cost.detail  # ...but noted separately

    def test_single_pass_omits_the_producer_note(self, healthy_env, monkeypatch):
        self._history(monkeypatch, per_round=2.0, producer_cost=100.0)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=1)
            if c.name == "cost"
        )
        assert "producer adds more" not in cost.detail  # single-pass runs no producer

    def test_incomplete_cost_runs_are_excluded(self, healthy_env, monkeypatch):
        # F1' (b): a run priced from incomplete tokens must not become a "precise" per-round.
        self._history(monkeypatch, per_round=2.0, incomplete=True)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        # No clean history remains -> coarse fallback rather than a false-precise $2.00/round.
        assert "VERY ROUGH" in cost.detail

    def test_no_history_emits_range_not_precise_amount(self, healthy_env, monkeypatch):
        # Regression: the no-history path was formatting a single cents-precise amount
        # (e.g. "~$5.85 for up to 3 round(s)") which implies false precision when there is
        # no data behind it. The fix emits a coarse dollar range instead (e.g. "$3–$18").
        import re

        self._empty(monkeypatch)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail
        assert not re.search(r"\$\d+\.\d{2}", cost.detail), (
            f"no-history cost must not contain a cents-precise amount: {cost.detail!r}"
        )
        assert re.search(r"\$\d+[–-]\$\d+", cost.detail), (
            f"no-history cost must show a dollar range: {cost.detail!r}"
        )

    def test_no_history_falls_back_to_coarse_estimate(self, healthy_env, monkeypatch):
        self._empty(monkeypatch)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert cost.status == "ok"
        assert "VERY ROUGH" in cost.detail
        assert "$" in cost.detail

    def test_coarse_estimate_also_scales_with_rounds(self, healthy_env, monkeypatch):
        self._empty(monkeypatch)
        one = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=1)
            if c.name == "cost"
        )
        three = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert one.detail != three.detail

    def test_unpriced_model_blocks_the_coarse_estimate_and_is_named(self, healthy_env, monkeypatch):
        self._empty(monkeypatch)
        cfg = SyncadeConfig(pricing=PricingConfig(models={}))  # nothing priced
        cost = next(
            c for c in doctor.collect_checks(cfg, healthy_env, max_rounds=1) if c.name == "cost"
        )
        assert "cannot estimate" in cost.detail
        assert "unpriced model" in cost.detail
        assert any(r.model in cost.detail for r in cfg.reviewers)

    def test_metrics_unavailable_is_green_not_a_traceback(self, healthy_env, monkeypatch):
        def _boom(*a, **k):
            raise sqlite3.Error("database is locked")

        monkeypatch.setattr(doctor_preview, "backfill", _boom)
        cost = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "cost"
        )
        assert cost.status == "ok"
        assert "unavailable" in cost.detail

    def test_cost_writes_no_metrics_db(self, healthy_env):
        # C2 inertness: the in-memory backfill must never create the on-disk metrics.db.
        doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert not (healthy_env / ".syncade" / "metrics.db").exists()

    def test_unknown_cost_actor_marks_run_incomplete(self, healthy_env, monkeypatch):
        # Finding 0b: an actor with cost_usd=None was silently skipped before its
        # incompleteness could mark the run. The run must be excluded, not used.
        runs = [RunRow(run_id="r0", rounds_executed=2)]
        actors = [
            ActorStatRow(run_id="r0", role="reviewer", name="rev1", cost_usd=2.0),
            ActorStatRow(run_id="r0", role="reviewer", name="rev2", cost_usd=None),
            ActorStatRow(run_id="r0", role="synthesizer", name="judge", cost_usd=1.0),
        ]
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: runs)
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: actors)
        cost = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail  # no clean history -> coarse fallback

    def test_partial_actor_coverage_excludes_run(self, healthy_env, monkeypatch):
        # Finding 0a: a run with fewer per-round actors than expected must be excluded.
        # Previously, a single reviewer row was accepted as a complete 2-reviewer+judge run.
        runs = [RunRow(run_id="r0", rounds_executed=2)]
        actors = [
            ActorStatRow(run_id="r0", role="reviewer", name="rev1", cost_usd=6.0),
            # Missing rev2 and synthesizer — partial coverage for a 3-actor plan.
        ]
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: runs)
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: actors)
        cost = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail  # partial history excluded -> coarse fallback

    def test_reviewer_only_history_excluded_missing_synthesizer(self, healthy_env, monkeypatch):
        # Three priced reviewer rows satisfy actor_count >= expected_actors for a 3-actor plan,
        # but the judge cost is completely absent. The run must be excluded (coarse fallback),
        # not used as if the judge cost were zero.
        runs = [RunRow(run_id="r0", rounds_executed=2)]
        actors = [
            ActorStatRow(run_id="r0", role="reviewer", name="rev1", cost_usd=2.0),
            ActorStatRow(run_id="r0", role="reviewer", name="rev2", cost_usd=2.0),
            ActorStatRow(run_id="r0", role="reviewer", name="rev3", cost_usd=2.0),
            # No synthesizer row — judge cost completely absent.
        ]
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: runs)
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: actors)
        cost = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail  # reviewer-only history excluded -> coarse fallback

    def test_unrelated_model_history_is_excluded(self, healthy_env, monkeypatch):
        # Round-2 finding 1: history with the RIGHT SHAPE (2 reviewers + judge) but DIFFERENT
        # models (an old gpt-5.6-sol roster) must NOT be counted toward the current gpt-5.5
        # roster's estimate — identity, not just count. Excluded -> coarse fallback.
        runs = [RunRow(run_id=f"r{i}", rounds_executed=2) for i in range(3)]
        actors = []
        for i in range(3):
            actors.append(
                ActorStatRow(
                    run_id=f"r{i}", role="reviewer", name="a", model="gpt-5.6-sol", cost_usd=2.0
                )
            )
            actors.append(
                ActorStatRow(
                    run_id=f"r{i}", role="reviewer", name="b", model="gpt-5.6-sol", cost_usd=2.0
                )
            )
            actors.append(
                ActorStatRow(
                    run_id=f"r{i}", role="synthesizer", name="j", model="gpt-5.6-sol", cost_usd=2.0
                )
            )
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: runs)
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: actors)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail  # unrelated-model history excluded -> coarse fallback

    def test_incomplete_round_coverage_excludes_run(self, healthy_env, monkeypatch):
        # C4 regression: a 2-round run where the judge appeared in only round 1 has
        # rounds_with_usage=1 < rounds_executed=2, so its cost per round would be
        # understated (judge's round-2 cost = $0 implicitly). Must fall back to coarse.
        runs = [RunRow(run_id="r0", rounds_executed=2)]
        cfg = SyncadeConfig()
        actors = [
            ActorStatRow(
                run_id="r0",
                role="reviewer",
                name="rev1",
                model=cfg.reviewers[0].model,
                cost_usd=2.0,
                rounds_with_usage=2,  # complete
            ),
            ActorStatRow(
                run_id="r0",
                role="reviewer",
                name="rev2",
                model=cfg.reviewers[1].model,
                cost_usd=2.0,
                rounds_with_usage=2,  # complete
            ),
            ActorStatRow(
                run_id="r0",
                role="synthesizer",
                name="judge",
                model=cfg.synthesizer.model,
                cost_usd=1.0,
                rounds_with_usage=1,  # only ran round 1 — incomplete
            ),
        ]
        monkeypatch.setattr(doctor_preview, "backfill", lambda *a, **k: None)
        monkeypatch.setattr(doctor_preview, "fetch_runs", lambda conn: runs)
        monkeypatch.setattr(doctor_preview, "fetch_actor_stats", lambda conn: actors)
        cost = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail  # incomplete coverage excluded -> coarse fallback

    def test_unpriced_producer_named_when_loop_mode(self, healthy_env, monkeypatch):
        # Finding 4: the unpriced check must include the producer when max_rounds > 1.
        self._empty(monkeypatch)
        cfg = SyncadeConfig(pricing=PricingConfig(models={}))  # nothing priced
        cost = next(
            c for c in doctor.collect_checks(cfg, healthy_env, max_rounds=3) if c.name == "cost"
        )
        assert "unpriced model" in cost.detail
        assert cfg.producer.model in cost.detail

    def test_unpriced_producer_named_in_coarse_fallback(self, healthy_env, monkeypatch):
        # C4 regression: when per-round actors (reviewer + judge) ARE priced but the
        # producer model is NOT, the coarse fallback omitted the unpriced-model note,
        # hiding that the producer's spend is unquantified. The note must appear.
        self._empty(monkeypatch)
        from syncade.pricing_config import ModelPrice

        # Price only gpt-5.5 (reviewers + judge); leave the default producer model unpriced.
        cfg = SyncadeConfig(
            pricing=PricingConfig(
                models={"gpt-5.5": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.0)}
            )
        )
        cost = next(
            c for c in doctor.collect_checks(cfg, healthy_env, max_rounds=3) if c.name == "cost"
        )
        assert "VERY ROUGH" in cost.detail
        assert "unpriced model" in cost.detail
        assert cfg.producer.model in cost.detail

    def test_cost_text_says_nonfinal_no_ship(self, healthy_env, monkeypatch):
        # Finding 6: the cost note must say "non-final NO-SHIP", not just "NO-SHIP".
        self._history(monkeypatch, per_round=2.0)
        cost = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, max_rounds=3)
            if c.name == "cost"
        )
        assert "non-final NO-SHIP" in cost.detail


class TestPlanLabelNamesTheActualBase:
    """PR-h-02c item 4 (D3): the plan detail must name the OID the diff was actually
    taken against, not the ref the operator typed.

    Under three-dot they differ whenever the branch is BEHIND its base — exactly the
    phantom-deletion case increment B fixes — so `diff vs main` read as the advanced tip
    and understated the preview in the one scenario where it matters most.
    """

    @staticmethod
    def _git(repo, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout

    @pytest.fixture
    def behind_base(self, tmp_path):
        g = self._git
        g(tmp_path, "init", "-q", "-b", "main")
        g(tmp_path, "config", "user.email", "t@t")
        g(tmp_path, "config", "user.name", "t")
        (tmp_path / "s.py").write_text("x = 1\n")
        g(tmp_path, "add", "-A")
        g(tmp_path, "commit", "-qm", "root")
        g(tmp_path, "checkout", "-qb", "feature")
        (tmp_path / "mine.py").write_text("m = 1\n")
        g(tmp_path, "add", "-A")
        g(tmp_path, "commit", "-qm", "mine")
        g(tmp_path, "checkout", "-q", "main")
        (tmp_path / "teammate.py").write_text("t = 1\n")
        g(tmp_path, "add", "-A")
        g(tmp_path, "commit", "-qm", "teammate")
        g(tmp_path, "checkout", "-q", "feature")
        return tmp_path

    def test_three_dot_names_the_branch_point_not_the_ref_tip(self, behind_base):
        tip = self._git(behind_base, "rev-parse", "main").strip()
        point = self._git(behind_base, "merge-base", "main", "HEAD").strip()
        assert tip[:7] != point[:7], "fixture does not diverge"

        detail = check_plan(
            behind_base,
            SyncadeConfig(),
            base_ref="main",
            scope=None,
            two_dot=False,
            max_rounds=None,
        ).detail

        assert point[:7] in detail
        assert tip[:7] not in detail, "presented the advanced tip as the diff base"
        assert "main" in detail, "operator can no longer tell which ref they asked for"

    def test_two_dot_names_the_ref_tip(self, behind_base):
        tip = self._git(behind_base, "rev-parse", "main").strip()
        detail = check_plan(
            behind_base,
            SyncadeConfig(),
            base_ref="main",
            scope=None,
            two_dot=True,
            max_rounds=None,
        ).detail
        assert tip[:7] in detail


class TestBudgetCheck:
    """check_budget names the active token/cost ceiling so --doctor tells the operator about
    the default stop condition (PR-h-field-06 acceptance criterion)."""

    def test_default_config_shows_50m_ceiling(self):
        check = doctor_preview.check_budget(SyncadeConfig())
        assert check.status == "ok"
        assert "50,000,000" in check.detail
        assert "budget_tokens = 0" in check.detail, "must name the opt-out"

    def test_opt_out_zero_reports_no_ceiling(self):
        config = SyncadeConfig(loop={"budget_tokens": 0})
        check = doctor_preview.check_budget(config)
        assert check.status == "ok"
        assert "no token or cost ceiling" in check.detail

    def test_custom_ceiling_is_shown(self):
        config = SyncadeConfig(loop={"budget_tokens": 10_000_000})
        assert "10,000,000" in doctor_preview.check_budget(config).detail

    def test_dollar_ceiling_is_shown(self):
        config = SyncadeConfig(loop={"budget_tokens": 0, "budget_usd": 20.0})
        detail = doctor_preview.check_budget(config).detail
        assert "20.00" in detail
        assert "not billed money" in detail
