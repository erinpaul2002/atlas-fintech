"""Render bounded visual specs into a polished, standalone Atlas page."""

import base64
import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any

CLIENT_SCRIPT = r"""
(() => {
  const dataNode = document.getElementById('visual-data');
  const canvas = document.getElementById('chart');
  if (!dataNode || !canvas) return;
  const spec = JSON.parse(dataNode.textContent);
  const context = canvas.getContext('2d');
  const shell = canvas.parentElement;
  const legend = document.getElementById('legend');
  const tooltip = document.getElementById('tooltip');
  const palette = ['#d9ff57', '#58d6ff', '#ff8d6b', '#c9a7ff', '#f9d65c', '#72e6a6'];
  const hidden = new Set();
  let targets = [];

  const format = value => {
    const number = Number(value);
    const absolute = Math.abs(number);
    const digits = absolute >= 100 ? 0 : absolute >= 10 ? 1 : 2;
    return `${spec.value_prefix || ''}${number.toLocaleString(undefined, {maximumFractionDigits: digits})}${spec.value_suffix || ''}`;
  };
  const color = index => spec.series[index]?.color || palette[index % palette.length];
  const short = (label, limit = 13) => label.length > limit ? `${label.slice(0, limit - 1)}…` : label;

  function makeLegend() {
    legend.replaceChildren();
    const doughnut = spec.kind === 'doughnut';
    const items = doughnut ? spec.labels.map((name, index) => ({name, index})) : spec.series.map((item, index) => ({name: item.name, index}));
    items.forEach(item => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = hidden.has(item.index) ? 'legend-item muted' : 'legend-item';
      button.innerHTML = `<i style="--swatch:${doughnut ? palette[item.index % palette.length] : color(item.index)}"></i><span>${escapeText(item.name)}</span>`;
      button.addEventListener('click', () => {
        hidden.has(item.index) ? hidden.delete(item.index) : hidden.add(item.index);
        makeLegend(); draw();
      });
      legend.append(button);
    });
  }

  function escapeText(value) {
    const node = document.createElement('span');
    node.textContent = String(value);
    return node.innerHTML;
  }

  function sizeCanvas() {
    const width = Math.max(300, shell.clientWidth);
    const height = Math.max(330, Math.min(520, width * 0.58));
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return {width, height};
  }

  function draw() {
    const {width, height} = sizeCanvas();
    context.clearRect(0, 0, width, height);
    targets = [];
    if (spec.kind === 'doughnut') drawDoughnut(width, height);
    else drawCartesian(width, height);
  }

  function drawCartesian(width, height) {
    const pad = {left: 64, right: 22, top: 26, bottom: 54};
    const chartWidth = width - pad.left - pad.right;
    const chartHeight = height - pad.top - pad.bottom;
    const visible = spec.series.map((item, index) => ({...item, index})).filter(item => !hidden.has(item.index));
    const all = visible.flatMap(item => item.values).filter(Number.isFinite);
    if (!all.length) return;
    let min = Math.min(...all), max = Math.max(...all);
    if (spec.kind === 'bar') { min = Math.min(0, min); max = Math.max(0, max); }
    if (min === max) { const gap = Math.abs(min || 1) * .1; min -= gap; max += gap; }
    const margin = (max - min) * .08;
    min -= margin; max += margin;
    const groupWidth = chartWidth / Math.max(spec.labels.length, 1);
    const x = index => spec.kind === 'bar'
      ? pad.left + groupWidth * (index + .5)
      : pad.left + (spec.labels.length === 1 ? chartWidth / 2 : index * chartWidth / (spec.labels.length - 1));
    const y = value => pad.top + (max - value) * chartHeight / (max - min);

    context.font = '11px ui-monospace, SFMono-Regular, Consolas, monospace';
    context.textBaseline = 'middle';
    for (let tick = 0; tick <= 4; tick++) {
      const value = max - (max - min) * tick / 4;
      const py = pad.top + chartHeight * tick / 4;
      context.strokeStyle = 'rgba(209, 232, 215, .13)';
      context.beginPath(); context.moveTo(pad.left, py); context.lineTo(width - pad.right, py); context.stroke();
      context.fillStyle = '#82988a'; context.textAlign = 'right'; context.fillText(format(value), pad.left - 10, py);
    }
    const labelStep = Math.max(1, Math.ceil(spec.labels.length / Math.max(4, Math.floor(width / 95))));
    context.fillStyle = '#82988a'; context.textAlign = 'center'; context.textBaseline = 'top';
    spec.labels.forEach((label, index) => {
      if (index % labelStep === 0 || index === spec.labels.length - 1) context.fillText(short(label), x(index), height - pad.bottom + 17);
    });

    if (spec.kind === 'bar') {
      const group = groupWidth;
      const barWidth = Math.max(2, Math.min(34, group * .72 / Math.max(visible.length, 1)));
      visible.forEach((series, seriesPosition) => series.values.forEach((value, index) => {
        if (!Number.isFinite(value)) return;
        const bx = pad.left + group * index + group / 2 + (seriesPosition - (visible.length - 1) / 2) * barWidth;
        const zero = y(0), py = y(value);
        context.fillStyle = color(series.index);
        context.globalAlpha = .88;
        context.fillRect(bx - barWidth * .42, Math.min(py, zero), barWidth * .84, Math.max(2, Math.abs(zero - py)));
        context.globalAlpha = 1;
        targets.push({x: bx, y: py, label: spec.labels[index], value, series: series.name, color: color(series.index)});
      }));
      return;
    }

    visible.forEach(series => {
      const points = series.values.map((value, index) => Number.isFinite(value) ? {x: x(index), y: y(value), value, index} : null);
      if (spec.kind === 'area') {
        const usable = points.filter(Boolean);
        if (usable.length) {
          const gradient = context.createLinearGradient(0, pad.top, 0, height - pad.bottom);
          gradient.addColorStop(0, `${color(series.index)}55`); gradient.addColorStop(1, `${color(series.index)}00`);
          context.fillStyle = gradient; context.beginPath(); context.moveTo(usable[0].x, height - pad.bottom);
          usable.forEach(point => context.lineTo(point.x, point.y));
          context.lineTo(usable.at(-1).x, height - pad.bottom); context.closePath(); context.fill();
        }
      }
      context.strokeStyle = color(series.index); context.lineWidth = 2.5; context.lineJoin = 'round'; context.beginPath();
      let started = false;
      points.forEach(point => {
        if (!point) { started = false; return; }
        started ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y);
        started = true;
        targets.push({x: point.x, y: point.y, label: spec.labels[point.index], value: point.value, series: series.name, color: color(series.index)});
      });
      context.stroke();
    });
  }

  function drawDoughnut(width, height) {
    const values = spec.series[0].values.map(Number);
    const total = values.reduce((sum, value, index) => hidden.has(index) ? sum : sum + Math.max(0, value), 0);
    const cx = width / 2, cy = height / 2, radius = Math.min(width, height) * .31;
    let angle = -Math.PI / 2;
    values.forEach((value, index) => {
      if (hidden.has(index) || value <= 0 || !total) return;
      const next = angle + Math.PI * 2 * value / total;
      context.beginPath(); context.arc(cx, cy, radius, angle, next); context.arc(cx, cy, radius * .58, next, angle, true); context.closePath();
      context.fillStyle = palette[index % palette.length]; context.fill();
      const middle = (angle + next) / 2;
      targets.push({x: cx + Math.cos(middle) * radius * .79, y: cy + Math.sin(middle) * radius * .79, label: spec.labels[index], value, series: spec.series[0].name, color: palette[index % palette.length]});
      angle = next;
    });
    context.fillStyle = '#f5f1e8'; context.textAlign = 'center'; context.textBaseline = 'middle';
    context.font = '600 26px Georgia, serif'; context.fillText(format(total), cx, cy - 5);
    context.fillStyle = '#82988a'; context.font = '11px ui-monospace, monospace'; context.fillText('TOTAL', cx, cy + 22);
  }

  canvas.addEventListener('mousemove', event => {
    const rect = canvas.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    let nearest = null, distance = 24;
    targets.forEach(point => { const next = Math.hypot(point.x - mx, point.y - my); if (next < distance) { distance = next; nearest = point; } });
    if (!nearest) { tooltip.hidden = true; return; }
    tooltip.innerHTML = `<b>${escapeText(nearest.label)}</b><span><i style="--swatch:${nearest.color}"></i>${escapeText(nearest.series)} · ${escapeText(format(nearest.value))}</span>`;
    tooltip.style.left = `${Math.min(widthClamp(shell.clientWidth - 190, 8), mx + 16)}px`;
    tooltip.style.top = `${Math.max(8, my - 30)}px`; tooltip.hidden = false;
  });
  canvas.addEventListener('mouseleave', () => { tooltip.hidden = true; });
  const widthClamp = (value, minimum) => Math.max(minimum, value);
  makeLegend(); draw();
  new ResizeObserver(draw).observe(shell);
})();
""".strip()

_SCRIPT_HASH = base64.b64encode(hashlib.sha256(CLIENT_SCRIPT.encode()).digest()).decode()
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    f"script-src 'sha256-{_SCRIPT_HASH}'; "
    "style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

STYLE = r"""
:root{--ink:#07100d;--paper:#f5f1e8;--muted:#82988a;--line:rgba(209,232,215,.15);--acid:#d9ff57}
*{box-sizing:border-box}html{background:var(--ink);color-scheme:dark}body{margin:0;min-height:100vh;color:var(--paper);font-family:Georgia,'Times New Roman',serif;background:radial-gradient(circle at 83% 4%,rgba(217,255,87,.11),transparent 28rem),linear-gradient(rgba(255,255,255,.022) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px),var(--ink);background-size:auto,32px 32px,32px 32px}
.page{width:min(1160px,100%);margin:auto;padding:clamp(24px,5vw,72px)}header{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2rem;align-items:start;border-top:1px solid var(--acid);padding-top:18px}.eyebrow,.stamp,.source,.legend-item,footer,summary{font:600 11px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.12em;text-transform:uppercase}.eyebrow{color:var(--acid)}h1{font-size:clamp(2.25rem,7vw,6.4rem);font-weight:400;line-height:.92;letter-spacing:-.055em;max-width:980px;margin:.5rem 0 1.1rem;text-wrap:balance}.subtitle{color:#b7c6bb;font-size:clamp(1rem,2vw,1.32rem);line-height:1.5;max-width:760px;margin:0}.stamp{text-align:right;color:var(--muted)}.visual-card{position:relative;margin-top:clamp(32px,6vw,70px);border:1px solid var(--line);background:rgba(7,16,13,.82);box-shadow:0 28px 80px rgba(0,0,0,.35);overflow:hidden}.visual-card:before{content:'SIGNAL CANVAS';position:absolute;top:15px;right:18px;color:rgba(217,255,87,.34);font:10px ui-monospace,monospace;letter-spacing:.14em}.chart-shell{position:relative;padding:30px 14px 4px}.chart-shell canvas{display:block;max-width:100%}.legend{display:flex;flex-wrap:wrap;gap:8px;padding:0 24px 24px}.legend-item{border:1px solid var(--line);background:transparent;color:#c8d3cb;padding:8px 10px;cursor:pointer}.legend-item:hover{border-color:#73887a}.legend-item.muted{opacity:.36}.legend-item i,.tooltip i{display:inline-block;width:8px;height:8px;margin-right:8px;background:var(--swatch)}.tooltip{position:absolute;pointer-events:none;z-index:4;min-width:170px;padding:11px 13px;background:#f5f1e8;color:#07100d;border-left:3px solid var(--acid);box-shadow:0 12px 36px #000}.tooltip b,.tooltip span{display:block}.tooltip b{font-size:14px;margin-bottom:5px}.tooltip span{font:11px ui-monospace,monospace}.takeaway{display:grid;grid-template-columns:auto 1fr;gap:16px;margin:18px 0 0;padding:18px 20px;border-left:3px solid var(--acid);background:rgba(217,255,87,.06)}.takeaway span{color:var(--acid);font:11px ui-monospace,monospace}.takeaway p{margin:0;line-height:1.5}.table-wrap{overflow:auto;max-height:70vh}.data-table{width:100%;border-collapse:collapse;font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.data-table th{position:sticky;top:0;background:#101b17;color:var(--acid);text-align:left;letter-spacing:.08em;text-transform:uppercase}.data-table th,.data-table td{padding:14px 16px;border-bottom:1px solid var(--line);white-space:nowrap}.data-table tbody tr:hover{background:rgba(217,255,87,.045)}.data-table td.numeric{text-align:right;font-variant-numeric:tabular-nums}.data-table td.positive{color:#72e6a6}.data-table td.negative{color:#ff8d6b}details{border-top:1px solid var(--line)}summary{cursor:pointer;color:var(--muted);padding:16px 22px}.source{color:var(--muted);margin-top:16px}.source strong{color:#bbc8be}footer{display:flex;justify-content:space-between;gap:1rem;color:#66786c;margin-top:36px;padding-top:16px;border-top:1px solid var(--line)}
@media(max-width:650px){.page{padding:22px 14px 34px}header{grid-template-columns:1fr}.stamp{text-align:left}h1{font-size:clamp(2.5rem,14vw,4.5rem)}.visual-card{margin-top:30px}.chart-shell{padding-inline:0}.takeaway{grid-template-columns:1fr}.data-table th,.data-table td{padding:12px}.source{padding-inline:4px}footer{display:block;line-height:1.8}}
@media(prefers-reduced-motion:no-preference){header,.visual-card,.takeaway{animation:rise .55s both}.visual-card{animation-delay:.08s}.takeaway{animation-delay:.16s}@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}}
""".strip()


def _safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _cell(value: Any) -> str:
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    classes = ["numeric"] if numeric else []
    if numeric and value > 0:
        classes.append("positive")
    elif numeric and value < 0:
        classes.append("negative")
    class_name = f' class="{" ".join(classes)}"' if classes else ""
    display = "—" if value is None else str(value)
    return f"<td{class_name}>{html.escape(display)}</td>"


def _table(columns: list[str], rows: list[list[Any]], label: str) -> str:
    heads = "".join(f"<th scope=col>{html.escape(column)}</th>" for column in columns)
    body = "".join("<tr>" + "".join(_cell(value) for value in row) + "</tr>" for row in rows)
    return (
        f'<div class="table-wrap"><table class="data-table" aria-label="{html.escape(label, quote=True)}">'
        f"<thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_visual_html(spec: dict[str, Any]) -> str:
    """Render a normalized spec. Callers must validate size and shape first."""
    title = html.escape(spec["title"])
    subtitle = html.escape(spec.get("subtitle") or "A focused view of the numbers that matter.")
    source = html.escape(spec.get("source_note") or "Data supplied in this Atlas conversation")
    takeaway = html.escape(spec.get("takeaway") or "")
    created = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    kind = spec["kind"]

    if kind == "table":
        visual = _table(spec["columns"], spec["rows"], spec["title"])
        data_node = script = ""
    else:
        rows = [
            [label, *(series["values"][index] for series in spec["series"])]
            for index, label in enumerate(spec["labels"])
        ]
        accessible = _table(["Period", *(series["name"] for series in spec["series"])], rows, spec["title"])
        visual = (
            '<div class="chart-shell"><canvas id="chart" role="img" '
            f'aria-label="Interactive {html.escape(kind)} chart: {html.escape(spec["title"], quote=True)}"></canvas>'
            '<div id="tooltip" class="tooltip" hidden></div></div><div id="legend" class="legend"></div>'
            f"<details><summary>View accessible data table</summary>{accessible}</details>"
        )
        data_node = f'<script type="application/json" id="visual-data">{_safe_json(spec)}</script>'
        script = f"<script>{CLIENT_SCRIPT}</script>"

    takeaway_block = (
        f'<aside class="takeaway"><span>READOUT</span><p>{takeaway}</p></aside>' if takeaway else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Atlas Visual</title><meta name="description" content="{subtitle}">
<meta property="og:title" content="{title}"><meta property="og:description" content="{subtitle}">
<meta property="og:type" content="article"><meta name="theme-color" content="#07100d"><style>{STYLE}</style></head>
<body><main class="page"><header><div><div class="eyebrow">Atlas · Visual intelligence</div><h1>{title}</h1><p class="subtitle">{subtitle}</p></div><div class="stamp">Generated<br>{created}</div></header>
<section class="visual-card">{visual}</section>{takeaway_block}<p class="source"><strong>Source</strong> · {source}</p>
<footer><span>Atlas Fintech</span><span>Interactive analysis · private link</span></footer></main>{data_node}{script}</body></html>"""
