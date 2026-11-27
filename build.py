#!/usr/bin/env python3
"""CashRate Watch static site generator.
Usage: python3 build.py [--csv rates_seed.csv] [--out site] [--history history/rates_history.csv]
Re-run weekly after refreshing the CSV. Rows with rate_apy == 'unknown' are never shown.
Each build appends this CSV's rates to the history store (keyed on date+name, so re-runs are no-ops)
and draws the rate-history charts from it. Points come only from real CSV rows; nothing is interpolated."""
import csv, html, os, re, sys, shutil, argparse, json
from datetime import datetime

BASE = "https://cashratewatch.com"
BRAND = "CashRate Watch"
PUBLISHER = "Sparta Labs"
POLICY_DATE = "October 21, 2026"  # effective date shown on /privacy/ and /terms/
# Email is never written literally into HTML/JS; /contact.js assembles it at runtime.
EMAIL_PARTS = ("hello", "cashratewatch", "com")
EMAIL_HTML = '<span class="js-email">hello [at] cashratewatch [dot] com</span>'
# beehiiv script-embed form. Set BEEHIIV_FORM_ID = None to hide the newsletter box everywhere.
BEEHIIV_FORM_ID = "b6176296-0930-4ddb-97c3-02cf67ec6982"
BEEHIIV_LOADER = "https://subscribe-forms.beehiiv.com/v3/loader.js"
NEWSLETTER_EMBED = BEEHIIV_FORM_ID  # kept for backwards compatibility: falsy = hidden
ap = argparse.ArgumentParser()
ap.add_argument("--csv", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "rates_seed.csv"))
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "site"))
ap.add_argument("--history", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "history", "rates_history.csv"),
                help="Rate history store (date,name,category,rate_apy). This week's rows are upserted on each build.")
ap.add_argument("--refresh-tbills", action="store_true",
                help="Fetch latest T-bill auctions from the FiscalData API, resolve each CUSIP's TreasuryDirect results page, and update the CSV")
args = ap.parse_args()

# ---- source-link policy: visible links must be human-readable pages, never API / JSON / XML endpoints ----
FISCALDATA_DATASET = "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/"
MACHINE_URL = re.compile(r"(^https?://api\.|/services/api/|/TA_WS/|\.json(\?|$)|\.xml(\?|$)|[?&]format=json)", re.I)
def human_source(url, category):
    if not MACHINE_URL.search(url):
        return url
    if category == "tbill":
        return FISCALDATA_DATASET
    print(f"WARNING: machine endpoint not shown as source: {url}", file=sys.stderr)
    return ""

def td_results_page(cusip, term):
    """TreasuryDirect's official auction-results press release (PDF) for this CUSIP and term, or None."""
    import urllib.request
    ua = {"User-Agent": "Mozilla/5.0 (CashRateWatch builder)"}
    try:
        req = urllib.request.Request(f"https://www.treasurydirect.gov/TA_WS/securities/search?cusip={cusip}&format=json", headers=ua)
        recs = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as ex:
        print(f"WARNING: TreasuryDirect lookup failed for {cusip}: {ex}", file=sys.stderr); return None
    for rec in recs:
        f = rec.get("treasuryAuctionResultsXmlFileName") or ""
        mm = re.match(r"(R_(\d{4})\d{4}_\d+)\.xml$", f)
        if rec.get("securityTerm") == term and mm:
            url = f"https://www.treasurydirect.gov/instit/annceresult/press/preanre/{mm.group(2)}/{mm.group(1)}.pdf"
            try:
                urllib.request.urlopen(urllib.request.Request(url, headers=ua, method="HEAD"), timeout=20)
                return url
            except Exception:
                return None
    return None

def refresh_tbills(csv_path):
    import urllib.request
    api = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
           "?filter=security_type:eq:Bill&sort=-auction_date&page[size]=60"
           "&fields=auction_date,security_term,issue_date,maturity_date,cusip,high_investment_rate")
    data = json.load(urllib.request.urlopen(api, timeout=30))["data"]
    latest = {}
    for x in data:
        if x["high_investment_rate"] not in (None, "null", ""):
            latest.setdefault(x["security_term"], x)
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f)); fields = list(rows[0].keys())
    stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M%z")
    for r in rows:
        if r["category"] != "tbill": continue
        term = r["name"].replace("US T-Bill ", "").replace("week", "Week")
        x = latest.get(term)
        if not x: print(f"WARNING: no posted auction for {term}", file=sys.stderr); continue
        page_url = td_results_page(x["cusip"], term)
        r["rate_apy"] = f'{float(x["high_investment_rate"]):.3f}'
        r["source_url"] = page_url or FISCALDATA_DATASET
        r["fetched_at"] = stamp[:-2] + ":" + stamp[-2:]
        r["notes"] = (f"Auction {x['auction_date']}; issued {x['issue_date']}, matures {x['maturity_date']}; "
                      f"high investment rate (bond-equivalent), CUSIP {x['cusip']}; data via FiscalData API")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    print(f"Refreshed T-bill rows in {csv_path}")
e = lambda s: html.escape(str(s), quote=True)

TYPE = {"hysa": "Savings", "money_market": "Money market", "tbill": "T-bill",
        "broker_cash": "Broker cash", "usdc": "USDC"}
TECH = re.compile(r"scrape|curl|\bJS\b|WebFetch|HTTP|URL|search-index|static HTML|embedded JSON|"
                  r"regex|redirect|\bconfirm\b|\bverify\b|secondary|not used|hard source|CUSIP|API", re.I)

def risk(label):
    l = label.lower()
    if "fdic" in l and "sipc" in l: return "FDIC / SIPC"
    if l.startswith("fdic"): return "FDIC"
    if l.startswith("sipc"): return "SIPC"
    if "treasury" in l: return "Treasury-backed"
    return "Not insured"

def public_note(row):
    parts = [p.strip() for p in re.split(r";", row["notes"]) if p.strip()]
    keep = []
    for p in parts:
        p = re.sub(r",?\s*CUSIP \w+", "", p)
        if TECH.search(p): continue
        keep.append(p)
    return "; ".join(keep)

def load():
    rows = []
    with open(args.csv, newline="") as f:
        for r in csv.DictReader(f):
            v = r["rate_apy"].strip()
            try: r["rate"] = float(v)
            except ValueError: r["rate"] = None
            r["type"] = TYPE.get(r["category"], r["category"])
            r["risk"] = risk(r["risk_label"])
            r["pnote"] = public_note(r)
            rows.append(r)
    return rows

# Rows whose source page currently 404s: rate can't be re-verified, so don't publish until the URL is fixed in the CSV.
EXCLUDE = {"E*TRADE Premium Savings", "M1 High-Yield Cash Account"}
# Rows whose notes say eligibility is unconfirmed are never ranked or tabled.
UNCONFIRMED = re.compile(r"eligibility (is )?(unconfirmed|not confirmed)|not confirmed for US", re.I)
if args.refresh_tbills:
    refresh_tbills(args.csv)
ALL = load()
UNRANKED = [r for r in ALL if r["rate"] is not None and UNCONFIRMED.search(r["notes"])]
SHOWN = sorted([r for r in ALL if r["rate"] is not None and r["name"] not in EXCLUDE and r not in UNRANKED], key=lambda r: -r["rate"])
dates = [r["fetched_at"][:10] for r in SHOWN if r["fetched_at"]]
UPD = datetime.strptime(max(dates), "%Y-%m-%d") if dates else datetime.now()
UPD_S = UPD.strftime("%b %-d, %Y"); UPD_ISO = UPD.strftime("%Y-%m-%d")
def get(name, pool=None):
    for r in (pool or SHOWN):
        if r["name"] == name: return r
    return None
def pct(name, default="n/a"):
    r = get(name); return f'{r["rate_apy"]}%' if r else default
def best(cat):
    rs = [r for r in SHOWN if r["category"] == cat]; return rs[0] if rs else None

# ---------- rate history (real points only; never interpolated) ----------
HIST_FIELDS = ["date", "name", "category", "rate_apy"]
def update_history(path, rows):
    """Upsert this CSV's rows into the history store, keyed on (date, name). date = the row's own fetched_at date,
    so rows that were not re-checked this week keep their old date and add nothing. Re-running is a no-op."""
    pts = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f): pts[(r["date"], r["name"])] = {k: r[k] for k in HIST_FIELDS}
    added = changed = 0
    for r in rows:
        if r["rate"] is None or not r["fetched_at"]: continue
        new = {"date": r["fetched_at"][:10], "name": r["name"], "category": r["category"], "rate_apy": r["rate_apy"].strip()}
        k = (new["date"], new["name"]); old = pts.get(k)
        if old == new: continue
        added += old is None; changed += old is not None; pts[k] = new
    if added or changed:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HIST_FIELDS); w.writeheader()
            w.writerows(sorted(pts.values(), key=lambda r: (r["date"], r["category"], r["name"])))
        os.replace(tmp, path)
    print(f"History {path}: {added} added, {changed} updated, {len(pts)} points total")
    return list(pts.values())
HIST = update_history(args.history, ALL)
HIDE = EXCLUDE | {r["name"] for r in UNRANKED}   # never charted, same as never tabled
HIST_OK = [dict(h, v=float(h["rate_apy"])) for h in HIST if h["name"] not in HIDE]
HIST_DATES = sorted({h["date"] for h in HIST_OK})

def series(cat, how):
    """[(date, value, name)] per check date: top rate in the category, or the mean of listed rates."""
    out = []
    for d in HIST_DATES:
        hs = [h for h in HIST_OK if h["date"] == d and h["category"] == cat]
        if not hs: continue
        if how == "top":
            t = max(hs, key=lambda h: h["v"]); out.append((d, t["v"], t["name"]))
        else:
            out.append((d, sum(h["v"] for h in hs) / len(hs), f"{len(hs)} listed"))
    return out

def fdate(d, fmt="%b %-d"): return datetime.strptime(d, "%Y-%m-%d").strftime(fmt)

_CHART_N = [0]
def chart(lines, title, note=""):
    """Inline SVG line chart. lines = [(label, color, dash, [(date, value, name)])]. Works with 1 point (dots only)."""
    lines = [l for l in lines if l[3]]
    if not lines: return ""
    W, H, L, R, T, B = 420, 200, 40, 14, 12, 28
    ds = sorted({p[0] for l in lines for p in l[3]})
    vs = [p[1] for l in lines for p in l[3]]
    lo, hi = min(vs), max(vs)
    span = max(hi - lo, 0.2)
    step = next(s for s in (0.1, 0.25, 0.5, 1.0, 2.0) if span / s <= 5)
    import math
    lo = math.floor((lo - span * 0.08) / step) * step; hi = math.ceil((hi + span * 0.08) / step) * step
    d0 = datetime.strptime(ds[0], "%Y-%m-%d"); days = (datetime.strptime(ds[-1], "%Y-%m-%d") - d0).days
    def X(d):
        if days == 0: return L + (W - L - R) / 2
        return L + 8 + (W - L - R - 16) * (datetime.strptime(d, "%Y-%m-%d") - d0).days / days
    def Y(v): return T + (H - T - B) * (hi - v) / (hi - lo)
    _CHART_N[0] += 1; cid = f"hc{_CHART_N[0]}"
    g = [f'<svg class="hc" viewBox="0 0 {W} {H}" role="img" aria-labelledby="{cid}"><title id="{cid}">{e(title)}</title>']
    v = lo
    while v <= hi + 1e-9:
        y = Y(v)
        g.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e3e5df"/>'
                 f'<text x="{L-5}" y="{y+4:.1f}" text-anchor="end">{v:.2f}%</text>')
        v += step
    # x labels: at most 6, evenly picked, always first and last; skip ones that would collide
    idx = sorted({round(i * (len(ds) - 1) / 5) for i in range(6)}) if len(ds) > 6 else range(len(ds))
    lastx = -99
    for i in idx:
        x = X(ds[i])
        if x - lastx < 46 and i != len(ds) - 1: continue
        if x - lastx < 46: g.pop()   # keep the last label, drop the one it collides with
        g.append(f'<text x="{x:.1f}" y="{H-8}" text-anchor="middle">{fdate(ds[i])}</text>'); lastx = x
    r = 3.5 if len(ds) <= 12 else 2.2
    for label, color, dash, pts in lines:
        if len(pts) > 1:
            g.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.2"{f" stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""} '
                     f'points="{" ".join(f"{X(d):.1f},{Y(val):.1f}" for d, val, _ in pts)}"/>')
        for d, val, nm in pts:
            g.append(f'<circle cx="{X(d):.1f}" cy="{Y(val):.1f}" r="{r}" fill="{color}"><title>{e(label)}, {fdate(d)}: {val:.2f}% ({e(nm)})</title></circle>')
    g.append("</svg>")
    leg = "".join(f'<li><i style="background:{c}"></i>{e(lb)} <b>{p[-1][1]:.2f}%</b>'
                  + (f' <small>{e(p[-1][2])}</small>' if not p[-1][2].endswith("listed") else "") + '</li>' for lb, c, _, p in lines)
    n = len(HIST_DATES)
    start = fdate(HIST_DATES[0], "%b %Y")
    cap = (f'History starts {start}. {n} rate check{"s" if n != 1 else ""} so far '
           f'({fdate(HIST_DATES[0], "%b %-d, %Y")}{" to " + fdate(HIST_DATES[-1], "%b %-d, %Y") if n > 1 else ""}); a point is added each week.')
    return (f'<figure class="box hist"><figcaption><b>{e(title)}</b></figcaption>{"".join(g)}<ul class="hl">{leg}</ul>'
            f'<p class="foot-n">{cap}{" " + note if note else ""}</p></figure>')

CAT_COLOR = {"hysa": ("#0b4f8a", ""), "money_market": ("#6a3fb5", "6 3"), "tbill": ("#0b6e4f", ""),
             "broker_cash": ("#b85c00", "2 3"), "usdc": ("#7a7f7c", "8 3 2 3")}
def home_chart():
    lab = {"hysa": "Savings", "money_market": "Money market", "tbill": "T-bill", "broker_cash": "Broker cash", "usdc": "USDC rewards"}
    return chart([(lab[c], *CAT_COLOR[c], series(c, "top")) for c in lab], "Top rate by type over time",
                 "Each line is the highest rate we list in that type on each check date; the provider holding the top spot can change. "
                 "USDC is a rewards rate, not interest.")
def cat_chart(cat, label):
    top, avg = series(cat, "top"), series(cat, "avg")
    color, _ = CAT_COLOR[cat]
    lines = [("Top", color, "", top)]
    if [round(p[1], 4) for p in avg] != [round(p[1], 4) for p in top]:
        lines.append(("Average of all listed", "#9aa29f", "5 3", avg))
    return chart(lines, f"{label[0].upper() + label[1:]}: top and average over time",
                 "The average covers every option we list with a confirmed rate on that date." if len(lines) > 1 else "")

CSS = """
:root{--bg:#fbfbf8;--fg:#1d2321;--mut:#5d6764;--line:#e3e5df;--acc:#0b6e4f;--acc2:#e8f3ee;--warn:#8a5a00}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--acc)}header.top{border-bottom:1px solid var(--line);background:#fff}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px}
.top .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:6px 18px;padding:12px 16px}
.logo{font-weight:800;font-size:1.15rem;text-decoration:none;color:var(--fg);display:flex;align-items:center;gap:8px}
.logo img{width:26px;height:26px}nav{display:flex;flex-wrap:wrap;gap:0 4px}nav a{color:var(--mut);text-decoration:none;font-size:.93rem;display:inline-flex;align-items:center;min-height:44px;padding:0 8px;white-space:nowrap;border-radius:8px}nav a:hover{color:var(--acc);background:var(--acc2)}.nl-short{display:none}
h1{font-size:clamp(1.6rem,4vw,2.3rem);line-height:1.15;margin:28px 0 6px}h2{margin-top:34px;font-size:1.3rem}h3{font-size:1.08rem;margin:20px 0 6px}
.upd{color:var(--mut);font-size:.92rem;margin:0 0 14px}.lead{font-size:1.08rem;max-width:760px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px}.card b{display:block;font-size:1.5rem;color:var(--acc)}
.card span{font-size:.85rem;color:var(--mut)}.card .card-tag{font-size:.8rem;font-weight:600;color:var(--warn);white-space:nowrap}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 8px}.controls input,.controls select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#fff}
.controls input{flex:1 1 220px}
.tw{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.95rem}th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#f3f4ef;font-size:.82rem;text-transform:uppercase;letter-spacing:.03em;color:var(--mut);white-space:nowrap}
th[data-k]{cursor:pointer;user-select:none}th[data-k]:after{content:" \\2195";opacity:.4}th.asc:after{content:" \\2191";opacity:1}th.desc:after{content:" \\2193";opacity:1}
td.apy{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}td small{display:block;color:var(--mut);font-size:.8rem}
.b{display:inline-block;font-size:.78rem;padding:2px 8px;border-radius:99px;white-space:nowrap;font-weight:600}
.b-FDIC{background:#e3f1ff;color:#0b4f8a}.b-SIPC{background:#efe8ff;color:#4b2a96}.b-Treasury-backed{background:var(--acc2);color:var(--acc)}
.b-Not-insured{background:#fff1dc;color:var(--warn)}.b-FDIC-SIPC{background:#e9ecff;color:#2c3c8f}
.foot-n{font-size:.85rem;color:var(--mut)}
.box{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin:20px 0}
.risk dt{font-weight:700;margin-top:8px}.risk dd{margin:2px 0 0}
.nl{background:var(--acc2);border:1px solid #cfe5da}.nl-embed{margin:0 -16px}@media(min-width:641px){.nl-embed{max-width:560px;margin:0 auto}}.nl-embed iframe{display:block;max-width:100%}.nl-embed{overflow-x:auto}.nl-legal{font-size:.82rem;color:var(--mut);margin:8px 0 0}.nl form{display:flex;flex-wrap:wrap;gap:8px}.nl input{flex:1 1 220px;font:inherit;padding:10px;border:1px solid #bcd8ca;border-radius:8px}
.nl button{font:inherit;font-weight:700;padding:10px 18px;border:0;border-radius:8px;background:var(--acc);color:#fff;cursor:pointer}
footer{margin-top:48px;border-top:1px solid var(--line);padding:20px 0 34px;color:var(--mut);font-size:.88rem}footer a{color:var(--mut);margin-right:14px;display:inline-block;padding:6px 0}
.prose{max-width:760px}.prose li{margin:4px 0}
.hist{max-width:620px;margin:24px 0}.hist figcaption{margin-bottom:6px}.hc{display:block;width:100%;height:auto;font:12px system-ui,-apple-system,Segoe UI,Roboto,sans-serif;fill:#5d6764}
.hl{list-style:none;padding:0;margin:8px 0 0;line-height:1.35;display:flex;flex-wrap:wrap;gap:4px 16px;font-size:.88rem}.hl i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}.hl small{color:var(--mut)}
.rel{font-size:.95rem}.rel a{display:inline-block;padding:4px 0;margin-right:14px}
table.kv th{text-transform:none;font-size:.85rem;letter-spacing:0;white-space:normal}table.kv td,table.kv th{min-width:110px}
@media(max-width:640px){.box.nl{margin-left:-16px;margin-right:-16px;border-radius:0;border-left:0;border-right:0}.top .wrap{padding:8px 12px}nav{width:100%;justify-content:space-between;gap:0}nav a{font-size:.86rem;padding:0 4px}.nl-full{display:none}.nl-short{display:inline}th,td{padding:8px 7px;font-size:.88rem}.hide-sm{display:none}table.kv td,table.kv th{min-width:0}}
"""

JS = """<script>
document.querySelectorAll('.rt').forEach(function(w){var t=w.querySelector('table'),tb=t.tBodies[0],rows=[].slice.call(tb.rows),
q=w.querySelector('.q'),ft=w.querySelector('.ft'),fr=w.querySelector('.fr'),n=w.querySelector('.n');
function f(){var s=(q&&q.value||'').toLowerCase(),a=ft&&ft.value,b=fr&&fr.value,c=0;rows.forEach(function(r){var ok=(!s||r.textContent.toLowerCase().indexOf(s)>-1)&&(!a||r.dataset.t==a)&&(!b||r.dataset.r==b);r.hidden=!ok;if(ok)c++});if(n)n.textContent=c}
[q,ft,fr].forEach(function(el){el&&el.addEventListener('input',f)});
t.querySelectorAll('th[data-k]').forEach(function(h,i){h.addEventListener('click',function(){var k=h.dataset.k,ds=h.classList.contains('asc')||(k=='a'&&!h.classList.contains('desc')),sg=ds?-1:1;
t.querySelectorAll('th').forEach(function(x){x.classList.remove('asc','desc')});h.classList.add(ds?'desc':'asc');
rows.sort(function(x,y){var u=x.dataset[k],v=y.dataset[k];if(k=='a')return (u-v)*sg;return u.localeCompare(v)*sg});rows.forEach(function(r){tb.appendChild(r)})})})});
</script>"""

def table(rows, filters=True, tid="t"):
    types = sorted({r["type"] for r in rows}); risks = sorted({r["risk"] for r in rows})
    h = ['<div class="rt">']
    if filters:
        h.append('<div class="controls"><input class="q" type="search" placeholder="Search name or notes" aria-label="Search">')
        if len(types) > 1:
            h.append('<select class="ft" aria-label="Type"><option value="">All types</option>' + "".join(f'<option>{e(t)}</option>' for t in types) + '</select>')
        if len(risks) > 1:
            h.append('<select class="fr" aria-label="Risk"><option value="">All risk labels</option>' + "".join(f'<option>{e(t)}</option>' for t in risks) + '</select>')
        h.append(f'<span class="foot-n" style="align-self:center"><span class="n">{len(rows)}</span> options</span></div>')
    cats = {r["category"] for r in rows}
    rate_h = "Rewards rate" if cats == {"usdc"} else ("Rate" if "usdc" in cats else "APY / yield")
    h.append(f'<div class="tw"><table><thead><tr><th data-k="n">Name</th><th data-k="t">Type</th><th data-k="a" class="desc">{rate_h}</th>'
             '<th data-k="r">Risk label</th><th class="hide-sm">Minimum</th><th>Source</th></tr></thead><tbody>')
    for r in rows:
        mn = "&mdash;" if r["min_deposit"].strip().lower() in ("unknown", "") else e(r["min_deposit"])
        src = human_source(r["source_url"], r["category"])
        note = f'<small>{e(r["pnote"])}</small>' if r["pnote"] else ""
        cls = "b-" + re.sub(r"[^A-Za-z]+", "-", r["risk"]).strip("-")
        h.append(f'<tr data-n="{e(r["name"].lower())}" data-t="{e(r["type"])}" data-a="{r["rate"]}" data-r="{e(r["risk"])}">'
                 f'<td>{e(r["name"])}{note}</td><td>{e(r["type"])}</td><td class="apy">{e(r["rate_apy"])}%{'<small>rewards rate</small>' if r["category"] == "usdc" else ''}</td>'
                 f'<td><span class="b {cls}">{e(r["risk"])}</span></td><td class="hide-sm">{mn}</td>'
                 + (f'<td><a href="{e(src)}" rel="nofollow noopener" target="_blank">source</a></td></tr>' if src else '<td>&mdash;</td></tr>'))
    h.append('</tbody></table></div></div>')
    return "".join(h)

TABLE_NOTE = ('<p class="foot-n">Savings and broker cash show APY. Money market funds show 7-day yield. '
              'T-bills show the auction high investment rate (bond-equivalent yield). USDC shows a rewards rate, not interest. '
              f'Rates checked {UPD_S}; they change often, so always confirm on the provider\'s site. Not financial advice.</p>')

USDC_NOTE = ('<p class="foot-n" id="usdc-note"><b>About USDC rewards rates:</b> these are rewards set by each platform. '
             'They are not interest or APY, are not guaranteed, can change or stop at any time, may require a paid membership '
             '(for example Coinbase One), and may not be available in every state. USDC is not FDIC- or SIPC-insured. '
             'Nothing here is a recommendation to buy crypto.</p>')

RISK_EXPLAINER = """<div class="box risk"><h2 style="margin-top:0">What the risk labels mean</h2><dl>
<dt><span class="b b-FDIC">FDIC</span></dt><dd>A bank deposit. If the bank fails, the FDIC covers up to $250,000 per depositor, per bank, per ownership category. Broker "cash sweep" accounts spread money across partner banks to raise that limit.</dd>
<dt><span class="b b-SIPC">SIPC</span></dt><dd>A security held at a brokerage, such as a money market fund. SIPC protects you if the <em>brokerage</em> fails and your assets go missing (up to $500,000, including $250,000 cash). It does <em>not</em> protect against the fund losing value. Government money funds aim for a stable $1 share price, which is not guaranteed.</dd>
<dt><span class="b b-Treasury-backed">Treasury-backed</span></dt><dd>A direct loan to the US government (T-bills). There is no insurance, and none is needed in the usual sense: you are paid by the US Treasury. If you sell before maturity the price can move a little.</dd>
<dt><span class="b b-Not-insured">Not insured</span></dt><dd>Crypto stablecoin rewards (USDC). There is no FDIC or SIPC coverage. You depend on the exchange, the stablecoin issuer, and rules that are still being written.</dd>
<dt><span class="b b-FDIC-SIPC">FDIC / SIPC</span></dt><dd>A mix. For example, part of the balance is held as brokerage cash (SIPC) and the rest is swept to banks (FDIC).</dd></dl></div>"""

def newsletter(compact=False, box_id="newsletter"):
    """'Weekly email, free' box with the beehiiv form. The loader script is emitted only inside this box,
    so it loads only on pages that show the box."""
    if not NEWSLETTER_EMBED:
        return ""
    embed = (f'<div class="nl-embed"><script async src="{e(BEEHIIV_LOADER)}" data-beehiiv-form="{e(BEEHIIV_FORM_ID)}"></script></div>'
             '<p class="nl-legal">Signups are handled by beehiiv. We never sell your email. '
             '<a href="/privacy/#newsletter">Privacy policy</a></p>')
    id_attr = f' id="{box_id}"' if box_id else ""
    if compact:
        return (f'<div class="box nl nl-compact"{id_attr}><h2 style="margin:0 0 4px;font-size:1.1rem">Weekly email, free</h2>'
                f'<p style="margin:0 0 8px">Top rates and what moved, once a week.</p>{embed}</div>')
    return (f'<div class="box nl"{id_attr}><h2 style="margin-top:0">Weekly email, free</h2>'
            f'<p>Get the weekly rate check: top rates, what moved, and the latest T-bill auctions. Unsubscribe anytime.</p>{embed}</div>')
NEWSLETTER = newsletter(compact=True)   # bottom of category / comparison pages
NEWSLETTER_FULL = newsletter(box_id="newsletter-more")  # home page, below the main table
NEWSLETTER_TOP = newsletter(compact=True, box_id="newsletter")  # home page, just under the top rate cards

# Experiment W1 (2026-11-18): on comparison pages, a second copy of the beehiiv form right after the first
# rate table, with page-specific copy. The bottom box (id="newsletter") stays. Measured as net new active
# beehiiv subscribers Nov 19-Dec 2. The v3 loader has no per-form tag, so top vs bottom signups can't be told apart.
W1_TOP_COPY = {
    "/usdc-vs-high-yield-savings/": "Get one email a week when USDC rewards and savings rates move.",
    "/best-broker-cash-rates/": "Get one email a week when broker cash rates move.",
    "/best-money-market-funds/": "Get one email a week when money market fund yields move.",
    "/t-bills-vs-high-yield-savings/": "Get one email a week with new T-bill auction rates and what moved in savings.",
    "/high-yield-savings-vs-money-market-fund/": "Get one email a week when savings rates and money fund yields move.",
    "/high-yield-savings-promo-vs-base-rate/": "Get one email a week when promo and ongoing savings rates change.",
    "/t-bills-vs-money-market-funds/": "Get one email a week with new T-bill auction rates and what moved in money funds.",
    "/spaxx-vs-vmfxx-vs-swvxx/": "Get one email a week when these fund yields move, SPAXX, VMFXX and SWVXX included.",
    "/schwab-bank-sweep-vs-swvxx/": "Get one email a week when broker sweep rates and fund yields like SWVXX move.",
    "/state-tax-free-money-market-funds/": "Get one email a week when Treasury money fund yields move.",
    "/fdlxx-vs-spaxx/": "Get one email a week when Fidelity fund yields like FDLXX and SPAXX move.",
    "/sgov-vs-money-market-funds/": "Get one email a week when T-bill ETF and money fund yields move.",
    "/sgov-vs-bil-vs-usfr/": "Get one email a week when T-bill yields move, with the latest auction rates.",
    "/wealthfront-vs-betterment-vs-robinhood-cash/": "Get one email a week when cash account rates like these change.",
    "/vusxx-vs-vmfxx/": "Get one email a week when Vanguard fund yields like VUSXX and VMFXX move.",
    "/i-bonds-vs-t-bills/": "Get one email a week with the latest T-bill auction rates.",
    "/schwab-money-market-funds/": "Get one email a week when money fund yields like Schwab's move.",
    "/ally-vs-marcus-savings/": "Get one email a week when online savings rates like these change.",
    "/capital-one-vs-ally-savings/": "Get one email a week when online savings rates like these change.",
    "/amex-vs-marcus-savings/": "Get one email a week when online savings rates like these change.",
    "/sofi-vs-marcus-savings/": "Get one email a week when online savings rates like these change.",
    "/synchrony-vs-ally-savings/": "Get one email a week when online savings rates like these change.",
    "/raisin-vs-marcus-savings/": "Get one email a week when top savings rates like these change.",
    "/vanguard-cash-plus-vs-vmfxx/": "Get one email a week when Vanguard cash rates and fund yields like VMFXX move.",
    "/spaxx-vs-fzfxx/": "Get one email a week when Fidelity fund yields like SPAXX and FZFXX move.",
    "/coinbase-vs-kraken-usdc-rewards/": "Get one email a week when USDC rewards, savings and T-bill rates move.",
    "/barclays-vs-marcus-savings/": "Get one email a week when online savings rates like these change.",
    "/interactive-brokers-vs-schwab-cash-interest/": "Get one email a week when broker cash rates and fund yields like SWVXX move.",
    "/sofi-vs-ally-savings/": "Get one email a week when online savings rates like these change.",
    "/cd-rates/": "Get one email a week when CD and savings rates move.",
    "/cds-vs-t-bills/": "Get one email a week with new T-bill auction rates and what moved in CDs.",
    "/cd-vs-high-yield-savings/": "Get one email a week when CD and savings rates move.",
    "/cd-vs-money-market-fund/": "Get one email a week with new money market yields and what moved in CDs.",
    "/marcus-vs-bread-savings-cd/": "Get one email a week when CD rates at Marcus, Bread Savings and other banks move.",
}

def newsletter_top(path):
    """Compact signup box for just under a comparison page's first rate table (id="newsletter-top")."""
    if not NEWSLETTER_EMBED or path not in W1_TOP_COPY:
        return ""
    embed = (f'<div class="nl-embed"><script async src="{e(BEEHIIV_LOADER)}" data-beehiiv-form="{e(BEEHIIV_FORM_ID)}"></script></div>'
             '<p class="nl-legal">Free. Unsubscribe anytime. Signups are handled by beehiiv. We never sell your email. '
             '<a href="/privacy/#newsletter">Privacy policy</a></p>')
    return (f'<div class="box nl nl-compact nl-top" id="newsletter-top"><h2 style="margin:0 0 4px;font-size:1.1rem">Weekly email, free</h2>'
            f'<p style="margin:0 0 8px">{e(W1_TOP_COPY[path])}</p>{embed}</div>')

_FIRST_TABLE_END = re.compile(r'</table>(?:\s*</div>)*(?:\s*<p class="foot-n">.*?</p>)?', re.S)
def add_top_signup(path, body):
    box = newsletter_top(path)
    if not box:
        return body
    m = _FIRST_TABLE_END.search(body)
    if not m:
        raise SystemExit(f"W1: no rate table on {path}")
    return body[:m.end()] + box + body[m.end():]

NAV_SHORT = {"Money market": "Money mkt", "Broker cash": "Brokers"}  # labels used on phones (<=640px)
NAV = [("/savings/", "Savings"), ("/cd-rates/", "CDs"), ("/money-market/", "Money market"), ("/treasury-bills/", "T-bills"),
       ("/broker-cash/", "Broker cash"), ("/usdc-rewards/", "USDC")]
PAGES = []
COMPARE = [("/usdc-vs-high-yield-savings/", "USDC vs high-yield savings"), ("/best-broker-cash-rates/", "Best broker cash rates"),
           ("/best-money-market-funds/", "Best money market fund yields"), ("/t-bills-vs-high-yield-savings/", "T-bills vs high-yield savings"),
           ("/high-yield-savings-vs-money-market-fund/", "High-yield savings vs money market funds"),
           ("/high-yield-savings-promo-vs-base-rate/", "Promo vs ongoing savings rates"),
           ("/t-bills-vs-money-market-funds/", "T-bills vs money market funds"),
           ("/spaxx-vs-vmfxx-vs-swvxx/", "SPAXX vs VMFXX vs SWVXX"),
           ("/schwab-bank-sweep-vs-swvxx/", "Schwab bank sweep vs SWVXX"),
           ("/state-tax-free-money-market-funds/", "State-tax-free money market funds"),
           ("/fdlxx-vs-spaxx/", "FDLXX vs SPAXX"),
           ("/sgov-vs-money-market-funds/", "SGOV vs money market funds"),
           ("/sgov-vs-bil-vs-usfr/", "SGOV vs BIL vs USFR"),
           ("/wealthfront-vs-betterment-vs-robinhood-cash/", "Wealthfront vs Betterment vs Robinhood cash"),
           ("/vusxx-vs-vmfxx/", "VUSXX vs VMFXX"),
           ("/i-bonds-vs-t-bills/", "I bonds vs T-bills"),
           ("/schwab-money-market-funds/", "Schwab money market funds"),
           ("/ally-vs-marcus-savings/", "Ally vs Marcus savings"),
           ("/capital-one-vs-ally-savings/", "Capital One vs Ally savings"),
           ("/amex-vs-marcus-savings/", "Amex vs Marcus savings"),
           ("/sofi-vs-marcus-savings/", "SoFi vs Marcus savings"),
           ("/synchrony-vs-ally-savings/", "Synchrony vs Ally savings"),
           ("/raisin-vs-marcus-savings/", "Raisin vs Marcus savings"),
           ("/vanguard-cash-plus-vs-vmfxx/", "Vanguard Cash Plus vs VMFXX"),
           ("/spaxx-vs-fzfxx/", "SPAXX vs FZFXX"),
           ("/coinbase-vs-kraken-usdc-rewards/", "Coinbase vs Kraken USDC rewards"),
           ("/barclays-vs-marcus-savings/", "Barclays vs Marcus savings"),
           ("/interactive-brokers-vs-schwab-cash-interest/", "Interactive Brokers vs Schwab cash interest"),
           ("/sofi-vs-ally-savings/", "SoFi vs Ally savings"),
           ("/cds-vs-t-bills/", "CDs vs T-bills"),
           ("/cd-vs-high-yield-savings/", "CD vs high-yield savings"),
           ("/cd-vs-money-market-fund/", "CD vs money market fund"),
           ("/marcus-vs-bread-savings-cd/", "Marcus vs Bread Savings CD rates"),
           ("/rate-changes/", "What moved this week")]
def related(here):
    return ('<div class="box rel"><b>Compare:</b> ' + "".join(f'<a href="{p}">{e(l)}</a>' for p, l in COMPARE if p != here) + '</div>')

def meta_desc(text, limit=155):
    """Plain-text description that fits in a search snippet. Never cuts mid-word."""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    window = text[:limit + 1]
    cut = None
    for sep in (". ", "? ", "! "):
        i = window.rfind(sep)
        if i >= 40:
            cut = window[:i + 1].strip()
            break
    if cut is None:
        i = window.rfind(" ")
        cut = (window[:i] if i > 0 else text[:limit]).rstrip(" ,;:-")
        trail = {"the", "a", "an", "and", "or", "of", "for", "with", "to", "by", "as", "such"}
        while " " in cut and cut.rsplit(" ", 1)[-1].lower() in trail:
            cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if not cut.endswith("."):
            cut += "."
    return cut

def schema_head(page_desc, items=None, faqs=None):
    graph = [
        {"@type": "Organization", "@id": f"{BASE}/#org", "name": BRAND,
         "url": BASE + "/", "parentOrganization": {"@type": "Organization", "name": PUBLISHER}},
        {"@type": "WebSite", "@id": f"{BASE}/#website", "name": BRAND, "url": BASE + "/",
         "description": page_desc, "publisher": {"@id": f"{BASE}/#org"}},
    ]
    if items:
        graph.append({
            "@type": "ItemList",
            "name": "Where to park cash this week",
            "numberOfItems": len(items),
            "itemListElement": [
                {"@type": "ListItem", "position": i, "name": r["name"],
                 "description": f'{r["rate_apy"]}% {r["type"]}, {r["risk"]}'}
                for i, r in enumerate(items, 1)
            ],
        })
    if faqs:
        graph.append({
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in faqs
            ],
        })
    return '<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False) + "</script>"

# Wells Fargo Platinum Savings new-customer offer, read Nov 23, 2026 on wellsfargo.com/savings-cds/rates/ (location shown: CA 94105).
WF_PROMO_DATE = "Nov 23, 2026"
WF_PROMO_TXT = (f"On {WF_PROMO_DATE}, Wells Fargo offered new savings customers 3.50% APY (3.44% interest rate) on Platinum Savings "
                "if they deposit and keep $25,000 or more in the new account within 30 days. The rate is variable and was shown for ZIP 94105; "
                "Wells Fargo sets savings and CD rates by ZIP code, so check its rates page for yours. Platinum Savings has a $12 monthly fee "
                "unless you keep a $3,500 minimum daily balance.")

def faq_block(faqs):
    if not faqs:
        return ""
    bits = ['<div class="prose"><h2>Questions</h2>']
    for q, a in faqs:
        bits.append(f"<h3>{e(q)}</h3><p>{e(a)}</p>")
    bits.append("</div>")
    return "".join(bits)

def page(path, title, desc, body, h1=None, items=None, faqs=None):

    url = BASE + path
    desc = meta_desc(desc)
    body = add_top_signup(path, body)
    nav = "".join(f'<a href="{p}" aria-label="{e(l)}"><span class="nl-full">{e(l)}</span><span class="nl-short" aria-hidden="true">{e(NAV_SHORT[l])}</span></a>'
                  if l in NAV_SHORT else f'<a href="{p}">{e(l)}</a>' for p, l in NAV)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc)}">
<link rel="canonical" href="{url}"><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta property="og:type" content="website"><meta property="og:site_name" content="{BRAND}">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{url}"><meta name="twitter:card" content="summary">
{schema_head(desc, items, faqs)}
<style>{CSS}</style></head><body>
<header class="top"><div class="wrap"><a class="logo" href="/"><img src="/favicon.svg" alt="">{BRAND}</a><nav>{nav}</nav></div></header>
<main class="wrap">{('<h1>'+e(h1)+'</h1>') if h1 else ''}{body}</main>
<footer><div class="wrap"><a href="/">Home</a><a href="/about/">About</a><a href="/disclosures/">Disclosures</a><a href="/privacy/">Privacy</a><a href="/terms/">Terms</a>
{"".join(f'<a href="{p}">{e(l)}</a>' for p, l in COMPARE)}
<p>Some links may be affiliate links. We may earn a commission at no cost to you; it never changes how rates are ranked or shown. <a href="/disclosures/#affiliate">Affiliate disclosure</a></p>
<p>&copy; {UPD.year} {BRAND}, published by {PUBLISHER}. Information only, not financial advice. Rates change; confirm with the provider. Contact: {EMAIL_HTML}</p></div></footer>
{JS}<script src="/contact.js" defer></script></body></html>"""
    d = os.path.join(args.out, path.strip("/"))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w") as f: f.write(doc)
    PAGES.append(path)

def upd(n): return f'<p class="upd">Updated {UPD_S} &middot; {n} option{"s" if n != 1 else ""} with a confirmed rate</p>'

# ---------- build ----------
if os.path.isdir(args.out): shutil.rmtree(args.out)
os.makedirs(args.out)

tb13 = get("US T-Bill 13-week")
_m = re.search(r"Auction (\d{4}-\d{2}-\d{2})", tb13["notes"]) if tb13 else None
tb13_label = "13-week T-bill" + (f" (auction {_m.group(1)})" if _m else " (auction date not available)")
cards = []
RATE_LOCK = re.compile(r"(\d+-day rate lock)", re.I)
for label, r in [("Top FDIC savings", best("hysa")), ("Top money market fund", best("money_market")),
                 (tb13_label, tb13), ("Top broker cash", best("broker_cash")), ("USDC rewards rate (Coinbase One)", get("Coinbase USDC rewards"))]:
    if r:
        # Promo tag comes from the row's own note (e.g. "90-day rate lock"), so it disappears when a non-promo row takes the spot.
        _lock = RATE_LOCK.search(r["notes"])
        tag = f' <small class="card-tag">({e(_lock.group(1).lower())})</small>' if _lock else ""
        cards.append(f'<div class="card"><span>{e(label)}</span><b>{e(r["rate_apy"])}%{tag}</b><span>{e(r["name"])}</span></div>')
home = (upd(len(SHOWN)) + '<p class="lead">Where to park cash in the US this week: high-yield savings, money market funds, broker cash sweeps, '
        'Treasury bills and USDC rewards side by side, each with a plain risk label. Every rate comes from the provider\'s own page and refreshes weekly.</p>'
        f'<div class="cards">{"".join(cards)}</div>' + NEWSLETTER_TOP + table(SHOWN) + TABLE_NOTE
        + (USDC_NOTE if any(r["category"] == "usdc" for r in SHOWN) else "") + NEWSLETTER_FULL + home_chart() + related("/") + RISK_EXPLAINER)
page("/", f"{BRAND}: best places to park cash this week (savings, money market, T-bills, USDC)",
     "This week's high-yield savings, money market, broker cash, T-bill and USDC rates, with plain FDIC, SIPC and not-insured labels. Updated weekly.",
     home, h1="Where to park your cash this week", items=SHOWN)

def note_as_of(row):
    m = re.search(r"as of (\d{1,2}/\d{1,2}/\d{4})", row.get("notes") or "")
    return m.group(1) if m else None
def note_expense(row):
    m = re.search(r"expense (\d+(?:\.\d+)?)%", row.get("notes") or "")
    return m.group(1) if m else None
def auction_on(row):
    m = re.search(r"Auction (\d{4}-\d{2}-\d{2})", row.get("notes") or "")
    return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%b %-d, %Y") if m else None
def tbill_term(row):
    return row["name"].replace("US T-Bill ", "")

CATS = [
 ("/savings/", "hysa", "High-yield savings account rates", "Best high-yield savings account rates this week",
  "Online banks pay far more than the national average savings rate. All of these are FDIC-insured bank deposits up to $250,000 per depositor, per bank. Watch for promo rates and balance tiers, noted under each name."),
 ("/money-market/", "money_market", "Money market fund yields", "Money market fund yields: VMFXX, SPAXX, SWVXX and more",
  "Money market funds hold short-term debt, such as Treasury bills, and pay a yield that tracks short-term interest rates. They are securities, not bank deposits: not FDIC-insured, though held at a brokerage they are covered by SIPC against broker failure. Yields shown are 7-day yields."),
 ("/treasury-bills/", "tbill", "Treasury bill rates (latest auctions)", "Latest US Treasury bill auction rates: 4, 8, 13, 17, 26 and 52 weeks",
  "T-bills are short-term loans to the US government, sold at regular auctions (weekly for most terms). The rates below are each term's latest auction high investment rate (a bond-equivalent yield), from the Treasury's own FiscalData API. Interest is exempt from state and local income tax. You can buy at TreasuryDirect.gov or through most brokers."),
 ("/broker-cash/", "broker_cash", "Broker cash and sweep rates", "Broker cash sweep rates: Robinhood, Wealthfront, Schwab, IBKR and more",
  "Uninvested cash in a brokerage account is usually swept to partner banks (FDIC) or held as a free credit balance (SIPC). Some brokers pay close to market rates; others pay almost nothing by default. Some top rates require a paid subscription."),
 ("/usdc-rewards/", "usdc", "USDC rewards rates on exchanges", "USDC rewards rates on Coinbase and other exchanges",
  "Some crypto exchanges pay rewards for holding USDC, a dollar-pegged stablecoin. These balances are not FDIC- or SIPC-insured, eligibility varies by country and state, and US rules on stablecoin rewards are still being settled. Kraken also advertises USDC rewards, but they are not confirmed for US users, so we do not list them. See our <a href=\"/usdc-vs-high-yield-savings/\">USDC vs high-yield savings</a> comparison."),
]
CAT_LABEL = {"hysa": "savings rate", "money_market": "money market yield", "tbill": "T-bill rate",
             "broker_cash": "broker cash rate", "usdc": "USDC rewards rate"}
for path, cat, h1, title, intro in CATS:
    rs = [r for r in SHOWN if r["category"] == cat]
    page_h1, page_title, lead, faqs = h1, f"{title} | {BRAND}", intro, None
    if path == "/treasury-bills/" and rs:
        top = max(rs, key=lambda r: r["rate"])
        term, auc = tbill_term(top), auction_on(top)
        page_h1 = "Highest T-bill rate this week"
        page_title = f"Highest T-bill rate this week: {term} {top['rate_apy']}% (updated {UPD_S}) | {BRAND}"
        top_s = f"The highest T-bill rate this week is the {term} bill at {top['rate_apy']}%"
        if auc:
            top_s += f", the auction high investment rate from the {auc} auction"
        else:
            top_s += ", the auction high investment rate"
        top_s += ". These are auction results, not the Treasury's daily secondary-market yields."
        lead = top_s + " " + intro
        faqs = [("What is the highest T-bill rate this week?", f"{top_s} Updated {UPD_S}.")]
    extra_rel = ""
    if path == "/savings/" and rs:
        def _sv(r):
            _lk = RATE_LOCK.search(r["notes"] or "")
            return f'{r["name"]} at {r["rate_apy"]}% APY' + (f" ({_lk.group(1).lower()} for new users)" if _lk else "")
        _top3 = rs[:3]
        faqs = [
            ("What's the highest high-yield savings rate this week?",
             f"{_sv(_top3[0])}" + (", then " + " and ".join(_sv(r) for r in _top3[1:]) if len(_top3) > 1 else "")
             + f". Updated {UPD_S}. Confirm the current APY on the bank's site before you move money."),
            ("What does Wells Fargo pay on savings today?",
             WF_PROMO_TXT + f" {_top3[0]['name']} pays {_top3[0]['rate_apy']}% APY with no $25,000 minimum."),
            ("Are high-yield savings accounts FDIC-insured?",
             "Every account in the table is a bank deposit, FDIC-insured up to $250,000 per depositor, per bank, per ownership category. "
             "Raisin is a marketplace, not a bank; coverage comes through the partner bank."),
            ("Should I pick a CD instead of a savings account?",
             "If you won't need the money until a set date, a CD fixes the rate for the term. A savings APY is variable and can change any time. "
             "Compare this week's CD APYs at cashratewatch.com/cd-rates/."),
        ]
    if path == "/savings/":
        extra_rel = ('<div class="prose"><p>If you can leave the money until a set date, compare <a href="/cd-rates/">this week\'s CD and fixed-term APYs</a>.</p>'
                     '<p>Related: <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs ongoing rate</a>.</p></div>')
    elif path == "/money-market/":
        extra_rel = ('<div class="prose"><p>Related: <a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a> and the <a href="/best-money-market-funds/">Vanguard vs Schwab vs Fidelity hub</a>.</p></div>')
    elif path == "/treasury-bills/":
        extra_rel = ('<div class="prose"><p>Related: <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a>.</p></div>')
    elif path == "/broker-cash/":
        extra_rel = ('<div class="prose"><p>Related: <a href="/schwab-bank-sweep-vs-swvxx/">Schwab bank sweep vs SWVXX</a> and <a href="/rate-changes/">what moved this week</a>.</p></div>')
    body = (upd(len(rs)) + f'<p class="lead">{lead}</p>' + table(rs) + TABLE_NOTE + (USDC_NOTE if cat == "usdc" else "")
            + cat_chart(cat, CAT_LABEL[cat]) + faq_block(faqs) + extra_rel + RISK_EXPLAINER + NEWSLETTER)
    page(path, page_title, lead if path == "/treasury-bills/" else intro, body, h1=page_h1, faqs=faqs)

# Comparison: USDC vs HYSA
cb, hy = get("Coinbase USDC rewards"), best("hysa")
kr = get("Kraken USDC rewards (flexible)", UNRANKED)  # prose only, never ranked
hys = [r for r in SHOWN if r["category"] == "hysa"][:5]
cmp_rows = [r for r in [cb] if r] + hys
kraken_li = ('<li><b>Kraken:</b> advertises USDC rewards, but the offer is marked "eligible countries only" and is '
             '<b>not confirmed for US users</b>, so we do not rank it. Check Kraken\'s terms for your state before counting on it '
             '(<a href="https://www.kraken.com/learn/what-is-usd-coin-usdc" rel="nofollow noopener">source</a>).</li>') if kr else ""
body = f"""{upd(len(cmp_rows))}<div class="prose">
<p class="lead">USDC rewards on exchanges can look competitive with a savings account. This week's numbers show what you give up for that yield.</p>
<h2>This week's numbers</h2></div>{table(cmp_rows, filters=False)}{TABLE_NOTE}{USDC_NOTE}<div class="prose">
<ul>
<li><b>Coinbase:</b> a {pct("Coinbase USDC rewards")} rewards rate on USDC, but only for <b>Coinbase One</b> members (plans start at $4.99/month, per <a href="https://www.coinbase.com/usdc" rel="nofollow noopener">coinbase.com/usdc</a>). On $10,000 that fee is about $60 a year, which cancels roughly 0.6 percentage points of the rewards rate.</li>
{kraken_li}
<li><b>Best FDIC savings this week:</b> {e(hy["name"]) if hy else "n/a"} at {e(hy["rate_apy"])+"%" if hy else "n/a"} APY, insured up to $250,000, with no subscription.</li>
</ul>
<h2>Risk: insured vs not insured</h2>
<p>A savings account is an FDIC-insured bank deposit. USDC is a token issued by Circle and held at an exchange. It is <b>not</b> FDIC- or SIPC-insured. Your money depends on the exchange staying solvent and the stablecoin keeping its $1 peg. USDC briefly traded well below $1 in March 2023 when some of its reserves were stuck at Silicon Valley Bank.</p>
<h2>Rules are still moving (GENIUS Act)</h2>
<p>The GENIUS Act, enacted July 18, 2025, prohibits stablecoin issuers from paying interest or yield to holders "solely in connection with the holding, use, or retention" of the stablecoin. Exchange rewards like Coinbase's are paid by the exchange, not directly by the issuer. On October 21, 2026 the OCC issued a final rule implementing the act, effective January 18, 2027. The rule drops a proposed presumption against certain third-party arrangements. It says a payment through an intermediary may or may not fall under the ban depending on the facts and circumstances (<a href="https://occ.gov/news-issuances/news-releases/2026/nr-occ-2026-92.html" rel="nofollow noopener">OCC news release</a>; <a href="https://www.occ.gov/news-issuances/bulletins/2026/bulletin-2026-50.html" rel="nofollow noopener">OCC Bulletin 2026-50</a>; <a href="https://www.occ.gov/news-issuances/news-releases/2026/nr-occ-2026-92a.pdf" rel="nofollow noopener">final rule text</a>). Other regulators are still writing their own rules, so USDC reward rates could change or disappear.</p>
<h2>Taxes</h2><p>Savings interest is taxed as ordinary income. Crypto rewards are generally taxable income too, and you track them yourself. T-bill interest, by contrast, is exempt from state and local tax.</p>
<h2>Bottom line</h2><p>For an emergency fund, a top FDIC-insured savings account (or T-bills) gives you nearly the same yield with far less risk. USDC rewards can make sense only for money already on an exchange, if you understand that it is uninsured and that the rules may change.</p>
</div>{NEWSLETTER}"""
page("/usdc-vs-high-yield-savings/", f"USDC rewards vs high-yield savings ({UPD.strftime('%B %Y')}) | {BRAND}",
     "USDC rewards on Coinbase vs FDIC-insured high-yield savings: this week's rates, the Coinbase One fee, insurance, and taxes.",
     body, h1="USDC rewards vs high-yield savings")

# Comparison: best broker cash rates
bc = [r for r in SHOWN if r["category"] == "broker_cash"]
mm = [r for r in SHOWN if r["category"] == "money_market"]
# AI-visibility block (2026-11-12): "broker cash sweep yield comparison" + "best place for cash after selling stocks".
# Every number is pulled from this week's rows; nothing is hard-coded.
bc_cmp, bc_faqs = "", None
_sweep = get("Schwab brokerage uninvested cash (Bank Sweep)")
_fee_re = re.compile(r"subscription|requires gold|paid", re.I)
_bc_free = [r for r in bc if not _fee_re.search(r.get("notes") or "") and r is not _sweep]
if bc and mm:
    _top_bc, _top_mm = bc[0], mm[0]
    _free = _bc_free[0] if _bc_free else None
    _rows = []
    _rows.append(["Top broker cash rate", e(_top_bc["name"]), f'<b>{e(_top_bc["rate_apy"])}%</b>', e(_top_bc["risk"])])
    if _free and _free is not _top_bc:
        _rows.append(["Top broker cash rate with no paid plan", e(_free["name"]), f'<b>{e(_free["rate_apy"])}%</b>', e(_free["risk"])])
    _rows.append(["Top money market fund (7-day yield)", e(_top_mm["name"]), f'<b>{e(_top_mm["rate_apy"])}%</b>', e(_top_mm["risk"])])
    if _sweep:
        _rows.append(["Default sweep example", e(_sweep["name"]), f'<b>{e(_sweep["rate_apy"])}%</b>', e(_sweep["risk"])])
    _gap_line = ""
    if _sweep:
        _gap = _top_mm["rate"] - _sweep["rate"]
        _gap_line = (f'<p>On $10,000 left in a {e(_sweep["rate_apy"])}% default sweep for a year, moving it to the top money market fund '
                     f'at this week\'s yield would earn roughly ${_gap / 100 * 10000:,.0f} more, if the yield held all year (it won\'t exactly; yields move weekly).</p>')
    _top_is_paid = bool(_fee_re.search(_top_bc.get("notes") or ""))
    bc_cmp = (f'<div class="prose"><h2>Broker cash sweep yield comparison</h2>'
              f'<p>The same idle cash can earn very different amounts depending on where your broker parks it. Rates as of {e(UPD_S)}.</p></div>'
              + ('<div class="tw"><table class="kv"><thead><tr><th></th><th>Where</th><th>Rate</th><th>Protection</th></tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in _rows) + '</tbody></table></div>') +
              '<div class="prose">' + _gap_line +
              '<h2>Best place for cash after selling stocks</h2><ul>'
              '<li><b>Leaving it in the account for a while:</b> buy a money market fund at the same broker. You avoid a bank transfer, and you can usually sell it and buy stock again the next trading day.</li>'
              '<li><b>Need it for a near-term bill or emergency fund:</b> an FDIC-insured high-yield savings account is the plainer choice. See <a href="/savings/">this week\'s savings rates</a>.</li>'
              '<li><b>Won\'t need it for a few months:</b> T-bills lock a rate for the term, and the interest is exempt from state and local tax. See <a href="/treasury-bills/">T-bill auction rates</a> and <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a>.</li>'
              '<li><b>Check the default.</b> After a sale the cash usually lands in your broker\'s default sweep or settlement fund. If that pays near zero, it will stay there until you move it.</li>'
              '</ul></div>')
    _a1 = (f'This week the highest broker cash rate we list is {_top_bc["name"]} at {_top_bc["rate_apy"]}%'
           + (" (it requires a paid subscription)" if _top_is_paid else "")
           + (f'; the highest with no paid plan is {_free["name"]} at {_free["rate_apy"]}%' if (_free and _free is not _top_bc) else "")
           + f'. The top money market fund we track, {_top_mm["name"]}, has a {_top_mm["rate_apy"]}% 7-day yield'
           + (f', versus {_sweep["rate_apy"]}% in {_sweep["name"]}' if _sweep else "") + f'. Rates as of {UPD_S}.')
    _a2 = ('If the money stays at your broker, a money market fund there usually pays far more than a default sweep and is easy to move back into stocks. '
           'If you need it soon or want FDIC insurance, use a high-yield savings account; if you can wait a few months, T-bills lock a rate and skip state tax. '
           f'Top money market yield this week: {_top_mm["rate_apy"]}% ({_top_mm["name"]}). Information only, not financial advice.')
    _a3 = ('Broker cash sweep programs place cash at partner banks, so it is usually FDIC-insured up to the limits. '
           'Money market funds are securities: they are covered by SIPC at the broker, not by the FDIC. Check each row\'s protection label.')
    bc_faqs = [("What is the best broker cash sweep yield this week?", _a1),
               ("Where should I put cash after selling stocks?", _a2),
               ("Is broker sweep cash FDIC insured?", _a3)]
body = f"""{upd(len(bc) + len(mm))}<div class="prose">
<p class="lead">Your brokerage may be paying you almost nothing on idle cash. Compare what the major brokers pay on uninvested cash this week with the money market funds you can buy instead.</p>
<h2>Broker cash rates this week</h2></div>{table(bc, filters=False)}<div class="prose">
<ul>
<li><b>Default sweeps can be tiny.</b> Schwab's standard sweep for uninvested brokerage cash pays {pct("Schwab brokerage uninvested cash (Bank Sweep)")} APY (<a href="https://www.schwab.com/cash-investments" rel="nofollow noopener">Schwab</a>). Schwab's own Prime Advantage money fund (SWVXX) yields {pct("Schwab Prime Advantage Money Fund (SWVXX)")}, but you have to buy it yourself.</li>
<li><b>Some top rates cost a subscription.</b> Robinhood's {pct("Robinhood Gold High-Yield Cash")} requires Robinhood Gold ($5/month). Public's base rate is {pct("Public High-Yield Cash Account")}; its higher advertised rate needs a paid membership.</li>
<li><b>Interactive Brokers</b> pays {pct("Interactive Brokers (IBKR Pro) cash interest")}, quoted as an annual rate rather than APY, only on cash above $10,000, and at the full rate only if your account is worth more than $100,000.</li>
<li><b>Fidelity</b> lets you hold uninvested cash in a money market fund as your core position (SPAXX, {pct("Fidelity Government Money Market (SPAXX)")} 7-day yield).</li>
</ul>
<h2>Money market funds you can buy instead</h2></div>{table(mm, filters=False)}{TABLE_NOTE}<div class="prose">
<p>Money market funds are SIPC-covered securities, not FDIC deposits; see the risk labels below. Some funds have minimums (Vanguard's are $3,000, except VMFXX used as the brokerage settlement fund, which has none).</p>
<p>Related: <a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a>.</p></div>{bc_cmp}{faq_block(bc_faqs)}{RISK_EXPLAINER}{NEWSLETTER}"""
page("/best-broker-cash-rates/", f"Best broker cash sweep rates ({UPD.strftime('%B %Y')}) | {BRAND}",
     "What Robinhood, Wealthfront, Betterment, Public, IBKR and Schwab pay on uninvested cash this week, and money market funds that beat a 0.01% default sweep.",
     body, h1="Best broker cash rates", faqs=bc_faqs)

# ---------- comparison pages generated from this week's CSV ----------
def median(xs):
    xs = sorted(xs); n = len(xs)
    return None if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)
def ff(v): return f"{v:.2f}%"
def dollars(pts, amt=10000): return f"${pts / 100 * amt:,.0f}"
def mm_kind(name):
    n = name.lower()
    if "prime" in n: return "Prime"
    if "treasury" in n: return "Treasury"
    if "government" in n or "federal" in n: return "Government"
    return "Other"
def ticker(name):
    m = re.search(r"\(([A-Z]{4,5})\)", name); return m.group(1) if m else name
def kv_table(head, rows):
    return ('<div class="tw"><table class="kv"><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + '</tr></thead><tbody>'
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + '</tbody></table></div>')
MONTH = UPD.strftime('%B %Y')
HY = [r for r in SHOWN if r["category"] == "hysa"]
MM = [r for r in SHOWN if r["category"] == "money_market"]
TB = [r for r in SHOWN if r["category"] == "tbill"]

# 1) Best money market fund yields: Vanguard vs Schwab vs Fidelity
if MM:
    fams = {}
    for r in MM: fams.setdefault(r["name"].split()[0], []).append(r)   # MM is already sorted by yield
    fam_rows = []
    for fam, rs in sorted(fams.items(), key=lambda kv: -kv[1][0]["rate"]):
        t = rs[0]
        mn = e(t["min_deposit"]) if t["min_deposit"].strip().lower() not in ("", "unknown") else "&mdash;"
        fam_rows.append([f"<b>{e(fam)}</b>", f'{e(t["name"])}', f'<b>{e(t["rate_apy"])}%</b>', mn, str(len(rs))])
    hi_, lo_ = MM[0], MM[-1]
    gap = hi_["rate"] - lo_["rate"]
    exp = [(ticker(r["name"]), m.group(1)) for r in MM for m in [re.search(r"expense (\d+(?:\.\d+)?)%", r["notes"])] if m]
    kinds = {}
    for r in MM: kinds.setdefault(mm_kind(r["name"]), []).append(ticker(r["name"]))
    kinds_txt = "; ".join(f"{k}: {', '.join(v)}" for k, v in kinds.items())
    vusxx = next((r for r in MM if ticker(r["name"]) == "VUSXX"), None)
    vmfxx = next((r for r in MM if ticker(r["name"]) == "VMFXX"), None)
    mm_cmp, mm_faqs = "", None
    if vusxx and vmfxx:
        asof_v, asof_m = note_as_of(vusxx), note_as_of(vmfxx)
        if asof_v and asof_m and asof_v == asof_m:
            asof_line = f"7-day yields as of {e(asof_v)}."
            faq_asof = f", both 7-day yields as of {asof_v}"
        elif asof_v or asof_m:
            bits = []
            if asof_v: bits.append(f"VUSXX as of {asof_v}")
            if asof_m: bits.append(f"VMFXX as of {asof_m}")
            asof_line = "7-day yields, " + " and ".join(e(b) for b in bits) + "."
            faq_asof = ", " + " and ".join(bits)
        else:
            asof_line = "7-day yields as of this week."
            faq_asof = ""
        def exp_cell(r):
            x = note_expense(r)
            return f"{e(x)}%" if x else "&mdash;"
        def min_cell(r):
            raw = r["min_deposit"].strip()
            base = e(raw) if raw.lower() not in ("", "unknown") else "&mdash;"
            if ticker(r["name"]) == "VMFXX":
                return f"{base} ($0 as settlement fund)"
            return base
        def tax_cell(r):
            if mm_kind(r["name"]) in ("Treasury", "Government"):
                return "May be exempt from state income tax"
            return "See the fund's annual tax information"
        mm_cmp = f"""<div class="prose"><h2>VUSXX vs VMFXX yield</h2>
<p>{asof_line}</p></div>
{kv_table(["Fund", "7-day yield", "Expense ratio", "Minimum", "State tax"], [
    [f'<b>{e(ticker(vusxx["name"]))}</b><br><small>{e(vusxx["name"])}</small>', f'<b>{e(vusxx["rate_apy"])}%</b>', exp_cell(vusxx), min_cell(vusxx), tax_cell(vusxx)],
    [f'<b>{e(ticker(vmfxx["name"]))}</b><br><small>{e(vmfxx["name"])}</small>', f'<b>{e(vmfxx["rate_apy"])}%</b>', exp_cell(vmfxx), min_cell(vmfxx), tax_cell(vmfxx)],
])}
<p class="foot-n">Part or all of the income from government and Treasury funds may be exempt from state income tax. The rules differ by state, so check the fund's annual tax information.</p>
"""
        mm_faqs = [("What is the VUSXX vs VMFXX yield this week?",
                    f"This week VUSXX yields {vusxx['rate_apy']}% and VMFXX yields {vmfxx['rate_apy']}%{faq_asof}.")]
    body = f"""{upd(len(MM))}<div class="prose">
<p class="lead">The money market funds we track from Vanguard, Schwab and Fidelity, ranked by 7-day yield. Yields come from each fund company's own pages and are updated weekly.</p>
<h2>Highest yield at each fund company</h2></div>
{kv_table(["Fund company", "Highest-yielding fund we track", "7-day yield", "Minimum", "Funds tracked"], fam_rows)}
{mm_cmp}<h2>All money market funds we track</h2>{table([dict(r, min_deposit=r["min_deposit"] + " ($0 as settlement fund)") if ticker(r["name"]) == "VMFXX" else r for r in MM], filters=False)}{TABLE_NOTE}<div class="prose">
<h2>What the gap is worth</h2>
<p>This week the spread between the highest and lowest yield on this list is {gap:.2f} percentage points ({e(ticker(hi_["name"]))} at {e(hi_["rate_apy"])}% vs {e(ticker(lo_["name"]))} at {e(lo_["rate_apy"])}%). On $10,000 held for a year at these yields, that is about {dollars(gap)}. Yields change daily, so the ranking can change too.</p>
<h2>Things to check before you buy</h2><ul>
<li><b>Type of fund.</b> Government and Treasury funds hold US government debt and repurchase agreements backed by it. Prime funds can also hold short-term corporate debt, which usually pays a little more and carries a little more credit risk. On this list: {e(kinds_txt)}.</li>
<li><b>Minimums.</b> {e("; ".join(sorted({f'{r["name"].split()[0]}: {r["min_deposit"]}' for r in MM})))}. Vanguard waives the minimum for VMFXX when it is your brokerage settlement fund.</li>
<li><b>Costs.</b> A 7-day yield is reported after fund expenses.{(" Expense ratios from the fund pages: " + e(", ".join(f"{t} {x}%" for t, x in exp)) + ".") if exp else ""}</li>
<li><b>Where you hold it.</b> Each company's funds are easiest to buy at its own brokerage. Buying another company's fund may cost a transaction fee or may not be possible.</li>
<li><b>State tax.</b> Part or all of the income from government and Treasury funds may be exempt from state income tax. The rules differ by state, so check the fund's annual tax information.</li>
<li><b>Risk.</b> Money market funds are not FDIC-insured. They aim to keep a $1 share price, which is not guaranteed. At a brokerage, SIPC covers missing assets if the broker fails, not a drop in the fund's value.</li></ul>
<p>Compare with <a href="/money-market/">all money market fund yields</a>, with <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings</a>, with <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a>, and with <a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a>. For a rate you can lock, see <a href="/cd-vs-money-market-fund/">CD vs money market fund</a>.</p></div>
{faq_block(mm_faqs)}{related("/best-money-market-funds/")}{RISK_EXPLAINER}{NEWSLETTER}"""
    page("/best-money-market-funds/", f"Best money market fund yields: Vanguard vs Schwab vs Fidelity ({MONTH}) | {BRAND}",
         f"This week's 7-day yields for Vanguard, Schwab and Fidelity money market funds such as {', '.join(ticker(r['name']) for r in MM[:4])}, with minimums and fund types.",
         body, h1="Best money market fund yields: Vanguard vs Schwab vs Fidelity", faqs=mm_faqs)

# 2) T-bills vs high-yield savings
tb13 = get("US T-Bill 13-week")
if TB and HY and tb13:
    hy_top, hy_med = HY[0], median([r["rate"] for r in HY])
    tb_hi = max(TB, key=lambda r: r["rate"]); tb_lo = min(TB, key=lambda r: r["rate"])
    STATE = 5.0
    eq = tb13["rate"] / (1 - STATE / 100)
    if eq > hy_top["rate"]:
        verdict = (f"At this week's rates, the 13-week T-bill's {e(tb13['rate_apy'])}% works out to about {ff(eq)} in a taxable savings account "
                   f"at a {STATE:.0f}% state income tax, above the top savings rate we list ({e(hy_top['rate_apy'])}%).")
    else:
        verdict = (f"At this week's rates, the top savings rate we list ({e(hy_top['rate_apy'])}%) is still at or above the 13-week T-bill "
                   f"({e(tb13['rate_apy'])}%) even after counting a {STATE:.0f}% state income tax.")
    rows = sorted(TB + HY[:5], key=lambda r: -r["rate"])
    tb_wks = sorted({int(m.group(1)) for r in TB for m in [re.search(r"(\d+)-week", r["name"])] if m})
    tb_terms = (", ".join(f"{w}-" for w in tb_wks[:-1]) + f" and {tb_wks[-1]}-week") if len(tb_wks) > 1 else f"{tb_wks[0]}-week"
    tb_list = ", ".join(f'{r["name"].replace("US T-Bill ", "")} {r["rate_apy"]}%' for r in TB)
    tb_vs_faqs = [("T-bill or HYSA: which pays more this week?",
                   f"This week the highest T-bill auction rate we list is the {tbill_term(tb_hi)} bill at {tb_hi['rate_apy']}%, "
                   f"and the top high-yield savings rate we list is {hy_top['name']} at {hy_top['rate_apy']}% APY. Updated {UPD_S}.")]
    body = f"""{upd(len(rows))}<div class="prose">
<p class="lead">Treasury bills vs high-yield savings accounts: both pay close to short-term interest rates. The differences are taxes, access to your money, and how long the rate lasts.</p>
<h2>This week's numbers</h2><ul>
<li><b>T-bills</b> (latest auctions): {e(tb_list)}.</li>
<li><b>Top savings rate we list:</b> {e(hy_top["name"])} at {e(hy_top["rate_apy"])}% APY{(" (" + e(public_note(hy_top)) + ")") if public_note(hy_top) else ""}.</li>
<li><b>Median savings rate we list:</b> {ff(hy_med)} across {len(HY)} accounts.</li></ul></div>
{table(rows, filters=False)}{TABLE_NOTE}<div class="prose">
<h2>State tax can tip the balance</h2>
<p>T-bill interest is taxed federally but is exempt from state and local income tax. Savings interest is taxed at both levels. If you pay {STATE:.0f}% state income tax, the 13-week T-bill at {e(tb13["rate_apy"])}% matches a savings account paying about {ff(eq)}. In a state with no income tax, there is no tax difference. {verdict}</p>
<p class="foot-n">How we get this: T-bill rate &divide; (1 &minus; state tax rate). {STATE:.0f}% is an example rate, not your rate. This leaves out federal tax; if you don't deduct state income tax on your federal return, the break-even savings rate is a little higher.</p>
<h2>How they differ</h2></div>
{kv_table(["", "T-bills", "High-yield savings"], [
 ["<b>Rate</b>", "Fixed until maturity. We show the auction high investment rate (a bond-equivalent yield).", "Variable. The bank can change the APY at any time."],
 ["<b>Terms</b>", f"4, 6, 8, 13, 17, 26 and 52 weeks (not every term is in our table; we list the {tb_terms} bills); can be set to roll over automatically", "No term"],
 ["<b>Access</b>", "Paid at maturity. At a broker you can sell early at the market price. At TreasuryDirect you must hold 45 days before you can move a bill to a broker to sell.", "Withdraw any time; transfers usually take 1 to 3 business days"],
 ["<b>Safety</b>", "Backed by the US government, with no dollar limit", "FDIC-insured up to $250,000 per depositor, per bank, per ownership category"],
 ["<b>Minimum</b>", e(tb13["min_deposit"]), "Often $0; see each account"],
 ["<b>Taxes</b>", "Federal only", "Federal and state"]])}
<div class="prose"><h2>Which fits</h2><ul>
<li>Money you may need any day, such as an emergency fund, is simpler in savings.</li>
<li>Money you can leave for a set number of weeks or months may earn more in T-bills, especially if you pay state income tax. This week the T-bill rates run from {e(tb_lo["rate_apy"])}% ({e(tb_lo["name"].replace("US T-Bill ", ""))}) to {e(tb_hi["rate_apy"])}% ({e(tb_hi["name"].replace("US T-Bill ", ""))}).</li>
<li>Promotional savings rates can end. Check the notes under each account.</li></ul>
<p>See all <a href="/treasury-bills/">T-bill auction rates</a> and <a href="/savings/">savings rates</a>. Related: <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a> and <a href="/high-yield-savings-promo-vs-base-rate/">promo vs ongoing savings rates</a>.</p></div>
{faq_block(tb_vs_faqs)}{related("/t-bills-vs-high-yield-savings/")}{RISK_EXPLAINER}{NEWSLETTER}"""
    page("/t-bills-vs-high-yield-savings/", f"T-bill vs HYSA: this week's rates (updated {UPD_S}) | {BRAND}",
         f"This week's T-bill auction rates vs the top high-yield savings APYs, plus how state tax, access to your money and FDIC insurance compare.",
         body, h1="T-bill vs HYSA", faqs=tb_vs_faqs)

# 3) High-yield savings vs money market funds
if HY and MM:
    hy_top, mm_top = HY[0], MM[0]
    hy_med, mm_med = median([r["rate"] for r in HY]), median([r["rate"] for r in MM])
    diff = mm_med - hy_med
    lead_txt = (f"money market funds are ahead by {abs(diff):.2f} points at the median" if diff > 0.005 else
                f"savings accounts are ahead by {abs(diff):.2f} points at the median" if diff < -0.005 else "the medians are about the same")
    rows = sorted(HY[:5] + MM, key=lambda r: -r["rate"])
    body = f"""{upd(len(rows))}<div class="prose">
<p class="lead">A high-yield savings account is a bank deposit. A money market fund is an investment you buy at a brokerage. Both pay close to short-term interest rates.</p>
<h2>This week's numbers</h2></div>
{kv_table(["", "High-yield savings", "Money market funds"], [
 ["<b>Top rate we list</b>", f'{e(hy_top["rate_apy"])}% APY<br><small>{e(hy_top["name"])}</small>', f'{e(mm_top["rate_apy"])}% 7-day yield<br><small>{e(mm_top["name"])}</small>'],
 ["<b>Median we list</b>", f"{ff(hy_med)} ({len(HY)} accounts)", f"{ff(mm_med)} ({len(MM)} funds)"]])}
<div class="prose"><p>At this week's rates, {lead_txt}. The top savings rates often come with promotions or balance tiers; see the notes in the table.</p></div>
{table(rows, filters=False)}{TABLE_NOTE}<div class="prose">
<h2>How they differ</h2></div>
{kv_table(["", "High-yield savings", "Money market fund"], [
 ["<b>What it is</b>", "A bank deposit", "A mutual fund that holds short-term debt"],
 ["<b>Protection</b>", "FDIC-insured up to $250,000 per depositor, per bank, per ownership category", "Not FDIC-insured. SIPC covers missing assets if the broker fails, not a loss in value. Aims for a $1 share price, not guaranteed."],
 ["<b>Rate shown</b>", "APY, set by the bank", "7-day yield, after fund expenses; moves with market rates"],
 ["<b>Where you hold it</b>", "At the bank", "In a brokerage account"],
 ["<b>Getting money out</b>", "Transfer out any time; usually 1 to 3 business days", "Sell shares, then move cash out of the brokerage account"],
 ["<b>Minimums</b>", "Often $0; see each account", e("; ".join(sorted({f'{r["name"].split()[0]} {r["min_deposit"]}' for r in MM})))],
 ["<b>Taxes</b>", "Interest taxed federally and by your state, if it has an income tax", "Dividends taxed federally; part of the income from government and Treasury funds may be exempt from state tax"]])}
<div class="prose"><h2>Money market account or money market fund?</h2>
<p>The names are close but the products differ. A money market <em>account</em> is a bank deposit, FDIC-insured like savings. A money market <em>fund</em> is a security and is not FDIC-insured. Every money market yield quoted here is for a fund.</p>
<h2>Which fits</h2><ul>
<li>If you want FDIC insurance and no brokerage account, a high-yield savings account is simpler.</li>
<li>If your cash already sits at a brokerage, a money market fund avoids moving it, and often beats the broker's default cash rate. See <a href="/best-broker-cash-rates/">broker cash rates</a>.</li>
<li>For money you won't need for a set period, <a href="/t-bills-vs-high-yield-savings/">T-bills</a> are another option.</li></ul>
<p>See all <a href="/savings/">savings rates</a> and <a href="/best-money-market-funds/">money market fund yields</a>.</p></div>
{related("/high-yield-savings-vs-money-market-fund/")}{RISK_EXPLAINER}{NEWSLETTER}"""
    page("/high-yield-savings-vs-money-market-fund/", f"High-yield savings vs money market funds ({MONTH}) | {BRAND}",
         "This week's top high-yield savings APYs vs money market fund 7-day yields, with how FDIC and SIPC protection, access, minimums and taxes differ.",
         body, h1="High-yield savings vs money market funds")

# 4) High-yield savings: promo rate vs ongoing rate
# Tables and figures are read from the current CSV so weekly rebuilds stay in sync.
def _fmt_mdy(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%b %-d, %Y")
        except ValueError:
            continue
    return s

def _pretty_dates(text):
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", lambda m: _fmt_mdy(m.group(0)), text)
    text = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", lambda m: _fmt_mdy(m.group(0)), text)
    return text

def _issued_matures(row):
    m = re.search(r"issued (\d{4}-\d{2}-\d{2}), matures (\d{4}-\d{2}-\d{2})", row.get("notes") or "")
    if not m:
        return None
    return _fmt_mdy(m.group(1)), _fmt_mdy(m.group(2))

def _get_any(name):
    return get(name) or get(name, ALL)

def _rate_txt(value, suffix="% APY"):
    if value is None:
        return "Unknown"
    if isinstance(value, float):
        s = f"{value:.2f}".rstrip("0").rstrip(".") if value != 0 else "0"
        # keep two decimals for APY-style figures (3.90, 4.30); leave three when needed (rare here)
        if "." in s and len(s.split(".")[1]) == 1:
            s = f"{value:.2f}"
        elif "." not in s:
            s = f"{value:.2f}"
        return f"{s}{suffix}"
    return f"{value}{suffix}"

def _promo_info(row):
    """Classify a savings or broker-cash row for the promo-vs-ongoing table."""
    notes = row.get("notes") or ""
    rate = row["rate"]
    info = {
        "row": row,
        "kind": "ongoing",  # ongoing | timed | qualification | lock | boosts | tiered
        "ongoing": rate,
        "advertised": None,
        "term_months": None,
        "term_days": None,
    }
    m = re.search(r"otherwise (\d+(?:\.\d+)?)%", notes, re.I)
    if m:
        info.update(kind="qualification", advertised=rate, ongoing=float(m.group(1)))
        return info
    m = re.search(r"PROMO:\s*(\d+(?:\.\d+)?)%\s*for\s*(?:up to\s*)?(\d+)\s*months", notes, re.I)
    if m:
        mb = re.search(r"base (?:APY|rate) after promo\s*(\d+(?:\.\d+)?)%", notes, re.I)
        info.update(kind="timed", advertised=float(m.group(1)), ongoing=float(mb.group(1)) if mb else None, term_months=int(m.group(2)))
        return info
    m = re.search(r"(?:boost to|boosted to)\s*(\d+(?:\.\d+)?)%\s*(?:for up to|for)\s*(\d+)\s*months", notes, re.I)
    if m:
        info.update(kind="timed", advertised=float(m.group(1)), ongoing=rate, term_months=int(m.group(2)))
        return info
    if re.search(r"(\d+)-day rate lock", notes, re.I):
        m = re.search(r"(\d+)-day rate lock", notes, re.I)
        info.update(kind="lock", advertised=rate, ongoing=None, term_days=int(m.group(1)))
        return info
    m = re.search(r"up to (\d+(?:\.\d+)?)%.{0,120}limited-time", notes, re.I)
    if m:
        info.update(kind="timed", advertised=float(m.group(1)), ongoing=rate, term_months=None)
        return info
    m = re.search(r"new-customer boost to (\d+(?:\.\d+)?)%", notes, re.I)
    if m:
        info.update(kind="timed", advertised=float(m.group(1)), ongoing=rate, term_months=None)
        return info
    m = re.search(r"up to (\d+(?:\.\d+)?)%\s*with boosts", notes, re.I)
    if m:
        info.update(kind="boosts", advertised=float(m.group(1)), ongoing=rate)
        return info
    m = re.search(r"up to (\d+(?:\.\d+)?)%\s*with qualifications", notes, re.I)
    if m:
        info.update(kind="qualification", advertised=float(m.group(1)), ongoing=rate)
        return info
    if re.search(r"\$[\d,]+\s+for\s+\d+(?:\.\d+)?%", row.get("min_deposit") or ""):
        info.update(kind="tiered", advertised=None, ongoing=rate)
        return info
    return info
    return info

def _promo_ongoing_cell(info):
    row, kind = info["row"], info["kind"]
    if kind == "lock":
        days = info.get("term_days") or 90
        return f"After the lock, the rate can change. The provider doesn't list a separate APY for after day {days}."
    if info["ongoing"] is None:
        return "Unknown"
    base = f'{info["ongoing"]:.2f}%'
    if kind == "qualification" and info["advertised"] is not None and info["advertised"] != info["ongoing"]:
        # Happen-style: ongoing is the rate if the condition is missed
        if re.search(r"Standard Rate", row.get("notes") or "", re.I):
            return f"{base} Standard Rate if the deposit condition is not met"
        if re.search(r"with qualifications", row.get("notes") or "", re.I):
            return f"{base} APY base"
        return f"{base} APY if the condition is not met"
    if kind in ("timed", "boosts") and info["advertised"] is not None:
        if re.search(r"\bBase APY\b", row.get("notes") or "", re.I):
            asof = note_as_of(row)
            extra = f", as of {_fmt_mdy(asof)}" if asof else ""
            return f"{base} APY base{extra}"
        return f"{base} APY"
    if kind == "tiered":
        m = re.search(r"(\$[\d,]+)\s+for\s+(\d+(?:\.\d+)?)%", row.get("min_deposit") or "")
        if m:
            return f"{m.group(2)}% APY at the {m.group(1)} tier"
        return f"{base} APY"
    return f"{base} APY"

def _promo_advertised_cell(info):
    kind, row = info["kind"], info["row"]
    adv = info["advertised"]
    if kind == "tiered" or (kind == "ongoing" and adv is None):
        return "Tiered rate, not a time-limited promo" if kind == "tiered" else "—"
    if adv is None:
        return "—"
    s = f"{adv:.2f}%"
    if kind == "qualification" and re.search(r"LevelUp Rate", row.get("notes") or "", re.I):
        return f"{s} LevelUp Rate"
    if kind == "qualification" and re.search(r"with qualifications", row.get("notes") or "", re.I):
        return f"Up to {s} with qualifications"
    if kind == "boosts":
        return f"Up to {s} with boosts"
    if kind == "timed":
        if info["term_months"]:
            prep = "for up to" if re.search(r"for up to", row.get("notes") or "", re.I) else "for"
            if re.search(r"new-customer", row.get("notes") or "", re.I):
                return f"{s} advertised for new customers"
            return f"{s} APY {prep} {info['term_months']} months"
        if re.search(r"new-customer", row.get("notes") or "", re.I):
            return f"{s} advertised for new customers"
        return f"Up to {s}"
    if kind == "lock":
        return f"{s} APY"
    return f"{s} APY"

def _promo_blend_cell(info):
    kind = info["kind"]
    if kind == "qualification":
        return "Not a timed promo, so no 6-month blend." if not re.search(r"with qualifications", info["row"].get("notes") or "", re.I) else "Not a simple timed promo."
    if kind == "lock":
        return "Not shown: no post-lock APY published."
    if kind == "boosts":
        return "Not shown: boost terms unclear."
    if kind == "timed" and info["ongoing"] is None:
        return "Not shown: ongoing rate unknown."
    if kind == "timed" and info["term_months"] is None:
        return "Not shown: boost length unknown."
    if kind == "timed" and info["ongoing"] is not None and info["advertised"] is not None and info["term_months"]:
        term, ong, adv = info["term_months"], info["ongoing"], info["advertised"]
        blend = (adv * term + ong * (12 - term)) / 12
        if term == 6:
            return (f"About {blend:.2f}%, if the boost lasts 6 months and the {ong:.2f}% rate holds for the next 6 months. "
                    f"({ong:.2f} + {adv:.2f}) / 2.")
        return (f"About {blend:.2f}%, weighting {term} months at {adv:.2f}% and {12 - term} months at {ong:.2f}%.")
    return "—"

def _promo_conditions(info):
    row = info["row"]
    raw = row.get("notes") or ""
    bits = []
    pub = _pretty_dates(public_note(row))
    if pub:
        bits.append(pub)
    if re.search(r"HTTP 404|404 status", raw, re.I):
        bits.append("The URL returned an HTTP 404 status while still showing the offer, so the address should be confirmed.")
    fet = (row.get("fetched_at") or "")[:10]
    if fet and fet < UPD_ISO and re.search(r"JS-injected|WebFetch", raw):
        asof = note_as_of(row)
        if not asof:
            m = re.search(r"as of (\d{4}-\d{2}-\d{2})", raw)
            asof = m.group(1) if m else None
        when = _fmt_mdy(asof) if asof else _fmt_mdy(fet)
        bits.append(f"This figure is from {when}; confirm the current rate on the provider's page.")
    m = re.search(r"accurate as of (\d{4}-\d{2}-\d{2})", raw)
    if m:
        bits.append(f"The provider says this APY was accurate as of {_fmt_mdy(m.group(1))}, so check for a newer rate.")
    if info["kind"] == "timed" and info["term_months"] is None and info["advertised"] is not None:
        if "boost length unknown" not in " ".join(bits).lower() and not re.search(r"as of", pub or ""):
            bits.append("Check the provider's page for when the boost ends and any other conditions.")
    if info["kind"] == "boosts":
        bits.append("Check whether each boost is a time-limited promo or a referral or deposit bonus.")
    if info["kind"] == "qualification" and re.search(r"with qualifications", raw) and "qualification rules" not in (pub or "").lower():
        bits.append("Check the provider's page for the exact qualification rules.")
    if row["category"] == "broker_cash":
        bits.append("This is a broker cash program, not a bank savings account." if "Betterment" in row["name"] else "Broker cash program.")
    cleaned = []
    for b in bits:
        b = b.strip()
        if not b:
            continue
        if cleaned and not cleaned[-1].endswith((".", ";", ":", "—")):
            cleaned[-1] = cleaned[-1] + "."
        cleaned.append(b)
    return " ".join(cleaned) if cleaned else "—"

_PROMO_PREFERRED = [
    "Western Alliance Bank High-Yield Savings via Raisin",
    "Happen Bank (formerly LendingClub) LevelUp Savings",
    "Bask Bank Savings",
    "SoFi Savings",
    "E*TRADE Premium Savings",
    "Betterment Cash Reserve",
    "Wealthfront Cash Account",
    "Varo Savings",
    "CIT Bank Platinum Savings",
    "EverBank Performance Savings",
    "Barclays Tiered Savings",
    "Marcus by Goldman Sachs Online Savings",
    "American Express High Yield Savings",
    "Ally Online Savings",
]
_BOOSTY = re.compile(r"\b(boosts?|promo|promotional|new-customer|rate lock)\b", re.I)

def _promo_candidates():
    names = {r["name"] for r in SHOWN if r["category"] == "hysa"}
    # Include a verified promo even if the live site hides the row (currently E*TRADE's URL 404s).
    for r in ALL:
        if r["name"] in EXCLUDE and r["rate"] is not None and r["category"] == "hysa":
            names.add(r["name"])
    for r in SHOWN:
        if r["category"] == "broker_cash" and _BOOSTY.search(r.get("notes") or ""):
            names.add(r["name"])
    rows = []
    for name in names:
        r = _get_any(name)
        if r and r["rate"] is not None:
            rows.append(_promo_info(r))
    def sk(info):
        name = info["row"]["name"]
        try:
            return (0, _PROMO_PREFERRED.index(name))
        except ValueError:
            group = 0 if info["kind"] in ("lock", "timed", "qualification", "boosts") else (1 if info["kind"] == "tiered" else 2)
            key_rate = info["advertised"] if info["advertised"] is not None else (info["ongoing"] or 0)
            return (1, group, -key_rate, name)
    rows.sort(key=sk)
    return rows

if HY:
    promo_infos = _promo_candidates()
    promo_tr = []
    for info in promo_infos:
        r = info["row"]
        src = human_source(r["source_url"], r["category"])
        src_td = f'<a href="{e(src)}" rel="nofollow noopener" target="_blank">source</a>' if src else "&mdash;"
        cls = "b-" + re.sub(r"[^A-Za-z]+", "-", r["risk"]).strip("-")
        promo_tr.append(
            "<tr>"
            f'<td>{e(r["name"])}</td>'
            f"<td>{e(_promo_ongoing_cell(info))}</td>"
            f"<td>{e(_promo_advertised_cell(info))}</td>"
            f"<td>{e(_promo_conditions(info))}</td>"
            f"<td>{e(_promo_blend_cell(info))}</td>"
            f'<td><span class="b {cls}">{e(r["risk"])}</span></td>'
            f'<td class="hide-sm">{src_td}</td>'
            "</tr>"
        )
    promo_table = (
        '<div class="tw"><table><thead><tr>'
        "<th>Name</th><th>Ongoing rate</th><th>Advertised or boosted rate</th>"
        "<th>Conditions</th>"
        "<th>First-year blend (only if both ends and a term are known)</th>"
        "<th>Risk label</th><th class=\"hide-sm\">Source</th>"
        "</tr></thead><tbody>"
        + "".join(promo_tr) +
        "</tbody></table></div>"
    )
    # Worked example: first timed promo that has both ends and a term (Bask this week).
    example = next((i for i in promo_infos if i["kind"] == "timed" and i["ongoing"] is not None
                    and i["advertised"] is not None and i["term_months"]), None)
    straight = next((i for i in promo_infos if i["kind"] == "ongoing" and i["row"]["name"].startswith("EverBank")), None)
    if straight is None:
        straight = next((i for i in promo_infos if i["kind"] == "ongoing"), None)
    if example:
        ex_name = example["row"]["name"]
        ong, adv, term = example["ongoing"], example["advertised"], example["term_months"]
        blend = (adv * term + ong * (12 - term)) / 12
        year_int = blend / 100 * 10000
        example_html = f'''<h2>A worked example</h2>
<p>Take {e(ex_name)}: {adv:.2f}% for up to {term} months, then {ong:.2f}% if the listed base rate is still {ong:.2f}%.</p>
<p>If you kept $10,000 in the account for 12 months, and both rates held as written, the first-year blend of {blend:.2f}% would be about ${year_int:,.0f} of interest before tax ({blend / 100:.3f} &times; $10,000). Six months at {adv:.2f}% and six months at {ong:.2f}%, with simple weighting and no compounding, is the same {blend:.2f}%. Compounding, a cap on the boosted balance, or a change in the base rate would change the result.</p>'''
        if straight and term == 6:
            st_rate = straight["ongoing"]
            st_name = straight["row"]["name"]
            example_html += (
                f'<p>By comparison, {e(st_name)} is listed at {st_rate:.2f}% APY with no promo. '
                f'That {st_rate:.2f}% is already above {e(ex_name.split()[0])}\'s {blend:.2f}% blend, and it is below '
                f"{e(ex_name.split()[0])}'s {adv:.2f}% headline. Which figure you compare depends on how long you expect to keep the money there, "
                f"and on whether you will open the bundled checking account {e(ex_name.split()[0])} requires for the boost.</p>"
                if "Bask" in ex_name else
                f'<p>By comparison, {e(st_name)} is listed at {st_rate:.2f}% APY with no promo. '
                f"Which figure you compare depends on how long you expect to keep the money there.</p>"
            )
        example_html += "<p>This is an illustration using this week's figures, not a recommendation of either account.</p>"
    else:
        example_html = ""
    hidden_hysa = [r["name"] for r in ALL if r["category"] == "hysa" and (r["rate"] is None) and r["name"] not in EXCLUDE]
    hidden_txt = ""
    if hidden_hysa:
        hidden_txt = (f'<p>No current APY for {e(", ".join(hidden_hysa))} as of {UPD_S}. Check the bank\'s own page for its rate.</p>')
    if example and straight:
        _p_lead = (f"{e(example['row']['name'])} advertises {example['advertised']:.2f}% APY for up to {example['term_months']} months, then pays {example['ongoing']:.2f}%. "
                   f"{e(straight['row']['name'])} pays {straight['ongoing']:.2f}% with no boost. A headline APY often lasts a few months or needs a condition; "
                   "the rate you keep afterward is the one to compare for money you'll leave alone.")
    else:
        _p_lead = ("Banks often advertise a higher APY that lasts a few months, or that you only get if you meet a condition. "
                   "The rate that remains after the boost ends, or if you miss the condition, can be lower. Compare the rate you keep afterward.")
    _p_faqs = []
    _timed = [i for i in promo_infos if i["kind"] == "timed" and i["ongoing"] is not None and i["advertised"] is not None and i["term_months"]]
    if _timed:
        _p_faqs.append(("What happens when a savings promo rate ends?",
            "The account drops to its ongoing rate, and that rate can change too. "
            + " ".join(f"{i['row']['name']}: {i['advertised']:.2f}% for up to {i['term_months']} months, then {i['ongoing']:.2f}%." for i in _timed[:3])
            + f" Figures as of {UPD_S}."))
    if example and straight:
        _bl = (example["advertised"] * example["term_months"] + example["ongoing"] * (12 - example["term_months"])) / 12
        _p_faqs.append(("Is a promo rate better than a higher ongoing rate?",
            f"Only if you'll move the money when the boost ends. Over 12 months, {example['row']['name']} works out to about {_bl:.2f}% "
            f"({example['advertised']:.2f}% for {example['term_months']} months, then {example['ongoing']:.2f}%), while {straight['row']['name']} pays "
            f"{straight['ongoing']:.2f}% the whole year, if both rates hold."))
    _qual = next((i for i in promo_infos if i["kind"] == "qualification" and i["advertised"] is not None and i["ongoing"] is not None and i["advertised"] != i["ongoing"]), None)
    if _qual:
        _p_faqs.append(("What's the difference between a promo rate and a qualification rate?",
            "A promo ends on a date. A qualification rate lasts as long as you meet a condition, such as a monthly deposit or direct deposit. "
            f"{_qual['row']['name']} pays {_qual['advertised']:.2f}% when you meet its condition and {_qual['ongoing']:.2f}% when you don't."))
    _p_faqs.append(("Do big banks run savings promos too?", WF_PROMO_TXT))
    body = f'''{upd(len(promo_infos))}<div class="prose">
<p class="lead">{_p_lead}</p>
<p>Rates change. Confirm the current APY, the length of any boost, and the conditions on the provider's site before you move money. Not financial advice.</p>
<h2>Why the two numbers are not interchangeable</h2>
<p>A promotional APY is usually limited by time, by a new-customer rule, by a required deposit, or by a bundled checking account. An ongoing APY is what the account lists after that condition no longer applies. Comparing a six-month promo at one bank with an ongoing rate at another mixes two different offers.</p>
<p>Where both rates and the boost length are published, the table also shows a simple first-year blend: half a year at the promo rate and half a year at the ongoing rate. That math assumes the ongoing rate does not change, the boost lasts the stated term, and there is no balance cap. Those assumptions often fail, so the blend is an illustration, not a forecast.</p>
<p>If a provider doesn't publish an ongoing rate, none is shown.</p>
<h2>Promo and ongoing rates this week</h2>
<p>Savings rows show APY. Broker-cash rows appear when the account has a boost or a qualification rate. A dash means the figure isn't available.</p></div>
{promo_table}
<div class="prose">{hidden_txt}
{example_html}
<h2>How to read the table</h2>
<ul>
<li>Boosted and qualification offers sit together at the top, then accounts with one ongoing rate. The headline promo doesn't set the order.</li>
<li>A qualification rate (Happen Bank's deposit rule, Varo's requirements, SoFi's direct deposit) isn't the same as a time-limited promo.</li>
<li>If a provider doesn't publish a post-promo APY, the cell says Unknown. Ask the bank before you open the account.</li>
<li>Raisin is a marketplace, not a bank. FDIC insurance, when it applies, is through the partner bank, up to applicable limits.</li>
</ul>
<p>See every savings rate we currently list at <a href="/savings/">cashratewatch.com/savings</a>. Broker cash programs are at <a href="/broker-cash/">cashratewatch.com/broker-cash</a>.</p>
</div>
{faq_block(_p_faqs)}{related("/high-yield-savings-promo-vs-base-rate/")}{RISK_EXPLAINER}{NEWSLETTER}'''
    page("/high-yield-savings-promo-vs-base-rate/",
         f"High-yield savings: promo vs ongoing rate (updated {UPD_S}) | {BRAND}",
         "This week's high-yield savings promo or boosted APYs vs the ongoing rate after the boost, with a first-year blend when both ends are known.",
         body, h1="High-yield savings: promo rate vs ongoing rate", faqs=_p_faqs)

# 5) T-bills vs money market funds
if TB and MM:
    tb_hi = max(TB, key=lambda r: r["rate"])
    mm_hi = MM[0]
    tb52 = get("US T-Bill 52-week")
    tb26 = get("US T-Bill 26-week")
    tb17 = get("US T-Bill 17-week")
    tb13 = get("US T-Bill 13-week")
    tb8 = get("US T-Bill 8-week")
    tb4 = get("US T-Bill 4-week")
    vusxx = next((r for r in MM if ticker(r["name"]) == "VUSXX"), None)
    vmfxx = next((r for r in MM if ticker(r["name"]) == "VMFXX"), None)
    mm_med = median([r["rate"] for r in MM])
    # Midpoint description when the median falls between two funds
    mm_sorted = sorted(MM, key=lambda r: r["rate"])
    nmm = len(mm_sorted)
    if nmm % 2 == 0:
        lo_m, hi_m = mm_sorted[nmm // 2 - 1], mm_sorted[nmm // 2]
        med_note = f"the midpoint of {e(ticker(hi_m['name']))} {e(hi_m['rate_apy'])}% and {e(ticker(lo_m['name']))} {e(lo_m['rate_apy'])}%"
    else:
        mid = mm_sorted[nmm // 2]
        med_note = f"{e(ticker(mid['name']))} at {e(mid['rate_apy'])}%"
    def _tb_item(row):
        if not row:
            return ""
        term = e(tbill_term(row))
        auc = auction_on(row)
        bits = f"{term}: {e(row['rate_apy'])}%"
        detail = []
        if auc:
            detail.append(f"auction {e(auc)}")
        im = _issued_matures(row)
        if im and "52-week" in row["name"]:
            detail.append(f"issued {e(im[0])}, matures {e(im[1])}")
        if detail:
            bits += f" ({'; '.join(detail)})"
        return bits
    tb_items = [_tb_item(r) for r in sorted(TB, key=lambda r: -r["rate"])]
    def _mm_item(r):
        mn_raw = r["min_deposit"].strip()
        mn = e(mn_raw) if mn_raw.lower() not in ("", "unknown") else "minimum not listed"
        extra = ""
        if ticker(r["name"]) == "VMFXX":
            extra = " (Vanguard waives the minimum when VMFXX is the brokerage settlement fund)"
        asofs = []
        # as-of is in notes as 11/09/2026 style; shown once in the heading
        return f'{e(r["name"])}: {e(r["rate_apy"])}%, {mn} minimum{extra}, {e(r["risk"])}'
    mm_asof = note_as_of(MM[0]) or ""
    # note_as_of looks for 11/09/2026; MMF notes use "as of 11/09/2026"
    mm_asof_s = _fmt_mdy(mm_asof) if mm_asof else UPD_S
    # Fallback: also try ISO-ish in notes "as of 11/09/2026" already handled. Some notes: "as of 11/09/2026"
    lead_tb = tb52 or tb_hi
    lead_mm = vusxx or mm_hi
    lead_auc = auction_on(lead_tb) or "the latest auction we list"
    # 2026-11-23 refresh: lead with the most recent short auctions (13- and 26-week) before the 52-week figure.
    _sa = [r for r in (tb26, tb13) if r and auction_on(r) and r is not lead_tb]
    short_auc = ""
    if _sa:
        if len(_sa) == 2 and auction_on(_sa[0]) == auction_on(_sa[1]):
            short_auc = (f"at the {e(auction_on(_sa[0]))} auction, the {e(tbill_term(_sa[0]))} bill paid {e(_sa[0]['rate_apy'])}% "
                         f"and the {e(tbill_term(_sa[1]))} bill paid {e(_sa[1]['rate_apy'])}%. Further out, ")
        else:
            short_auc = ("; ".join(f"the {e(tbill_term(r))} bill paid {e(r['rate_apy'])}% at the {e(auction_on(r))} auction" for r in _sa)
                         + ". Further out, ")
    _tmm = [t for t in ("VUSXX", "FZFXX", "SNSXX") if any(ticker(r["name"]) == t for r in MM)]
    tmm_line = (f" {', '.join(_tmm[:-1]) + ' and ' + _tmm[-1] if len(_tmm) > 1 else _tmm[0]} {'are Treasury money market funds' if len(_tmm) > 1 else 'is a Treasury money market fund'}, which invest mainly in US Treasury debt." if _tmm else "")
    tbmm_faqs = [
        ("Why isn't a T-bill auction rate the same number as a money market fund's 7-day yield?",
         "A T-bill auction rate is the bond-equivalent yield you lock in if you buy at that auction and hold the bill to maturity. A fund's 7-day yield is its last seven days of income, annualized after fund expenses, and it can change daily."),
        ("Are money market funds FDIC-insured?",
         "No. A money market fund is a security, not a bank deposit. SIPC covers missing assets if the broker fails (up to $500,000, including $250,000 cash), but it doesn't cover a drop in the fund's value."),
        ("What happens if I sell a T-bill before it matures?",
         "At a broker you can sell early at the market price, which can be a little above or below what you paid. At TreasuryDirect you must hold a bill 45 days before you can transfer it to a broker to sell."),
        ("What is a Treasury money market fund, and how is it different from buying T-bills?",
         "It's a mutual fund that invests mainly in Treasury bills and other US Treasury debt, such as VUSXX, FZFXX or SNSXX. Its yield changes daily and you can usually sell on any business day. A T-bill locks the auction rate until it matures, and its interest is exempt from state and local income tax. Part or all of a Treasury fund's income may also be state-tax exempt, depending on the fund and your state."),
    ]
    short_cmp = ""
    if tb4 and lead_mm:
        if tb4["rate"] < lead_mm["rate"]:
            short_cmp = (f"The 4-week bill at {e(tb4['rate_apy'])}% is slightly below {e(ticker(lead_mm['name']))}'s {e(lead_mm['rate_apy'])}%, "
                         "so the shortest bill is not ahead of the top fund this week on the raw number.")
        elif tb4["rate"] > lead_mm["rate"]:
            short_cmp = (f"The 4-week bill at {e(tb4['rate_apy'])}% is slightly above {e(ticker(lead_mm['name']))}'s {e(lead_mm['rate_apy'])}%, "
                         "so even the shortest bill is ahead of the top fund this week on the raw number.")
        else:
            short_cmp = (f"The 4-week bill at {e(tb4['rate_apy'])}% matches {e(ticker(lead_mm['name']))}'s {e(lead_mm['rate_apy'])}% "
                         "this week on the raw number.")
    STATE = 5.0
    tax_lines = []
    if tb13:
        eq13 = tb13["rate"] / (1 - STATE / 100)
        tax_lines.append(f"If you pay {STATE:.0f}% state income tax, this week's 13-week bill at {e(tb13['rate_apy'])}% matches a fully taxable yield of about {ff(eq13)}.")
    if tb52:
        eq52 = tb52["rate"] / (1 - STATE / 100)
        tax_lines.append(f"This week's 52-week bill at {e(tb52['rate_apy'])}% matches about {ff(eq52)}.")
    have_wks = sorted({int(m.group(1)) for r in TB for m in [re.search(r"(\d+)-week", r["name"])] if m})
    standard_wks = [4, 6, 8, 13, 17, 26, 52]
    missing_wks = [w for w in standard_wks if w not in have_wks]
    listed_txt = ", ".join(f"{w}" for w in have_wks[:-1]) + (f" and {have_wks[-1]}" if len(have_wks) > 1 else str(have_wks[0]) if have_wks else "")
    terms_note = f"4, 6, 8, 13, 17, 26 and 52 weeks (our table lists {listed_txt}"
    if missing_wks:
        miss = ", ".join(f"{w}-week" for w in missing_wks)
        terms_note += f"; the {miss} bill is not in this week's data" if len(missing_wks) == 1 else f"; the {miss} bills are not in this week's data"
    terms_note += "). You can set bills to roll over."
    mm_for_table = [dict(r, min_deposit=r["min_deposit"] + " ($0 as settlement fund)") if ticker(r["name"]) == "VMFXX" else r for r in MM]
    cmp_rows = sorted(TB + mm_for_table, key=lambda r: -r["rate"])
    mm_asof_heading = f"7-day SEC yield as of {e(mm_asof_s)}"
    body = f'''{upd(len(cmp_rows))}<div class="prose">
<p class="lead">Treasury bills and money market funds are both ways to keep cash in short-term government or government-backed debt, or in a fund that holds that kind of debt. The differences are how long your money is committed, how you are taxed, and what the risk label means.</p>
<p>This week's numbers: {short_auc}the latest {e(tbill_term(lead_tb))} T-bill auction paid {e(lead_tb["rate_apy"])}% (bond-equivalent, auction {e(lead_auc) if auction_on(lead_tb) else e(str(lead_auc))}). The highest money market fund we list is {e(lead_mm["name"])} at {e(lead_mm["rate_apy"])}% ({mm_asof_heading}). Those figures aren't directly comparable. One is an auction rate for a bill you hold to a date; the other is a recent annualized fund yield that can move.</p>
<p>Rates change. Confirm current yields, minimums and fund documents with the provider, or the latest Treasury auction results, before you move money. Not financial advice.</p>
<h2>This week's numbers</h2>
<p><b>T-bills</b> (latest auctions we list; high investment rate, which is a bond-equivalent yield):</p>
<ul>{"".join(f"<li>{item}</li>" for item in tb_items if item)}</ul>
<p><b>Money market funds</b> we list ({mm_asof_heading}).{tmm_line}</p>
<ul>{"".join(f"<li>{_mm_item(r)}</li>" for r in MM)}</ul>
<p>The median 7-day yield across those {len(MM)} funds is {ff(mm_med)} ({med_note}).</p>
<p>T-bills in our table start at $100. Fund minimums vary, and some settlement-fund uses have no minimum.</p></div>
{table(cmp_rows, filters=False)}{TABLE_NOTE}<div class="prose">
<h2>How they differ</h2></div>
{kv_table(["", "T-bills", "Money market funds"], [
 ["<b>What it is</b>", "A short-term loan you make to the US Treasury, bought at auction or in the secondary market.", "A mutual fund that holds short-term debt, such as Treasury bills, government securities, or (in a prime fund) other short-term instruments."],
 ["<b>Rate</b>", "Fixed until maturity. We show the auction high investment rate (bond-equivalent).", "Variable. We show the 7-day SEC yield, which is reported after fund expenses and can change daily."],
 ["<b>Terms</b>", terms_note, "No term. You can typically sell on a business day."],
 ["<b>Access</b>", "Paid at maturity. At a broker you can sell early at the market price, which can be a little above or below what you paid. At TreasuryDirect you must hold 45 days before you can move a bill to a broker to sell. A 4-week bill matures before 45 days, so in practice you cannot transfer it.", "Sold through a brokerage. Redemption is usually next business day; some brokers treat a government or Treasury fund as a settlement fund and make the cash easier to reuse."],
 ["<b>Safety</b>", "A direct US Treasury obligation. There is no FDIC or SIPC insurance on the bill itself, and none is needed in the usual sense: you are paid by the US Treasury. If you sell before maturity, the price can move a little.", "Not FDIC-insured. SIPC covers missing assets if the broker fails (up to $500,000, including $250,000 cash). It does not cover a drop in the fund's value. Funds aim for a stable $1 share price, which is not guaranteed. SWVXX is a prime fund and can hold short-term corporate debt; the Treasury and government funds are narrower."],
 ["<b>Minimum</b>", "$100 at TreasuryDirect, in $100 increments.", "$0 at Fidelity and Schwab in our table. Vanguard lists $3,000, except VMFXX used as the brokerage settlement fund, which has no minimum."],
 ["<b>Taxes</b>", "Interest is taxed federally and is exempt from state and local income tax.", "Dividends are taxed federally. Part or all of the income from government and Treasury funds may be exempt from state income tax. The rules differ by state, so check the fund's annual tax information. We do not yet have a 2026 breakdown for these funds."],
])}
<div class="prose"><p class="foot-n">Sources for the tax and transfer rows: <a href="https://www.irs.gov/taxtopics/tc403" rel="nofollow noopener">IRS Topic 403</a>; <a href="https://www.treasurydirect.gov/marketable-securities/selling-marketable-securities/" rel="nofollow noopener">TreasuryDirect, selling marketable securities</a>; 31 CFR 363.203; <a href="https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/updated-12" rel="nofollow noopener">Investor.gov money market fund bulletin</a>; <a href="https://www.sipc.org/for-investors/what-sipc-protects" rel="nofollow noopener">SIPC</a>. Fund-level state-tax percentages vary by year; do not assume last year's share still applies.</p>
<h2>State tax can tip the balance</h2>
<p>T-bill interest is exempt from state and local income tax. A simple way to compare a T-bill with a fully taxable yield is: T-bill rate &divide; (1 &minus; state tax rate). {" ".join(tax_lines)}</p>
<p>{STATE:.0f}% is an example, not your rate. This leaves out federal tax; if you do not deduct state income tax on your federal return, the break-even taxable yield is a little higher.</p>
<p>A Treasury money market fund may also get a partial or full state exemption, which narrows that gap. The share that qualifies is published after the year ends and differs by fund and by state, so we do not apply a fund-level tax adjustment here.</p>
<p>In a state with no income tax, there is no state-tax difference, and you are left comparing the raw yields, the term, and access to the cash.</p>
<h2>Which fits</h2>
<ul>
<li>Cash you may need on any business day is simpler in a money market fund, especially if it is already at that brokerage as a settlement fund.</li>
<li>Cash you can leave until a known date may earn more in a T-bill at this week's auctions, particularly if you pay state income tax. {short_cmp}</li>
<li>Prime funds such as SWVXX can pay more than some Treasury funds and carry a little more credit risk. Treasury and government funds are narrower.</li>
<li>If your cash already sits at Fidelity, Schwab or Vanguard, buying that broker's fund avoids a transfer. Buying a T-bill there is also possible; TreasuryDirect is not the only way.</li>
</ul>
<p>See all <a href="/treasury-bills/">T-bill auction rates</a> and <a href="/money-market/">money market fund yields</a>. Related comparisons: <a href="/t-bills-vs-high-yield-savings/">T-bills vs high-yield savings</a> and <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market funds</a>. Comparing a T-bill ETF with a fund? See <a href="/sgov-vs-money-market-funds/">SGOV vs money market funds</a>.</p>
</div>
{faq_block(tbmm_faqs)}{related("/t-bills-vs-money-market-funds/")}{RISK_EXPLAINER}{NEWSLETTER}'''
    page("/t-bills-vs-money-market-funds/",
         f"T-bills vs money market funds: this week's rates (updated {UPD_S}) | {BRAND}",
         "This week's T-bill auction rates vs money market fund 7-day yields, including VUSXX and VMFXX, plus how term, access, safety and state tax compare.",
         body, h1="T-bills vs money market funds", faqs=tbmm_faqs)

# 5) SPAXX vs VMFXX vs SWVXX — ticker head-to-head; /best-money-market-funds/ stays the hub.
# Table and figures come from this week's CSV so the Tuesday refresh updates them.
# Two [verify] claims from the draft were cut (prospectus "usually pay more" wording, and a
# state-tax sentence that still needed fund-company citations). The tax note below matches
# the general wording already used on /best-money-market-funds/.
_core = ("SPAXX", "VMFXX", "SWVXX")
_tickers = ("SPAXX", "VMFXX", "SWVXX", "VUSXX", "FZFXX")
_by_t = {ticker(r["name"]): r for r in MM if ticker(r["name"]) in _tickers}
if all(t in _by_t for t in _core):
    def _broker(r):
        return r["name"].split()[0]
    def _exp_txt(r):
        m = re.search(r"expense (\d+(?:\.\d+)?)%(?:\s*\(([^)]+)\))?", r.get("notes") or "")
        if not m:
            return "see fund page"
        extra = f" ({m.group(2)})" if m.group(2) else ""
        return e(m.group(1) + "%" + extra)
    def _min_txt(r):
        raw = r["min_deposit"].strip()
        base = e(raw) if raw.lower() not in ("", "unknown") else "&mdash;"
        if ticker(r["name"]) == "VMFXX":
            return f"{base} (none as the settlement fund)"
        return base
    def _yield_cell(r):
        y = f'<b>{e(r["rate_apy"])}%</b>'
        if re.search(r"waiver", r.get("notes") or "", re.I):
            y += " (with fee waivers)"
        return y
    def _asof_pretty(r):
        raw = note_as_of(r)
        return _fmt_mdy(raw) if raw else None
    ranked = sorted((_by_t[t] for t in _tickers if t in _by_t), key=lambda r: -r["rate"])
    asofs = [_asof_pretty(r) for r in ranked]
    uniq = list(dict.fromkeys(a for a in asofs if a))
    if len(uniq) == 1:
        asof_phrase = f"7-day yields as of {e(uniq[0])}"
        faq_asof = f" (7-day yields as of {uniq[0]})"
    elif uniq:
        asof_phrase = "7-day yields, " + " and ".join(
            f"{e(ticker(r['name']))} as of {e(a)}" for r, a in zip(ranked, asofs) if a)
        faq_asof = ""
    else:
        asof_phrase = "7-day yields as of this week"
        faq_asof = ""
    srcs = []
    seen_src = set()
    for r in ranked:
        url = (r.get("source_url") or "").strip()
        if not url or url in seen_src:
            continue
        seen_src.add(url)
        srcs.append(f'<a href="{e(url)}" rel="nofollow noopener">{e(_broker(r) + " " + ticker(r["name"]))}</a>')
    src_line = (", ".join(srcs) + ".") if srcs else "each fund company's page."
    tbl = kv_table(
        ["Fund", "Broker", "Type", "7-day yield", "Expense ratio", "Minimum", "Protection"],
        [[f'<b>{e(ticker(r["name"]))}</b><br><small>{e(r["name"])}</small>',
          e(_broker(r)), e(mm_kind(r["name"])), _yield_cell(r), _exp_txt(r), _min_txt(r), e(r["risk"])]
         for r in ranked])
    sp, vm, sw = _by_t["SPAXX"], _by_t["VMFXX"], _by_t["SWVXX"]
    fz, vu = _by_t.get("FZFXX"), _by_t.get("VUSXX")
    three = (sp, vm, sw)
    hi3 = max(three, key=lambda r: r["rate"])
    lo3 = min(three, key=lambda r: r["rate"])
    gap3 = hi3["rate"] - lo3["rate"]
    vm_sp = vm["rate"] - sp["rate"]
    fz_line = ""
    if fz:
        fz_line = (
            f" If you're at Fidelity and want more, look at the other funds Fidelity offers; "
            f"{e(ticker(fz['name']))} is barely different ({e(fz['rate_apy'])}%) this week."
        )
    vu_sec = ""
    if vu:
        vu_exp, vm_exp = _exp_txt(vu), _exp_txt(vm)
        exp_bit = ""
        if "see fund page" not in (vu_exp + vm_exp):
            exp_bit = f" and has a lower expense ratio ({vu_exp} vs {vm_exp})"
        vu_sec = (
            "<h2>VMFXX vs VUSXX (the sibling switch)</h2>\n"
            f"<p>{e(ticker(vu['name']))}, Vanguard's Treasury fund, out-yields VMFXX this week "
            f"({e(vu['rate_apy'])}% vs {e(vm['rate_apy'])}%){exp_bit}. Moving from the settlement fund to "
            f"{e(ticker(vu['name']))} means buying a separate fund ({e(vu['min_deposit'])} minimum) "
            "and selling it when you need the cash.</p>\n"
        )
    sweep = get("Schwab brokerage uninvested cash (Bank Sweep)")
    if sweep:
        sw_default = (
            f'<li><b>Default cash:</b> Schwab\'s default for uninvested brokerage cash is its bank sweep, '
            f'which pays {e(sweep["rate_apy"])}% APY this week. {e(ticker(sw["name"]))} only earns '
            f'{e(sw["rate_apy"])}% if you buy it yourself.</li>'
        )
        sweep_faq = (
            f", and at Schwab you have to buy SWVXX yourself; the default sweep pays {sweep['rate_apy']}%"
        )
    else:
        sw_default = (
            f'<li><b>Default cash:</b> Schwab\'s default for uninvested brokerage cash is its bank sweep, '
            f'which pays much less. {e(ticker(sw["name"]))} only earns {e(sw["rate_apy"])}% if you buy it yourself.</li>'
        )
        sweep_faq = ", and at Schwab you have to buy SWVXX yourself"
    dollar_line = (
        f"On $10,000 for a year at this week's yields (if they held, which they won't exactly): "
        f"{e(ticker(vm['name']))} \u2248 {dollars(vm['rate'])}, {e(ticker(sw['name']))} \u2248 {dollars(sw['rate'])}, "
        f"{e(ticker(sp['name']))} \u2248 {dollars(sp['rate'])}. The gap between the top and bottom of these three "
        f"is about {dollars(gap3)} a year per $10,000."
    )
    ticker_faqs = [
        ("Which pays more, SPAXX or VMFXX?",
         f"This week VMFXX: {vm['rate_apy']}% vs {sp['rate_apy']}%{faq_asof}."),
        ("Is SWVXX better than SPAXX?",
         f"SWVXX yields more this week ({sw['rate_apy']}% vs {sp['rate_apy']}%), but it is a prime fund while SPAXX is a government fund{sweep_faq}."),
        ("Are SPAXX, VMFXX and SWVXX FDIC insured?",
         "No. They are money market funds, covered by SIPC at the broker for custody, not by the FDIC."),
    ]
    body = f"""{upd(len(ranked))}<div class="prose">
<p class="lead">SPAXX, VMFXX and SWVXX are the money market funds most people meet first at Fidelity, Vanguard and Schwab. They are not the same kind of fund, and this week they don't pay the same. Here are the yields, what each one holds, and when it is worth switching to a sibling fund at the same broker.</p>
<p>Rates change. Confirm yields, minimums and fund documents with the provider before you move money. Not financial advice.</p>
<h2>This week's yields</h2>
<p>{asof_phrase}, from {src_line} SIPC covers custody at the broker if the broker fails, not the fund's value. None of these are FDIC-insured.</p></div>
{tbl}
<div class="prose">
<p>{dollar_line}</p>
<h2>SPAXX vs VMFXX</h2>
<ul>
<li><b>Yield:</b> VMFXX {e(vm["rate_apy"])}% vs SPAXX {e(sp["rate_apy"])}% this week, a {vm_sp:.2f}-point gap.</li>
<li><b>Role:</b> both are the place uninvested cash commonly sits by default: SPAXX as a Fidelity core position, VMFXX as Vanguard's settlement fund.</li>
<li><b>Type:</b> both are government money market funds.</li>
<li><b>Bottom line:</b> you can't hold VMFXX as your core at Fidelity or SPAXX at Vanguard, so this is really a broker comparison.{fz_line}</li>
</ul>
<h2>SPAXX vs SWVXX</h2>
<ul>
<li><b>Yield:</b> SWVXX {e(sw["rate_apy"])}% vs SPAXX {e(sp["rate_apy"])}%.</li>
<li><b>Type:</b> SWVXX is a prime fund, which can hold short-term corporate and bank debt; SPAXX is a government fund.</li>
{sw_default}
</ul>
<h2>VMFXX vs SWVXX</h2>
<ul>
<li><b>Yield:</b> VMFXX {e(vm["rate_apy"])}% vs SWVXX {e(sw["rate_apy"])}%.</li>
<li><b>Minimum:</b> VMFXX needs {e(vm["min_deposit"])} to buy as a separate fund (none as the settlement fund); SWVXX shows {e(sw["min_deposit"])} per Schwab's list.</li>
<li><b>Type:</b> government (VMFXX) vs prime (SWVXX).</li>
</ul>
{vu_sec}<h2>Taxes</h2>
<p>Money market dividends are generally taxed as ordinary income. Part or all of the income from government and Treasury funds may be exempt from state income tax. The rules differ by state, so check the fund's annual tax information.</p>
<p>See all <a href="/money-market/">money market fund yields</a>, the <a href="/best-money-market-funds/">Vanguard vs Schwab vs Fidelity hub</a>, and <a href="/best-broker-cash-rates/">broker cash rates</a>.</p>
</div>
{faq_block(ticker_faqs)}{related("/spaxx-vs-vmfxx-vs-swvxx/")}{RISK_EXPLAINER}{NEWSLETTER}"""
    page("/spaxx-vs-vmfxx-vs-swvxx/",
         f"SPAXX vs VMFXX vs SWVXX: which cash fund pays more this week ({MONTH}) | {BRAND}",
         "SPAXX, VMFXX and SWVXX compared: this week's 7-day yields, fund type, expenses and minimums, plus when a sibling fund at the same broker pays more.",
         body, h1="SPAXX vs VMFXX vs SWVXX: which cash fund pays more this week", faqs=ticker_faqs)


# /rate-changes/ — regenerated each build from history + this week's shown rows.
# Logic matches growth/gen_rate_changes.py: latest two history dates with 20+ rows,
# only names on the live table; carried rates are called out in the notes.
def _rc_fmt(x):
    return ("%.3f" % x).rstrip("0").rstrip(".") if x is not None else ""
def _rc_fd(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d, %Y")
_rc_by, _rc_cat, _rc_counts = {}, {}, {}
for h in HIST:
    _rc_by.setdefault(h["name"], {})[h["date"]] = float(h["rate_apy"])
    _rc_cat[h["name"]] = h["category"]
    _rc_counts[h["date"]] = _rc_counts.get(h["date"], 0) + 1
_rc_full = [d for d in sorted(_rc_counts) if _rc_counts[d] >= 20]
if len(_rc_full) >= 2:
    _rc_cur, _rc_prev = _rc_full[-1], _rc_full[-2]
    _RC_CATN = {"hysa": "High-yield savings", "money_market": "Money market fund",
                "tbill": "T-bill", "broker_cash": "Broker cash", "usdc": "USDC rewards"}
    _rc_live = {r["name"]: r for r in SHOWN}
    _rc_up, _rc_down, _rc_flat, _rc_carried = [], [], [], []
    for n in sorted(_rc_live):
        d = _rc_by.get(n, {})
        if _rc_cur in d and _rc_prev in d:
            ch = round(d[_rc_cur] - d[_rc_prev], 3)
            (_rc_up if ch > 0 else _rc_down if ch < 0 else _rc_flat).append((ch, n))
        else:
            _rc_carried.append(n)
    _rc_up.sort(key=lambda x: -x[0])
    _rc_down.sort(key=lambda x: x[0])
    _rc_head = ["Account / fund", "Type", _rc_fd(_rc_prev), _rc_fd(_rc_cur), "Change", "Protection", "Source"]
    def _rc_row(ch, n):
        l = _rc_live[n]; c = _rc_cat.get(n, "")
        src = (l.get("source_url") or "").strip()
        src_cell = f'<a href="{e(src)}" rel="nofollow noopener">source</a>' if src else ""
        sign = "+" if ch > 0 else ""
        return [e(n), e(_RC_CATN.get(c, c)), e(_rc_fmt(_rc_by[n][_rc_prev]) + "%"),
                f'<b>{e(_rc_fmt(_rc_by[n][_rc_cur]) + "%")}</b>', e(f"{sign}{_rc_fmt(ch)} pts"),
                e(l.get("risk") or ""), src_cell]
    _rc_parts = [
        '<div class="prose">',
        f'<p class="lead">Week of {_rc_fd(_rc_cur)} vs {_rc_fd(_rc_prev)}. {len(_rc_up)} rates rose, {len(_rc_down)} fell, {len(_rc_flat)} held, out of {len(_rc_live)} we track.</p>',
        "<p>Rates change. Confirm with the provider before you move money. Not financial advice.</p></div>",
    ]
    if _rc_up:
        _rc_parts += ['<div class="prose"><h2>Went up</h2></div>', kv_table(_rc_head, [_rc_row(*x) for x in _rc_up])]
    if _rc_down:
        _rc_parts += ['<div class="prose"><h2>Went down</h2></div>', kv_table(_rc_head, [_rc_row(*x) for x in _rc_down])]
    if _rc_flat:
        names = ", ".join(f"{n} ({_rc_fmt(_rc_by[n][_rc_cur])}%)" for _, n in _rc_flat)
        _rc_parts.append(f'<div class="prose"><h2>No change</h2><p>{e(names)}.</p></div>')
    _rc_lis = [
        "<li>Money market fund figures are 7-day yields; T-bill figures are the latest auction's investment rate. Savings, broker cash and USDC are APYs.</li>"
    ]
    if any("52" in n for _, n in _rc_flat):
        _rc_lis.append("<li>The 52-week bill auctions every four weeks, so it shows no change between auctions.</li>")
    for n in _rc_carried:
        l = _rc_live[n]
        _rc_lis.append(
            f"<li>{e(n)}: could not be re-read this week; shown at its last confirmed rate ({e(l.get('rate_apy'))}%), so it is left out of the change tables.</li>"
        )
    _rc_lis.append(
        '<li>Our history starts Oct 21, 2026. Full table: <a href="/">cashratewatch.com</a>. Get the weekly email: <a href="https://cashratewatch.beehiiv.com/">cashratewatch.beehiiv.com</a>.</li>'
    )
    _rc_parts.append('<div class="prose"><h2>Notes</h2><ul>' + "".join(_rc_lis) + "</ul></div>")
    _rc_body = "".join(_rc_parts) + related("/rate-changes/") + RISK_EXPLAINER + NEWSLETTER
    _rc_desc = (
        f"What moved in cash rates this week: {len(_rc_up)} rates rose, {len(_rc_down)} fell, "
        f"{len(_rc_flat)} held, week of {_rc_fd(_rc_cur)} vs {_rc_fd(_rc_prev)}."
    )
    page("/rate-changes/",
         f"What moved in cash rates this week (updated {_rc_fd(_rc_cur)}) | {BRAND}",
         _rc_desc, _rc_body, h1="What moved in cash rates this week")
else:
    print("WARNING: not enough history dates with 20+ rows to build /rate-changes/", file=sys.stderr)

# Schwab bank sweep vs SWVXX — figures from the staged draft (schwab.com/cash-investments, read 11/13).
# Not auto-updated from rates.json; the comparison copy is specific to this week's write-up.
_schwab_tbl = kv_table(
    ["Where the cash sits", "Rate", "Type", "Protection"],
    [
        ["Schwab Prime Advantage Money Fund (SWVXX)", "<b>3.76%</b> 7-day yield (with fee waivers)", "Prime money fund", "SIPC"],
        ["Schwab U.S. Treasury Money Fund (SNSXX)", "<b>3.69%</b> 7-day yield", "Treasury money fund", "SIPC"],
        ["Schwab Treasury Obligations Money Fund (SNOXX)", "<b>3.63%</b> 7-day yield", "Treasury money fund", "SIPC"],
        ["Schwab Government Money Fund (SNVXX)", "<b>3.62%</b> 7-day yield", "Government money fund", "SIPC"],
        ["Schwab Intelligent Portfolios sweep (robo accounts only)", "<b>3.51%</b> APY", "Bank sweep", "FDIC"],
        ["Schwab brokerage uninvested cash (Bank Sweep)", "<b>0.01%</b> APY", "Bank sweep (default)", "FDIC"],
    ],
)
_schwab_faqs = [
    ("What is the Schwab sweep rate this week?",
     "0.01% APY on uninvested brokerage cash (our Nov 17, 2026 check)."),
    ("Is SWVXX better than the Schwab bank sweep?",
     "It yields far more (3.76% vs 0.01% this week), but it is a money fund you have to buy, protected by SIPC rather than FDIC, and sales settle the next business day."),
    ("Does Schwab pay interest on uninvested cash?",
     "Yes, but at the sweep rate. Schwab itself suggests a money fund such as SWVXX for cash you don't plan to spend soon."),
]
_schwab_body = (
    '<div class="prose">'
    '<p class="lead">If you hold cash in a Schwab brokerage account and haven\'t bought anything with it, it sits in Schwab\'s Bank Sweep. This week that pays <b>0.01% APY</b>. Schwab\'s own money funds pay far more, but only if you buy one.</p>'
    '<p>Rates change. Confirm yields, minimums and fund documents with Schwab before you move money. Not financial advice.</p>'
    '<p class="upd">Updated Nov 17, 2026</p>'
    '<h2>This week at Schwab</h2></div>'
    + _schwab_tbl
    + '<div class="prose">'
    '<p>Fund yields are 7-day yields as of Nov 16, 2026, from Schwab\'s money funds list. Sweep rates from Schwab\'s cash and sweep-rate pages. Money funds are covered by SIPC for custody at the broker, not by the FDIC, and can lose value.</p>'
    '<p>On $10,000 for a year at this week\'s rates (if they held, which they won\'t exactly): Bank Sweep ≈ $1, SWVXX ≈ $376. That is about $375 a year per $10,000 for one trade.</p>'
    '<h2>Why the default pays so little</h2>'
    '<p>The Bank Sweep is where cash lands when you sell or deposit. Schwab says uninvested cash earns the same yield whether it is in the Bank Sweep or the Schwab One Interest feature, so switching between those two doesn\'t help. The higher rate comes from buying a money fund. (The 3.51% Intelligent Portfolios sweep applies only to Schwab\'s robo accounts, not a regular brokerage account.)</p>'
    '<h2>How the switch works</h2>'
    '<ul>'
    '<li><b>Buy it like a fund.</b> Schwab says SWVXX can be bought in a single trade with no minimum and no transaction fee (its systems require at least $1 per trade).</li>'
    '<li><b>Getting cash back.</b> Schwab lists next-business-day access when you sell. Cash in the Bank Sweep is available right away, so keep what you need for trades or bills there.</li>'
    '<li><b>It doesn\'t happen automatically.</b> New deposits and sale proceeds go back to the sweep. Buy again after each one.</li>'
    '</ul>'
    '<h2>SWVXX or a Treasury fund?</h2>'
    '<p>SWVXX pays the most of the four this week, but it is a prime fund, which can hold short-term corporate and bank debt. SNSXX and SNOXX hold Treasury securities and pay a little less (3.69% and 3.63%). Interest from Treasury holdings may be partly exempt from state income tax, depending on your state; check the fund\'s tax information.</p>'
    '<p>See <a href="/best-broker-cash-rates/">broker cash rates at every major broker</a>, <a href="/best-money-market-funds/">Vanguard vs Schwab vs Fidelity money funds</a>, and <a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a>.</p>'
    '</div>'
    + faq_block(_schwab_faqs) + related("/schwab-bank-sweep-vs-swvxx/") + RISK_EXPLAINER + NEWSLETTER
)
page("/schwab-bank-sweep-vs-swvxx/",
     f"Schwab bank sweep vs SWVXX: what your uninvested cash earns ({MONTH}) | {BRAND}",
     "Schwab's default bank sweep pays 0.01% APY this week; SWVXX and Schwab's other money funds pay far more if you buy one. How the switch works and the trade-offs.",
     _schwab_body, h1="Schwab bank sweep vs SWVXX: what your uninvested cash earns", faqs=_schwab_faqs)


# State-tax-free money market funds, FDLXX vs SPAXX, and SGOV vs money market funds
# — copy from staged drafts in growth/pages/ (figures as staged; not auto-updated from rates.json).
_stf_tbl = kv_table(
    ["Fund", "7-day yield", "2025 income from US government obligations", "Meets CA/CT/NY 50% rule?", "Kept at a 5% state tax (example)", "Kept at 9.3% in CA, CT or NY (example)"],
    [
        ["Vanguard Treasury Money Market (VUSXX)", "3.98%", "100.00%", "Yes", "<b>3.98%</b>", "<b>3.98%</b>"],
        ["Vanguard Federal Money Market (VMFXX)", "3.88%", "66.61%", "Yes", "<b>3.82%</b>", "<b>3.76%</b>"],
        ["Schwab U.S. Treasury Money Fund (SNSXX)", "3.69%", "99.99%", "Yes", "<b>3.69%</b>", "<b>3.69%</b>"],
        ["Schwab Treasury Obligations Money Fund (SNOXX)", "3.63%", "31.30%", "No", "<b>3.51%</b>", "<b>3.29%</b>"],
        ["Schwab Prime Advantage Money Fund (SWVXX)", "3.76%", "0.00%", "No", "<b>3.57%</b>", "<b>3.41%</b>"],
        ["Fidelity Treasury Money Market (FZFXX)", "3.58%", "61.52%", "No", "<b>3.51%</b>", "<b>3.25%</b>"],
        ["Fidelity Government Money Market (SPAXX)", "3.56%", "50.90%", "No", "<b>3.47%</b>", "<b>3.23%</b>"],
        ["US T-Bill 13-week (direct)", "4.26%", "100% (direct Treasury)", "n/a (held directly)", "<b>4.26%</b>", "<b>4.26%</b>"],
        ["EverBank / Happen Bank top HYSA", "4.20%", "0% (bank interest)", "n/a", "<b>3.99%</b>", "<b>3.81%</b>"],
    ],
)
_stf_body = (
    '<div class="prose">'
    '<p class="lead">Money market fund yields look close on paper. After state income tax they can drift apart, because interest from US Treasury securities is exempt from state income tax and some funds hold far more Treasuries than others. If you live in a state with an income tax, the fund with the highest headline yield may not leave you with the most.</p>'
    '<p>These after-tax figures use this week\'s 7-day yields and each fund\'s 2025 tax data. Not tax or financial advice. Your state\'s rules, your own rate and your tax software decide the real number.</p>'
    '<p class="upd">Updated Nov 17, 2026</p>'
    '<h2>After-state-tax yields this week</h2></div>'
    + _stf_tbl
    + '<div class="prose">'
    '<p>Yields: 7-day yields as of Nov 16, 2026 (T-bill: Nov 16 auction investment rate; HYSA: bank APY), from our weekly table. Percentages: each fund family\'s 2025 tax supplement. "Kept" = yield × (1 − state rate × taxable share). In CA, CT and NY a fund that fails the 50% rule is treated as fully taxable. Federal tax applies to all of these and is left out because it hits them equally. These are example tax rates, not yours.</p>'
    '<h2>What the table says</h2>'
    '<ul>'
    '<li><b>Among money funds, Treasury-only funds win once state tax is counted.</b> At a 5% state rate, VUSXX keeps all 3.98%, ahead of SWVXX\'s 3.76% headline yield, which drops to about 3.57% because none of its income is from Treasuries.</li>'
    '<li><b>"Treasury" in the name isn\'t enough.</b> Funds that hold a lot of repurchase agreements (repos) backed by Treasuries don\'t pass the exemption through, because repo income is generally taxable by states. SNOXX reported 31.30% for 2025 and FZFXX 61.52%. Neither met the CA/CT/NY test.</li>'
    '<li><b>The default sweep fund may be the weakest choice.</b> SPAXX, Fidelity\'s default core fund, reported 50.90% and failed the CA/CT/NY test, so in those states all of its dividends are state-taxable.</li>'
    '<li><b>A top savings account can still win in a no-income-tax state.</b> With no state tax, the HYSA\'s 4.20% and a direct T-bill beat every fund here. The state-tax edge only matters where you pay state tax.</li>'
    '</ul>'
    '<h2>The California, Connecticut and New York 50% rule</h2>'
    '<p>Most states let you exclude the Treasury share of a fund\'s dividends. California, Connecticut and New York add a gate: the fund must hold at least 50% of its assets in qualifying US government obligations at the end of every quarter of its tax year. Miss it once and none of that year\'s dividends are exempt in those three states. Each fund family marks which funds passed in its annual tax supplement.</p>'
    '<h2>How to claim it</h2>'
    '<p>You don\'t get the exemption automatically. Your 1099-DIV shows total ordinary dividends in Box 1a. Multiply that by the fund\'s percentage for the year (from the fund family\'s tax supplement, usually posted in January or February) and subtract the result on your state return. Tax software usually asks for it as "US government interest" or "US obligations." Fund families post new percentages each year, and they move: a fund\'s mix of Treasuries and repos changes.</p>'
    '<h2>VUSXX vs VMFXX in one line</h2>'
    '<p>Both are Vanguard, both met the CA/CT/NY test for 2025. VUSXX paid 3.98% this week with 100% Treasury income; VMFXX paid 3.88% with 66.61%. In a state with income tax, VUSXX comes out ahead this week on both counts.</p>'
    '<h2>Related</h2>'
    '<ul>'
    '<li><a href="/best-money-market-funds/">Best money market funds this week</a></li>'
    '<li><a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a></li>'
    '<li><a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a></li>'
    '<li><a href="https://cashratewatch.beehiiv.com">Get the weekly rate check by email</a></li>'
    '</ul>'
    '<p>Rates change weekly and tax rules vary by state. Check each fund\'s documents and your state\'s instructions, or ask a tax professional. Published by Sparta Labs. Not financial or tax advice.</p>'
    '</div>'
    + related("/state-tax-free-money-market-funds/") + RISK_EXPLAINER + NEWSLETTER
)
page("/state-tax-free-money-market-funds/",
     f"State-tax-free money market funds: VUSXX, SNSXX and what you keep after state tax ({MONTH}) | {BRAND}",
     "Money market fund yields can drift apart after state tax. This week's after-tax math for VUSXX, SNSXX, SPAXX and more, using 2025 tax data.",
     _stf_body, h1="State-tax-free money market funds: VUSXX, SNSXX and what you keep after state tax")

_fdlxx_tbl = kv_table(
    ["", "FDLXX", "SPAXX"],
    [
        ["7-day yield (as of Nov 13, 2026)", "3.62%", "3.55%"],
        ["Expense ratio", "0.42%", "0.42%"],
        ["Minimum", "$0", "$0"],
        ["2025 income from US government securities", "98.67%", "50.90%"],
        ["Met the CA/CT/NY 50% test for 2025?", "Yes", "No"],
        ["Kept in a state with no income tax", "<b>3.62%</b>", "<b>3.55%</b>"],
        ["Kept at a 5% state tax (example)", "<b>3.62%</b>", "<b>3.46%</b>"],
        ["Kept at 9.3% in CA, CT or NY (example)", "<b>3.62%</b>", "<b>3.22%</b>"],
    ],
)
_fdlxx_body = (
    '<div class="prose">'
    '<p class="lead">SPAXX (Fidelity Government Money Market) is the cash fund many Fidelity customers end up holding. FDLXX (Fidelity Treasury Only Money Market) charges the same 0.42% expense ratio, has the same $0 minimum, and this week pays a little more. The bigger gap shows up at tax time: FDLXX holds almost nothing but US Treasuries, while SPAXX also holds agency debt and repurchase agreements, and that changes how much of its income your state can tax.</p>'
    '<p>Not tax or financial advice. Your state\'s rules and your own tax rate decide the real number.</p>'
    '<p class="upd">Updated Nov 15, 2026</p>'
    '<h2>The numbers this week</h2></div>'
    + _fdlxx_tbl
    + '<div class="prose">'
    '<p>On $50,000 for a year at these yields, before federal tax, that is roughly $35 more with FDLXX in a no-tax state, about $77 more at a 5% state rate, and about $198 more at 9.3% in California, Connecticut or New York.</p>'
    '<h2>Why the gap is bigger in California, Connecticut and New York</h2>'
    '<p>Most states exempt the part of a fund\'s dividends that came from US government securities. California, Connecticut and New York add a condition: at least 50% of the fund\'s assets must be in US government obligations at the end of each quarter. Fidelity reported that SPAXX did not meet that test for 2025, so in those three states all of its 2025 dividends are state-taxable, not just half. FDLXX met it.</p>'
    '<h2>What\'s in each fund (Oct 31, 2026)</h2>'
    '<ul>'
    '<li><b>FDLXX:</b> Treasury bills 87.63%, Treasury coupons 14.21% (net other assets -1.84%).</li>'
    '<li><b>SPAXX:</b> Treasury bills 36.75%, agency floating-rate securities 22.11%, government repurchase agreements 28.66%, Treasury coupons 7.91%, TIPS 5.28%.</li>'
    '</ul>'
    '<p>Both keep a $1.00 share price and are SIPC-covered as brokerage holdings, not FDIC-insured. Both are government money market funds, so the credit risk difference is small; the practical difference is state tax.</p>'
    '<h2>Who should care</h2>'
    '<ul>'
    '<li><b>You live in a state with an income tax and hold a large cash balance at Fidelity:</b> FDLXX is worth a look.</li>'
    '<li><b>You live in a no-income-tax state:</b> the difference is just the yield gap, a few basis points this week.</li>'
    '<li><b>You\'re in a tax-advantaged account (IRA, 401(k)):</b> state tax doesn\'t apply, so compare yields only.</li>'
    '</ul>'
    '<h2>Compare more funds</h2>'
    '<ul>'
    '<li><a href="/state-tax-free-money-market-funds/">State-tax-free money market funds across Vanguard, Schwab and Fidelity</a></li>'
    '<li><a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a></li>'
    '<li><a href="/best-money-market-funds/">Best money market funds this week</a></li>'
    '<li><a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a></li>'
    '</ul>'
    '<p>Sources: Fidelity fund pages for FDLXX and SPAXX (yields as of Nov 13, 2026; holdings as of Oct 31, 2026); Fidelity, "2025 Percentage of Income from U.S. Government Securities." Yields change daily; the 2025 percentages change once a year.</p>'
    '</div>'
    + related("/fdlxx-vs-spaxx/") + RISK_EXPLAINER + NEWSLETTER
)
page("/fdlxx-vs-spaxx/",
     f"FDLXX vs SPAXX: which Fidelity money market fund keeps more after state tax? ({MONTH}) | {BRAND}",
     "FDLXX and SPAXX charge the same fee, but FDLXX keeps more after state tax this week, especially in California, Connecticut and New York.",
     _fdlxx_body, h1="FDLXX vs SPAXX: which Fidelity money market fund keeps more after state tax?")

_sgov_tbl = kv_table(
    ["", "Yield", "Measure (as of)", "Price", "Risk label"],
    [
        ["SGOV (iShares 0-3 Month Treasury)", "3.86%", "30-day SEC yield (Nov 12, 2026)", "Trades on NYSE, about $100.56", "Treasury-backed, SIPC at the broker"],
        ["VUSXX (Vanguard Treasury MMF)", "3.98%", "7-day yield", "Fixed $1.00", "SIPC"],
        ["VMFXX (Vanguard Federal MMF)", "3.88%", "7-day yield", "Fixed $1.00", "SIPC"],
        ["SWVXX (Schwab Prime Advantage)", "3.76%", "7-day yield", "Fixed $1.00", "SIPC"],
        ["SPAXX (Fidelity Government MMF)", "3.56%", "7-day yield", "Fixed $1.00", "SIPC"],
        ["4-week T-bill (auction)", "4.022%", "Bond-equivalent yield", "$100 minimum", "Treasury-backed"],
        ["13-week T-bill (auction)", "4.257%", "Bond-equivalent yield", "$100 minimum", "Treasury-backed"],
    ],
)
_sgov_body = (
    '<div class="prose">'
    '<p class="lead">SGOV (iShares 0-3 Month Treasury Bond ETF) has become a common place to park cash in a brokerage account. It holds US Treasury bills that mature within three months. A money market fund such as VUSXX, SPAXX or SWVXX does a similar job. Yields are close. The differences are in how you buy and sell, whether the price moves, and how the income is taxed.</p>'
    '<p>Not tax or financial advice.</p>'
    '<p class="upd">Updated Nov 17, 2026</p>'
    '<h2>The numbers this week</h2></div>'
    + _sgov_tbl
    + '<div class="prose">'
    '<p>Money fund and T-bill rows come from our weekly table (refreshed Nov 17, 2026). SGOV\'s 30-day SEC yield and a money fund\'s 7-day yield are calculated differently, so treat a gap of a few hundredths of a point as noise. Both are quoted after fund expenses. SGOV\'s expense ratio is 0.09%.</p>'
    '<h2>How SGOV differs from a money market fund</h2>'
    '<ul>'
    '<li><b>Price moves a little.</b> SGOV is an ETF. Its NAV ranged from $100.27 to $100.74 over the past 52 weeks, rising as bills accrue interest and dropping when it pays its monthly distribution. A money fund aims to hold a $1.00 share price.</li>'
    '<li><b>You trade it like a stock.</b> You buy and sell SGOV during market hours at the market price, and the trade settles one business day later. Its 30-day median bid/ask spread is 0.01%, so the trading cost is small but not zero. Money funds are bought and sold at $1.00 through your broker, often without a trade.</li>'
    '<li><b>It works at any broker.</b> SGOV trades at any brokerage. Many money funds are easiest (or only available) at their own broker, and some need a minimum (VUSXX and VMFXX list $3,000).</li>'
    '<li><b>It holds only short Treasury bills.</b> SGOV tracks the ICE 0-3 Month US Treasury Securities Index. SPAXX and SWVXX hold other things too (agency debt, repurchase agreements, and for SWVXX, non-government paper).</li>'
    '</ul>'
    '<h2>State tax</h2>'
    '<p>Interest on US Treasury bills is exempt from state income tax when you own the bills directly. For funds, the share of income that came from US government securities is usually exempt too, and California, Connecticut and New York add a 50% test. A Treasury-only fund like SGOV or VUSXX tends to keep more after state tax than a fund that also holds repurchase agreements. Check the fund\'s annual tax letter for the exact percentage. See our <a href="/state-tax-free-money-market-funds/">state-tax page</a> for the math.</p>'
    '<h2>Which one fits</h2>'
    '<ul>'
    '<li><b>Your broker\'s money fund pays well and you move cash often:</b> a money fund is simpler. No trades, no price wiggle.</li>'
    '<li><b>Your broker\'s cash option pays little (for example a 0.01% sweep) and its money funds are limited:</b> SGOV is one way to get a T-bill yield at almost any broker.</li>'
    '<li><b>You can lock money up for a set term:</b> a T-bill bought at auction pays a known yield to maturity. See <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a>.</li>'
    '</ul>'
    '<h2>Compare more cash options</h2>'
    '<ul>'
    '<li><a href="/best-money-market-funds/">Best money market funds</a></li>'
    '<li><a href="/best-broker-cash-rates/">Best broker cash rates</a></li>'
    '<li><a href="/t-bills-vs-high-yield-savings/">T-bills vs high-yield savings</a></li>'
    '</ul>'
    '<p>Rates change. Every number links to its source in our <a href="/">weekly table</a>. Published by Sparta Labs. Not financial advice.</p>'
    '</div>'
    + related("/sgov-vs-money-market-funds/") + RISK_EXPLAINER + NEWSLETTER
)
page("/sgov-vs-money-market-funds/",
     f"SGOV vs money market funds: where should idle cash sit? ({MONTH}) | {BRAND}",
     "SGOV and money market funds such as VUSXX, SPAXX and SWVXX do a similar job. This week's yields, how trading and price differ, and how state tax comes in.",
     _sgov_body, h1="SGOV vs money market funds: where should idle cash sit?")


# 8) SGOV vs BIL vs USFR — issuer-sourced SEC yields (not in the weekly CSV).
_sgov_bil_faqs = [
    ("Is SGOV or BIL better?",
     "This week they pay almost the same (3.86% vs 3.87% 30-day SEC yield). SGOV costs less (0.09% vs 0.1352% gross), holds bills under one month that BIL skips, and is much larger."),
    ("Why does USFR yield more than SGOV?",
     "USFR holds floating-rate Treasury notes whose coupon resets weekly at the 13-week bill rate plus a small spread. That spread is part of why its SEC yield (4.01%) is a little higher this week."),
    ("Can these ETFs lose money?",
     "Prices move slightly day to day, mostly around each monthly payout. With very short or floating-rate Treasuries, those moves are small, but they are not insured or guaranteed like a bank deposit."),
]
_sgov_bil_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th>ETF</th><th>What it holds</th><th>30-day SEC yield</th><th>Expense ratio</th><th>As of</th>"
    "</tr></thead><tbody>"
    "<tr><td>USFR (WisdomTree Floating Rate Treasury)</td><td>2-year floating-rate Treasury notes, coupon resets weekly</td><td>4.01%</td><td>0.15%</td><td>11/13/2026</td></tr>"
    "<tr><td>BIL (SPDR Bloomberg 1-3 Month T-Bill)</td><td>T-bills maturing in 1-3 months</td><td>3.87%</td><td>0.1352% (gross)</td><td>11/13/2026</td></tr>"
    "<tr><td>SGOV (iShares 0-3 Month Treasury Bond)</td><td>T-bills maturing in 0-3 months</td><td>3.86%</td><td>0.09%</td><td>11/12/2026</td></tr>"
    "</tbody></table></div>"
)
_sgov_bil_body = (
    '<div class="prose">'
    '<p class="lead">SGOV, BIL and USFR are the three ETFs people use most to park cash in a brokerage account in Treasuries. All three hold US government debt and pay monthly. They differ in what they hold, what they cost, and how fast their yield follows Fed moves.</p>'
    "<p>Not tax or financial advice.</p>"
    '<p class="upd">Updated Nov 16, 2026</p>'
    "<h2>This week's numbers</h2></div>"
    + _sgov_bil_tbl
    + '<div class="prose">'
    "<p>Yields are from each issuer's fund page. SEC yields are already net of fees, so you can compare them directly. They move every day. The figures are issuer yields as of Nov 16, 2026 (SGOV's 3.86% 30-day SEC yield as of Nov 12 matches the live <a href=\"/sgov-vs-money-market-funds/\">SGOV vs money market funds</a> page).</p>"
    "<h2>In one line each</h2>"
    "<ul>"
    "<li><b>SGOV:</b> cheapest of the three and the biggest by assets. The default pick for most people parking cash.</li>"
    "<li><b>BIL:</b> the oldest (2007) and very similar to SGOV. It costs a bit more and skips bills under one month.</li>"
    "<li><b>USFR:</b> holds floating-rate notes, not bills. Its coupon resets weekly off the 13-week T-bill auction, so it tracks short rates closely. On paper it holds 2-year notes, but price risk is small because the rate floats.</li>"
    "</ul>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You want the lowest cost and plain T-bills:</b> SGOV.</li>"
    "<li><b>Your broker charges a fee on SGOV or you already hold BIL:</b> BIL does the same job. The yield gap this week is 0.01 points.</li>"
    "<li><b>You want a slightly higher yield and don't mind notes instead of bills:</b> USFR paid about 0.15 points more this week. That gap can shrink or flip.</li>"
    '<li><b>You\'d rather not trade an ETF at all:</b> a Treasury money market fund (VUSXX, SNSXX, FZFXX) or buying T-bills directly does the same job without bid/ask spreads. See <a href="/sgov-vs-money-market-funds/">SGOV vs money market funds</a> and <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a>.</li>'
    "</ul>"
    "<h2>Taxes</h2>"
    '<p>All three hold US Treasury debt, and Treasury interest is exempt from state income tax. How much of each ETF\'s income your state lets you exclude depends on your state\'s rules and the fund\'s year-end tax letter. Check the fund\'s annual tax information before you count on it. See <a href="/state-tax-free-money-market-funds/">state-tax-free money market funds</a> for the money-fund version.</p>'
    "</div>"
    + faq_block(_sgov_bil_faqs)
    + '<div class="prose"><p>Sources: iShares SGOV page, SSGA BIL page, WisdomTree USFR page (all read Nov 16, 2026). Money fund and T-bill rates: CashRate Watch weekly refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/sgov-vs-bil-vs-usfr/") + RISK_EXPLAINER + NEWSLETTER
)
page("/sgov-vs-bil-vs-usfr/",
     f"SGOV vs BIL vs USFR: which Treasury cash ETF pays more? ({MONTH}) | {BRAND}",
     "SGOV, BIL and USFR all hold US Treasuries and pay monthly. This week's 30-day SEC yields, fees, and how the holdings differ.",
     _sgov_bil_body, h1="SGOV vs BIL vs USFR: which Treasury cash ETF pays more?", faqs=_sgov_bil_faqs)

# 9) Wealthfront vs Betterment vs Robinhood Gold — base rates from the weekly CSV (M1 is excluded).
_wf_order = [
    "Robinhood Gold High-Yield Cash",
    "Wealthfront Cash Account",
    "Schwab Intelligent Portfolios sweep",
    "Betterment Cash Reserve",
    "Interactive Brokers (IBKR Pro) cash interest",
    "Public High-Yield Cash Account",
]
_wf_need = {
    "Robinhood Gold High-Yield Cash": "Gold subscription, $5/month (non-Gold earns 0%)",
    "Wealthfront Cash Account": "$1 to open",
    "Schwab Intelligent Portfolios sweep": "a Schwab robo account",
    "Betterment Cash Reserve": "none listed",
    "Interactive Brokers (IBKR Pro) cash interest": "paid on USD above $10k",
    "Public High-Yield Cash Account": "$0",
}
_wf_boost = {
    "Robinhood Gold High-Yield Cash": "none listed",
    "Wealthfront Cash Account": "up to 4.45% with boosts",
    "Schwab Intelligent Portfolios sweep": "none (resets monthly)",
    "Betterment Cash Reserve": "new-customer boost to 4.25%",
    "Interactive Brokers (IBKR Pro) cash interest": "none",
    "Public High-Yield Cash Account": "3.75% with paid Public Reserve",
}
_wf_protect = {
    "Robinhood Gold High-Yield Cash": "FDIC via program banks for swept cash; SIPC for some",
    "Wealthfront Cash Account": "FDIC via program banks",
    "Schwab Intelligent Portfolios sweep": "FDIC via Schwab banks",
    "Betterment Cash Reserve": "FDIC via program banks",
    "Interactive Brokers (IBKR Pro) cash interest": "SIPC",
    "Public High-Yield Cash Account": "FDIC via program banks",
}
_wf_rows = [get(n) for n in _wf_order]
_wf_rows = [r for r in _wf_rows if r]
_wf_tr = []
for r in _wf_rows:
    apy = f'{e(r["rate_apy"])}%'
    if r["name"].startswith("Interactive Brokers"):
        apy = f'{e(r["rate_apy"])}% (annual rate)'
    elif r["name"] in ("Wealthfront Cash Account", "Betterment Cash Reserve", "Public High-Yield Cash Account"):
        apy = f'{e(r["rate_apy"])}% APY'
    _wf_tr.append(
        "<tr>"
        f'<td>{e(r["name"])}</td>'
        f"<td>{apy}</td>"
        f'<td>{e(_wf_need.get(r["name"], r.get("min_deposit") or "none listed"))}</td>'
        f'<td>{e(_wf_boost.get(r["name"], "none listed"))}</td>'
        f'<td>{e(_wf_protect.get(r["name"], r["risk"]))}</td>'
        "</tr>"
    )
_wf_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th>Account</th><th>Base rate</th><th>What you need</th><th>Advertised boost</th><th>Protection</th>"
    "</tr></thead><tbody>" + "".join(_wf_tr) + "</tbody></table></div>"
)
_rh = get("Robinhood Gold High-Yield Cash")
_wf = get("Wealthfront Cash Account")
_bt = get("Betterment Cash Reserve")
_wf_math = ""
if _rh and _wf:
    rh_r, wf_r = _rh["rate"], _wf["rate"]
    rh10 = rh_r * 100  # dollars on $10,000 before the Gold fee
    wf10 = wf_r * 100
    after_fee = rh10 - 60
    gap = rh_r - wf_r
    if gap > 0:
        be = 60 / (gap / 100.0)
        be_txt = f"about ${be:,.0f}"
    else:
        be_txt = "not a useful comparison at these rates"
    _wf_math = (
        f'<li><b>Robinhood Gold looks highest but costs $60 a year.</b> On $10,000, {e(_rh["rate_apy"])}% is about ${rh10:,.0f} a year, '
        f'or about ${after_fee:,.0f} after the fee. Wealthfront\'s {e(_wf["rate_apy"])}% with no fee pays about ${wf10:,.0f}. '
        f'The break-even is {be_txt}: below that, Wealthfront\'s base rate pays more after fees.</li>'
    )
_wf_faqs = []
if _wf and _bt:
    _wf_faqs.append((
        "Is Wealthfront or Betterment better for cash?",
        f'Base rates are close this week: Wealthfront {e(_wf["rate_apy"])}% APY, Betterment {e(_bt["rate_apy"])}% APY. Both advertise boosts above that. Both sweep cash to FDIC-insured program banks.',
    ))
if _rh:
    _wf_faqs.append((
        "Does Robinhood pay interest on cash without Gold?",
        f'According to Robinhood\'s Gold page, non-Gold customers earn 0% on uninvested cash. The {e(_rh["rate_apy"])}% rate requires the $5/month Gold subscription.',
    ))
_wf_picks = ""
if _wf and _bt and _rh:
    _wf_picks = (
        "<h2>Quick picks</h2><ul>"
        f'<li><b>No fees, no conditions:</b> Wealthfront Cash ({e(_wf["rate_apy"])}%) or Betterment Cash Reserve ({e(_bt["rate_apy"])}%).</li>'
        f'<li><b>Already pay for Robinhood Gold:</b> its {e(_rh["rate_apy"])}% is the top base rate here.</li>'
        '<li><b>Big balance at a broker:</b> compare with a Treasury money market fund on our <a href="/best-broker-cash-rates/">best broker cash rates</a> page.</li>'
        "</ul>"
    )
_wf_body = (
    '<div class="prose">'
    '<p class="lead">Robo-advisor and broker cash accounts now pay close to the best online savings rates. The headline number often includes a boost or a paid subscription, so here\'s the base rate side by side with what it takes to earn it.</p>'
    "<p>Not tax or financial advice.</p>"
    f'<p class="upd">Updated Nov 16, 2026 (rates as of {UPD_S})</p>'
    "<h2>This week's base rates</h2></div>"
    + _wf_tbl
    + '<div class="prose">'
    f"<p>Rates are variable and taken from each provider's site in our {UPD_S} refresh. Boost terms (how long, how much cash) aren't shown on these pages in enough detail for us to compute a blended rate, so we don't.</p>"
    "<h2>What actually matters</h2>"
    "<ul>"
    + _wf_math
    + '<li><b>Boosts are temporary or conditional.</b> Wealthfront\'s "up to 4.45%" and Betterment\'s 4.25% are boosts, not the ongoing rate. Compare base rates for money you\'ll leave for a year. See <a href="/high-yield-savings-promo-vs-base-rate/">promo vs base rates</a>.</li>'
    '<li><b>These are bank deposits, not funds.</b> Cash is swept to partner banks and covered by FDIC insurance up to the limits of each program. A money market fund like VMFXX or SPAXX pays a bit more this week but isn\'t FDIC-insured. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market funds</a>.</li>'
    "</ul>"
    + _wf_picks
    + "</div>"
    + faq_block(_wf_faqs)
    + '<div class="prose"><p>Sources: provider pages linked in our weekly rate table. Rates refresh every Tuesday. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/wealthfront-vs-betterment-vs-robinhood-cash/") + RISK_EXPLAINER + NEWSLETTER
)
page("/wealthfront-vs-betterment-vs-robinhood-cash/",
     f"Wealthfront vs Betterment vs Robinhood Gold: which cash account pays more? ({MONTH}) | {BRAND}",
     "Base rates for Wealthfront Cash, Betterment Cash Reserve and Robinhood Gold, with boosts and the Gold fee break-even shown separately.",
     _wf_body, h1="Wealthfront vs Betterment vs Robinhood Gold: which cash account pays more?", faqs=_wf_faqs)



# 10) VUSXX vs VMFXX — issuer-sourced 7-day yields from Vanguard pages read Nov 16, 2026.
# Use the staged figures only; do not substitute the Nov 10 weekly CSV rates.
_vusxx_faqs = [
    ("Is VUSXX better than VMFXX?",
     "This week VUSXX pays more (3.98% vs 3.88% 7-day SEC yield) and costs less. The bigger long-term difference is holdings: VUSXX is nearly all Treasuries; VMFXX also holds agency debt and repurchase agreements, which can change the state-tax result."),
    ("Why is VMFXX so much larger?",
     "VMFXX had $375.3 billion in assets as of Oct 31, 2026, versus $118.4 billion for VUSXX. Size doesn't decide the yield. Compare the current 7-day figures and, if you file a state income tax return, the Treasury share."),
    ("Can these funds lose money?",
     "Both aim for a stable $1.00 share price and invest in short-term U.S. government securities. They are not FDIC-insured, and the share price is not guaranteed."),
]
_vusxx_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>VUSXX</th><th>VMFXX</th>"
    "</tr></thead><tbody>"
    "<tr><td>7-day SEC yield (as of Nov 16, 2026)</td><td>3.98%</td><td>3.88%</td></tr>"
    "<tr><td>Expense ratio (as of Dec 19, 2025)</td><td>0.07%</td><td>0.11%</td></tr>"
    "<tr><td>Minimum</td><td>$3,000</td><td>$3,000</td></tr>"
    "<tr><td>Net assets (as of Oct 31, 2026)</td><td>$118.4 billion</td><td>$375.3 billion</td></tr>"
    "<tr><td>Weighted average maturity (as of Oct 31, 2026)</td><td>54 days</td><td>50 days</td></tr>"
    "</tbody></table></div>"
)
_vusxx_body = (
    '<div class="prose">'
    '<p class="lead">VUSXX (Vanguard Treasury Money Market) and VMFXX (Vanguard Federal Money Market) are the two Vanguard money market funds people compare most. Both aim to keep a $1.00 share price, both are government money market funds, and both have a $3,000 minimum. They differ in what they hold, what they cost, and how much of the income a state can tax.</p>'
    "<p>Not tax or financial advice.</p>"
    '<p class="upd">Updated Nov 16, 2026</p>'
    "<h2>This week's numbers</h2></div>"
    + _vusxx_tbl
    + '<div class="prose">'
    "<p>Yields are from each fund's Vanguard profile page. The 7-day SEC yield is already net of fees, so you can compare the two figures directly. They move every day.</p>"
    "<p>On $50,000 for a year, the 0.10-point yield gap is about $50 before tax.</p>"
    "<h2>What's in each fund (Oct 31, 2026)</h2>"
    "<ul>"
    "<li><b>VUSXX:</b> U.S. Treasury bills 98.27%, other U.S. government obligations 1.73%, repurchase agreements 0%. Vanguard's policy is to invest in U.S. Treasury securities and in repurchase agreements fully collateralized by Treasuries, and only those repos with the Federal Reserve Bank of New York.</li>"
    "<li><b>VMFXX:</b> U.S. Treasury bills 37.68%, other U.S. government obligations 36.10%, repurchase agreements 26.22%. Its policy is to invest at least 80% in securities issued by the U.S. government and its agencies and instrumentalities, including repos collateralized by those securities or cash. Vanguard notes that some of those holdings are neither guaranteed by the U.S. Treasury nor backed by the full faith and credit of the U.S. government.</li>"
    "</ul>"
    "<p>Both funds are SIPC-covered as brokerage holdings, not FDIC-insured.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You want the higher yield and almost all Treasuries:</b> VUSXX paid more this week and costs less (0.07% vs 0.11%).</li>"
    '<li><b>You live in a state with an income tax:</b> Treasury interest is often exempt from state tax; repo and agency income usually is not. VUSXX is almost entirely Treasuries, so more of its dividends typically qualify. VMFXX\'s large repo and agency slice usually means a smaller exemption. The after-tax math is on <a href="/state-tax-free-money-market-funds/">state-tax-free money market funds</a>.</li>'
    "<li><b>You already hold VMFXX in a tax-advantaged account (IRA, 401(k)):</b> state tax doesn't apply, so the question is the yield gap, a tenth of a point this week.</li>"
    '<li><b>You\'re choosing at Fidelity or Schwab instead:</b> see <a href="/fdlxx-vs-spaxx/">FDLXX vs SPAXX</a> or <a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a>.</li>'
    "</ul>"
    "</div>"
    + faq_block(_vusxx_faqs)
    + '<div class="prose"><p>Sources: Vanguard VUSXX page and Vanguard VMFXX page (both read Nov 16, 2026). Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/vusxx-vs-vmfxx/") + RISK_EXPLAINER + NEWSLETTER
)
page("/vusxx-vs-vmfxx/",
     f"VUSXX vs VMFXX: which Vanguard money market fund pays more? ({MONTH}) | {BRAND}",
     "VUSXX and VMFXX are Vanguard's Treasury and Federal money market funds. This week's 7-day yields, fees, holdings, and how state tax comes in.",
     _vusxx_body, h1="VUSXX vs VMFXX: which Vanguard money market fund pays more?", faqs=_vusxx_faqs)

# 11) I bonds vs T-bills — TreasuryDirect figures read Nov 16, 2026. Use staged rates only.
_ibond_faqs = [
    ("Why does the I bond rate look so much higher?",
     "Part of the 5.51% is an inflation component that lasts six months and then gets reset. The only piece that stays with that bond for life is the 1.40% fixed rate. A 52-week T-bill at 4.496% is lower today, but that rate is locked for the full year if you hold it to maturity."),
    ("What if I cash an I bond early?",
     "You must wait 12 months. If you cash in before five years, Treasury withholds the last three months of interest."),
    ("Are both state-tax free?",
     "Treasury says interest on I bonds and on T-bills is subject to federal income tax and is not taxed by states or localities. Your own tax situation can differ. This is not tax advice."),
]
_ibond_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>New I bond</th><th>Latest T-bill (investment rate)</th>"
    "</tr></thead><tbody>"
    "<tr><td>Rate now</td><td>5.51% combined (for the first 6 months)</td><td>4.022% (4-week) to 4.496% (52-week)</td></tr>"
    "<tr><td>What is locked</td><td>The 1.40% fixed rate, for the life of that bond</td><td>The auction rate, if you hold to maturity</td></tr>"
    "<tr><td>What can change</td><td>The inflation piece, every 6 months</td><td>Nothing, if you hold to maturity</td></tr>"
    "<tr><td>How long the cash is tied up</td><td>At least 12 months (3 months of interest lost if you cash in before 5 years)</td><td>4 to 52 weeks, or you can sell earlier</td></tr>"
    "<tr><td>How much you can buy</td><td>$25 minimum; $10,000 per year in electronic I bonds</td><td>$100 minimum; up to $10 million on a non-competitive bid</td></tr>"
    "<tr><td>Where you buy</td><td>TreasuryDirect only (electronic, as of Jan 1, 2025)</td><td>TreasuryDirect, or a bank or broker</td></tr>"
    "</tbody></table></div>"
)
_ibond_auctions = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th>Term</th><th>Investment rate</th><th>Auction results</th><th>Issue date</th>"
    "</tr></thead><tbody>"
    "<tr><td>4-week</td><td>4.022%</td><td>Nov 12, 2026</td><td>Nov 17, 2026</td></tr>"
    "<tr><td>8-week</td><td>4.163%</td><td>Nov 12, 2026</td><td>Nov 17, 2026</td></tr>"
    "<tr><td>13-week</td><td>4.257%</td><td>Nov 16, 2026</td><td>Nov 19, 2026</td></tr>"
    "<tr><td>17-week</td><td>4.344%</td><td>Nov 12, 2026</td><td>Nov 17, 2026</td></tr>"
    "<tr><td>26-week</td><td>4.467%</td><td>Nov 16, 2026</td><td>Nov 19, 2026</td></tr>"
    "<tr><td>52-week</td><td>4.496%</td><td>Oct 27, 2026</td><td>Oct 29, 2026</td></tr>"
    "</tbody></table></div>"
)
_ibond_body = (
    '<div class="prose">'
    '<p class="lead">Series I savings bonds and Treasury bills are both Treasury obligations. Interest on both is subject to federal income tax and is not taxed by states or localities. They are not interchangeable. An I bond\'s combined rate resets every six months with inflation, and you cannot cash it for the first year. A T-bill locks a rate for a set term and can be sold before it matures.</p>'
    "<p>Not tax or financial advice.</p>"
    '<p class="upd">Updated Nov 16, 2026</p>'
    "<h2>This week's numbers</h2></div>"
    + _ibond_tbl
    + '<div class="prose">'
    "<p>The 5.51% I bond figure is the combined rate Treasury set for bonds issued November 1, 2026 through April 30, 2027. It includes a 1.40% fixed rate and a 2.04% semiannual inflation rate. That combined rate is an annual rate that applies for six months. The next reset is May 1, 2027, and that number is not out yet, so we do not turn 5.51% into a full-year forecast.</p>"
    "<h2>Latest T-bill auctions</h2>"
    "<p>Investment rates from Treasury's most recent auction of each term:</p></div>"
    + _ibond_auctions
    + '<div class="prose">'
    "<p>The 52-week bill is auctioned only every four weeks, so its latest result is older than the others. Bills are sold at a discount (or at par) and pay face value at maturity. The investment rate is the bond-equivalent yield Treasury publishes with each result.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You want a higher starting rate and can leave the money for at least a year:</b> a new I bond starts at 5.51% for six months, which is above every T-bill rate listed. The rate after May 1, 2027 depends on inflation.</li>"
    "<li><b>You want a rate that cannot change, or you may need the cash in less than a year:</b> buy a T-bill that matures when you need the money. You can also sell a T-bill before maturity; you cannot cash an I bond in the first 12 months.</li>"
    "<li><b>You want to invest more than $10,000:</b> T-bills. The electronic I bond cap is $10,000 a year.</li>"
    '<li><b>You already use a brokerage account for cash:</b> T-bills (or a Treasury money market fund or a Treasury ETF). See <a href="/t-bills-vs-money-market-funds/">T-bills vs money market funds</a> and <a href="/sgov-vs-bil-vs-usfr/">SGOV vs BIL vs USFR</a>.</li>'
    '<li><b>You\'re comparing T-bills with a savings account instead:</b> see <a href="/t-bills-vs-high-yield-savings/">T-bills vs high-yield savings</a>.</li>'
    "</ul>"
    "</div>"
    + faq_block(_ibond_faqs)
    + '<div class="prose"><p>Sources: TreasuryDirect I bond interest-rate page, TreasuryDirect I bonds page, TreasuryDirect Treasury bills page, and TreasuryDirect auction results (all read Nov 16, 2026). Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/i-bonds-vs-t-bills/") + RISK_EXPLAINER + NEWSLETTER
)
page("/i-bonds-vs-t-bills/",
     f"I bonds vs T-bills: which Treasury cash option pays more? ({MONTH}) | {BRAND}",
     "A new I bond starts at 5.51% for six months. Latest T-bill investment rates run from 4.022% to 4.496%. How the rates, lockups and purchase limits differ.",
     _ibond_body, h1="I bonds vs T-bills: which Treasury cash option pays more?", faqs=_ibond_faqs)



# 12) Schwab money market funds — yields from Schwab's page, read Nov 17, 2026. Ship staged figures as written.
_schwab_mmf_faqs = [
    ("Which Schwab money market fund pays the most?",
     "Among the $0-minimum Investor Shares, SWVXX (Prime Advantage) at 3.76% as of Nov 16, 2026."),
    ("SNSXX vs SNOXX: what's the difference?",
     "Both are Treasury funds. This week SNSXX yielded 3.69% and SNOXX 3.63%. Check each fund's holdings before counting on a state-tax exemption, because repurchase agreements are usually not exempt."),
    ("Can I use these as my Schwab cash?",
     "Some Schwab account types can use a money market fund as the designated cash feature; Schwab's Cash Features Disclosure Statement lists the options. Otherwise you buy the fund like any other mutual fund, with no transaction fee."),
]
_schwab_mmf_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th>Fund</th><th>Ticker</th><th>Type</th><th>7-day yield (as of Nov 16, 2026)</th><th>Minimum</th>"
    "</tr></thead><tbody>"
    "<tr><td>Schwab Prime Advantage Money Fund</td><td>SWVXX</td><td>Prime (retail only)</td><td>3.76%</td><td>$0</td></tr>"
    "<tr><td>Schwab U.S. Treasury Money Fund</td><td>SNSXX</td><td>Treasury</td><td>3.69%</td><td>$0</td></tr>"
    "<tr><td>Schwab Treasury Obligations Money Fund</td><td>SNOXX</td><td>Treasury</td><td>3.63%</td><td>$0</td></tr>"
    "<tr><td>Schwab Government Money Fund</td><td>SNVXX</td><td>Government</td><td>3.62%</td><td>$0</td></tr>"
    "<tr><td>Schwab Municipal Money Fund</td><td>SWTXX</td><td>Municipal (retail only)</td><td>2.37%</td><td>$0</td></tr>"
    "<tr><td>Schwab Government Money Market ETF</td><td>SGVT</td><td>Government ETF</td><td>3.68%</td><td>No minimum</td></tr>"
    "</tbody></table></div>"
)
_schwab_te_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th>Federal bracket</th><th>SWTXX tax-equivalent yield</th>"
    "</tr></thead><tbody>"
    "<tr><td>24%</td><td>3.12%</td></tr>"
    "<tr><td>32%</td><td>3.49%</td></tr>"
    "<tr><td>35%</td><td>3.65%</td></tr>"
    "<tr><td>37%</td><td>3.76%</td></tr>"
    "</tbody></table></div>"
)
_schwab_mmf_body = (
    '<div class="prose">'
    '<p class="lead">Schwab sells four main retail money market funds with a $0 minimum, plus municipal funds for people in high tax brackets. They all try to keep a stable $1.00 share price. They differ in what they hold, who can buy them, and how their income is taxed.</p>'
    "<p>Not tax or financial advice.</p>"
    '<p class="upd">Updated Nov 17, 2026</p>'
    "<h2>This week's yields (Investor Shares)</h2></div>"
    + _schwab_mmf_tbl
    + '<div class="prose">'
    "<p>Source: Schwab's money market funds page, 7-day yields with waivers. Yields change daily. Schwab's Ultra Shares pay more (SNAXX 3.91%, SUTXX 3.84%) but need $1,000,000 to start.</p>"
    "<p>On $25,000 for a year, the 0.14-point gap between SWVXX and SNVXX is about $35 before tax.</p>"
    "<h2>What each one holds</h2>"
    "<ul>"
    '<li><b>SWVXX (prime):</b> taxable short-term debt from corporations and banks, plus repurchase agreements and asset-backed commercial paper. It usually pays the most of the four. Schwab only offers it to "natural persons" (individuals), not institutions.</li>'
    "<li><b>SNSXX and SNOXX (Treasury):</b> Treasury money funds typically limit holdings to U.S. Treasury obligations or repurchase agreements backed by Treasuries. Open to all investors.</li>"
    "<li><b>SNVXX (government):</b> short-term U.S. government debt, including repurchase agreements. Open to all investors.</li>"
    "<li><b>SWTXX (municipal):</b> short-term debt from states, local governments and other municipal agencies. Its interest is generally exempt from federal income tax.</li>"
    "</ul>"
    "<p>None of these are FDIC-insured. In a brokerage account they are covered by SIPC, which protects against the broker failing, not against a fund losing value.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>Highest yield in a taxable account, no state income tax:</b> SWVXX paid the most this week.</li>"
    '<li><b>You pay state income tax:</b> Treasury interest is often exempt from state tax. SNSXX is the higher-yielding of the two Treasury funds this week, and more of its income may qualify than SWVXX\'s or SNVXX\'s. The math for other brokers\' funds is on <a href="/state-tax-free-money-market-funds/">state-tax-free money market funds</a>.</li>'
    "<li><b>IRA or other tax-advantaged account:</b> taxes don't change the answer, so compare the yields directly.</li>"
    '<li><b>Your cash is sitting in Schwab\'s bank sweep:</b> that paid 0.01% on our last check. See <a href="/schwab-bank-sweep-vs-swvxx/">Schwab bank sweep vs SWVXX</a>.</li>'
    "</ul>"
    "<h2>Is the municipal fund worth it?</h2>"
    "<p>SWTXX yields 2.37%, but its income is generally free of federal income tax. Divide by (1 minus your federal tax bracket) to compare it with a taxable fund:</p></div>"
    + _schwab_te_tbl
    + '<div class="prose">'
    "<p>This week, only someone in the top bracket comes out about even with SWVXX (3.76%), before state tax. Schwab also offers state-specific versions (California SWKXX 2.14%, New York SWYXX 2.34%) and an AMT tax-free fund (SWWXX 2.35%) that may make sense for residents of those states.</p>"
    "</div>"
    + faq_block(_schwab_mmf_faqs)
    + '<div class="prose"><p>Source: Charles Schwab, Money Market Funds page (read Nov 17, 2026). Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/schwab-money-market-funds/") + RISK_EXPLAINER + NEWSLETTER
)
page("/schwab-money-market-funds/",
     f"Schwab money market funds compared: SWVXX vs SNSXX vs SNOXX vs SNVXX ({MONTH}) | {BRAND}",
     "Schwab's retail money market funds compared: this week's 7-day yields for SWVXX, SNSXX, SNOXX, SNVXX and SWTXX, plus who each one is for.",
     _schwab_mmf_body, h1="Schwab money market funds compared: SWVXX vs SNSXX vs SNOXX vs SNVXX", faqs=_schwab_mmf_faqs)

# 13) Ally vs Marcus savings — rates from each bank's page, read Nov 17, 2026. Ship staged copy as written.
_ally_faqs = [
    ("Is Marcus better than Ally?",
     "On rate, yes this week: 3.50% vs 3.20%. Both have no monthly fees and FDIC insurance. Ally's draw is its budgeting tools; Marcus's is speed on large transfers."),
    ("Do Ally and Marcus rates change?",
     "Yes. Both are variable and can change at any time after you open the account. We recheck them every week."),
    ("Is my money safe at Ally or Marcus?",
     "Both are FDIC-insured banks, which covers deposits up to the legal limit per depositor, per bank, per ownership category."),
]
_ally_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Ally Online Savings</th><th>Marcus Online Savings</th>"
    "</tr></thead><tbody>"
    "<tr><td>APY</td><td>3.20% (as of Nov 16, 2026)</td><td>3.50% (as of Nov 17, 2026)</td></tr>"
    "<tr><td>Monthly fees</td><td>None</td><td>None</td></tr>"
    "<tr><td>Minimum deposit</td><td>No minimum stated on the rate page</td><td>No minimum deposit</td></tr>"
    "<tr><td>Insurance</td><td>FDIC</td><td>FDIC</td></tr>"
    "<tr><td>Notable features</td><td>Savings \"buckets\", recurring transfers and other automatic savings tools</td><td>Same-day transfers of $100,000 or less when sent by 12 pm ET on a business day</td></tr>"
    "</tbody></table></div>"
)
_ally_body = (
    '<div class="prose">'
    '<p class="lead">Ally Bank and Marcus by Goldman Sachs are two of the best-known online savings accounts. Both are FDIC-insured, both have no monthly fees, and both rates are variable. This week Marcus pays more.</p>'
    "<p>Not financial advice.</p>"
    '<p class="upd">Updated Nov 17, 2026</p>'
    "<h2>This week's rates</h2></div>"
    + _ally_tbl
    + '<div class="prose">'
    "<p>Rates are from each bank's own savings page. Marcus notes that maximum balance limits apply.</p>"
    "<p>On $10,000 for a year, the 0.30-point gap is about $30. On $50,000 it's about $150.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You want the higher rate between the two:</b> Marcus, at 3.50% vs 3.20% this week.</li>"
    "<li><b>You like splitting savings into goals inside one account:</b> Ally's buckets do that while paying interest on the whole balance.</li>"
    "<li><b>You move large sums in and out quickly:</b> Marcus advertises same-day transfers up to $100,000.</li>"
    '<li><b>You want the most interest overall:</b> neither tops our table. On our Nov 17 check, Western Alliance Bank via Raisin paid 4.35% (a 90-day rate lock for new Raisin users), EverBank Performance Savings 4.20%, and Bask Bank 3.90%. Before you chase a headline rate, read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>Ally and Marcus vs a money market fund</h2>"
    '<p>If your money is going to sit for a while, a Treasury money market fund at a broker can pay more than either bank. VMFXX yielded 3.88% on our last check. Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_ally_faqs)
    + '<div class="prose"><p>Sources: Ally Bank savings page and Marcus by Goldman Sachs savings page (both read Nov 17, 2026); other rates from the CashRate Watch Nov 17, 2026 refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/ally-vs-marcus-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/ally-vs-marcus-savings/",
     f"Ally vs Marcus: which savings account pays more? ({MONTH}) | {BRAND}",
     "Marcus pays 3.50% APY this week and Ally pays 3.20%. Both are FDIC-insured with no monthly fees. How they compare, and how they stack up against higher-rate alternatives.",
     _ally_body, h1="Ally vs Marcus: which savings account pays more?", faqs=_ally_faqs)


# ---- Capital One vs Ally savings (added 2026-11-18 daily build) ----
_coa_faqs = [
    ("Is Capital One or Ally better for savings?",
     "On rate, it's a tie this week at 3.20%. Pick Capital One if you want branches. Pick Ally if you want buckets and savings tools in one account."),
    ("Is Capital One 360 Performance Savings tiered?",
     "No. Capital One pays the same rate on the whole balance. Ally has balance tiers but pays the same APY in each one right now."),
    ("Do Capital One and Ally rates change?",
     "Yes. Both are variable and can change after you open the account. We recheck them every week."),
    ("Is my money safe at Capital One or Ally?",
     "Both are FDIC-insured banks. Coverage is up to the legal limit per depositor, per bank, per ownership category."),
]
_coa_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Capital One 360 Performance Savings</th><th>Ally Online Savings</th>"
    "</tr></thead><tbody>"
    "<tr><td>APY</td><td>3.20% (effective Nov 17, 2026)</td><td>3.20% (as of Nov 16, 2026)</td></tr>"
    "<tr><td>Monthly fees</td><td>$0</td><td>None</td></tr>"
    "<tr><td>Minimum balance</td><td>None</td><td>No minimum stated on the rate page</td></tr>"
    "<tr><td>Rate tiers</td><td>Same rate on the whole balance</td><td>Has balance tiers, but pays the same APY in all of them</td></tr>"
    "<tr><td>Insurance</td><td>FDIC</td><td>FDIC</td></tr>"
    "<tr><td>Notable features</td><td>Branches and Capital One Caf\u00e9s; multiple savings accounts for goals; automatic savings</td><td>Savings \"buckets\", boosters and round ups inside one account</td></tr>"
    "</tbody></table></div>"
)
_coa_body = (
    '<div class="prose">'
    '<p class="lead">Capital One 360 Performance Savings and Ally Online Savings pay the same rate this week: 3.20% APY. Both are FDIC-insured, both have no monthly fees, and both rates are variable. So the choice comes down to how you bank, not the rate.</p>'
    "<p>Not financial advice.</p>"
    '<p class="upd">Updated Nov 18, 2026</p>'
    "<h2>This week's rates</h2></div>"
    + _coa_tbl
    + '<div class="prose">'
    "<p>Rates are from each bank's own savings page. Capital One's 3.20% is from Nov 17; confirm the current rate on its page.</p>"
    "<p>On $10,000 for a year, the gap is $0. On $50,000 it's still $0.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You want a branch or in-person help:</b> Capital One has branches and Caf\u00e9s. Ally is online only.</li>"
    "<li><b>You want ATM access to savings:</b> neither gives it directly. Capital One says you have to move money to checking first.</li>"
    "<li><b>You like splitting savings into goals:</b> Ally does it with buckets inside one account. Capital One lets you open several savings accounts.</li>"
    "<li><b>You already bank at Discover:</b> Capital One notes that Discover Bank deposits opened on or after May 18, 2025 count with your other Capital One deposits for FDIC limits.</li>"
    '<li><b>You want the most interest:</b> neither tops our table. On our Nov 17 check, Western Alliance Bank via Raisin paid 4.35% (a 90-day rate lock for new Raisin users), EverBank Performance Savings 4.20%, and Bask Bank 3.90%. The 1.15-point gap to Raisin is about $115 a year on $10,000 and $575 on $50,000, if rates hold. Before you chase a headline rate, read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>Capital One and Ally vs a money market fund</h2>"
    '<p>If your money will sit for a while, a money market fund at a broker can pay more than either bank. VMFXX yielded 3.88% on our last check. Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_coa_faqs)
    + '<div class="prose"><p>Sources: Ally Bank savings page and Capital One 360 Performance Savings page (both read Nov 18, 2026; Capital One rate as read Nov 17, 2026); other rates from the CashRate Watch Nov 17, 2026 refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/capital-one-vs-ally-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/capital-one-vs-ally-savings/",
     f"Capital One vs Ally savings: which one should you pick? ({MONTH}) | {BRAND}",
     "Capital One 360 Performance Savings and Ally both pay 3.20% APY this week. Both are FDIC-insured with no monthly fees. How they differ, and higher-rate options.",
     _coa_body, h1="Capital One vs Ally savings: which one should you pick?", faqs=_coa_faqs)

# ---- Amex vs Marcus savings (added 2026-11-18 daily build) ----
_amx_faqs = [
    ("Is Marcus better than Amex for savings?",
     "On rate, yes this week: 3.50% vs 3.20%. Both have no fees, no minimum and FDIC insurance. Amex's draw is 24/7 phone support; Marcus's is same-day transfers."),
    ("Does Amex High Yield Savings have a minimum?",
     "No. Amex says there is no minimum balance to open the account, avoid fees, or earn the APY."),
    ("Do Amex and Marcus rates change?",
     "Yes. Both are variable and can change after you open the account. We recheck them every week."),
    ("Is my money safe at Amex or Marcus?",
     "Both are FDIC-insured banks. Coverage is up to the legal limit per depositor, per bank, per ownership category."),
]
_amx_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>American Express High Yield Savings</th><th>Marcus Online Savings</th>"
    "</tr></thead><tbody>"
    "<tr><td>APY</td><td>3.20% (as of Nov 17, 2026)</td><td>3.50% (as of Nov 18, 2026)</td></tr>"
    "<tr><td>Monthly fees</td><td>None</td><td>None</td></tr>"
    "<tr><td>Minimum deposit</td><td>No minimum balance</td><td>No minimum deposit</td></tr>"
    "<tr><td>Insurance</td><td>FDIC</td><td>FDIC</td></tr>"
    "<tr><td>Notable features</td><td>24/7 phone support; CDs up to five years at the same bank</td><td>Same-day transfers of $100,000 or less when sent by 12 pm ET on a business day</td></tr>"
    "</tbody></table></div>"
)
_amx_body = (
    '<div class="prose">'
    '<p class="lead">American Express High Yield Savings and Marcus by Goldman Sachs are two big-name online savings accounts. Both are FDIC-insured, both have no monthly fees and no minimum, and both rates are variable. This week Marcus pays more.</p>'
    "<p>Not financial advice.</p>"
    '<p class="upd">Updated Nov 18, 2026</p>'
    "<h2>This week's rates</h2></div>"
    + _amx_tbl
    + '<div class="prose">'
    "<p>Rates are from each bank's own savings page. Amex's 3.20% is from Nov 17; confirm the current rate on its page. Marcus notes that maximum balance limits apply.</p>"
    "<p>On $10,000 for a year, the 0.30-point gap is about $30. On $50,000 it's about $150.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You want the higher rate between the two:</b> Marcus, at 3.50% vs 3.20% this week.</li>"
    "<li><b>You want to talk to someone at any hour:</b> Amex has 24/7 phone support.</li>"
    "<li><b>You move large sums quickly:</b> Marcus processes transfers of $100,000 or less the same day if sent by 12 pm ET on a business day. Amex transfers sent after 7 pm ET start the next business day.</li>"
    "<li><b>You have a friend at Marcus:</b> Marcus says a referral can give you both a 1.00% rate boost for 3 months.</li>"
    '<li><b>You want the most interest:</b> neither tops our table. On our Nov 17 check, Western Alliance Bank via Raisin paid 4.35% (a 90-day rate lock for new Raisin users), EverBank Performance Savings 4.20%, and Bask Bank 3.90%. Even Marcus trails Raisin by 0.85 points, about $85 a year on $10,000. Before you chase a headline rate, read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>Amex and Marcus vs a money market fund</h2>"
    '<p>If your money will sit for a while, a money market fund at a broker can pay more than either bank. VMFXX yielded 3.88% on our last check. Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_amx_faqs)
    + '<div class="prose"><p>Sources: Marcus by Goldman Sachs savings page and American Express savings page (both read Nov 18, 2026; Amex rate as read Nov 17, 2026); other rates from the CashRate Watch Nov 17, 2026 refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/amex-vs-marcus-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/amex-vs-marcus-savings/",
     f"Amex vs Marcus: which savings account pays more? ({MONTH}) | {BRAND}",
     "Marcus pays 3.50% APY this week and American Express pays 3.20%. Both are FDIC-insured with no monthly fees or minimum. How they compare, and higher-rate options.",
     _amx_body, h1="Amex vs Marcus: which savings account pays more?", faqs=_amx_faqs)


# ---- SoFi vs Marcus savings and Synchrony vs Ally savings (added 2026-11-18 PM build) ----
# Rates, as-of dates and the SoFi boost come from rates_seed.csv rows at build time, so the weekly refresh updates them.
def _pp_asof(r):
    m = re.search(r"as of (\d{1,2}/\d{1,2}/\d{2,4})", r.get("notes") or "")
    return _fmt_mdy(m.group(1)) if m else _fmt_mdy(r["fetched_at"][:10])
def _pp_r(r): return f'{r["rate"]:.2f}%'
def _pp_gap(a, b):
    pts = round(abs(a["rate"] - b["rate"]), 2)
    return pts, dollars(pts), dollars(pts, 50000)
def _pp_alts():
    out = []
    for n, tag in (("Western Alliance Bank High-Yield Savings via Raisin", "Western Alliance Bank via Raisin paid {r} (a 90-day rate lock for new Raisin users)"),
                   ("EverBank Performance Savings", "EverBank Performance Savings {r}"),
                   ("Bask Bank Savings", "Bask Bank {r}")):
        r = get(n)
        if r: out.append(tag.format(r=_pp_r(r)))
    return (", ".join(out[:-1]) + ", and " + out[-1]) if len(out) > 1 else "".join(out)
def _pp_vmfxx():
    r = get("Vanguard Federal Money Market (VMFXX)")
    return f"VMFXX yielded {_pp_r(r)} on our last check. " if r else ""
_PP_ALT_DATE = fdate(UPD_ISO)

_sm_s, _sm_m = _get_any("SoFi Savings"), _get_any("Marcus by Goldman Sachs Online Savings")
_sm_boost = re.search(r"up to (\d+(?:\.\d+)?)% with limited-time (\d+(?:\.\d+)?)% boost(?: \+ (direct deposit))?", _sm_s.get("notes") or "")
_sm_pts, _sm_d10, _sm_d50 = _pp_gap(_sm_s, _sm_m)
_sm_hi = "Marcus" if _sm_m["rate"] > _sm_s["rate"] else ("SoFi" if _sm_s["rate"] > _sm_m["rate"] else None)
if _sm_boost:
    _sm_up, _sm_bp, _sm_dd = _sm_boost.group(1), _sm_boost.group(2), _sm_boost.group(3)
    _sm_boost_cell = (f"Up to {_sm_up}% with a limited-time {_sm_bp}% boost" + (" and direct deposit" if _sm_dd else "")
                      + ". Check SoFi's page for how long the boost lasts and the exact conditions.")
    _sm_boost_txt = (f"SoFi also advertises up to {_sm_up}% APY. That is the base rate plus a limited-time {_sm_bp}% boost"
                     + (", and it needs direct deposit" if _sm_dd else "") + ". Check SoFi's page for how long the boost lasts and the full conditions before you count on it.")
else:
    _sm_up = None
    _sm_boost_cell = "No boost in our notes this week"
    _sm_boost_txt = "Our notes show no SoFi boost this week."
_sm_lead_rate = (f"On base rates, {_sm_hi} pays more this week: {_pp_r(_sm_m if _sm_hi == 'Marcus' else _sm_s)} vs {_pp_r(_sm_s if _sm_hi == 'Marcus' else _sm_m)}."
                 if _sm_hi else f"On base rates, they pay the same this week: {_pp_r(_sm_s)}.")
_sm_faqs = [
    ("Is SoFi or Marcus better for savings?",
     _sm_lead_rate + (f" SoFi's advertised {_sm_up}% depends on a limited-time boost and direct deposit." if _sm_up else "")
     + " Marcus has no fees and no minimum deposit."),
    ("What is SoFi's savings rate without the boost?",
     f"{_pp_r(_sm_s)} APY base on our {_pp_asof(_sm_s)} check." + (f" The up-to {_sm_up}% figure adds a limited-time boost." if _sm_up else "")),
    ("Do SoFi and Marcus rates change?",
     "Yes. Both are variable and can change after you open the account. We recheck them every week."),
    ("Is my money safe at SoFi or Marcus?",
     "Both are FDIC-insured banks. Coverage is up to the legal limit per depositor, per bank, per ownership category."),
]
_sm_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>SoFi Savings</th><th>Marcus Online Savings</th>"
    "</tr></thead><tbody>"
    f"<tr><td>Base APY</td><td>{_pp_r(_sm_s)} (as of {_pp_asof(_sm_s)})</td><td>{_pp_r(_sm_m)} (as of {_pp_asof(_sm_m)})</td></tr>"
    f"<tr><td>Boost</td><td>{e(_sm_boost_cell)}</td><td>Referral boost: 1.00% for 3 months for you and a new-to-Marcus friend</td></tr>"
    "<tr><td>Monthly fees</td><td>See the bank's page</td><td>None</td></tr>"
    f"<tr><td>Minimum deposit</td><td>{e(_sm_s['min_deposit'])}</td><td>No minimum deposit</td></tr>"
    f"<tr><td>Insurance</td><td>{_sm_s['risk']}</td><td>{_sm_m['risk']}</td></tr>"
    "<tr><td>Notable features</td><td>See the bank's page</td><td>Same-day transfers of $100,000 or less when sent by 12 pm ET on a business day</td></tr>"
    "</tbody></table></div>"
)
_sm_body = (
    '<div class="prose">'
    f'<p class="lead">SoFi Savings and Marcus by Goldman Sachs Online Savings are both online savings accounts. Both are FDIC-insured and both rates are variable. {_sm_lead_rate} SoFi advertises a higher rate with a limited-time boost and direct deposit.</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _sm_tbl
    + '<div class="prose">'
    f"<p>Rates are from each bank's own savings page. Marcus notes that maximum balance limits apply. {e(_sm_boost_txt)}</p>"
    + (f"<p>On base rates, the {_sm_pts:.2f}-point gap is about {_sm_d10} a year on $10,000 and about {_sm_d50} on $50,000, if rates hold.</p>" if _sm_hi else "<p>On base rates, the gap is $0.</p>")
    + "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You want the higher base rate between the two:</b> {_sm_hi}, at {_pp_r(_sm_m if _sm_hi == 'Marcus' else _sm_s)} vs {_pp_r(_sm_s if _sm_hi == 'Marcus' else _sm_m)} this week.</li>" if _sm_hi else "")
    + (f"<li><b>You already have direct deposit you can move:</b> SoFi's up-to {_sm_up}% rate may be worth a look while the boost lasts. When it ends, you're back on the {_pp_r(_sm_s)} base rate.</li>" if _sm_up else "")
    + "<li><b>You move large sums quickly:</b> Marcus processes transfers of $100,000 or less the same day if sent by 12 pm ET on a business day.</li>"
    "<li><b>You have a friend at Marcus:</b> Marcus says a referral can give you both a 1.00% rate boost for 3 months.</li>"
    f'<li><b>You want the most interest without conditions:</b> neither base rate tops our table. On our {_PP_ALT_DATE} check, {_pp_alts()}. Before you chase a headline rate, read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>SoFi and Marcus vs a money market fund</h2>"
    f'<p>If your money will sit for a while, a money market fund at a broker can pay more than either bank\'s base rate. {_pp_vmfxx()}Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_sm_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_sm_s["source_url"])}" rel="nofollow">SoFi savings page</a> and <a href="{e(_sm_m["source_url"])}" rel="nofollow">Marcus by Goldman Sachs savings page</a>; rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/sofi-vs-marcus-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/sofi-vs-marcus-savings/",
     f"SoFi vs Marcus: which savings account pays more? ({MONTH}) | {BRAND}",
     f"Marcus pays {_pp_r(_sm_m)} APY this week and SoFi's base rate is {_pp_r(_sm_s)}"
     + (f", up to {_sm_up}% with a limited-time boost and direct deposit" if _sm_up else "") + ". How they compare, and higher-rate options.",
     _sm_body, h1="SoFi vs Marcus: which savings account pays more?", faqs=_sm_faqs)

# ---- Synchrony vs Ally savings ----
_sa_s, _sa_a = _get_any("Synchrony High Yield Savings"), _get_any("Ally Online Savings")
_sa_pts, _sa_d10, _sa_d50 = _pp_gap(_sa_s, _sa_a)
_sa_hi = "Synchrony" if _sa_s["rate"] > _sa_a["rate"] else ("Ally" if _sa_a["rate"] > _sa_s["rate"] else None)
_sa_hr, _sa_lr = (_pp_r(_sa_s), _pp_r(_sa_a)) if _sa_hi != "Ally" else (_pp_r(_sa_a), _pp_r(_sa_s))
_sa_lead_rate = f"This week {_sa_hi} pays more: {_sa_hr} vs {_sa_lr}." if _sa_hi else f"This week they pay the same: {_pp_r(_sa_s)}."
_sa_faqs = [
    ("Is Synchrony or Ally better for savings?",
     _sa_lead_rate + " Ally's draw is savings buckets and tools inside one account and no monthly maintenance fees."),
    ("Does Synchrony High Yield Savings have a minimum deposit?",
     f"Synchrony lists a minimum deposit of {_sa_s['min_deposit']}. Check Synchrony's page for other terms, such as fees."),
    ("Do Synchrony and Ally rates change?",
     "Yes. Both are variable and can change after you open the account. We recheck them every week."),
    ("Is my money safe at Synchrony or Ally?",
     "Both are FDIC-insured banks. Coverage is up to the legal limit per depositor, per bank, per ownership category."),
]
_sa_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Synchrony High Yield Savings</th><th>Ally Online Savings</th>"
    "</tr></thead><tbody>"
    f"<tr><td>APY</td><td>{_pp_r(_sa_s)} (as of {_pp_asof(_sa_s)})</td><td>{_pp_r(_sa_a)} (as of {_pp_asof(_sa_a)})</td></tr>"
    "<tr><td>Monthly fees</td><td>See the bank's page</td><td>None</td></tr>"
    f"<tr><td>Minimum deposit</td><td>{e(_sa_s['min_deposit'])}</td><td>{e(_sa_a['min_deposit'])}</td></tr>"
    "<tr><td>Rate tiers</td><td>See the bank's page</td><td>Has balance tiers, but pays the same APY in all of them</td></tr>"
    f"<tr><td>Insurance</td><td>{_sa_s['risk']}</td><td>{_sa_a['risk']}</td></tr>"
    "<tr><td>Notable features</td><td>See the bank's page</td><td>Savings \"buckets\", boosters and round ups inside one account</td></tr>"
    "</tbody></table></div>"
)
_sa_body = (
    '<div class="prose">'
    f'<p class="lead">Synchrony High Yield Savings and Ally Online Savings are both online savings accounts. Both are FDIC-insured and both rates are variable. {_sa_lead_rate}</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _sa_tbl
    + '<div class="prose">'
    "<p>Rates are from each bank's own savings page.</p>"
    + (f"<p>On $10,000 for a year, the {_sa_pts:.2f}-point gap is about {_sa_d10}. On $50,000 it's about {_sa_d50}, if rates hold.</p>" if _sa_hi else "<p>On $10,000 for a year, the gap is $0.</p>")
    + "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You want the higher rate between the two:</b> {_sa_hi}, at {_sa_hr} vs {_sa_lr} this week.</li>" if _sa_hi else "")
    + "<li><b>You like splitting savings into goals:</b> Ally does it with buckets inside one account, and pays interest on the whole balance.</li>"
    "<li><b>You want to know every fee before you open:</b> Ally says it has no monthly maintenance fees. Read Synchrony's fee disclosures first.</li>"
    f'<li><b>You want the most interest:</b> neither tops our table. On our {_PP_ALT_DATE} check, {_pp_alts()}. Before you chase a headline rate, read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>Synchrony and Ally vs a money market fund</h2>"
    f'<p>If your money will sit for a while, a money market fund at a broker can pay about the same or more. {_pp_vmfxx()}Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_sa_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_sa_s["source_url"])}" rel="nofollow">Synchrony savings page</a> and <a href="{e(_sa_a["source_url"])}" rel="nofollow">Ally Bank savings page</a>; rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/synchrony-vs-ally-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/synchrony-vs-ally-savings/",
     f"Synchrony vs Ally savings: which pays more? ({MONTH}) | {BRAND}",
     f"Synchrony pays {_pp_r(_sa_s)} APY this week and Ally pays {_pp_r(_sa_a)}. Both are FDIC-insured. How they compare, and higher-rate options.",
     _sa_body, h1="Synchrony vs Ally savings: which pays more?", faqs=_sa_faqs)


# ---- Raisin vs Marcus savings and Vanguard Cash Plus vs VMFXX (added 2026-11-19 build) ----
# Rates, minimums and as-of dates come from rates_seed.csv rows at build time, so the weekly refresh updates them.
# Non-rate facts are from the Raisin Western Alliance page and the Vanguard Cash Plus page (read Nov 17, 2026).
_rm_r, _rm_m = _get_any("Western Alliance Bank High-Yield Savings via Raisin"), _get_any("Marcus by Goldman Sachs Online Savings")
_rm_pts, _rm_d10, _rm_d50 = _pp_gap(_rm_r, _rm_m)
_rm_hi = "Raisin" if _rm_r["rate"] > _rm_m["rate"] else ("Marcus" if _rm_m["rate"] > _rm_r["rate"] else None)
_rm_hr, _rm_lr = (_pp_r(_rm_r), _pp_r(_rm_m)) if _rm_hi != "Marcus" else (_pp_r(_rm_m), _pp_r(_rm_r))
_rm_hysa = [r for r in SHOWN if r["category"] == "hysa"]
_rm_top = bool(_rm_hysa) and _rm_hysa[0]["name"] == _rm_r["name"]
_rm_lock = bool(re.search(r"90-day rate lock", _rm_r.get("notes") or ""))
_rm_lead_rate = (f"This week {'Western Alliance Bank via Raisin' if _rm_hi == 'Raisin' else 'Marcus'} pays more: {_rm_hr} vs {_rm_lr}."
                 if _rm_hi else f"This week they pay the same: {_pp_r(_rm_r)}.")
_rm_faqs = [
    ("Is Raisin or Marcus better for savings?",
     _rm_lead_rate + " Marcus is an account you open with Marcus itself. With Raisin you sign up on Raisin's platform and the account is held at a partner bank, here Western Alliance Bank."),
    ("Is Raisin a bank?",
     "No. Raisin is a platform that connects savers with banks and credit unions. It doesn't hold customer funds. Your deposits go to the partner bank, in this case Western Alliance Bank, Member FDIC."),
    ("Is money in a Raisin account FDIC-insured?",
     "Raisin says deposits for this account sit in a custodial account at Western Alliance Bank for your benefit and qualify for FDIC insurance up to $250,000 per depositor, subject to certain conditions. Raisin itself is not an FDIC-insured bank."),
    ("How long does the Raisin rate last?",
     ("New Raisin users get a 90-day rate lock. After that the rate is variable and can change, like Marcus's rate. We recheck both every week."
      if _rm_lock else "Both rates are variable and can change after you open the account. We recheck both every week.")),
]
_rm_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Western Alliance Bank via Raisin</th><th>Marcus Online Savings</th>"
    "</tr></thead><tbody>"
    f"<tr><td>APY</td><td>{_pp_r(_rm_r)} (as of {_pp_asof(_rm_r)})</td><td>{_pp_r(_rm_m)} (as of {_pp_asof(_rm_m)})</td></tr>"
    + (f"<tr><td>Intro terms</td><td>90-day rate lock for new Raisin users, then variable</td><td>Referral boost: 1.00% for 3 months for you and a new-to-Marcus friend</td></tr>"
       if _rm_lock else "<tr><td>Intro terms</td><td>None in our notes this week</td><td>Referral boost: 1.00% for 3 months for you and a new-to-Marcus friend</td></tr>")
    + "<tr><td>Monthly fees</td><td>None (no platform or maintenance fees)</td><td>None</td></tr>"
    f"<tr><td>Minimum deposit</td><td>{e(_rm_r['min_deposit'])}</td><td>No minimum deposit</td></tr>"
    "<tr><td>Who holds your money</td><td>Western Alliance Bank, through Raisin (not a bank)</td><td>Marcus by Goldman Sachs</td></tr>"
    f"<tr><td>Insurance</td><td>{_rm_r['risk']}, up to $250,000 per depositor through Western Alliance Bank</td><td>{_rm_m['risk']}</td></tr>"
    "<tr><td>Notable features</td><td>One Raisin login for accounts at many partner banks</td><td>Same-day transfers of $100,000 or less when sent by 12 pm ET on a business day</td></tr>"
    "</tbody></table></div>"
)
_rm_body = (
    '<div class="prose">'
    f'<p class="lead">Western Alliance Bank High-Yield Savings is opened through Raisin. Marcus by Goldman Sachs Online Savings is opened with Marcus directly. Both are FDIC-insured and both rates are variable. {_rm_lead_rate}'
    + (" That Raisin rate is the highest savings rate on our board this week." if _rm_top else "") + "</p>"
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _rm_tbl
    + '<div class="prose">'
    "<p>Rates are from Raisin's Western Alliance Bank page and the Marcus savings page. Marcus notes that maximum balance limits apply."
    + (" Raisin's rate comes with a 90-day lock for new users. After that it can change." if _rm_lock else "") + "</p>"
    + (f"<p>On $10,000 for a year, the {_rm_pts:.2f}-point gap is about {_rm_d10}. On $50,000 it's about {_rm_d50}, if rates hold.</p>" if _rm_hi else "<p>On $10,000 for a year, the gap is $0.</p>")
    + "<h2>How a Raisin account works</h2>"
    "<p>Raisin is a marketplace, not a bank. You sign up once with Raisin, pick a savings account from one of its partner banks, and fund it. Raisin says your money goes into a custodial account at Western Alliance Bank for your benefit. It qualifies for FDIC insurance up to $250,000 per depositor, subject to certain conditions.</p>"
    "<p>Raisin says it charges no platform or maintenance fees and has no minimum balance. Interest accrues daily and is posted monthly. Raisin is paid by its partner banks, not by you.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You want the higher rate between the two:</b> {'Western Alliance Bank via Raisin' if _rm_hi == 'Raisin' else 'Marcus'}, at {_rm_hr} vs {_rm_lr} this week.</li>" if _rm_hi else "")
    + "<li><b>You'd rather deal with one company:</b> Marcus. You open, fund and manage the account with Marcus itself. There's no platform in the middle.</li>"
    "<li><b>You keep more than $250,000 in savings:</b> Raisin lets you open accounts at several partner banks under one login, which makes it easier to stay under the FDIC limit at each bank.</li>"
    "<li><b>You move large sums quickly:</b> Marcus processes transfers of $100,000 or less the same day if sent by 12 pm ET on a business day.</li>"
    + ("<li><b>You plan to leave the money a long time:</b> the 90-day lock only covers the first 90 days. After that, both rates float. Compare again when the lock ends.</li>" if _rm_lock else "")
    + '<li><b>You want to see how promos work first:</b> read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>Raisin and Marcus vs a money market fund</h2>"
    f'<p>A money market fund at a broker is another place to park cash. {_pp_vmfxx()}Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_rm_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_rm_r["source_url"])}" rel="nofollow">Raisin Western Alliance Bank page</a> and <a href="{e(_rm_m["source_url"])}" rel="nofollow">Marcus by Goldman Sachs savings page</a>; rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/raisin-vs-marcus-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/raisin-vs-marcus-savings/",
     f"Raisin vs Marcus: which savings account pays more? ({MONTH}) | {BRAND}",
     f"Western Alliance Bank via Raisin pays {_pp_r(_rm_r)} APY this week and Marcus pays {_pp_r(_rm_m)}. How a Raisin account works, FDIC coverage, and the gap in dollars.",
     _rm_body, h1="Raisin vs Marcus: which savings account pays more?", faqs=_rm_faqs)

# ---- Vanguard Cash Plus vs VMFXX ----
_cv_c, _cv_v = _get_any("Vanguard Cash Plus"), _get_any("Vanguard Federal Money Market (VMFXX)")
_cv_u = _get_any("Vanguard Treasury Money Market (VUSXX)")
_cv_pts, _cv_d10, _cv_d50 = _pp_gap(_cv_c, _cv_v)
_cv_hi = "VMFXX" if _cv_v["rate"] > _cv_c["rate"] else ("Cash Plus" if _cv_c["rate"] > _cv_v["rate"] else None)
_cv_hr, _cv_lr = (_pp_r(_cv_v), _pp_r(_cv_c)) if _cv_hi != "Cash Plus" else (_pp_r(_cv_c), _pp_r(_cv_v))
_cv_exp = re.search(r"expense (\d+(?:\.\d+)?)%", _cv_v.get("notes") or "")
_cv_lead_rate = (f"This week {_cv_hi} pays more: {_cv_hr} vs {_cv_lr}." if _cv_hi else f"This week they pay the same: {_pp_r(_cv_c)}.")
_cv_faqs = [
    ("Is Vanguard Cash Plus or VMFXX better?",
     _cv_lead_rate + f" Cash Plus is a bank sweep eligible for FDIC insurance and has a {_cv_c['min_deposit']} minimum. VMFXX is a money market fund, not FDIC-insured, with a {_cv_v['min_deposit']} minimum."),
    ("Is Vanguard Cash Plus FDIC-insured?",
     "Cash Plus moves your balance to one or more program banks, where it's eligible for FDIC coverage, subject to limits and conditions. Those bank balances are not covered by SIPC."),
    ("Is VMFXX FDIC-insured?",
     "No. VMFXX is a government money market fund. It's covered by SIPC as a brokerage holding, which protects custody, not the fund's value."),
    ("How much more does VMFXX pay on $10,000?",
     (f"At this week's rates, about {_cv_d10} a year on $10,000 and about {_cv_d50} on $50,000, if rates hold. VMFXX's figure is a 7-day SEC yield and Cash Plus shows an APY, so treat the gap as a rough guide."
      if _cv_hi == "VMFXX" else f"At this week's rates, VMFXX doesn't pay more. The gap is about {_cv_d10} a year on $10,000.")),
]
_cv_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Vanguard Cash Plus</th><th>VMFXX</th>"
    "</tr></thead><tbody>"
    f"<tr><td>Rate</td><td>{_pp_r(_cv_c)} APY, starting rate (as of {_pp_asof(_cv_c)})</td><td>{_pp_r(_cv_v)} 7-day SEC yield (as of {_pp_asof(_cv_v)})</td></tr>"
    "<tr><td>What it is</td><td>Bank sweep account</td><td>Government money market fund</td></tr>"
    f"<tr><td>Protection</td><td>{_cv_c['risk']} through program banks, subject to limits. Not SIPC.</td><td>{_cv_v['risk']} (covers custody, not value). Not FDIC.</td></tr>"
    f"<tr><td>Minimum</td><td>{e(_cv_c['min_deposit'])}</td><td>{e(_cv_v['min_deposit'])}</td></tr>"
    + (f"<tr><td>Costs</td><td>$0 account service fees with e-delivery</td><td>Expense ratio {_cv_exp.group(1)}% (already netted out of the yield)</td></tr>"
       if _cv_exp else "<tr><td>Costs</td><td>$0 account service fees with e-delivery</td><td>Not in our notes this week</td></tr>")
    + "<tr><td>Everyday banking</td><td>Routing and account numbers, direct deposit, bill pay, mobile check deposit</td><td>See Vanguard's fund page</td></tr>"
    "</tbody></table></div>"
)
_cv_body = (
    '<div class="prose">'
    f'<p class="lead">Vanguard Cash Plus and VMFXX (Vanguard Federal Money Market Fund) are two ways to hold cash at Vanguard. Cash Plus sweeps your money to program banks, where it\'s eligible for FDIC insurance. VMFXX is a money market fund. It isn\'t FDIC-insured. {_cv_lead_rate}</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _cv_tbl
    + '<div class="prose">'
    "<p>Rates are from Vanguard's Cash Plus page and the VMFXX fund page. VMFXX's 7-day SEC yield moves every day. The two figures aren't measured quite the same way, so treat the gap as a rough guide.</p>"
    + (f"<p>On $10,000 for a year, the {_cv_pts:.2f}-point gap is about {_cv_d10}. On $50,000 it's about {_cv_d50}, if rates hold.</p>" if _cv_hi else "<p>On $10,000 for a year, the gap is $0.</p>")
    + f"<p>The {_pp_r(_cv_c)} is Cash Plus's starting rate. Vanguard lists a higher Cash Plus Enhanced APY for clients with $1 million or more in qualifying assets, Personal Advisor clients, and some employer plan participants. We don't have that rate, so it isn't shown.</p>"
    "<h2>FDIC sweep vs money fund</h2>"
    "<p>Vanguard isn't a bank. With Cash Plus, your balance sits at one or more program banks and is eligible for FDIC coverage there. That money is not covered by SIPC.</p>"
    "<p>VMFXX is a mutual fund that holds short-term U.S. government debt, including repurchase agreements. It's covered by SIPC as a brokerage holding. SIPC protects you if the broker fails. It doesn't protect the fund's value.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You want the higher yield and have {e(_cv_v['min_deposit'])}:</b> VMFXX, at {_pp_r(_cv_v)} vs {_pp_r(_cv_c)} this week.</li>" if _cv_hi == "VMFXX" else "")
    + "<li><b>You want FDIC coverage:</b> Cash Plus. VMFXX doesn't have it.</li>"
    f"<li><b>You have less than {e(_cv_v['min_deposit'])}:</b> Cash Plus has a {e(_cv_c['min_deposit'])} minimum. VMFXX needs {e(_cv_v['min_deposit'])} to start.</li>"
    "<li><b>You pay bills from this money:</b> Cash Plus gives you routing and account numbers, direct deposit and bill pay.</li>"
    '<li><b>You live in a state with an income tax:</b> part of VMFXX\'s income may be exempt from state tax, since some of it is Treasury interest. Bank interest from Cash Plus is not. The math is on <a href="/state-tax-free-money-market-funds/">state-tax-free money market funds</a>.</li>'
    + (f'<li><b>You want a higher yield at Vanguard:</b> VUSXX, the Treasury fund, yielded {_pp_r(_cv_u)} on our last check. See <a href="/vusxx-vs-vmfxx/">VUSXX vs VMFXX</a>.</li>' if _cv_u and _cv_u["rate"] > max(_cv_v["rate"], _cv_c["rate"]) else "")
    + "</ul>"
    "<p>You don't have to pick one. Vanguard's Cash Plus page says you can also hold Vanguard money market funds in the account, with a $3,000 minimum each.</p>"
    "</div>"
    + faq_block(_cv_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_cv_c["source_url"])}" rel="nofollow">Vanguard Cash Plus page</a> and <a href="{e(_cv_v["source_url"])}" rel="nofollow">Vanguard VMFXX page</a>; rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/vanguard-cash-plus-vs-vmfxx/") + RISK_EXPLAINER + NEWSLETTER
)
page("/vanguard-cash-plus-vs-vmfxx/",
     f"Vanguard Cash Plus vs VMFXX: which pays more? ({MONTH}) | {BRAND}",
     f"VMFXX yields {_pp_r(_cv_v)} this week and Vanguard Cash Plus starts at {_pp_r(_cv_c)} APY. FDIC bank sweep vs government money fund, minimums, and the gap per $10,000.",
     _cv_body, h1="Vanguard Cash Plus vs VMFXX: which pays more?", faqs=_cv_faqs)


# ---- SPAXX vs FZFXX and Coinbase vs Kraken USDC rewards (added 2026-11-20 build) ----
# Rates, minimums, expense ratio, eligibility and as-of dates come from rates_seed.csv rows at build time.
# Non-rate facts: Fidelity "2025 Percentage of Income from U.S. Government Securities" (ty25-gse-supplemental-letter.pdf,
# read Nov 20, 2026: SPAXX 50.90%*, FZFXX 61.52%*, FDLXX 98.67%; * = missed the CA/CT/NY minimum) and Fidelity's
# core position FAQ (fidelity.com/trading/faqs-about-account/, read Nov 20, 2026).
_FID_GSE_URL = "https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/taxes/ty25-gse-supplemental-letter.pdf"
_FID_CORE_URL = "https://www.fidelity.com/trading/faqs-about-account/"
_sf_s, _sf_f = _get_any("Fidelity Government Money Market (SPAXX)"), _get_any("Fidelity Treasury Money Market (FZFXX)")
_sf_pts, _sf_d10, _sf_d50 = _pp_gap(_sf_s, _sf_f)
_sf_hi = "FZFXX" if _sf_f["rate"] > _sf_s["rate"] else ("SPAXX" if _sf_s["rate"] > _sf_f["rate"] else None)
_sf_hr, _sf_lr = (_pp_r(_sf_f), _pp_r(_sf_s)) if _sf_hi != "SPAXX" else (_pp_r(_sf_s), _pp_r(_sf_f))
_sf_lo = "SPAXX" if _sf_hi == "FZFXX" else "FZFXX"
_sf_exp = {k: re.search(r"expense (\d+(?:\.\d+)?)%( \(gross\))?", r.get("notes") or "") for k, r in (("SPAXX", _sf_s), ("FZFXX", _sf_f))}
def _sf_exp_txt(k):
    m = _sf_exp[k]
    return f"{m.group(1)}%{' (gross)' if m.group(2) else ''}" if m else "See Fidelity's fund page"
_SF_GSE = {"SPAXX": 50.90, "FZFXX": 61.52}   # 2025, Fidelity letter; both marked as missing the CA/CT/NY minimum
def _sf_keep(r, k, st=5.0):
    return r["rate"] - r["rate"] * (1 - _SF_GSE[k] / 100) * st / 100
_sf_k_s, _sf_k_f = _sf_keep(_sf_s, "SPAXX"), _sf_keep(_sf_f, "FZFXX")
_sf_lead_rate = (f"This week {_sf_hi} yields a little more: {_sf_hr} vs {_sf_lr}." if _sf_hi
                 else f"This week they yield the same: {_pp_r(_sf_s)}.")
_sf_faqs = [
    ("Is FZFXX or SPAXX better?",
     _sf_lead_rate + " Both have a " + _sf_s["min_deposit"] + " minimum and can be your core position in a nonretirement Fidelity account. FZFXX holds Treasuries and Treasury repos, so more of its income was state-tax-exempt in 2025: 61.52% vs 50.90% for SPAXX."),
    ("Is FZFXX state tax-free?",
     "Partly. Fidelity reported that 61.52% of FZFXX's 2025 income came from US government securities, which most states exempt. The rest came mostly from repurchase agreements, which states usually tax. FZFXX missed the minimum that California, Connecticut and New York require, so in those states none of its 2025 dividends were exempt. Check the fund's annual tax letter each year."),
    ("Can FZFXX be my core position at Fidelity?",
     "Yes, in a nonretirement account. Fidelity lists SPAXX, FZFXX and FCASH as core options there, and you can change your core position after the account is open. Retirement accounts and some other account types have different choices."),
    ("Are SPAXX and FZFXX FDIC-insured?",
     "No. Both are money market funds, not bank accounts. They aim to hold a $1.00 share price but can't promise it. SIPC covers them as brokerage holdings, which protects custody if the broker fails, not the fund's value."),
]
_sf_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>SPAXX</th><th>FZFXX</th>"
    "</tr></thead><tbody>"
    f"<tr><td>7-day yield</td><td>{_pp_r(_sf_s)} (as of {_pp_asof(_sf_s)})</td><td>{_pp_r(_sf_f)} (as of {_pp_asof(_sf_f)})</td></tr>"
    "<tr><td>Full name</td><td>Fidelity Government Money Market Fund</td><td>Fidelity Treasury Money Market Fund</td></tr>"
    "<tr><td>What it holds</td><td>US government agency and Treasury debt, plus related repurchase agreements</td><td>US Treasury securities, plus related repurchase agreements</td></tr>"
    f"<tr><td>Expense ratio</td><td>{_sf_exp_txt('SPAXX')}</td><td>{_sf_exp_txt('FZFXX')}</td></tr>"
    f"<tr><td>Minimum</td><td>{e(_sf_s['min_deposit'])}</td><td>{e(_sf_f['min_deposit'])}</td></tr>"
    "<tr><td>2025 income from US government securities</td><td>50.90%</td><td>61.52%</td></tr>"
    "<tr><td>Met the CA/CT/NY minimum for 2025?</td><td>No</td><td>No</td></tr>"
    "<tr><td>Core position option</td><td>Yes, in nonretirement and retirement accounts</td><td>Yes, in nonretirement accounts</td></tr>"
    f"<tr><td>Protection</td><td>{_sf_s['risk']} (covers custody, not value). Not FDIC.</td><td>{_sf_f['risk']} (covers custody, not value). Not FDIC.</td></tr>"
    "</tbody></table></div>"
)
_sf_body = (
    '<div class="prose">'
    f'<p class="lead">SPAXX and FZFXX are the two money market funds Fidelity offers as a core position in a regular brokerage account. SPAXX is the government fund. FZFXX is the Treasury fund. {_sf_lead_rate} The bigger difference shows up on your state tax return.</p>'
    "<p>Not tax or financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's yields</h2></div>"
    + _sf_tbl
    + '<div class="prose">'
    "<p>Yields are from Fidelity's fund pages and are quoted after fund expenses. A 7-day yield moves every day.</p>"
    + (f"<p>On $10,000 for a year, the {_sf_pts:.2f}-point gap is about {_sf_d10}. On $50,000 it's about {_sf_d50}, if yields hold.</p>" if _sf_hi else "<p>On $10,000 for a year, the gap is $0.</p>")
    + "<h2>What each fund holds</h2>"
    "<p>Fidelity describes SPAXX as a fund that invests in US government agency and Treasury debt and related repurchase agreements. FZFXX invests in US Treasury securities and related repurchase agreements. Both are low-risk government funds. Neither one is Treasury-only, and that matters for state tax.</p>"
    "<h2>State tax: where FZFXX pulls ahead</h2>"
    "<p>Most states exempt the part of a fund's dividends that came from US government securities. Interest from repurchase agreements usually doesn't count, even when the repo is backed by Treasuries. For 2025, Fidelity reported that 61.52% of FZFXX's income and 50.90% of SPAXX's came from US government securities.</p>"
    f"<p>Say you pay a 5% state income tax and this year's mix looks like 2025. SPAXX would keep about {_sf_k_s:.2f}% after state tax and FZFXX about {_sf_k_f:.2f}%. In a state with no income tax, or inside an IRA, only the yield gap matters.</p>"
    "<p>California, Connecticut and New York add a test. A fund must hold at least 50% of its assets in US government securities at the end of each quarter, or none of its income is exempt in those states. Fidelity marked both SPAXX and FZFXX as missing that minimum for 2025. So for 2025, residents of those three states owed state tax on every dividend dollar from either fund.</p>"
    f'<p>The percentages change every year. Check your fund\'s annual tax letter before you file. If state tax is your main concern, FDLXX (Fidelity Treasury Only) reported 98.67% for 2025. See <a href="/fdlxx-vs-spaxx/">FDLXX vs SPAXX</a> and <a href="/state-tax-free-money-market-funds/">state-tax-free money market funds</a>.</p>'
    "<h2>Using either one as your core position</h2>"
    "<p>Your core position is where Fidelity holds uninvested cash. Sale proceeds land there, and buys are paid from it automatically. In a nonretirement account, Fidelity lists SPAXX, FZFXX and FCASH (a cash balance that pays interest) as core options. In retirement accounts, SPAXX is the money fund on the core list. Some account types offer different choices.</p>"
    "<p>You can change your core position after the account is open. Since both funds aim for a $1.00 share price, moving between them shouldn't create a gain or loss. FDLXX isn't on the standard core list, so you'd buy it like any other fund.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You only care about yield:</b> {_sf_hi}, at {_sf_hr} vs {_sf_lr} this week. The gap is small and can flip.</li>" if _sf_hi else "")
    + "<li><b>You pay state income tax outside California, Connecticut and New York:</b> FZFXX. More of its 2025 income was state-tax-exempt.</li>"
    "<li><b>You live in California, Connecticut or New York:</b> neither fund passed the 2025 test. Look at FDLXX, which did. See <a href=\"/fdlxx-vs-spaxx/\">FDLXX vs SPAXX</a>.</li>"
    "<li><b>Your cash sits in an IRA or 401(k):</b> state tax doesn't apply. Compare yields, and check which core options your account offers.</li>"
    '<li><b>You want to compare other brokers\' funds:</b> see <a href="/spaxx-vs-vmfxx-vs-swvxx/">SPAXX vs VMFXX vs SWVXX</a>.</li>'
    "</ul>"
    "</div>"
    + faq_block(_sf_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_sf_s["source_url"])}" rel="nofollow">Fidelity SPAXX page</a>, <a href="{e(_sf_f["source_url"])}" rel="nofollow">Fidelity FZFXX page</a>, <a href="{_FID_GSE_URL}" rel="nofollow">Fidelity 2025 Percentage of Income from U.S. Government Securities</a> and <a href="{_FID_CORE_URL}" rel="nofollow">Fidelity core position FAQ</a>; yields from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not tax or financial advice.</p></div>'
    + related("/spaxx-vs-fzfxx/") + RISK_EXPLAINER + NEWSLETTER
)
page("/spaxx-vs-fzfxx/",
     f"SPAXX vs FZFXX: which Fidelity core fund keeps more? ({MONTH}) | {BRAND}",
     f"SPAXX yields {_pp_r(_sf_s)} and FZFXX {_pp_r(_sf_f)} this week. What each Fidelity fund holds, the state-tax difference, and using either as your core position.",
     _sf_body, h1="SPAXX vs FZFXX: which Fidelity core fund keeps more?", faqs=_sf_faqs)

# ---- Coinbase vs Kraken USDC rewards ----
_ck_c, _ck_k = _get_any("Coinbase USDC rewards"), _get_any("Kraken USDC rewards (flexible)")
_ck_hy = best("hysa")
_ck_t13, _ck_t26 = _get_any("US T-Bill 13-week"), _get_any("US T-Bill 26-week")
_ck_fee = re.search(r"\$(\d+(?:\.\d+)?)\+?/mo", _ck_c.get("min_deposit") or "")
_ck_fee_mo = float(_ck_fee.group(1)) if _ck_fee else None
_ck_fee_yr = _ck_fee_mo * 12 if _ck_fee_mo else 0.0
_ck_c1 = bool(re.search(r"Coinbase One", (_ck_c.get("notes") or "") + (_ck_c.get("min_deposit") or "")))
_ck_k_elig = bool(re.search(r"eligible countries only", _ck_k.get("notes") or "", re.I))
_ck_k_us = bool(UNCONFIRMED.search(_ck_k.get("notes") or ""))
_ck_bond = re.search(r"(\d+(?:\.\d+)?)% APY bonded (\d+) days", _ck_k.get("notes") or "")
_ck_k_min = _ck_k["min_deposit"] if _ck_k["min_deposit"] and _ck_k["min_deposit"].lower() != "unknown" else None
def _ck_auction(r):
    m = re.search(r"Auction (\d{4}-\d{2}-\d{2})", r.get("notes") or "")
    return _fmt_mdy(m.group(1)) if m else _pp_asof(r)
def _ck_tb(r): return f'{r["rate_apy"].strip()}%'
_ck_c_net10 = 10000 * _ck_c["rate"] / 100 - _ck_fee_yr
_ck_c_netpct = _ck_c_net10 / 100
_ck_who_c = ("Coinbase One members only" + (f" (plans start at ${_ck_fee_mo:.2f} a month)" if _ck_fee_mo else "")) if _ck_c1 else "See Coinbase's terms"
_ck_who_k = ('Marked "eligible countries only." Check Kraken\'s terms for your state.' if _ck_k_elig else "See Kraken's terms")
_ck_k_line = (f"Kraken advertises {_pp_r(_ck_k)} on flexible USDC rewards"
              + (", but marks the offer \"eligible countries only.\" Check whether your state qualifies before you count on it." if _ck_k_elig else "."))
_ck_c_line = f"Coinbase pays {_pp_r(_ck_c)}" + (" to Coinbase One members" if _ck_c1 else "") + "."
_ck_faqs = [
    ("Does Coinbase or Kraken pay more on USDC?",
     f"On paper, Kraken: {_pp_r(_ck_k)} on flexible USDC vs {_pp_r(_ck_c)} at Coinbase."
     + (" Kraken limits the offer to eligible countries, so check your state first." if _ck_k_elig else "")
     + (f" Coinbase only pays its rate to Coinbase One members, and plans start at ${_ck_fee_mo:.2f} a month." if _ck_c1 and _ck_fee_mo else "")),
    ("Are USDC rewards FDIC-insured?",
     "No. USDC held at an exchange is not FDIC- or SIPC-insured. You depend on the exchange staying solvent and on USDC holding its $1 peg."),
    ("Do I need Coinbase One to earn USDC rewards?",
     (f"Yes. Coinbase says USDC rewards are for Coinbase One members. Plans start at ${_ck_fee_mo:.2f} a month, about ${_ck_fee_yr:,.0f} a year." if _ck_c1 and _ck_fee_mo
      else "Check Coinbase's USDC page for who qualifies.")),
    ("Is USDC a better place for cash than a savings account?",
     f"For most savers, no. The best FDIC-insured savings rate this week is {_pp_r(_ck_hy)}, with no subscription and no peg risk. T-bills are backed by the US government and their interest is exempt from state and local tax."),
]
_ck_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Coinbase</th><th>Kraken (flexible)</th>"
    "</tr></thead><tbody>"
    f"<tr><td>USDC rewards rate</td><td>{_pp_r(_ck_c)} (as of {_pp_asof(_ck_c)})</td><td>{_pp_r(_ck_k)} advertised (as of {_pp_asof(_ck_k)})</td></tr>"
    f"<tr><td>Who gets it</td><td>{e(_ck_who_c)}</td><td>{e(_ck_who_k)}</td></tr>"
    f"<tr><td>Minimum and cost</td><td>{e(_ck_c['min_deposit'])}</td><td>{e(_ck_k_min) if _ck_k_min else 'Not listed. Check Kraken.'}</td></tr>"
    + f"<tr><td>Risk label</td><td><b>{_ck_c['risk']}</b>: not FDIC or SIPC</td><td><b>{_ck_k['risk']}</b>: not FDIC or SIPC</td></tr>"
    "</tbody></table></div>"
)
_ck_cmp = kv_table(
    ["", "Rate", "As of", "Risk label"],
    [r for r in [
        ["Coinbase USDC rewards", _pp_r(_ck_c), _pp_asof(_ck_c), f"<b>{_ck_c['risk']}</b>"],
        ["Kraken USDC rewards (flexible)", _pp_r(_ck_k) + " advertised", _pp_asof(_ck_k), f"<b>{_ck_k['risk']}</b>"],
        [e(_ck_hy["name"]) + " (top FDIC savings)", _pp_r(_ck_hy) + " APY", _pp_asof(_ck_hy), _ck_hy["risk"]] if _ck_hy else None,
        ["13-week T-bill", _ck_tb(_ck_t13), "Auction " + _ck_auction(_ck_t13), _ck_t13["risk"]] if _ck_t13 else None,
        ["26-week T-bill", _ck_tb(_ck_t26), "Auction " + _ck_auction(_ck_t26), _ck_t26["risk"]] if _ck_t26 else None,
    ] if r],
)
_ck_body = (
    '<div class="prose">'
    f'<p class="lead">{_ck_k_line} {_ck_c_line} Neither one is insured. A USDC balance at an exchange isn\'t a bank deposit, and it isn\'t covered by FDIC or SIPC.</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's USDC rewards</h2></div>"
    + _ck_tbl
    + '<div class="prose">'
    + (f"<p>Coinbase's rate needs a Coinbase One plan. At ${_ck_fee_mo:.2f} a month, that's about ${_ck_fee_yr:,.0f} a year. On $10,000 in USDC for a year, {_pp_r(_ck_c)} pays about ${10000 * _ck_c['rate'] / 100:,.0f}, or about ${_ck_c_net10:,.0f} after the fee. That works out to roughly {_ck_c_netpct:.2f}%. On smaller balances the fee eats a bigger share.</p>" if _ck_c1 and _ck_fee_mo else "")
    + (f"<p>That {_pp_r(_ck_k)} is Kraken's flexible rate. Kraken also lists {_ck_bond.group(1)}% APY for USDC bonded for {_ck_bond.group(2)} days. Read Kraken's terms on eligibility and lock-ups before you move money.</p>" if _ck_bond else "")
    + "<h2>USDC vs FDIC savings and T-bills</h2></div>"
    + _ck_cmp
    + '<div class="prose">'
    + (f"<p>{e(_ck_hy['name'])} pays {_pp_r(_ck_hy)} APY and is FDIC-insured up to $250,000 per depositor, per bank. On $10,000 for a year that's about ${10000 * _ck_hy['rate'] / 100:,.0f}, with no subscription.</p>" if _ck_hy else "")
    + (f"<p>A 26-week T-bill sold at {_ck_tb(_ck_t26)} at its {_ck_auction(_ck_t26)} auction. T-bills are backed by the US government, you can buy them from $100, and the interest is exempt from state and local tax. You lock in the rate until the bill matures. See <a href=\"/t-bills-vs-high-yield-savings/\">T-bills vs high-yield savings</a>.</p>" if _ck_t26 else "")
    + "<h2>Why \"not insured\" matters</h2>"
    "<p>USDC is a token issued by Circle and held for you at the exchange. If the exchange fails or freezes withdrawals, no government program steps in. USDC can also slip below $1. It briefly traded well below $1 in March 2023, when some of its reserves were stuck at Silicon Valley Bank.</p>"
    "<p>Rewards aren't interest. The exchange sets them and can cut or end them at any time. US rules on stablecoin rewards are still being written, so these rates could change or disappear. See <a href=\"/usdc-vs-high-yield-savings/\">USDC vs high-yield savings</a> for the GENIUS Act details.</p>"
    "<h2>Taxes</h2>"
    "<p>Crypto rewards are generally taxable income, and you may need to track them yourself. Savings interest is taxed as ordinary income. T-bill interest is federally taxable but exempt from state and local tax.</p>"
    "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You already pay for Coinbase One:</b> Coinbase's {_pp_r(_ck_c)} costs you nothing extra.</li>" if _ck_c1 else "")
    + ("<li><b>You're in the US and want Kraken's rate:</b> confirm on Kraken's site that your state is eligible first.</li>" if _ck_k_elig else "")
    + "<li><b>It's your emergency fund:</b> use FDIC-insured savings or T-bills. You can get a similar yield without exchange or peg risk.</li>"
    "<li><b>The money is already on an exchange:</b> USDC rewards can make sense if you accept that the balance is not insured.</li>"
    '<li><b>You want every exchange in one place:</b> see <a href="/usdc-rewards/">USDC rewards rates</a>.</li>'
    "</ul>"
    "</div>"
    + faq_block(_ck_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_ck_c["source_url"])}" rel="nofollow">Coinbase USDC page</a> and <a href="{e(_ck_k["source_url"])}" rel="nofollow">Kraken USDC page</a>; savings and T-bill rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/coinbase-vs-kraken-usdc-rewards/") + RISK_EXPLAINER + NEWSLETTER
)
page("/coinbase-vs-kraken-usdc-rewards/",
     f"Coinbase vs Kraken USDC rewards: rates and risks ({MONTH}) | {BRAND}",
     f"Kraken advertises {_pp_r(_ck_k)} on flexible USDC and Coinbase pays {_pp_r(_ck_c)} to Coinbase One members. Neither is insured. How both compare with FDIC savings and T-bills.",
     _ck_body, h1="Coinbase vs Kraken USDC rewards: which pays more?", faqs=_ck_faqs)


# ---- Barclays vs Marcus savings and Interactive Brokers vs Schwab cash interest (added 2026-11-21 build) ----
# Rates, minimums and as-of dates come from rates_seed.csv rows at build time, so the weekly refresh updates them.
# Non-rate facts: Barclays online savings / tiered savings pages (banking.barclaysus.com, read Nov 21, 2026: "up to" tiered APY,
# tier APYs dated 10/07/2026, no monthly fees, no minimum to open, interest compounded daily and credited monthly, Member FDIC via
# Barclays Bank Delaware, phone 8 am-8 pm ET seven days, $200 bonus for new customers through 01/31/2027 with $25,000 held 120 days);
# Marcus facts as on the existing Marcus pages; IBKR interest-rates page (read Nov 21, 2026); Schwab cash-investments page (read Nov 21, 2026).
_BAR_TIER_URL = "https://banking.barclaysus.com/tiered-savings.html"
_bm_b, _bm_m = _get_any("Barclays Tiered Savings"), _get_any("Marcus by Goldman Sachs Online Savings")
_bm_pts, _bm_d10, _bm_d50 = _pp_gap(_bm_b, _bm_m)
_bm_hi = "Barclays" if _bm_b["rate"] > _bm_m["rate"] else ("Marcus" if _bm_m["rate"] > _bm_b["rate"] else None)
_bm_hr, _bm_lr = (_pp_r(_bm_b), _pp_r(_bm_m)) if _bm_hi != "Marcus" else (_pp_r(_bm_m), _pp_r(_bm_b))
_bm_tier = bool(re.search(r"tiered", (_bm_b.get("notes") or "") + _bm_b["name"], re.I))
_bm_upto = "up to " if _bm_tier else ""
_bm_bonus = UPD_ISO <= "2027-01-31"          # Barclays' $200 offer runs through 01/31/2027
_bm_bonus_gap = 25000 * _bm_pts / 100        # one year of the rate gap on the $25,000 the bonus needs
_bm_min_b = "No minimum to open" if (_bm_b["min_deposit"] or "").strip() == "$0" else e(_bm_b["min_deposit"])
_bm_lead_rate = (f"This week {_bm_hi} pays more: {_bm_upto if _bm_hi == 'Barclays' else ''}{_bm_hr} vs {_bm_lr}." if _bm_hi
                 else f"This week they pay the same: {_pp_r(_bm_b)}.")
_bm_faqs = [
    ("Is Barclays or Marcus better for savings?",
     _bm_lead_rate + (" Barclays pays by balance tier, so check its page for the rate on your balance." if _bm_tier else "")
     + " Neither charges monthly fees or needs a minimum to open, and both are FDIC-insured."),
    ("Is Barclays savings FDIC-insured?",
     "Yes. Barclays Online Banking is provided by Barclays Bank Delaware, Member FDIC. Deposits are insured up to the maximum allowed by law."),
    ("How does the Barclays Tiered Savings rate work?",
     "The APY you earn depends on the tier your end-of-day balance falls into. Barclays says the same rate may apply to more than one tier and that tiers can change without notice. Check Barclays' Tiered Savings page for the current tiers."),
] + ([
    ("How does the Barclays $200 savings bonus work?",
     "Barclays says new Tiered Savings customers who open an account by Jan 31, 2027, deposit at least $25,000 within 30 days and keep at least $25,000 in it for 120 days in a row get a $200 bonus, paid about 60 days later and taxed as interest. Current and past Barclays savings or CD customers don't qualify. Check the full terms on Barclays' page."),
] if _bm_bonus else [])
_bm_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Barclays Tiered Savings</th><th>Marcus Online Savings</th>"
    "</tr></thead><tbody>"
    f"<tr><td>APY</td><td>{'Up to ' if _bm_tier else ''}{_pp_r(_bm_b)} (as of {_pp_asof(_bm_b)})</td><td>{_pp_r(_bm_m)} (as of {_pp_asof(_bm_m)})</td></tr>"
    + ("<tr><td>How the rate is set</td><td>By balance tier. Check Barclays' page for the rate on your balance.</td><td>Variable. Maximum balance limits apply.</td></tr>" if _bm_tier
       else "<tr><td>How the rate is set</td><td>Variable</td><td>Variable. Maximum balance limits apply.</td></tr>")
    + (f"<tr><td>New-customer offer</td><td>$200 bonus with $25,000 held 120 days (open by Jan 31, 2027)</td><td>Referral boost: 1.00% for 3 months for you and a new-to-Marcus friend</td></tr>"
       if _bm_bonus else "<tr><td>New-customer offer</td><td>Check Barclays' page</td><td>Referral boost: 1.00% for 3 months for you and a new-to-Marcus friend</td></tr>")
    + "<tr><td>Monthly fees</td><td>None</td><td>None</td></tr>"
    f"<tr><td>Minimum deposit</td><td>{_bm_min_b}</td><td>No minimum deposit</td></tr>"
    f"<tr><td>Insurance</td><td>{_bm_b['risk']} (Barclays Bank Delaware)</td><td>{_bm_m['risk']}</td></tr>"
    "<tr><td>Notable features</td><td>Interest compounded daily, credited monthly; phone help 8 am to 8 pm ET, seven days a week</td><td>Same-day transfers of $100,000 or less when sent by 12 pm ET on a business day</td></tr>"
    "</tbody></table></div>"
)
_bm_body = (
    '<div class="prose">'
    f'<p class="lead">Barclays Tiered Savings and Marcus by Goldman Sachs Online Savings are two big-name online savings accounts with no monthly fees and no minimum to open. Both are FDIC-insured and both rates are variable. {_bm_lead_rate}</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _bm_tbl
    + '<div class="prose">'
    "<p>Rates are from the Barclays and Marcus savings pages."
    + (" Barclays shows an \"up to\" rate and pays by balance tier. Barclays' footnote dates the tier APYs Oct 7, 2026 (checked Nov 21, 2026), so confirm the rate for your balance on its page." if _bm_tier else "")
    + "</p>"
    + (f"<p>On $10,000 for a year, the {_bm_pts:.2f}-point gap is about {_bm_d10}. On $50,000 it's about {_bm_d50}, if rates hold.</p>" if _bm_hi else "<p>On $10,000 for a year, the gap is $0.</p>")
    + ("<h2>The Barclays $200 bonus</h2>"
       "<p>Barclays offers new Tiered Savings customers a $200 bonus through Jan 31, 2027. You need to deposit at least $25,000 within 30 days of opening and keep the balance at $25,000 or more for 120 days in a row. Barclays adds the bonus about 60 days after that, and it's taxed as interest. If you've had a Barclays savings account or CD before, you don't qualify.</p>"
       + (f"<p>For scale: on $25,000, a year of the {_bm_pts:.2f}-point rate gap is about ${_bm_bonus_gap:,.0f}. If you'd park $25,000 anyway, the bonus is worth more than the gap in year one.</p>" if _bm_hi and _bm_bonus_gap < 200 else "")
       if _bm_bonus else "")
    + "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You want the higher rate between the two:</b> {_bm_hi}, at {_bm_upto if _bm_hi == 'Barclays' else ''}{_bm_hr} vs {_bm_lr} this week.</li>" if _bm_hi else "")
    + ("<li><b>You can park $25,000 or more for four months:</b> Barclays, if you're new to it. The $200 bonus needs that balance for 120 days.</li>" if _bm_bonus else "")
    + "<li><b>You have a friend at Marcus:</b> Marcus says a referral can give you both a 1.00% rate boost for 3 months.</li>"
    "<li><b>You move large sums quickly:</b> Marcus processes transfers of $100,000 or less the same day if sent by 12 pm ET on a business day.</li>"
    "<li><b>You like to call someone:</b> Barclays answers the phone 8 am to 8 pm ET, seven days a week.</li>"
    + (f"<li><b>You want the most interest:</b> neither tops our table. On our {_PP_ALT_DATE} check, {_pp_alts()}. Before you chase a headline rate, read <a href=\"/high-yield-savings-promo-vs-base-rate/\">promo rate vs base rate</a>.</li>" if _pp_alts() else "")
    + "</ul>"
    "<h2>Barclays and Marcus vs a money market fund</h2>"
    f'<p>A money market fund at a broker is another place to park cash. {_pp_vmfxx()}Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_bm_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_bm_b["source_url"])}" rel="nofollow">Barclays online savings page</a>, <a href="{_BAR_TIER_URL}" rel="nofollow">Barclays Tiered Savings page</a> (terms read Nov 21, 2026) and <a href="{e(_bm_m["source_url"])}" rel="nofollow">Marcus by Goldman Sachs savings page</a>; rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/barclays-vs-marcus-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/barclays-vs-marcus-savings/",
     f"Barclays vs Marcus: which savings account pays more? ({MONTH}) | {BRAND}",
     f"Barclays Tiered Savings pays {_bm_upto}{_pp_r(_bm_b)} APY this week and Marcus pays {_pp_r(_bm_m)}. Fees, minimums, the Barclays bonus, and the gap in dollars.",
     _bm_body, h1="Barclays vs Marcus: which savings account pays more?", faqs=_bm_faqs)

# ---- Interactive Brokers vs Schwab cash interest ----
_IBKR_URL = "https://www.interactivebrokers.com/en/accounts/fees/pricing-interest-rates.php"
_SCHWAB_CASH_URL = "https://www.schwab.com/cash-investments"
_is_i = _get_any("Interactive Brokers (IBKR Pro) cash interest")
_is_s = _get_any("Schwab brokerage uninvested cash (Bank Sweep)")
_is_w = _get_any("Schwab Prime Advantage Money Fund (SWVXX)")
_is_full = 100000   # IBKR: full rate only with NAV above $100,000
_is_skip = 10000    # IBKR: no interest on the first $10,000 of USD cash
def _is_blend(cash):
    return _is_i["rate"] * max(cash - _is_skip, 0) / cash
def _is_yr(rate, cash): return f"${cash * rate / 100:,.0f}"
_is_rows = []
for _c in (10000, 25000, 50000, 100000):
    _b = _is_blend(_c)
    _is_rows.append([f"${_c:,.0f}", f"{_b:.2f}% ({_is_yr(_b, _c)})", f"{_pp_r(_is_s)} ({_is_yr(_is_s['rate'], _c)})"]
                    + ([f"{_pp_r(_is_w)} ({_is_yr(_is_w['rate'], _c)})"] if _is_w else []))
_is_grid = kv_table(["Cash balance", "IBKR Pro, account over $100,000", "Schwab Bank Sweep", "Schwab SWVXX"] if _is_w
                    else ["Cash balance", "IBKR Pro, account over $100,000", "Schwab Bank Sweep"], _is_rows)
_is_b25 = _is_blend(25000)
_is_lead = (f"Interactive Brokers pays {_pp_r(_is_i)} on uninvested cash and Schwab's default Bank Sweep pays {_pp_r(_is_s)} APY. "
            "The IBKR rate comes with strings: no interest on the first $10,000, and the full rate only if your account is worth more than $100,000.")
_is_faqs = [
    ("Does Interactive Brokers or Schwab pay more on cash?",
     f"Interactive Brokers, by a wide margin, if your cash is above $10,000: {_pp_r(_is_i)} vs {_pp_r(_is_s)} on Schwab's default sweep this week."
     + (f" At Schwab, a money fund such as SWVXX yielded {_pp_r(_is_w)}, so buying one closes the gap." if _is_w else "")),
    ("Does Interactive Brokers pay interest on the first $10,000?",
     "No. IBKR pays no interest on the first $10,000 of uninvested USD cash. Interest is paid only on the settled cash above that, so your blended rate is lower than the headline rate."),
    ("Why is my IBKR interest rate lower than the advertised rate?",
     f"Two reasons. The first $10,000 earns nothing, and the full {_pp_r(_is_i)} applies only to accounts with a net asset value above $100,000. Smaller accounts get a lower rate that rises as the account nears $100,000. IBKR Lite pays less than IBKR Pro. IBKR's interest page has a calculator for your exact rate."),
    ("What does Schwab pay on uninvested cash?",
     f"{_pp_r(_is_s)} APY this week, whether the cash sits in the Bank Sweep or the Schwab One Interest feature. Schwab suggests a money fund such as SWVXX for cash you don't plan to spend soon."),
]
_is_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>Interactive Brokers (IBKR Pro)</th><th>Schwab Bank Sweep (default)</th>"
    "</tr></thead><tbody>"
    f"<tr><td>Rate on uninvested cash</td><td>{_pp_r(_is_i)} annual rate (as of {_pp_asof(_is_i)})</td><td>{_pp_r(_is_s)} APY (as of {_pp_asof(_is_s)})</td></tr>"
    "<tr><td>First $10,000</td><td>Earns no interest</td><td>Earns the sweep rate</td></tr>"
    "<tr><td>Who gets the full rate</td><td>Accounts with a net asset value above $100,000; smaller accounts get less</td><td>Bank Sweep and Schwab One Interest pay the same rate</td></tr>"
    "<tr><td>Plan differences</td><td>IBKR Lite pays the benchmark minus 1.5%, IBKR Pro the benchmark minus 0.5%</td><td>None</td></tr>"
    f"<tr><td>Account minimum</td><td>$0 (no maintenance fee)</td><td>{e(_is_s['min_deposit'])}</td></tr>"
    f"<tr><td>Risk label</td><td>{_is_i['risk']}</td><td>{_is_s['risk']}</td></tr>"
    "</tbody></table></div>"
)
_is_body = (
    '<div class="prose">'
    f'<p class="lead">{_is_lead}</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _is_tbl
    + '<div class="prose">'
    "<p>Rates are from IBKR's interest-rates page and Schwab's cash page. IBKR quotes an annual rate, not an APY, so the two figures aren't measured quite the same way.</p>"
    "<h2>What you'd earn in a year</h2></div>"
    + _is_grid
    + '<div class="prose">'
    f"<p>IBKR figures assume an account worth more than $100,000 and apply {_pp_r(_is_i)} only to the cash above $10,000. On $25,000 that works out to about {_is_b25:.2f}% overall. Smaller IBKR accounts earn less. All figures assume rates hold for the year, which they won't exactly.</p>"
    + "<h2>The Schwab fix: buy a money fund</h2>"
    + (f"<p>Schwab's own cash page points savers to a money fund such as SWVXX, which yielded {_pp_r(_is_w)} (7-day yield as of {_pp_asof(_is_w)}). Schwab says it can be bought in a single trade with no minimum and no transaction fee (its systems need at least $1 per trade), with next-business-day access when you sell. It isn't automatic: new deposits and sale proceeds land back in the sweep. "
       "SWVXX is a prime fund, not FDIC-insured, and covered by SIPC only for custody. The full walk-through is on <a href=\"/schwab-bank-sweep-vs-swvxx/\">Schwab bank sweep vs SWVXX</a>.</p>" if _is_w else "")
    + "<h2>The fine print at IBKR</h2>"
    "<ul>"
    "<li>Interest is paid on settled cash only.</li>"
    "<li>The $10,000 threshold applies to each currency you hold, and balances in separate IBKR accounts aren't combined.</li>"
    "<li>The rate is tied to a benchmark, so it moves when short-term rates move. IBKR can change it at any time.</li>"
    "<li>If your account was opened through another broker, that broker sets your rate, and it may pay less or nothing.</li>"
    "</ul>"
    "<h2>Which one fits</h2>"
    "<ul>"
    "<li><b>You keep well over $10,000 in cash and your account tops $100,000:</b> IBKR pays the higher rate on cash without buying anything.</li>"
    "<li><b>You keep less than $10,000 in cash:</b> IBKR pays nothing on it, and Schwab's sweep pays almost nothing. A money fund or a high-yield savings account will do more.</li>"
    + (f"<li><b>You're happy at Schwab:</b> buy SWVXX or another Schwab money fund. At {_pp_r(_is_w)}, it yields more than IBKR's {_pp_r(_is_i)} on cash this week.</li>" if _is_w and _is_w["rate"] > _is_i["rate"]
       else (f"<li><b>You're happy at Schwab:</b> buy SWVXX or another Schwab money fund, at {_pp_r(_is_w)} this week.</li>" if _is_w else ""))
    + "<li><b>You want every broker side by side:</b> see <a href=\"/best-broker-cash-rates/\">best broker cash rates</a>.</li>"
    "</ul>"
    "</div>"
    + faq_block(_is_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_is_i["source_url"])}" rel="nofollow">Interactive Brokers interest rates page</a> and <a href="{_SCHWAB_CASH_URL}" rel="nofollow">Schwab cash investments page</a> (terms read Nov 21, 2026)'
    + (f', and the <a href="{e(_is_w["source_url"])}" rel="nofollow">Schwab money funds list</a>' if _is_w else "")
    + f'; rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/interactive-brokers-vs-schwab-cash-interest/") + RISK_EXPLAINER + NEWSLETTER
)
page("/interactive-brokers-vs-schwab-cash-interest/",
     f"Interactive Brokers vs Schwab: interest on cash ({MONTH}) | {BRAND}",
     f"IBKR pays {_pp_r(_is_i)} on cash above $10,000; Schwab's default sweep pays {_pp_r(_is_s)} APY. What each pays on $10,000 to $100,000, and the fix at Schwab.",
     _is_body, h1="Interactive Brokers vs Schwab: who pays more on cash?", faqs=_is_faqs)

# ---- SoFi vs Ally savings (added 2026-11-22 daily build) ----
# Rates and the SoFi boost come from rates_seed.csv rows at build time (reuses _sm_s/_sm_boost and _sa_a above).
# Non-rate facts: SoFi savings page (read Nov 22, 2026: no account, service or maintenance fees; no minimum balance; boost for
# members new to SoFi Checking and Savings, needs Eligible Direct Deposit or $5,000 in Qualifying Deposits every 31 days, lasts up
# to 6 months, promotion period through Dec 31, 2027; SoFi Bank, N.A., Member FDIC). Ally savings page (read Nov 22, 2026: no monthly
# maintenance fees or minimum balance, 10 withdrawals per statement cycle, buckets, 24/7 call/chat/email, interest compounded daily).
_sx_s, _sx_a = _sm_s, _sa_a
_sx_pts, _sx_d10, _sx_d50 = _pp_gap(_sx_s, _sx_a)
_sx_hi = "SoFi" if _sx_s["rate"] > _sx_a["rate"] else ("Ally" if _sx_a["rate"] > _sx_s["rate"] else None)
_sx_hr, _sx_lr = (_pp_r(_sx_s), _pp_r(_sx_a)) if _sx_hi != "Ally" else (_pp_r(_sx_a), _pp_r(_sx_s))
_sx_promo = bool(_sm_up) and UPD_ISO <= "2027-12-31"     # SoFi's boost promotion period ends Dec 31, 2027
_sx_up_txt = f"{_sm_up}%" if _sx_promo else ""
_sx_lead_rate = (f"On base rates, {_sx_hi} pays more this week: {_sx_hr} vs {_sx_lr}." if _sx_hi
                 else f"On base rates, they pay the same this week: {_pp_r(_sx_s)}.")
if _sx_promo:
    _sx_boost_gain = dollars(float(_sm_bp) / 2)   # about half a year of the boost on $10,000
_sx_faqs = [
    ("Is SoFi or Ally better for savings?",
     _sx_lead_rate + (f" New SoFi members can get up to {_sx_up_txt} for up to six months with direct deposit or $5,000 in deposits every 31 days." if _sx_promo else "")
     + " Ally's draw is savings buckets and 24/7 support. Neither charges monthly fees or needs a minimum balance."),
    ("How does the SoFi savings boost work?",
     (f"SoFi adds a {_sm_bp}% boost to its {_pp_r(_sx_s)} base rate for members new to SoFi Checking and Savings. You need an eligible direct deposit or $5,000 in qualifying deposits every 31 days. The boost lasts up to six months, then you're back on the base rate. Check SoFi's page for the full terms."
      if _sx_promo else "Our notes show no SoFi boost this week. Check SoFi's page for current offers.")),
    ("Does Ally limit savings withdrawals?",
     "Yes. Ally says there's a limit of 10 withdrawals per statement cycle on its savings account."),
    ("Is my money safe at SoFi or Ally?",
     "Both are FDIC-insured banks. Coverage is up to the legal limit per depositor, per bank, per ownership category."),
]
_sx_tbl = (
    '<div class="tw"><table class="kv"><thead><tr>'
    "<th></th><th>SoFi Savings</th><th>Ally Online Savings</th>"
    "</tr></thead><tbody>"
    f"<tr><td>Base APY</td><td>{_pp_r(_sx_s)} (as of {_pp_asof(_sx_s)})</td><td>{_pp_r(_sx_a)} (as of {_pp_asof(_sx_a)})</td></tr>"
    + (f"<tr><td>Boost</td><td>Up to {_sx_up_txt} for up to 6 months, new members with direct deposit or $5,000 in deposits every 31 days</td><td>None</td></tr>" if _sx_promo
       else "<tr><td>Boost</td><td>Check SoFi's page</td><td>None</td></tr>")
    + "<tr><td>Monthly fees</td><td>No account, service or maintenance fees</td><td>No monthly maintenance fees</td></tr>"
    "<tr><td>Minimum balance</td><td>None</td><td>None</td></tr>"
    f"<tr><td>Insurance</td><td>{_sx_s['risk']} (SoFi Bank, N.A.)</td><td>{_sx_a['risk']}</td></tr>"
    "<tr><td>Notable features</td><td>Savings and checking opened together</td><td>Savings \"buckets\"; 24/7 call, chat or email; 10 withdrawals per statement cycle</td></tr>"
    "</tbody></table></div>"
)
_sx_body = (
    '<div class="prose">'
    f'<p class="lead">SoFi Savings and Ally Online Savings are both online savings accounts. Both are FDIC-insured, both have no monthly fees and no minimum balance, and both rates are variable. {_sx_lead_rate}</p>'
    "<p>Not financial advice.</p>"
    f'<p class="upd">Updated {UPD_S}</p>'
    "<h2>This week's rates</h2></div>"
    + _sx_tbl
    + '<div class="prose">'
    "<p>Rates are from each bank's own savings page.</p>"
    + (f"<p>On base rates, the {_sx_pts:.2f}-point gap is about {_sx_d10} a year on $10,000 and about {_sx_d50} on $50,000, if rates hold.</p>" if _sx_hi else "<p>On base rates, the gap is $0.</p>")
    + ("<h2>The SoFi boost</h2>"
       f"<p>SoFi advertises up to {_sx_up_txt} APY for new members: the {_pp_r(_sx_s)} base rate plus a {_sm_bp}% boost. To keep it, you need an eligible direct deposit or $5,000 in qualifying deposits every 31 days. Miss a month and you earn the base rate for that period. The boost lasts up to six months, so on $10,000 it's worth about {_sx_boost_gain} in total before you drop back to {_pp_r(_sx_s)}.</p>"
       if _sx_promo else "")
    + "<h2>Which one fits</h2>"
    "<ul>"
    + (f"<li><b>You want the higher base rate between the two:</b> {_sx_hi}, at {_sx_hr} vs {_sx_lr} this week.</li>" if _sx_hi else "")
    + (f"<li><b>You can move your paycheck:</b> SoFi's up-to {_sx_up_txt} rate pays more for up to six months if you're new to SoFi.</li>" if _sx_promo else "")
    + "<li><b>You like splitting savings into goals:</b> Ally does it with buckets inside one account.</li>"
    "<li><b>You want help at any hour:</b> Ally offers 24/7 phone, chat and email.</li>"
    "<li><b>You pull money out often:</b> Ally limits savings to 10 withdrawals per statement cycle, so check how often you'd move money.</li>"
    f'<li><b>You want the most interest without conditions:</b> neither base rate tops our table. On our {_PP_ALT_DATE} check, {_pp_alts()}. Before you chase a headline rate, read <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</li>'
    "</ul>"
    "<h2>SoFi and Ally vs a money market fund</h2>"
    f'<p>If your money will sit for a while, a money market fund at a broker can pay more than either base rate. {_pp_vmfxx()}Money funds aren\'t FDIC-insured, though. See <a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market fund</a> for the trade-offs.</p>'
    "</div>"
    + faq_block(_sx_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_sx_s["source_url"])}" rel="nofollow">SoFi savings page</a> and <a href="{e(_sx_a["source_url"])}" rel="nofollow">Ally Bank savings page</a> (terms read Nov 22, 2026); rates from the CashRate Watch {UPD_S} refresh. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/sofi-vs-ally-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/sofi-vs-ally-savings/",
     f"SoFi vs Ally savings: which pays more? ({MONTH}) | {BRAND}",
     f"SoFi's base savings rate is {_pp_r(_sx_s)} APY this week and Ally pays {_pp_r(_sx_a)}"
     + (f". New SoFi members can get up to {_sx_up_txt} with a boost" if _sx_promo else "") + ". Fees, limits and the gap in dollars.",
     _sx_body, h1="SoFi vs Ally savings: which pays more?", faqs=_sx_faqs)


# ---- CD and fixed-term rates (added 2026-11-23 daily build) ----
# CD rows live in cd_seed.csv (NOT rates_seed.csv), so the home table, rates.json, rate history and the other
# category tables are unchanged. The weekly refresh updates cd_seed.csv: one row per bank + term, read from each
# bank's own CD page (rate_apy, min_deposit, early_withdrawal if published, source_url, fetched_at, as_of, notes).
# Rows with no APY are skipped; never estimate. Non-rate facts read on the banks' CD pages Nov 23, 2026:
# Marcus ($500 minimum, compounds daily, credited monthly, 10-day CD rate guarantee, penalty details in its CD FAQs);
# Bread Savings ($1,500 minimum, $1,000,000 maximum, penalty 90/180/365 days simple interest by term);
# EverBank ($1,000 minimum; 7-, 13- and 25-month CDs renew into 6-, 12- and 24-month CDs; penalty not on the page).
CD_CSV = os.path.join(os.path.dirname(os.path.abspath(args.csv)), "cd_seed.csv")
CD = []
if os.path.exists(CD_CSV):
    with open(CD_CSV, newline="") as f:
        for r in csv.DictReader(f):
            try: r["rate"] = float(r["rate_apy"])
            except ValueError: continue
            r["months"] = int(r["term_months"])
            CD.append(r)
if len(CD) < 1:
    raise SystemExit("cd_seed.csv has no CD rows with an APY; /cd-rates/ would be empty")
CD.sort(key=lambda r: (r["months"], -r["rate"], r["bank"]))
_cd_banks = []
for r in sorted(CD, key=lambda r: -r["rate"]):
    if r["bank"] not in _cd_banks: _cd_banks.append(r["bank"])
_cd_short = {"Marcus by Goldman Sachs": "Marcus", "Bread Savings": "Bread Savings", "EverBank": "EverBank"}
def _cd_bn(b): return _cd_short.get(b, b)
def _cd_d(iso): return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
_cd_upd_iso = max(r["fetched_at"][:10] for r in CD)
_cd_upd = _cd_d(_cd_upd_iso)
def _cd_join(xs): return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
def _cd_top(bank):
    rs = [r for r in CD if r["bank"] == bank]; hi = max(r["rate"] for r in rs)
    return rs, hi, [r["term_label"] for r in rs if r["rate"] == hi]
def _cd_tl(mo): return (f"{mo // 12} year" + ("s" if mo > 12 else "")) if mo % 12 == 0 else f"{mo} months"
_cd_lead_bits = []
for i, b in enumerate(_cd_banks):
    rs, hi, terms = _cd_top(b)
    tms = [r["months"] for r in rs if r["rate"] == hi]
    where = (f"on terms from {_cd_tl(min(tms))} to {_cd_tl(max(tms))}" if len(tms) > 2
             else f"on its {_cd_join(terms)} {'CD' if len(terms) == 1 else 'CDs'}")
    _cd_lead_bits.append(f"{_cd_bn(b)} pays {hi:.2f}%{' APY' if i == 0 else ''} {where}")
_cd_lead_rate = _cd_lead_bits[0] + " this week." + ((" " + _cd_join(_cd_lead_bits[1:]) + ".") if len(_cd_lead_bits) > 1 else "")
_cd_ranges = []
for b in _cd_banks:
    rs = [r for r in CD if r["bank"] == b]
    lo, hi = min(r["rate"] for r in rs), max(r["rate"] for r in rs)
    _cd_ranges.append(f"{_cd_bn(b)} pays {lo:.2f}% to {hi:.2f}% on terms from {_cd_tl(rs[0]['months'])} to {_cd_tl(rs[-1]['months'])}")
_cd_hy = best("hysa")
def _cd_pen(r):
    p = (r.get("early_withdrawal") or "").strip()
    return e(p) if p else "Confirm on the bank's CD page"
def _cd_row(r):
    note = []
    if r["as_of"] and r["as_of"] != r["fetched_at"][:10]: note.append(f"APY as of {_cd_d(r['as_of'])}")
    m = re.search(r"Renews into an? [\w-]+ CD at maturity", r["notes"] or "")
    if m: note.append(m.group(0))
    sm = f'<small>{e("; ".join(note))}</small>' if note else ""
    return (f'<tr><td>{e(r["term_label"])}</td><td>{e(_cd_bn(r["bank"]))} {e(r["product"])}{sm}</td><td class="apy">{e(r["rate_apy"])}%</td>'
            f'<td><span class="b b-FDIC">FDIC</span></td><td>{e(r["min_deposit"])}</td><td>{_cd_pen(r)}</td>'
            f'<td><a href="{e(r["source_url"])}" rel="nofollow noopener" target="_blank">source</a></td></tr>')
_cd_tbl = ('<div class="tw"><table><thead><tr><th>Term</th><th>Bank and CD</th><th>APY</th><th>Risk label</th><th>Minimum</th>'
           '<th>Early withdrawal penalty</th><th>Source</th></tr></thead><tbody>' + "".join(_cd_row(r) for r in CD) + '</tbody></table></div>')
_cd_asof = []
for b in _cd_banks:
    r0 = next(r for r in CD if r["bank"] == b)
    _cd_asof.append(f"{_cd_bn(b)} {'as of ' + _cd_d(r0['as_of']) if r0['as_of'] else 'checked ' + _cd_d(r0['fetched_at'])}")
_cd_note = (f'<p class="foot-n">APY dates: {e("; ".join(_cd_asof))}. A CD\'s APY is fixed once you open and fund it, '
            "but the bank can change its offered rate before then. Confirm on the bank's site. Not financial advice.</p>")
_cd_by_term = []
for mo in (3, 6, 12, 18, 24, 36, 60):
    rs = [r for r in CD if r["months"] == mo]
    if rs:
        top = max(rs, key=lambda r: r["rate"]); hi = [r for r in rs if r["rate"] == top["rate"]]
        _cd_by_term.append(f"<li><b>{e(top['term_label'])}:</b> {e(_cd_join([_cd_bn(r['bank']) for r in hi]))} at {e(top['rate_apy'])}%"
                           + (f" (of {len(rs)} banks listed)" if len(rs) > 1 else "") + "</li>")
_cd_bread = [r for r in CD if r["bank"] == "Bread Savings"]
_cd_pen_faq = ("Most banks charge a penalty, usually a set number of days of interest. "
               + ("Bread Savings charges 90 days of simple interest on terms under 1 year, 180 days on 12 months to 3 years, and 365 days on 4 years and up. " if _cd_bread else "")
               + "Check each bank's CD page or account agreement for its exact rule.")
_cd_faqs = [
    ("What are this week's CD APYs, and for which terms?",
     ". ".join(_cd_ranges) + f". Checked {_cd_upd}; confirm on each bank's page before you open a CD."),
    ("Is a fixed-term bond the same thing as a CD?",
     "In US retail banking, yes for most searches. A fixed-term or fixed-rate bond at a bank is a time deposit, which US banks call a certificate of deposit (CD). It's not a Treasury bill, an I bond or a corporate bond."),
    ("How is a CD different from a high-yield savings account?",
     "A savings account lets you take money out any day, and its APY can change at any time. A CD's APY is fixed for the term if you hold it to maturity, and taking money out early usually costs a penalty."),
    ("Are CDs FDIC-insured?",
     "CDs at an FDIC member bank are insured up to $250,000 per depositor, per bank, per ownership category, the same as savings. Confirm the bank's FDIC membership on its page."),
    ("What happens if I take the money out before the term ends?", _cd_pen_faq),
    ("How do CD rates compare to T-bills or I bonds this week?",
     "They're different products. T-bills are Treasury debt sold at auction, and their interest is exempt from state and local income tax. I bonds are US savings bonds with a rate tied partly to inflation. See the T-bill rates and I bonds vs T-bills pages for those numbers."),
    ("Why aren't Morgan Stanley or Wells Fargo CDs listed?",
     "Morgan Stanley, Wells Fargo and Encore Bank CDs aren't in this week's table. Check the bank's own CD page for its current APYs."),
]
_cd_body = (
    '<div class="prose">'
    f'<p class="lead">{e(_cd_lead_rate)} A CD is a bank deposit you agree to leave for a set term, with an APY that stays fixed if you hold it to maturity. '
    'In US banking, a "fixed-term bond" usually means the same thing. It\'s not a Treasury bill or an I bond.</p>'
    f'<p class="upd">Updated {_cd_upd} &middot; {len(CD)} CD terms at {len(_cd_banks)} FDIC banks</p>'
    "<p>Not financial advice.</p>"
    "<h2>This week's CD APYs by term</h2></div>"
    + _cd_tbl + _cd_note
    + '<div class="prose">'
    + ("<h2>Highest APY at common terms</h2><ul>" + "".join(_cd_by_term) + "</ul>" if _cd_by_term else "")
    + "<h2>CD vs high-yield savings</h2>"
    "<p>With a <a href=\"/savings/\">high-yield savings account</a> you can take the money any day, but the APY can change at any time"
    + (f". The top savings APY this week is {e(_cd_hy['name'])} at {e(_cd_hy['rate_apy'])}% ({UPD_S} check)." if _cd_hy else ".")
    + "</p><p>With a CD, the APY is locked for the term if you hold to maturity. Taking money out early usually costs a penalty, and at maturity "
    "the CD renews at whatever rate the bank offers then unless you choose otherwise. If a CD rate is a short promo or tied to another account, "
    'read it the same way as a <a href="/high-yield-savings-promo-vs-base-rate/">promo savings rate vs the base rate</a>. Side by side: <a href="/cd-vs-high-yield-savings/">CD vs high-yield savings</a>.</p>'
    "<h2>Fixed-term bonds, Treasuries and I bonds</h2>"
    "<p>A fixed-term or fixed-rate bond at a bank is a CD. Treasury bills are a different product: Treasury-backed, sold at auction, and exempt from state "
    'and local income tax. See <a href="/treasury-bills/">this week\'s T-bill auction rates</a> and <a href="/t-bills-vs-high-yield-savings/">T-bills vs high-yield savings</a>. '
    'For inflation-linked savings bonds, see <a href="/i-bonds-vs-t-bills/">I bonds vs T-bills</a>. Matched terms: <a href="/cds-vs-t-bills/">CDs vs T-bills</a>.</p>'
    "<h2>Insurance</h2>"
    '<p>CDs at an FDIC member bank are deposits, insured up to $250,000 per depositor, per bank, per ownership category (<a href="https://www.fdic.gov/resources/deposit-insurance" rel="nofollow noopener">FDIC</a>). '
    "Brokered CDs bought through a brokerage work differently: check how they're held and what happens if you sell before maturity.</p>"
    "<h2>Minimums, compounding and penalties</h2><ul>"
    + ("<li><b>Marcus:</b> $500 minimum to open and earn the stated APY. Interest compounds daily and is credited monthly. If the rate for your term rises within 10 days of opening, you get the higher rate. Marcus charges an early withdrawal penalty; check its CD FAQs for the amount.</li>" if any(r["bank"] == "Marcus by Goldman Sachs" for r in CD) else "")
    + ("<li><b>Bread Savings:</b> $1,500 minimum in a single deposit, up to $1,000,000 per account. Early withdrawal costs 90 days of simple interest on terms under 1 year, 180 days on 12 months to 3 years, and 365 days on 4 years and up.</li>" if _cd_bread else "")
    + ("<li><b>EverBank:</b> $1,000 minimum to open. The 7-, 13- and 25-month CDs renew into 6-, 12- and 24-month CDs at maturity. Confirm the early withdrawal penalty on EverBank's CD page.</li>" if any(r["bank"] == "EverBank" for r in CD) else "")
    + "</ul>"
    "<h2>Which one fits</h2><ul>"
    '<li><b>You may need the money on any day:</b> <a href="/savings/">savings</a> or a <a href="/money-market/">money market fund</a> is built for that. Side by side: <a href="/cd-vs-money-market-fund/">CD vs money market fund</a>.</li>'
    "<li><b>You can leave it until a set date and want a rate that won't change:</b> that's what a CD does. Bank vs bank: <a href=\"/marcus-vs-bread-savings-cd/\">Marcus vs Bread Savings CD rates</a>.</li>"
    '<li><b>You pay state income tax and can hold to a date:</b> compare <a href="/treasury-bills/">T-bill auction rates</a> too, since T-bill interest skips state tax.</li>'
    "</ul></div>"
    + faq_block(_cd_faqs)
    + '<div class="prose"><p>Sources: ' + ", ".join(f'<a href="{e(u)}" rel="nofollow">{e(_cd_bn(b))} CD page</a>' for b, u in
        [(b, next(r["source_url"] for r in CD if r["bank"] == b)) for b in _cd_banks])
    + f" (read {_cd_upd}). Published by Sparta Labs. Not financial advice.</p></div>"
    + related("/cd-rates/") + RISK_EXPLAINER + NEWSLETTER
)
_cd_desc = (" ".join(f"{_cd_bn(b)} up to {_cd_top(b)[1]:.2f}%" + ("," if i < len(_cd_banks) - 2 else (" and" if i == len(_cd_banks) - 2 else ""))
                     for i, b in enumerate(_cd_banks)))
page("/cd-rates/",
     f"CD rates this week: fixed-term APYs by term | {BRAND}",
     f"CD APYs this week: {_cd_desc}. Rates by term with FDIC labels and early withdrawal penalties.",
     _cd_body, h1="CD and fixed-term rates", faqs=_cd_faqs)


# ---- CDs vs T-bills and CD vs high-yield savings (added 2026-11-25 daily build) ----
# Rates come from cd_seed.csv (CD list above) and rates_seed.csv rows at build time; no figures are typed in here.
# Non-rate facts are the ones already checked in affiliates/factcheck_2026-10-27.md: T-bill interest is exempt from state
# and local income tax (IRS Topic 403); TreasuryDirect 45-day hold before a transfer to a broker (31 CFR 363.203);
# state-tax-equivalent = rate / (1 - state rate) is a floor (row 6 hedge); FDIC $250,000 per depositor, per bank, per
# ownership category. CD minimums and penalties as read on the banks' CD pages Nov 23, 2026 (see the /cd-rates/ block).
_ct_pairs = [(3, "US T-Bill 13-week", "13-week"), (6, "US T-Bill 26-week", "26-week"), (12, "US T-Bill 52-week", "52-week")]
_ct_rows = []
for _mo, _tn, _tl in _ct_pairs:
    _tb = _get_any(_tn)
    _cds = [r for r in CD if r["months"] == _mo]
    if not _tb or _tb.get("rate") is None or not _cds: continue
    _top = max(_cds, key=lambda r: r["rate"])
    _ct_rows.append({"mo": _mo, "tl": _tl, "tb": _tb, "top": _top, "cds": _cds})
if not _ct_rows:
    raise SystemExit("cds-vs-t-bills: no CD term matches a T-bill term")
def _ct_cdlab(mo): return {3: "3-month", 6: "6-month", 12: "1-year"}.get(mo, f"{mo}-month")
_ct_tb_wins = [x for x in _ct_rows if x["tb"]["rate"] > x["top"]["rate"]]
_ct_cd_wins = [x for x in _ct_rows if x["top"]["rate"] > x["tb"]["rate"]]
_ct_last = _ct_rows[-1]
_ct_gap = _ct_last["tb"]["rate"] - _ct_last["top"]["rate"]
_ct_lead = (f'The {_ct_last["tl"]} T-bill sold at {_ct_last["tb"]["rate"]:.3f}% at the {auction_on(_ct_last["tb"])} auction. '
            f'The best {_ct_cdlab(_ct_last["mo"])} CD here pays {_ct_last["top"]["rate"]:.2f}% APY at {_cd_bn(_ct_last["top"]["bank"])}.')
if len(_ct_tb_wins) == len(_ct_rows):
    _ct_verdict = f"T-bills pay more at all {len(_ct_rows)} matched terms this week, before you count the state tax break."
elif len(_ct_cd_wins) == len(_ct_rows):
    _ct_verdict = f"CDs pay more at all {len(_ct_rows)} matched terms this week, before you count the T-bill state tax break."
else:
    _ct_verdict = ("T-bills pay more at " + _cd_join([x["tl"] for x in _ct_tb_wins] or ["no term"]) + " and CDs pay more at "
                   + _cd_join([_ct_cdlab(x["mo"]) for x in _ct_cd_wins] or ["no term"]) + " this week, before the state tax break.")
_ct_long = max(CD, key=lambda r: (r["rate"], r["months"]))
_ct_st = 0.05
_ct_teq = _ct_last["tb"]["rate"] / (1 - _ct_st)
def _ct_tr(x):
    others = sorted((r for r in x["cds"] if r is not x["top"]), key=lambda r: -r["rate"])
    oth = "; ".join(f'{_cd_bn(r["bank"])} {r["rate"]:.2f}%' for r in others) or "None listed"
    g = x["tb"]["rate"] - x["top"]["rate"]
    gtxt = f'T-bill +{g:.2f} pts' if g > 0 else (f'CD +{-g:.2f} pts' if g < 0 else "Even")
    return (f'<tr><td>{e(x["tl"])} bill vs {e(_ct_cdlab(x["mo"]))} CD</td>'
            f'<td class="apy">{x["tb"]["rate"]:.3f}%<small>Auction {e(auction_on(x["tb"]) or "")}</small></td>'
            f'<td class="apy">{x["top"]["rate"]:.2f}%<small>{e(_cd_bn(x["top"]["bank"]))}</small></td>'
            f'<td>{e(oth)}</td><td>{e(gtxt)}</td></tr>')
_ct_tbl = ('<div class="tw"><table><thead><tr><th>Term</th><th>T-bill rate</th><th>Best CD APY</th><th>Other CDs</th><th>Gap</th></tr></thead><tbody>'
           + "".join(_ct_tr(x) for x in _ct_rows) + "</tbody></table></div>"
           + f'<p class="foot-n">T-bill rates are each term\'s latest auction high investment rate (a bond-equivalent yield) from TreasuryDirect. '
           f'CD APYs are from each bank\'s CD page, checked {_cd_upd}. The two measures are close but not identical. Confirm with the provider. Not financial advice.</p>')
_ct_bread12 = next((r for r in CD if r["bank"] == "Bread Savings" and r["months"] == 12), None)
_ct_faqs = [
    ("Do CDs or T-bills pay more right now?",
     f"{_ct_lead} {_ct_verdict} CDs run longer, though: {_cd_bn(_ct_long['bank'])} pays {_ct_long['rate']:.2f}% on its {_ct_long['term_label']} CD, and T-bills stop at 52 weeks."),
    ("Is T-bill interest taxed differently from CD interest?",
     "Yes. Both are taxed federally. T-bill interest is exempt from state and local income tax, and CD interest isn't. In a state with an income tax, that gap favors T-bills."),
    ("Are T-bills safer than CDs?",
     "Both are very safe. CDs at an FDIC member bank are insured up to $250,000 per depositor, per bank, per ownership category. T-bills are direct loans to the US Treasury, with no insurance cap because the Treasury pays you."),
    ("What if I need the money before the term ends?",
     "A CD usually charges an early withdrawal penalty, often a set number of days of interest. A T-bill can be sold at a broker before it matures, at a price that can move a little. At TreasuryDirect you must hold a bill 45 days before you can move it to a broker to sell."),
    ("What's the minimum to buy a T-bill or open a CD?",
     "T-bills sell in $100 steps. CD minimums vary: Marcus asks $500, EverBank $1,000 and Bread Savings $1,500."),
]
_ct_body = (
    '<div class="prose">'
    f'<p class="lead">{e(_ct_lead)} {e(_ct_verdict)}</p>'
    f'<p class="upd">Updated {UPD_S} &middot; {len(_ct_rows)} matched terms</p>'
    "<p>Not financial advice.</p>"
    "<h2>T-bills vs CDs at matching terms</h2></div>"
    + _ct_tbl
    + '<div class="prose">'
    + (f"<p>On the {_ct_cdlab(_ct_last['mo'])} term, the {abs(_ct_gap):.2f}-point gap is about {dollars(abs(_ct_gap))} a year on $10,000 and about {dollars(abs(_ct_gap), 50000)} on $50,000.</p>" if _ct_gap else "")
    + "<h2>The state tax break</h2>"
    f"<p>T-bill interest skips state and local income tax. Say your state takes {_ct_st * 100:.0f}%. A {_ct_last['tb']['rate']:.3f}% {_ct_last['tl']} bill then matches a CD paying about {_ct_teq:.2f}%, "
    f"and the best {_ct_cdlab(_ct_last['mo'])} CD here pays {_ct_last['top']['rate']:.2f}%. That leaves out federal tax; if you don't deduct state income tax on your federal return, the CD would need a little more. "
    "In a state with no income tax, there's no tax difference.</p>"
    "<h2>Getting your money out early</h2><ul>"
    + (f"<li><b>CDs:</b> expect a penalty. Bread Savings charges 180 days of simple interest on its 1-year CD; on $10,000 at {_ct_bread12['rate']:.2f}% that's about ${10000 * _ct_bread12['rate'] / 100 * 180 / 365:,.0f}. Confirm Marcus's and EverBank's penalties with each bank.</li>" if _ct_bread12
       else "<li><b>CDs:</b> expect a penalty, usually a set number of days of interest. Check the bank's terms.</li>")
    + "<li><b>T-bills at a broker:</b> you can sell before maturity. The price can move a little, so you may get slightly more or less than you paid plus interest.</li>"
    "<li><b>T-bills at TreasuryDirect:</b> you can't sell there. You must hold a bill 45 days before moving it to a broker to sell.</li></ul>"
    "<h2>Longer than a year</h2>"
    f"<p>T-bills top out at 52 weeks. CDs here run to {_cd_tl(max(r['months'] for r in CD))}, and the best long-term APY is {_ct_long['rate']:.2f}% at {_cd_bn(_ct_long['bank'])} ({_ct_long['term_label']}). "
    'For a Treasury that runs past a year, look at Treasury notes on TreasuryDirect. See all terms on <a href="/cd-rates/">CD rates this week</a>.</p>'
    "<h2>Which one fits</h2><ul>"
    "<li><b>You pay state income tax and can hold up to a year:</b> T-bills, unless a CD beats the tax-adjusted rate.</li>"
    "<li><b>You want one rate locked for two to five years:</b> a CD, since T-bills stop at 52 weeks.</li>"
    "<li><b>You might need the cash before maturity:</b> T-bills held at a broker are easier to sell than a CD is to break. For daily access, see <a href=\"/savings/\">savings</a> or <a href=\"/money-market/\">money market funds</a>.</li>"
    "<li><b>You'd rather stay at a bank:</b> a CD keeps it in one place with FDIC insurance.</li></ul>"
    '<p>More: <a href="/treasury-bills/">this week\'s T-bill auction rates</a>, <a href="/t-bills-vs-high-yield-savings/">T-bills vs high-yield savings</a> and <a href="/cd-vs-high-yield-savings/">CD vs high-yield savings</a>.</p>'
    "</div>"
    + faq_block(_ct_faqs)
    + '<div class="prose"><p>Sources: T-bill results from TreasuryDirect ('
    + ", ".join(f'<a href="{e(x["tb"]["source_url"])}" rel="nofollow">{e(x["tl"])}</a>' for x in _ct_rows)
    + "); " + ", ".join(f'<a href="{e(u)}" rel="nofollow">{e(_cd_bn(b))} CD page</a>' for b, u in
        [(b, next(r["source_url"] for r in CD if r["bank"] == b)) for b in _cd_banks])
    + f' (read {_cd_upd}); tax rule from <a href="https://www.irs.gov/taxtopics/tc403" rel="nofollow">IRS Topic 403</a>. Published by Sparta Labs. Not financial advice.</p></div>'
    + related("/cds-vs-t-bills/") + RISK_EXPLAINER + NEWSLETTER
)
page("/cds-vs-t-bills/",
     f"CDs vs T-bills: which pays more this week? ({MONTH}) | {BRAND}",
     f"The {_ct_last['tl']} T-bill sold at {_ct_last['tb']['rate']:.3f}% and the best {_ct_cdlab(_ct_last['mo'])} CD pays {_ct_last['top']['rate']:.2f}% APY. "
     "Matched terms, the state tax break, early exit and minimums.",
     _ct_body, h1="CDs vs T-bills: which pays more?", faqs=_ct_faqs)

# CD vs high-yield savings
_cs_hy = [r for r in SHOWN if r["category"] == "hysa"][:3]
if not _cs_hy:
    raise SystemExit("cd-vs-high-yield-savings: no savings rows")
_cs_top = _cs_hy[0]
def _cs_lock(r):
    m = re.search(r"(\d+)-day rate lock", r.get("notes") or "")
    return f"{m.group(1)}-day rate lock for new Raisin users, then variable" if m and "Raisin" in r["name"] else "Variable"
_cs_terms = []
for _mo in (6, 12, 18, 24, 60):
    _rs = [r for r in CD if r["months"] == _mo]
    if _rs: _cs_terms.append(max(_rs, key=lambda r: (r["rate"], r["bank"] == "Marcus by Goldman Sachs")))
_cs_12 = next((r for r in _cs_terms if r["months"] == 12), None)
_cs_best = max(CD, key=lambda r: (r["rate"], -r["months"]))
def _cs_name(r): return r["name"].replace(" via Raisin", "").replace("Western Alliance Bank High-Yield Savings", "Western Alliance (via Raisin)")
def _cs_gap(rate):
    g = rate - _cs_top["rate"]
    return f"+{g:.2f} pts" if g > 0 else (f"{g:.2f} pts" if g < 0 else "Even")
_cs_tbl = ('<div class="tw"><table><thead><tr><th>Option</th><th>Best APY</th><th>Rate fixed?</th><th>vs top savings</th><th>Interest on $10,000, first year</th></tr></thead><tbody>'
           + f'<tr><td>Savings, any-day access</td><td class="apy">{_cs_top["rate"]:.2f}%<small>{e(_cs_name(_cs_top))}</small></td><td>{e(_cs_lock(_cs_top))}</td><td>Top savings</td><td>${10000 * _cs_top["rate"] / 100:,.0f} if the rate holds</td></tr>'
           + "".join(f'<tr><td>{e(r["term_label"])} CD</td><td class="apy">{r["rate"]:.2f}%<small>{e(_cd_bn(r["bank"]))}</small></td><td>Fixed for the term</td>'
                     f'<td>{_cs_gap(r["rate"])}</td><td>${10000 * r["rate"] / 100:,.0f}</td></tr>' for r in _cs_terms)
           + "</tbody></table></div>"
           + f'<p class="foot-n">Savings APYs from the {UPD_S} check; CD APYs from each bank\'s CD page, checked {_cd_upd}. First-year interest assumes the APY over 12 months. Confirm with the bank. Not financial advice.</p>')
_cs_same = []
for _bk, _sn in (("Marcus by Goldman Sachs", "Marcus by Goldman Sachs Online Savings"), ("EverBank", "EverBank Performance Savings")):
    _sv = _get_any(_sn)
    _cd12 = next((r for r in CD if r["bank"] == _bk and r["months"] == 12), None)
    _cdhi = max((r for r in CD if r["bank"] == _bk), key=lambda r: r["rate"], default=None)
    if _sv and _sv.get("rate") is not None and _cd12 and _cdhi:
        _cs_same.append((_bk, _sv, _cd12, _cdhi))
def _cs_same_li(bk, sv, cd12, cdhi):
    g = cd12["rate"] - sv["rate"]
    s = f"<li><b>{e(_cd_bn(bk))}:</b> savings {sv['rate']:.2f}%, 1-year CD {cd12['rate']:.2f}%. "
    if g > 0: s += f"The CD pays about {dollars(g)} more on $10,000 over a year."
    elif g < 0: s += f"The savings account pays more than the 1-year CD right now, by about {dollars(-g)} a year on $10,000."
    else: s += "They pay the same."
    if cdhi["rate"] > cd12["rate"]:
        hs = [r for r in CD if r["bank"] == bk and r["rate"] == cdhi["rate"]]
        s += (f" Its best CD rate is {cdhi['rate']:.2f}% on terms from {e(hs[0]['term_label'])} to {e(hs[-1]['term_label'])}." if len(hs) > 1
              else f" Its best CD is the {e(cdhi['term_label'])} at {cdhi['rate']:.2f}%.")
    return s + "</li>"
def _cs_cond(r):
    n = r.get("notes") or ""
    m = re.search(r"with \$(\d+)\+? in deposits per statement cycle, otherwise (\d+\.\d+)%", n)
    if m: return f" with ${m.group(1)} in deposits each statement cycle, otherwise {m.group(2)}%"
    if re.search(r"\d+-day rate lock", n) and "Raisin" in r["name"]: return " (rate lock for new Raisin users, then variable)"
    return ""
_cs_bread12 = next((r for r in CD if r["bank"] == "Bread Savings" and r["months"] == 12), None)
_cs_lead = (f"The top savings APY this week is {_cs_top['rate']:.2f}% at {_cs_name(_cs_top)}"
            + (f", and the best 1-year CD pays {_cs_12['rate']:.2f}% at {_cd_bn(_cs_12['bank'])}." if _cs_12 else ".")
            + f" Lock in for {_cd_tl(_cs_best['months'])} and {_cd_bn(_cs_best['bank'])} pays {_cs_best['rate']:.2f}%.")
_cs_faqs = [
    ("Is a CD or high-yield savings better right now?",
     _cs_lead + " Savings lets you take money out any day; a CD fixes the rate but charges a penalty if you leave early."),
    ("Can my savings rate drop?",
     "Yes. A savings APY is variable and the bank can change it at any time. A CD's APY is fixed for the term if you hold it to maturity."),
    ("Are CDs and savings accounts both FDIC-insured?",
     "Yes, at an FDIC member bank. Coverage is up to $250,000 per depositor, per bank, per ownership category, and your savings and CDs at the same bank count toward the same limit."),
    ("What happens when a CD matures?",
     "Most CDs renew at the bank's rate on that day unless you tell the bank otherwise. EverBank's 7-, 13- and 25-month CDs renew into 6-, 12- and 24-month CDs."),
    ("How much does it cost to break a CD early?",
     (f"It depends on the bank. Bread Savings charges 180 days of simple interest on its 1-year CD, about ${10000 * _cs_bread12['rate'] / 100 * 180 / 365:,.0f} on $10,000 at {_cs_bread12['rate']:.2f}%. Check each bank's terms."
      if _cs_bread12 else "It depends on the bank, usually a set number of days of interest. Check each bank's terms.")),
]
_cs_body = (
    '<div class="prose">'
    f'<p class="lead">{e(_cs_lead)} Savings lets you take money out any day. A CD fixes the rate for the term.</p>'
    f'<p class="upd">Updated {UPD_S} &middot; top savings vs {len(_cs_terms)} CD terms</p>'
    "<p>Not financial advice.</p>"
    "<h2>Best savings APY vs best CD APYs</h2></div>"
    + _cs_tbl
    + '<div class="prose">'
    + ("<h2>Same bank, savings vs CD</h2><ul>" + "".join(_cs_same_li(*x) for x in _cs_same) + "</ul>" if _cs_same else "")
    + "<h2>Other savings rates this week</h2><ul>"
    + "".join(f"<li>{e(_cs_name(r))}: {r['rate']:.2f}%{e(_cs_cond(r))}</li>" for r in _cs_hy)
    + '</ul><p>Check whether a headline savings rate is a promo before you compare it with a CD. See <a href="/high-yield-savings-promo-vs-base-rate/">promo rate vs base rate</a>.</p>'
    "<h2>When a CD wins</h2><ul>"
    "<li>You won't need the money until a set date.</li>"
    "<li>You think savings rates will fall during the term. A CD keeps paying its APY; a savings rate can drop any week.</li>"
    "<li>The CD pays clearly more than the savings account at the same bank.</li></ul>"
    "<h2>When savings wins</h2><ul>"
    "<li>It's your emergency fund, or you might need it soon. Breaking a CD costs interest.</li>"
    "<li>The gap is small. A few dollars a year may not be worth locking up the money.</li>"
    "<li>You think rates will rise. Savings rates can go up; a CD's won't.</li></ul>"
    '<p>Splitting works too: keep cash you might need in <a href="/savings/">savings</a> and put the rest in a CD. '
    'If you pay state income tax, compare <a href="/cds-vs-t-bills/">CDs vs T-bills</a>, since T-bill interest skips state tax. All CD terms: <a href="/cd-rates/">CD rates this week</a>.</p>'
    "</div>"
    + faq_block(_cs_faqs)
    + '<div class="prose"><p>Sources: savings rates from each bank\'s savings page ('
    + ", ".join(f'<a href="{e(r["source_url"])}" rel="nofollow">{e(_cs_name(r))}</a>' for r in _cs_hy)
    + "".join(f'; <a href="{e(sv["source_url"])}" rel="nofollow">{e(_cd_bn(bk))} savings</a>' for bk, sv, _a, _b in _cs_same if sv not in _cs_hy)
    + f"), {UPD_S} check; " + ", ".join(f'<a href="{e(u)}" rel="nofollow">{e(_cd_bn(b))} CD page</a>' for b, u in
        [(b, next(r["source_url"] for r in CD if r["bank"] == b)) for b in _cd_banks])
    + f" (read {_cd_upd}). Published by Sparta Labs. Not financial advice.</p></div>"
    + related("/cd-vs-high-yield-savings/") + RISK_EXPLAINER + NEWSLETTER
)
page("/cd-vs-high-yield-savings/",
     f"CD vs high-yield savings: which pays more this week? ({MONTH}) | {BRAND}",
     f"Top savings pays {_cs_top['rate']:.2f}% APY this week"
     + (f" and the best 1-year CD pays {_cs_12['rate']:.2f}%" if _cs_12 else "")
     + ". Fixed vs variable rates, same-bank gaps, penalties and when each one wins.",
     _cs_body, h1="CD vs high-yield savings: which pays more?", faqs=_cs_faqs)

# ---- CD vs money market fund (added 2026-11-26 daily build) ----
# Rates come from cd_seed.csv (CD list above, read Nov 23, 2026) and rates_seed.csv money_market rows (7-day yields,
# current refresh) at build time; no figures are typed in here. Penalty text is the cd_seed.csv early_withdrawal column.
# Non-rate wording reuses facts already on the site: FDIC $250,000 per depositor, per bank, per ownership category;
# money funds not FDIC-insured, $1 share price not guaranteed, SIPC covers missing assets not a drop in value
# (/best-money-market-funds/); state-tax note for government and Treasury funds (/best-money-market-funds/ and
# /state-tax-free-money-market-funds/); Vanguard waives the VMFXX minimum as a settlement fund (/best-money-market-funds/).
_cm_mm = [r for r in SHOWN if r["category"] == "money_market"]
if not _cm_mm:
    raise SystemExit("cd-vs-money-market-fund: no money market rows")
_cm_funds = _cm_mm[:3] + [r for r in _cm_mm[3:] if ticker(r["name"]) in ("VMFXX", "SPAXX")]
_cm_top = _cm_mm[0]
_cm_cds = []
for _mo in (3, 6, 12):
    _rs = [r for r in CD if r["months"] == _mo]
    if _rs: _cm_cds.append(max(_rs, key=lambda r: (r["rate"], r["bank"] == "Marcus by Goldman Sachs")))
if not _cm_cds:
    raise SystemExit("cd-vs-money-market-fund: no 3-, 6- or 12-month CD rows")
def _cm_lab(mo): return {3: "3-month", 6: "6-month", 12: "1-year"}.get(mo, f"{mo}-month")
_cm_6 = next((r for r in _cm_cds if r["months"] == 6), None)
_cm_12 = next((r for r in _cm_cds if r["months"] == 12), None)
def _cm_asof(r):
    a = note_as_of(r)
    return datetime.strptime(a, "%m/%d/%Y").strftime("%b %-d, %Y") if a else None
_cm_asofs = sorted({_cm_asof(r) for r in _cm_funds if _cm_asof(r)})
_cm_asof_txt = (f"7-day yields as of {_cm_asofs[0]}" if len(_cm_asofs) == 1 else f"7-day yields from the {UPD_S} check")
def _cm_tk(r): return ticker(r["name"])
def _cm_gap(rate):
    g = rate - _cm_top["rate"]
    return f"+{g:.2f} pts" if g > 0 else (f"{g:.2f} pts" if g < 0 else "Even")
def _cm_pen(r):
    p = (r.get("early_withdrawal") or "").strip()
    return p[0].upper() + p[1:] if p else "Penalty; confirm on the bank's CD page"
def _cm_days(r):
    m = re.search(r"(\d+) days simple interest", r.get("early_withdrawal") or "")
    return int(m.group(1)) if m else None
_cm_others = [r for r in _cm_funds if r is not _cm_top]
_cm_lead = []
if _cm_6 and _cm_12:
    _cm_lead.append(f"The best 6-month CD pays {_cm_6['rate']:.2f}% APY at {_cd_bn(_cm_6['bank'])}, and the best 1-year CD pays {_cm_12['rate']:.2f}% at {_cd_bn(_cm_12['bank'])}.")
else:
    _x = _cm_cds[-1]
    _cm_lead.append(f"The best {_cm_lab(_x['months'])} CD pays {_x['rate']:.2f}% APY at {_cd_bn(_x['bank'])}.")
_cm_lead.append(f"The top money market fund, {_cm_tk(_cm_top)}, yields {_cm_top['rate']:.2f}%"
                + (", with " + _cd_join([f"{_cm_tk(r)} at {r['rate']:.2f}%" for r in _cm_others]) if _cm_others else "") + ".")
_cm_lead = " ".join(_cm_lead)
_cm_cd_wins = [r for r in _cm_cds if r["rate"] > _cm_top["rate"]]
_cm_mm_wins = [r for r in _cm_cds if r["rate"] < _cm_top["rate"]]
if len(_cm_cd_wins) == len(_cm_cds):
    _cm_verdict = f"CDs pay more than the top fund at all {len(_cm_cds)} terms, and the CD rate stays put."
elif len(_cm_mm_wins) == len(_cm_cds):
    _cm_verdict = f"{_cm_tk(_cm_top)} pays more than the best CD at all {len(_cm_cds)} terms this week, but its yield can change any day."
else:
    _cm_verdict = (f"{_cm_tk(_cm_top)} beats the best " + _cd_join([_cm_lab(r["months"]) for r in _cm_mm_wins] or ["no"]) + " CD, and the best "
                   + _cd_join([_cm_lab(r["months"]) for r in _cm_cd_wins]) + (" CD pays more." if len(_cm_cd_wins) == 1 else " CDs pay more."))
def _cm_mmrow(r):
    sub = e(r["name"]) + (" (top fund)" if r is _cm_top else "")
    return (f'<tr><td>{e(mm_kind(r["name"]))} money market fund</td><td class="apy">{r["rate"]:.2f}%<small>{sub}</small></td>'
            f'<td>Floating; can change daily</td><td>{"Top fund" if r is _cm_top else _cm_gap(r["rate"])}</td>'
            f'<td>${10000 * r["rate"] / 100:,.0f} if the yield holds</td><td>Sell any business day</td></tr>')
def _cm_cdrow(r):
    return (f'<tr><td>{e(_cm_lab(r["months"]))} CD</td><td class="apy">{r["rate"]:.2f}%<small>{e(_cd_bn(r["bank"]))} {e(r["product"])}</small></td>'
            f'<td>Fixed for the term</td><td>{_cm_gap(r["rate"])}</td><td>${10000 * r["rate"] / 100:,.0f}</td><td>{e(_cm_pen(r))}</td></tr>')
_cm_tbl = ('<div class="tw"><table><thead><tr><th>Option</th><th>Yield or APY</th><th>Rate fixed?</th><th>vs top fund</th>'
           '<th>A year on $10,000</th><th>Getting money out early</th></tr></thead><tbody>'
           + "".join(_cm_mmrow(r) for r in _cm_funds) + "".join(_cm_cdrow(r) for r in _cm_cds) + "</tbody></table></div>"
           + f'<p class="foot-n">Fund yields are {e(_cm_asof_txt)}, from each fund company\'s page. CD APYs are from each bank\'s CD page, checked {_cd_upd}. '
           "A 7-day yield and an APY are close but not identical measures. A year on $10,000 assumes the rate holds for 12 months; "
           "a 3- or 6-month CD would renew at the rate on that day. Confirm with the provider. Not financial advice.</p>")
_cm_bpen = next((r for r in _cm_cds if _cm_days(r)), None)
_cm_pen_txt = (f"At {_cd_bn(_cm_bpen['bank'])}, the {_cm_lab(_cm_bpen['months'])} CD charges {_cm_days(_cm_bpen)} days of simple interest. "
               f"On $10,000 at {_cm_bpen['rate']:.2f}% that's about ${10000 * _cm_bpen['rate'] / 100 * _cm_days(_cm_bpen) / 365:,.0f}."
               if _cm_bpen else "Expect a penalty, usually a set number of days of interest.")
_cm_kinds = {}
for r in _cm_funds: _cm_kinds.setdefault(mm_kind(r["name"]), []).append(_cm_tk(r))
_cm_govt = [t for k in ("Treasury", "Government") for t in _cm_kinds.get(k, [])]
_cm_prime = _cm_kinds.get("Prime", [])
_cm_mins_mm = sorted({f'{_cm_tk(r)} {r["min_deposit"]}' for r in _cm_funds if r["min_deposit"].strip().lower() not in ("", "unknown")})
_cm_mins_cd = []
for b in _cd_banks:
    r0 = next(r for r in CD if r["bank"] == b)
    _cm_mins_cd.append(f"{_cd_bn(b)} {r0['min_deposit']}")
_cm_gap12 = (_cm_12["rate"] - _cm_top["rate"]) if _cm_12 else None
_cm_faqs = [
    ("Does a CD or a money market fund pay more right now?", f"{_cm_lead} {_cm_verdict}"),
    ("Can a money market fund's yield drop?",
     "Yes. A money market fund's yield moves with short-term interest rates, so it can fall when the Fed cuts. A CD's APY is fixed for the term if you hold it to maturity."),
    ("Is a money market fund FDIC-insured like a CD?",
     "No. CDs at an FDIC member bank are insured up to $250,000 per depositor, per bank, per ownership category. Money market funds are not FDIC-insured. "
     "They aim to keep a $1 share price, which is not guaranteed. At a brokerage, SIPC covers missing assets if the broker fails, not a drop in the fund's value."),
    ("How fast can I get my money out?",
     f"You can sell a money market fund on any business day. Breaking a CD early costs a penalty. {_cm_pen_txt}"),
    ("What's the minimum to start?",
     "Money market funds: " + "; ".join(_cm_mins_mm) + ". CDs: " + ", ".join(_cm_mins_cd) + "."),
]
_cm_body = (
    '<div class="prose">'
    f'<p class="lead">{e(_cm_lead)} {e(_cm_verdict)}</p>'
    f'<p class="upd">Updated {UPD_S} &middot; {len(_cm_funds)} money market funds vs {len(_cm_cds)} CD terms</p>'
    "<p>Not financial advice.</p>"
    "<h2>Top money market funds vs the best CDs</h2></div>"
    + _cm_tbl
    + '<div class="prose">'
    + (f"<p>On the 1-year term, the {abs(_cm_gap12):.2f}-point gap is about {dollars(abs(_cm_gap12))} a year on $10,000 and about {dollars(abs(_cm_gap12), 50000)} on $50,000"
       + (", in the CD's favor" if _cm_gap12 > 0 else f", in {_cm_tk(_cm_top)}'s favor") + ", if the fund's yield holds.</p>" if _cm_gap12 else "")
    + "<h2>Rate lock vs floating yield</h2>"
    "<p>A money market fund's yield isn't fixed. It moves with short-term rates, which follow the Fed. When the Fed cuts, fund yields tend to fall soon after; when it raises rates, they climb.</p>"
    + (f"<p>A CD locks its APY for the term if you hold it to maturity. If you think rates will fall over the next year, a {_cm_12['rate']:.2f}% 1-year CD keeps paying that rate. "
       "If you think they'll rise, the fund can follow them up and the CD can't.</p>" if _cm_12 else
       "<p>A CD locks its APY for the term if you hold it to maturity. If rates rise, the fund can follow them up and the CD can't.</p>")
    )
_cm_body += (
    "<h2>Getting to your money</h2><ul>"
    "<li><b>Money market funds:</b> you can sell on any business day with no penalty.</li>"
    f"<li><b>CDs:</b> taking money out before the term ends costs a penalty. {e(_cm_pen_txt)} "
    + e("Penalties by bank for these terms: " + "; ".join(f"{_cd_bn(r['bank'])} {_cm_lab(r['months'])}: {_cm_pen(r).rstrip('.')}" for r in _cm_cds) + ".")
    + "</li></ul>"
    "<h2>Safety</h2>"
    '<p>CDs at an FDIC member bank are deposits, insured up to $250,000 per depositor, per bank, per ownership category (<a href="https://www.fdic.gov/resources/deposit-insurance" rel="nofollow noopener">FDIC</a>).</p>'
    "<p>Money market funds are not FDIC-insured. They aim to keep a $1 share price, which is not guaranteed. At a brokerage, SIPC covers missing assets if the broker fails, not a drop in the fund's value."
    + (f" {e(_cd_join(_cm_govt))} {'is a government or Treasury fund' if len(_cm_govt) == 1 else 'are government or Treasury funds'}, which hold US government debt." if _cm_govt else "")
    + (f" {e(_cd_join(_cm_prime))} {'is a prime fund' if len(_cm_prime) == 1 else 'are prime funds'}, which can also hold short-term corporate debt." if _cm_prime else "")
    + "</p>"
    "<h2>Taxes</h2>"
    "<p>CD interest is taxed federally and by your state, if it has an income tax. Money market fund dividends are taxed federally. "
    "Part or all of the income from government and Treasury funds may be exempt from state income tax. The rules differ by state, so check the fund's annual tax information. "
    'See <a href="/state-tax-free-money-market-funds/">state-tax-free money market funds</a> for what each fund keeps after state tax.</p>'
    "<h2>Minimums</h2><ul>"
    f"<li><b>Money market funds:</b> {e('; '.join(_cm_mins_mm))}."
    + (" Vanguard waives the minimum for VMFXX when it is your brokerage settlement fund." if any(_cm_tk(r) == "VMFXX" for r in _cm_funds) else "")
    + "</li>"
    f"<li><b>CDs:</b> {e(', '.join(_cm_mins_cd))}.</li></ul>"
    "<h2>Which one fits</h2><ul>"
    "<li><b>You might need the money soon:</b> a money market fund. You can sell any business day with no penalty.</li>"
    "<li><b>You won't touch it until a set date and want a rate that won't change:</b> a CD.</li>"
    "<li><b>You think rates will fall:</b> a CD locks today's rate. A fund's yield would drift down with them.</li>"
    "<li><b>You want FDIC insurance:</b> a CD at an FDIC member bank, or a <a href=\"/savings/\">high-yield savings account</a>.</li>"
    "<li><b>You pay state income tax:</b> part of a Treasury or government fund's income may be exempt from state tax. CD interest isn't.</li></ul>"
    '<p>Splitting works too: keep cash you might need in a money market fund and put the rest in a CD. '
    'More: <a href="/cd-rates/">CD rates this week</a>, <a href="/best-money-market-funds/">best money market fund yields</a>, '
    '<a href="/cd-vs-high-yield-savings/">CD vs high-yield savings</a>, <a href="/cds-vs-t-bills/">CDs vs T-bills</a> and '
    '<a href="/high-yield-savings-vs-money-market-fund/">high-yield savings vs money market funds</a>.</p>'
    "</div>"
    + faq_block(_cm_faqs)
    + '<div class="prose"><p>Sources: fund yields from each fund company\'s page ('
    + ", ".join(f'<a href="{e(r["source_url"])}" rel="nofollow">{e(_cm_tk(r))}</a>' for r in _cm_funds)
    + f"), {UPD_S} check; " + ", ".join(f'<a href="{e(u)}" rel="nofollow">{e(_cd_bn(b))} CD page</a>' for b, u in
        [(b, next(r["source_url"] for r in CD if r["bank"] == b)) for b in _cd_banks])
    + f" (read {_cd_upd}). Published by Sparta Labs. Not financial advice.</p></div>"
    + related("/cd-vs-money-market-fund/") + RISK_EXPLAINER + NEWSLETTER
)
page("/cd-vs-money-market-fund/",
     f"CD vs money market fund: which pays more this week? ({MONTH}) | {BRAND}",
     (f"The best 1-year CD pays {_cm_12['rate']:.2f}% APY and the top money market fund, {_cm_tk(_cm_top)}, yields {_cm_top['rate']:.2f}%. " if _cm_12
      else f"The top money market fund, {_cm_tk(_cm_top)}, yields {_cm_top['rate']:.2f}%. ")
     + "Rate lock, access, FDIC vs SIPC, state tax and minimums.",
     _cm_body, h1="CD vs money market fund: which pays more?", faqs=_cm_faqs)

# ---- Marcus vs Bread Savings CD (added 2026-11-27 daily build) ----
# Every rate, minimum and penalty comes from the cd_seed.csv rows above (Marcus High-Yield CD and Bread Savings CD,
# read Nov 23, 2026); no figures are typed in here. Non-rate wording reuses facts already on /cd-rates/ (Marcus compounds
# daily, credited monthly, 10-day CD rate guarantee; Bread $1,000,000 per-account maximum; FDIC limit wording).
_MB_M, _MB_B = "Marcus by Goldman Sachs", "Bread Savings"
_mb_m = {r["months"]: r for r in CD if r["bank"] == _MB_M}
_mb_b = {r["months"]: r for r in CD if r["bank"] == _MB_B}
_mb_both = sorted(set(_mb_m) & set(_mb_b))
if not _mb_both:
    raise SystemExit("marcus-vs-bread-savings-cd: no shared Marcus/Bread CD terms in cd_seed.csv")
_mb_only_b = sorted(set(_mb_b) - set(_mb_m))
_mb_only_m = sorted(set(_mb_m) - set(_mb_b))
def _mb_lab(mo): return {12: "1-year"}.get(mo, f"{mo // 12}-year" if mo % 12 == 0 and mo >= 24 else f"{mo}-month")
def _mb_short(mo): return {12: "1 year"}.get(mo, f"{mo // 12} years" if mo % 12 == 0 and mo >= 24 else f"{mo} months")
_mb_bwin = [mo for mo in _mb_both if _mb_b[mo]["rate"] > _mb_m[mo]["rate"]]
_mb_mwin = [mo for mo in _mb_both if _mb_m[mo]["rate"] > _mb_b[mo]["rate"]]
_mb_tie = [mo for mo in _mb_both if _mb_m[mo]["rate"] == _mb_b[mo]["rate"]]
def _mb_hi(d):
    hi = max(r["rate"] for r in d.values())
    return hi, sorted(mo for mo, r in d.items() if r["rate"] == hi)
_mb_bhi, _mb_bhi_t = _mb_hi(_mb_b)
_mb_mhi, _mb_mhi_t = _mb_hi(_mb_m)
def _mb_where(ts):
    if len(ts) > 2: return f"on terms from {_mb_short(ts[0])} to {_mb_short(ts[-1])}"
    return f"on its {_cd_join([_mb_lab(t) for t in ts])} {'CD' if len(ts) == 1 else 'CDs'}"
_mb_lead = [f"Bread Savings pays up to {_mb_bhi:.2f}% APY {_mb_where(_mb_bhi_t)}, and Marcus pays up to {_mb_mhi:.2f}% {_mb_where(_mb_mhi_t)}."]
def _mb_pct(r): return f"{r['rate']:.2f}%"
def _mb_vs(mo, a, b): return f"{a[mo]['rate']:.2f}% vs {b[mo]['rate']:.2f}%"
if len(_mb_bwin) >= len(_mb_mwin):
    _mb_lead.append(f"Bread pays more at {len(_mb_bwin)} of the {len(_mb_both)} terms both banks offer.")
    if _mb_mwin:
        _mb_lead.append("Marcus pays more at " + _cd_join([f"{_mb_short(mo)} ({_mb_vs(mo, _mb_m, _mb_b)})" for mo in _mb_mwin]) + ".")
else:
    _mb_lead.append(f"Marcus pays more at {len(_mb_mwin)} of the {len(_mb_both)} terms both banks offer.")
    if _mb_bwin:
        _mb_lead.append("Bread pays more at " + _cd_join([f"{_mb_short(mo)} ({_mb_vs(mo, _mb_b, _mb_m)})" for mo in _mb_bwin]) + ".")
if _mb_tie:
    _mb_lead.append("They pay the same at " + _cd_join([_mb_short(mo) for mo in _mb_tie]) + ".")
_mb_lead = " ".join(_mb_lead)
_mb_gaps = {mo: _mb_b[mo]["rate"] - _mb_m[mo]["rate"] for mo in _mb_both}
_mb_big = max(_mb_both, key=lambda mo: (abs(_mb_gaps[mo]), mo))
def _mb_row(mo):
    m, b = _mb_m.get(mo), _mb_b.get(mo)
    mc = f'{m["rate"]:.2f}%' if m else "n/a"
    bc = f'{b["rate"]:.2f}%' if b else "n/a"
    if m and b:
        g = b["rate"] - m["rate"]
        hi = "Bread Savings" if g > 0 else ("Marcus" if g < 0 else "Same")
        gap = f"{abs(g):.2f} pts ({dollars(abs(g))})" if g else "Even"
    else:
        hi, gap = ("Marcus only" if m else "Bread Savings only"), "n/a"
    return f'<tr><td>{e(_mb_lab(mo))}</td><td class="apy">{mc}</td><td class="apy">{bc}</td><td>{e(hi)}</td><td>{e(gap)}</td></tr>'
_mb_terms = sorted(set(_mb_m) | set(_mb_b))
_mb_tbl = ('<div class="tw"><table><thead><tr><th>Term</th><th>Marcus APY</th><th>Bread Savings APY</th><th>Pays more</th>'
           '<th>Gap (a year on $10,000)</th></tr></thead><tbody>' + "".join(_mb_row(mo) for mo in _mb_terms) + "</tbody></table></div>"
           + f'<p class="foot-n">APYs are from each bank\'s CD page, checked {_cd_upd}. Rates change; confirm on the bank\'s page before you open a CD. '
           "The gap column is the extra interest for one year on $10,000. Not financial advice.</p>")
def _mb_pen_groups(d):
    out = []
    for mo in sorted(d):
        p = (d[mo].get("early_withdrawal") or "").strip() or "Confirm on the bank's CD page"
        if out and out[-1][0] == p: out[-1][1].append(mo)
        else: out.append([p, [mo]])
    return out
def _mb_pen_txt(d):
    g = _mb_pen_groups(d)
    if len(g) == 1: return g[0][0][0].upper() + g[0][0][1:]
    return "; ".join(f"{p[0].upper() + p[1:]} ({_mb_short(ts[0])}" + (f" to {_mb_short(ts[-1])})" if len(ts) > 1 else ")") for p, ts in g)
_mb_min_m = next(iter(_mb_m.values()))["min_deposit"]
_mb_min_b = next(iter(_mb_b.values()))["min_deposit"]
_mb_url_m = next(iter(_mb_m.values()))["source_url"]
_mb_url_b = next(iter(_mb_b.values()))["source_url"]
_mb_tbl2 = ('<div class="tw"><table><thead><tr><th>Bank</th><th>Minimum deposit</th><th>Early withdrawal penalty</th><th>Terms</th><th>Source</th></tr></thead><tbody>'
            f'<tr><td>Marcus High-Yield CD</td><td>{e(_mb_min_m)}</td><td>{e(_mb_pen_txt(_mb_m))}</td><td>{e(_mb_short(min(_mb_m)))} to {e(_mb_short(max(_mb_m)))}</td>'
            f'<td><a href="{e(_mb_url_m)}" rel="nofollow">Marcus CD page</a></td></tr>'
            f'<tr><td>Bread Savings CD</td><td>{e(_mb_min_b)}</td><td>{e(_mb_pen_txt(_mb_b))}</td><td>{e(_mb_short(min(_mb_b)))} to {e(_mb_short(max(_mb_b)))}</td>'
            f'<td><a href="{e(_mb_url_b)}" rel="nofollow">Bread Savings CD page</a></td></tr></tbody></table></div>')
def _mb_days(r):
    m = re.search(r"(\d+) days simple interest", r.get("early_withdrawal") or "")
    return int(m.group(1)) if m else None
_mb_b12 = _mb_b.get(12)
_mb_b12_cost = (f"Breaking Bread's 1-year CD costs {_mb_days(_mb_b12)} days of simple interest: about "
                f"${10000 * _mb_b12['rate'] / 100 * _mb_days(_mb_b12) / 365:,.0f} on $10,000 at {_mb_b12['rate']:.2f}%."
                if _mb_b12 and _mb_days(_mb_b12) else "Bread charges a set number of days of simple interest; check its CD page.")
_mb_ev = next((r for r in CD if r["bank"] == "EverBank" and r["months"] == 13), None)
_mb_ev_txt = (f'Outside these two banks, EverBank\'s 13-month CD pays {_mb_ev["rate"]:.2f}% (as of {_cd_d(_mb_ev['as_of'] or _mb_ev['fetched_at'])}); see <a href="/cd-rates/">all CD rates</a>.'
              if _mb_ev else "")
_mb_faqs = [
    ("Does Marcus or Bread Savings pay more on CDs?", _mb_lead),
    ("Which bank pays more on a 1-year CD?",
     (f"Marcus pays {_mb_m[12]['rate']:.2f}% APY on its 1-year CD and Bread Savings pays {_mb_b[12]['rate']:.2f}%. "
      f"On $10,000 that's a gap of about {dollars(abs(_mb_gaps[12]))} a year.") if 12 in _mb_both else "Check each bank's CD page for its 1-year rate."),
    ("What's the minimum deposit?",
     f"Marcus needs {_mb_min_m} to open a CD. Bread Savings needs {_mb_min_b} in a single deposit."),
    ("What does it cost to cash out a CD early?",
     "Marcus charges a penalty and lists the amount in its CD FAQs. "
     f"Bread Savings charges {_mb_pen_txt(_mb_b)[0].lower() + _mb_pen_txt(_mb_b)[1:]}. {_mb_b12_cost}"),
    ("Are Marcus and Bread Savings CDs FDIC-insured?",
     "Yes. Both are FDIC member banks, and CDs are insured up to $250,000 per depositor, per bank, per ownership category. "
     "Money at two different banks counts toward two separate limits."),
]
_mb_body = (
    '<div class="prose">'
    f'<p class="lead">{e(_mb_lead)}</p>'
    f'<p class="upd">Rates from {_cd_upd} &middot; {len(_mb_both)} terms both banks offer</p>'
    f"<p>These APYs are from {_cd_upd}. Confirm the current rate on the bank's page before you open a CD. Not financial advice.</p>"
    "<h2>Marcus vs Bread Savings CD rates by term</h2></div>"
    + _mb_tbl
    + '<div class="prose">'
    + f"<p>The biggest gap is at {_mb_short(_mb_big)}: {_mb_b[_mb_big]['rate']:.2f}% at Bread vs {_mb_m[_mb_big]['rate']:.2f}% at Marcus. "
    + f"That's about {dollars(abs(_mb_gaps[_mb_big]))} a year on $10,000 and {dollars(abs(_mb_gaps[_mb_big]), 50000)} on $50,000.</p>"
    + ("<p>" + " ".join(
        ([f"Bread also has a {_cd_join([_mb_lab(mo) for mo in _mb_only_b])} CD at {_cd_join([_mb_pct(_mb_b[mo]) for mo in _mb_only_b])}."] if _mb_only_b else [])
        + ([f"Marcus also has a {_cd_join([_mb_lab(mo) for mo in _mb_only_m])} CD at {_cd_join([_mb_pct(_mb_m[mo]) for mo in _mb_only_m])}."] if _mb_only_m else [])
      ) + "</p>" if (_mb_only_b or _mb_only_m) else "")
    + (f"<p>{_mb_ev_txt}</p>" if _mb_ev_txt else "")
    + "<h2>Minimums, penalties and sources</h2></div>"
    + _mb_tbl2
    + '<div class="prose">'
    "<h2>Other differences</h2><ul>"
    f"<li><b>Minimum:</b> Marcus asks for {e(_mb_min_m)}. Bread asks for {e(_mb_min_b)} in a single deposit, up to $1,000,000 per account.</li>"
    "<li><b>Interest:</b> Marcus compounds daily and credits interest monthly. If its rate for your term rises within 10 days of opening, you get the higher rate.</li>"
    f"<li><b>Early withdrawal:</b> {e(_mb_b12_cost)} Marcus charges a penalty too; check its CD FAQs for the amount.</li>"
    "<li><b>Insurance:</b> both are FDIC member banks. CDs are insured up to $250,000 per depositor, per bank, per ownership category "
    '(<a href="https://www.fdic.gov/resources/deposit-insurance" rel="nofollow noopener">FDIC</a>).</li></ul>'
    "<h2>Which one fits</h2><ul>"
    + (f"<li><b>You want a 1-year CD:</b> Marcus, at {_mb_m[12]['rate']:.2f}% vs {_mb_b[12]['rate']:.2f}%, with a lower minimum.</li>" if 12 in _mb_mwin else "")
    + (f"<li><b>You can lock up money for 18 months or longer:</b> Bread pays more at every term from 18 months to 5 years.</li>"
       if all(mo in _mb_bwin for mo in _mb_both if mo >= 18) and any(mo >= 18 for mo in _mb_both) else "")
    + (f"<li><b>You have less than {e(_mb_min_b)}:</b> Marcus, with its {e(_mb_min_m)} minimum.</li>")
    + (f"<li><b>You want a term past 5 years:</b> Marcus has a 6-year CD at {_mb_m[72]['rate']:.2f}%.</li>" if 72 in _mb_only_m else "")
    + "</ul>"
    '<p>More: <a href="/cd-rates/">CD rates this week</a>, <a href="/cd-vs-high-yield-savings/">CD vs high-yield savings</a>, '
    '<a href="/cds-vs-t-bills/">CDs vs T-bills</a>, <a href="/cd-vs-money-market-fund/">CD vs money market fund</a> and '
    '<a href="/ally-vs-marcus-savings/">Ally vs Marcus savings</a>.</p>'
    "</div>"
    + faq_block(_mb_faqs)
    + f'<div class="prose"><p>Sources: <a href="{e(_mb_url_m)}" rel="nofollow">Marcus CD page</a>, <a href="{e(_mb_url_b)}" rel="nofollow">Bread Savings CD page</a>'
    + (f', <a href="{e(_mb_ev["source_url"])}" rel="nofollow">EverBank CD page</a>' if _mb_ev else "")
    + f" (checked {_cd_upd}). Published by Sparta Labs. Not financial advice.</p></div>"
    + related("/marcus-vs-bread-savings-cd/") + RISK_EXPLAINER + NEWSLETTER
)
page("/marcus-vs-bread-savings-cd/",
     f"Marcus vs Bread Savings CD rates: which pays more? ({MONTH}) | {BRAND}",
     f"Bread Savings CDs pay up to {_mb_bhi:.2f}% APY and Marcus up to {_mb_mhi:.2f}%. "
     + (f"Marcus wins at 1 year ({_mb_m[12]['rate']:.2f}% vs {_mb_b[12]['rate']:.2f}%). " if 12 in _mb_mwin else "")
     + "Rates by term, minimums and early withdrawal penalties.",
     _mb_body, h1="Marcus vs Bread Savings CD rates: which pays more?", faqs=_mb_faqs)

# About / Disclosures
srcs = sorted({re.sub(r"^https?://([^/]+).*", r"\1", r["source_url"]) for r in SHOWN})
page("/about/", f"About {BRAND}", f"{BRAND} is a weekly-updated comparison of where US savers can park cash, with plain risk labels.",
 f"""<div class="prose"><p class="lead">{BRAND} answers one question: where should cash sit this week?</p>
<p>Every week we check the published rates for high-yield savings accounts, money market funds, broker cash programs, US Treasury bill auctions and USDC rewards. We put them in one table with a plain risk label, so you can see the trade-off between yield and safety at a glance.</p>
<h2>How we collect rates</h2><ul><li>Each rate is read from the provider's public website or official data, for example the US Treasury's FiscalData API for T-bills. Every row links to its source.</li>
<li>If we can't read a rate directly, we leave it out. We never estimate or fill in a number.</li><li>Promotional rates, balance tiers and subscription requirements are noted under each name.</li></ul>
<h2>Who publishes {BRAND}</h2><p>{BRAND} is an independent publication by {PUBLISHER}. We're not owned by any bank, broker or crypto exchange.</p>
<p>Questions or corrections: {EMAIL_HTML}.</p></div>""", h1=f"About {BRAND}")
page("/disclosures/", f"Disclosures | {BRAND}", f"{BRAND} disclosures: not financial advice, rates change, affiliate relationships and data sources.",
 f"""<div class="prose"><h2>Not financial advice</h2><p>{BRAND} provides general information only. It is not investment, tax or legal advice, and we are not a bank, broker or registered adviser. Consider your own situation and consult a qualified professional before making decisions.</p>
<h2>Rates change</h2><p>Rates are variable and were accurate to the best of our knowledge when checked (last update {UPD_S}). Providers can change them at any time. Promotional rates, balance tiers, eligibility and fees may apply. Always confirm the current terms on the provider's website before opening an account.</p>
<h2>Insurance and risk labels</h2><p>Our FDIC / SIPC / Treasury-backed / Not insured labels are simplified summaries. Coverage limits and conditions apply. Crypto assets, including USDC, are not FDIC- or SIPC-insured and can lose value.</p>
<h2 id="newsletter-privacy">Newsletter</h2><p>Newsletter signups are handled by beehiiv, our email provider (see <a href="https://www.beehiiv.com/privacy" rel="nofollow noopener">beehiiv\'s privacy policy</a>). Subscriber email addresses are used only to send the weekly CashRate Watch email. We never sell them. You can unsubscribe anytime using the link at the bottom of every email.</p>\n<h2 id="affiliate">Affiliate links</h2><p><b>Some links on this site may be affiliate links. If you open or fund an account through one, we may earn a commission at no cost to you. It never changes how rates are ranked or shown.</b></p><p>Today we have no affiliate relationships, and every link on this site is a plain link to the provider's own page. If that changes, we will label affiliate links where they appear. Tables are always sorted by rate, and a provider can't pay to be listed, moved up or have its rate shown differently.</p>
<h2>Data sources</h2><p>Rates come from providers' public pages and from US Treasury auction data (<a href="https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/" rel="nofollow noopener">FiscalData auctions dataset</a>; each T-bill row links to its TreasuryDirect auction results release). Sources used this week: {", ".join(e(s) for s in srcs)}.</p>
<p>Contact: {EMAIL_HTML}</p></div>""", h1="Disclosures")

# Privacy policy / Terms of use
page("/privacy/", f"Privacy policy | {BRAND}", f"How {BRAND} handles newsletter signups, emails you send us, and basic server logs. We never sell your information.",
 f"""<div class="prose"><p class="upd">Effective {POLICY_DATE}</p>
<p>{BRAND} (cashratewatch.com) is published by {PUBLISHER} ("we", "us"). This policy explains what we collect and how we use it.</p>
<h2>What we collect</h2><ul>
<li id="newsletter"><b>Newsletter signups.</b> If you subscribe to the weekly email, you give us your email address. The signup form and mailing list are run by our email provider, beehiiv, which stores your address and records basic delivery data such as whether an email was opened or a link clicked. The form is loaded from beehiiv and may use cookies or similar storage under <a href="https://www.beehiiv.com/privacy" rel="nofollow noopener">beehiiv's privacy policy</a>.</li>
<li><b>Emails you send us.</b> If you email us, we receive your address and your message.</li>
<li><b>Server logs.</b> Our hosting provider (GitHub Pages) may automatically log technical data such as IP address, browser type and pages requested in order to deliver and secure the site.</li>
<li><b>No analytics or ad tracking.</b> We do not currently use analytics, advertising or tracking cookies of our own. If that changes, we will update this policy first.</li>
<li><b>Links to providers.</b> Links to banks, brokers and other providers take you to their sites, which have their own privacy policies.</li></ul>
<p>We don't ask for and don't collect account numbers, balances, Social Security numbers or other financial information.</p>
<h2>How we use it</h2><p>To send the weekly email you signed up for, reply to messages, and keep the site running and secure.</p>
<h2>What we don't do</h2><p>We don't sell or rent your personal information, and we don't share your email address with advertisers or the providers listed on this site.</p>
<h2>Your choices</h2><p>Unsubscribe anytime using the link at the bottom of every email. To ask us to delete your email address or a message you sent us, email {EMAIL_HTML}. You can block or delete cookies in your browser settings.</p>
<h2>Children</h2><p>This site is for adults and is not directed to children under 13. We don't knowingly collect their information.</p>
<h2>Changes</h2><p>If this policy changes, we'll update the effective date above.</p>
<h2>Contact</h2><p>{EMAIL_HTML}</p></div>""", h1="Privacy policy")
page("/terms/", f"Terms of use | {BRAND}", f"Terms for using {BRAND}: information only, not financial advice; rates change, so confirm with the provider.",
 f"""<div class="prose"><p class="upd">Effective {POLICY_DATE}</p>
<p>These terms apply to cashratewatch.com and the {BRAND} newsletter, published by {PUBLISHER} ("we", "us"). By using the site you agree to them.</p>
<h2>Information only, not advice</h2><p>{BRAND} publishes general information about published interest rates and yields. It is not investment, tax, legal or financial advice. We are not a bank, broker-dealer, investment adviser or crypto platform, and we don't open accounts or hold money.</p>
<h2>Rates change; check with the provider</h2><p>Rates come from providers' public pages and official data, and we believe they were accurate when we checked them. They change often and may be out of date or incomplete, or depend on balance tiers, promotions, eligibility rules or fees. Always check the current terms on the provider's own site before you act. We aren't responsible for decisions you make based on this site.</p>
<h2>Risk labels are summaries</h2><p>Labels such as FDIC, SIPC, Treasury-backed and Not insured are simplified summaries, not guarantees. Crypto assets, including USDC, are not FDIC- or SIPC-insured and can lose value. Rewards on crypto balances are not interest and can change or end at any time.</p>
<h2>Other websites</h2><p>We link to websites we don't control. Your relationship with any provider is governed by that provider's terms. See our <a href="/disclosures/">Disclosures</a> for how links on this site work.</p>
<h2>Using our content</h2><p>You may share links to our pages and quote short excerpts with attribution. Don't copy or republish our tables in bulk, or scrape the site in a way that burdens it, without permission.</p>
<h2>No warranty; limitation of liability</h2><p>The site is provided "as is", without warranties of any kind. To the extent the law allows, we are not liable for any indirect or consequential loss arising from your use of the site.</p>
<h2>Changes and contact</h2><p>We may update these terms. If you keep using the site after an update, the new version applies. Questions: {EMAIL_HTML}</p></div>""", h1="Terms of use")

# 404 page (GitHub Pages serves /404.html)
page("/404/", f"Page not found | {BRAND}", "This page does not exist.",
     '<div class="prose"><p class="lead">Sorry, that page doesn\'t exist. It may have moved.</p><ul><li><a href="/">Home: all rates this week</a></li>'
     + "".join(f'<li><a href="{p}">{e(l)}</a></li>' for p, l in NAV) + '</ul></div>', h1="Page not found")
PAGES.remove("/404/")
shutil.move(os.path.join(args.out, "404", "index.html"), os.path.join(args.out, "404.html")); os.rmdir(os.path.join(args.out, "404"))
p0, p1, p2 = EMAIL_PARTS
open(os.path.join(args.out, "contact.js"), "w").write(
 "(function(){var p=[%s,%s,%s],a=p[0]+String.fromCharCode(64)+p[1]+String.fromCharCode(46)+p[2];"
 "document.querySelectorAll('.js-email').forEach(function(s){var l=document.createElement('a');"
 "l.href='mai'+'lto:'+a;l.textContent=a;s.textContent='';s.appendChild(l)})})();\n"
 % tuple(json.dumps(x) for x in (p0, p1, p2)))
# Static files
open(os.path.join(args.out, "CNAME"), "w").write("cashratewatch.com\n")
open(os.path.join(args.out, ".nojekyll"), "w").write("")
open(os.path.join(args.out, "favicon.svg"), "w").write(
 '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0b6e4f"/>'
 '<path d="M14 44l11-12 8 7 15-17" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
 '<circle cx="48" cy="22" r="5" fill="#ffd166"/></svg>')
open(os.path.join(args.out, "robots.txt"), "w").write(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
for p in PAGES: sm.append(f"<url><loc>{BASE}{p}</loc><lastmod>{UPD_ISO}</lastmod></url>")
sm.append("</urlset>")
open(os.path.join(args.out, "sitemap.xml"), "w").write("\n".join(sm) + "\n")
open(os.path.join(args.out, "rates.json"), "w").write(json.dumps(
 [{k: r[k] for k in ("name", "type", "rate_apy", "risk", "min_deposit", "source_url", "fetched_at")} for r in SHOWN], indent=1))
print(f"Built {len(PAGES)} pages into {args.out}; {len(SHOWN)} rows shown, {len(ALL)-len(SHOWN)} hidden (unknown rate or excluded). Updated {UPD_S}.")
