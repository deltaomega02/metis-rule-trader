#!/usr/bin/env python3
"""METIS dashboard — zero-dependency monochrome status page (bare http.server).

PAPER mode renders an A/B/C comparison of the gate arms (none / ema20>50 /
ema50>200): each arm is an independent paper portfolio (its own state DB), and the
top table compares equity, return, drawdown, trade stats and gate state so we can
see which gate actually compounds best. LIVE mode shows the single live arm.
Shared below: per-coin market state (same indicators the engine uses), combined
open positions and recent trades. No auto-refresh; mark prices fetched on load.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import (BYBIT, MARKET_SNAPSHOT_PATH, PAPER_ARMS, PAPER_INITIAL_BALANCE_USDT,
                             PAPER_MODE, RISK, STATE_DB_PATH, TRADING, arm_db_path)
import sqlite3
import time

from core.market_snapshot import read_snapshot


# ── helpers ──
def get_usdkrw() -> float:
    try:
        with urlopen("https://api.exchangerate-api.com/v4/latest/USD", timeout=3) as r:
            return float(json.loads(r.read())["rates"]["KRW"])
    except Exception:
        return 1370.0


def q(sql: str, params: tuple = (), path=STATE_DB_PATH) -> list[dict]:
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def is_active(svc: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", svc], capture_output=True,
                              text=True, timeout=2).stdout.strip() == "active"
    except Exception:
        return False


def free_mem_mi() -> int:
    try:
        for line in subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=2).stdout.splitlines():
            if line.startswith("Mem:"):
                return int(line.split()[2])
    except Exception:
        pass
    return 0


def disk_pct() -> str:
    try:
        return subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=2).stdout.splitlines()[-1].split()[3]
    except Exception:
        return "?"


def esc(s) -> str:
    return html_lib.escape(str(s)) if s is not None else ""


def _spark(points: list, seed: float, w: int = 1000, h: int = 100) -> str:
    """Inline SVG sparkline of the M2M equity curve — smooth bezier ribbon with a
    dashed seed baseline, soft area fill and a latest-point marker. Stretches to fill
    its cell (width:100%). Green if the last point is above seed, red below."""
    if not points or len(points) < 2:
        return '<span class="muted" style="font-size:11px">곡선 축적 중 — 사이클마다 1점</span>'
    lo = min(min(points), seed); hi = max(max(points), seed)
    pad = (hi - lo) * 0.12 or 1.0
    lo -= pad; hi += pad; rng = (hi - lo) or 1.0
    n = len(points)
    xy = [(i / (n - 1) * w, h - (p - lo) / rng * h) for i, p in enumerate(points)]
    dpath = _smooth_path(xy)
    y0 = h - (seed - lo) / rng * h
    up = points[-1] >= seed
    col = "#059669" if up else "#dc2626"
    fid = f"sf{int(xy[-1][0])}{int(xy[-1][1])}{'u' if up else 'd'}"
    area_d = dpath + f" L{xy[-1][0]:.1f},{h} L{xy[0][0]:.1f},{h} Z"
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'style="width:100%;height:52px;display:block">'
            f'<defs><linearGradient id="{fid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{col}" stop-opacity=".24"/>'
            f'<stop offset="1" stop-color="{col}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{area_d}" fill="url(#{fid})"/>'
            f'<line x1="0" y1="{y0:.1f}" x2="{w}" y2="{y0:.1f}" stroke="#94a3b8" stroke-width="1.4" stroke-dasharray="6 5" vector-effect="non-scaling-stroke"/>'
            f'<path d="{dpath}" fill="none" stroke="{col}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
            f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="3.2" fill="{col}" '
            f'vector-effect="non-scaling-stroke"/></svg>')


# Line styles for the overlay chart — monochrome base, the most-active arm darkest/thickest.
_ARM_STYLE = {"none": ("#94a3b8", 1.4, ""), "fast": ("#475569", 1.6, "5 3"),
              "slow": ("#94a3b8", 1.6, "1 3"), "ls": ("#94a3b8", 1.6, "4 3"),
              "live": ("#0f172a", 2.4, "")}


def _fmt_ts(t) -> str:
    """ISO ts_utc → 'MM-DD HH:MM' KST for x-axis labels. Falls back to a slice."""
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(hours=9)).strftime("%m-%d %H:%M")
    except Exception:
        return str(t)[5:16]


def _smooth_path(pts: list) -> str:
    """Catmull-Rom → cubic-bezier path 'd' through the given (x,y) points, so the
    equity curve flows as a smooth glassy ribbon instead of a jagged polyline."""
    if len(pts) < 2:
        return ""
    if len(pts) == 2:
        return f"M{pts[0][0]:.1f},{pts[0][1]:.1f} L{pts[1][0]:.1f},{pts[1][1]:.1f}"
    n = len(pts); d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else pts[i + 1]
        c1x = p1[0] + (p2[0] - p0[0]) / 6; c1y = p1[1] + (p2[1] - p0[1]) / 6
        c2x = p2[0] - (p3[0] - p1[0]) / 6; c2y = p2[1] - (p3[1] - p1[1]) / 6
        d.append(f"C{c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def _multi_chart(arms: list, w: int = 1120, h: int = 400) -> str:
    """Clean TradingView-style overlay of every arm's M2M curve on a shared scale:
    smooth bezier lines on a white field (no zone shading), light horizontal grid,
    a dashed seed baseline, and right-gutter value tags that de-collide so no two
    numbers overlap. The primary (live) line is emphasized with a soft glow + pulse."""
    allts = sorted({t for a in arms for t, _ in a.get("curve", [])})
    if len(allts) < 2:
        return '<div class="muted">곡선 축적 중 — 4h 사이클마다 1점씩 쌓입니다</div>'
    xi = {t: i for i, t in enumerate(allts)}
    N = len(allts)
    seed = arms[0]["seed"] if arms else 1000.0
    vals = [m for a in arms for _, m in a.get("curve", [])] + [seed]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or 1.0  # small headroom — lines nearly fill the frame
    lo -= pad; hi += pad; rng = (hi - lo) or 1.0
    PADL, PADR, PADT, PADB = 56, 80, 22, 34  # PADR = right gutter for value tags
    W = w - PADL - PADR; H = h - PADT - PADB
    def X(t): return PADL + xi[t] / (N - 1) * W
    def Y(v): return PADT + H - (v - lo) / rng * H
    ysd = Y(seed)
    xr = PADL + W  # plot right edge
    primary = next((a for a in arms if a.get("name_key") == "live"), arms[0] if arms else None)
    pcol = _ARM_STYLE.get(primary.get("name_key", ""), ("#0f172a", 2.6, ""))[0] if primary else "#0f172a"
    s = [f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" '
         f'style="width:100%;height:auto;display:block" font-family="SF Mono,Menlo,monospace">']
    s.append('<defs>'
             '<filter id="mcglow" x="-15%" y="-40%" width="130%" height="180%">'
             '<feGaussianBlur stdDeviation="3" result="b"/>'
             '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
             f'<linearGradient id="mcarea" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0" stop-color="{pcol}" stop-opacity=".10"/>'
             f'<stop offset="1" stop-color="{pcol}" stop-opacity="0"/></linearGradient></defs>')
    # ── horizontal gridlines + $ ticks (6 clean divisions), no vertical grid, no zone shading ──
    for k in range(6):
        v = lo + rng * (k / 5); y = Y(v)
        s.append(f'<line x1="{PADL}" y1="{y:.1f}" x2="{xr:.1f}" y2="{y:.1f}" '
                 f'stroke="rgba(15,23,42,0.06)" stroke-width="1"/>')
        s.append(f'<text x="{PADL - 8}" y="{y + 3:.0f}" font-size="10.5" fill="#94a3b8" '
                 f'text-anchor="end">${v:,.0f}</text>')
    # ── x-axis time labels (~6 evenly spaced cycles) ──
    step = max(1, (N - 1) // 5)
    for i in range(0, N, step):
        t = allts[i]; x = X(t)
        anch = "start" if i == 0 else ("end" if i >= N - step else "middle")
        s.append(f'<text x="{x:.1f}" y="{PADT + H + 18:.0f}" font-size="9.5" fill="#94a3b8" '
                 f'text-anchor="{anch}">{esc(_fmt_ts(t))}</text>')
    # ── seed baseline (label pinned at the left, out of the way) ──
    s.append(f'<line x1="{PADL}" y1="{ysd:.1f}" x2="{xr:.1f}" y2="{ysd:.1f}" '
             f'stroke="#cbd5e1" stroke-width="1.2" stroke-dasharray="5 4"/>')
    s.append(f'<text x="{PADL + 4}" y="{ysd - 5:.0f}" font-size="9" fill="#b0b8c4">시드 ${seed:,.0f}</text>')
    # ── lines: secondary arms first, primary on top ──
    ordered = sorted(arms, key=lambda a: a is primary)
    for a in ordered:
        nk = a.get("name_key", a["label"].split()[0])
        col, sw, dash = _ARM_STYLE.get(nk, ("#94a3b8", 1.6, ""))
        cur = a.get("curve", [])
        if len(cur) < 2:
            continue
        xy = [(X(t), Y(m)) for t, m in cur]
        dpath = _smooth_path(xy)
        if a is primary:
            area_d = dpath + f" L{xy[-1][0]:.1f},{PADT + H:.1f} L{xy[0][0]:.1f},{PADT + H:.1f} Z"
            s.append(f'<path d="{area_d}" fill="url(#mcarea)"/>')
            s.append(f'<path d="{dpath}" fill="none" stroke="{col}" stroke-width="{sw + 1.2}" '
                     f'stroke-linejoin="round" stroke-linecap="round" opacity=".30" filter="url(#mcglow)"/>')
            s.append(f'<path d="{dpath}" fill="none" stroke="{col}" stroke-width="{sw}" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        else:
            da = f' stroke-dasharray="{dash}"' if dash else ""
            s.append(f'<path d="{dpath}" fill="none" stroke="{col}" stroke-width="{sw}"{da} '
                     f'stroke-linejoin="round" stroke-linecap="round" opacity=".8"/>')
    # ── right-gutter value tags, de-collided so numbers never overlap ──
    tags = []
    for a in arms:
        nk = a.get("name_key", a["label"].split()[0])
        col = _ARM_STYLE.get(nk, ("#94a3b8", 1.6, ""))[0]
        cur = a.get("curve", [])
        if len(cur) < 2:
            continue
        _, lm = cur[-1]
        tags.append({"yr": Y(lm), "y": Y(lm), "val": lm, "col": col, "pri": a is primary})
    tags.sort(key=lambda e: e["yr"])
    top, bot, gap = PADT + 7, PADT + H - 2, 15
    for i, e in enumerate(tags):
        e["y"] = max(e["y"], top if i == 0 else tags[i - 1]["y"] + gap)
    overflow = (tags[-1]["y"] - bot) if tags else 0
    if overflow > 0:
        for e in tags:
            e["y"] = max(top, e["y"] - overflow)
    for e in tags:
        # endpoint dot on the line + a faint leader to the gutter tag
        s.append(f'<line x1="{xr:.1f}" y1="{e["yr"]:.1f}" x2="{xr + 8:.1f}" y2="{e["y"]:.1f}" '
                 f'stroke="{e["col"]}" stroke-width="1" opacity=".35"/>')
        s.append(f'<circle cx="{xr:.1f}" cy="{e["yr"]:.1f}" r="{3.6 if e["pri"] else 2.6}" fill="{e["col"]}"/>')
        fw = 800 if e["pri"] else 600
        s.append(f'<text x="{xr + 11:.1f}" y="{e["y"] + 3.5:.1f}" font-size="{11 if e["pri"] else 10}" '
                 f'fill="{e["col"]}" text-anchor="start" font-weight="{fw}">${e["val"]:,.0f}</text>')
    # ── analytics on the primary line: peak, trough, max-drawdown ──
    if primary and len(primary.get("curve", [])) >= 3:
        pc = primary["curve"]; vs = [m for _, m in pc]; last = len(vs) - 1
        i_hi = max(range(len(vs)), key=lambda i: vs[i])
        i_lo = min(range(len(vs)), key=lambda i: vs[i])
        if i_hi != last:  # skip if it coincides with the endpoint (already marked)
            hX, hY = X(pc[i_hi][0]), Y(pc[i_hi][1])
            s.append(f'<circle cx="{hX:.1f}" cy="{hY:.1f}" r="3.4" fill="#fff" stroke="#059669" stroke-width="1.8"/>')
            s.append(f'<text x="{hX:.1f}" y="{hY - 10:.1f}" font-size="10" fill="#059669" '
                     f'text-anchor="middle" font-weight="700">최고 ${pc[i_hi][1]:,.0f}</text>')
        if i_lo != last:
            lX, lY = X(pc[i_lo][0]), Y(pc[i_lo][1])
            s.append(f'<circle cx="{lX:.1f}" cy="{lY:.1f}" r="3.4" fill="#fff" stroke="#dc2626" stroke-width="1.8"/>')
            s.append(f'<text x="{lX:.1f}" y="{lY + 16:.1f}" font-size="10" fill="#dc2626" '
                     f'text-anchor="middle" font-weight="700">최저 ${pc[i_lo][1]:,.0f}</text>')
        # max drawdown: worst drop from a running peak to a later trough
        run = vs[0]; run_i = 0; worst = 0.0; dp = 0; dt = 0
        for i, m in enumerate(vs):
            if m > run:
                run = m; run_i = i
            dd = (m - run) / run if run else 0.0
            if dd < worst:
                worst = dd; dp = run_i; dt = i
        if worst < -0.002 and dp != dt:
            mx = X(pc[dt][0]); yTop = Y(pc[dp][1]); yBot = Y(pc[dt][1])
            right = mx < PADL + W * 0.6  # put the label on the roomier side
            s.append(f'<line x1="{mx:.1f}" y1="{yTop:.1f}" x2="{mx:.1f}" y2="{yBot:.1f}" '
                     f'stroke="#dc2626" stroke-width="1.2" stroke-dasharray="3 3" opacity=".5"/>')
            s.append(f'<line x1="{mx - 4:.1f}" y1="{yTop:.1f}" x2="{mx + 4:.1f}" y2="{yTop:.1f}" stroke="#dc2626" stroke-width="1.2" opacity=".5"/>')
            lxb = mx + 7 if right else mx - 7
            s.append(f'<text x="{lxb:.1f}" y="{(yTop + yBot) / 2 + 3:.1f}" font-size="9.5" fill="#dc2626" '
                     f'text-anchor="{"start" if right else "end"}" font-weight="700">MDD {worst * 100:.1f}%</text>')
    # ── pulsing marker on the primary latest point ──
    if primary and len(primary.get("curve", [])) >= 2:
        lt, lm = primary["curve"][-1]; lx, ly = X(lt), Y(lm)
        s.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="none" stroke="{pcol}" stroke-width="1.5" opacity=".6">'
                 f'<animate attributeName="r" values="4;12;4" dur="2.4s" repeatCount="indefinite"/>'
                 f'<animate attributeName="opacity" values=".55;0;.55" dur="2.4s" repeatCount="indefinite"/></circle>'
                 f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="#fff" stroke="{pcol}" stroke-width="2"/>')
    s.append('</svg>')
    # ── legend ──
    legend = []
    for a in arms:
        key = a["label"].split()[0]
        col, _, dash = _ARM_STYLE.get(a.get("name_key", key), ("#94a3b8", 1.6, ""))
        dsh = "border-bottom:2px dashed" if dash else "border-bottom:2px solid"
        legend.append(f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px">'
                      f'<span style="width:18px;{dsh} {col}"></span>{esc(key)} '
                      f'<b style="color:{col}">${a["mark_equity"]:,.0f}</b></span>')
    return "".join(s) + (f'<div style="font-size:11.5px;margin-top:10px;color:#334155;'
                         f'display:flex;flex-wrap:wrap;align-items:center">{"".join(legend)}</div>')


_TICK = {"ts": 0.0, "v": {}}


def live_marks() -> dict:
    """Current price per traded symbol via ONE tickers request (all linear symbols
    in a single call — negligible rate-limit weight, unlike the per-coin klines),
    cached 15s. Returns {} on failure so the caller falls back to the snapshot price.
    This is the only live exchange call the dashboard makes: cheap and bounded."""
    now = time.time()
    if _TICK["v"] and now - _TICK["ts"] < 15:
        return _TICK["v"]
    try:
        with urlopen(f"{BYBIT.base_url}/v5/market/tickers?category=linear", timeout=4) as r:
            lst = json.loads(r.read()).get("result", {}).get("list", [])
        v = {x["symbol"]: float(x["lastPrice"]) for x in lst
             if x.get("symbol") in TRADING.SYMBOLS and x.get("lastPrice")}
        if v:
            _TICK.update(ts=now, v=v)
            return v
    except Exception:
        pass
    return _TICK["v"]


def coin_status(m: dict, held_side: str | None) -> tuple[str, str]:
    """Operator-facing status line for a coin from its snapshot indicators and
    whether an arm holds it. Pure formatting — no exchange calls."""
    if held_side:
        return ("보유 중 (롱)" if held_side == "Buy" else "보유 중 (숏)", "hold")
    if not m.get("trending"):
        return (f"무추세 관망 (ADX {m['adx']:.0f})", "mut")
    if m.get("long_cand"):
        return ("🟢 상승 돌파 — 롱 진입", "pos")
    if m.get("short_cand"):
        return ("🔴 하락 돌파 — 숏 진입", "neg")
    if m.get("up"):
        return (f"상승추세 · 롱돌파까지 +{m['dist_hi']:.1f}%", "mut")
    return (f"하락추세 · 숏돌파까지 -{m.get('dist_lo', 0):.1f}%", "mut")


def _cost_bp(side: str, ref: float, fill: float, is_exit: bool = False) -> float | None:
    """체결 비용 bp (양수=불리). 진입: Buy는 비싸게 사면 +, Sell은 싸게 팔면 +.
    청산은 방향 반전(숏 청산=매수)."""
    if not ref or not fill:
        return None
    d = 1 if side == "Buy" else -1
    if is_exit:
        d = -d
    return d * (fill - ref) / ref * 1e4


def _experiment(live: dict, paper: dict) -> dict:
    """실전 vs 페이퍼 검증 실험 지표 — 같은 신호 페어 매칭 + 비용 실측 통계.
    페어 = 심볼·방향 동일 + 진입시각 15분 내(버퍼 45s vs 150s 어긋남 흡수)."""
    from datetime import datetime

    def ts(s):
        try:
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    pairs = []
    for st, lrows, prows in (("보유", live["positions"], paper["positions"]),
                             ("청산", live["trades"], paper["trades"])):
        for lp in lrows:
            for pp in prows:
                if (lp["symbol"] == pp["symbol"] and lp["side"] == pp["side"]
                        and abs(ts(lp["opened_utc"]) - ts(pp["opened_utc"])) < 900):
                    ref = lp.get("ref_entry") or 0
                    pairs.append(dict(
                        sym=lp["symbol"], side=lp["side"], st=st,
                        t=(lp["opened_utc"] or "")[5:16], ref=ref,
                        lf=lp["entry_price"], pf=pp["entry_price"],
                        lbp=_cost_bp(lp["side"], ref, lp["entry_price"]),
                        pbp=_cost_bp(pp["side"], pp.get("ref_entry") or ref, pp["entry_price"]),
                        lR=lp.get("realized_R"), pR=pp.get("realized_R")))
                    break
    # 진입 비용: 라이브 전 체결(보유+청산) ref 대비
    ent = [_cost_bp(x["side"], x.get("ref_entry") or 0, x["entry_price"])
           for x in list(live["positions"]) + list(live["trades"])]
    ent = [e for e in ent if e is not None]
    # 청산 비용: 라이브 청산거래 ref_exit 대비
    exi = [_cost_bp(t["side"], t.get("ref_exit") or 0, t.get("exit_price") or 0, is_exit=True)
           for t in live["trades"]]
    exi = [e for e in exi if e is not None]
    closed = live["trades"]
    return dict(pairs=pairs, n_ent=len(ent),
                ent_avg=(sum(ent) / len(ent)) if ent else None,
                n_exi=len(exi), exi_avg=(sum(exi) / len(exi)) if exi else None,
                n_closed=len(closed),
                net=sum((t.get("realized_usdt") or 0) for t in closed))


def _side_html(v) -> str:
    """보유 방향 배지: 롱(초록)/숏(빨강)/미보유(–). v=(side, is_1d) — 1D 암 보유는 ·1D 표기."""
    if not v:
        return '<span class="mut">–</span>'
    side, is_1d = v
    txt = ("롱" if side == "Buy" else "숏") + ("·1D" if is_1d else "")
    cls = "pos" if side == "Buy" else "neg"
    return f'<span class="{cls}">{txt}</span>'


# ── gather ──
def _uptime(started_iso) -> str:
    """가동 시작 ISO ts → '10일 6시간' (며칠째+몇시간째). None이면 '–'."""
    if not started_iso:
        return "–"
    try:
        st = datetime.fromisoformat(str(started_iso).replace("Z", "+00:00"))
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        sec = (datetime.now(timezone.utc) - st).total_seconds()
        days = int(sec // 86400); hrs = int(sec // 3600) % 24
        return f"{days}일 {hrs}시간" if days else f"{int(sec // 3600)}시간"
    except Exception:
        return "–"


def _arm_summary(label: str, gate_kind: str, path, gates: dict, marks: dict) -> dict:
    # wallet_usdt/funding은 신 스키마. 아직 마이그레이션 안 된 DB(재기동 전 페이퍼)도
    # 그대로 렌더되어야 하므로 없으면 조용히 빠진다.
    try:
        eqr = q("SELECT equity_usdt, high_water_usdt, initial_usdt, wallet_usdt FROM equity WHERE key='main'", path=path)
    except Exception:
        eqr = q("SELECT equity_usdt, high_water_usdt, initial_usdt FROM equity WHERE key='main'", path=path)
    eq = eqr[0]["equity_usdt"] if eqr else PAPER_INITIAL_BALANCE_USDT
    hw = eqr[0]["high_water_usdt"] if eqr else PAPER_INITIAL_BALANCE_USDT
    seed = (eqr[0]["initial_usdt"] if eqr and eqr[0].get("initial_usdt") else None) or PAPER_INITIAL_BALANCE_USDT
    wallet = eqr[0].get("wallet_usdt") if eqr else None
    # 펀딩은 청산손익에 안 들어있는 실제 현금흐름 (페이퍼 암은 테이블이 비어 0).
    try:
        fr = q("SELECT COALESCE(SUM(amount_usdt),0) s FROM funding", path=path)
        funding = float(fr[0]["s"]) if fr else 0.0
    except Exception:
        funding = 0.0
    positions = q("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_utc", path=path)
    agg = q("""SELECT COUNT(*) n, SUM(CASE WHEN realized_usdt>0 THEN 1 ELSE 0 END) wins,
               AVG(realized_R) avg_r, SUM(realized_usdt) sum_pnl,
               SUM(CASE WHEN realized_usdt>0 THEN realized_usdt ELSE 0 END) gw,
               SUM(CASE WHEN realized_usdt<=0 THEN realized_usdt ELSE 0 END) gl
               FROM trades""", path=path)
    trades = q("SELECT * FROM trades ORDER BY id DESC LIMIT 500", path=path)
    # mark-to-market: realized equity + open-position unrealized at snapshot price
    upnl = 0.0
    for pos in positions:
        mk = marks.get(pos["symbol"], 0.0); dd = 1 if pos["side"] == "Buy" else -1
        if mk > 0:
            upnl += dd * (mk - pos["entry_price"]) * pos["qty"]
    mark_eq = eq + upnl
    try:
        curve = [(r["ts_utc"], r["mark_usdt"]) for r in
                 q("SELECT ts_utc,mark_usdt FROM equity_curve ORDER BY ts_utc", path=path)]
    except Exception:
        curve = []
    return {"label": label, "gate_kind": gate_kind, "equity": eq, "high_water": hw, "seed": seed,
            "wallet": wallet, "funding": funding,
            "pnl_usd": eq - seed, "pnl_pct": (eq - seed) / seed * 100 if seed else 0,
            "unrealized": upnl, "mark_equity": mark_eq,
            "mark_pnl_pct": (mark_eq - seed) / seed * 100 if seed else 0,
            "dd_pct": (hw - eq) / hw * 100 if hw > 0 else 0.0,
            "gate": gates.get(gate_kind, False), "positions": positions, "curve": curve,
            "trades": trades, "agg": agg[0] if agg else {},
            "started": curve[0][0] if curve else None}


# ── 스캘핑 실험 엔진(별도 DB) — breakout arm과 스키마가 달라 전용 수집. 고정4 vs 동적고유 A/B ──
_SCDIR = MARKET_SNAPSHOT_PATH.parent


def scalp_summary(db_name, snap_name, label) -> dict | None:
    db = _SCDIR / db_name
    if not db.exists():
        return None
    eqr = q("SELECT equity_usdt, high_water_usdt, initial_usdt FROM equity WHERE key='main'", path=db)
    if not eqr:
        return None
    snap = read_snapshot(_SCDIR / snap_name) or {}
    agg = q("""SELECT COUNT(*) n, SUM(CASE WHEN realized_usdt>0 THEN 1 ELSE 0 END) wins,
               AVG(realized_R) avg_r, SUM(realized_usdt) sum_pnl,
               AVG(realized_usdt/(entry_price*qty)) avg_net, AVG(spread_bp) avg_spread
               FROM trades""", path=db)
    trades = q("SELECT * FROM trades ORDER BY id DESC LIMIT 40", path=db)
    eq = eqr[0]["equity_usdt"]; seed = eqr[0].get("initial_usdt") or 1000.0
    return {"eq": eq, "seed": seed, "mark": snap.get("mark", eq),
            "unreal": snap.get("unrealized", 0.0), "pnl": eq - seed, "label": label,
            "coins": snap.get("coins", []), "agg": agg[0] if agg else {},
            "trades": trades, "updated": snap.get("updated_utc")}


def _scalp_card(sc, rate) -> list:
    """스캘핑 변종 1개 카드 HTML. (고정4/동적고유 각각 호출)"""
    a = sc["agg"]; n = a.get("n") or 0; wins = a.get("wins") or 0
    wr = 100 * wins / n if n else 0
    netbp = (a.get("avg_net") or 0) * 1e4
    avgspread = a.get("avg_spread") or 0
    sumpnl = a.get("sum_pnl") or 0
    mark = sc["mark"]; markpct = (mark - sc["seed"]) / sc["seed"] * 100 if sc["seed"] else 0
    krw = lambda v: f"₩{v*rate:,.0f}"
    pcls = "pos" if sc["pnl"] >= 0 else "neg"; mcls = "pos" if mark >= sc["seed"] else "neg"
    ncls = "pos" if netbp >= 0 else "neg"; scls = "pos" if sumpnl >= 0 else "neg"
    ucls = "pos" if sc["unreal"] > 0 else ("neg" if sc["unreal"] < 0 else "mut")
    p = ['<div class="card" style="border-left:3px solid #6b7280">']
    p.append(f'<div style="font-weight:800;font-size:15px;margin-bottom:10px">🔹 {esc(sc.get("label",""))}</div>')
    p.append('<div style="display:flex;gap:30px;flex-wrap:wrap;margin-bottom:6px">'
             f'<div><span class="mlbl">평가액 (M2M)</span><b class="{mcls}" style="font-size:22px">${mark:,.2f} ({markpct:+.2f}%)</b>'
             f'<div class="muted" style="font-size:12px">{krw(mark)}</div></div>'
             f'<div><span class="mlbl">실현 손익</span><b class="{pcls}" style="font-size:17px">${sc["pnl"]:+,.2f}</b>'
             f'<div class="{pcls}" style="font-size:12px">{("+" if sc["pnl"]>=0 else "")}{krw(sc["pnl"])}</div></div>'
             f'<div><span class="mlbl">미실현</span><b class="{ucls}" style="font-size:17px">${sc["unreal"]:+,.2f}</b></div>'
             f'<div><span class="mlbl">승 / 거래</span><b style="font-size:17px">{wins} / {n} <span class="{("pos" if wr>=50 else "mut")}">({wr:.0f}%)</span></b></div>'
             f'<div><span class="mlbl">누적 수익 (net)</span><b class="{scls}" style="font-size:17px">${sumpnl:+,.2f}</b>'
             f'<div class="{scls}" style="font-size:12px">{("+" if sumpnl>=0 else "")}{krw(sumpnl)}</div></div>'
             f'<div><span class="mlbl">평균 net</span><b class="{ncls}" style="font-size:17px">{netbp:+.1f}bp</b>'
             f'<div class="muted" style="font-size:12px">스프레드 {avgspread:.1f}bp</div></div>'
             '</div>')
    if sc["coins"]:
        p.append('<table style="margin-top:14px"><thead><tr><th>심볼</th><th>현재가</th><th>z</th>'
                 '<th>OFI</th><th>스프레드(실측)</th><th>ATR%</th><th>보유</th><th>미실현$</th></tr></thead><tbody>')
        for c in sc["coins"]:
            held = c.get("held"); hcls = "pos" if held == "Buy" else ("neg" if held == "Sell" else "mut")
            htxt = "롱" if held == "Buy" else ("숏" if held == "Sell" else "–")
            ofi = c.get("ofi", 0); ocls = "neg" if ofi >= 0.35 else ("pos" if ofi <= -0.35 else "mono mut")
            p.append(f'<tr><td><b>{esc(c["symbol"])}</b></td><td class="mono">{c.get("price",0):.4f}</td>'
                     f'<td class="mono">{c.get("z",0):+.1f}</td><td class="{ocls}">{ofi:+.2f}</td>'
                     f'<td class="mono">{c.get("spread_bp",0):.1f}bp</td><td class="mono">{c.get("atrp",0):.2f}%</td>'
                     f'<td class="{hcls}">{htxt}</td><td class="mono">${c.get("u",0):+.2f}</td></tr>')
        p.append('</tbody></table>')
    if sc["trades"]:
        p.append('<table style="margin-top:14px"><thead><tr><th>청산</th><th>심볼</th><th>방향</th>'
                 '<th>진입→청산</th><th>net bp</th><th>R</th><th>USD</th><th>스프레드</th><th>사유</th></tr></thead><tbody>')
        for t in sc["trades"][:8]:
            r = t.get("realized_usdt") or 0; cls = "pos" if r >= 0 else "neg"
            nb = r / ((t["entry_price"] * t["qty"]) or 1) * 1e4
            p.append(f'<tr><td class="mono">{esc((t.get("closed_utc") or "")[5:16])}</td>'
                     f'<td><b>{esc(t["symbol"])}</b></td><td>{"롱" if t["side"]=="Buy" else "숏"}</td>'
                     f'<td class="mono">{t["entry_price"]:.4f}→{(t.get("exit_price") or 0):.4f}</td>'
                     f'<td class="{cls}">{nb:+.1f}</td><td class="{cls}">{(t.get("realized_R") or 0):+.2f}</td>'
                     f'<td class="{cls}">${r:+.2f}</td><td class="mono mut">{(t.get("spread_bp") or 0):.1f}bp</td>'
                     f'<td class="mut">{esc(t.get("close_reason"))}</td></tr>')
        p.append('</tbody></table>')
    else:
        p.append('<div class="muted" style="margin-top:10px">아직 거래 없음 — OFI 임계(±0.35) 도달 시 진입(best-case 페이퍼).</div>')
    p.append('</div>')
    return p


def gather() -> dict:
    d: dict = {}
    d["mode"] = "PAPER" if PAPER_MODE else "LIVE"
    # everything market-related comes from the engine's snapshot file — zero Bybit calls
    snap = read_snapshot(MARKET_SNAPSHOT_PATH) or {}
    d["gates"] = snap.get("gates") or {"none": True, "ema20_50": False, "ema50_200": False}
    d["snap_updated"] = snap.get("updated_utc")
    coins = snap.get("coins") or {}
    snap_prices = {sym: c["price"] for sym, c in coins.items()}
    lm = live_marks()  # one cheap tickers call; {} on failure
    marks = {**snap_prices, **lm}  # live price wins; snapshot price is the fallback
    d["marks"] = marks
    d["marks_live"] = bool(lm)
    if PAPER_MODE:
        d["arms"] = []
        for c in PAPER_ARMS:
            s = _arm_summary(c.label, c.gate, arm_db_path(c.name), d["gates"], marks)
            s["name_key"] = c.name
            d["arms"].append(s)
    else:
        # LIVE: 실전은 지갑이 하나 = 원장도 한 벌(state.db). 같은 지갑을 쓴 1D 암의
        # 거래도 여기 들어있고(trades.arm='live1d'), 어느 암 거래인지는 그 컬럼으로만
        # 구분한다. state_live1d.db는 아카이브 — 읽으면 이중계상이므로 읽지 않는다.
        # 페이퍼 셋은 진짜 별도 장부(다른 자본 경로)라 분리 유지.
        arms_l = []
        live = _arm_summary("실전 (LIVE)", "none", STATE_DB_PATH, d["gates"], marks)
        live["name_key"] = "live"
        # 실전 M2M은 원시값에서 재구성: 평가액 = 시드 + 확정실현(Σ closedPnl) + 미실현.
        # 지갑값(live["equity"])을 그대로 쓰면 실현/미실현 분해가 불가하고,
        # 거기 upnl을 또 더하면 이중계상.
        # 펀딩은 여기 더하지 않는다 — closedPnl이 이미 포함한다(실측 확인).
        # funding은 '실현 중 얼마가 펀딩이었나'를 보여주는 내역일 뿐이다.
        live["mark_equity"] = (live["seed"] + ((live.get("agg") or {}).get("sum_pnl") or 0.0)
                               + live["unrealized"])
        live["mark_pnl_pct"] = (live["mark_equity"] - live["seed"]) / live["seed"] * 100 if live.get("seed") else 0
        # 정산 잔여: 지갑현금 − 시드 − 확정실현. 기장이 완전하면 0.
        live["residual"] = ((live["wallet"] - live["seed"])
                            - ((live.get("agg") or {}).get("sum_pnl") or 0.0)
                            ) if live.get("wallet") is not None else None
        arms_l.append(live)
        paper = _arm_summary("페이퍼 4h (비교)", "none", arm_db_path("ls"), d["gates"], marks)
        paper["name_key"] = "ls"; arms_l.append(paper)
        cand = _arm_summary("페이퍼 cand (ema50/200)", "none", arm_db_path("cand"), d["gates"], marks)
        cand["name_key"] = "cand"; arms_l.append(cand)
        fv = _arm_summary("페이퍼 fveto (펀딩숏베토)", "none", arm_db_path("fveto"), d["gates"], marks)
        fv["name_key"] = "fveto"; arms_l.append(fv)
        # 페이퍼 1D(ls1d)는 중지 — 그 슬롯은 스캘핑 실험(state_scalp.db)이 대체. DB는 보존.
        d["arms"] = arms_l
        d["exp"] = _experiment(live, paper)  # 실전 vs 페이퍼 검증 실험 지표
    held_live, held_paper = {}, {}
    for arm in d["arms"]:
        nk = arm.get("name_key") or ""
        tgt = held_live if nk.startswith("live") else held_paper
        for pos in arm["positions"]:
            # (방향, 1D암 여부) — 라이브는 포지션 태그, 페이퍼는 암 이름으로 판별
            tgt[pos["symbol"]] = (pos["side"], nk.endswith("1d"))
    d["held_live"], d["held_paper"] = held_live, held_paper
    market = []
    for sym in TRADING.SYMBOLS:
        m = coins.get(sym)
        if not m:
            continue
        st, tone = coin_status(m, None)  # 상태=순수 시장신호 / 보유는 별도 실·페 컬럼
        m = dict(m); m["status"] = st; m["tone"] = tone
        m["trend"] = "▲ 상승" if m.get("up") else "▼ 하락"
        market.append(m)
    d["market"] = market
    # 시장 국면 (BTC 기준): ADX<22 횡보 / ema50>200 상승 / 하락은 ADX 강도로 세분
    btc = coins.get("BTCUSDT", {})
    badx = btc.get("adx", 0); uptrend = d["gates"].get("ema50_200")
    if badx < 22:
        d["regime"] = ("⏸ 횡보장", "mut")
    elif uptrend:
        d["regime"] = ("🟢 상승장", "pos")
    elif badx >= 55:
        d["regime"] = (f"🔴 강한 하락장 (ADX {badx:.0f})", "neg")
    elif badx >= 35:
        d["regime"] = (f"🔻 중간 하락장 (ADX {badx:.0f})", "neg")
    else:
        d["regime"] = (f"🔻 약한 하락장 (ADX {badx:.0f})", "neg")
    import glob
    _SCLBL = {"dyn_tk_mra15": "★ 추세정렬 평균회귀 15분 / 150bp", "dyn_tk_mom30": "모멘텀 30분 / 150bp",
              "dyn_tk_mom15": "모멘텀 15분 / 150bp", "dyn_tk_mom15b": "모멘텀 15분 / 300bp",
              "dyn_tk_mom60": "모멘텀 1h / 200bp", "dyn_tk_mom15t": "모멘텀 15분 강추세 / 100bp"}
    d["scalps"] = []
    for dbp in sorted(glob.glob(str(_SCDIR / "state_scalp*.db"))):
        base = os.path.basename(dbp)
        suf = base.replace("state_scalp", "").replace(".db", "").lstrip("_")
        if "tk" not in suf.split("_"):   # 시장가(taker)만 표시 — maker DB는 보존하되 화면 숨김
            continue
        snap = "market_scalp" + (("_" + suf) if suf else "") + ".json"
        s = scalp_summary(base, snap, _SCLBL.get(suf, "scalp " + suf))
        if s:
            s["suf"] = suf
            d["scalps"].append(s)
    d["svc"] = {s: is_active(s) for s in ("metis-v6", "metis-v6-paper", "metis-v6-dashboard")}
    d["scalp_units"] = {u: is_active(u) for u in
        ("metis-v6-scalp-mra15", "metis-v6-scalp-mom30", "metis-v6-scalp-mom15",
         "metis-v6-scalp-mom15b", "metis-v6-scalp-mom60", "metis-v6-scalp-mom15t")}
    d["mem_mi"] = free_mem_mi()
    d["disk"] = disk_pct()
    d["usdkrw"] = get_usdkrw()
    d["now_kst"] = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%H:%M:%S")
    return d


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
/* ── Apple Liquid Glass tokens ── translucent fills, bright specular edges, high-saturation refraction */
:root{
  /* Apple Liquid Glass, carved to the extreme — fill≈0, panels read as glass purely
     from a hairline edge + specular top light + a soft lift shadow on the white page. */
  --glass-blur:blur(26px) saturate(150%);
  --glass-blur-strong:blur(34px) saturate(160%);
  --glass-fill:rgba(255,255,255,.10);
  --glass-fill-soft:rgba(255,255,255,.06);
  --glass-edge:rgba(15,23,42,.10);
  --glass-hi:inset 0 1px 0 rgba(255,255,255,.95),inset 0 -1px 0 rgba(15,23,42,.03);
  --glass-drop:0 6px 22px rgba(15,23,42,.07),0 1px 3px rgba(15,23,42,.05);
}
/* shared specular sheen — a soft diagonal light band that reads as a curved glass surface */
.glass-sheen{position:relative;isolation:isolate}
.glass-sheen::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;background:linear-gradient(135deg,rgba(255,255,255,.5),rgba(255,255,255,0) 42%,rgba(255,255,255,0) 62%,rgba(255,255,255,.2));mix-blend-mode:screen;z-index:0}
body{color:#1a1d24;font:14px/1.5 -apple-system,'Inter',system-ui,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh;background:#ffffff;background-attachment:fixed}
.wrap{max-width:1380px;margin:0 auto;padding:28px 32px 90px}
.topbar{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:space-between;padding:20px 28px;border-radius:22px;background:var(--glass-fill);-webkit-backdrop-filter:var(--glass-blur-strong);backdrop-filter:var(--glass-blur-strong);border:1px solid var(--glass-edge);box-shadow:var(--glass-drop),var(--glass-hi);margin-bottom:26px;gap:24px}
.topbar::after{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(135deg,rgba(255,255,255,.55),rgba(255,255,255,0) 38%);mix-blend-mode:screen}
.brand{display:flex;align-items:center;gap:18px}
.brand-mark{font-size:36px;font-weight:900;letter-spacing:-.04em;color:#0f172a}
.brand-divider{width:1px;align-self:stretch;background:linear-gradient(rgba(15,23,42,.04),rgba(15,23,42,.14),rgba(15,23,42,.04));margin:4px 0}
.brand-title{font-size:13px;font-weight:700;letter-spacing:.22em;color:#0f172a;font-family:'SF Mono',Menlo,monospace}
.brand-title .mode{color:#9ca3af;margin-left:6px}
.brand-sub{font-size:11px;color:#9ca3af;font-weight:500;font-family:'SF Mono',monospace;margin-top:5px}
.topmeta{display:flex;align-items:center;gap:20px;font-size:11.5px;color:#4b5563;font-family:'SF Mono',Menlo,monospace}
.topmeta .lbl{color:#9ca3af;font-weight:600;letter-spacing:.08em;text-transform:uppercase;font-size:10px}
.topmeta b{color:#0f172a;font-weight:700}
.svc-dot{display:inline-block;width:8px;height:8px;border-radius:50%}
.svc-on{background:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.2)}
.svc-off{background:#d1d5db}
.sec-h{display:flex;align-items:baseline;gap:12px;margin:32px 0 14px;padding:0 4px 0 14px;border-left:3px solid rgba(99,102,241,.55)}
.sec-h h2{font-size:19px;font-weight:700;letter-spacing:-.015em;color:#0f172a}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px 22px;align-items:start}
.grid2 .sec-h{margin-top:0}
@media(max-width:1040px){.grid2{grid-template-columns:1fr}}
.col{min-width:0}
.sec-tag{font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.card{position:relative;overflow:hidden;background:var(--glass-fill);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);border:1px solid var(--glass-edge);border-radius:20px;padding:22px 24px;box-shadow:0 6px 24px rgba(31,45,80,.10),var(--glass-hi);margin-bottom:16px;transition:transform .25s ease,box-shadow .25s ease,background .25s ease}
.card::after{content:"";position:absolute;inset:0;pointer-events:none;border-radius:inherit;background:linear-gradient(135deg,rgba(255,255,255,.32),rgba(255,255,255,0) 40%,rgba(255,255,255,0) 66%,rgba(255,255,255,.10));mix-blend-mode:screen}
.card>*{position:relative;z-index:1}
.card:hover{transform:translateY(-2px);background:rgba(15,23,42,.02);box-shadow:0 16px 40px rgba(15,23,42,.12),var(--glass-hi)}
.hero{position:relative;overflow:hidden;color:#0f172a;border-radius:26px;padding:28px 32px;margin-bottom:24px;background:var(--glass-fill);-webkit-backdrop-filter:var(--glass-blur-strong);backdrop-filter:var(--glass-blur-strong);border:1px solid var(--glass-edge);box-shadow:0 22px 58px rgba(15,23,42,.16),var(--glass-hi)}
.hero::before{content:"";position:absolute;top:-45%;left:-8%;width:55%;height:150%;background:radial-gradient(closest-side,rgba(255,255,255,.30),transparent 70%);pointer-events:none;transform:rotate(8deg)}
.hero::after{content:"";position:absolute;inset:0;pointer-events:none;border-radius:inherit;background:linear-gradient(135deg,rgba(255,255,255,.35),rgba(255,255,255,0) 40%,rgba(255,255,255,0) 66%,rgba(255,255,255,.12));mix-blend-mode:screen}
.hero>*{position:relative;z-index:1}
.hero-top{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;padding-bottom:20px;border-bottom:1px solid rgba(15,23,42,.10);margin-bottom:22px}
.hero-brand{display:flex;align-items:center;gap:20px}
.hero-mark{font-size:48px;font-weight:900;letter-spacing:-.04em;color:#0f172a;line-height:1}
.hero-div{width:1px;align-self:stretch;background:rgba(15,23,42,.14)}
.hero-name{font-size:15px;font-weight:800;letter-spacing:.3em;color:#0f172a;font-family:'SF Mono',Menlo,monospace}
.hero-sub{font-size:11px;color:#64748b;margin-top:6px;font-family:'SF Mono',monospace}
.hero-meta{display:flex;flex-wrap:wrap;gap:14px 16px;font-size:11px;color:#475569;font-family:'SF Mono',monospace;align-items:center;justify-content:flex-end}
.hero-meta .lbl{color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;font-size:9.5px}
.hero-meta b{color:#0f172a}
.live-pill{padding:6px 14px;background:rgba(220,38,38,.14);border:1px solid rgba(220,38,38,.35);border-radius:11px;-webkit-backdrop-filter:blur(14px) saturate(180%);backdrop-filter:blur(14px) saturate(180%);box-shadow:inset 0 1px 0 rgba(255,255,255,.5),0 2px 10px rgba(220,38,38,.12)}
.live-pill .lbl{color:#dc2626}.live-pill b{color:#b91c1c;font-size:14px}
.hero-kpi{display:flex;gap:36px;flex-wrap:wrap;align-items:flex-end}
.kpi .kl{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.07em;font-weight:700;display:block;margin-bottom:7px}
.kpi .kv{font-size:30px;font-weight:800;color:#0f172a;line-height:1;display:block}
.kpi .kv.sm{font-size:21px}
.kpi .kk{font-size:12px;color:#64748b;margin-top:6px;display:block}
.kpi .pos,.rbadge .pos{color:#059669}.kpi .neg,.rbadge .neg{color:#dc2626}
.rbadge{margin-left:auto;text-align:right}.rbadge .rv{font-size:19px;font-weight:800;margin-top:2px}.rbadge .mut{color:#94a3b8}
.pos{color:#059669;font-weight:700}.neg{color:#dc2626;font-weight:700}.mut{color:#6b7280;font-weight:600}
.muted{color:#64748b;font-size:13px}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;font-size:10.5px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;text-align:left;padding:11px 12px;border-bottom:1px solid rgba(15,23,42,.10);background:rgba(248,250,252,.72);-webkit-backdrop-filter:blur(16px) saturate(160%);backdrop-filter:blur(16px) saturate(160%)}
td{padding:11px 12px;font-size:12.5px;border-bottom:1px solid rgba(15,23,42,.06)}
tr:hover td{background:rgba(15,23,42,.03)}
td.mono{font-family:'SF Mono',Menlo,monospace;font-size:11.5px;color:#6b7280}
.badge-on{padding:4px 10px;font-size:11px;font-weight:700;background:rgba(16,185,129,.14);color:#047857;border-radius:9px;border:1px solid rgba(16,185,129,.30);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);box-shadow:inset 0 1px 0 rgba(255,255,255,.4)}
.mlbl{display:block;font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:3px}
.mini-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px 18px}
.lead td{font-weight:800;background:rgba(16,185,129,.10)}
select{-webkit-backdrop-filter:blur(14px) saturate(180%);backdrop-filter:blur(14px) saturate(180%);box-shadow:inset 0 1px 0 rgba(255,255,255,.5),0 2px 8px rgba(31,45,80,.06);cursor:pointer}
footer{margin-top:40px;padding-top:20px;border-top:1px solid rgba(120,130,160,.16);font-size:11.5px;color:#9ca3af;text-align:center}
"""


def render(d: dict, card=None) -> str:
    rate = d["usdkrw"]; arms = d["arms"]; gates = d["gates"]; mem = d["mem_mi"]
    # snapshot freshness — the dashboard shows engine data, flag it if the engine stalled
    snap_txt, snap_stale = "데이터 없음 — 엔진 첫 사이클 대기", True
    su = d.get("snap_updated")
    if su:
        try:
            sdt = datetime.fromisoformat(su)
            age = (datetime.now(timezone.utc) - sdt).total_seconds()
            snap_stale = age > 5 * 3600  # one 4h cycle + buffer
            kst = (sdt + timedelta(hours=9)).strftime("%m-%d %H:%M")
            snap_txt = f"{kst} KST 기준" + (" ⚠ 갱신 지연(엔진 점검)" if snap_stale else "")
        except Exception:
            pass
    on, off = '<span class="svc-dot svc-on"></span>', '<span class="svc-dot svc-off"></span>'
    p: list[str] = [f'<!doctype html><html><head><meta charset="utf-8"><title>METIS · {d["mode"]}</title>',
                    f'<style>{CSS}</style></head><body><div class="wrap">']

    # ── 통합 히어로: METIS 브랜딩 + 핵심 KPI(USD/KRW) + 시장국면 ──
    _live = next((a for a in d["arms"] if a.get("name_key") == "live"), None) or (d["arms"][0] if d["arms"] else None)
    _live_up = _uptime(_live["started"]) if _live else "–"
    krw = lambda v: f"₩{v*rate:,.0f}"
    h = ['<div class="hero"><div class="hero-top">'
         '<div class="hero-brand"><div class="hero-mark">METIS</div><div class="hero-div"></div>'
         '<div><div class="hero-name">Rule Trader</div>'
         '<div class="hero-sub">8코인 4h ls(롱+숏) mixed 돌파 · 실전 LIVE + 페이퍼 + 단타실험</div></div></div>'
         '<div class="hero-meta">'
         f'<span class="live-pill"><span class="lbl">🔴 실전가동</span> <b>{_live_up}</b></span>'
         f'<span><span class="lbl">engine</span> {on if d["svc"].get("metis-v6") else off}</span>'
         f'<span><span class="lbl">kst</span> <b>{esc(d["now_kst"])}</b></span>'
         f'<span><span class="lbl">fx</span> <b>₩{rate:,.0f}</b></span>'
         f'<span><span class="lbl">mem</span> <b>{mem}Mi</b></span>'
         f'<span><span class="lbl">disk</span> <b>{esc(d["disk"])}</b></span>'
         '</div></div><div class="hero-kpi">']
    if _live:
        seed = _live["seed"]; meq = _live["mark_equity"]
        u = _live["unrealized"]; npos = len(_live["positions"])
        rpnl = (_live.get("agg") or {}).get("sum_pnl") or 0.0   # 확정 실현 = 청산거래 net 합(Σ closedPnl)
        mpnl = meq - seed                                        # = 실현 + 미실현 (재구성 → 항상 정합)
        mpct = (mpnl / seed * 100) if seed else 0; rpct = (rpnl / seed * 100) if seed else 0
        mc = "pos" if mpnl >= 0 else "neg"; rc = "pos" if rpnl >= 0 else "neg"
        uc = "pos" if u >= 0 else ("neg" if u < 0 else "mut")
        fnd = _live.get("funding") or 0.0
        resid = _live.get("residual")
        h.append(f'<div class="kpi"><span class="kl">실전 평가액 (M2M)</span>'
                 f'<span class="kv {mc}">${meq:,.2f}</span><span class="kk">{krw(meq)}</span></div>')
        h.append(f'<div class="kpi"><span class="kl">평가 손익 (실현+미실현)</span>'
                 f'<span class="kv {mc}">${mpnl:+,.0f} <span style="font-size:17px">({mpct:+.1f}%)</span></span>'
                 f'<span class="kk {mc}">{("+" if mpnl>=0 else "")}{krw(mpnl)}</span></div>')
        h.append(f'<div class="kpi"><span class="kl">실현 손익 (확정)</span>'
                 f'<span class="kv sm {rc}">${rpnl:+,.2f}</span>'
                 f'<span class="kk">{rpct:+.2f}% · 그중 펀딩 ${fnd:+.2f}</span></div>')
        # 정산 잔여 = 지갑현금 − (시드+실현+펀딩). 기장이 완전하면 0. 0이 아니면 숨기지 않는다.
        rtxt = ("정산 ✓" if resid is not None and abs(resid) <= 0.10
                else (f"⚠ 잔여 ${resid:+.2f}" if resid is not None else "정산 –"))
        rcls = "mut" if (resid is not None and abs(resid) <= 0.10) else "neg"
        h.append(f'<div class="kpi"><span class="kl">미실현 · 보유</span>'
                 f'<span class="kv sm {uc}">${u:+,.2f}</span>'
                 f'<span class="kk {rcls}">{npos}/{RISK.MAX_CONCURRENT} 보유 · 시드 ${seed:,.0f} · {rtxt}</span></div>')
    regime_txt, regime_cls = d.get("regime", ("-", "mut"))
    h.append(f'<div class="rbadge"><span class="kl" style="color:#8b8f98">시장 국면 (BTC)</span>'
             f'<div class="rv {regime_cls}">{esc(regime_txt)}</div>'
             f'<div class="kk" style="color:{"#fca5a5" if snap_stale else "#8b8f98"}">📡 {esc(snap_txt)}</div></div>')
    h.append('</div></div>')
    p.append("".join(h))

    # ── combined open positions (현황 먼저 — 위로) ──
    marks = d["marks"]
    pos_rows = []
    for arm in arms:
        for pos in arm["positions"]:
            mk = marks.get(pos["symbol"], 0.0); dd = 1 if pos["side"] == "Buy" else -1
            u = dd * (mk - pos["entry_price"]) * pos["qty"] if mk > 0 else 0.0
            risk = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
            rr = u / risk if risk > 0 else 0.0
            ucls = "pos" if u >= 0 else "neg"
            vlabel = arm["label"].split()[0] + ("·1D" if (pos.get("arm") or "").endswith("1d") else "")
            pos_rows.append(
                f'<tr><td><b>{esc(vlabel)}</b></td><td><b>{esc(pos["symbol"])}</b></td>'
                f'<td>{"롱" if pos["side"]=="Buy" else "숏"} x{pos["leverage"]}</td>'
                f'<td class="mono">{pos["qty"]}</td><td class="mono">{pos["entry_price"]:.4f}</td>'
                f'<td class="mono">{pos["stop_price"]:.4f}</td><td class="mono">{mk:.4f}</td>'
                f'<td class="{ucls}">${u:+.2f}</td><td class="{ucls}">{rr:+.2f}R</td></tr>')
    live_tag = "미실현 실시간 (ticker)" if d.get("marks_live") else "미실현 · 스냅샷 가격(ticker 실패)"
    p.append(f'<div class="sec-h" style="margin-top:0"><h2>🎯 보유 포지션</h2><span class="sec-tag">변종별 · {live_tag}</span></div>')
    if pos_rows:
        p.append('<div class="card"><table><thead><tr><th>변종</th><th>심볼</th><th>방향</th><th>수량</th>'
                 '<th>진입</th><th>SL</th><th>현재가</th><th>미실현$</th><th>R</th></tr></thead><tbody>')
        p.append("".join(pos_rows)); p.append('</tbody></table></div>')
    else:
        p.append('<div class="card"><div class="muted">⏸ 보유 포지션 없음 — 돌파 + 게이트 열림 대기</div></div>')

    # ── arm comparison (centerpiece) ──
    lead = max(range(len(arms)), key=lambda i: arms[i]["mark_equity"]) if arms else -1
    p.append('<div class="sec-h" style="margin-top:0"><h2>⚔️ 실전 vs 페이퍼</h2>'
             '<span class="sec-tag">같은 신호 · 실전=실체결, 페이퍼=가정 · 슬리피지·성과 비교</span></div>')
    p.append('<div class="card"><table><thead><tr><th>구분</th><th>가동</th><th>평가액(M2M)</th><th>수익률</th>'
             '<th>미실현</th><th>MDD</th><th>거래</th><th>승률</th><th>평균R</th><th>PF</th><th>보유</th></tr></thead><tbody>')
    for i, arm in enumerate(arms):
        a = arm["agg"] or {}
        n = a.get("n") or 0; wins = a.get("wins") or 0
        wr = wins / n * 100 if n else 0
        avg_r = a.get("avg_r") or 0
        gw = a.get("gw") or 0; gl = abs(a.get("gl") or 0)
        pf = f"{gw / gl:.2f}" if gl > 0 else ("∞" if gw > 0 else "–")
        mpnl = arm["mark_pnl_pct"]
        pcls = "pos" if mpnl > 0 else ("neg" if mpnl < 0 else "mut")
        u = arm["unrealized"]; ucls = "pos" if u > 0 else ("neg" if u < 0 else "mut")
        lead_cls = ' class="lead"' if i == lead and n + len(arm["positions"]) > 0 else ""
        p.append(f'<tr{lead_cls}><td><b>{esc(arm["label"])}</b></td>'
                 f'<td class="muted" style="font-size:11px">{_uptime(arm["started"])}</td>'
                 f'<td class="mono">${arm["mark_equity"]:,.0f}</td>'
                 f'<td class="{pcls}">{mpnl:+.2f}%</td>'
                 f'<td class="{ucls}">{("$%+.2f" % u) if arm["positions"] else "–"}</td>'
                 f'<td class="mut">{arm["dd_pct"]:.1f}%</td>'
                 f'<td>{n}</td><td>{wr:.0f}%</td>'
                 f'<td class="{"pos" if avg_r>=0 else "neg"}">{avg_r:+.2f}R</td>'
                 f'<td>{pf}</td><td>{len(arm["positions"])}</td></tr>')
    p.append('</tbody></table>'
             '<div class="muted" style="margin-top:10px">평가액 = 실현 + 미실현(현재가 M2M). '
             '실전(LIVE)·페이퍼가 같은 ls(롱+숏) 돌파 신호를 받음 — 실전은 실제 체결(슬리피지·수수료 반영), '
             '페이퍼는 가정 체결. 둘의 차이 = 실거래 비용. 초록 행 = 선두.</div></div>')

    # ── 스캘핑 실험 A/B (고정4 vs 동적고유) — 실전vs페이퍼 바로 밑 ──
    variants = d.get("scalps") or []
    if variants:
        p.append('<div class="sec-h"><h2>🔬 스캘핑 실험 (페이퍼 · 전 시장가)</h2>'
                 '<span class="sec-tag">OFI역행 · 전부 시장가+보수슬립5bp(라이브 직결) · 유니버스×신호×익절</span></div>')
        if variants:
            p.append('<div class="card"><table><thead><tr><th>변종</th><th>평가액(M2M)</th><th>실현</th>'
                     '<th>승률</th><th>거래</th><th>평균net</th><th>누적net</th></tr></thead><tbody>')
            for s in variants:
                a = s["agg"]; n = a.get("n") or 0; w = a.get("wins") or 0
                wr = 100 * w / n if n else 0; nb = (a.get("avg_net") or 0) * 1e4; sp = a.get("sum_pnl") or 0
                mkpct = (s["mark"] - s["seed"]) / s["seed"] * 100 if s["seed"] else 0
                mcls = "pos" if s["mark"] >= s["seed"] else "neg"
                p.append(f'<tr><td><b>{esc(s["label"])}</b></td>'
                         f'<td class="mono {mcls}">${s["mark"]:,.2f} ({mkpct:+.2f}%)</td>'
                         f'<td class="{"pos" if s["pnl"]>=0 else "neg"}">${s["pnl"]:+.2f}</td>'
                         f'<td class="{("pos" if wr>=50 else "mut")}">{wr:.0f}%</td><td>{n}</td>'
                         f'<td class="{("pos" if nb>=0 else "neg")}">{nb:+.1f}bp</td>'
                         f'<td class="{("pos" if sp>=0 else "neg")}">${sp:+.2f}</td></tr>')
            p.append('</tbody></table></div>')
        # 상세 거래 카드는 1개만(가독성) — 드롭다운으로 변종 선택(URL ?card= 이동)
        opts = "".join(
            f'<option value="{esc(s["suf"])}"{" selected" if s["suf"] == card else ""}>{esc(s["label"])}</option>'
            for s in variants)
        p.append('<div style="margin:4px 0 12px;padding:0 4px">'
                 '<span class="muted" style="font-size:12px">상세 거래 변종 선택 ▸ </span>'
                 "<select onchange=\"location.href='/?card='+this.value\" "
                 'style="padding:5px 11px;border:1px solid rgba(255,255,255,.6);border-radius:9px;font-size:13px;'
                 f'background:rgba(255,255,255,.35);font-weight:600">{opts}</select></div>')
        rep = next((s for s in variants if s["suf"] == card), None) or \
              next((s for s in variants if "★" in s.get("label", "")), variants[0])
        p.extend(_scalp_card(rep, rate))

    # ── mark-to-market equity curves ──
    p.append('<div class="sec-h"><h2>📈 평가액 추이</h2>'
             '<span class="sec-tag">M2M(실현+미실현) · 4h 사이클마다 1점 · 점선 = 시드</span></div>')
    p.append('<div class="card">' + _multi_chart(arms) + '</div>')
    crows = []
    for arm in arms:
        spark = _spark([m for _, m in arm["curve"]], arm["seed"])
        u = arm["unrealized"]; ucls = "pos" if u > 0 else ("neg" if u < 0 else "mut")
        mcls = "pos" if arm["mark_equity"] >= arm["seed"] else "neg"
        crows.append(f'<tr><td style="white-space:nowrap"><b>{esc(arm["label"])}</b></td>'
                     f'<td style="width:99%;padding-right:20px">{spark}</td>'
                     f'<td class="mono {mcls}" style="white-space:nowrap">${arm["mark_equity"]:,.2f}</td>'
                     f'<td class="mono {ucls}" style="white-space:nowrap">${u:+.2f}</td></tr>')
    p.append('<div class="card"><table><thead><tr><th style="white-space:nowrap">변종</th>'
             '<th style="width:99%">곡선</th><th style="white-space:nowrap">평가액</th>'
             '<th style="white-space:nowrap">미실현</th></tr></thead><tbody>'
             + "".join(crows) + '</tbody></table></div>')

    # ── 검증 실험: 실전 vs 페이퍼 (같은 신호, 실체결 vs 가정체결) ──
    exp = d.get("exp")
    if exp:
        p.append('<div class="sec-h"><h2>🧪 검증 실험</h2>'
                 '<span class="sec-tag">실전 vs 페이퍼 · 같은 신호 페어 · 비용 양수=불리</span></div>')
        p.append('<div class="card">')
        ea = exp["ent_avg"]; xa = exp["exi_avg"]
        ent_txt = f'{ea:+.1f}bp' if ea is not None else '–'
        ent_cls = "mut" if ea is None else ("pos" if ea <= 2.0 else "neg")
        exi_txt = f'{xa:+.1f}bp' if xa is not None else '대기 (청산 0건)'
        verdict = ('–' if ea is None else
                   ('✓ 가정(+2bp)보다 좋음' if ea < 2.0 else ('≈ 가정 수준' if ea <= 3.0 else '✗ 가정보다 나쁨')))
        p.append('<div class="mini-grid">'
                 f'<div><span class="mlbl">진입비용 실측(n={exp["n_ent"]})</span><b class="{ent_cls}">{ent_txt}</b></div>'
                 f'<div><span class="mlbl">페이퍼 가정</span><b>+2.0bp</b></div>'
                 f'<div><span class="mlbl">판정</span><b>{verdict}</b></div>'
                 f'<div><span class="mlbl">청산비용 실측(n={exp["n_exi"]})</span><b>{exi_txt}</b></div>'
                 f'<div><span class="mlbl">증명 표본(청산)</span><b>{exp["n_closed"]}건 · net ${exp["net"]:+.2f}</b></div>'
                 '</div>')
        if exp["pairs"]:
            p.append('<table style="margin-top:14px"><thead><tr><th>진입(UTC)</th><th>심볼</th><th>방향</th>'
                     '<th>상태</th><th>신호가</th><th>실전체결</th><th>페이퍼체결</th>'
                     '<th>실전bp</th><th>페이퍼bp</th><th>R 실/페</th></tr></thead><tbody>')
            for q_ in exp["pairs"]:
                lb = f'{q_["lbp"]:+.1f}' if q_["lbp"] is not None else '–'
                pb = f'{q_["pbp"]:+.1f}' if q_["pbp"] is not None else '–'
                lcls = "mut" if q_["lbp"] is None else ("pos" if q_["lbp"] <= (q_["pbp"] or 2) else "neg")
                rtxt = (f'{q_["lR"]:+.2f} / {q_["pR"]:+.2f}'
                        if q_["lR"] is not None and q_["pR"] is not None else '–')
                p.append(f'<tr><td class="mono">{esc(q_["t"])}</td><td><b>{esc(q_["sym"])}</b></td>'
                         f'<td>{"롱" if q_["side"]=="Buy" else "숏"}</td><td class="mut">{q_["st"]}</td>'
                         f'<td class="mono">{q_["ref"]:.4f}</td><td class="mono">{q_["lf"]:.4f}</td>'
                         f'<td class="mono">{q_["pf"]:.4f}</td>'
                         f'<td class="{lcls}">{lb}</td><td class="mono mut">{pb}</td>'
                         f'<td class="mono">{rtxt}</td></tr>')
            p.append('</tbody></table>')
        else:
            p.append('<div class="muted" style="margin-top:10px">아직 실전·페이퍼 동시 진입 페어 없음 — 다음 공통 신호부터 자동 누적</div>')
        p.append('</div>')

    # ── shared market state ──
    p.append('<div class="sec-h"><h2>📡 코인별 시장 현황</h2>'
             '<span class="sec-tag">봇과 동일 지표 · 페이지 로드 시점</span></div>')
    if d["market"]:
        p.append('<div class="card"><table><thead><tr><th>심볼</th><th>현재가</th><th>추세(EMA20/50)</th>'
                 '<th>ADX</th><th>ATR%</th><th>롱돌파선(신고가)</th><th>롱까지</th>'
                 '<th>숏돌파선(신저가)</th><th>숏까지</th><th>보유 실/페</th><th>상태</th></tr></thead><tbody>')
        for m in d["market"]:
            tr_cls = "pos" if "상승" in m["trend"] else "neg"
            adx_cls = "pos" if m["trending"] else "mut"
            st_cls = {"pos": "pos", "neg": "neg", "hold": "mut", "mut": "mut"}.get(m["tone"], "mut")
            bl = m.get("brk_lo"); dl = m.get("dist_lo")
            # 진입 임박 강조: 롱후보면 롱까지 초록·굵게, 숏후보면 숏까지 빨강·굵게
            hi_cls = "pos" if m.get("long_cand") else "mono mut"
            lo_cls = "neg" if m.get("short_cand") else "mono mut"
            hl = d["held_live"].get(m["symbol"]); hp = d["held_paper"].get(m["symbol"])
            p.append(f'<tr><td><b>{esc(m["symbol"])}</b></td><td class="mono">{m["price"]:.4f}</td>'
                     f'<td class="{tr_cls}">{m["trend"]}</td>'
                     f'<td class="{adx_cls}">{m["adx"]:.0f}{" ✓" if m["trending"] else ""}</td>'
                     f'<td class="mono">{m["atrp"]:.2f}%</td>'
                     f'<td class="mono">{m["brk"]:.4f}</td><td class="{hi_cls}">{m["dist_hi"]:+.1f}%</td>'
                     f'<td class="mono">{("%.4f" % bl) if bl is not None else "–"}</td>'
                     f'<td class="{lo_cls}">{("%+.1f%%" % dl) if dl is not None else "–"}</td>'
                     f'<td class="mono">실 {_side_html(hl)} · 페 {_side_html(hp)}</td>'
                     f'<td class="{st_cls}">{esc(m["status"])}</td></tr>')
        p.append('</tbody></table>'
                 '<div class="muted" style="margin-top:10px">ls(롱+숏) mixed · ADX&gt;22 추세장만 · '
                 '롱돌파=Donchian 신고가 위로(가격 올라야 진입) / 숏돌파=신저가 아래로(가격 내려야 진입) · '
                 '롱까지/숏까지 = 돌파선까지 거리(작을수록 임박, 0%면 돌파) · 롱=추세종료까지, 숏=2.5R 익절 · '
                 '강한 하락(ADX55+)서 숏 엣지 · 보유 실/페 = 실전·페이퍼 엔진별 보유 방향(별개 표시)</div></div>')
    else:
        p.append('<div class="card"><div class="muted">시장 데이터 로드 실패</div></div>')

    # ── 전체 거래 내역: 4암 통합 단일 테이블 — 청산시각 역순, 암은 배지로 구분.
    #    실전 행 = 굵은 배지 + 옅은 배경(한눈에 튐), 페이퍼 행 = 회색 배지.
    #    같은 신호의 실·페 짝은 청산시각이 붙어 위아래로 나란히 → 슬리피지 비교 즉시 가능.
    _BADGE = {"ls": "페 · 4h", "ls1d": "페 · 1D"}
    merged = []
    for arm in arms:
        nk = arm.get("name_key") or ""
        is_live = nk.startswith("live")
        for t in arm["trades"]:
            # 라이브는 단일 원장 — 어느 암 거래인지는 trades.arm 컬럼으로 표기
            tag = ("실 · 1D" if (t.get("arm") or "").endswith("1d") else "실 · 4h") if is_live else _BADGE.get(nk, arm["label"])
            merged.append(((t.get("closed_utc") or ""), tag, is_live, t))
    merged.sort(key=lambda x: x[0], reverse=True)
    n_live = sum(1 for m in merged if m[2])
    p.append('<div class="sec-h"><h2>전체 거래 내역</h2>'
             f'<span class="sec-tag">4암 통합 {len(merged)}건 (실전 {n_live}) · 최신순 · 슬리피지(신호가 vs 체결) · 스크롤</span></div>')
    p.append('<div class="card" style="max-height:360px;overflow-y:auto;padding:14px 16px">')
    if merged:
        p.append('<table><thead><tr><th>청산</th><th>암</th><th>심볼</th><th>방향</th>'
                 '<th>진입→청산</th><th>슬립</th><th>R</th><th>USD</th><th>사유</th></tr></thead><tbody>')
        for closed, tag, is_live, t in merged:
            r = float(t.get("realized_usdt") or 0); cls = "pos" if r >= 0 else "neg"
            re = float(t.get("ref_entry") or 0); ep = float(t.get("entry_price") or 0)
            slip = f'{(ep - re) / re * 10000:+.0f}bp' if re > 0 else '-'
            row_st = ' style="background:rgba(16,185,129,.06)"' if is_live else ''
            arm_td = f'<b>{tag}</b>' if is_live else f'<span class="mut">{tag}</span>'
            p.append(f'<tr{row_st}><td class="mono">{esc(closed[5:16])}</td>'
                     f'<td style="white-space:nowrap">{arm_td}</td>'
                     f'<td><b>{esc(t["symbol"])}</b></td><td>{"롱" if t["side"]=="Buy" else "숏"}</td>'
                     f'<td class="mono">{(t.get("entry_price") or 0):.4f}→{(t.get("exit_price") or 0):.4f}</td>'
                     f'<td class="mono mut">{slip}</td>'
                     f'<td class="{cls}">{(t.get("realized_R") or 0):+.2f}</td>'
                     f'<td class="{cls}">${r:+.2f}</td><td class="mut">{esc(t.get("close_reason"))}</td></tr>')
        p.append('</tbody></table>')
    else:
        p.append('<div class="muted">아직 거래 없음 — 첫 진입부터 집계</div>')
    p.append('</div>')

    # ── system ──
    p.append('<div class="sec-h"><h2>시스템 상태</h2><span class="sec-tag">systemd</span></div>')
    p.append('<div class="card"><div class="mini-grid">')
    p.append(f'<div><span class="mlbl">engine</span><b>{"ACTIVE" if d["svc"].get("metis-v6") else "DOWN"}</b></div>')
    p.append(f'<div><span class="mlbl">paper</span><b>{"ACTIVE" if d["svc"].get("metis-v6-paper") else "DOWN"}</b></div>')
    su = d.get("scalp_units", {}); nsc = sum(1 for v in su.values() if v)
    p.append(f'<div><span class="mlbl">scalp 변종</span><b>{nsc}/{len(su)} active</b></div>')
    p.append(f'<div><span class="mlbl">dashboard</span><b>{"ACTIVE" if d["svc"].get("metis-v6-dashboard") else "DOWN"}</b></div>')
    p.append(f'<div><span class="mlbl">mem used</span><b>{mem} Mi</b></div>')
    p.append(f'<div><span class="mlbl">disk free</span><b>{esc(d["disk"])}</b></div>')
    p.append('</div></div>')

    p.append(f'<footer>METIS-System · {esc(d["mode"])} · 8코인 4h ls(롱+숏) 돌파 mixed · 실전 LIVE + 페이퍼 비교 · USD/KRW {rate:,.0f} · F5</footer>')
    p.append('</div></body></html>')
    return "".join(p)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        card = parse_qs(u.query).get("card", [None])[0]
        try:
            body = render(gather(), card).encode("utf-8")
            self.send_response(200)
        except Exception as e:
            body = f"error: {e}".encode(); self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    print(f"METIS v6 dashboard on 0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
