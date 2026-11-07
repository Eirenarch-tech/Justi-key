"""Sprint 1b/1c acceptance: sampled-pilot extractor and analog monitor model.

One test class per row of the Sprint 1b-1c acceptance table. Thresholds are
not universal: steps are chosen well inside / outside the envelope that each
capture reports for itself (step_detection_floor_worst_case_rad)."""
import math
import os
import tempfile
import unittest
from dataclasses import replace

from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.monitors import (MonitorParams, extract_analog_monitor, extract_sampled_pilot,
                                         window_state)
from caelarch_rf.gate04.observability import CONFIGS, SENSORS, leakage_breaks
from caelarch_rf.gate04.states import PublicState as PS
from caelarch_rf.sim import generator as G
from caelarch_rf.sim.disturbances import Disturbance
from caelarch_rf.sim.generator import AcqFault, Scenario, generate, simulate
from caelarch_rf.sim.leakage import pilot_into_scene, scene_into_analog, scene_into_pilot
from caelarch_rf.sim.matched import emittable_circuits, matched_pair

SEED = "a11ce" * 12 + "0000"
DUR = 12_000_000
T_STEP = 6_030_000          # deliberately not on a block boundary
C2, C6, C6S = "C2_sampled_pilot", "C6_pilot_analog", "C6s_pilot_analog_srcref"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.k = 0

    def tearDown(self):
        self._tmp.cleanup()

    def cap(self, sc, seed=SEED):
        self.k += 1
        d = os.path.join(self._tmp.name, f"c{self.k}")
        generate(sc, seed, d, os.path.join(self._tmp.name, f"t{self.k}.json"))
        return load_capture(d)

    def sc(self, *ds, config=C2, faults=(), **kw):
        return Scenario(disturbances=tuple(ds), faults=tuple(faults), config=config, duration_ns=DUR, **kw)

    @staticmethod
    def onset_dev(sc, t_phys, seed=SEED):
        return simulate(sc, seed).clock.dev(t_phys)


class TestReceiverStep(Base):
    def test_detected_within_reported_envelope_and_latency(self):
        for target in ("G", "A"):
            sc = self.sc(Disturbance(target, "step", T_STEP, 0.5))
            r = extract_sampled_pilot(self.cap(sc))
            floor = r.envelope["step_detection_floor_worst_case_rad"]
            self.assertLess(floor, 0.5, "step must be inside this capture's envelope")
            onset = self.onset_dev(sc, T_STEP)
            hits = [e for e in r.excursions if e.available_ns >= onset]
            with self.subTest(target=target):
                self.assertTrue(hits)
                self.assertLessEqual(hits[0].available_ns - onset, r.envelope["detection_latency_max_ns"])
                self.assertFalse([e for e in r.excursions if e.available_ns < onset])  # causal

    def test_below_envelope_step_is_missed_not_invented(self):
        r = extract_sampled_pilot(self.cap(self.sc(Disturbance("G", "step", T_STEP, 0.05))))
        self.assertGreater(r.envelope["detection_floor_rad"], 0.05)
        self.assertEqual(r.excursions, [])

    def test_baseline_has_no_excursion_and_availability_includes_latency(self):
        p = MonitorParams()
        r = extract_sampled_pilot(self.cap(self.sc()), params=p)
        self.assertEqual(r.excursions, [])
        for o in r.observations:
            self.assertEqual(o.available_ns, o.window_end_ns + p.latency_ns)
        # Skip the acquisition/warm-up head: only TRACKING coverage can be CONDITIONAL.
        mon = [c for c in r.coverage if c.status == "MONITORED"]
        self.assertEqual(window_state(r, mon[0].start_ns, mon[-1].end_ns, mon[-1].end_ns + 10 ** 7)[0],
                         PS.CONDITIONAL)


class TestCausalDecisions(Base):
    """Review P1: evidence counts only once available; readiness is explicit."""

    def test_excursion_not_used_before_it_is_available(self):
        r = extract_sampled_pilot(self.cap(self.sc(Disturbance("G", "step", T_STEP, 0.5))))
        e = r.excursions[0]
        start = e.window_start_ns - 1_000_000
        early = window_state(r, start, e.window_end_ns, e.window_end_ns)       # measured, not yet available
        late = window_state(r, start, e.window_end_ns, e.available_ns)
        self.assertEqual(early[0], PS.UNRESOLVED)
        self.assertNotIn("excursion", early[1])
        self.assertIn("not fully covered", early[1])
        self.assertEqual(late[0], PS.UNRESOLVED)
        self.assertIn("excursion", late[1])
        self.assertEqual(window_state(r, start, e.window_end_ns, e.window_end_ns - 1)[0], PS.UNRESOLVED)

    def test_retune_forces_requalification_before_conditional(self):
        cap = self.cap(self.sc(faults=[AcqFault("retune", at_ns=6_000_000)]))
        r = extract_sampled_pilot(cap)
        gap = [o for o in r.observations if o.reason == "SETTLING_RETUNE"]
        after = [o for o in r.observations if o.window_start_ns > gap[-1].window_end_ns]
        self.assertEqual(after[0].monitor_state, "ACQUIRING")
        self.assertEqual(after[0].freq_offset_hz, 0.0)            # learned frequency discarded
        first_tracking = next(i for i, o in enumerate(after) if o.monitor_state == "TRACKING")
        need = MonitorParams().acquisition_innovations + MonitorParams().warmup_innovations
        self.assertGreaterEqual(first_tracking, need)
        # Samples have resumed, but the monitor is not yet at stated sensitivity.
        w0, w1 = after[1].window_start_ns, after[first_tracking - 1].window_end_ns
        st, why = window_state(r, w0, w1, w1 + 10 ** 7)
        self.assertEqual(st, PS.UNRESOLVED)
        self.assertIn("not at stated sensitivity", why)
        for o in r.observations:
            self.assertEqual(o.detection_floor_rad is not None, o.monitor_state == "TRACKING")

    def test_benign_known_overflow_costs_one_block_not_a_requalification(self):
        r = extract_sampled_pilot(self.cap(self.sc(faults=[AcqFault("overflow_known", at_ns=6_000_000)])))
        gap = max(i for i, o in enumerate(r.observations) if o.reason == "OVERFLOW_BOUNDARY")
        self.assertEqual(r.observations[gap + 1].monitor_state, "REACQUIRING")
        self.assertEqual(r.observations[gap + 2].monitor_state, "TRACKING")

    def test_online_floor_is_causal_not_the_retrospective_envelope(self):
        r = extract_sampled_pilot(self.cap(self.sc()))
        floors = [o.detection_floor_rad for o in r.observations if o.detection_floor_rad]
        self.assertTrue(r.envelope["retrospective"])
        self.assertGreater(len(set(round(f, 6) for f in floors)), 10)   # evolves with time
        self.assertEqual(floors[-1], r.envelope["detection_floor_rad"])  # summary = LAST causal value


class TestReferenceBracketBaseline(Base):
    def test_sees_endpoint_changes_but_not_returning_pulses(self):
        from caelarch_rf.gate04.monitors import extract_reference_bracket
        base = self.sc(config=C6)
        r = extract_reference_bracket(self.cap(replace(base, disturbances=(
            Disturbance("R", "step", T_STEP, 0.6),))))
        self.assertTrue(r.excursions)
        sim = simulate(base, SEED)
        dwells = [(s["t"], n["t"]) for s, n in zip(sim.plan["switches"], sim.plan["switches"][1:])
                  if s["port"] == "R"]
        gap_mid = (dwells[0][1] + dwells[1][0]) // 2       # between the first two R dwells
        p = extract_reference_bracket(self.cap(replace(base, disturbances=(
            Disturbance("G", "pulse", gap_mid, 1.0, 200_000),))))
        self.assertEqual(p.excursions, [])
        st, why = window_state(p, p.coverage[0].start_ns, p.coverage[0].end_ns, p.coverage[0].end_ns + 10 ** 7)
        self.assertEqual(st, PS.UNRESOLVED)
        self.assertIn("bracket-only", why)


class TestFalseAlarms(Base):
    def test_no_excursions_on_baseline_captures(self):
        blocks = 0
        for i in range(6):
            config = (C2, C6, C6S)[i % 3]
            cap = self.cap(Scenario(config=config, duration_ns=DUR), seed=f"{0xfa15e + i:064x}")
            rs = [extract_sampled_pilot(cap)] + ([extract_analog_monitor(cap)] if config != C2 else [])
            for r in rs:
                with self.subTest(i=i, sensor=r.sensor):
                    self.assertEqual(r.excursions, [])
                blocks += len(r.usable())
        self.assertGreater(blocks, 1000)


class TestNoFalseReceiverAttribution(Base):
    def test_source_and_injection_changes_are_unattributed(self):
        for target in ("J", "S"):
            r = extract_sampled_pilot(self.cap(self.sc(Disturbance(target, "step", T_STEP, 0.5))))
            with self.subTest(target=target):
                self.assertTrue(r.excursions)
                for e in r.excursions:
                    self.assertEqual(e.attribution, "UNATTRIBUTED")
                    self.assertEqual(set(e.candidates), SENSORS["pilot_sampled"].terms)
                st, why = window_state(r, r.coverage[0].start_ns, r.coverage[-1].end_ns,
                                       r.coverage[-1].end_ns + 10 ** 7)
                self.assertEqual(st, PS.UNRESOLVED)
                self.assertIn("unattributed", why)

    def test_slow_source_drift_is_tracked_not_alarmed(self):
        r = extract_sampled_pilot(self.cap(self.sc(
            Disturbance("S", "random_walk", 1_000_000, 3.0, 10_000_000, seed=99))))
        self.assertEqual(r.excursions, [])

    def test_pilot_frequency_offset_is_compensated(self):
        r = extract_sampled_pilot(self.cap(self.sc()))
        f_true = next(d.value for d in simulate(self.sc(), SEED).background if d.kind == "freq_offset")
        # Background random walks also have local slope, so allow a few tens of Hz.
        self.assertAlmostEqual(r.observations[-1].freq_offset_hz, f_true, delta=40.0)
        self.assertGreater(abs(f_true), 50.0)


class TestMatchedControlsSurviveTheMonitors(Base):
    def _outputs(self, cap, analog):
        rs = [extract_sampled_pilot(cap)] + ([extract_analog_monitor(cap)] if analog else [])
        return [([(e.available_ns, round(e.innovation_rad, 6)) for e in r.excursions],
                 [(o.window_start_ns, o.continuity_id) for o in r.observations]) for r in rs]

    def test_monitor_outputs_identical_for_matched_pairs(self):
        tpl = Disturbance("G", "step", T_STEP, 0.5)
        cases = [(C2, "JS"), (C2, "GA"), (C2, "XJG"), (C6, "GAM"), (C6, {"J", "FA", "M"}), (C6S, "GAM")]
        for config, circ in cases:
            sa, sb = matched_pair(self.sc(config=config), frozenset(circ), tpl)
            ca, cb = self.cap(sa), self.cap(sb)
            with self.subTest(config=config, circuit=sorted(circ)):
                oa, ob = self._outputs(ca, config != C2), self._outputs(cb, config != C2)
                self.assertEqual(oa, ob)
                self.assertTrue(any(ex for ex, _ in oa), "the change must be detected, just not attributed")

    def test_leakage_broken_pairs_still_indistinguishable_to_current_monitors(self):
        """Information exists in the leakage, but the monitors do not use it:
        excursions and continuity are identical (phases agree to ~mrad)."""
        tpl = Disturbance("G", "step", T_STEP, 0.5)
        sensors = CONFIGS[C6][0]
        circs = [c for c in emittable_circuits(C6) if leakage_breaks(sensors, c)]
        self.assertTrue(circs)
        for circ in circs[:3]:
            sa, sb = matched_pair(self.sc(config=C6), circ, tpl)
            ra, rb = extract_analog_monitor(self.cap(sa)), extract_analog_monitor(self.cap(sb))
            with self.subTest(circuit=sorted(circ)):
                self.assertEqual([e.available_ns for e in ra.excursions], [e.available_ns for e in rb.excursions])
                d = [abs((a.phase_rad - b.phase_rad + math.pi) % (2 * math.pi) - math.pi)
                     for a, b in zip(ra.usable(), rb.usable())]
                self.assertLess(max(d), 0.05)


class TestUnknownSampleLoss(Base):
    def test_fails_closed_after_unknown_drop(self):
        cap = self.cap(self.sc(faults=[AcqFault("overflow_unknown", at_ns=7_000_000)]))
        r = extract_sampled_pilot(cap)
        tail = r.coverage[-1]
        self.assertEqual((tail.status, tail.reason, tail.end_ns), ("UNMONITORED", "UNTIMED", None))
        sv = cap.streams["rx0"]
        self.assertTrue(all(o.window_end_ns <= tail.start_ns for o in r.observations))
        self.assertEqual(window_state(r, tail.start_ns - 1_000_000, tail.start_ns + 1_000_000,
                                      tail.start_ns + 10 ** 7)[0], PS.INVALID)
        self.assertLess(len(r.observations) * 100, sv.n + 1)


class TestPilotInterruption(Base):
    def test_unmonitored_interval_reported_and_continuity_broken(self):
        cap = self.cap(self.sc(faults=[AcqFault("pilot_off", at_ns=5_000_000)]))
        r = extract_sampled_pilot(cap)
        off = [c for c in r.unmonitored() if c.reason == "PILOT_OFF"]
        self.assertEqual(len(off), 1)
        before = [o.continuity_id for o in r.observations if o.usable and o.window_end_ns < off[0].start_ns]
        after = [o.continuity_id for o in r.observations if o.usable and o.window_start_ns > off[0].end_ns]
        self.assertTrue(before and after)
        self.assertGreater(min(after), max(before))
        self.assertEqual(window_state(r, off[0].start_ns, off[0].end_ns, off[0].end_ns + 10 ** 7)[0],
                         PS.INVALID)


class TestPhaseWrapping(Base):
    def test_fast_change_is_ambiguous_and_breaks_continuity(self):
        r = extract_sampled_pilot(self.cap(self.sc(Disturbance("G", "ramp", 5_000_000, 25.0, 1_000_000))))
        self.assertTrue(any(e.ambiguous_2pi for e in r.excursions))
        ids = [o.continuity_id for o in r.usable()]
        self.assertGreater(len(set(ids)), 1)
        # Inside any one segment no step exceeds the unwrap limit: nothing was fabricated.
        prev = {}
        for o in r.usable():
            if o.continuity_id in prev:
                self.assertLess(abs(o.innovation_rad), MonitorParams().unwrap_limit_rad + 1e-12)
            prev[o.continuity_id] = o

    def test_slow_large_ramp_is_unwrapped_within_one_segment(self):
        ramp = Disturbance("G", "ramp", 2_000_000, 12.0, 8_000_000)
        with_ramp = extract_sampled_pilot(self.cap(self.sc(ramp))).usable()
        without = extract_sampled_pilot(self.cap(self.sc())).usable()
        self.assertEqual(len({o.continuity_id for o in with_ramp}), 1)
        # Same seed: the difference of unwrapped totals isolates the 12 rad ramp
        # (frequency offset and background are common to both captures).
        total = lambda obs: obs[-1].unwrapped_phase_rad - obs[0].unwrapped_phase_rad
        self.assertEqual([o.window_start_ns for o in with_ramp], [o.window_start_ns for o in without])
        self.assertAlmostEqual(total(with_ramp) - total(without), 12.0, delta=0.3)

    def test_known_gap_restarts_unwrapping(self):
        cap = self.cap(self.sc(faults=[AcqFault("overflow_known", at_ns=6_000_000)]))
        r = extract_sampled_pilot(cap)
        self.assertTrue(any(c.reason == "OVERFLOW_BOUNDARY" for c in r.unmonitored()))
        self.assertGreaterEqual(len({o.continuity_id for o in r.usable()}), 2)


class TestIntersampleGlitch(Base):
    def test_model_dependent_limitation_reported(self):
        cap = self.cap(self.sc(Disturbance("G", "intersample_pulse", T_STEP, 1.0), config=C6))
        r = extract_sampled_pilot(cap)
        self.assertEqual(r.excursions, [])
        self.assertTrue(any("point sampling" in s and "IN THIS MODEL" in s for s in r.limitations))
        a = extract_analog_monitor(cap)
        self.assertEqual(a.excursions, [])   # sub-us pulse attenuated far below the floor
        self.assertTrue(any("bandwidth" in s for s in a.limitations))

    def test_near_floor_pulse_does_not_inflate_the_reported_envelope(self):
        # A 60 us pulse sits at the edge of the analog monitor's envelope (missed
        # here, honestly). Its sub-threshold innovations must not raise the floor
        # the monitor reports afterwards (winsorised variance update).
        base = extract_analog_monitor(self.cap(self.sc(config=C6)))
        hit = extract_analog_monitor(self.cap(self.sc(Disturbance("G", "pulse", T_STEP, 1.0, 60_000),
                                                       config=C6)))
        self.assertLess(hit.envelope["detection_floor_rad"], 1.2 * base.envelope["detection_floor_rad"])

    def test_analog_sees_short_inband_pulse(self):
        # 150 us: several detector time constants, i.e. inside its bandwidth.
        cap = self.cap(self.sc(Disturbance("G", "pulse", T_STEP, 1.0, 150_000), config=C6))
        self.assertTrue(extract_analog_monitor(cap).excursions)


class TestAnalogDetectorDriftM(Base):
    def test_M_moves_only_the_analog_monitor_and_stays_ambiguous(self):
        cap = self.cap(self.sc(Disturbance("M", "step", T_STEP, 0.5), config=C6))
        self.assertEqual(extract_sampled_pilot(cap).excursions, [])
        a = extract_analog_monitor(cap)
        self.assertTrue(a.excursions)
        self.assertIn("M", a.excursions[0].candidates)
        self.assertEqual(a.excursions[0].attribution, "UNATTRIBUTED")

    def test_downstream_sdr_event_invisible_to_analog(self):
        cap = self.cap(self.sc(Disturbance("A", "step", T_STEP, 0.5), config=C6))
        self.assertTrue(extract_sampled_pilot(cap).excursions)
        self.assertEqual(extract_analog_monitor(cap).excursions, [])

    def test_independent_vs_source_referenced(self):
        step = Disturbance("S", "step", T_STEP, 0.5)
        ind = extract_analog_monitor(self.cap(self.sc(step, config=C6)))
        src = extract_analog_monitor(self.cap(self.sc(step, config=C6S)))
        self.assertTrue(ind.excursions)
        self.assertEqual(src.excursions, [])          # S cancels against the source reference
        self.assertGreater(abs(ind.observations[-1].freq_offset_hz), 20)   # sees S frequency offset
        self.assertLess(abs(src.observations[-1].freq_offset_hz), 20)
        j = extract_analog_monitor(self.cap(self.sc(Disturbance("J", "step", T_STEP, 0.5), config=C6S)))
        self.assertTrue(j.excursions)                  # J does not cancel

    def test_logger_dead_time_reported(self):
        a = extract_analog_monitor(self.cap(self.sc(config=C6)))
        self.assertTrue(any(c.reason == "OVERFLOW_BOUNDARY" for c in a.unmonitored()))


class TestReferenceClockJump(Base):
    def test_stale_alignment_invalidated_and_no_continuity_across_jump(self):
        cap = self.cap(self.sc(config=C6, faults=[AcqFault("time_discontinuity", at_ns=6_000_000)]))
        disc = next(e for e in cap.events if e["type"] == "TIME_DISCONTINUITY")
        mon_last = cap.streams["mon0"].time_of(cap.streams["mon0"].n - 1)
        self.assertIsNone(cap.map_time(int(mon_last), "mon0", "sdr0"))
        self.assertEqual(cap.latest_witness(disc["t_ns"] + 2_000_000, "sdr0", 10 ** 15).reason,
                         "ALIGNMENT_VOID_AFTER_DISCONTINUITY")
        r = extract_sampled_pilot(cap)
        before = {o.continuity_id for o in r.usable() if o.window_end_ns < disc["t_ns"]}
        after = {o.continuity_id for o in r.usable() if o.window_start_ns > disc["t_ns"]}
        self.assertTrue(before and after and before.isdisjoint(after))


class TestPilotLeakage(unittest.TestCase):
    def test_interference_quantified_both_ways(self):
        sc = Scenario(duration_ns=10_000_000)
        ps = pilot_into_scene(SEED, sc)
        sp = scene_into_pilot(SEED, sc)
        sa = scene_into_analog(SEED, sc)
        for name, r in (("pilot->scene", ps), ("scene->pilot", sp), ("scene->analog", sa)):
            with self.subTest(direction=name):
                self.assertGreater(r["n"], 50)
                self.assertGreater(r["rms_rad"], 0.0)
        # Steady-state Hann bound holds in RMS; transients (switching) exceed it at the max.
        self.assertLessEqual(ps["rms_rad"], 1.25 * ps["analytic_bound_rad"])
        self.assertLessEqual(sp["rms_rad"], 1.25 * sp["analytic_bound_rad"])
        # Leakage stays far below the detection floor at the default pilot level.
        r = extract_sampled_pilot(load_capture(self._one(sc)))
        self.assertLess(sp["max_rad"], r.envelope["detection_floor_rad"] / 5)

    def _one(self, sc):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = os.path.join(self._tmp.name, "c")
        generate(sc, SEED, d, os.path.join(self._tmp.name, "t.json"))
        return d


if __name__ == "__main__":
    unittest.main()
