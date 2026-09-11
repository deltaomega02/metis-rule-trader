"""Exhaustive base-strategy sweep — test ALL timeframes × MANY
indicator families × wide param grids, honestly, to see if anything beats the
deployed BREAKOUT-4h edge out-of-sample.

Design that keeps it fair and fast:
- ENTRY SIGNAL varies (9 indicator families, wide params). EXIT machinery is the
  proven structural one (ATR stop + EMA50 trend-exit / fixed-R), shared by all —
  this isolates "is there a better ENTRY signal?" from exit tuning (exits already
  swept exhaustively elsewhere). trades_coin does the sim.
- Every entry signal is causal (uses only bars <= i). IS/OOS = calendar 70/30 on
  the TF's BTC timeline; walk-forward = 5 expanding folds. A config only "wins" if
  it beats the deployed baseline OOS *and* is positive in >=4/5 folds.
- Multiprocessing over (tf, family, sigparams) tasks; per-worker precompute cache.
  Results streamed to CSV + a ranked summary JSON. Progress to a log file.

Coverage is logged explicitly (cells run, cells skipped). Run:
  python3 exhaustive_sweep.py            # full sweep, background
Env: METIS_TFS (comma list, default all), METIS_QUICK=1 (tiny grid smoke test).
"""
from __future__ import annotations
import os, sys, json, time, itertools, csv
import numpy as np
from pathlib import Path
import multiprocessing as mp

from edge_scan import precompute
from exit_redesign import trades_coin
from validate_breakout import portfolio_sim
from backtest_trend_15m import ema, atr, adx

DATA = Path(__file__).resolve().parent / "data"
OUT  = Path(__file__).resolve().parent / "sweep_out"
OUT.mkdir(exist_ok=True)
LOG  = OUT / "progress.log"

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

# ---------- universe per timeframe (files that exist on disk) ----------
ALL23 = [c+"USDT" for c in ["AAVE","ADA","APT","ARB","ATOM","AVAX","BNB","BTC","DOGE","DOT",
         "ETH","FIL","INJ","LINK","LTC","NEAR","OP","SOL","SUI","TON","TRX","UNI","XRP"]]
MAJ8  = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT"]
FIVE  = [c+"USDT" for c in ["AAVE","ADA","AGLD","AVAX","BNB","BTC","DOGE","ETH","HYPE","LAB",
         "SOL","SOXL","SUI","WLD","XRP","ZEC"]]
COINS_TF = {"4h": ALL23, "1h": MAJ8, "15m": ["BTCUSDT","ETHUSDT","SOLUSDT"],
            "5": FIVE, "1d": ALL23}
TF_H = {"4h": 4, "1h": 1, "15m": 0.25, "5": 1/12, "1d": 24}

# ---------- causal indicators ----------
def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    ru = _wilder(up, n); rd = _wilder(dn, n)
    return 100 - 100/(1 + ru/np.maximum(rd, 1e-12))
def _wilder(x, n):
    a = 1/n; o = np.empty_like(x, float); o[0] = x[0]
    for i in range(1, len(x)): o[i] = a*x[i] + (1-a)*o[i-1]
    return o
def macd(c, f=12, s=26, sig=9):
    m = ema(c, f) - ema(c, s); return m, ema(m, sig)
def stoch(h, l, c, k=14, d=3):
    lo = np.array([l[max(0,i-k+1):i+1].min() for i in range(len(c))])
    hi = np.array([h[max(0,i-k+1):i+1].max() for i in range(len(c))])
    kf = 100*(c-lo)/np.maximum(hi-lo, 1e-12)
    df = np.convolve(kf, np.ones(d)/d, mode="full")[:len(kf)]
    return kf, df
def roll_max(x, n): return np.array([x[max(0,i-n):i].max() if i>0 else x[0] for i in range(len(x))])
def roll_min(x, n): return np.array([x[max(0,i-n):i].min() if i>0 else x[0] for i in range(len(x))])
def boll(c, n=20, k=2.0):
    ma = np.convolve(c, np.ones(n)/n, mode="full")[:len(c)]
    sd = np.array([c[max(0,i-n+1):i+1].std() for i in range(len(c))])
    return ma - k*sd, ma + k*sd
def keltner(h, l, c, n=20, m=2.0):
    ma = ema(c, n); a = atr(h, l, c, 14); return ma - m*a, ma + m*a

# ---------- signal families -> dirs (int8 +1/-1/0 per bar, causal) ----------
def make_dirs(fam, p, o, h, l, c):
    n = len(c); d = np.zeros(n, np.int8)
    if fam == "DONCHIAN":
        N, ep, axmin = p["N"], p["ema"], p["adxmin"]
        hi = roll_max(h, N); lo = roll_min(l, N)
        gate_ok = np.ones(n, bool)
        if ep:
            ef, es = ema(c, ep[0]), ema(c, ep[1])
        ax = adx(h, l, c, 14) if axmin > 0 else np.full(n, 99.0)
        for i in range(N+1, n):
            if ax[i] <= axmin: continue
            up = (not ep) or ef[i] > es[i]; dnn = (not ep) or ef[i] < es[i]
            if c[i] > hi[i] and up: d[i] = 1
            elif c[i] < lo[i] and dnn: d[i] = -1
    elif fam == "EMACROSS":
        ef, es = ema(c, p["f"]), ema(c, p["s"])
        for i in range(p["s"]+1, n):
            if ef[i] > es[i] and ef[i-1] <= es[i-1]: d[i] = 1
            elif ef[i] < es[i] and ef[i-1] >= es[i-1]: d[i] = -1
    elif fam == "BOLL_MR":
        bl, bu = boll(c, p["N"], p["k"])
        for i in range(p["N"]+1, n):
            if c[i] < bl[i]: d[i] = 1      # oversold -> long
            elif c[i] > bu[i]: d[i] = -1
    elif fam == "KELTNER":
        kl, ku = keltner(h, l, c, p["N"], p["m"])
        for i in range(p["N"]+1, n):
            if c[i] > ku[i]: d[i] = 1
            elif c[i] < kl[i]: d[i] = -1
    elif fam == "RSI":
        r = rsi(c, p["n"]); lo_b, hi_b = p["band"]
        for i in range(p["n"]+1, n):
            if p["mode"] == "revert":
                if r[i] < lo_b: d[i] = 1
                elif r[i] > hi_b: d[i] = -1
            else:  # trend: cross of 50
                if r[i] > 50 and r[i-1] <= 50: d[i] = 1
                elif r[i] < 50 and r[i-1] >= 50: d[i] = -1
    elif fam == "MACD":
        m, sg = macd(c, *p["p"])
        for i in range(p["p"][1]+p["p"][2]+1, n):
            if m[i] > sg[i] and m[i-1] <= sg[i-1]: d[i] = 1
            elif m[i] < sg[i] and m[i-1] >= sg[i-1]: d[i] = -1
    elif fam == "STOCH":
        kf, df = stoch(h, l, c, *p["kd"]); lo_b, hi_b = p["band"]
        for i in range(p["kd"][0]+p["kd"][1]+1, n):
            if kf[i] < lo_b and kf[i] > df[i] and kf[i-1] <= df[i-1]: d[i] = 1
            elif kf[i] > hi_b and kf[i] < df[i] and kf[i-1] >= df[i-1]: d[i] = -1
    elif fam == "VOLBREAK":
        N = p["N"]; hi = roll_max(h, N); lo = roll_min(l, N)
        for i in range(N+1, n):
            if c[i] > hi[i]: d[i] = 1
            elif c[i] < lo[i]: d[i] = -1
    elif fam == "MOMENTUM":
        N = p["N"]
        for i in range(N+1, n):
            r = c[i]/c[i-N] - 1
            if r > p["thr"]: d[i] = 1
            elif r < -p["thr"]: d[i] = -1
    return d

def family_grid():
    G = []
    for N in (10,20,30,55):
        for ep in ((20,50),(10,30),(50,200),None):
            for ax in (0,15,22,30):
                G.append(("DONCHIAN", dict(N=N, ema=ep, adxmin=ax)))
    for f,s in ((9,21),(20,50),(50,200),(8,34)): G.append(("EMACROSS", dict(f=f,s=s)))
    for N in (14,20,30):
        for k in (2.0,2.5,3.0): G.append(("BOLL_MR", dict(N=N,k=k)))
    for N in (14,20,30):
        for m in (1.5,2.0,2.5): G.append(("KELTNER", dict(N=N,m=m)))
    for band in ((30,70),(20,80),(40,60)):
        for mode in ("revert","trend"): G.append(("RSI", dict(n=14,band=band,mode=mode)))
    for pp in ((12,26,9),(5,35,5),(8,17,9)): G.append(("MACD", dict(p=pp)))
    for band in ((20,80),(10,90)): G.append(("STOCH", dict(kd=(14,3),band=band)))
    for N in (10,20,55): G.append(("VOLBREAK", dict(N=N)))
    for N in (30,120):
        for thr in (0.05,0.10): G.append(("MOMENTUM", dict(N=N,thr=thr)))
    return G

# trade-execution grid applied on top of every signal
EXITS = ("trend","fixed","mixed"); ATRKS = (1.0,1.5,2.0,2.5); RS = (1.5,2.0,2.5,3.0); SIDES = (0,1,-1)

BASELINE_TAG = "DONCHIAN|N20|ema(20,50)|adx22 | mixed ak1.5 R2.5 sd0"  # deployed

# ---------- per-worker cache ----------
_CACHE = {}
def get_coin(coin, tf):
    key = (coin, tf)
    if key in _CACHE: return _CACHE[key]
    try:
        if tf == "1d":
            d = np.load(DATA / f"{coin}_4h.npz"); f = 6
            o = d["o"][::f][:len(d["o"])//f*1]; # resample below
            O,H,L,C,T = _resample(d,6)
        else:
            d = np.load(DATA / f"{coin}_{tf}.npz")
            O,H,L,C,T = d["o"],d["h"],d["l"],d["c"],d["t"]
    except Exception:
        _CACHE[key] = None; return None
    if C is None or len(C) < 300:      # too short for EMA200 warmup / meaningful IS-OOS
        _CACHE[key] = None; return None
    try:
        ind = precompute(O,H,L,C)   # provides 'av'(ATR14) + 'es'(EMA50) for the shared exit machinery
    except Exception:
        _CACHE[key] = None; return None
    _CACHE[key] = (O,H,L,C,T,ind); return _CACHE[key]

def _resample(d, f):
    o,h,l,c,t = d["o"],d["h"],d["l"],d["c"],d["t"]; n=len(c)//f*f
    o,h,l,c,t = o[:n],h[:n],l[:n],c[:n],t[:n]
    O=o[::f]; T=t[::f]
    H=h.reshape(-1,f).max(1); L=l.reshape(-1,f).min(1); C=c.reshape(-1,f)[:,-1]
    return O,H,L,C,T

# split timestamps per tf from BTC
def split_ts(tf):
    g = get_coin("BTCUSDT", tf); T = g[4]
    t0,t1 = int(T[0]), int(T[-1]); return t0, t0+int((t1-t0)*0.70), t1

def metrics(trades, t0, split, t1):
    if len(trades) < 30: return None
    tr = sorted(trades)
    Ri = np.array([r for (ti,to,r) in tr if ti < split])
    Ro = np.array([r for (ti,to,r) in tr if ti >= split])
    if len(Ri) < 15 or len(Ro) < 15: return None
    oos = [(ti,to,r) for (ti,to,r) in tr if ti >= split]
    pm = portfolio_sim(sorted(oos), risk_frac=0.0075, cap=4)
    # walk-forward 5 expanding folds on OOS-relevant span (use whole span)
    edges = np.linspace(t0, t1+1, 6)
    wf = 0; wfn = 0
    for k in range(1,5):
        seg = [r for (ti,to,r) in tr if edges[k] <= ti < edges[k+1]]
        if len(seg) >= 10:
            wfn += 1; wf += 1 if np.mean(seg) > 0 else 0
    g = Ro[Ro>0].sum(); b = -Ro[Ro<0].sum()
    return dict(n=len(tr), n_oos=len(Ro),
                is_expR=round(float(Ri.mean()),4), oos_expR=round(float(Ro.mean()),4),
                oos_pf=round(float(g/b),3) if b>0 else 99.0,
                oos_total=None if not pm else round(pm["total"],4),
                oos_mdd=None if not pm else round(pm["mdd"],3),
                wf_pos=wf, wf_n=wfn,
                overfit_gap=round(float(Ri.mean()-Ro.mean()),4))

def task(args):
    tf, fam, p = args
    coins = COINS_TF[tf]
    t0, split, t1 = split_ts(tf)
    # accumulate pooled trades per trade-config
    pools = {}
    for coin in coins:
        g = get_coin(coin, tf)
        if g is None: continue
        O,H,L,C,T,ind = g
        dirs = make_dirs(fam, p, O,H,L,C)
        if not dirs.any(): continue
        for ex, ak, R, sd in itertools.product(EXITS, ATRKS, RS, SIDES):
            key = (ex,ak,R,sd)
            tr = trades_coin(O,H,L,C,T, ind, dirs, atr_k=ak, R=R, exit_mode=ex,
                             trail_k=0.0, slip_bps=2, fund_rate=0.0001, side_filter=sd,
                             tf_h=TF_H[tf])
            pools.setdefault(key, []).extend(tr)
    rows = []
    ptag = "|".join(f"{k}{v}" for k,v in p.items())
    for (ex,ak,R,sd), tr in pools.items():
        m = metrics(tr, t0, split, t1)
        if m is None: continue
        m.update(tf=tf, family=fam, sig=ptag, exit=ex, atr_k=ak, R=R, side=sd,
                 tag=f"{fam}|{ptag}|{ex}|ak{ak}|R{R}|sd{sd}")
        rows.append(m)
    return rows

def main():
    quick = os.getenv("METIS_QUICK") == "1"
    tfs = os.getenv("METIS_TFS", "4h,1h,1d,15m,5").split(",")
    fams = family_grid()
    if quick:
        fams = fams[:3]; tfs = ["4h"]
    tasks = [(tf, fam, p) for tf in tfs for (fam,p) in fams]
    trade_cells = len(tasks) * len(EXITS)*len(ATRKS)*len(RS)*len(SIDES)
    open(LOG,"w").close()
    log(f"START exhaustive sweep: {len(tfs)} TFs × {len(fams)} signal-configs = {len(tasks)} tasks")
    log(f"trade grid per signal = {len(EXITS)}×{len(ATRKS)}×{len(RS)}×{len(SIDES)} = {len(EXITS)*len(ATRKS)*len(RS)*len(SIDES)}")
    log(f"=> up to {trade_cells:,} config-cells (pooled across coins). cores={mp.cpu_count()}")
    t_start = time.time()
    results = []
    csv_path = OUT / "all_cells.csv"; wrote_header = False
    with mp.Pool(max(1, mp.cpu_count()-1)) as pool, open(csv_path,"w",newline="") as cf:
        w = None
        done = 0
        for rows in pool.imap_unordered(task, tasks, chunksize=1):
            done += 1
            if rows:
                if w is None:
                    w = csv.DictWriter(cf, fieldnames=list(rows[0].keys())); w.writeheader()
                for r in rows: w.writerow(r)
                results.extend(rows)
            if done % 25 == 0 or done == len(tasks):
                el = time.time()-t_start
                log(f"  {done}/{len(tasks)} tasks ({el:.0f}s, {el/done:.1f}s/task) — {len(results):,} cells kept")
    # rank
    ok = [r for r in results if r["oos_total"] is not None]
    by_oos = sorted(ok, key=lambda r: r["oos_total"], reverse=True)
    robust = [r for r in ok if r["wf_n"]>=4 and r["wf_pos"]>=4 and r["oos_pf"]>1.05
              and r["oos_total"]>0]
    summary = dict(
        tfs=tfs, signal_configs=len(fams), tasks=len(tasks),
        cells_kept=len(results), cells_scored=len(ok),
        elapsed_sec=round(time.time()-t_start,1),
        baseline=BASELINE_TAG,
        top20_by_oos_total=by_oos[:20],
        robust_wf_positive=sorted(robust, key=lambda r:r["oos_total"], reverse=True)[:40],
        n_robust=len(robust),
    )
    (OUT/"summary.json").write_text(json.dumps(summary, indent=1, default=str))
    log(f"DONE. kept {len(results):,} cells, {len(ok):,} scored, {len(robust)} robust(wf>=4/5 & pf>1.05 & oos+).")
    log(f"summary -> {OUT/'summary.json'} ; all cells -> {csv_path}")

if __name__ == "__main__":
    main()
