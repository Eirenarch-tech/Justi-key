"""External raw-I/Q adapter (used for public recordings). No network needed."""
import array
import contextlib
import io
import os
import sys
import tempfile
import unittest

from caelarch_rf.adapters.external_raw import convert, parse_rtl433_name, to_sc16
from caelarch_rf.gate04 import contract as C
from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.validate import main as validate_main
from caelarch_rf.tests import synth


def _ints(b):
    a = array.array("h")
    a.frombytes(b)
    if sys.byteorder != "little":
        a.byteswap()
    return list(a)


class TestConversion(unittest.TestCase):
    def test_name_parsing(self):
        self.assertEqual(parse_rtl433_name("tests/x/g001_433.92M_250k.cu8"), (433.92e6, 250e3, "cu8"))
        self.assertEqual(parse_rtl433_name("a_2359.3k.cs16".replace("a_", "a_912.6M_")), (912.6e6, 2359.3e3, "cs16"))
        with self.assertRaises(ValueError):
            parse_rtl433_name("gfile001.cu8")       # no rate/frequency: refuse, never guess

    def test_cu8_is_lossless_and_dc_free(self):
        self.assertEqual(_ints(to_sc16(bytes([0, 255, 127, 128]), "cu8")), [-32640, 32640, -128, 128])

    def test_cs8_and_cs16(self):
        self.assertEqual(_ints(to_sc16(bytes([0x80, 0x7F]), "cs8")), [-32768, 32512])
        raw = array.array("h", [1, -2, 300, -32768])
        if sys.byteorder != "little":
            raw.byteswap()
        self.assertEqual(to_sc16(raw.tobytes(), "cs16"), raw.tobytes())

    def test_partial_pair_refused(self):
        for fmt, n in (("cu8", 3), ("cs8", 1), ("cs16", 6)):
            with self.subTest(fmt=fmt), self.assertRaisesRegex(ValueError, "refusing to truncate"):
                to_sc16(bytes(n), fmt)


class TestCaptureFromRecording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _rec(self, data, fmt="cu8"):
        src = os.path.join(self.d, f"g001_433.92M_250k.{fmt}")
        with open(src, "wb") as f:
            f.write(data)
        cap = os.path.join(self.d, "cap")
        m, rec = convert(src, cap, 433.92e6, 250e3, fmt, "2026-11-08T03:00:24Z", "RTL-SDR class (cu8)")
        return cap, m, rec

    def test_loads_without_invented_metadata(self):
        cap, m, rec = self._rec(bytes([127, 128] * 9000))
        c = load_capture(cap)
        s = m["streams"][0]
        self.assertEqual(m["provenance"]["kind"], "external_recording")
        self.assertIsNone(s["rx_gain_db"])
        self.assertIsNone(m["truth_commitment"])
        self.assertIsNone(s["pilot"])
        self.assertEqual(c.streams["rx0"].n, 9000)
        self.assertEqual(c.streams["rx0"].accepted_ranges(), [(0, 9000)])
        self.assertEqual(len(rec["source_sha256"]), 64)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(validate_main([cap]), 0)
        self.assertIn("third-party recording", out.getvalue())

    def test_adc_rails_are_flagged_as_clipping(self):
        data = bytearray([127, 128] * 9000)
        data[2 * 5000] = 255                       # one I sample on the rail
        cap, _, _ = self._rec(bytes(data))
        sv = load_capture(cap).streams["rx0"]
        self.assertEqual(sv.accepted_ranges(), [(0, 4096), (8192, 9000)])

    def test_internal_reference_cannot_be_unlocked(self):
        m = synth.base_manifest()
        m["streams"][0]["sha256"] = m["events_sha256"] = m["witness_sha256"] = "0" * 64
        m["clock"]["ref_locked_at_start"] = False
        with self.assertRaisesRegex(C.ContractError, "internal reference"):
            C.validate_manifest(m)


if __name__ == "__main__":
    unittest.main()
