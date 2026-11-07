"""
Did the American Airlines - US Airways Merger Raise Fares?
A Difference-in-Differences Retrospective
==========================================================

American Airlines (AA) and US Airways (US) merged on December 9, 2013, after
the U.S. Department of Justice sued to block the deal and settled for slot and
gate divestitures. This script estimates the merger's effect on average fares
on routes where the two airlines competed head-to-head before the merger,
compared with routes where neither airline was a major competitor.

Data: U.S. DOT Consumer Airfare Report, Table 6 (contiguous-state city-pair
markets averaging at least 10 passengers per day), 2010-2017, from
data.transportation.gov. For each route and quarter the table reports the
average fare, passengers, distance, and the largest carrier and the
lowest-fare carrier with their market shares and fares.

Design
- Panel: routes observed in every quarter from 2011Q1 to 2016Q4 (balanced).
- Pre-period 2011Q1-2013Q3; merger quarter 2013Q4 excluded from the main
  estimate; post-period 2014Q1-2016Q4.
- Treated ("overlap") routes: AA and US were the largest and the lowest-fare
  carrier (in either order) in at least 2 of the 11 pre-merger quarters.
- Control routes: neither AA nor US was the largest or lowest-fare carrier in
  any pre-merger quarter.
- Model: log(fare) = b * Overlap x Post + route FE + quarter FE + e,
  standard errors clustered by route. exp(b) - 1 is the % fare effect.
- Checks: event study (parallel pre-trends), placebo merger date, alternative
  treatment and control definitions, distance-band x quarter FE, and a
  passengers outcome.

Run:  python3 merger_analysis.py      (downloads any missing data, writes output/)
"""
import glob
import os
import subprocess
import urllib.error
import urllib.request

import numpy as np
import pandas as pd
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)

YEARS = range(2010, 2018)
COLS = ("year,quarter,citymarketid_1,citymarketid_2,city1,city2,nsmiles,passengers,"
        "fare,carrier_lg,large_ms,fare_lg,carrier_low,lf_ms,fare_low")
START, MERGER_Q, END = 20111, 20134, 20164
PRE_END, POST_START = 20133, 20141
A, B = "AA", "US"


# ---------------------------------------------------------------- data
def download(year):
    path = os.path.join(DATA, f"fares_{year}.csv")
    if os.path.exists(path):
        return
    os.makedirs(DATA, exist_ok=True)
    url = ("https://data.transportation.gov/resource/yj5y-b2ir.csv?"
           f"$where=year={year}&$limit=50000&$select={COLS}")
    try:
        urllib.request.urlretrieve(url, path)
    except urllib.error.URLError:                # e.g. python.org builds without root certificates
        subprocess.run(["curl", "-sSfL", "-o", path, url], check=True)


for y in YEARS:
    download(y)
raw = pd.concat(pd.read_csv(f) for f in sorted(glob.glob(os.path.join(DATA, "fares_*.csv"))))
raw["t"] = raw["year"] * 10 + raw["quarter"]
raw["route"] = raw["citymarketid_1"].astype(str) + "-" + raw["citymarketid_2"].astype(str)
assert not raw.duplicated(["route", "t"]).any()

quarters = sorted(t for t in raw["t"].unique() if START <= t <= END)
df = raw[raw["t"].between(START, END)].copy()
counts = df.groupby("route")["t"].nunique()
df = df[df["route"].isin(counts[counts == len(quarters)].index)].copy()   # balanced panel

# ---------------------------------------------------------------- treatment groups
pre = df[df["t"] <= PRE_END].copy()
pre["has_a"] = (pre["carrier_lg"] == A) | (pre["carrier_low"] == A)
pre["has_b"] = (pre["carrier_lg"] == B) | (pre["carrier_low"] == B)
pre["head_to_head"] = pre["has_a"] & pre["has_b"]
g = pre.groupby("route").agg(h2h=("head_to_head", "sum"), a=("has_a", "sum"), b=("has_b", "sum"),
                             miles=("nsmiles", "mean"), pax=("passengers", "mean"), fare=("fare", "mean"))
g["group"] = np.select(
    [g["h2h"] >= 2, (g["a"] == 0) & (g["b"] == 0), (g["h2h"] == 0)],
    ["overlap", "control", "one_carrier"], default="weak_overlap")
df = df.join(g[["group", "h2h", "a", "b"]], on="route")
df["post"] = (df["t"] >= POST_START).astype(float)
df["lfare"] = np.log(df["fare"])
df["lpax"] = np.log(df["passengers"])
df["band"] = pd.cut(df["nsmiles"], [0, 500, 1000, 1500, 2000, 9999], labels=False)


# ---------------------------------------------------------------- estimator
def demean_by(arr, codes, n_groups):
    sums = np.zeros((n_groups,) + arr.shape[1:])
    np.add.at(sums, codes, arr)
    cnt = np.bincount(codes, minlength=n_groups).astype(float)
    return arr - (sums / cnt.reshape((-1,) + (1,) * (arr.ndim - 1)))[codes]


def twfe(data, y, xvars, time_fe="t"):
    """OLS of y on xvars with route fixed effects (within transformation) and
    time fixed effects (dummies), standard errors clustered by route.
    time_fe can be 't' or 'band_t' (distance band x quarter)."""
    d = data.reset_index(drop=True)
    r_codes, r_idx = pd.factorize(d["route"])
    fe_key = d["t"].astype(str) if time_fe == "t" else d["band"].astype(str) + "_" + d["t"].astype(str)
    dummies = pd.get_dummies(fe_key, drop_first=True, dtype=float).values
    X = np.column_stack([d[xvars].values.astype(float), dummies])
    Y = d[y].values.astype(float)
    Xd, Yd = demean_by(X, r_codes, len(r_idx)), demean_by(Y, r_codes, len(r_idx))
    keep = np.abs(Xd).sum(axis=0) > 1e-10               # drop columns absorbed by route FE
    Xd = Xd[:, keep]
    XtX_inv = np.linalg.pinv(Xd.T @ Xd)
    beta = XtX_inv @ Xd.T @ Yd
    u = Yd - Xd @ beta
    G, N = len(r_idx), len(Yd)
    K = Xd.shape[1] + G                                  # parameters incl. route FE
    scores = np.zeros((G, Xd.shape[1]))
    np.add.at(scores, r_codes, Xd * u[:, None])
    meat = scores.T @ scores
    c = G / (G - 1) * (N - 1) / (N - K)
    V = c * XtX_inv @ meat @ XtX_inv
    k = len(xvars)
    return dict(beta=beta[:k], se=np.sqrt(np.diag(V)[:k]), V=V[:k, :k], N=N, G=G,
                resid=u, full_beta=beta)


def effect_row(label, res, i=0, n_t=None, n_c=None):
    b, se = res["beta"][i], res["se"][i]
    lo, hi = b - 1.96 * se, b + 1.96 * se
    p = 2 * (1 - stats.t.cdf(abs(b / se), res["G"] - 1))
    return dict(Specification=label, Coef=b, SE=se, Effect_pct=np.expm1(b), CI_low_pct=np.expm1(lo),
                CI_high_pct=np.expm1(hi), p_value=p, Treated_routes=n_t, Control_routes=n_c,
                Observations=res["N"])


def sample(groups_t, groups_c, drop_merger_q=True, data=None):
    d = (df if data is None else data)
    d = d[d["group"].isin(groups_t + groups_c)].copy()
    if drop_merger_q:
        d = d[d["t"] != MERGER_Q]
    d["treat"] = d["group"].isin(groups_t).astype(float)
    d["did"] = d["treat"] * d["post"]
    return d


main = sample(["overlap"], ["control"])
n_t = main.loc[main.treat == 1, "route"].nunique()
n_c = main.loc[main.treat == 0, "route"].nunique()
res_main = twfe(main, "lfare", ["did"])

rows = [effect_row("Main: overlap vs. control, route + quarter FE", res_main, n_t=n_t, n_c=n_c)]

res_band = twfe(main, "lfare", ["did"], time_fe="band_t")
rows.append(effect_row("Distance band x quarter FE", res_band, n_t=n_t, n_c=n_c))

for th, lab in [(1, "Overlap in >=1 pre quarter"), (3, "Overlap in >=3 pre quarters")]:
    d = df.copy()
    d["group"] = np.where(d["h2h"] >= th, "overlap", np.where((d["a"] == 0) & (d["b"] == 0), "control", "other"))
    s = sample(["overlap"], ["control"], data=d)
    rows.append(effect_row(lab, twfe(s, "lfare", ["did"]),
                           n_t=s.loc[s.treat == 1, "route"].nunique(), n_c=s.loc[s.treat == 0, "route"].nunique()))

s = sample(["overlap"], ["one_carrier"])
rows.append(effect_row("Control = routes with only one of AA/US", twfe(s, "lfare", ["did"]),
                       n_t=s.loc[s.treat == 1, "route"].nunique(), n_c=s.loc[s.treat == 0, "route"].nunique()))

s = sample(["overlap"], ["control"], drop_merger_q=False)
rows.append(effect_row("Including merger quarter 2013Q4 as post", twfe(s, "lfare", ["did"]), n_t=n_t, n_c=n_c))

# placebo: pretend the merger happened in 2012Q3, using pre-merger data only
plac = main[main["t"] <= PRE_END].copy()
plac["post"] = (plac["t"] >= 20123).astype(float)
plac["did"] = plac["treat"] * plac["post"]
res_plac = twfe(plac, "lfare", ["did"])
rows.append(effect_row("Placebo: fake merger in 2012Q3 (pre-period only)", res_plac, n_t=n_t, n_c=n_c))

res_pax = twfe(main, "lpax", ["did"])
rows.append(effect_row("Outcome: log passengers", res_pax, n_t=n_t, n_c=n_c))

# effect by distance
for lab, cond in [("Routes under 1,000 miles", main["nsmiles"] < 1000), ("Routes 1,000+ miles", main["nsmiles"] >= 1000)]:
    s = main[cond]
    rows.append(effect_row(lab, twfe(s, "lfare", ["did"]),
                           n_t=s.loc[s.treat == 1, "route"].nunique(), n_c=s.loc[s.treat == 0, "route"].nunique()))
# effect by phase: 2014 = integration year; 2015-16 = single operating certificate (Apr 2015) onward
main["tt"] = main["t"].rank(method="dense")
main["phase14"] = main["treat"] * main["t"].between(20141, 20144)
main["phase1516"] = main["treat"] * (main["t"] >= 20151)
main["trend_t"] = main["treat"] * main["tt"]
res_ph = twfe(main, "lfare", ["phase14", "phase1516"])
rows.append(effect_row("Phase: 2014 (integration year)", res_ph, 0, n_t, n_c))
rows.append(effect_row("Phase: 2015-2016 (after single operating certificate)", res_ph, 1, n_t, n_c))

# pre-existing differential trend, estimated on pre-merger quarters only
pre_main = main[main["t"] <= PRE_END]
res_pt = twfe(pre_main, "lfare", ["trend_t"])
pretrend_b, pretrend_se = res_pt["beta"][0], res_pt["se"][0]
pretrend_p = 2 * (1 - stats.t.cdf(abs(pretrend_b / pretrend_se), res_pt["G"] - 1))

# allow overlap routes their own linear trend
res_tr = twfe(main, "lfare", ["phase14", "phase1516", "trend_t"])
rows.append(effect_row("Trend-adjusted: 2014", res_tr, 0, n_t, n_c))
rows.append(effect_row("Trend-adjusted: 2015-2016", res_tr, 1, n_t, n_c))

# matched controls: 5 nearest control routes per overlap route on log distance, traffic and pre-merger fare
gz = g.assign(lmi=np.log(g["miles"]), lpax=np.log(g["pax"]), lf=np.log(g["fare"]))
Zc = ["lmi", "lpax", "lf"]
zt, zc = gz.loc[gz.group == "overlap", Zc], gz.loc[gz.group == "control", Zc]
mu, sd = pd.concat([zt, zc]).mean(), pd.concat([zt, zc]).std()
zt, zc = (zt - mu) / sd, (zc - mu) / sd
matched = set()
for _, row in zt.iterrows():
    matched.update(((zc - row) ** 2).sum(axis=1).nsmallest(5).index)
mt = main[(main.treat == 1) | main["route"].isin(matched)].copy()
res_m = twfe(mt, "lfare", ["did"])
rows.append(effect_row("Matched controls (5 nearest neighbors)", res_m, 0, n_t, len(matched)))
res_m2 = twfe(mt, "lfare", ["phase14", "phase1516"])
rows.append(effect_row("Matched controls: 2015-2016", res_m2, 1, n_t, len(matched)))
match_bal = gz.loc[list(zt.index) + list(matched)].groupby("group")[["miles", "pax", "fare"]].mean()

results = pd.DataFrame(rows)

# ---------------------------------------------------------------- event study
BASE = PRE_END
ev = sample(["overlap"], ["control"], drop_merger_q=False)
ev_q = [q for q in quarters if q != BASE]
for q in ev_q:
    ev[f"ev_{q}"] = ev["treat"] * (ev["t"] == q)
res_ev = twfe(ev, "lfare", [f"ev_{q}" for q in ev_q])
# express every quarter relative to the average pre-merger quarter (a single base quarter is noisy);
# standard errors from the exact linear combination of the coefficient covariance matrix
n_e = len(ev_q)
pre_mask = np.array([q < BASE for q in ev_q])
n_pre_all = pre_mask.sum() + 1                                   # + the omitted base quarter (coef 0)
coef_c, se_c = [], []
for q in quarters:
    a = np.zeros(n_e)
    if q != BASE:
        a[ev_q.index(q)] += 1
    a[pre_mask] -= 1 / n_pre_all
    coef_c.append(a @ res_ev["beta"])
    se_c.append(np.sqrt(a @ res_ev["V"] @ a))
event = pd.DataFrame({"quarter": quarters, "coef": coef_c, "se": se_c})
event["effect_pct"] = np.expm1(event["coef"])
event["ci_low"] = np.expm1(event["coef"] - 1.96 * event["se"])
event["ci_high"] = np.expm1(event["coef"] + 1.96 * event["se"])
pre_idx = [i for i, q in enumerate(ev_q) if q < BASE]
bp, Vp = res_ev["beta"][pre_idx], res_ev["V"][np.ix_(pre_idx, pre_idx)]
wald = float(bp @ np.linalg.pinv(Vp) @ bp)
F_pre = wald / len(pre_idx)
p_pre = 1 - stats.f.cdf(F_pre, len(pre_idx), res_ev["G"] - 1)

# ---------------------------------------------------------------- descriptives
desc = main.groupby(["treat", "post"]).agg(mean_fare=("fare", "mean"), mean_lfare=("lfare", "mean"),
                                           mean_pax=("passengers", "mean")).reset_index()
did_2x2 = ((desc.query("treat==1 and post==1").mean_lfare.iloc[0] - desc.query("treat==1 and post==0").mean_lfare.iloc[0])
           - (desc.query("treat==0 and post==1").mean_lfare.iloc[0] - desc.query("treat==0 and post==0").mean_lfare.iloc[0]))
pre_bal = g[g["group"].isin(["overlap", "control"])].groupby("group")[["miles", "pax", "fare"]].mean()
trend = main.groupby(["t", "treat"])["fare"].mean().unstack()
trend.columns = ["control_avg_fare", "overlap_avg_fare"]
top_routes = (main[main.treat == 1].groupby(["city1", "city2"])
              .apply(lambda x: pd.Series({"pre_fare": x.loc[x.post == 0, "fare"].mean(),
                                          "post_fare": x.loc[x.post == 1, "fare"].mean(),
                                          "passengers": x["passengers"].mean()}), include_groups=False)
              .sort_values("passengers", ascending=False).head(10))
top_routes["change_pct"] = top_routes["post_fare"] / top_routes["pre_fare"] - 1
avg_post_pax = main.loc[(main.treat == 1) & (main.post == 1), "passengers"].mean()
avg_pre_fare_t = desc.query("treat==1 and post==0").mean_fare.iloc[0]
tr_row = results.set_index("Specification").loc["Trend-adjusted: 2015-2016"]
annual_cost = avg_pre_fare_t * tr_row["Effect_pct"] * avg_post_pax * 365 * n_t

# ---------------------------------------------------------------- validation checks
checks = []


def check(name, ok, detail):
    checks.append(dict(Check=name, Result="PASS" if ok else "FAIL", Detail=detail))


check("No duplicate route-quarters", not raw.duplicated(["route", "t"]).any(), f"{len(raw):,} rows")
check("Balanced panel (every route in every quarter)", (df.groupby("route")["t"].nunique() == len(quarters)).all(),
      f"{df['route'].nunique():,} routes x {len(quarters)} quarters")
check("Treated and control groups are disjoint",
      not set(main.loc[main.treat == 1, "route"]) & set(main.loc[main.treat == 0, "route"]), f"{n_t} vs {n_c} routes")
check("No missing or non-positive fares", (main["fare"] > 0).all() and main["lfare"].notna().all(), "all fares > 0")
check("TWFE estimate equals 2x2 difference-in-differences of means", abs(did_2x2 - res_main["beta"][0]) < 1e-10,
      f"{res_main['beta'][0]:.6f} vs {did_2x2:.6f}")
# brute-force LSDV with explicit route dummies on a subsample must match the within estimator
rng = np.random.default_rng(0)
sub_routes = np.concatenate([rng.choice(main.loc[main.treat == 1, "route"].unique(), 20, replace=False),
                             rng.choice(main.loc[main.treat == 0, "route"].unique(), 80, replace=False)])
sub = main[main["route"].isin(sub_routes)].reset_index(drop=True)
r_sub = twfe(sub, "lfare", ["did"])
Xl = np.column_stack([sub["did"].values, pd.get_dummies(sub["route"], dtype=float).values,
                      pd.get_dummies(sub["t"], drop_first=True, dtype=float).values])
bl, *_ = np.linalg.lstsq(Xl, sub["lfare"].values, rcond=None)
ul = sub["lfare"].values - Xl @ bl
check("Within estimator matches brute-force dummy-variable OLS (100-route subsample)",
      abs(bl[0] - r_sub["beta"][0]) < 1e-8, f"{r_sub['beta'][0]:.8f} vs {bl[0]:.8f}")
check("Residuals match between within and dummy-variable OLS", np.allclose(ul, r_sub["resid"], atol=1e-8),
      f"max diff {np.max(np.abs(ul - r_sub['resid'])):.1e}")
XtXi = np.linalg.pinv(Xl.T @ Xl)
cl = pd.factorize(sub["route"])[0]
sc = np.zeros((cl.max() + 1, Xl.shape[1]))
np.add.at(sc, cl, Xl * ul[:, None])
Gs, Ns = cl.max() + 1, len(ul)
Vl = Gs / (Gs - 1) * (Ns - 1) / (Ns - Xl.shape[1]) * XtXi @ (sc.T @ sc) @ XtXi
check("Clustered standard error matches dummy-variable computation", abs(np.sqrt(Vl[0, 0]) - r_sub["se"][0]) < 1e-6,
      f"{r_sub['se'][0]:.6f} vs {np.sqrt(Vl[0, 0]):.6f}")
check("Merging carrier US disappears from data after integration",
      raw.loc[raw["t"] >= 20153, ["carrier_lg", "carrier_low"]].eq("US").sum().sum() == 0,
      "US Airways code absent from 2015Q3 on")
check("Event-study pre-merger effects average to zero", abs(event.loc[event.quarter <= PRE_END, "coef"].mean()) < 1e-12,
      "re-centered on pre-period mean")
check("Matched controls are balanced on distance, traffic and fare (within 10%)",
      bool((np.abs(match_bal.loc["control"] / match_bal.loc["overlap"] - 1) < 0.10).all()),
      ", ".join(f"{c} {match_bal.loc['control', c] / match_bal.loc['overlap', c] - 1:+.1%}" for c in match_bal.columns))
chk = pd.DataFrame(checks)

# ---------------------------------------------------------------- outputs
results.to_csv(os.path.join(OUT, "regression_results.csv"), index=False)
event.to_csv(os.path.join(OUT, "event_study.csv"), index=False)
trend.to_csv(os.path.join(OUT, "average_fares_by_quarter.csv"))
top_routes.to_csv(os.path.join(OUT, "largest_overlap_routes.csv"))
chk.to_csv(os.path.join(OUT, "validation_checks.csv"), index=False)


def qlabel(q):
    return f"{q // 10}Q{q % 10}"


# event-study chart (SVG, no plotting library needed)
W, H, L, R, T, Bm = 760, 380, 70, 20, 30, 60
ys = np.concatenate([event.ci_low, event.ci_high]) * 100
ymin, ymax = np.floor(min(ys.min(), -2) / 2) * 2, np.ceil(max(ys.max(), 2) / 2) * 2
xpos = lambda i: L + i * (W - L - R) / (len(event) - 1)
ypos = lambda v: T + (ymax - v) / (ymax - ymin) * (H - T - Bm)
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Arial" font-size="11">',
       f'<rect width="{W}" height="{H}" fill="white"/>',
       f'<text x="{W/2}" y="18" text-anchor="middle" font-size="13" font-weight="bold">Fare gap: AA-US overlap routes vs. control routes (% vs. pre-merger average)</text>']
for v in np.arange(ymin, ymax + 0.1, 2):
    svg.append(f'<line x1="{L}" x2="{W-R}" y1="{ypos(v):.1f}" y2="{ypos(v):.1f}" stroke="#e5e5e5"/>')
    svg.append(f'<text x="{L-8}" y="{ypos(v)+4:.1f}" text-anchor="end">{v:+.0f}%</text>')
svg.append(f'<line x1="{L}" x2="{W-R}" y1="{ypos(0):.1f}" y2="{ypos(0):.1f}" stroke="#555"/>')
mi = list(event.quarter).index(MERGER_Q)
svg.append(f'<line x1="{xpos(mi):.1f}" x2="{xpos(mi):.1f}" y1="{T}" y2="{H-Bm}" stroke="#c0392b" stroke-dasharray="4,3"/>')
svg.append(f'<text x="{xpos(mi)+4:.1f}" y="{T+12}" fill="#c0392b">Merger closes (Dec 2013)</text>')
for i, r in event.iterrows():
    x = xpos(i)
    svg.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ypos(r.ci_low*100):.1f}" y2="{ypos(r.ci_high*100):.1f}" stroke="#1f5fa8"/>')
    svg.append(f'<circle cx="{x:.1f}" cy="{ypos(r.effect_pct*100):.1f}" r="3.5" fill="#1f5fa8"/>')
    if i % 2 == 0:
        svg.append(f'<text x="{x:.1f}" y="{H-Bm+16}" text-anchor="middle">{qlabel(int(r.quarter))}</text>')
svg.append(f'<text x="{W/2}" y="{H-12}" text-anchor="middle" fill="#555">Points: difference-in-differences coefficient by quarter; bars: 95% confidence interval (clustered by route)</text>')
svg.append("</svg>")
open(os.path.join(OUT, "event_study.svg"), "w").write("\n".join(svg))

m = lambda x: f"${x:,.0f}"
pc = lambda x: f"{x:+.1%}"
r0 = results.iloc[0]
L_ = [f"# Did the American Airlines - US Airways Merger Raise Fares?\n",
      f"Difference-in-differences retrospective using U.S. DOT Consumer Airfare Report data, "
      f"{qlabel(quarters[0])}-{qlabel(quarters[-1])} | {df['route'].nunique():,} routes in a balanced panel\n",
      "## Headline result\n",
      f"- **Average effect:** across all post-merger quarters, fares on the **{n_t} routes** where American and US Airways "
      f"competed head-to-head rose **{pc(r0.Effect_pct)}** relative to **{n_c} control routes** "
      f"(95% CI {pc(r0.CI_low_pct)} to {pc(r0.CI_high_pct)}; p = {r0.p_value:.2f}). This is **not statistically significant**.\n",
      f"- **Pre-existing trend:** before the merger, overlap-route fares were already falling relative to controls by "
      f"{pretrend_b:+.2%} a quarter (p = {pretrend_p:.3f}), which violates the parallel-trends assumption of the simple model.\n",
      f"- **Trend-adjusted effect:** allowing overlap routes their own trend, fares were unchanged in 2014 "
      f"({pc(results.set_index('Specification').loc['Trend-adjusted: 2014', 'Effect_pct'])}) and rose "
      f"**{pc(tr_row.Effect_pct)} in 2015-2016** (95% CI {pc(tr_row.CI_low_pct)} to {pc(tr_row.CI_high_pct)}; "
      f"p = {tr_row.p_value:.3f}), after the airlines began operating as one carrier.\n",
      f"- **Where:** the increase is concentrated on routes of 1,000+ miles "
      f"({pc(results.set_index('Specification').loc['Routes 1,000+ miles', 'Effect_pct'])}, "
      f"p = {results.set_index('Specification').loc['Routes 1,000+ miles', 'p_value']:.3f}); traffic on overlap routes rose "
      f"{pc(results.set_index('Specification').loc['Outcome: log passengers', 'Effect_pct'])}.\n",
      f"- **Conclusion:** the evidence of a merger price effect is **suggestive but not robust**: it depends on how the "
      f"pre-existing trend and the control group are specified. Taking the trend-adjusted 2015-2016 estimate at face value "
      f"implies about **{m(annual_cost)} a year** in higher fares on these routes.\n",
      "## Regression results (outcome: log average fare unless noted)\n",
      "| Specification | Effect | 95% CI | p-value | Treated | Control | Obs. |\n|---|---|---|---|---|---|---|"]
for r in results.itertuples():
    L_.append(f"| {r.Specification} | {pc(r.Effect_pct)} | {pc(r.CI_low_pct)} to {pc(r.CI_high_pct)} | {r.p_value:.3f} | "
              f"{r.Treated_routes} | {r.Control_routes} | {r.Observations:,} |")
L_.append(f"\n## Event study\n\n![event study](event_study.svg)\n\n"
          f"Joint test that the overlap-control gap was constant across all pre-merger quarters: F = {F_pre:.2f}, p = {p_pre:.3f}. "
          f"Linear pre-trend: {pretrend_b:+.2%} per quarter (p = {pretrend_p:.3f}).\n")
L_.append("| Quarter | Gap vs. pre-merger average | 95% CI |\n|---|---|---|")
for r in event.itertuples():
    L_.append(f"| {qlabel(int(r.quarter))} | {pc(r.effect_pct)} | {pc(r.ci_low)} to {pc(r.ci_high)} |")
L_.append("\n## Pre-merger route characteristics (2011Q1-2013Q3 averages)\n")
L_.append("| Group | Routes | Distance (mi) | Passengers/day | Avg fare |\n|---|---|---|---|---|")
for grp, n in [("overlap", n_t), ("control", n_c)]:
    rr = pre_bal.loc[grp]
    L_.append(f"| {grp.title()} | {n} | {rr.miles:,.0f} | {rr.pax:,.0f} | {m(rr.fare)} |")
L_.append(f"| Matched controls | {len(matched)} | {match_bal.loc['control', 'miles']:,.0f} | "
          f"{match_bal.loc['control', 'pax']:,.0f} | {m(match_bal.loc['control', 'fare'])} |")
L_.append("\n## Largest overlap routes by traffic\n")
L_.append("| Route | Pre-merger fare | Post-merger fare | Change |\n|---|---|---|---|")
for (c1, c2), r in top_routes.iterrows():
    L_.append(f"| {c1} - {c2} | {m(r.pre_fare)} | {m(r.post_fare)} | {pc(r.change_pct)} |")
L_.append("\n## Validation checks\n")
L_.append("| Check | Result | Detail |\n|---|---|---|")
for r in chk.itertuples():
    L_.append(f"| {r.Check} | {r.Result} | {r.Detail} |")
L_.append("\n## Limitations\n\n"
          "- The DOT table names only the largest and the lowest-fare carrier on each route, so head-to-head overlap is "
          "measured with a proxy; routes where AA and US were both present but neither was largest or cheapest are not identified.\n"
          "- The 2015-2016 effect relies on extrapolating the overlap routes' pre-merger trend; the simple and matched-control estimates for that period are smaller and not significant.\n"
          "- Fares are market averages across carriers and are nominal; quarter fixed effects absorb common inflation and fuel-cost changes.\n"
          "- Control routes differ in size and distance from overlap routes; the distance-band x quarter specification and the "
          "event study address this, but do not rule out every route-specific shock (for example, other carriers' entry).\n"
          "- This is a retrospective academic exercise, not a legal or expert opinion.")
open(os.path.join(OUT, "merger_report.md"), "w").write("\n".join(L_) + "\n")
print("\n".join(L_))
print(f"\nChecks: {(chk.Result == 'PASS').sum()}/{len(chk)} passed")
