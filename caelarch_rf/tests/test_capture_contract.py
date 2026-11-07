"""Measurement contract + ingestion: format, timing, fail-closed and truth isolation."""
import contextlib
import io
import os
import tempfile
import unittest

from caelarch_rf.gate04 import contract as C
from caelarch_rf.gate04 import sealed_truth
from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.validate import main as validate_main
from caelarch_rf.tests import synth


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "cap")
        self.truth_dir = os.path.join(self._tmp.name, "controller")
        os.makedirs(self.truth_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, **kw):
        return synth.write_capture(self.root, **kw)

    def refused(self, pattern, **kw):
        self.write(**kw)
        with self.assertRaisesRegex(C.ContractError, pattern):
            load_capture(self.root)


class TestValidCapture(Base):
    def test_baseline_loads_and_derives_settling(self):
        self.write()
        cap = load_capture(self.root)
        sv = cap.streams["rx0"]
        self.assertTrue(cap.synthetic)
        # 1 MS/s: switch at 1 ms guarded 50 us; switch at 5 ms extended to the
        # device SETTLED report at 5.08 ms (settling may extend, never shorten).
        self.assertEqual(sv.accepted_ranges(), [(0, 1000), (1050, 5000), (5080, synth.N)])
        self.assertEqual(sv.port_at(500), "UNKNOWN")
        self.assertEqual(sv.port_at(2000), "H")
        self.assertEqual(sv.port_at(9000), "R")

    def test_sample_time_is_exact_integer_arithmetic(self):
        m = synth.base_manifest()
        m["streams"][0]["sample_rate_hz"] = 30.72e6  # AD9361-class rate, 32.55.. ns
        m["streams"][0]["t0_ns"] = 123
        self.write(manifest=m, events=[])
        sv = load_capture(self.root).streams["rx0"]
        self.assertEqual(sv.time_of(3072), 123 + 100_000)
        self.assertEqual(sv.index_at(123 + 100_000), 3072)
        self.assertEqual(sv.index_at(124, "ceil"), 1)
        self.assertEqual(sv.index_at(124, "floor"), 0)

    def test_validate_cli(self):
        self.write()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(validate_main([self.root]), 0)
            os.remove(os.path.join(self.root, "events.jsonl"))
            self.assertEqual(validate_main([self.root]), 1)
        self.assertIn("synthetic capture", out.getvalue())
        self.assertIn("REFUSED", err.getvalue())


class TestTruthIsolation(Base):
    """Disturbance truth must have no route into an OnlineCapture."""

    def test_undeclared_truth_file_in_capture_is_refused(self):
        self.write()
        with open(os.path.join(self.root, "notes.json"), "w") as f:
            f.write("{}")
        with self.assertRaisesRegex(C.ContractError, "undeclared"):
            load_capture(self.root)

    def test_sealed_truth_written_into_capture_is_refused(self):
        m = self.write()
        sealed_truth.write_truth(os.path.join(self.root, "schedule.json"), m["capture_id"], "ctl0",
                                 [{"target": "G", "t_ns": 120, "phase_rad": 0.3}])
        with self.assertRaisesRegex(C.ContractError, "undeclared"):
            load_capture(self.root)

    def test_truth_file_presented_as_manifest_is_refused(self):
        self.write()
        sealed_truth.write_truth(os.path.join(self.root, "manifest.json"), "syn-0001", "ctl0",
                                 [{"target": "G", "t_ns": 120}])
        with self.assertRaisesRegex(C.ContractError, "sealed-truth"):
            load_capture(self.root)

    def test_unknown_manifest_key_is_refused(self):
        m = synth.base_manifest()
        m["disturbance_schedule"] = [{"target": "G", "t_ns": 120}]
        self.refused("unknown field", manifest=m)

    def test_unknown_nested_key_is_refused(self):
        m = synth.base_manifest()
        m["streams"][0]["expected_phase_step_rad"] = 0.3
        self.refused("unknown field", manifest=m)

    def test_truth_marker_in_identifier_or_filename_is_refused(self):
        for mutate in (lambda m: m["streams"][0].__setitem__("file", "rx0_truth.sc16"),
                       lambda m: m.__setitem__("capture_id", "run7-G-step-label"),
                       lambda m: m["provenance"].__setitem__("operator", "oracle bench")):
            m = synth.base_manifest()
            mutate(m)
            with self.subTest(m=m["capture_id"]):
                self.refused("forbidden marker", manifest=m)

    def test_perturbation_event_type_is_refused(self):
        ev = synth.base_events() + [{"seq": 9, "type": "PERTURBATION", "timebase": "sdr0",
                                     "t_ns": 7_000_000, "target": "G"}]
        self.refused("unknown event type", events=ev)

    def test_extra_field_on_allowed_event_is_refused(self):
        ev = synth.base_events()
        ev[0]["applied_phase_rad"] = 0.3
        self.refused("unknown field", events=ev)

    def test_free_text_label_cannot_carry_newlines_or_long_prose(self):
        m = synth.base_manifest()
        m["device"]["model"] = "B220mini\nG step 0.3rad at 120ms"
        self.refused("invalid label", manifest=m)

    def test_path_traversal_and_symlinks_refused(self):
        m = synth.base_manifest()
        m["streams"][0]["file"] = "../rx0.sc16"
        with self.assertRaisesRegex(C.ContractError, "invalid file name"):
            C.validate_manifest(m)
        self.write()
        os.symlink(os.path.join(self.root, "rx0.sc16"), os.path.join(self.root, "alias.sc16"))
        with self.assertRaisesRegex(C.ContractError, "symlink"):
            load_capture(self.root)

    def test_commitment_verifies_offline_and_salt_defeats_dictionary(self):
        events = [{"target": "G", "t_ns": 120_000_000, "phase_rad": 0.3}]
        p1 = os.path.join(self.truth_dir, "t1.json")
        p2 = os.path.join(self.truth_dir, "t2.json")
        c1 = sealed_truth.write_truth(p1, "syn-0001", "ctl0", events)
        c2 = sealed_truth.write_truth(p2, "syn-0001", "ctl0", events)
        # Same schedule, different digests: a guessed schedule cannot be confirmed.
        self.assertNotEqual(c1["digest"], c2["digest"])
        m = synth.base_manifest()
        m["truth_commitment"] = c1
        self.write(manifest=m)
        cap = load_capture(self.root)  # commitment is allowed in the capture
        doc = sealed_truth.open_for_scoring(cap.manifest, p1)
        self.assertEqual(doc["events"], events)
        with self.assertRaisesRegex(C.ContractError, "does not match"):
            sealed_truth.open_for_scoring(cap.manifest, p2)

    def test_short_salt_rejected(self):
        with self.assertRaises(ValueError):
            sealed_truth.write_truth(os.path.join(self.truth_dir, "t.json"), "x", "c",
                                     [], salt=b"0" * 8)


class TestMalformedInput(Base):
    def test_hash_mismatch(self):
        self.write()
        with open(os.path.join(self.root, "rx0.sc16"), "r+b") as f:
            f.write(b"\x00\x01")
        with self.assertRaisesRegex(C.ContractError, "sha256 mismatch"):
            load_capture(self.root)

    def test_sample_count_mismatch(self):
        m = synth.base_manifest()
        m["streams"][0]["sample_count"] = synth.N + 1
        self.write(manifest=m, iq=synth.tone_sc16(synth.N))
        with self.assertRaisesRegex(C.ContractError, "size"):
            load_capture(self.root)

    def test_nan_and_duplicate_keys_rejected(self):
        self.write()
        mp = os.path.join(self.root, "manifest.json")
        with open(mp) as f:
            text = f.read()
        with open(mp, "w") as f:
            f.write(text.replace('"rx_gain_db": 30.0', '"rx_gain_db": NaN'))
        with self.assertRaisesRegex(C.ContractError, "non-finite"):
            load_capture(self.root)
        with open(mp, "w") as f:
            f.write(text.replace('"capture_id": "syn-0001",', '"capture_id": "a", "capture_id": "b",'))
        with self.assertRaisesRegex(C.ContractError, "duplicate JSON key"):
            load_capture(self.root)

    def test_events_out_of_order(self):
        ev = synth.base_events()
        ev[1]["t_ns"] = 10
        self.refused("backwards", events=ev)
        ev = synth.base_events()
        ev[2]["seq"] = 1
        self.refused("strictly increasing", events=ev)

    def test_unknown_stream_or_timebase(self):
        ev = synth.base_events()
        ev[2]["stream_id"] = "rx9"
        self.refused("unknown stream_id", events=ev)
        ev = synth.base_events()
        ev[0]["timebase"] = "gps9"
        self.refused("unknown timebase", events=ev)

    def test_pilot_block_must_match_carries(self):
        m = synth.base_manifest()
        m["streams"][0]["pilot"] = None
        self.refused("pilot", manifest=m)
        m = synth.base_manifest()
        m["streams"][0]["pilot"]["offset_hz"] = 600_000.0
        self.refused("Nyquist", manifest=m)


class TestFailClosedTiming(Base):
    def test_overflow_with_unknown_drop_untimes_rest_of_stream(self):
        ev = synth.base_events() + [{"seq": 3, "type": "OVERFLOW", "timebase": "sdr0",
                                     "t_ns": 7_000_000, "stream_id": "rx0",
                                     "sample_index": 7000, "dropped_samples": None}]
        self.write(events=ev)
        sv = load_capture(self.root).streams["rx0"]
        self.assertEqual(sv.untimed_from, 7000)
        self.assertIsNone(sv.time_of(7500))
        self.assertEqual(sv.accepted_ranges()[-1], (5080, 7000))

    def test_overflow_with_known_drop_shifts_time_map(self):
        ev = [{"seq": 0, "type": "OVERFLOW", "timebase": "sdr0", "t_ns": 4_000_000,
               "stream_id": "rx0", "sample_index": 4000, "dropped_samples": 250}]
        self.write(events=ev)
        sv = load_capture(self.root).streams["rx0"]
        self.assertEqual(sv.time_of(3999), 3_999_000)
        self.assertEqual(sv.time_of(4000), 4_250_000)
        # Inside the dropout gap: resolve to the boundary samples.
        self.assertEqual(sv.index_at(4_100_000, "ceil"), 4000)
        self.assertEqual(sv.index_at(4_100_000, "floor"), 3999)
        self.assertEqual(sv.accepted_ranges(), [(0, 3999), (4001, synth.N)])

    def test_switch_on_unaligned_timebase_is_refused(self):
        m = synth.base_manifest()
        m["timebases"].append({"id": "ctl0", "kind": "controller"})
        ev = synth.base_events()
        ev[0]["timebase"] = "ctl0"
        self.refused("declare a timebase alignment", manifest=m, events=ev)

    def test_alignment_uncertainty_widens_guard(self):
        m = synth.base_manifest()
        m["timebases"].append({"id": "ctl0", "kind": "controller"})
        m["timebase_alignments"].append({"from": "ctl0", "to": "sdr0", "offset_ns": 0,
                                         "uncertainty_ns": 10_000, "method": "loopback_xcorr"})
        ev = [{"seq": 0, "type": "SWITCH_COMMAND", "timebase": "ctl0", "t_ns": 1_000_000, "port": "H"}]
        self.write(manifest=m, events=ev)
        sv = load_capture(self.root).streams["rx0"]
        self.assertEqual(sv.accepted_ranges(), [(0, 990), (1060, synth.N)])

    def test_retune_and_lo_unlock(self):
        ev = [{"seq": 0, "type": "LO_RETUNE", "timebase": "sdr0", "t_ns": 2_000_000,
               "stream_id": "rx0", "new_center_freq_hz": 916e6, "lo_locked": False},
              {"seq": 1, "type": "LO_RETUNE", "timebase": "sdr0", "t_ns": 6_000_000,
               "stream_id": "rx0", "new_center_freq_hz": 916e6, "lo_locked": True}]
        self.write(events=ev)
        sv = load_capture(self.root).streams["rx0"]
        self.assertEqual(sv.retune_indices, [2000, 6000])
        self.assertEqual(sv.accepted_ranges(), [(0, 2000), (6200, synth.N)])

    def test_reference_unlocked_at_start(self):
        m = synth.base_manifest()
        m["clock"]["ref_locked_at_start"] = False
        ev = [{"seq": 0, "type": "REF_LOCK", "timebase": "sdr0", "t_ns": 3_000_000, "locked": True}]
        self.write(manifest=m, events=ev)
        self.assertEqual(load_capture(self.root).streams["rx0"].accepted_ranges(), [(3000, synth.N)])

    def test_clipped_block_rejected(self):
        self.write(iq=synth.tone_sc16(synth.N, clip_at=5000), events=[])
        sv = load_capture(self.root).streams["rx0"]
        self.assertEqual(sv.accepted_ranges(), [(0, 4096), (8192, synth.N)])

    def test_pilot_off_ranges(self):
        ev = [{"seq": 0, "type": "PILOT_STATE", "timebase": "sdr0", "t_ns": 2_000_000, "enabled": False},
              {"seq": 1, "type": "PILOT_STATE", "timebase": "sdr0", "t_ns": 3_000_000, "enabled": True}]
        self.write(events=ev)
        self.assertEqual(load_capture(self.root).streams["rx0"].pilot_off, [(2000, 3000)])


class TestWitnessFreshness(Base):
    def test_fresh_stale_and_causal(self):
        self.write()
        cap = load_capture(self.root)
        w = cap.latest_witness(5_500_000, "sdr0", max_age_ns=4_000_000)
        self.assertEqual((w.reason, w.record["seq"]), ("OK", 0))
        self.assertEqual(w.age_ns, 5_500_000 - (2_000_000 - 500))  # conservative age
        self.assertEqual(cap.latest_witness(5_500_000, "sdr0", max_age_ns=1_000_000).reason, "STALE")
        # A witness at 6 ms +/- 0.5 us is not certainly in the past at 6.0002 ms.
        self.assertEqual(cap.latest_witness(6_000_200, "sdr0", 10**7).record["seq"], 0)
        self.assertEqual(cap.latest_witness(1_000_000, "sdr0", 10**7).reason, "NO_VALID_PRIOR_WITNESS")

    def test_time_discontinuity_voids_alignment(self):
        ev = [{"seq": 0, "type": "TIME_DISCONTINUITY", "timebase": "sdr0", "t_ns": 4_000_000,
               "stream_id": "rx0", "sample_index": 4000, "jump_ns": 50_000}]
        self.write(events=ev)
        cap = load_capture(self.root)
        self.assertEqual(cap.latest_witness(3_900_000, "sdr0", 10 ** 9).reason, "OK")
        # The witness at 6 ms (wit0) would map past the jump: refused, so the
        # newest usable record is the pre-jump one at 2 ms.
        self.assertIsNone(cap.map_time(6_000_000, "wit0", "sdr0"))
        self.assertEqual(cap.latest_witness(9_000_000, "sdr0", 10 ** 9).record["seq"], 0)

    def test_witness_without_alignment_is_unusable(self):
        m = synth.base_manifest()
        m["timebase_alignments"] = []
        self.write(manifest=m)
        cap = load_capture(self.root)
        self.assertEqual(cap.latest_witness(9_000_000, "sdr0", 10**9).reason, "NO_TIMEBASE_ALIGNMENT")

    def test_invalid_witness_records_skipped(self):
        wit = synth.base_witness()
        wit[1]["valid"] = False
        self.write(witness=wit)
        self.assertEqual(load_capture(self.root).latest_witness(9_000_000, "sdr0", 10**9).record["seq"], 0)

    def test_events_until_is_causal(self):
        self.write()
        cap = load_capture(self.root)
        self.assertEqual([e["seq"] for e in cap.events_until(5_000_000, "sdr0")], [0, 1])


if __name__ == "__main__":
    unittest.main()
