"""Sprint 1a acceptance: blind generator reproducibility, contract conformance,
timestamp causality, renderer/graph consistency, matched-observation negative
controls per sensor configuration, and blindness beyond import isolation."""
import glob
import inspect
import json
import math
import os
import tempfile
import unittest
from dataclasses import replace

from caelarch_rf.gate04 import contract as C
from caelarch_rf.gate04 import sealed_truth
from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.observability import CONFIGS, ORDER, SENSORS, circuits, leakage_breaks
from caelarch_rf.sim import batch, generator as G
from caelarch_rf.sim.disturbances import Disturbance
from caelarch_rf.sim.generator import AcqFault, Scenario, generate, simulate
from caelarch_rf.sim.matched import emittable_circuits, matched_pair, online_difference, split_vector

SEED = "5eed" * 16           # 256-bit test seed (hex)
SEED2 = "c0de" * 16
SHORT = 6_000_000            # 6 ms captures keep the suite fast


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.k = 0

    def tearDown(self):
        self._tmp.cleanup()

    def gen(self, sc, seed=SEED):
        self.k += 1
        cap = os.path.join(self.d, "caps", f"c{self.k}")
        truth = os.path.join(self.d, "truth", f"c{self.k}.json")
        generate(sc, seed, cap, truth)
        return cap, truth


def _files(cap):
    out = {}
    for p in sorted(glob.glob(os.path.join(cap, "*"))):
        with open(p, "rb") as f:
            out[os.path.basename(p)] = f.read()
    return out


class TestSeedsAndReproducibility(Tmp):
    def test_enumerable_seeds_refused(self):
        for bad in (7, 2 ** 64, "abcd", b"short"):
            with self.subTest(seed=bad), self.assertRaises((TypeError, ValueError)):
                G.normalize_seed(bad)

    def test_same_seed_same_bytes_including_truth(self):
        sc = Scenario(disturbances=(Disturbance("G", "step", 3_000_000, 0.3),),
                      faults=(AcqFault("retune"),), duration_ns=SHORT)
        a, ta = self.gen(sc)
        b, tb = self.gen(sc)
        self.assertEqual(_files(a), _files(b))
        with open(ta, "rb") as f1, open(tb, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        c, _ = self.gen(sc, seed=SEED2)
        self.assertNotEqual(_files(a)["rx0.sc16"], _files(c)["rx0.sc16"])

    def test_refuses_truth_inside_capture_and_overwrite(self):
        sc = Scenario(duration_ns=SHORT)
        cap = os.path.join(self.d, "cap")
        with self.assertRaisesRegex(ValueError, "outside"):
            generate(sc, SEED, cap, os.path.join(cap, "t.json"))
        generate(sc, SEED, cap, os.path.join(self.d, "t.json"))
        with self.assertRaises(FileExistsError):
            generate(sc, SEED, cap, os.path.join(self.d, "t2.json"))


class TestContractAndCausality(Tmp):
    def _fault(self, kind, config="C2_sampled_pilot", dur=20_000_000):
        sc = Scenario(faults=(AcqFault(kind),), config=config, duration_ns=dur)
        cap, truth = self.gen(sc)
        with open(truth) as f:
            t = json.load(f)
        return load_capture(cap), t, simulate(sc, SEED)

    def test_every_fault_and_config_validates(self):
        for config in G.EMITTED:
            for kind in G.FAULT_KINDS:
                pilot, wit, _ = G.EMITTED[config]
                if (kind == "pilot_off" and not pilot) or (kind == "witness_invalid" and not wit):
                    continue
                with self.subTest(config=config, kind=kind):
                    cap, truth, _ = self._fault(kind, config, SHORT)
                    self.assertEqual(sealed_truth.open_for_scoring(cap.manifest, self._last_truth())["capture_id"],
                                     cap.manifest["capture_id"])

    def _last_truth(self):
        return os.path.join(self.d, "truth", f"c{self.k}.json")

    def test_event_ordering_and_settled_after_command(self):
        cap, _, _ = self._fault("retune")
        ts = [e["t_ns"] for e in cap.events]
        self.assertEqual(ts, sorted(ts))
        last_cmd = {}
        for e in cap.events:
            if e["type"] == "SWITCH_COMMAND":
                last_cmd["switch"] = e["t_ns"]
            if e["type"] == "LO_RETUNE":
                last_cmd["retune"] = e["t_ns"]
            if e["type"] == "SETTLED":
                self.assertLess(last_cmd[e["cause"]], e["t_ns"])

    def test_declared_witness_alignment_is_honest(self):
        cap, truth, sim = self._fault("gain_change")
        unc = cap.manifest["timebase_alignments"][0]["uncertainty_ns"]
        for rec, t_phys in zip(cap.witness, sim.plan["witness_t"]):
            mapped, u = cap.map_time(rec["t_ns"], "wit0", "sdr0")
            self.assertLessEqual(abs(mapped - sim.clock.dev(t_phys)), unc)

    def test_switch_events_land_on_physical_switch_samples(self):
        cap, _, sim = self._fault("gain_change")
        sv = cap.streams["rx0"]
        phys = [int(math.ceil(sim.clock.n_of(s["t"]))) for s in sim.plan["switches"]]
        self.assertEqual([i for i, _ in sv.port_changes], phys)

    def test_honest_guard_accepts_no_unsettled_sample_but_short_guard_does(self):
        def accepted_unsettled(cap, sim):
            acc = cap.streams["rx0"].accepted_ranges()
            return sum(1 for a, b in acc for n in range(a, b) if sim.port_phys[n] == "SETTLING")
        cap, _, sim = self._fault("gain_change")
        self.assertEqual(accepted_unsettled(cap, sim), 0)
        cap, truth, sim = self._fault("short_guard")
        self.assertGreater(accepted_unsettled(cap, sim), 0)   # the hazard the scorer must count
        self.assertTrue(any(n["kind"] == "short_guard" for n in truth["generator"]["acquisition_faults"]))

    def test_unknown_drop_untimes_from_true_drop(self):
        cap, truth, _ = self._fault("overflow_unknown")
        note = truth["generator"]["acquisition_faults"][0]
        self.assertEqual(cap.streams["rx0"].untimed_from, note["orig_index"])

    def test_known_drop_keeps_true_timestamps(self):
        cap, truth, sim = self._fault("overflow_known")
        note = truth["generator"]["acquisition_faults"][0]
        i, k = note["orig_index"], note["dropped"]
        sv = cap.streams["rx0"]
        self.assertEqual(sv.n, sim.clock.n0 - k)
        self.assertAlmostEqual(float(sv.time_of(i + 10)), sim.clock.dev_exact(i + k + 10), delta=1)

    def test_ref_unlock_report_latency_leaves_unreported_unlocked_samples(self):
        cap, truth, sim = self._fault("ref_unlock")
        note = truth["generator"]["acquisition_faults"][0]
        sv = cap.streams["rx0"]
        rej = [r for r in sv.rejections if r.reason == "REF_UNLOCKED"]
        self.assertEqual(len(rej), 1)
        n_unlock = math.ceil(sim.clock.n_of(note["t_ns"]))
        self.assertGreater(rej[0].start, n_unlock)            # online learns late
        acc = sv.accepted_ranges()
        self.assertTrue(any(a <= n < b for a, b in acc for n in range(n_unlock, rej[0].start)))

    def test_time_discontinuity_voids_witness_alignment(self):
        cap, truth, sim = self._fault("time_discontinuity")
        note = truth["generator"]["acquisition_faults"][0]
        disc_t = next(e["t_ns"] for e in cap.events if e["type"] == "TIME_DISCONTINUITY")
        self.assertEqual(cap.latest_witness(disc_t - 1, "sdr0", 10 ** 9).reason, "OK")
        after = cap.latest_witness(disc_t + 5_000_000, "sdr0", 10 ** 15)
        self.assertEqual((after.record, after.reason), (None, "ALIGNMENT_VOID_AFTER_DISCONTINUITY"))
        self.assertGreater(note["jump_ns"], cap.manifest["timebase_alignments"][0]["uncertainty_ns"])

    def test_overload_clips_and_retune_and_pilot_gating_visible(self):
        cap, _, _ = self._fault("overload")
        self.assertTrue(any(r.reason == "CLIPPED_OR_NONFINITE" for r in cap.streams["rx0"].rejections))
        cap, _, _ = self._fault("retune_unlocked")
        self.assertEqual(len(cap.streams["rx0"].retune_indices), 2)
        self.assertTrue(any(r.reason == "LO_UNLOCKED" for r in cap.streams["rx0"].rejections))
        cap, _, _ = self._fault("pilot_off")
        self.assertEqual(len(cap.streams["rx0"].pilot_off), 1)
        cap, _, _ = self._fault("witness_invalid")
        self.assertTrue(any(not w["valid"] for w in cap.witness))


class TestRendererMatchesGraph(unittest.TestCase):
    """Each term's unit step moves each emitted sensor by its graph coefficient."""

    def test_unit_steps(self):
        base = Scenario(config="C2_sampled_pilot", duration_ns=SHORT, background=False)
        ref = G.sensor_phases(simulate(base, SEED))
        delta, t0 = 0.25, 2_000_000
        for k in ORDER:
            if k == "M":
                continue
            sim = simulate(replace(base, disturbances=(Disturbance(k, "step", t0, delta),)), SEED)
            got = G.sensor_phases(sim)
            for sensor in CONFIGS["C2_sampled_pilot"][0]:
                after = [(a[1], b[1]) for a, b in zip(ref[sensor], got[sensor]) if a[0] >= t0]
                self.assertTrue(after, sensor)
                expect = SENSORS[sensor].c(k) * delta
                with self.subTest(term=k, sensor=sensor):
                    self.assertTrue(all(abs((g - r) - expect) < 1e-12 for r, g in after))

    def test_delay_step_is_frequency_selective(self):
        tau, t0 = 1.0, 2_000_000
        base = Scenario(duration_ns=SHORT, background=False)
        ref = G.sensor_phases(simulate(base, SEED))
        got = G.sensor_phases(simulate(replace(base, disturbances=(Disturbance("G", "delay_step", t0, tau),)), SEED))
        ds = [g[1] - r[1] for r, g in zip(ref["scene"], got["scene"]) if r[0] >= t0][0]
        dp = [g[1] - r[1] for r, g in zip(ref["pilot_sampled"], got["pilot_sampled"]) if r[0] >= t0][0]
        self.assertAlmostEqual(dp - ds, -2 * math.pi * (G.PILOT_RF_HZ - G.SCENE_RF_HZ) * tau * 1e-9, places=12)


class TestMatchedObservations(Tmp):
    """Refinement 1: matched pairs are built and checked per configuration."""
    TEMPLATE = Disturbance("G", "step", 3_100_000, 0.3)

    def _pair(self, config, circuit, template=None, base=None):
        base = base or Scenario(config=config, duration_ns=SHORT)
        sa, sb = matched_pair(base, circuit, template or self.TEMPLATE)
        ca, _ = self.gen(sa)
        cb, _ = self.gen(sb)
        return online_difference(ca, cb)

    def test_every_circuit_of_every_emittable_config_is_observationally_equivalent(self):
        broken = 0
        for config in G.EMITTED:
            sensors = CONFIGS[config][0]
            for circ in emittable_circuits(config):
                with self.subTest(config=config, circuit=sorted(circ)):
                    diff = self._pair(config, circ)
                    if not leakage_breaks(sensors, circ):
                        self.assertTrue(diff["equivalent"], diff)
                        continue
                    # Scene leakage into the P4 detector (unmodelled in the static
                    # graph) separates the pair, ONLY in the detector stream...
                    broken += 1
                    self.assertFalse(diff["equivalent"])
                    self.assertLessEqual(diff["max_iq_lsb"], 1)
                    self.assertTrue(diff["events_equal"] and diff["witness_max_abs"] <= 1e-9)
                    # ...and with the scene removed the pair is equivalent again.
                    off = self._pair(config, circ, base=Scenario(config=config, duration_ns=SHORT,
                                                                  scene_on=False))
                    self.assertTrue(off["equivalent"], off)
        self.assertGreater(broken, 0)

    def test_pairs_separate_once_a_sensor_breaks_the_circuit(self):
        order = list(G.EMITTED)
        checked = 0
        for i, config in enumerate(order):
            for richer in order[i + 1:]:
                richer_circs = set(circuits(CONFIGS[richer][0]))
                for circ in emittable_circuits(config):
                    if circ in richer_circs:
                        continue
                    sa, sb = matched_pair(Scenario(config=config, duration_ns=SHORT), circ, self.TEMPLATE)
                    ca, _ = self.gen(replace(sa, config=richer))
                    cb, _ = self.gen(replace(sb, config=richer))
                    diff = online_difference(ca, cb)
                    with self.subTest(built_for=config, rendered_as=richer, circuit=sorted(circ)):
                        self.assertFalse(diff["equivalent"])
                        self.assertTrue(diff["max_iq_lsb"] > 20 or diff["witness_max_abs"] > 0.05
                                        or diff["max_abs_diff"].get("mon0", 0.0) > 0.01, diff)
                    checked += 1
        self.assertGreater(checked, 0)

    def test_source_vs_receiver_ambiguity_is_configuration_specific(self):
        xg = frozenset({"X", "G"})
        for config in ("C0_bracket", "C1_witness"):
            self.assertTrue(self._pair(config, xg)["equivalent"], config)
        self.assertNotIn(xg, circuits(CONFIGS["C2_sampled_pilot"][0]))
        sa, sb = matched_pair(Scenario(config="C1_witness", duration_ns=SHORT), xg, self.TEMPLATE)
        ca, _ = self.gen(replace(sa, config="C2_sampled_pilot"))
        cb, _ = self.gen(replace(sb, config="C2_sampled_pilot"))
        self.assertFalse(online_difference(ca, cb)["equivalent"])
        # ...but X still hides behind a pilot-side term in C2.
        self.assertTrue(self._pair("C2_sampled_pilot", frozenset({"X", "J", "G"}))["equivalent"])

    def test_brief_cases(self):
        c2 = "C2_sampled_pilot"
        self.assertTrue(self._pair(c2, frozenset({"J", "S"}))["equivalent"])      # injection vs source
        self.assertTrue(self._pair(c2, frozenset({"G", "A"}))["equivalent"])      # receiver stages
        self.assertEqual(split_vector(c2, frozenset({"X", "J", "G"})), ({"X": 1.0, "J": 1.0}, {"G": 1.0}))
        # Drift and glitch shapes are matched too, not only steps.
        ramp = Disturbance("G", "ramp", 1_000_000, 0.4, 3_000_000)
        self.assertTrue(self._pair(c2, frozenset({"J", "S"}), template=ramp)["equivalent"])
        glitch = Disturbance("G", "pulse", 3_000_000, 0.8, 200_000)
        self.assertTrue(self._pair(c2, frozenset({"G", "A"}), template=glitch)["equivalent"])

    def test_broadband_delay_matches_phase_step_plus_mismatch(self):
        tau, t0 = 1.5, 3_000_000
        delay = Scenario(disturbances=(Disturbance("G", "delay_step", t0, tau),), duration_ns=SHORT)
        g = -2 * math.pi * G.SCENE_RF_HZ * tau * 1e-9
        fg = -2 * math.pi * (G.PILOT_RF_HZ - G.SCENE_RF_HZ) * tau * 1e-9
        equiv = replace(delay, disturbances=(Disturbance("G", "step", t0, g), Disturbance("FG", "step", t0, fg)))
        ca, _ = self.gen(delay)
        cb, _ = self.gen(equiv)
        self.assertTrue(online_difference(ca, cb)["equivalent"])

    def test_intersample_glitch_is_invisible_but_inband_glitch_is_not(self):
        base = Scenario(duration_ns=SHORT)
        c0, _ = self.gen(base)
        c1, t1 = self.gen(replace(base, disturbances=(Disturbance("G", "intersample_pulse", 3_000_000, 1.0),)))
        c2, _ = self.gen(replace(base, disturbances=(Disturbance("G", "pulse", 3_000_000, 1.0, 100_000),)))
        self.assertTrue(online_difference(c0, c1)["equivalent"])
        self.assertFalse(online_difference(c0, c2)["equivalent"])
        with open(t1) as f:
            self.assertTrue(any(e["kind"] == "pulse" and e["value"] == 1.0 for e in json.load(f)["events"]))


class TestBlindness(Tmp):
    """Refinement 3: no seed, name, timing pattern, metadata or file reveals truth."""

    def test_operating_metadata_is_independent_of_the_disturbance_schedule(self):
        base = Scenario(duration_ns=SHORT, faults=(AcqFault("gain_change"),))
        a, _ = self.gen(base)
        b, _ = self.gen(replace(base, disturbances=(Disturbance("G", "pulse", 1_000_000, 1.0, 300_000),
                                                    Disturbance("R", "step", 4_000_000, -0.4))))
        diff = online_difference(a, b)
        self.assertTrue(diff["events_equal"] and diff["witness_timing_equal"] and diff["manifest_equal_normalized"])
        self.assertFalse(diff["equivalent"])  # the physics differs, as it should
        with open(os.path.join(a, "events.jsonl"), "rb") as f1, open(os.path.join(b, "events.jsonl"), "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_batch_captures_reveal_no_seed_case_or_parameters(self):
        caps, truth = os.path.join(self.d, "caps"), os.path.join(self.d, "truth")
        cases = ["G_step", "J_step", "G_glitch_intersample", "fault_retune", "baseline"]
        ids = batch.generate_blind_batch(caps, truth, SEED, 10, cases=cases, duration_ns=SHORT)
        self.assertEqual(sorted(os.listdir(caps)), sorted(ids))
        with open(os.path.join(truth, "batch_index.json")) as f:
            index = json.load(f)["captures"]
        forbidden = {SEED, *cases, *batch.LIBRARY, "step", "pulse", "ramp", "glitch", "intersample",
                     "delay", "sine", "random_walk", "phys", "scheduled", "baseline", "matched"}
        seen_ids = set()
        for entry in index:
            cid = entry["capture_id"]
            seen_ids.add(cid)
            files = _files(os.path.join(caps, cid))
            self.assertEqual(set(files) - {"manifest.json", "rx0.sc16", "events.jsonl", "witness.jsonl"}, set())
            text = b"".join(v for k, v in files.items() if k != "rx0.sc16").decode().lower()
            for word in forbidden:
                with self.subTest(capture=cid, word=word):
                    self.assertNotIn(word.lower(), text)
            self.assertNotIn(entry["case"].lower(), cid.lower())
            for name in files:
                st = os.stat(os.path.join(caps, cid, name))
                self.assertEqual(int(st.st_mtime), G.FIXED_MTIME)
            m = load_capture(os.path.join(caps, cid)).manifest
            self.assertEqual(m["created_utc"], G.FIXED_CREATED)
            sealed_truth.open_for_scoring(m, os.path.join(truth, cid + ".truth.json"))
        self.assertEqual(len(seen_ids), 10)
        # Same case twice -> unrelated ids.
        by_case = {}
        for e in index:
            by_case.setdefault(e["case"], []).append(e["capture_id"])
        dup = next(v for v in by_case.values() if len(v) > 1)
        self.assertNotEqual(dup[0][4:12], dup[1][4:12])

    def test_batch_roots_must_be_disjoint(self):
        caps = os.path.join(self.d, "caps")
        with self.assertRaisesRegex(ValueError, "disjoint"):
            batch.generate_blind_batch(caps, os.path.join(caps, "truth"), SEED, 1, duration_ns=SHORT)

    def test_case_templates_see_only_rng_and_geometry(self):
        for name, fn in batch.LIBRARY.items():
            with self.subTest(case=name):
                self.assertEqual(len(inspect.signature(fn).parameters), 3)


if __name__ == "__main__":
    unittest.main()
