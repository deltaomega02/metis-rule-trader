"""그리드 우승 후보(ema50/200)가 진짜냐 과적합/노이즈냐 — 적대적 정밀검증.
현행 vs 후보들: 연도별 expR·거래빈도·부트스트랩 CI(차이가 노이즈인가, 게이트 전례처럼)."""
from __future__ import annotations
from datetime import datetime, timezone
import numpy as np
import grid_sweep as G

G.init()
np.random.seed(7)
CONFIGS=[
 ("현행 ema20/50",       ((20,50),20,22,1.5,2.5,"slow")),
 ("A:50/200 N30 ADX15",  ((50,200),30,15,1.5,2.5,"slow")),
 ("B:50/200 N40 ADX22 k2",((50,200),40,22,2.0,2.5,"slow")),
 ("C:50/200 N40 ADX18 fast",((50,200),40,18,1.0,3.0,"fast")),
]
def trades(cfg):
    pair,N,AM,AK,R,lex=cfg; tr=[]
    for coin,C in G._C.items():
        ef,es=C["emas"][pair]; pdhi,pdlo=C["don"][N]
        ex=es if lex=="slow" else ef
        tr+=G.sim_coin(C,ef,es,C["ax"],C["av"],pdhi,pdlo,ex,AM,AK,R)
    return tr
def emdd(Rs,risk=0.0075):
    eq=peak=1.0;mdd=0
    for r in Rs:
        eq*=(1+risk*r);peak=max(peak,eq);mdd=max(mdd,(peak-eq)/peak)
    return eq-1,mdd
def yr(ts): return datetime.fromtimestamp(ts/1000,tz=timezone.utc).year
D={nm:trades(c) for nm,c in CONFIGS}
yrs=sorted({yr(x[0]) for x in D["현행 ema20/50"]})
print("=== 연도별 expR (거래수) — 후보 우위가 특정 시기 몰렸나? ===")
print(f"  {'year':>6} | "+" | ".join(f"{nm[:14]:>14}" for nm,_ in CONFIGS))
for y in yrs:
    row=[]
    for nm,_ in CONFIGS:
        seg=[x[2] for x in D[nm] if yr(x[0])==y]
        row.append(f"{np.mean(seg):>+5.2f}({len(seg):>3})" if len(seg)>=5 else "   -    ")
    print(f"  {y:>6} | "+" | ".join(f"{c:>14}" for c in row))
print("\n=== 전체 + 거래빈도 + 부트스트랩 CI (3000회) ===")
cur=D["현행 ema20/50"]; Rcur=np.array([x[2] for x in cur])
days=(max(x[1] for x in cur)-min(x[0] for x in cur))/1000/86400; yrsN=days/365
def boot(Rs):
    cg=[]
    for _ in range(3000):
        s=Rs[np.random.randint(0,len(Rs),len(Rs))]; t,_=emdd(s)
        cg.append(((1+t)**(1/yrsN)-1) if 1+t>0 else -1)
    return np.array(cg)*100
bc=boot(Rcur)
for nm,_ in CONFIGS:
    tr=D[nm]; Rs=np.array([x[2] for x in tr]); tot,mdd=emdd(Rs)
    cagr=((1+tot)**(1/yrsN)-1)*100
    npy=len(tr)/yrsN
    bb=boot(Rs)
    # paired diff vs current
    if nm!="현행 ema20/50":
        d=[]
        for _ in range(3000):
            sa=Rs[np.random.randint(0,len(Rs),len(Rs))];sc=Rcur[np.random.randint(0,len(Rcur),len(Rcur))]
            ta,_=emdd(sa);tc,_=emdd(sc)
            d.append((((1+ta)**(1/yrsN)-1)-((1+tc)**(1/yrsN)-1)))
        pbet=np.mean(np.array(d)>0)*100
        pp=f"P(현행보다↑)={pbet:.0f}%"
    else: pp=""
    print(f"  {nm:>22} | n{len(tr):>4} {npy:>4.0f}/년 CAGR{cagr:>+5.0f}% MDD{mdd*100:>4.0f}% "
          f"| CI[{np.percentile(bb,5):>+4.0f}~{np.percentile(bb,95):>+4.0f}] {pp}")
