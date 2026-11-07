"""Sprint 1d guard and Sprint 1e blind scorer."""
import json
import math
import os
import tempfile
import unittest

from caelarch_rf.gate04.evaluate import capture_record, freeze, run_monitors
from caelarch_rf.gate04.guard import assess
from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.states import PublicState as PS
from caelarch_rf.sim import generator as G
from caelarch_rf.sim.disturbances import Disturbance
from caelarch_rf.sim.generator import AcqFault, Scenario, capture_id_for, generate
from caelarch_rf.sim.matched import matched_pair
from caelarch_rf.sim.scorer import MatchPolicy, cluster_bootstrap, poisson_upper, score, wilson

C6 = "C6_pilot_analog"
DUR = 12_000_000


def _seed(i):
    return f"{0x5c0de + i:064x}"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.caps, self.truth = os.path.join(self.d, "caps"), os.path.join(self.d, "truth")

    def tearDown(self):
        self._tmp.cleanup()

    def gen(self, sc, i):
        seed = _seed(i)
        cid = capture_id_for(bytes.fromhex(seed))
        generate(sc, seed, os.path.join(self.caps, cid), os.path.join(self.truth, cid + ".truth.json"))
        return load_capture(os.path.join(self.caps, cid))

    @staticmethod
    def sc(*ds, faults=(), **kw):
        return Scenario(disturbances=tuple(ds), faults=tuple(faults), config=C6, duration_ns=DUR, **kw)


class TestGuard(Base):
    def _windows(self, cap, active, delay=300_000):
        res = run_monitors(cap)
        t0 = int(cap.streams["rx0"].time_of(0))
        return res, [assess(cap, res, active, t0 + k * 1_000_000, t0 + (k + 1) * 1_000_000,
                            t0 + (k + 1) * 1_000_000 + delay) for k in range(11)]

    def test_quiet_windows_conditional_and_nothing_moved(self):
        """Regression: the CONDITIONAL reason once counted as an excursion."""
        _, ws = self._windows(self.gen(self.sc(), 1), "PILOT_ANALOG")
        cond = [w for w in ws if w.state == PS.CONDITIONAL]
        self.assertTrue(cond)
        for w in ws:
            self.assertEqual(w.detected, bool(w.moved))
            self.assertNotEqual(w.state, PS.VALID_BOUNDED)
            self.assertIsNone(w.unique_attribution)
        self.assertTrue(all(not w.moved for w in cond))

    def test_consistent_causes_narrow_only_when_justified_and_never_attribute(self):
        pilot_terms = {"J", "S", "G", "A", "FG", "FA"}
        narrowed = False
        for i, amp in ((2, 0.5), (6, 1.2)):
            res, ws = self._windows(self.gen(self.sc(Disturbance("A", "step", 7_030_000, amp)), i),
                                    "PILOT_ANALOG")
            hit = [w for w in ws if w.detected]
            self.assertTrue(hit)
            for w in hit:
                cs = set(w.consistent_single_causes)
                self.assertIn("pilot_sampled", w.moved)
                self.assertIn("A", cs)                                # true cause always retained
                self.assertTrue(cs <= pilot_terms)
                self.assertIsNone(w.unique_attribution)
                self.assertEqual(w.state, PS.UNRESOLVED)
                self.assertTrue(w.multi_cause_explanations)
                if cs <= {"A", "FA"}:
                    # Narrowing requires the analog to have been still AND sensitive
                    # enough to have seen the same change under each excluded cause.
                    self.assertIn("analog_monitor_P4", w.still)
                    narrowed = True
        self.assertTrue(narrowed, "a 1.2 rad A step must let the still analog exclude G, J, S, FG")
        self.assertTrue(all(alts for alts in hit[0].requires_for_attribution.values()))

    def test_insensitive_still_sensor_cannot_exclude(self):
        """Non-detection is not zero change: a small step near the analog floor."""
        res, ws = self._windows(self.gen(self.sc(Disturbance("A", "step", 7_030_000, 0.3)), 7), "PILOT_ANALOG")
        for w in ws:
            if w.detected and "analog_monitor_P4" in w.still:
                self.assertIn("G", w.consistent_single_causes)

    def test_reference_only_never_conditional(self):
        _, ws = self._windows(self.gen(self.sc(), 3), "REF")
        self.assertTrue(all(w.state != PS.CONDITIONAL for w in ws))

    def test_decision_before_alarm_availability_does_not_see_it(self):
        cap = self.gen(self.sc(Disturbance("G", "step", 7_030_000, 0.5)), 4)
        res = run_monitors(cap)
        e = res["sampled_digital"].excursions[0]
        w0 = e.window_start_ns - 1_000_000
        early = assess(cap, res, "PILOT", w0, e.window_end_ns, e.available_ns - 1)
        late = assess(cap, res, "PILOT", w0, e.window_end_ns, e.available_ns)
        self.assertFalse(early.detected)
        self.assertNotEqual(early.state, PS.CONDITIONAL)
        self.assertTrue(late.detected)

    def test_clock_jump_blocks_cross_clock_evidence(self):
        cap = self.gen(self.sc(faults=[AcqFault("time_discontinuity", at_ns=5_000_000)]), 5)
        res = run_monitors(cap)
        disc = next(e["t_ns"] for e in cap.events if e["type"] == "TIME_DISCONTINUITY")
        w = assess(cap, res, "ANALOG", disc + 2_000_000, disc + 3_000_000, disc + 3_300_000)
        self.assertEqual(w.state, PS.UNRESOLVED)
        self.assertIn("no valid alignment epoch", w.evidence["analog_quadrature"][1])
        self.assertEqual(w.evidence["witness"][1], "ALIGNMENT_VOID_AFTER_DISCONTINUITY")

    def test_matched_pairs_give_identical_guard_decisions(self):
        for i, circ in enumerate((frozenset("JS"), frozenset("GAM"))):
            sa, sb = matched_pair(self.sc(), circ, Disturbance("G", "step", 7_030_000, 0.5))
            seed = _seed(100 + i)
            recs = []
            for j, sc in enumerate((sa, sb)):
                d = os.path.join(self.d, f"m{i}{j}")
                generate(sc, seed, d, os.path.join(self.d, f"m{i}{j}.truth.json"))
                recs.append(capture_record(load_capture(d)))
            with self.subTest(circuit=sorted(circ)):
                self.assertEqual(recs[0]["guard"], recs[1]["guard"])
                self.assertTrue(any(w["detected"] for w in recs[0]["guard"]["PILOT_ANALOG"]))


class TestStatistics(unittest.TestCase):
    def test_poisson_upper_bounds(self):
        self.assertAlmostEqual(poisson_upper(0), -math.log(0.05), places=4)    # 2.9957
        self.assertAlmostEqual(poisson_upper(1), 4.7439, places=3)
        self.assertAlmostEqual(poisson_upper(5), 10.5130, places=3)

    def test_wilson_and_bootstrap(self):
        lo, hi = wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)
        p = MatchPolicy(bootstrap=500)
        ci = cluster_bootstrap([(1, 2), (2, 2), (0, 2), (2, 2)], p)
        self.assertEqual(ci, cluster_bootstrap([(1, 2), (2, 2), (0, 2), (2, 2)], p))   # deterministic
        self.assertTrue(0 <= ci[0] <= 5 / 8 <= ci[1] <= 1)


class TestBlindScorer(Base):
    def _study(self, scenarios):
        for i, sc in enumerate(scenarios):
            self.gen(sc, 200 + i)
        frozen = os.path.join(self.d, "frozen.json")
        digest = freeze(self.caps, frozen)
        return frozen, digest

    def test_freeze_and_score_are_reproducible(self):
        frozen, digest = self._study([self.sc(Disturbance("G", "step", 7_030_000, 0.5)), self.sc()])
        again = os.path.join(self.d, "frozen2.json")
        self.assertEqual(freeze(self.caps, again), digest)
        p = MatchPolicy(bootstrap=200)
        self.assertEqual(score(frozen, self.truth, digest, p), score(again, self.truth, digest, p))

    def test_refuses_changed_frozen_outputs_before_touching_truth(self):
        frozen, digest = self._study([self.sc()])
        with open(frozen, "a") as f:
            f.write(" ")
        with self.assertRaisesRegex(ValueError, "changed after freezing"):
            score(frozen, os.path.join(self.d, "no-such-truth-dir"), digest)

    def test_categories_matching_and_denominators(self):
        intersample = Disturbance("G", "intersample_pulse", 7_030_000, 1.0)
        scs = [self.sc(Disturbance("G", "step", 7_030_000, 0.5)),
               self.sc(Disturbance("J", "step", 7_030_000, 0.5)),
               self.sc(Disturbance("G", "step", 7_030_000, 0.5), Disturbance("J", "step", 7_030_000, -0.5)),
               self.sc(intersample),
               self.sc(Disturbance("G", "pulse", 7_030_000, 1.0, 400_000)),
               self.sc(Disturbance("G", "step", 7_030_000, 0.02)),
               self.sc()]
        frozen, digest = self._study(scs)
        rep = score(frozen, self.truth, digest, MatchPolicy(bootstrap=200))
        with open(frozen) as f:
            cap_ids = [c["capture_id"] for c in json.load(f)["captures"]]
        order = sorted(range(len(scs)), key=lambda i: capture_id_for(bytes.fromhex(_seed(200 + i))))
        self.assertEqual(cap_ids, [capture_id_for(bytes.fromhex(_seed(200 + i))) for i in order])
        sd, rb = rep["detectors"]["sampled_digital"], rep["detectors"]["reference_bracket"]
        for d in rep["detectors"].values():
            self.assertEqual(sum(d["counts"].values()), d["events"])      # nothing dropped
            self.assertEqual(d["events"], 6)                               # baseline has no event
        cases = sd["by_case"]["custom"]
        self.assertEqual(sd["counts"]["UNOBSERVABLE_MODEL"], 1)          # intersample pulse
        self.assertEqual(sd["counts"]["UNOBSERVABLE_STRUCTURAL"], 1)     # G + J cancellation on the pilot
        self.assertGreaterEqual(sd["counts"]["DETECTED"], 3)             # G step, J step, wide pulse
        self.assertEqual(sd["counts"]["MISSED_BELOW_FLOOR"], 1)          # 0.02 rad step
        self.assertEqual(rb["counts"]["UNOBSERVABLE_STRUCTURAL"], 1)     # J never reaches the reference
        self.assertGreaterEqual(sd["duplicates"], 1)                      # pulse rise + fall: one credit
        self.assertEqual(sum(cases.values()), 6)
        self.assertLessEqual(sd["pd_all_events"]["pd"], sd["pd_observable"]["pd"])
        fa = sd["false_alarms"]
        self.assertEqual(fa["count"], 0)
        self.assertAlmostEqual(fa["upper_bound_per_minute"], poisson_upper(0) / fa["exposure_minutes"])
        g = rep["guard"]["PILOT_ANALOG"]
        self.assertEqual(g["valid_bounded_issued"], 0)
        self.assertEqual(g["unique_attributions_issued"], 0)
        self.assertEqual(g["consistent_set_contains_a_true_cause"], 1.0)
        self.assertEqual(rep["frozen_sha256"], digest)
        self.assertIn("abrupt >= 0.4 rad", g["unsafe_conditional_by_stratum"])

    def test_gradual_drift_is_reported_as_a_blind_spot(self):
        frozen, digest = self._study([self.sc(Disturbance("G", "ramp", 3_000_000, 0.4, 6_000_000))])
        g = score(frozen, self.truth, digest, MatchPolicy(bootstrap=100))["guard"]["PILOT"]
        grad = g["unsafe_conditional_by_stratum"]["gradual (any size)"]
        self.assertGreater(grad["n"], 0)
        self.assertGreater(grad["rate"], 0.5)   # frequency tracking absorbs slow drift: honest blind spot


if __name__ == "__main__":
    unittest.main()
