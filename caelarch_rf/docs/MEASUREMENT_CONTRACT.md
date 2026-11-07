# Gate 0.4 measurement contract (`caelarch.capture/0.4`)

**Status:** draft for team review. Backlog item P0.3. Implemented by
`gate04/contract.py` (schema) and `gate04/ingest.py` (loader). Conforming to
this contract says a capture is *well-formed*. It does not qualify an
instrument, a phase-transfer plane or a monitor.

The contract is device-agnostic on purpose. It fixes what any SDR adapter
must record. It does not depend on the B220mini or on any CelonixLab answer.
Fields a vendor reply will fill in are listed in §7.

## 1. Capture directory

```
capture/                     <- nothing else may be in here
  manifest.json              schema caelarch.capture/0.4
  rx0.sc16                   raw IQ, one file per stream
  events.jsonl               online control/status events
  witness.jsonl              independent R witness records (optional)
controller/                  <- separate location, separate access
  <capture_id>.truth.json    schema caelarch.sealed_truth/0.4 (offline only)
```

The loader **refuses** a capture that contains any undeclared file, symlink or
subdirectory, or whose files fail their SHA-256 in the manifest. Sealed truth
therefore cannot sit next to the data "for convenience".

## 2. Time semantics

* Every timestamp is an **integer nanosecond count on a named timebase**
  (`sdr_device`, `instrument`, `controller`). No floats, and no host wall
  clock for alignment (`created_utc` is informational only).
* For a stream, **sample index is the primary axis**. The time of sample `n`
  is `t0_ns + n·10⁹/fs`, computed exactly with rationals, in piecewise segments
  split at `OVERFLOW` / `TIME_DISCONTINUITY` events.
* Two timebases are compared **only** through a declared
  `timebase_alignments` entry (`offset_ns`, `uncertainty_ns`, method). The
  uncertainty widens every guard interval and every freshness age, always in
  the conservative direction.
* A shared 10 MHz/PPS is *not* an alignment by itself. Someone has to measure
  the offset and declare it with a method and an uncertainty.
* Alignments are **epochs**. An alignment holds from its optional
  `valid_from_ns` (on its `to` timebase; default −∞) until the first
  `TIME_DISCONTINUITY` on **either** clock after that. A cross-clock time
  maps only through an epoch that certainly contains it. After a jump,
  nothing maps until a **requalified** epoch is declared (a new alignment
  entry with `valid_from_ns`). An event that can't be placed for that reason
  rejects the stream from the discontinuity onward.
* An event time inside an overflow dropout gap resolves to the gap's
  boundary samples. A time in an untimed tail resolves to the tail's start.
  Neither ever falls back to sample 0.

## 3. Manifest fields

| Field | Meaning |
|---|---|
| `capture_id`, `created_utc`, `graph_version` | Identity; graph version pins plane definitions in `GATE0_4_ARCHITECTURE.md` |
| `provenance.kind` | `synthetic` or `bench_capture`. Synthetic captures cannot support hardware claims |
| `device` | model, serial, driver/UHD version, `fpga_image_sha256` |
| `clock` | `ref_source`, `pps_source`, `ref_locked_at_start` |
| `timebases`, `timebase_alignments` | §2 |
| `settling` | `switch_guard_ns`, `retune_guard_ns`, `gain_guard_ns`: minimum discard after each action |
| `streams[]` | `stream_id`, `channel`, `file`, `sha256`, `format` (`sc16_le`/`fc32_le`, UHD interleaved), `sample_count`, `sample_rate_hz`, `center_freq_hz`, `rx_gain_db`, `timebase`, `t0_ns`, `clip_level`, `carries` ⊆ {scene, reference, pilot, analog_monitor}, `pilot` {offset_hz relative to the *initial* `center_freq_hz`; follow `LO_RETUNE` events, waveform_id}, `monitor` {kind `analog_quadrature`, reference `independent`/`pilot_source`, detector_bandwidth_hz, tap_plane `P4`}, `reference_tone` {offset_hz of the known RF source tone, relative to the initial `center_freq_hz`} (operating metadata that lets an online method measure the switched reference). A stream carries `analog_monitor` iff it has the `monitor` block, and then carries nothing else |
| `events_file`/`events_sha256`, `witness_file`/`witness_sha256` | Sidecar files |
| `qualification_ref` | {record_id, record_sha256} of a signed physical qualification record, or null. **Not verified by this package** |
| `truth_commitment` | {scheme `sha256-salted-v1`, digest}, or null (null means the capture cannot be scored blind) |

All objects are **allowlists**. Unknown keys are refused at every level.
Identifiers, file names and labels have restricted character sets and
lengths, and are refused if they contain truth markers (`truth`, `sealed`,
`oracle`, `label`, `perturb`, ...). There are no free-text fields.

## 4. Online events (`events.jsonl`)

Common fields: `seq` (strictly increasing), `type`, `timebase`, `t_ns`
(non-decreasing per timebase).

| Type | Fields | Derived effect |
|---|---|---|
| `SWITCH_COMMAND` | `port` ∈ H/R/THRU/TERM | Selector-port timeline. Reject `[t−u, t+u+switch_guard]` |
| `LO_RETUNE` | `stream_id`, `new_center_freq_hz`, `lo_locked` | Reject retune guard. Record retune index (qualification stale). Unlocked LO rejects until a locked retune |
| `GAIN_CHANGE` | `stream_id`, `new_rx_gain_db` | Reject gain guard |
| `SETTLED` | `stream_id`, `cause` | Can **extend** the guard to the device-reported settle time. Never shortens it |
| `OVERFLOW` | `stream_id`, `sample_index`, `dropped_samples` or null | Known drop: new time segment, boundary samples rejected. **Unknown drop: every later sample is untimed and rejected** |
| `TIME_DISCONTINUITY` | `stream_id`, `sample_index`, `jump_ns` | New time segment. A backwards jump makes the rest of the stream untimed |
| `REF_LOCK` | `locked` | Reject while unlocked, **for streams on the reporting event's timebase only**: another device's lock state does not gate them. If it can't be ordered against such a stream, reject the whole stream. `clock.ref_locked_at_start` likewise applies to `sdr_device` timebases only |
| `PILOT_STATE` | `enabled` | Pilot-off ranges on pilot and analog-monitor streams (scene still usable) |

These are things the SDR host or scheduler knows at run time. The **blind
perturbation controller's actions are not an event type.** They exist only in
sealed truth. A switch/retune/gain event on a timebase with no alignment to the
affected stream is a contract error, because its effect can't be placed.

**Accepted samples** = all samples minus the union of rejected ranges, plus
any block (4096 samples) containing a clipped or non-finite sample. Samples
before the first `SWITCH_COMMAND` are accepted for pilot use but have selector
port `UNKNOWN`. They must not be used as scene or reference.

## 5. Witness records (`witness.jsonl`)

`seq`, `timebase`, `t_ns`, `instrument`, `plane_cal_id`, `ratio_re`,
`ratio_im` (W1/W0), `phase_std_rad`, `valid`.
`OnlineCapture.latest_witness(t, timebase, max_age_ns)` returns the newest
valid record that was **certainly** in the past (`t_w + u ≤ t`) **and lies in
the same alignment epoch as the decision** (for a same-timebase witness: the
same clock segment). A pre-jump witness is therefore never fresh after a
jump, however generous `max_age_ns` is. Its age is conservative
(`t − (t_w − u)`). The result reason is one of `OK`, `STALE`,
`NO_VALID_PRIOR_WITNESS`, `NO_TIMEBASE_ALIGNMENT`,
`ALIGNMENT_VOID_AFTER_DISCONTINUITY`, `NO_WITNESS_FILE`. A witness
without an alignment to the SDR timebase is never fresh.

## 6. Online evidence records (`gate04/records.py`)

This is the interface the Sprint 1 monitors must emit. `PilotObservation`
records the window start and end, and **`available_ns`**: the earliest time the
estimate existed, including extraction latency. Policy may use a record at
decision time `t` only if `available_ns ≤ t`. Acausal records are refused at
construction. Analog-monitor records must state the measured detector
bandwidth.

## 7. Sealed truth and its commitment

* The truth file (`caelarch.sealed_truth/0.4`) holds a ≥32-byte random salt,
  the controller timebase and the perturbation list (`target` ∈ the observation-graph
  impairment ids X, H, R, K, J, S, G, A, FG, FA, M, plus `t_ns`), and
  optionally a `generator` block (seed, clock model, fault notes).
* The manifest holds only `sha256(truth file bytes)`. The salt keeps the digest
  from being an oracle: without it, an online method could hash candidate
  schedules ("G step 0.3 rad at 120 ms") and confirm a guess.
* `sealed_truth.open_for_scoring()` verifies the digest and capture id. Only
  the offline scorer calls it, after online decisions are frozen. A unit test
  asserts that no online module imports `sealed_truth`.

## 8. Fields pending vendor confirmation (P0.4)

| Contract field | Needs from CelonixLab / bring-up |
|---|---|
| `device.driver`, `device.fpga_image_sha256` | Exact UHD release and FPGA image/source |
| `OVERFLOW.dropped_samples` | Whether the image reports drop counts or only `O` flags. If only flags, every overflow makes the rest of the stream untimed |
| `streams[].t0_ns` per channel and alignment of `rx0`/`rx1` | Evidence of timestamp-aligned 2-channel RX |
| `clock.*`, `REF_LOCK` | External 10 MHz/PPS lock reporting |
| `settling.*` | Measured switch/retune/gain settling (bench, not datasheet) |

## 9. Known gaps (deliberate, for review)

* No multi-hop timebase alignment. Only direct pairs count (chained
  uncertainty is not modelled).
* `sc16` clip detection uses a configured `clip_level`. ADC-level saturation
  that happens before digital scaling has to come from the device, and no
  event for it exists yet.
* The truth-marker deny-list is a backstop. The allowlist and the
  undeclared-file rule are the real guards.
* Encryption at rest of the truth file is a controller-host responsibility and
  is not implemented here.
