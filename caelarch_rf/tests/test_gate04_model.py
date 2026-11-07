"""Observability matrix, four-state adapter, evidence records, module isolation."""
import ast
import math
import os
import unittest

from caelarch_rf.gate04 import observability as O
from caelarch_rf.gate04 import states
from caelarch_rf.gate04.records import PilotObservation
from caelarch_rf.gate04.states import PublicState as S

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "..", "gate04")


class TestObservability(unittest.TestCase):
    """Regression-encode the handoff brief's permitted claims in the graph model."""

    def test_pilot_cannot_see_reference_path(self):
        self.assertNotIn("R", O.SENSORS["pilot_sampled"].terms)
        self.assertNotIn("R", O.SENSORS["analog_monitor_P4"].terms)

    def test_analog_monitor_at_P4_cannot_see_internal_sdr_conversion(self):
        self.assertNotIn("A", O.SENSORS["analog_monitor_P4"].terms)

    def test_bracket_only_reference_drift_is_detectable_but_unresolved(self):
        r = O.analyse_config("C0_bracket")
        self.assertTrue(r["R"].detectable)
        self.assertEqual(r["R"].status, "CONFOUNDED")
        self.assertIn(frozenset("XHR"), r["R"].circuits)

    def test_witness_makes_R_estimable_only_under_qualified_K(self):
        self.assertEqual(O.analyse(("scene", "reference", "witness"))["R"].status, "CONFOUNDED")
        self.assertEqual(O.analyse_config("C1_witness")["R"].status, "CONDITIONAL")

    def test_known_source_drift_mimics_receiver_drift_in_bracket(self):
        # Not stated in the brief; found by this model. X must be witnessed/bounded.
        self.assertIn(frozenset("XG"), O.analyse_config("C1_witness")["G"].circuits)

    def test_single_pilot_detection_is_not_attribution(self):
        r = O.analyse_config("C2_sampled_pilot")
        for k in ("G", "J", "S"):
            self.assertTrue(r[k].detectable)
        self.assertEqual(r["G"].status, "CONFOUNDED")
        self.assertIn(frozenset("JS"), r["J"].circuits)        # source vs injection
        self.assertIn(frozenset("GA"), r["G"].circuits)        # identical signatures
        self.assertIn(frozenset({"X", "J", "G"}), r["G"].circuits)
        # U=AG needs the source/injection AND frequency-mismatch bounds.
        self.assertEqual(r["U=AG"].status, "CONDITIONAL")
        self.assertEqual(O.analyse(("scene", "reference", "witness", "pilot_sampled"))["U=AG"].status,
                         "CONFOUNDED")
        for one_bound in (("K_qualified", "SJ_bounded"), ("K_qualified", "F_bounded")):
            self.assertEqual(O.analyse(("scene", "reference", "witness", "pilot_sampled"),
                                       one_bound)["U=AG"].status, "CONFOUNDED")

    def test_no_configuration_attributes_G_from_online_data_alone(self):
        for name, (sensors, _, _) in O.CONFIGS.items():
            with self.subTest(config=name):
                self.assertNotEqual(O.analyse(sensors)["G"].status, "ATTRIBUTABLE")

    def test_circuits_agree_with_rowspace(self):
        for name, (sensors, _, _) in O.CONFIGS.items():
            res = O.analyse(sensors)
            circ = O.circuits(sensors)
            for k in O.ORDER:
                in_circuit = any(k in c for c in circ)
                with self.subTest(config=name, k=k):
                    self.assertEqual(res[k].status == "ATTRIBUTABLE", not in_circuit)

    def test_monitor_own_drift_weakens_option_b(self):
        # Refinement 2: the detector reports signal minus its own reference phase.
        self.assertEqual(O.SENSORS["analog_monitor_P4"].c("M"), -1)
        sensors, assumptions, _ = O.CONFIGS["C3_analog_P4"]
        for alt in O.minimal_requirements(sensors, assumptions, "G"):
            self.assertIn("M_bounded", alt)
        no_m = tuple(a for a in assumptions if a != "M_bounded")
        self.assertEqual(O.analyse(sensors, no_m)["G"].status, "CONFOUNDED")

    def test_source_referenced_detector_cancels_S_but_not_J_or_M(self):
        self.assertNotIn("S", O.SENSORS["analog_monitor_P4_srcref"].terms)
        sensors, assumptions, _ = O.CONFIGS["C3s_analog_P4_srcref"]
        self.assertEqual(O.minimal_requirements(sensors, assumptions, "G"),
                         [frozenset({"J_bounded", "F_bounded", "M_bounded"})])

    def test_R1_is_an_explicit_qualification_requirement(self):
        sensors, assumptions, _ = O.CONFIGS["C1_witness"]
        self.assertEqual(O.REQUIREMENTS["X_bounded"][0], "QR-X")
        self.assertEqual(O.minimal_requirements(sensors, assumptions, "U=AG"),
                         [frozenset({"K_qualified", "X_bounded"})])
        self.assertEqual(O.analyse(sensors, ("K_qualified",))["U=AG"].status, "CONFOUNDED")

    def test_circuits_are_configuration_specific(self):
        xg = frozenset("XG")
        self.assertIn(xg, O.circuits(O.CONFIGS["C0_bracket"][0]))
        self.assertIn(xg, O.circuits(O.CONFIGS["C1_witness"][0]))
        self.assertNotIn(xg, O.circuits(O.CONFIGS["C2_sampled_pilot"][0]))

    def test_circuit_vectors_annihilate_their_sensors(self):
        for name, (sensors, _, _) in O.CONFIGS.items():
            for c in O.circuits(sensors):
                if len(c) < 2:
                    continue
                v = O.circuit_vector(sensors, c)
                self.assertEqual(set(v), set(c))
                self.assertTrue(all(abs(x) < 1e-12 for x in O.observe(sensors, v).values()), (name, c))

    def test_generated_doc_is_current(self):
        path = os.path.join(HERE, "..", "docs", "OBSERVABILITY_MATRIX.md")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), O.render_markdown(),
                             "run: python -m caelarch_rf.gate04.observability")


class TestStates(unittest.TestCase):
    def test_no_upstream_status_maps_to_valid_bounded(self):
        for status in states.GATE0_V03:
            self.assertNotEqual(states.from_gate0_v03(status)[0], S.VALID_BOUNDED)
        for status in ("CONDITIONAL", "UNRESOLVED", "INVALID", "VALID_BOUNDED"):
            self.assertNotEqual(states.from_scaffold_0044({"status": status})[0], S.VALID_BOUNDED)

    def test_unknown_fails_closed(self):
        self.assertEqual(states.from_gate0_v03("HARDWARE_QUALIFIED")[0], S.INVALID)
        self.assertEqual(states.from_scaffold_0044({"status": "VALID_BOUNDED"})[0], S.INVALID)
        self.assertEqual(states.from_scaffold_0044(None)[0], S.INVALID)

    def test_brief_mappings(self):
        self.assertEqual(states.from_gate0_v03("UNRESOLVED_RETUNE_REQUALIFICATION")[0], S.INVALID)
        self.assertEqual(states.from_gate0_v03("UNRESOLVED_LIVE_MONITOR_MISSING")[0], S.INVALID)
        self.assertEqual(states.from_gate0_v03("UNRESOLVED_NO_INDEPENDENT_WITNESS")[0], S.UNRESOLVED)
        self.assertEqual(states.from_gate0_v03("UNRESOLVED_LIVE_AG_MONITOR_ALARM")[0], S.UNRESOLVED)
        self.assertEqual(states.from_gate0_v03("BOUNDED_MODEL_ONLY")[0], S.CONDITIONAL)

    def test_combine_is_most_conservative(self):
        self.assertEqual(states.combine(S.CONDITIONAL, S.INVALID, S.UNRESOLVED), S.INVALID)
        self.assertEqual(states.combine(S.CONDITIONAL, S.UNRESOLVED), S.UNRESOLVED)
        self.assertEqual(states.combine(), S.UNRESOLVED)

    def test_valid_bounded_unreachable(self):
        with self.assertRaises(states.QualificationVerifierMissing):
            states.promote_to_valid_bounded(S.CONDITIONAL, {"record_id": "fake", "record_sha256": "0" * 64})


def _obs(**kw):
    base = dict(stream_id="rx0", monitor_kind="sampled_digital", timebase="sdr0",
                window_start_ns=0, window_end_ns=1000, available_ns=1500, offset_hz=1e5,
                phase_rad=0.1, unwrapped_phase_rad=0.1, phase_std_rad=0.01, snr_db=20.0,
                clipped=False, gap_in_window=False, pilot_enabled=True)
    base.update(kw)
    return PilotObservation(**base)


class TestRecords(unittest.TestCase):
    def test_causality(self):
        o = _obs()
        self.assertFalse(o.usable_at(1000))  # latency not yet elapsed
        self.assertTrue(o.usable_at(1500))
        with self.assertRaises(ValueError):
            _obs(available_ns=900)  # estimate claimed before its window ended

    def test_unusable_conditions(self):
        for kw in ({"clipped": True}, {"gap_in_window": True}, {"pilot_enabled": False},
                   {"phase_rad": None, "unwrapped_phase_rad": None}):
            self.assertFalse(_obs(**kw).usable)

    def test_validation(self):
        with self.assertRaises(ValueError):
            _obs(phase_rad=4.0)
        with self.assertRaises(ValueError):
            _obs(snr_db=math.nan)
        with self.assertRaises(ValueError):
            _obs(monitor_kind="analog_quadrature")  # must state detector bandwidth
        self.assertTrue(_obs(monitor_kind="analog_quadrature", detector_bandwidth_hz=5e3).usable)


class TestIsolation(unittest.TestCase):
    ONLINE = ("contract.py", "ingest.py", "records.py", "states.py", "observability.py", "validate.py")

    def test_online_modules_never_import_sealed_truth(self):
        for name in self.ONLINE:
            with open(os.path.join(PKG, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""] + [a.name for a in node.names]
                with self.subTest(module=name):
                    self.assertFalse(any("sealed_truth" in m for m in mods))
                    self.assertFalse(any(m == "sim" or "caelarch_rf.sim" in m for m in mods))

    def test_stdlib_only(self):
        import sys
        allowed = set(sys.stdlib_module_names) | {"__future__", "caelarch_rf"}
        sim = os.path.join(HERE, "..", "sim")
        paths = [os.path.join(d, n) for d in (PKG, sim) for n in os.listdir(d) if n.endswith(".py")]
        for path in paths:
            name = os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                with self.subTest(module=name):
                    self.assertTrue(set(roots) <= allowed, roots)


if __name__ == "__main__":
    unittest.main()
