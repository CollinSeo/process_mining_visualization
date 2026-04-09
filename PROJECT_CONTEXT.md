# Process Mining Visualization Project Context

## Overview

This project is a Python-based process mining visualization tool that builds a Directly-Follows Graph (DFG) from an event log CSV.

It now supports two execution modes:
- CLI generation via `dfg_visualizer.py`
- GUI/web generation via `web_app.py` (Streamlit)

It produces two outputs:
- A static PNG DFG rendered with `matplotlib` + `networkx`
- An interactive HTML DFG rendered with `pyvis` and then post-processed with custom JavaScript controls

The core mining and rendering logic still lives in `dfg_visualizer.py`, but `web_app.py` is now the main user-facing entry point for interactive use.

## Session Progress Snapshot

The following major changes were completed in this session:

1. Runtime and dependency setup
- `py -3` is available and was used for all verification
- dependencies were installed and verified
- `requirements.txt` is now pinned to verified versions, including `streamlit`

2. Event log parsing improvements
- timestamp parsing now uses `pd.to_datetime(..., utc=True)`
- this specifically fixed `open_event_log_receipt.csv`, which contains mixed timezone timestamps

3. Replay model redesign
- old replay behavior was step/event-index based and too slow to be useful
- replay is now continuous absolute-time replay
- tokens for multiple cases move concurrently on the same global time axis
- replay speed is adjustable and no longer tied to one-event-per-step playback
- replay now explicitly includes `[START]` and `[END]` so tokens start at the start node and finish at the end node

4. HTML performance optimization
- the biggest HTML bottleneck was the inline replay event payload
- replay data was compacted from verbose event objects into a smaller case/activity/time structure
- `receipt_dfg.html` size dropped from about `1.88 MB` to about `309 KB`

5. HTML interaction controls expanded
- complexity controls retained
- visual scaling controls added for:
  - node size
  - node font size
  - edge width
  - edge label size
- the control panel can now be collapsed/expanded with a `Hide` / `Show` toggle to reduce overlap with the model view
- replay controls now include:
  - play/pause
  - reset
  - timeline slider
  - speed options `0.1x`, `0.3x`, `0.5x`, `1x`, `2x`, `4x`, `8x`

6. Visual readability improvements
- nodes were made larger in both PNG and HTML renderers
- long activity labels are wrapped into two lines
- node label font sizes were increased
- after that, the default initial HTML node size and node font size were tuned back down to a more moderate level so the first view is less visually heavy
- default node sizing was later reduced again to lower node overlap in the initial view, especially on dense logs
- frequency-based node color shading was refined from a simpler linear blue interpolation to a more discriminative scale so mid/low frequency nodes are visually easier to distinguish

7. Web app conversion
- a Streamlit GUI was added in `web_app.py`
- users can upload a CSV, pick columns, configure options, generate results, preview PNG/HTML, and download outputs

8. Verification completed
- CLI generation was re-run successfully for:
  - `sample_event_log.csv`
  - `open_event_log_receipt.csv`
- Streamlit startup was verified successfully

9. HTML performance optimization for large logs
- filtering on large logs such as `receipt` could still feel slow because the browser recalculated too much work on every slider move
- applied optimizations while keeping behavior unchanged:
  - debounced complexity filter application for slider drag events
  - bulk node position lookup per render instead of repeated per-token lookups
  - replay cursor caching per case so continuous replay does not binary-search every trace on every frame
  - hidden token nodes are no longer created unnecessarily during replay/filter refresh

10. Additional large-log usability improvements
- replay now iterates the started-case set instead of always looping through every case on every frame
- a visible `Applying filters...` status was added during expensive filter updates
- a `Large log mode` toggle was added to the HTML controls
  - this is automatically enabled for sufficiently large datasets
  - it starts the graph in a lighter initial view by using stricter initial filtering and slightly reduced visual scale defaults

11. Major replay rendering optimization
- replay tokens are no longer rendered as `vis-network` nodes
- tokens are now drawn on a dedicated canvas overlay above the graph
- this removes large `nodes.update(...)` churn during replay and is especially important for logs with many simultaneous active cases
- the overlay is redrawn on replay frames and on graph viewport changes such as zoom/drag/resize

## Core Files

- `dfg_visualizer.py`: main mining, graph building, rendering, and CLI entry point
- `web_app.py`: Streamlit web UI for CSV upload, column mapping, option selection, preview, and download
- `requirements.txt`: pinned Python dependencies for mining, rendering, and web UI
- `sample_event_log.csv`: small synthetic sample log with columns `case_id`, `activity`, `timestamp`
- `open_event_log_running_example.csv`: small public-style event log example
- `open_event_log_receipt.csv`: large receipt event log used for a more realistic DFG
- `dfg.html`, `dfg.png`: generated visualization outputs
- `dfg_sample.html`, `dfg_sample.png`: generated output for the sample log
- `receipt_dfg.html`, `receipt_dfg.png`: generated output for the receipt log
- `lib/vis-9.1.2/*`, `lib/tom-select/*`, `lib/bindings/utils.js`: local frontend assets shipped with generated HTML or kept for local support

## Main Flow In `dfg_visualizer.py`

1. Parse CLI arguments in `parse_args()`
2. Load CSV with `pandas`
3. Normalize and validate the working log in `_prepare_work_log()`
4. Optionally auto-recommend complexity defaults in `_recommend_complexity_filters()`
5. Build the filtered DFG for PNG output with `build_dfg()`
6. Build the full DFG for HTML output with `build_dfg(... min_edge_count=1, max_activities=0)`
7. Build replay events from chronological log order with `_build_replay_events()`
8. Render static image with `draw_dfg()`
9. Render interactive HTML with `draw_dfg_interactive_html()`

## Main Flow In `web_app.py`

1. User uploads a CSV or selects a bundled sample CSV
2. User maps case ID, activity, and timestamp columns
3. User configures title, label mode, complexity options, and exclusions
4. App computes recommended defaults from `_recommend_complexity_filters()`
5. App calls the same `build_dfg()`, `draw_dfg()`, and `draw_dfg_interactive_html()` functions used by the CLI
6. App displays PNG + embedded interactive HTML and offers downloads

## Important Functions

### `_prepare_work_log()`

- Verifies required columns exist
- Keeps only case ID, activity, and timestamp columns
- Parses timestamps with `pandas.to_datetime(errors="coerce", utc=True)`
- Drops null or invalid rows
- Optionally excludes listed activities
- Sorts by case ID and timestamp with stable sort

### `_recommend_complexity_filters()`

- Calculates activity frequency coverage
- Recommends `max_activities` based on approximately 90% event coverage with bounds
- Recommends `min_edge_count` from edge frequency distribution
- Used when CLI passes `0`/unset values and auto complexity is enabled

### `_build_replay_events()`

- Builds replay traces per case from chronological event order
- Filters out activities not present in the full HTML graph when needed
- Inserts `[START]` and `[END]` replay events when start/end nodes are enabled
- Produces replay data suitable for continuous absolute-time animation

### `_compact_replay_data()`

- Compresses replay payload before injecting it into HTML
- Stores activities once and references them by index
- Stores timestamps as epoch milliseconds
- Was added specifically to reduce large HTML load times

### `build_dfg()`

- Groups rows by case to build traces
- Counts node frequencies and directly-follows edge frequencies
- Optionally injects `[START]` and `[END]`
- Filters edges by minimum count
- Removes disconnected nodes after edge filtering
- Computes transition probability per source node
- Derives a top-to-bottom level map from average trace position
- Returns a `DFGResult` dataclass with graph + metadata

### `draw_dfg()`

- Uses manually computed layered positions from `_compute_positions()`
- Draws ellipse nodes whose size/color encode frequency
- Draws directed edges whose width encodes edge count
- Shows edge labels as count, probability, both, or none
- Saves a static PNG

### `draw_dfg_interactive_html()`

- Builds a `pyvis.Network`
- Writes HTML to disk
- Reopens the generated HTML and injects custom UI/JS

The injected HTML features are important:
- Complexity controls
  - `Large log mode` toggle for lighter initial rendering on large logs
  - loading/status message while expensive filter updates are being applied
  - slider for minimum edge count
  - slider for top-N activities by frequency
  - slider for node size
  - slider for node font size
  - slider for edge width
  - slider for edge label size
  - collapsible control panel (`Hide` / `Show`)
- Global token replay
  - play/pause control
  - speed selector
  - reset control
  - timeline slider
  - continuous absolute-time replay
  - concurrent token movement across cases
  - `[START]` to `[END]` replay path

Important implementation note:
- the HTML still comes from `pyvis`, but it is post-processed heavily after generation
- most interactivity now lives in injected JavaScript inside `draw_dfg_interactive_html()`
- recent optimization work focused on reducing browser-side recomputation cost for large logs without changing visible functionality
- replay performance work now includes moving token rendering off the main graph dataset and onto a canvas overlay layer

This means the HTML is not only a graph viewer; it is also a lightweight process replay UI.

## CLI Arguments

Supported arguments in `parse_args()`:

- `--csv`: input CSV path
- `--case-id`: case identifier column
- `--activity`: activity column
- `--timestamp`: timestamp column
- `--out`: PNG output path
- `--html-out`: HTML output path
- `--title`: title for both outputs
- `--edge-label-mode`: `both|count|prob|none`
- `--min-edge-count`: minimum edge frequency, `0` means auto
- `--max-activities`: keep top N activities by frequency, `0` means auto
- `--exclude-activities`: comma-separated activities to remove before mining
- `--no-start-end`: disables `[START]` and `[END]`
- `--no-auto-complexity`: disables recommended defaults

## Data Assumptions

The script assumes the event log can be interpreted as:
- one row = one event
- one case ID groups events into a trace
- timestamp establishes event order within each case
- activity is the node label shown in the DFG

No lifecycle filtering is currently built into the script. If a log contains multiple lifecycle states per activity, the caller must choose the appropriate activity/timestamp columns or pre-filter the CSV externally.

## Known Sample Mappings

### `sample_event_log.csv`

- `--case-id case_id`
- `--activity activity`
- `--timestamp timestamp`

### `open_event_log_receipt.csv`

- `--case-id case:concept:name`
- `--activity concept:name`
- `--timestamp time:timestamp`

### `open_event_log_running_example.csv`

Likely usable with:
- `--case-id case:concept:name`
- `--activity concept:name` or `Activity`
- `--timestamp time:timestamp`

## Output Strategy

There are two different graph scopes by design:

- PNG uses the filtered DFG based on chosen/recommended complexity
- HTML uses the full DFG and applies complexity interactively in the browser

This is intentional and important. The HTML preserves more information and starts from a recommended default view.

## Current State Observed

- The workspace is a flat project folder, not a packaged application
- There is no test suite yet
- There is no README at the root besides this handoff note
- Generated artifacts are already committed/saved in the project root
- The project now includes a usable local web UI, but it is still prototype-style rather than productionized
- the main logic file has grown significantly and now mixes mining logic, static rendering, HTML generation, and injected JS

## Environment Note

Current environment status:
- `py -3` is available
- dependencies including `streamlit` are installed
- CLI generation was verified with sample and receipt logs
- Streamlit startup was verified with `py -3 -m streamlit run web_app.py --server.headless true --server.port 8503`

Verified dependency versions:
- `pandas==3.0.2`
- `networkx==3.6.1`
- `matplotlib==3.10.8`
- `pyvis==0.3.2`
- `streamlit==1.56.0`

## How To Run

### Primary GUI

```bash
py -3 -m streamlit run web_app.py
```

### CLI Example: Sample Log

```bash
py -3 dfg_visualizer.py --csv sample_event_log.csv --case-id case_id --activity activity --timestamp timestamp --out dfg_sample.png --html-out dfg_sample.html
```

### CLI Example: Receipt Log

```bash
py -3 dfg_visualizer.py --csv open_event_log_receipt.csv --case-id "case:concept:name" --activity "concept:name" --timestamp "time:timestamp" --out receipt_dfg.png --html-out receipt_dfg.html --title "Receipt Directly-Follows Graph"
```

## Good Next Steps

1. Add a short `README.md` with example commands for each CSV.
2. Split `dfg_visualizer.py` into mining/rendering/html injection modules if the project grows.
3. Add simple regression checks for DFG counts and filtering behavior.
4. Decide whether generated HTML/PNG files should remain in source control or move to an `outputs/` directory.
5. If lifecycle-heavy logs are expected, add explicit lifecycle filtering options.
6. Consider moving the embedded HTML replay UI into separate JS/CSS assets if the app keeps growing.
7. Consider moving the web app to a small package layout if more pages/features are added.

## Suggested Re-entry Prompt For A New OpenCode Session

Use this project context file first: `PROJECT_CONTEXT.md`.

Then inspect `dfg_visualizer.py` and continue from there. This project has already been converted from CLI-only usage into a Streamlit GUI. The HTML visualization now supports compact replay data, continuous absolute-time concurrent token replay, `[START]` to `[END]` token paths, and interactive controls for complexity plus visual scaling.

Primary local app entry point:
- `py -3 -m streamlit run web_app.py`
