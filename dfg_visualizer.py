#!/usr/bin/env python3
"""
Build and visualize a Directly-Follows Graph (DFG) from an event log CSV.

Example:
    python dfg_visualizer.py \
        --csv sample_event_log.csv \
        --case-id case_id \
        --activity activity \
        --timestamp timestamp \
        --out dfg.png \
        --html-out dfg.html \
        --min-edge-count 1
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib.patches import Ellipse
import networkx as nx
import pandas as pd

try:
    from pyvis.network import Network
except ImportError:
    Network = None


@dataclass
class DFGResult:
    graph: nx.DiGraph
    node_freq: Counter
    edge_freq: Counter
    edge_prob: Dict[Tuple[str, str], float]
    level_map: Dict[str, int]


def _scale(v: float, v_min: float, v_max: float, out_min: float, out_max: float) -> float:
    if v_max == v_min:
        return (out_min + out_max) / 2
    return out_min + ((v - v_min) / (v_max - v_min)) * (out_max - out_min)


def _blue_by_frequency(freq: int, min_f: int, max_f: int) -> str:
    t = _scale(freq, min_f, max_f, 0.0, 1.0)
    low = mcolors.to_rgb("#BFDBFE")
    high = mcolors.to_rgb("#1D4ED8")
    mixed = tuple(low[i] + t * (high[i] - low[i]) for i in range(3))
    return mcolors.to_hex(mixed)


def _edge_label_text(count: int, prob: float, mode: str, multiline: bool) -> str:
    if mode == "none":
        return ""
    if mode == "count":
        return f"n={count}"
    if mode == "prob":
        return f"p={prob:.2f}"
    return f"n={count}\np={prob:.2f}" if multiline else f"n={count} | p={prob:.2f}"


def _wrap_activity_label(label: str, max_line_length: int = 18) -> str:
    if len(label) <= max_line_length or "\n" in label:
        return label
    words = label.split()
    if len(words) <= 1:
        mid = max(1, len(label) // 2)
        return f"{label[:mid]}\n{label[mid:]}"

    best_split = 1
    best_score = None
    for split_at in range(1, len(words)):
        left = " ".join(words[:split_at])
        right = " ".join(words[split_at:])
        score = max(len(left), len(right)) * 10 + abs(len(left) - len(right))
        if best_score is None or score < best_score:
            best_score = score
            best_split = split_at

    return " ".join(words[:best_split]) + "\n" + " ".join(words[best_split:])


def _wrap_activity_label_html(label: str, max_line_length: int = 18) -> str:
    return _wrap_activity_label(label, max_line_length=max_line_length).replace("\n", "<br>")


def _prepare_work_log(
    df: pd.DataFrame,
    case_id_col: str,
    activity_col: str,
    timestamp_col: str,
    exclude_activities: Optional[Set[str]] = None,
) -> pd.DataFrame:
    required = {case_id_col, activity_col, timestamp_col}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}")

    work = df[[case_id_col, activity_col, timestamp_col]].copy()
    work[timestamp_col] = pd.to_datetime(work[timestamp_col], errors="coerce", utc=True)
    work = work.dropna(subset=[case_id_col, activity_col, timestamp_col])
    work[activity_col] = work[activity_col].astype(str)

    if exclude_activities:
        work = work[~work[activity_col].isin(exclude_activities)]

    if work.empty:
        raise ValueError("No valid rows after parsing timestamps and dropping nulls.")

    return work.sort_values([case_id_col, timestamp_col], kind="mergesort")


def _recommend_complexity_filters(
    work: pd.DataFrame,
    case_id_col: str,
    activity_col: str,
) -> Tuple[int, int, Dict[str, int]]:
    act_freq = Counter(work[activity_col].tolist())
    total_activities = len(act_freq)

    sorted_acts = sorted(act_freq.items(), key=lambda x: (-x[1], x[0]))
    total_events = sum(act_freq.values())
    coverage_target = 0.9
    running = 0
    top_n_for_coverage = total_activities
    for i, (_act, freq) in enumerate(sorted_acts, start=1):
        running += freq
        if total_events > 0 and (running / total_events) >= coverage_target:
            top_n_for_coverage = i
            break

    recommended_max_activities = min(total_activities, max(8, min(35, top_n_for_coverage)))

    edge_freq = Counter()
    for _, trace in work.groupby(case_id_col, sort=False):
        acts = trace[activity_col].tolist()
        for a, b in zip(acts, acts[1:]):
            edge_freq[(a, b)] += 1

    edge_counts = sorted(edge_freq.values())
    if not edge_counts:
        recommended_min_edge = 1
    elif len(edge_counts) < 15:
        recommended_min_edge = 1
    else:
        idx = int(0.55 * (len(edge_counts) - 1))
        recommended_min_edge = max(1, edge_counts[idx])

    stats = {
        "unique_activities": total_activities,
        "unique_edges": len(edge_freq),
    }
    return recommended_min_edge, recommended_max_activities, stats


def _build_replay_events(
    work: pd.DataFrame,
    case_id_col: str,
    activity_col: str,
    timestamp_col: str,
    allowed_activities: Optional[Set[str]] = None,
    add_start_end: bool = True,
) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []

    for case_id_value, trace in work.groupby(case_id_col, sort=False):
        case_id = str(case_id_value)
        trace = trace.sort_values(timestamp_col, kind="mergesort")
        filtered_rows = []

        for _, row in trace.iterrows():
            activity = str(row[activity_col])
            if allowed_activities is not None and activity not in allowed_activities:
                continue
            filtered_rows.append(row)

        if not filtered_rows:
            continue

        timestamps = [pd.Timestamp(row[timestamp_col]) for row in filtered_rows]
        activities = [str(row[activity_col]) for row in filtered_rows]

        if add_start_end:
            start_ts = timestamps[0] - pd.Timedelta(milliseconds=1)
            events.append(
                {
                    "case_id": case_id,
                    "activity": "[START]",
                    "timestamp": start_ts.isoformat(),
                    "edge_id": "",
                }
            )

        prev = "[START]" if add_start_end else None
        for activity, ts in zip(activities, timestamps):
            edge_id = f"{prev}|||{activity}" if prev else ""
            events.append(
                {
                    "case_id": case_id,
                    "activity": activity,
                    "timestamp": ts.isoformat(),
                    "edge_id": edge_id,
                }
            )
            prev = activity

        if add_start_end:
            end_ts = timestamps[-1] + pd.Timedelta(milliseconds=1)
            events.append(
                {
                    "case_id": case_id,
                    "activity": "[END]",
                    "timestamp": end_ts.isoformat(),
                    "edge_id": f"{activities[-1]}|||[END]",
                }
            )

    events.sort(key=lambda ev: (ev["timestamp"], ev["case_id"]))
    return events


def _compact_replay_data(replay_events: List[Dict[str, str]]) -> Dict[str, object]:
    activity_to_idx: Dict[str, int] = {}
    activities: List[str] = []
    cases: Dict[str, List[List[int]]] = defaultdict(list)
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    for ev in replay_events:
        activity = str(ev["activity"])
        if activity not in activity_to_idx:
            activity_to_idx[activity] = len(activities)
            activities.append(activity)

        ts = pd.Timestamp(ev["timestamp"])
        time_ms = int(ts.timestamp() * 1000)
        cases[str(ev["case_id"])].append([time_ms, activity_to_idx[activity]])

        if start_ms is None or time_ms < start_ms:
            start_ms = time_ms
        if end_ms is None or time_ms > end_ms:
            end_ms = time_ms

    return {
        "activities": activities,
        "cases": dict(cases),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "case_count": len(cases),
        "total_events": len(replay_events),
    }


def build_dfg(
    df: pd.DataFrame,
    case_id_col: str,
    activity_col: str,
    timestamp_col: str,
    min_edge_count: int = 1,
    max_activities: int = 0,
    exclude_activities: Optional[Set[str]] = None,
    add_start_end: bool = True,
) -> DFGResult:
    work = _prepare_work_log(df, case_id_col, activity_col, timestamp_col, exclude_activities)

    raw_act_freq = Counter(work[activity_col].tolist())
    keep_activities: Optional[Set[str]] = None
    if max_activities > 0 and len(raw_act_freq) > max_activities:
        sorted_acts = sorted(raw_act_freq.items(), key=lambda x: (-x[1], x[0]))
        keep_activities = {act for act, _ in sorted_acts[:max_activities]}

    node_freq: Counter = Counter()
    edge_freq: Counter = Counter()

    # For top-to-bottom ranking: average position index of each activity within traces.
    position_sum = defaultdict(float)
    position_count = defaultdict(int)

    for _, trace in work.groupby(case_id_col, sort=False):
        acts = trace[activity_col].tolist()
        if keep_activities is not None:
            acts = [a for a in acts if a in keep_activities]
        if not acts:
            continue

        node_freq.update(acts)
        for idx, act in enumerate(acts):
            position_sum[act] += idx
            position_count[act] += 1
        for a, b in zip(acts, acts[1:]):
            edge_freq[(a, b)] += 1

        if add_start_end:
            edge_freq[("[START]", acts[0])] += 1
            edge_freq[(acts[-1], "[END]")] += 1
            node_freq["[START]"] += 1
            node_freq["[END]"] += 1

    min_edge_count = max(1, int(min_edge_count))
    if min_edge_count > 1:
        edge_freq = Counter({edge: cnt for edge, cnt in edge_freq.items() if cnt >= min_edge_count})

    if not node_freq:
        raise ValueError("No activities found in the input data.")

    if not edge_freq:
        raise ValueError("No edges left after filtering. Lower --min-edge-count or adjust filters.")

    # Keep only nodes that still appear in at least one remaining edge.
    connected_nodes = set()
    for src, dst in edge_freq.keys():
        connected_nodes.add(src)
        connected_nodes.add(dst)
    node_freq = Counter({n: c for n, c in node_freq.items() if n in connected_nodes})

    # Source-based transition probabilities.
    outgoing_sum = defaultdict(int)
    for (src, _dst), cnt in edge_freq.items():
        outgoing_sum[src] += cnt

    edge_prob: Dict[Tuple[str, str], float] = {}
    for edge, cnt in edge_freq.items():
        src = edge[0]
        edge_prob[edge] = cnt / outgoing_sum[src] if outgoing_sum[src] else 0.0

    # Rank activities by average position in traces and bucket similar stages together.
    avg_position = {}
    for act in node_freq.keys():
        if act == "[START]":
            avg_position[act] = -1.0
        elif act == "[END]":
            avg_position[act] = max(position_sum.values(), default=0.0) + 1.0
        else:
            avg_position[act] = position_sum[act] / max(position_count[act], 1)

    raw_level_map = {node: int(round(pos)) for node, pos in avg_position.items()}
    min_level = min(raw_level_map.values())
    level_map = {node: lvl - min_level for node, lvl in raw_level_map.items()}

    if "[START]" in level_map:
        level_map["[START]"] = 0
    if "[END]" in level_map:
        max_non_end = max((lvl for n, lvl in level_map.items() if n != "[END]"), default=0)
        level_map["[END]"] = max_non_end + 1

    g = nx.DiGraph()
    for act, freq in node_freq.items():
        g.add_node(act, freq=freq, level=level_map[act])

    for (src, dst), cnt in edge_freq.items():
        g.add_edge(src, dst, count=cnt, prob=edge_prob[(src, dst)])

    return DFGResult(
        graph=g,
        node_freq=node_freq,
        edge_freq=edge_freq,
        edge_prob=edge_prob,
        level_map=level_map,
    )


def _compute_positions(level_map: Dict[str, int]) -> Dict[str, Tuple[float, float]]:
    # Group nodes by level and place them centered per row (top -> bottom).
    levels: Dict[int, List[str]] = defaultdict(list)
    for node, lvl in level_map.items():
        levels[lvl].append(node)

    pos: Dict[str, Tuple[float, float]] = {}
    x_spacing = 3.6
    y_spacing = 2.4

    for lvl in sorted(levels.keys()):
        nodes = sorted(levels[lvl])
        n = len(nodes)
        start_x = -((n - 1) * x_spacing) / 2.0
        y = -lvl * y_spacing
        for i, node in enumerate(nodes):
            x = start_x + i * x_spacing
            pos[node] = (x, y)

    return pos


def draw_dfg(
    result: DFGResult,
    out_path: Path,
    title: str = "Directly-Follows Graph",
    edge_label_mode: str = "both",
) -> None:
    g = result.graph
    pos = _compute_positions(result.level_map)

    plt.figure(figsize=(14, 10), dpi=170)
    ax = plt.gca()
    ax.set_title(title, fontsize=16, pad=12)
    ax.set_axis_off()

    node_freq_values = [result.node_freq[n] for n in g.nodes()]
    min_nf, max_nf = min(node_freq_values), max(node_freq_values)
    node_order = list(g.nodes())
    node_color_map = {n: _blue_by_frequency(result.node_freq[n], min_nf, max_nf) for n in node_order}

    edge_count_values = [data["count"] for _, _, data in g.edges(data=True)] or [1]
    min_ec, max_ec = min(edge_count_values), max(edge_count_values)
    edge_widths = [_scale(data["count"], min_ec, max_ec, 2.0, 8.0) for _, _, data in g.edges(data=True)]

    label_map = {n: _wrap_activity_label(str(n), max_line_length=18) for n in node_order}

    for n in node_order:
        x, y = pos[n]
        label_lines = label_map[n].splitlines() or [str(n)]
        longest_line = max(len(line) for line in label_lines)
        line_count = len(label_lines)
        base_w = 3.0
        w = min(9.2, base_w + (longest_line * 0.16))
        h = _scale(result.node_freq[n], min_nf, max_nf, 1.2, 1.7) + ((line_count - 1) * 0.45)
        patch = Ellipse(
            (x, y),
            width=w,
            height=h,
            facecolor=node_color_map[n],
            edgecolor="#0F172A",
            linewidth=2.0,
            alpha=0.97,
            zorder=2,
        )
        ax.add_patch(patch)

    nx.draw_networkx_labels(
        g,
        pos,
        labels=label_map,
        font_size=17,
        font_weight="bold",
        font_color="#F8FAFC",
        verticalalignment="center",
        horizontalalignment="center",
    )

    nx.draw_networkx_edges(
        g,
        pos,
        width=edge_widths,
        edge_color="#1F4E79",
        arrows=True,
        arrowsize=28,
        arrowstyle="-|>",
        connectionstyle="arc3,rad=0.08",
        min_source_margin=22,
        min_target_margin=24,
        alpha=0.92,
    )

    if edge_label_mode != "none":
        # Draw edge labels manually to improve readability and avoid overlap with arrows.
        edge_items = list(g.edges(data=True))
        for i, (u, v, d) in enumerate(edge_items):
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            mx = x0 + (x1 - x0) * 0.52
            my = y0 + (y1 - y0) * 0.52
            dx = (y1 - y0) * 0.03
            dy = -(x1 - x0) * 0.03
            if i % 2 == 0:
                dx *= -1
                dy *= -1
            label = _edge_label_text(d["count"], d["prob"], edge_label_mode, multiline=True)
            plt.text(
                mx + dx,
                my + dy,
                label,
                fontsize=9,
                fontweight="bold",
                color="#0F172A",
                ha="center",
                va="center",
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "facecolor": "#FFFFFF",
                    "edgecolor": "#1E3A8A",
                    "linewidth": 0.8,
                    "alpha": 0.92,
                },
                zorder=6,
            )

    legend_text = "Edge label: n=count, p=transition probability from source"
    plt.text(
        0.01,
        0.01,
        legend_text,
        transform=ax.transAxes,
        fontsize=10,
        color="#334155",
        ha="left",
        va="bottom",
        bbox={"facecolor": "#F8FAFC", "edgecolor": "#CBD5E1", "pad": 5.0},
    )

    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, bbox_inches="tight")
    plt.close()


def draw_dfg_interactive_html(
    result: DFGResult,
    html_out_path: Path,
    title: str,
    edge_label_mode: str,
    default_min_edge: int = 1,
    default_top_activities: Optional[int] = None,
    replay_events: Optional[List[Dict[str, str]]] = None,
) -> None:
    if Network is None:
        raise ImportError(
            "pyvis is not installed. Install dependencies with: pip install -r requirements.txt"
        )

    g = result.graph
    net = Network(height="900px", width="100%", directed=True, bgcolor="#F7FAFC", font_color="#0F172A")
    net.toggle_physics(False)

    node_values = [result.node_freq[n] for n in g.nodes()]
    min_nf, max_nf = min(node_values), max(node_values)
    pos = _compute_positions(result.level_map)

    for n, data in g.nodes(data=True):
        display_label = _wrap_activity_label_html(str(n), max_line_length=18)
        size = _scale(result.node_freq[n], min_nf, max_nf, 48, 90)
        font_size = _scale(result.node_freq[n], min_nf, max_nf, 22, 30)
        color = _blue_by_frequency(result.node_freq[n], min_nf, max_nf)
        x, y = pos[n]
        net.add_node(
            n,
            label=display_label,
            size=size,
            baseSize=size,
            baseFontSize=font_size,
            freq=int(result.node_freq[n]),
            baseColor=color,
            title=f"Activity: {n}<br>Frequency: {result.node_freq[n]}",
            color={"background": color, "border": "#0F172A", "highlight": "#2563EB"},
            shape="ellipse",
            borderWidth=2,
            font={"size": font_size, "face": "Segoe UI Semibold", "color": "#F8FAFC", "vadjust": 0, "multi": "html"},
            widthConstraint={"minimum": size * 2.0},
            heightConstraint={"minimum": size * 1.05},
            x=x * 90,
            y=(-y) * 90,
            fixed={"x": False, "y": False},
        )

    edge_values = [d["count"] for _, _, d in g.edges(data=True)] or [1]
    min_ec, max_ec = min(edge_values), max(edge_values)

    for u, v, d in g.edges(data=True):
        width = _scale(d["count"], min_ec, max_ec, 2.0, 10.0)
        label = _edge_label_text(d["count"], d["prob"], edge_label_mode, multiline=False)
        net.add_edge(
            u,
            v,
            id=f"{u}|||{v}",
            title=f"{u} -> {v}<br>Count: {d['count']}<br>Probability: {d['prob']:.2f}",
            width=width,
            baseWidth=width,
            baseFontSize=16,
            count=int(d["count"]),
            prob=float(d["prob"]),
            baseColor="#1E40AF",
            color={"color": "#1E40AF", "highlight": "#1D4ED8", "inherit": False},
            arrows="to",
            font={"size": 16, "align": "middle", "strokeWidth": 6, "strokeColor": "#FFFFFF"},
            smooth={"enabled": True, "type": "curvedCW", "roundness": 0.12},
            label=label,
        )

    net.set_options(
        """
        {
          "autoResize": false,
          "layout": {
            "improvedLayout": false
          },
          "interaction": {
            "hover": true,
            "tooltipDelay": 120,
            "navigationButtons": true,
            "keyboard": true,
            "zoomView": true,
            "dragView": true,
            "dragNodes": true
          },
          "edges": {
            "shadow": true,
            "selectionWidth": 2
          },
          "nodes": {
            "shadow": true
          },
          "physics": {
            "enabled": false
          }
        }
        """
    )

    html_out_path.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(html_out_path), open_browser=False, notebook=False)

    # Put an obvious title at the top and add complexity sliders for live filtering.
    html_text = html_out_path.read_text(encoding="utf-8")
    title_block = (
        f"<h2 style='font-family:Segoe UI,Arial,sans-serif;margin:14px 18px;color:#0F172A;'>{title}</h2>"
    )
    html_text = html_text.replace("<body>", f"<body>{title_block}", 1)

    controls_script = """
<script type=\"text/javascript\"> 
(function () {
  function attachControls() {
    if (typeof nodes === 'undefined' || typeof edges === 'undefined' || typeof network === 'undefined') {
      setTimeout(attachControls, 200);
      return;
    }

    var allNodes = nodes.get();
    var allEdges = edges.get();
    var replayData = __REPLAY_DATA_JSON__;
    if (!allNodes.length) {
      return;
    }

    var panel = document.createElement('div');
    panel.style.position = 'fixed';
    panel.style.top = '14px';
    panel.style.right = '14px';
    panel.style.zIndex = '9999';
    panel.style.background = 'rgba(255,255,255,0.96)';
    panel.style.border = '1px solid #cbd5e1';
    panel.style.borderRadius = '10px';
    panel.style.padding = '10px 12px';
    panel.style.fontFamily = 'Segoe UI, Arial, sans-serif';
    panel.style.fontSize = '12px';
    panel.style.boxShadow = '0 8px 20px rgba(2, 6, 23, 0.12)';
    panel.style.width = '320px';
    panel.innerHTML = ""
      + "<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;'>"
      + "<div style='font-weight:700;color:#0f172a;'>Controls</div>"
      + "<button id='panelToggle' style='padding:3px 8px;border:1px solid #94a3b8;background:#f8fafc;border-radius:6px;cursor:pointer;font-size:11px;'>Hide</button>"
      + "</div>"
      + "<div id='panelBody'>"
      + "<div style='font-weight:700;color:#0f172a;margin-bottom:6px;'>Complexity Controls</div>"
      + "<label style='display:flex;align-items:center;gap:6px;margin-bottom:10px;color:#334155;'>"
      + "<input id='largeLogToggle' type='checkbox'/>"
      + "<span>Large log mode</span>"
      + "</label>"
      + "<div id='filterStatus' style='display:none;margin-bottom:8px;padding:6px 8px;border-radius:6px;background:#eff6ff;color:#1d4ed8;font-size:11px;'>Applying filters...</div>"
      + "<div style='margin-bottom:8px;'>Min edge count: <span id='minEdgeValue'></span></div>"
      + "<input id='minEdgeSlider' type='range' style='width:100%;margin-bottom:10px;'/>"
      + "<div style='margin-bottom:8px;'>Top activities by freq: <span id='topActValue'></span></div>"
      + "<input id='topActSlider' type='range' style='width:100%;margin-bottom:12px;'/>"
      + "<div style='margin-bottom:8px;'>Node size: <span id='nodeSizeValue'></span></div>"
      + "<input id='nodeSizeSlider' type='range' style='width:100%;margin-bottom:10px;'/>"
      + "<div style='margin-bottom:8px;'>Node font: <span id='nodeFontValue'></span></div>"
      + "<input id='nodeFontSlider' type='range' style='width:100%;margin-bottom:10px;'/>"
      + "<div style='margin-bottom:8px;'>Edge width: <span id='edgeWidthValue'></span></div>"
      + "<input id='edgeWidthSlider' type='range' style='width:100%;margin-bottom:10px;'/>"
      + "<div style='margin-bottom:8px;'>Edge label: <span id='edgeLabelValue'></span></div>"
      + "<input id='edgeLabelSlider' type='range' style='width:100%;margin-bottom:12px;'/>"
      + "<div style='height:1px;background:#e2e8f0;margin:6px 0 10px;'></div>"
      + "<div style='font-weight:700;color:#0f172a;margin-bottom:8px;'>Global Token Replay</div>"
      + "<div style='display:flex;gap:6px;align-items:center;margin-bottom:8px;'>"
      + "<button id='replayToggle' style='padding:4px 8px;border:1px solid #94a3b8;background:#f8fafc;border-radius:6px;cursor:pointer;'>Play</button>"
      + "<button id='replayReset' style='padding:4px 8px;border:1px solid #94a3b8;background:#f8fafc;border-radius:6px;cursor:pointer;'>Reset</button>"
      + "<label style='font-size:11px;color:#334155;'>Speed</label>"
      + "<select id='replaySpeed' style='padding:3px 4px;border:1px solid #cbd5e1;border-radius:6px;'>"
      + "<option value='0.1'>0.1x</option><option value='0.3'>0.3x</option><option value='0.5'>0.5x</option><option value='1' selected>1x</option><option value='2'>2x</option><option value='4'>4x</option><option value='8'>8x</option>"
      + "</select></div>"
      + "<div style='margin-bottom:6px;'>Timeline position</div>"
      + "<input id='replaySlider' type='range' style='width:100%;margin-bottom:6px;'/>"
      + "<div id='replayInfo' style='font-size:11px;color:#334155;line-height:1.45;'></div>"
      + "</div>";
    document.body.appendChild(panel);

    var startEndSet = new Set(['[START]', '[END]']);
    var activityNodes = allNodes.filter(function (n) { return !startEndSet.has(n.id); });
    activityNodes.sort(function (a, b) {
      return (b.freq || 0) - (a.freq || 0) || String(a.id).localeCompare(String(b.id));
    });

    var maxEdgeCount = allEdges.reduce(function (m, e) { return Math.max(m, e.count || 1); }, 1);
    var minTop = 1;
    var maxTop = Math.max(minTop, activityNodes.length);
    var defaultMinEdge = __DEFAULT_MIN_EDGE__;
    var defaultTop = __DEFAULT_TOP_ACTIVITIES__;

    var minEdgeSlider = document.getElementById('minEdgeSlider');
    var topActSlider = document.getElementById('topActSlider');
    var panelToggle = document.getElementById('panelToggle');
    var panelBody = document.getElementById('panelBody');
    var largeLogToggle = document.getElementById('largeLogToggle');
    var filterStatus = document.getElementById('filterStatus');
    var minEdgeValue = document.getElementById('minEdgeValue');
    var topActValue = document.getElementById('topActValue');
    var nodeSizeSlider = document.getElementById('nodeSizeSlider');
    var nodeFontSlider = document.getElementById('nodeFontSlider');
    var edgeWidthSlider = document.getElementById('edgeWidthSlider');
    var edgeLabelSlider = document.getElementById('edgeLabelSlider');
    var nodeSizeValue = document.getElementById('nodeSizeValue');
    var nodeFontValue = document.getElementById('nodeFontValue');
    var edgeWidthValue = document.getElementById('edgeWidthValue');
    var edgeLabelValue = document.getElementById('edgeLabelValue');
    var replaySlider = document.getElementById('replaySlider');
    var replayToggle = document.getElementById('replayToggle');
    var replayReset = document.getElementById('replayReset');
    var replaySpeed = document.getElementById('replaySpeed');
    var replayInfo = document.getElementById('replayInfo');

    minEdgeSlider.min = '1';
    minEdgeSlider.max = String(maxEdgeCount);
    minEdgeSlider.step = '1';
    minEdgeSlider.value = String(Math.min(maxEdgeCount, Math.max(1, defaultMinEdge)));

    topActSlider.min = String(minTop);
    topActSlider.max = String(maxTop);
    topActSlider.step = '1';
    topActSlider.value = String(Math.min(maxTop, Math.max(minTop, defaultTop)));

    nodeSizeSlider.min = '40';
    nodeSizeSlider.max = '260';
    nodeSizeSlider.step = '5';
    nodeSizeSlider.value = '100';

    nodeFontSlider.min = '50';
    nodeFontSlider.max = '220';
    nodeFontSlider.step = '5';
    nodeFontSlider.value = '100';

    edgeWidthSlider.min = '50';
    edgeWidthSlider.max = '240';
    edgeWidthSlider.step = '5';
    edgeWidthSlider.value = '100';

    edgeLabelSlider.min = '50';
    edgeLabelSlider.max = '220';
    edgeLabelSlider.step = '5';
    edgeLabelSlider.value = '100';

    replaySlider.min = '0';
    replaySlider.max = '1000';
    replaySlider.step = '1';
    replaySlider.value = '0';

    var replayPlaying = false;
    var replayHandle = null;
    var lastFrameTs = null;
    var panelCollapsed = false;
    var complexityApplyHandle = null;
    var tokenDraws = [];
    var replayCursorByCase = {};
    var lastRenderedSimTimeMs = null;
    var timelineCases = {};
    var globalStartMs = replayData.start_ms;
    var globalEndMs = replayData.end_ms;
    var currentSimTimeMs = null;
    var basePlaybackDurationMs = 45000;

    Object.keys(replayData.cases || {}).forEach(function (caseId) {
      timelineCases[caseId] = (replayData.cases[caseId] || []).map(function (item) {
        var timeMs = item[0];
        var activity = replayData.activities[item[1]];
        return {
          case_id: caseId,
          activity: activity,
          timeMs: timeMs
        };
      });
    });

    var caseIds = Object.keys(timelineCases);
    var visibleNodeIds = [];
    var networkContainer = document.getElementById('mynetwork');
    var overlayCanvas = null;
    var overlayCtx = null;
    var overlayDpr = window.devicePixelRatio || 1;
    var caseStartEntries = caseIds
      .map(function (caseId) {
        return timelineCases[caseId] && timelineCases[caseId].length
          ? {caseId: caseId, startMs: timelineCases[caseId][0].timeMs}
          : null;
      })
      .filter(function (item) { return item !== null; })
      .sort(function (a, b) { return a.startMs - b.startMs; });
    var startedCaseSet = {};
    var startCursor = 0;
    var suggestedLargeLogMode = (replayData.case_count || 0) >= 500 || (replayData.total_events || 0) >= 4000 || allEdges.length >= 90;

    function hasReplayData() {
      return caseIds.length > 0 && globalStartMs !== null && globalEndMs !== null;
    }

    function formatTime(ms) {
      if (ms === null || typeof ms === 'undefined' || isNaN(ms)) {
        return '-';
      }
      return new Date(ms).toLocaleString();
    }

    function getDurationMs() {
      if (!hasReplayData()) {
        return 0;
      }
      return Math.max(1, globalEndMs - globalStartMs);
    }

    function nodeScaleFactor() {
      return parseInt(nodeSizeSlider.value, 10) / 100;
    }

    function nodeFontScaleFactor() {
      return parseInt(nodeFontSlider.value, 10) / 100;
    }

    function edgeScaleFactor() {
      return parseInt(edgeWidthSlider.value, 10) / 100;
    }

    function edgeLabelScaleFactor() {
      return parseInt(edgeLabelSlider.value, 10) / 100;
    }

    function updateScaleLabels() {
      nodeSizeValue.textContent = String(Math.round(nodeScaleFactor() * 100)) + '%';
      nodeFontValue.textContent = String(Math.round(nodeFontScaleFactor() * 100)) + '%';
      edgeWidthValue.textContent = String(Math.round(edgeScaleFactor() * 100)) + '%';
      edgeLabelValue.textContent = String(Math.round(edgeLabelScaleFactor() * 100)) + '%';
    }

    function applyPanelCollapsedState() {
      panelBody.style.display = panelCollapsed ? 'none' : 'block';
      panel.style.width = panelCollapsed ? '120px' : '320px';
      panel.style.paddingBottom = panelCollapsed ? '10px' : '12px';
      panelToggle.textContent = panelCollapsed ? 'Show' : 'Hide';
    }

    function setFilterBusy(isBusy) {
      filterStatus.style.display = isBusy ? 'block' : 'none';
    }

    function initTokenOverlay() {
      if (!networkContainer || overlayCanvas) {
        return;
      }
      networkContainer.style.position = 'relative';
      overlayCanvas = document.createElement('canvas');
      overlayCanvas.style.position = 'absolute';
      overlayCanvas.style.left = '0';
      overlayCanvas.style.top = '0';
      overlayCanvas.style.width = '100%';
      overlayCanvas.style.height = '100%';
      overlayCanvas.style.pointerEvents = 'none';
      overlayCanvas.style.zIndex = '8';
      networkContainer.appendChild(overlayCanvas);
      overlayCtx = overlayCanvas.getContext('2d');
      resizeTokenOverlay();
    }

    function resizeTokenOverlay() {
      if (!overlayCanvas || !networkContainer) {
        return;
      }
      var width = Math.max(1, networkContainer.clientWidth);
      var height = Math.max(1, networkContainer.clientHeight);
      overlayDpr = window.devicePixelRatio || 1;
      overlayCanvas.width = Math.round(width * overlayDpr);
      overlayCanvas.height = Math.round(height * overlayDpr);
      overlayCanvas.style.width = width + 'px';
      overlayCanvas.style.height = height + 'px';
      if (overlayCtx) {
        overlayCtx.setTransform(overlayDpr, 0, 0, overlayDpr, 0, 0);
      }
    }

    function clearTokenOverlay() {
      if (!overlayCtx || !networkContainer) {
        return;
      }
      overlayCtx.clearRect(0, 0, networkContainer.clientWidth, networkContainer.clientHeight);
    }

    function drawTokenOverlay() {
      clearTokenOverlay();
      if (!overlayCtx || !tokenDraws.length) {
        return;
      }
      tokenDraws.forEach(function (token) {
        var dom = network.canvasToDOM({x: token.x, y: token.y});
        overlayCtx.beginPath();
        overlayCtx.arc(dom.x, dom.y, 5.5, 0, Math.PI * 2);
        overlayCtx.fillStyle = '#F59E0B';
        overlayCtx.strokeStyle = '#7C2D12';
        overlayCtx.lineWidth = 2;
        overlayCtx.fill();
        overlayCtx.stroke();
      });
    }

    function applyVisualScale() {
      updateScaleLabels();

      var nodeUpdates = [];
      nodes.get().forEach(function (n) {
        if (String(n.id).indexOf('__token__') === 0) {
          return;
        }
        var baseSize = n.baseSize || n.size || 60;
        var scaledSize = baseSize * nodeScaleFactor();
        var baseFontSize = n.baseFontSize || (n.font && n.font.size) || 28;
        nodeUpdates.push({
          id: n.id,
          size: scaledSize,
          font: {
            size: baseFontSize * Math.pow(nodeScaleFactor(), 0.35) * nodeFontScaleFactor(),
            face: 'Segoe UI Semibold',
            color: '#F8FAFC',
            vadjust: 0,
            multi: 'html'
          },
          widthConstraint: {minimum: scaledSize * 2.0},
          heightConstraint: {minimum: scaledSize * 1.05}
        });
      });
      if (nodeUpdates.length) {
        nodes.update(nodeUpdates);
      }

      var edgeUpdates = [];
      edges.get().forEach(function (e) {
        var baseWidth = e.baseWidth || e.width || 2;
        var baseFontSize = e.baseFontSize || (e.font && e.font.size) || 16;
        edgeUpdates.push({
          id: e.id,
          width: baseWidth * edgeScaleFactor(),
          font: {
            align: 'middle',
            size: baseFontSize * edgeLabelScaleFactor(),
            strokeColor: '#FFFFFF',
            strokeWidth: 6
          }
        });
      });
      if (edgeUpdates.length) {
        edges.update(edgeUpdates);
      }
    }

    function sliderToSimTime() {
      if (!hasReplayData()) {
        return null;
      }
      var ratio = parseInt(replaySlider.value, 10) / 1000;
      return globalStartMs + (getDurationMs() * ratio);
    }

    function syncSliderToSimTime() {
      if (!hasReplayData() || currentSimTimeMs === null) {
        replaySlider.value = '0';
        return;
      }
      var ratio = (currentSimTimeMs - globalStartMs) / getDurationMs();
      var clamped = Math.max(0, Math.min(1, ratio));
      replaySlider.value = String(Math.round(clamped * 1000));
    }

    function isNodeVisible(nodeId) {
      return nodes.get(nodeId) != null;
    }

    function hideAllTokens() {
      tokenDraws = [];
      clearTokenOverlay();
    }

    function rebuildStartedCases(simTimeMs) {
      startedCaseSet = {};
      startCursor = 0;
      while (startCursor < caseStartEntries.length && caseStartEntries[startCursor].startMs <= simTimeMs) {
        startedCaseSet[caseStartEntries[startCursor].caseId] = true;
        startCursor += 1;
      }
    }

    function ensureStartedCasesForTime(simTimeMs) {
      if (lastRenderedSimTimeMs === null || simTimeMs < lastRenderedSimTimeMs) {
        rebuildStartedCases(simTimeMs);
        return;
      }
      while (startCursor < caseStartEntries.length && caseStartEntries[startCursor].startMs <= simTimeMs) {
        startedCaseSet[caseStartEntries[startCursor].caseId] = true;
        startCursor += 1;
      }
    }

    function locateCaseState(caseId, events, simTimeMs) {
      if (!events.length || simTimeMs < events[0].timeMs) {
        replayCursorByCase[caseId] = 0;
        return null;
      }
      if (events.length === 1 || simTimeMs >= events[events.length - 1].timeMs) {
        replayCursorByCase[caseId] = Math.max(0, events.length - 2);
        return {type: 'at-node', current: events[events.length - 1], next: null, progress: 1};
      }

      var cachedIdx = replayCursorByCase[caseId];
      if (typeof cachedIdx === 'number' && cachedIdx >= 0 && cachedIdx < events.length - 1) {
        if (events[cachedIdx].timeMs <= simTimeMs && simTimeMs < events[cachedIdx + 1].timeMs) {
          var cachedSpan = Math.max(1, events[cachedIdx + 1].timeMs - events[cachedIdx].timeMs);
          return {
            type: 'moving',
            current: events[cachedIdx],
            next: events[cachedIdx + 1],
            progress: Math.max(0, Math.min(1, (simTimeMs - events[cachedIdx].timeMs) / cachedSpan))
          };
        }
        if (lastRenderedSimTimeMs !== null && simTimeMs >= lastRenderedSimTimeMs) {
          while (cachedIdx < events.length - 2 && simTimeMs >= events[cachedIdx + 1].timeMs) {
            cachedIdx += 1;
          }
          if (events[cachedIdx].timeMs <= simTimeMs && simTimeMs < events[cachedIdx + 1].timeMs) {
            replayCursorByCase[caseId] = cachedIdx;
            var forwardSpan = Math.max(1, events[cachedIdx + 1].timeMs - events[cachedIdx].timeMs);
            return {
              type: 'moving',
              current: events[cachedIdx],
              next: events[cachedIdx + 1],
              progress: Math.max(0, Math.min(1, (simTimeMs - events[cachedIdx].timeMs) / forwardSpan))
            };
          }
        }
      }

      var lo = 0;
      var hi = events.length - 2;
      while (lo <= hi) {
        var mid = Math.floor((lo + hi) / 2);
        var left = events[mid];
        var right = events[mid + 1];
        if (simTimeMs < left.timeMs) {
          hi = mid - 1;
        } else if (simTimeMs >= right.timeMs) {
          lo = mid + 1;
        } else {
          replayCursorByCase[caseId] = mid;
          var span = Math.max(1, right.timeMs - left.timeMs);
          return {
            type: 'moving',
            current: left,
            next: right,
            progress: Math.max(0, Math.min(1, (simTimeMs - left.timeMs) / span))
          };
        }
      }

      replayCursorByCase[caseId] = Math.max(0, events.length - 2);
      return {type: 'at-node', current: events[events.length - 1], next: null, progress: 1};
    }

    function renderSimulationTime(simTimeMs) {
      if (!hasReplayData()) {
        replayInfo.textContent = 'No replay data available';
        hideAllTokens();
        return;
      }

      currentSimTimeMs = Math.max(globalStartMs, Math.min(globalEndMs, simTimeMs));
      syncSliderToSimTime();

      ensureStartedCasesForTime(currentSimTimeMs);
      var positionMap = visibleNodeIds.length ? network.getPositions(visibleNodeIds) : {};
      var nextTokenDraws = [];
      var activeCount = 0;
      var movingCount = 0;

      Object.keys(startedCaseSet).forEach(function (caseId) {
        var events = timelineCases[caseId];
        var state = locateCaseState(caseId, events, currentSimTimeMs);
        if (!state) {
          return;
        }

        if (state.type === 'at-node') {
          var nodePos = positionMap[state.current.activity] || null;
          if (!nodePos) {
            return;
          }
          activeCount += 1;
          nextTokenDraws.push({x: nodePos.x, y: nodePos.y});
          return;
        }

        var fromPos = positionMap[state.current.activity] || null;
        var toPos = positionMap[state.next.activity] || null;
        if (!fromPos || !toPos) {
          return;
        }

        var x = fromPos.x + (toPos.x - fromPos.x) * state.progress;
        var y = fromPos.y + (toPos.y - fromPos.y) * state.progress;
        activeCount += 1;
        movingCount += 1;
        nextTokenDraws.push({x: x, y: y});
      });

      tokenDraws = nextTokenDraws;
      drawTokenOverlay();

      var elapsedRatio = (currentSimTimeMs - globalStartMs) / getDurationMs();
      replayInfo.innerHTML =
        "<b>Simulated time:</b> " + formatTime(currentSimTimeMs) + "<br/>"
        + "<b>Range:</b> " + formatTime(globalStartMs) + " - " + formatTime(globalEndMs) + "<br/>"
        + "<b>Active tokens:</b> " + activeCount + "<br/>"
        + "<b>Moving tokens:</b> " + movingCount + "<br/>"
        + "<b>Progress:</b> " + Math.round(Math.max(0, Math.min(1, elapsedRatio)) * 100) + "%";
      lastRenderedSimTimeMs = currentSimTimeMs;
    }

    function getTimelineAdvancePerMs() {
      return (getDurationMs() / basePlaybackDurationMs) * parseFloat(replaySpeed.value || '1');
    }

    function stopReplay() {
      replayPlaying = false;
      replayToggle.textContent = 'Play';
      lastFrameTs = null;
      if (replayHandle) {
        cancelAnimationFrame(replayHandle);
        replayHandle = null;
      }
    }

    function tick(frameTs) {
      if (!replayPlaying) {
        return;
      }
      if (lastFrameTs === null) {
        lastFrameTs = frameTs;
      }
      var deltaMs = frameTs - lastFrameTs;
      lastFrameTs = frameTs;
      currentSimTimeMs += deltaMs * getTimelineAdvancePerMs();
      if (currentSimTimeMs >= globalEndMs) {
        currentSimTimeMs = globalEndMs;
        renderSimulationTime(currentSimTimeMs);
        stopReplay();
        return;
      }
      renderSimulationTime(currentSimTimeMs);
      replayHandle = requestAnimationFrame(tick);
    }

    function startReplay() {
      if (!hasReplayData()) {
        return;
      }
      if (currentSimTimeMs === null || currentSimTimeMs >= globalEndMs) {
        currentSimTimeMs = globalStartMs;
      }
      stopReplay();
      replayPlaying = true;
      replayToggle.textContent = 'Pause';
      replayHandle = requestAnimationFrame(tick);
    }

    function resetReplay() {
      stopReplay();
      if (!hasReplayData()) {
        return;
      }
      currentSimTimeMs = globalStartMs;
      renderSimulationTime(currentSimTimeMs);
    }

    function applyComplexity() {
      setFilterBusy(true);
      var minEdge = parseInt(minEdgeSlider.value, 10);
      var topN = parseInt(topActSlider.value, 10);

      var keepNodeIds = new Set(activityNodes.slice(0, topN).map(function (n) { return n.id; }));
      if (allNodes.some(function (n) { return n.id === '[START]'; })) keepNodeIds.add('[START]');
      if (allNodes.some(function (n) { return n.id === '[END]'; })) keepNodeIds.add('[END]');

      var filteredEdges = allEdges.filter(function (e) {
        var c = e.count || 1;
        return c >= minEdge && keepNodeIds.has(e.from) && keepNodeIds.has(e.to);
      });

      var connected = new Set();
      filteredEdges.forEach(function (e) {
        connected.add(e.from);
        connected.add(e.to);
      });

      var filteredNodes = allNodes.filter(function (n) {
        return connected.has(n.id) || (startEndSet.has(n.id) && keepNodeIds.has(n.id));
      });

      visibleNodeIds = filteredNodes.map(function (n) { return n.id; });

      nodes.clear();
      nodes.add(filteredNodes);
      edges.clear();
      edges.add(filteredEdges);
      tokenDraws = [];
      clearTokenOverlay();
      replayCursorByCase = {};
      lastRenderedSimTimeMs = null;
      rebuildStartedCases(currentSimTimeMs === null ? globalStartMs : currentSimTimeMs);

      minEdgeValue.textContent = String(minEdge);
      topActValue.textContent = String(topN);
      applyVisualScale();

      if (hasReplayData()) {
        renderSimulationTime(currentSimTimeMs === null ? globalStartMs : currentSimTimeMs);
      }
      setFilterBusy(false);
    }

    function scheduleComplexityApply() {
      if (complexityApplyHandle) {
        clearTimeout(complexityApplyHandle);
      }
      setFilterBusy(true);
      complexityApplyHandle = setTimeout(function () {
        complexityApplyHandle = null;
        requestAnimationFrame(function () {
          applyComplexity();
        });
      }, 80);
    }

    function applyLargeLogMode(enabled) {
      if (enabled) {
        var largeMinEdge = Math.min(maxEdgeCount, Math.max(1, Math.max(defaultMinEdge, Math.ceil(maxEdgeCount * 0.12))));
        var largeTop = Math.min(maxTop, Math.max(minTop, Math.min(defaultTop, Math.max(6, Math.min(12, Math.ceil(maxTop * 0.45))))));
        minEdgeSlider.value = String(largeMinEdge);
        topActSlider.value = String(largeTop);
        nodeSizeSlider.value = '85';
        nodeFontSlider.value = '90';
        edgeWidthSlider.value = '100';
        edgeLabelSlider.value = '90';
      } else {
        minEdgeSlider.value = String(Math.min(maxEdgeCount, Math.max(1, defaultMinEdge)));
        topActSlider.value = String(Math.min(maxTop, Math.max(minTop, defaultTop)));
        nodeSizeSlider.value = '100';
        nodeFontSlider.value = '100';
        edgeWidthSlider.value = '100';
        edgeLabelSlider.value = '100';
      }
      applyVisualScale();
      scheduleComplexityApply();
    }

    minEdgeSlider.addEventListener('input', function () {
      scheduleComplexityApply();
    });
    topActSlider.addEventListener('input', function () {
      scheduleComplexityApply();
    });
    largeLogToggle.addEventListener('change', function () {
      applyLargeLogMode(largeLogToggle.checked);
    });
    nodeSizeSlider.addEventListener('input', function () {
      applyVisualScale();
    });
    nodeFontSlider.addEventListener('input', function () {
      applyVisualScale();
    });
    edgeWidthSlider.addEventListener('input', function () {
      applyVisualScale();
    });
    edgeLabelSlider.addEventListener('input', function () {
      applyVisualScale();
    });
    replaySlider.addEventListener('input', function () {
      stopReplay();
      var simTime = sliderToSimTime();
      if (simTime !== null) {
        renderSimulationTime(simTime);
      }
    });
    replayToggle.addEventListener('click', function () {
      if (replayPlaying) {
        stopReplay();
      } else {
        startReplay();
      }
    });
    replayReset.addEventListener('click', function () {
      resetReplay();
    });
    panelToggle.addEventListener('click', function () {
      panelCollapsed = !panelCollapsed;
      applyPanelCollapsedState();
    });
    replaySpeed.addEventListener('change', function () {
      if (replayPlaying) {
        lastFrameTs = null;
      }
    });

    if (!hasReplayData()) {
      replayInfo.textContent = 'No replay data available';
      replayToggle.disabled = true;
      replayReset.disabled = true;
      replaySlider.disabled = true;
    }

    initTokenOverlay();
    window.addEventListener('resize', function () {
      resizeTokenOverlay();
      drawTokenOverlay();
    });
    network.on('resize', function () {
      resizeTokenOverlay();
      drawTokenOverlay();
    });
    network.on('dragEnd', function () {
      drawTokenOverlay();
    });
    network.on('zoom', function () {
      drawTokenOverlay();
    });

    largeLogToggle.checked = suggestedLargeLogMode;
    updateScaleLabels();
    applyPanelCollapsedState();
    if (suggestedLargeLogMode) {
      applyLargeLogMode(true);
    } else {
      applyComplexity();
    }
    if (hasReplayData()) {
      currentSimTimeMs = globalStartMs;
      renderSimulationTime(currentSimTimeMs);
    }
    setTimeout(function(){network.moveTo({scale: 1.5});}, 200);
  }

  setTimeout(attachControls, 150);
})();
</script>
"""
    html_text = html_text.replace("</body>", controls_script + "</body>", 1)
    html_text = html_text.replace("__DEFAULT_MIN_EDGE__", str(max(1, int(default_min_edge))))
    if default_top_activities is None:
        default_top_activities = len([n for n in g.nodes() if n not in {"[START]", "[END]"}])
    html_text = html_text.replace("__DEFAULT_TOP_ACTIVITIES__", str(max(1, int(default_top_activities))))
    replay_events = replay_events or []
    html_text = html_text.replace("__REPLAY_DATA_JSON__", json.dumps(_compact_replay_data(replay_events), ensure_ascii=True))
    html_out_path.write_text(html_text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build and visualize a Directly-Follows Graph (DFG)")
    p.add_argument("--csv", required=True, help="Input CSV path")
    p.add_argument("--case-id", required=True, help="Case ID column name")
    p.add_argument("--activity", required=True, help="Activity column name")
    p.add_argument("--timestamp", required=True, help="Timestamp column name")
    p.add_argument("--out", default="dfg.png", help="Output image path (PNG)")
    p.add_argument("--html-out", default="dfg.html", help="Output interactive HTML path")
    p.add_argument("--title", default="Directly-Follows Graph", help="Plot title")
    p.add_argument(
        "--edge-label-mode",
        choices=["both", "count", "prob", "none"],
        default="both",
        help="Edge label display mode: both, count, prob, none",
    )
    p.add_argument(
        "--min-edge-count",
        type=int,
        default=0,
        help="Minimum edge frequency to keep in the model (0 = auto)",
    )
    p.add_argument(
        "--max-activities",
        type=int,
        default=0,
        help="Maximum number of activities to keep by frequency (0 = auto)",
    )
    p.add_argument(
        "--exclude-activities",
        default="",
        help="Comma-separated activities to exclude before mining (e.g. Rework,Debug)",
    )
    p.add_argument(
        "--no-start-end",
        action="store_true",
        help="Disable automatic [START]/[END] node insertion",
    )
    p.add_argument(
        "--no-auto-complexity",
        action="store_true",
        help="Disable automatic frequency-based defaults for complexity",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    excluded = {x.strip() for x in args.exclude_activities.split(",") if x.strip()}

    df = pd.read_csv(csv_path)
    work_for_replay = _prepare_work_log(df, args.case_id, args.activity, args.timestamp, excluded)

    min_edge_count = args.min_edge_count
    max_activities = args.max_activities
    auto_stats: Dict[str, int] = {}
    auto_applied = False

    if not args.no_auto_complexity and (min_edge_count <= 0 or max_activities <= 0):
        rec_min_edge, rec_max_activities, stats = _recommend_complexity_filters(
            work_for_replay,
            args.case_id,
            args.activity,
        )
        auto_stats = stats
        if min_edge_count <= 0:
            min_edge_count = rec_min_edge
        if max_activities <= 0:
            max_activities = rec_max_activities
        auto_applied = True

    if min_edge_count <= 0:
        min_edge_count = 1

    result = build_dfg(
        df,
        args.case_id,
        args.activity,
        args.timestamp,
        min_edge_count=min_edge_count,
        max_activities=max_activities,
        exclude_activities=excluded,
        add_start_end=not args.no_start_end,
    )

    # Interactive HTML keeps full activity/edge range and starts from recommended defaults.
    html_full_result = build_dfg(
        df,
        args.case_id,
        args.activity,
        args.timestamp,
        min_edge_count=1,
        max_activities=0,
        exclude_activities=excluded,
        add_start_end=not args.no_start_end,
    )
    allowed_activities = {n for n in html_full_result.graph.nodes() if n not in {"[START]", "[END]"}}
    replay_events = _build_replay_events(
        work_for_replay,
        args.case_id,
        args.activity,
        args.timestamp,
        allowed_activities=allowed_activities,
        add_start_end=not args.no_start_end,
    )

    draw_dfg(result, Path(args.out), title=args.title, edge_label_mode=args.edge_label_mode)
    draw_dfg_interactive_html(
        html_full_result,
        Path(args.html_out),
        title=args.title,
        edge_label_mode=args.edge_label_mode,
        default_min_edge=min_edge_count,
        default_top_activities=max_activities if max_activities > 0 else None,
        replay_events=replay_events,
    )

    print("DFG created successfully")
    print(f"- Input:  {csv_path}")
    print(f"- Output: {Path(args.out).resolve()}")
    print(f"- HTML:   {Path(args.html_out).resolve()}")
    print(f"- Nodes:  {len(result.graph.nodes())}")
    print(f"- Edges:  {len(result.graph.edges())}")
    print(f"- HTML full range: nodes={len(html_full_result.graph.nodes())}, edges={len(html_full_result.graph.edges())}")
    print(f"- Replay events: {len(replay_events)}")
    print(f"- Edge labels: mode={args.edge_label_mode}")
    print(
        f"- Filters: min_edge_count={min_edge_count}, max_activities={max_activities if max_activities > 0 else 'all'}, "
        f"excluded={sorted(excluded) if excluded else '[]'}"
    )
    if auto_applied:
        print(
            f"- Auto complexity: enabled (activities={auto_stats.get('unique_activities', 0)}, "
            f"edges={auto_stats.get('unique_edges', 0)})"
        )


if __name__ == "__main__":
    main()
