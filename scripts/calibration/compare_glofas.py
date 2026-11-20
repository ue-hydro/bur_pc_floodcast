import json, sys, urllib.request
import numpy as np
S=sys.argv[1]; tags=sys.argv[2:]
url="https://flood-api.open-meteo.com/v1/flood?latitude=38.325&longitude=-8.525&daily=river_discharge&start_date=2025-10-01&end_date=2026-11-13"
g=json.load(urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"floodcast-test"}),timeout=120))["daily"]
gd={t:v for t,v in zip(g["time"],g["river_discharge"])}
def daily(tag):
    z=np.load(f"{S}/year_{tag}.npz"); t=z["t"].astype("datetime64[s]"); q=z["q"]
    days=t.astype("datetime64[D]"); out={}
    for dday in np.unique(days): out[str(dday)]=float(q[days==dday].mean())
    return out
def kge(s,o):
    r=np.corrcoef(s,o)[0,1]; return 1-np.sqrt((r-1)**2+(s.std()/o.std()-1)**2+(s.mean()/o.mean()-1)**2), r
sims={t:daily(t) for t in tags}
common=sorted(set(gd)&set.intersection(*[set(v) for v in sims.values()]))
common=[c for c in common if c>="2025-12-01"]  # skip 2 months cold-start
o=np.array([gd[c] for c in common])
print(f"{len(common)} days {common[0]}..{common[-1]}  GloFAS mean {o.mean():.1f}")
for t,s in sims.items():
    s=np.array([s[c] for c in common]); k,r=kge(s,o)
    lk,lr=kge(np.log(s+0.1),np.log(o+0.1))
    print(f"{t:12s} mean {s.mean():6.1f}  bias {100*(s.mean()/o.mean()-1):+5.0f}%  KGE {k:.2f} r {r:.2f}  logKGE {lk:.2f}  max {s.max():.0f} (GloFAS {o.max():.0f})")
print("month   GloFAS " + " ".join(f"{t:>10s}" for t in tags))
for m in sorted({c[:7] for c in common}):
    idx=[i for i,c in enumerate(common) if c[:7]==m]
    print(m, f"{o[idx].mean():7.1f} "+" ".join(f"{np.mean([sims[t][common[i]] for i in idx]):10.1f}" for t in tags))
print("last days:"); 
for c in common[-8:]: print(c, f"{gd[c]:7.1f} "+" ".join(f"{sims[t][c]:10.1f}" for t in tags))
