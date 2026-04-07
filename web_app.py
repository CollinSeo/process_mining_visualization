from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from dfg_visualizer import (
    _build_replay_events,
    _prepare_work_log,
    _recommend_complexity_filters,
    build_dfg,
    draw_dfg,
    draw_dfg_interactive_html,
)


APP_TITLE = "Process Mining DFG Visualizer"
ROOT = Path(__file__).resolve().parent


def _choose_default(columns: Iterable[str], candidates: list[str]) -> Optional[str]:
    cols = list(columns)
    exact = {c: c for c in cols}
    lower = {c.lower(): c for c in cols}
    for candidate in candidates:
        if candidate in exact:
            return exact[candidate]
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return cols[0] if cols else None


def _load_dataframe(source_mode: str):
    if source_mode == "Upload CSV":
        uploaded = st.file_uploader("CSV file", type=["csv"])
        if uploaded is None:
            return None, None
        return pd.read_csv(uploaded), uploaded.name

    sample_paths = sorted(ROOT.glob("*.csv"))
    if not sample_paths:
        st.error("No bundled CSV files found in the project folder.")
        return None, None
    selected = st.selectbox("Bundled CSV", sample_paths, format_func=lambda p: p.name)
    return pd.read_csv(selected), selected.name


def _render_results(result: dict) -> None:
    st.subheader("Results")
    metric_cols = st.columns(5)
    metric_cols[0].metric("Nodes", result["nodes"])
    metric_cols[1].metric("Edges", result["edges"])
    metric_cols[2].metric("Replay Events", result["replay_events"])
    metric_cols[3].metric("Min Edge", result["min_edge_count"])
    metric_cols[4].metric("Max Activities", result["max_activities_label"])

    download_cols = st.columns(2)
    download_cols[0].download_button(
        "Download PNG",
        data=result["png_bytes"],
        file_name=result["png_name"],
        mime="image/png",
        use_container_width=True,
    )
    download_cols[1].download_button(
        "Download HTML",
        data=result["html_text"],
        file_name=result["html_name"],
        mime="text/html",
        use_container_width=True,
    )

    png_tab, html_tab = st.tabs(["PNG Preview", "Interactive HTML"])
    with png_tab:
        st.image(result["png_bytes"], use_container_width=True)
    with html_tab:
        components.html(result["html_text"], height=1000, scrolling=True)


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption("Upload an event log CSV, map the key columns, and generate PNG + interactive HTML DFG visualizations.")

    source_mode = st.radio("Data source", ["Upload CSV", "Use bundled sample"], horizontal=True)
    df, source_name = _load_dataframe(source_mode)
    if df is None:
        st.info("Choose a CSV to begin.")
        return

    st.subheader("Data Preview")
    st.caption(f"Source: `{source_name}` | Rows: `{len(df)}` | Columns: `{len(df.columns)}`")
    st.dataframe(df.head(50), use_container_width=True)

    columns = df.columns.tolist()
    default_case = _choose_default(columns, ["case_id", "case:concept:name", "case", "trace_id"])
    default_activity = _choose_default(columns, ["activity", "concept:name", "Activity", "task"])
    default_timestamp = _choose_default(columns, ["timestamp", "time:timestamp", "event_time", "datetime"])

    st.subheader("Column Mapping")
    map_cols = st.columns(3)
    case_id_col = map_cols[0].selectbox("Case ID column", columns, index=columns.index(default_case) if default_case in columns else 0)
    activity_col = map_cols[1].selectbox("Activity column", columns, index=columns.index(default_activity) if default_activity in columns else 0)
    timestamp_col = map_cols[2].selectbox("Timestamp column", columns, index=columns.index(default_timestamp) if default_timestamp in columns else 0)

    with st.sidebar:
        st.header("Options")
        title = st.text_input("Title", value="Directly-Follows Graph")
        edge_label_mode = st.selectbox("Edge label mode", ["both", "count", "prob", "none"], index=0)
        add_start_end = st.checkbox("Add [START]/[END] nodes", value=True)
        auto_complexity = st.checkbox("Use auto complexity defaults", value=True)
        min_edge_count = st.number_input("Min edge count (0 = auto)", min_value=0, value=0, step=1)
        max_activities = st.number_input("Max activities (0 = auto/all)", min_value=0, value=0, step=1)
        exclude_activities_raw = st.text_input("Exclude activities", value="", help="Comma-separated activity names")

    excluded = {item.strip() for item in exclude_activities_raw.split(",") if item.strip()}

    try:
        work_for_replay = _prepare_work_log(df, case_id_col, activity_col, timestamp_col, excluded)
        rec_min_edge, rec_max_activities, stats = _recommend_complexity_filters(work_for_replay, case_id_col, activity_col)
    except Exception as exc:
        st.error(f"Failed to prepare the event log: {exc}")
        return

    st.subheader("Auto Recommendation")
    rec_cols = st.columns(4)
    rec_cols[0].metric("Unique Activities", stats.get("unique_activities", 0))
    rec_cols[1].metric("Unique Edges", stats.get("unique_edges", 0))
    rec_cols[2].metric("Recommended Min Edge", rec_min_edge)
    rec_cols[3].metric("Recommended Max Activities", rec_max_activities)

    generate = st.button("Generate Visualization", type="primary", use_container_width=True)

    if generate:
        try:
            effective_min_edge = int(min_edge_count)
            effective_max_activities = int(max_activities)
            if auto_complexity:
                if effective_min_edge <= 0:
                    effective_min_edge = rec_min_edge
                if effective_max_activities <= 0:
                    effective_max_activities = rec_max_activities
            if effective_min_edge <= 0:
                effective_min_edge = 1

            with st.spinner("Building DFG visualizations..."):
                result = build_dfg(
                    df,
                    case_id_col,
                    activity_col,
                    timestamp_col,
                    min_edge_count=effective_min_edge,
                    max_activities=effective_max_activities,
                    exclude_activities=excluded,
                    add_start_end=add_start_end,
                )

                html_full_result = build_dfg(
                    df,
                    case_id_col,
                    activity_col,
                    timestamp_col,
                    min_edge_count=1,
                    max_activities=0,
                    exclude_activities=excluded,
                    add_start_end=add_start_end,
                )

                allowed_activities = {n for n in html_full_result.graph.nodes() if n not in {"[START]", "[END]"}}
                replay_events = _build_replay_events(
                    work_for_replay,
                    case_id_col,
                    activity_col,
                    timestamp_col,
                    allowed_activities=allowed_activities,
                    add_start_end=add_start_end,
                )

                with TemporaryDirectory() as tmp_dir:
                    tmp_root = Path(tmp_dir)
                    png_path = tmp_root / "dfg_result.png"
                    html_path = tmp_root / "dfg_result.html"
                    draw_dfg(result, png_path, title=title, edge_label_mode=edge_label_mode)
                    draw_dfg_interactive_html(
                        html_full_result,
                        html_path,
                        title=title,
                        edge_label_mode=edge_label_mode,
                        default_min_edge=effective_min_edge,
                        default_top_activities=effective_max_activities if effective_max_activities > 0 else None,
                        replay_events=replay_events,
                    )

                    png_bytes = png_path.read_bytes()
                    html_text = html_path.read_text(encoding="utf-8")

                html_text = html_text.replace('<script src="lib/bindings/utils.js"></script>', "")

            st.session_state["dfg_result"] = {
                "nodes": len(result.graph.nodes()),
                "edges": len(result.graph.edges()),
                "replay_events": len(replay_events),
                "min_edge_count": effective_min_edge,
                "max_activities_label": effective_max_activities if effective_max_activities > 0 else "all",
                "png_bytes": png_bytes,
                "html_text": html_text,
                "png_name": f"{Path(source_name).stem}_dfg.png",
                "html_name": f"{Path(source_name).stem}_dfg.html",
            }
        except Exception as exc:
            st.error(f"Visualization failed: {exc}")

    if "dfg_result" in st.session_state:
        _render_results(st.session_state["dfg_result"])


if __name__ == "__main__":
    main()
