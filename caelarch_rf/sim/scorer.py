"""Sprint 1e blind offline scorer. OFFLINE ONLY.

Protocol (freeze before reveal):
1. ``gate04.evaluate.freeze`` writes every online output; the caller records
   its SHA-256 at that moment.
2. ``score(frozen_path, truth_root, expected_sha256)`` re-hashes the frozen
   file and refuses to proceed on any mismatch, BEFORE opening any truth.
   Truth is then opened per capture via ``sealed_truth.open_for_scoring``,
   which verifies each salted commitment carried in the frozen file.

Predeclared event-matching policy (``MatchPolicy``, digest in the report):
* an event = scheduled disturbances sharing an onset; its match window is
  [onset, end + max_latency(detector)];
* the earliest unclaimed alarm whose availability falls in the window is the
  ONE credited detection; further alarms in the window are duplicates (no
  credit, not false alarms);
* alarms outside every event window and outside announced acquisition
  changes (retune, gain step, unlock) are false alarms.

Every event stays in the denominator. Categories are reported separately:
DETECTED, MISSED, MISSED_BELOW_FLOOR (projection below the causal floor at
onset), UNMONITORED (onset outside the detector's TRACKING coverage),
UNOBSERVABLE_STRUCTURAL (zero projection on the detector's sensor, including
cancellations), UNOBSERVABLE_MODEL (intersample pulses for point-sampled
detectors; pulses entirely between reference dwells for the bracket).
Rates are given over all events, over observable events, and over observable
and monitored events, with capture-clustered bootstrap CIs. Zero false alarms
is reported with an exact one-sided Poisson upper bound on the rate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from caelarch_rf.gate04 import sealed_truth
from caelarch_rf.gate04.observability import SENSORS

from . import generator as G


@dataclass(frozen=True)
class MatchPolicy:
    version: str = "match-policy/0.1"
    max_latency_ns: Tuple[Tuple[str, int], ...] = (("sampled_digital", 1_000_000),
                                                   ("analog_quadrature", 1_000_000),
                                                   ("reference_bracket", 8_000_000))
    announced_margin_ns: int = 1_000_000
    alpha: float = 0.05
    bootstrap: int = 2000
    bootstrap_seed: int = 20261107
    projection_epsilon_rad: float = 1e-9

    def latency(self, kind):
        return dict(self.max_latency_ns)[kind]

    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------- statistics

def wilson(k, n, alpha=0.05):
    if n == 0:
        return (None, None)
    z = 1.959963984540054 if abs(alpha - 0.05) < 1e-12 else _z(1 - alpha / 2)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _z(q):
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def poisson_upper(k, alpha=0.05):
    """Exact one-sided upper bound on a Poisson mean given k observed counts."""
    def cdf(lam):
        term, s = math.exp(-lam), math.exp(-lam)
        for i in range(1, k + 1):
            term *= lam / i
            s += term
        return s
    lo, hi = 0.0, max(10.0, 5.0 * (k + 1))
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def cluster_bootstrap(per_cluster: List[Tuple[float, float]], policy: MatchPolicy):
    """Percentile CI of sum(num)/sum(den) resampling clusters (captures)."""
    if not per_cluster or sum(d for _, d in per_cluster) == 0:
        return (None, None)
    rng = random.Random(policy.bootstrap_seed)
    vals = []
    n = len(per_cluster)
    for _ in range(policy.bootstrap):
        num = den = 0.0
        for _ in range(n):
            a, b = per_cluster[rng.randrange(n)]
            num += a
            den += b
        if den > 0:
            vals.append(num / den)
    vals.sort()
    lo = vals[int(policy.alpha / 2 * len(vals))]
    hi = vals[min(len(vals) - 1, int((1 - policy.alpha / 2) * len(vals)))]
    return (lo, hi)


def cluster_bootstrap_diff(pairs: List[Tuple[float, float, float]], policy: MatchPolicy):
    """CI of (sum a - sum b)/sum den, resampling clusters, for paired detectors."""
    if not pairs or sum(d for _, _, d in pairs) == 0:
        return (None, None)
    rng = random.Random(policy.bootstrap_seed + 1)
    vals, n = [], len(pairs)
    for _ in range(policy.bootstrap):
        a = b = d = 0.0
        for _ in range(n):
            x, y, z = pairs[rng.randrange(n)]
            a, b, d = a + x, b + y, d + z
        if d:
            vals.append((a - b) / d)
    vals.sort()
    return (vals[int(policy.alpha / 2 * len(vals))],
            vals[min(len(vals) - 1, int((1 - policy.alpha / 2) * len(vals)))])


# ------------------------------------------------------------------- truth model

class _TruthClock:
    def __init__(self, gen):
        c = gen["clock"]
        self.eps, self.t0 = c["eps"], c["t0_dev_ns"]
        self.T = 1e9 / gen["scenario"]["fs_hz"]
        self.jump = gen.get("jump")
        mc = gen.get("monitor_clock")
        self.mon_t0 = mc["t0_dev_ns"] if mc else None

    def to_phys(self, dev, timebase):
        if timebase == "mon0":
            return (dev - self.mon_t0) * (1 + self.eps)
        if self.jump and dev >= self.t0 + self.jump["orig_index"] * self.T + self.jump["jump_ns"]:
            dev -= self.jump["jump_ns"]
        return (dev - self.t0) * (1 + self.eps)


def _delta(d, sensor, term):
    kind, v = d["kind"], d["value"]
    if kind == "random_walk":
        return v * math.sqrt(d["duration_ns"] * 1e-9)
    if kind == "delay_step":
        scene_side = sensor in ("reference", "scene")
        rf = G.SCENE_RF_HZ if scene_side else G.PILOT_RF_HZ
        f = rf if term == "G" else rf - G.CENTER0_HZ
        return -2 * math.pi * f * v * 1e-9
    if kind == "freq_offset":
        return 2 * math.pi * v * 1e-3   # phase accrued per ms, as a scale
    return v


def _wrap(x):
    return (x + math.pi) % (2 * math.pi) - math.pi


def _projection(members, sensor):
    """Net phase change seen by ``sensor``, WRAPPED: a change of 2*pi*k is invisible."""
    return _wrap(sum(SENSORS[sensor].c(d["target"]) * _delta(d, sensor, d["target"]) for d in members))


def _end(d, duration):
    if d["kind"] in ("pulse", "ramp", "random_walk") or (d["kind"] == "sine" and d["duration_ns"]):
        return d["t_ns"] + d["duration_ns"]
    if d["kind"] == "sine":
        return duration
    return d["t_ns"]


def _events(truth):
    gen = truth["generator"]
    dur = gen["scenario"]["duration_ns"]
    groups: Dict[int, list] = {}
    for d in truth["events"]:
        if d.get("cause") == "scheduled":
            groups.setdefault(d["t_ns"], []).append(d)
    out = []
    for t0 in sorted(groups):
        ms = groups[t0]
        out.append({"onset": t0, "end": max(_end(d, dur) for d in ms), "members": ms,
                    "targets": sorted({d["target"] for d in ms}), "case": gen["case"]})
    return out


def _announced(truth):
    dur = truth["generator"]["scenario"]["duration_ns"]
    return [(d["t_ns"], _end(d, dur), d["target"]) for d in truth["events"]
            if d.get("cause") not in ("scheduled", "background")]


def _r_dwells(truth):
    sw = truth["generator"]["switches"]
    dur = truth["generator"]["scenario"]["duration_ns"]
    return [(s["t"], (sw[i + 1]["t"] if i + 1 < len(sw) else dur)) for i, s in enumerate(sw) if s["port"] == "R"]


def _union_len(iv):
    tot, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(iv):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                tot += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        tot += cur_e - cur_s
    return tot


def _subtract(iv, cuts):
    out = []
    for s, e in iv:
        segs = [(s, e)]
        for cs, ce in cuts:
            nxt = []
            for a, b in segs:
                if ce <= a or cs >= b:
                    nxt.append((a, b))
                else:
                    if cs > a:
                        nxt.append((a, cs))
                    if ce < b:
                        nxt.append((ce, b))
            segs = nxt
        out += segs
    return out


# ------------------------------------------------------------------------ scoring

def _score_detector(kind, det, truth, clk, events, policy, sensor):
    tb = det["timebase"]
    T = 1e9 / truth["generator"]["scenario"]["fs_hz"]
    lat = policy.latency(kind)
    alarms = sorted(clk.to_phys(e["available_ns"], tb) for e in det["excursions"])
    claimed = [False] * len(alarms)
    mon = [(clk.to_phys(s, tb), clk.to_phys(e, tb)) for s, e, _ in det["monitored_intervals"] if e is not None]
    tracking = [(clk.to_phys(s, tb), clk.to_phys(e, tb), f) for s, e, _, f in det["tracking"]]
    dwells = _r_dwells(truth)
    rows = []
    windows = []
    for ev in events:
        p = _projection(ev["members"], sensor)
        w0, w1 = ev["onset"], ev["end"] + lat
        windows.append((w0, w1))
        cat, latency, dup = None, None, 0
        hits = [i for i, a in enumerate(alarms) if w0 <= a <= w1]
        if abs(p) < policy.projection_epsilon_rad:
            cat = "UNOBSERVABLE_STRUCTURAL"
        elif kind in ("sampled_digital", "reference_bracket") and all(
                d["kind"] == "pulse" and d["duration_ns"] < T for d in ev["members"]):
            cat = "UNOBSERVABLE_MODEL"
        elif kind == "reference_bracket" and all(
                d["kind"] == "pulse" and not any(a < d["t_ns"] + d["duration_ns"] and d["t_ns"] < b
                                                 for a, b in dwells) for d in ev["members"]):
            cat = "UNOBSERVABLE_MODEL"
        free = [i for i in hits if not claimed[i]]
        if cat is None and free:
            cat, latency = "DETECTED", alarms[free[0]] - ev["onset"]
        for i in hits:
            if not claimed[i]:
                claimed[i] = True
                dup += 1
        dup = max(0, dup - (1 if cat == "DETECTED" else 0))
        if cat is None:
            if not any(s <= ev["onset"] <= e for s, e in mon):
                cat = "UNMONITORED"
            else:
                # Causal floor of the first TRACKING block that could have seen the onset.
                fl = [f for s, e, f in sorted(tracking) if e >= ev["onset"]]
                cat = "MISSED_BELOW_FLOOR" if fl and fl[0] is not None and abs(p) < fl[0] else "MISSED"
        rows.append({"onset": ev["onset"], "case": ev["case"], "targets": ev["targets"],
                     "projection_rad": p, "category": cat, "latency_ns": latency, "duplicates": dup})
    announced = [(s, e + policy.announced_margin_ns) for s, e, _ in _announced(truth)]
    fa = [a for i, a in enumerate(alarms) if not claimed[i] and not any(s <= a <= e for s, e in announced)]
    exposure = _union_len(_subtract(mon, windows + announced))
    return {"rows": rows, "false_alarms": len(fa), "exposure_ns": exposure,
            "monitored_ns": _union_len(mon)}


ABRUPT = ("step", "pulse", "delay_step")


def _window_u_change(ev, w0, w1, T):
    """(class, |change of U=G+A| attributable to this event inside [w0, w1))."""
    mag, cls = 0.0, "gradual"
    for d in ev["members"]:
        if d["target"] not in ("G", "A"):
            continue
        if d["kind"] == "pulse" and d["duration_ns"] < T:
            if d["t_ns"] < w1 and d["t_ns"] + d["duration_ns"] > w0:
                mag, cls = max(mag, abs(d["value"])), "intersample"
            continue
        full = abs(_wrap(_delta(d, "scene", d["target"])))
        k = d["kind"]
        if k in ABRUPT:
            if w0 <= d["t_ns"] < w1 or (k == "pulse" and d["t_ns"] < w1 and d["t_ns"] + d["duration_ns"] > w0):
                mag, cls = max(mag, full), "abrupt"
        elif k == "ramp":
            ov = max(0.0, min(w1, d["t_ns"] + d["duration_ns"]) - max(w0, d["t_ns"]))
            mag = max(mag, full * ov / d["duration_ns"])
        elif k == "random_walk":
            ov = max(0.0, min(w1, d["t_ns"] + d["duration_ns"]) - max(w0, d["t_ns"]))
            mag = max(mag, abs(d["value"]) * math.sqrt(ov * 1e-9))
        elif k == "sine":
            mag = max(mag, min(2 * full, 2 * math.pi * d["freq_hz"] * full * (w1 - w0) * 1e-9))
    return cls, mag


def _score_guard(decisions, truth, clk, events, announced):
    out = []
    for g in decisions:
        w0, w1 = clk.to_phys(g["t_start_ns"], "sdr0"), clk.to_phys(g["t_end_ns"], "sdr0")
        active = [ev for ev in events if (ev["onset"] < w1 and ev["end"] >= w0) and
                  (ev["end"] > ev["onset"] or w0 <= ev["onset"] < w1)]
        cls, mag = "none", 0.0
        for ev in active:
            c, m = _window_u_change(ev, w0, w1, clk.T)
            # Precedence when several events share a window: abrupt > gradual > intersample.
            rank = {"none": 0, "intersample": 1, "gradual": 2, "abrupt": 3}
            if m > 1e-9 and rank[c] >= rank[cls]:
                cls = c
            mag = max(mag, m)
        quiet = not active and not any(s < w1 and e >= w0 for s, e, _ in announced)
        # True causes include announced acquisition-induced changes (e.g. a retune's
        # A phase reset): they are physical causes even though they are not scored events.
        truths = sorted({t for ev in active for t in ev["targets"]} |
                        {tgt for s, e, tgt in announced if s < w1 and e + 300_000 >= w0})
        out.append({"state": g["state"], "hazard": mag > 1e-9, "hazard_class": cls, "hazard_rad": mag,
                    "quiet": quiet, "detected": g["detected"], "unique": g["unique_attribution"],
                    "consistent": g["consistent_single_causes"], "true_targets": truths})
    return out


def score(frozen_path, truth_root, expected_sha256, policy: MatchPolicy = MatchPolicy()) -> dict:
    with open(frozen_path, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise ValueError("frozen online outputs changed after freezing; refusing to reveal truth")
    frozen = json.loads(data)
    per_det: Dict[str, list] = {}
    per_guard: Dict[str, list] = {}
    for cap in frozen["captures"]:
        truth = sealed_truth.open_for_scoring(
            {"capture_id": cap["capture_id"], "truth_commitment": cap["truth_commitment"]},
            os.path.join(truth_root, cap["capture_id"] + ".truth.json"))
        clk = _TruthClock(truth["generator"])
        events = _events(truth)
        for kind, det in cap["detectors"].items():
            r = _score_detector(kind, det, truth, clk, events, policy, det["sensor"])
            r["capture_id"] = cap["capture_id"]
            per_det.setdefault(kind, []).append(r)
        ann = _announced(truth)
        for active, decisions in cap["guard"].items():
            per_guard.setdefault(active, []).append(_score_guard(decisions, truth, clk, events, ann))
    report = {"frozen_sha256": digest, "policy": asdict(policy), "policy_sha256": policy.digest(),
              "n_captures": len(frozen["captures"]), "detectors": {}, "guard": {}, "paired": {}}
    for kind, caps in per_det.items():
        report["detectors"][kind] = _aggregate(caps, policy)
    kinds = sorted(per_det)
    for i, a in enumerate(kinds):
        for b in kinds[i + 1:]:
            pairs = []
            for ca, cb in zip(per_det[a], per_det[b]):
                da = sum(r["category"] == "DETECTED" for r in ca["rows"])
                db = sum(r["category"] == "DETECTED" for r in cb["rows"])
                pairs.append((da, db, len(ca["rows"])))
            n = sum(p[2] for p in pairs)
            report["paired"][f"{a} - {b}"] = {
                "pd_difference_all_events": (sum(p[0] for p in pairs) - sum(p[1] for p in pairs)) / n if n else None,
                "ci_cluster_bootstrap": cluster_bootstrap_diff(pairs, policy), "events": n}
    for active, caps in per_guard.items():
        report["guard"][active] = _aggregate_guard(caps, policy)
    return report


def _aggregate(caps, policy):
    cats = ["DETECTED", "MISSED", "MISSED_BELOW_FLOOR", "UNMONITORED", "UNOBSERVABLE_STRUCTURAL",
            "UNOBSERVABLE_MODEL"]
    counts = {c: sum(r["category"] == c for cap in caps for r in cap["rows"]) for c in cats}
    total = sum(counts.values())
    obs = total - counts["UNOBSERVABLE_STRUCTURAL"] - counts["UNOBSERVABLE_MODEL"]
    obs_mon = obs - counts["UNMONITORED"]

    def rate(den_fn):
        per = [(sum(r["category"] == "DETECTED" for r in cap["rows"]), den_fn(cap)) for cap in caps]
        k, n = sum(a for a, _ in per), sum(b for _, b in per)
        return {"pd": k / n if n else None, "k": k, "n": n, "wilson": wilson(k, n, policy.alpha),
                "ci_cluster_bootstrap": cluster_bootstrap(per, policy)}

    def n_obs(cap):
        return sum(r["category"] not in ("UNOBSERVABLE_STRUCTURAL", "UNOBSERVABLE_MODEL") for r in cap["rows"])

    def n_obs_mon(cap):
        return sum(r["category"] not in ("UNOBSERVABLE_STRUCTURAL", "UNOBSERVABLE_MODEL", "UNMONITORED")
                   for r in cap["rows"])
    lats = sorted(r["latency_ns"] for cap in caps for r in cap["rows"] if r["latency_ns"] is not None)
    fa = sum(c["false_alarms"] for c in caps)
    expo_min = sum(c["exposure_ns"] for c in caps) / 6e10
    by_case: Dict[str, Dict[str, int]] = {}
    for cap in caps:
        for r in cap["rows"]:
            d = by_case.setdefault(r["case"], {c: 0 for c in cats})
            d[r["category"]] += 1
    return {
        "events": total, "counts": counts, "observable": obs, "observable_monitored": obs_mon,
        "pd_all_events": rate(lambda cap: len(cap["rows"])),
        "pd_observable": rate(n_obs),
        "pd_observable_monitored": rate(n_obs_mon),
        "duplicates": sum(r["duplicates"] for cap in caps for r in cap["rows"]),
        "latency_ns": {"n": len(lats), "mean": sum(lats) / len(lats) if lats else None,
                       "p95": lats[min(len(lats) - 1, int(0.95 * len(lats)))] if lats else None,
                       "jitter_std": (math.sqrt(sum((x - sum(lats) / len(lats)) ** 2 for x in lats) / len(lats))
                                      if lats else None)},
        "false_alarms": {"count": fa, "exposure_minutes": expo_min,
                         "rate_per_minute": fa / expo_min if expo_min else None,
                         "upper_bound_per_minute": poisson_upper(fa, policy.alpha) / expo_min if expo_min else None,
                         "note": "exact one-sided Poisson bound on monitored exposure outside event windows"},
        "monitored_fraction": None,
        "by_case": by_case,
    }


def _aggregate_guard(caps, policy):
    flat = [w for cap in caps for w in cap]

    def prop(pred_num, pred_den):
        per = [(sum(1 for w in cap if pred_den(w) and pred_num(w)), sum(1 for w in cap if pred_den(w)))
               for cap in caps]
        k, n = sum(a for a, _ in per), sum(b for _, b in per)
        return {"rate": k / n if n else None, "k": k, "n": n, "wilson": wilson(k, n, policy.alpha),
                "ci_cluster_bootstrap": cluster_bootstrap(per, policy)}
    states = {}
    for w in flat:
        states[w["state"]] = states.get(w["state"], 0) + 1
    det_haz = [w for w in flat if w["detected"] and w["true_targets"]]
    return {
        "windows": len(flat), "states": states,
        "unsafe_conditional_given_U_change": prop(lambda w: w["state"] == "CONDITIONAL", lambda w: w["hazard"]),
        # Stratified: abrupt changes well inside / near / below a typical floor, and
        # gradual drift (which frequency tracking absorbs by construction).
        "unsafe_conditional_by_stratum": {
            name: prop(lambda w: w["state"] == "CONDITIONAL", pred) for name, pred in (
                ("abrupt >= 0.4 rad", lambda w: w["hazard_class"] == "abrupt" and w["hazard_rad"] >= 0.4),
                ("abrupt 0.15-0.4 rad", lambda w: w["hazard_class"] == "abrupt" and 0.15 <= w["hazard_rad"] < 0.4),
                ("abrupt < 0.15 rad", lambda w: w["hazard_class"] == "abrupt" and w["hazard_rad"] < 0.15),
                ("gradual (any size)", lambda w: w["hazard_class"] == "gradual" and w["hazard"]),
                ("intersample (model-unobservable)", lambda w: w["hazard_class"] == "intersample"))},
        "conditional_availability_quiet": prop(lambda w: w["state"] == "CONDITIONAL", lambda w: w["quiet"]),
        "valid_bounded_issued": sum(w["state"] == "VALID_BOUNDED" for w in flat),
        "unique_attributions_issued": sum(1 for w in flat if w["unique"]),
        "detected_event_windows": len(det_haz),
        "consistent_set_contains_a_true_cause": (sum(1 for w in det_haz if set(w["consistent"]) & set(w["true_targets"]))
                                                 / len(det_haz) if det_haz else None),
        "mean_consistent_set_size": (sum(len(w["consistent"]) for w in det_haz) / len(det_haz) if det_haz else None),
    }
