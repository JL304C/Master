import sim, pandas as pd
pd.set_option("display.width",250); pd.set_option("display.float_format",lambda x:f"{x:,.3f}")
allsnaps=dict(sim.snaps)
first=min(d for d,q in allsnaps.items() if sim.pick_exps(q,d)[1] is not None)
sim.snaps.clear(); sim.snaps.update({d:q for d,q in allsnaps.items() if d>=first})
rows=[]
for name,kw in [("1 wheel as written",dict(after="wheel")),("2 wheel, calls never below cost",dict(after="floor")),
                ("3 wheel + no-below-cost + stop -25%",dict(after="stop",stop=0.25)),("4 hold shares, no calls, sell at breakeven",dict(after="hold")),
                ("5 no wheel: take loss in cash at expiry",dict(after="nowheel")),("6 roll put at same strike",dict(after="roll"))]:
    eq,one=sim.run("down","A",**kw)
    yrs=(eq.index[-1]-eq.index[0]).days/365.25
    p=one[one.cp=="P"]
    by=eq.A.groupby(eq.index.year).last()
    rows.append(dict(rule=name, final_x=eq.A.iloc[-1], cagr=eq.A.iloc[-1]**(1/yrs)-1, max_dd=(eq.A/eq.A.cummax()-1).min(),
        puts=len(p), itm=int(p.assigned.sum()), weeks_in_shares=int((eq.stock>0).sum()), weeks=len(eq),
        **{f"y{y}":v for y,v in by.items()}))
print(pd.DataFrame(rows).set_index("rule").to_string())
print(sim.bh(eq))
