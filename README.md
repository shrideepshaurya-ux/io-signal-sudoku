# IO Signal Sudoku — DeltaV IO allocation prototype

Allocates field signals from an IO Master Database (Excel) to
**Cabinet → CIOC → Baseplate → CHARM slot**, validates the result independently,
raises Technical Queries (TQs) for anything that cannot be resolved safely,
exports Excel reports and compares database revisions.

Runs fully locally (no API keys, no network). The allocation engine is a
deterministic constraint model (Google OR-Tools CP-SAT), not a neural network.

> **Status:** working prototype. No official hackathon IO MDB or output template
> was present in the workspace, so everything below was exercised on **synthetic**
> data (`src/sample_data.py`). Hardware compatibility and naming defaults are
> **provisional** (see *Limitations*).

## Requirements
* Python 3.11+ (developed/tested on 3.13), Windows/macOS/Linux
* Packages in `requirements.txt` (streamlit, pandas, openpyxl, ortools, pydantic, plotly, pytest, psutil)

## Install
```powershell
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Run
```powershell
streamlit run app.py
```
Pages: Overview · Upload & Validate · Rule Configuration · Allocation · Reports · Revision Comparison.

### Typical workflow
1. **Upload & Validate** — upload one or more `.xlsx/.xlsm` MDBs (or click *Load SYNTHETIC demo MDB*).
   Pick the worksheet and header row, check/fix the auto-detected column mapping, read the
   validation report (errors vs. warnings) and download it.
2. **Rule Configuration** — hardware limits, spare capacity, grouping, A/B segregation, cable policy,
   naming patterns, CHARM/terminal-block matrix, IO-type aliases, solver weights. *Apply*, then save/load
   JSON profiles (`config/*.json`).
3. **Allocation** — optionally upload the official output template, *Run allocation*, inspect status,
   unallocated signals, TQs and independent validation, then *Prepare Excel workbook* → download.
4. **Reports** — cabinet/CIOC/baseplate utilisation, spare capacity, cable blocks, violations,
   per-signal inspector ("why is it here?").
5. **Revision Comparison** — compare the current allocation (or an uploaded Rev A, or a previously
   exported workbook) with Rev B; Rev B is re-allocated with movement penalised and unchanged
   positions pinned. Download the Rev B workbook with revision sheets.

### Command line
```powershell
python -m src.sample_data      # writes synthetic workbooks to ./samples
python -m src.demo             # allocates synthetic Rev A, compares Rev B, writes ./outputs/*.xlsx
```

## Tests
```powershell
pytest                    # 29 fast tests (~75 s)
pytest -m benchmark       # 10,000-signal benchmark, writes outputs/benchmark_10000.json
```

## Architecture
| File | Role |
|---|---|
| `src/models.py` | Pydantic rule configuration (single place for hardware limits) + runtime dataclasses |
| `src/hardware.py` | Hardware hierarchy geometry, spare-baseplate layout, naming |
| `src/excel_reader.py` | Sheet/header/column detection, manual mapping, normalisation, input validation |
| `src/cable_model.py` | Cable blocks (ordered pairs, spares) shared by reader/allocator/validator |
| `src/rule_engine.py` | Rule registry (`@register`); each rule has id, name, description, hard/pref, severity, check() |
| `src/allocator.py` | Prechecks → greedy hint → CP-SAT → exact slot layout → result |
| `src/validator.py` | Independent re-check of any allocation |
| `src/tq_generator.py` | TQs from findings |
| `src/report_generator.py` | Template-aware Excel export + report sheets |
| `src/revision_diff.py` | Tag matching, change classes, move reasons, TQ delta |
| `src/utilization.py` | Cabinet/CIOC/baseplate/cable-spare metrics |
| `src/sample_data.py`, `src/demo.py` | Synthetic data and headless demo |

### Algorithm
1. **Prechecks** (deterministic, produce specific TQs): missing/invalid fields, unknown IO type or CHARM
   mapping, duplicate tags/pairs, pair outside cable size, A/B conflict inside one cable, cable or group
   larger than hardware allows.
2. **Blocks**: pairs of one cable form an ordered block that must stay contiguous in one CIOC;
   stand-alone signals are one-slot blocks.
3. **Greedy** first-fit placement gives a warm start and a fallback.
4. **CP-SAT** assigns blocks to CIOCs. Hard: CIOC capacity (minus reserved spare baseplates), A/B
   segregation (CIOC or cabinet scope), hard grouping (cabinet/CIOC). Objective (lexicographic-style
   weights, configurable): unplaced slots ≫ movement vs previous revision > cabinets > CIOCs > group spread.
5. **Layout** places blocks in slot order inside each CIOC (groups adjacent, pair order kept, previous
   positions pinned when still valid). Capacity is charged conservatively so layout cannot fail.
6. **Validation** recomputes every rule from the final placements. A result is "fully valid" only
   when there are no violations *and* every signal is allocated.

Result status: `OPTIMAL` / `FEASIBLE` (all eligible signals placed; proven or not),
`INFEASIBLE` (solver proved a complete placement does not exist; partial returned),
`UNKNOWN` (not placed within solver limits — no infeasibility claim), `INVALID_INPUT` (nothing eligible).
Status is about *eligible* signals; records excluded for input errors appear as TQs.

### Interpretations made where the brief was ambiguous (all configurable)
* **Grouping**: default = same cabinet, hard; sequence checked as a warning. Options: CIOC scope, soft mode.
* **Segregation**: only designations in a configured conflict pair (default A/B) are separated; scope CIOC (or cabinet).
* **Cable continuity**: `linear` = consecutive slots, may cross baseplates within one CIOC (so a 24-pair
  cable can span two baseplates); `baseplate_aligned` = ≤1 baseplate cables stay in one baseplate, larger ones
  start on a baseplate boundary (charged as whole baseplates → conservative).
* **Cable spares**: `compact` = only used pairs take slots, unused pairs are reported as spares;
  `reserve_positions` = pair *n* sits at offset *n−1* and unused positions stay empty.
  Cable size = declared column if present, else the smallest of 2/6/12/24 covering the highest pair.
* **Spare capacity**: N whole empty baseplates per used cabinet (`ceil(fraction × 32)` by default = 4),
  taken from the highest-numbered CIOCs. `soft`: relax only when `max_cabinets` leaves no room and report
  the shortfall; `hard`: leave signals unallocated.
* **Duplicate tags**: all rows with the tag are excluded and queried (`keep_first` available).
* Cable ID and pair are mandatory (switchable). Signals lacking them are queried, never invented.

## Measured results (this machine, synthetic data)
* `pytest`: **29 passed** (`pytest -m benchmark`: 1 passed).
* 10,000 signals / 1,349 cable blocks (`outputs/benchmark_10000.json`): reading ≈ 3 s (the benchmark's
  "read_s" also includes generating and writing the synthetic workbook), allocation 68.5 s
  (greedy 0.06 s + CP-SAT 60 s limit), validation 0.26 s, RSS ≈ 170 MB.
  Result `FEASIBLE`, complete, 30 cabinets (= capacity lower bound with 4 spare baseplates per cabinet),
  0 violations, 132 group-sequence *warnings* (groups split over CIOCs with foreign blocks between).
  CP-SAT did **not** prove optimality within the limit; the objective bound is weak, so no optimality is claimed.
* Revision demo (synthetic): 10 added, 2 deleted, 2 modified, 0 reallocated, all 66 unchanged signals kept
  their exact slot.

## Limitations and unverified assumptions
* **No real MDB/template was available.** Header aliases and the template matcher are heuristics; real
  files may need manual mapping. Template filling writes values under the detected header row and keeps
  styles/merged cells, but untested against the real template. `.xlsm` is read, but exported as `.xlsx`
  (macros dropped, never executed).
* **CHARM / terminal-block mappings are the examples from the problem statement only** and are marked
  `provisional`; no manufacturer part numbers, terminal numbering or per-baseplate CHARM restrictions
  are modelled. Default naming patterns are placeholders.
* Cable spare pairs of cables with excluded (invalid) rows are computed from rows present in the MDB.
* A/B scope baseplate-level segregation, per-slot terminal numbering, redundancy pairing and
  arbitrary user-authored rules are **not** implemented; new rules need Python (`rule_engine.py`, and
  `allocator.py` if the solver must enforce them).
* At 10k signals CP-SAT returns a feasible, not proven-optimal, solution within 60 s; bigger models
  (> `solver.max_model_vars`) fall back to greedy only and are reported as `FEASIBLE` via the greedy method.
* Group-sequence ordering across CIOCs inside a cabinet is not optimised (warnings only).
* Streamlit runs the solve synchronously (spinner, no incremental progress).
* Spreadsheets are read with `data_only=True`; formulas without cached values read as empty and are
  therefore queried as missing data. Strings beginning with `=` are exported as text.
