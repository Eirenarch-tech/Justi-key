"""Receiver hardware artefacts in the generator (calibrated on public recordings)."""
import json
import math
import os
import tempfile
import unittest
from dataclasses import replace

import array
import sys

from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.monitors import extract_sampled_pilot
from caelarch_rf.sim import generator as G
from caelarch_rf.sim.disturbances import Disturbance
from caelarch_rf.sim.generator import Scenario, generate
from caelarch_rf.sim.hardware import PRESETS, FrontEnd, HardwareModel
from caelarch_rf.sim.matched import matched_pair, online_difference

SEED = "4a" * 32
DUR = 12_000_000


def _iq(cap):
    a = array.array("h")
    with open(os.path.join(cap, "rx0.sc16"), "rb") as f:
        a.frombytes(f.read())
    if sys.byteorder != "little":
        a.byteswap()
    return [complex(a[2 * k], a[2 * k + 1]) for k in range(len(a) // 2)]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.k = 0

    def tearDown(self):
        self._tmp.cleanup()

    def gen(self, sc, seed=SEED):
        self.k += 1
        d = os.path.join(self._tmp.name, f"c{self.k}")
        generate(sc, seed, d, d + ".truth.json")
        return d


class TestFrontEnd(unittest.TestCase):
    def test_8bit_adc_matches_cu8_levels(self):
        fe = FrontEnd(HardwareModel(adc_bits=8), __import__("random").Random(1))
        self.assertEqual(fe(complex(0.1, -0.1)), (128, -128))
        self.assertEqual(fe(complex(1e9, -1e9)), (32640, -32640))   # rails
        self.assertEqual(HardwareModel(adc_bits=8).rail, 32640)

    def test_iq_imbalance_sets_image_rejection(self):
        fe = FrontEnd(PRESETS["iq_worst_observed"], __import__("random").Random(1))
        # Standard mixer model for 0.81 dB / 1.89 deg; the measured file gave 26.7 dB with the
        # analysis estimator (the two formulas differ slightly at this imbalance).
        self.assertAlmostEqual(fe.summary()["irr_db"], 26.1, delta=0.2)
        ideal = FrontEnd(HardwareModel(), __import__("random").Random(1))
        self.assertEqual(ideal(complex(1000.4, -7.6)), (1000, -8))

    def test_compression_is_above_adc_full_scale(self):
        for name in ("rtl_sdr_typical", "rtl_sdr_adverse"):
            self.assertGreater(PRESETS[name].sat_level_dbfs, 0.0)   # rails clip before the LNA compresses


class TestGeneratedArtefacts(Base):
    def test_ideal_receiver_is_unchanged(self):
        a = self.gen(Scenario(duration_ns=DUR))
        b = self.gen(Scenario(duration_ns=DUR, hardware=None))
        with open(os.path.join(a, "rx0.sc16"), "rb") as f1, open(os.path.join(b, "rx0.sc16"), "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        with open(a + ".truth.json") as f:
            self.assertNotIn("hardware", json.load(f)["generator"])

    def test_typical_preset_dc_iq_and_quantisation_match_calibration(self):
        hw = PRESETS["rtl_sdr_typical"]
        cap = self.gen(Scenario(duration_ns=DUR, hardware=hw))
        c = load_capture(cap)
        self.assertEqual(c.streams["rx0"].meta["clip_level"], 32640)
        self.assertIn("8-bit", c.manifest["device"]["model"])
        z = _iq(cap)
        self.assertLessEqual(len({int(v.real) for v in z}), 256)
        self.assertTrue(all((int(v.real) - 128) % 256 == 0 for v in z[:2000]))   # cu8 lattice
        dc = abs(sum(z) / len(z))
        self.assertAlmostEqual(20 * math.log10(dc / 32768), hw.dc_offset_dbfs, delta=1.5)
        with open(cap + ".truth.json") as f:
            gen = json.load(f)["generator"]
        self.assertEqual(gen["hardware"]["model"]["name"], "rtl_sdr_typical")
        self.assertNotIn("rtl_sdr", json.dumps(c.manifest))       # profile name stays in truth

    def test_adverse_interference_clips_and_is_rejected_fail_closed(self):
        hw = PRESETS["rtl_sdr_adverse"]
        cap = self.gen(Scenario(duration_ns=DUR, hardware=hw), seed="5b" * 32)
        with open(cap + ".truth.json") as f:
            bursts = json.load(f)["generator"]["hardware"]["interference"]
        self.assertTrue(bursts)
        sv = load_capture(cap).streams["rx0"]
        z = _iq(cap)
        rails = sum(1 for v in z if abs(v.real) >= 32640 or abs(v.imag) >= 32640)
        if rails:
            self.assertTrue(any(r.reason == "CLIPPED_OR_NONFINITE" for r in sv.rejections))
        # Interference is physics only: never an online event.
        self.assertFalse(any(e["type"] not in ("SWITCH_COMMAND", "SETTLED") for e in load_capture(cap).events))

    def test_interference_does_not_depend_on_disturbances(self):
        hw = PRESETS["rtl_sdr_adverse"]
        a = self.gen(Scenario(duration_ns=DUR, hardware=hw))
        b = self.gen(Scenario(duration_ns=DUR, hardware=hw, disturbances=(Disturbance("G", "step", 6_000_000, 0.4),)))
        with open(a + ".truth.json") as f1, open(b + ".truth.json") as f2:
            ia = json.load(f1)["generator"]["hardware"]
            ib = json.load(f2)["generator"]["hardware"]
        self.assertEqual(ia, ib)

    def test_matched_pairs_remain_equivalent_through_the_front_end(self):
        hw = PRESETS["rtl_sdr_adverse"]
        for circ in (frozenset("JS"), frozenset("GA")):
            sa, sb = matched_pair(Scenario(duration_ns=DUR, hardware=hw), circ,
                                  Disturbance("G", "step", 6_030_000, 0.4))
            with self.subTest(circuit=sorted(circ)):
                self.assertTrue(online_difference(self.gen(sa), self.gen(sb))["equivalent"])

    def test_extractor_runs_on_8bit_capture_and_detects_a_large_step(self):
        hw = PRESETS["rtl_sdr_typical"]
        cap = self.gen(Scenario(duration_ns=DUR, hardware=hw, disturbances=(Disturbance("G", "step", 6_030_000, 0.6),)))
        r = extract_sampled_pilot(load_capture(cap))
        self.assertTrue(r.excursions)
        self.assertGreater(r.envelope["detection_floor_rad"], 0)


if __name__ == "__main__":
    unittest.main()
