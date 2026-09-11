"""전수 그리드서치 — "지금 config가 진짜 최적인가?" 적대적 검증.
EMA쌍×Donchian-N×ADX_MIN×ATR_K×숏R×롱청산(slow/fast) = 2400 config.
각 config: 전체 CAGR/MDD + OOS(70/30) + walk-forward(5fold 양수개수) + 최근(2024+) expR.
멀티프로세싱(전 코어). 과적합 경계 = OOS·WF·최근 동시 통과만 신뢰."""
from __future__ import annotations
import itertools, multiprocessing as mp
from datetime import datetime, timezone
import numpy as np
from edge_scan import precompute, COINS, DATA
from backtest_trend_15m import ema, adx, atr, FEE_RT
from validate_breakout import portfolio_sim

COST = FEE_RT + 2*2/10000.0; FUND=0.0001; TF_H=4; WARM=210
EMA_PAIRS=[(10,30),(20,50),(20,100),(50,200)]
DON_NS=[10,15,20,30,40]; ADX_MINS=[15,18,22,25,30]; ATR_KS=[1.0,1.5,2.0,2.5]
SHORT_RS=[2.0,2.5,3.0]; LONG_EXITS=["slow","fast"]
REC=datetime(2024,1,1,tzinfo=timezone.utc).timestamp()*1000

_C={}  # coin -> raw + cached indicators
def _roll(x,n,fn):
    out=np.full_like(x, np.nan)
    for i in range(n,len(x)): out[i]=fn(x[i-n:i])
    return out
def init():
    for coin in COINS:
        f=DATA/f"{coin}_4h.npz"
        if not f.exists(): continue
        d=np.load(f); o,h,l,c,t=d["o"],d["h"],d["l"],d["c"],d["t"]
        emas={p:(ema(c,p[0]),ema(c,p[1])) for p in EMA_PAIRS}
        don={N:(np.roll(_roll(h,N,np.max),1),np.roll(_roll(l,N,np.min),1)) for N in DON_NS}
        _C[coin]=dict(o=o,h=h,l=l,c=c,t=t,ax=adx(h,l,c,14),av=atr(h,l,c,14),emas=emas,don=don)

def sim_coin(C, ef,es, ax,av, pdhi,pdlo, exit_ema, AM, AK, R):
    h,l,c,t=C["h"],C["l"],C["c"],C["t"]; n=len(c); out=[]; i=WARM
    while i<n-1:
        lng = c[i]>pdhi[i] and ef[i]>es[i] and ax[i]>AM
        sht = c[i]<pdlo[i] and ef[i]<es[i] and ax[i]>AM
        if not (lng or sht): i+=1; continue
        di=1 if lng else -1; entry=c[i]; rk=AK*av[i]
        if rk<=0 or not np.isfinite(rk): i+=1; continue
        sl=entry-di*rk; tp=entry+di*R*rk; j=i+1; expx=None
        while j<n:
            if di==1:
                if l[j]<=sl: expx=sl;break
                if c[j]<exit_ema[j]: expx=c[j];break
            else:
                if h[j]>=sl: expx=sl;break
                if l[j]<=tp: expx=tp;break
            j+=1
        if expx is None: break
        net=di*(expx-entry)/entry - COST - (j-i)*(TF_H/8.0)*FUND
        out.append((int(t[i]),int(t[j]),net/(rk/entry))); i=j+1
    return out

def run(cfg):
    pair,N,AM,AK,R,lex=cfg; tr=[]
    for coin,C in _C.items():
        ef,es=C["emas"][pair]; pdhi,pdlo=C["don"][N]
        exit_ema = es if lex=="slow" else ef
        tr+=sim_coin(C,ef,es,C["ax"],C["av"],pdhi,pdlo,exit_ema,AM,AK,R)
    if len(tr)<30: return None
    R_=np.array([x[2] for x in tr]); pm=portfolio_sim(tr,risk_frac=0.0075,cap=4)
    if not pm: return None
    s=sorted(tr); m=int(len(s)*0.7)
    oos=np.mean([x[2] for x in s[m:]])
    e=np.linspace(s[0][0],s[-1][0]+1,6)
    wf=sum(1 for k in range(5) for seg in [[x[2] for x in s if e[k]<=x[0]<e[k+1]]] if len(seg)>=8 and np.mean(seg)>0)
    rec=[x[2] for x in tr if x[0]>=REC]; recE=np.mean(rec) if len(rec)>=20 else -9
    return dict(cfg=cfg,n=len(tr),expR=R_.mean(),cagr=pm["cagr"],mdd=pm["mdd"],
                oos=oos,wf=wf,rec=recE)

def main():
    cfgs=list(itertools.product(EMA_PAIRS,DON_NS,ADX_MINS,ATR_KS,SHORT_RS,LONG_EXITS))
    print(f"configs={len(cfgs)}, cores={mp.cpu_count()}")
    with mp.Pool(mp.cpu_count(), initializer=init) as p:
        res=[r for r in p.map(run,cfgs,chunksize=8) if r]
    cur=next((r for r in res if r["cfg"]==((20,50),20,22,1.5,2.5,"slow")),None)
    def fmt(r):
        p,N,AM,AK,R,lex=r["cfg"]
        return (f"ema{p[0]}/{p[1]} N{N} ADX{AM} k{AK} R{R} {lex:>4} | n{r['n']:>4} "
                f"CAGR{r['cagr']*100:>+5.0f}% MDD{r['mdd']*100:>4.0f}% OOS{r['oos']:>+.2f} WF{r['wf']} 최근{r['rec']:>+.2f}")
    print("\n=== 현행 config ==="); print("  "+fmt(cur) if cur else "  (없음)")
    robust=[r for r in res if r["oos"]>0 and r["wf"]>=4 and r["rec"]>0 and r["cagr"]>0]
    print(f"\n=== robust 통과(OOS>0 & WF>=4 & 최근>0 & CAGR>0): {len(robust)}/{len(res)} ===")
    print("--- 최근expR 상위 12 ---")
    for r in sorted(robust,key=lambda x:-x["rec"])[:12]: print("  "+fmt(r))
    print("--- 전체CAGR 상위 8 (과적합 의심: 최근/OOS 낮으면 함정) ---")
    for r in sorted(res,key=lambda x:-x["cagr"])[:8]: print("  "+fmt(r))
    # 현행이 robust 안에서 어디?
    if cur:
        better=[r for r in robust if r["rec"]>cur["rec"] and r["cagr"]>cur["cagr"] and r["mdd"]<=cur["mdd"]+0.03]
        print(f"\n=== 현행을 모든면(최근expR↑ & CAGR↑ & MDD≈↓)서 지배하는 config: {len(better)}개 ===")
        for r in sorted(better,key=lambda x:-x["rec"])[:10]: print("  "+fmt(r))
if __name__=="__main__":
    main()
