#!/usr/bin/env python3
"""Build a self-contained dashboard.html from data/papers.db.

The dashboard is one file with the data embedded as JSON and all filtering done
in the browser -- no server, no internet needed. Just double-click it.
"""
import os, re, json, sqlite3, datetime, html

# ---- conservative author-name merging --------------------------------------
# Surnames common enough that an initial+surname match is NOT enough evidence.
COMMON_SURNAMES = {
    "chen", "wang", "li", "zhang", "liu", "yang", "huang", "zhao", "wu", "zhou",
    "xu", "sun", "ma", "zhu", "hu", "guo", "lin", "he", "gao", "luo", "song",
    "kim", "lee", "park", "choi", "jung", "kang", "yoon", "cho", "jeong",
    "nguyen", "tran", "pham", "le", "singh", "kumar", "patel", "sharma", "das",
    "khan", "ali", "gupta", "shah", "yu", "wong", "tan", "ng", "ho", "yan",
}


def _parse_name(name):
    parts = [p.strip(".,") for p in name.split() if p.strip(".,")]
    if len(parts) < 2:
        return None
    return parts[-1].lower(), parts[:-1]   # surname, given-tokens


def _compat(g1, g2):
    """Aligned given-tokens must not conflict (an initial may stand for a full name)."""
    for x, y in zip(g1, g2):
        xl, yl = x.lower(), y.lower()
        if xl == yl:
            continue
        if len(x) == 1 and yl.startswith(xl):
            continue
        if len(y) == 1 and xl.startswith(yl):
            continue
        return False
    return True


def _shared_full(g1, g2):
    f1 = {t.lower() for t in g1 if len(t) >= 2}
    f2 = {t.lower() for t in g2 if len(t) >= 2}
    return len(f1 & f2)


def _can_merge(n1, n2, stats):
    # Surnames must match for ANY merge -- never let a shared ORCID bridge
    # different last names (guards against dirty/mis-entered ORCID data).
    p1, p2 = _parse_name(n1), _parse_name(n2)
    if not p1 or not p2 or p1[0] != p2[0]:
        return False
    s, g1, g2 = p1[0], p1[1], p2[1]
    o1, o2 = stats[n1]["orcids"], stats[n2]["orcids"]
    if o1 and o2:
        return bool(o1 & o2)               # same surname assured; ORCID decides
    if not _compat(g1, g2):
        return False
    sf = _shared_full(g1, g2)
    if sf < 1:
        return False
    if s in COMMON_SURNAMES and sf < 2:
        return False                       # extra-strict for common surnames
    return True


def build_canonical_map(stats):
    """Union-find over author-name variants -> {raw_name: canonical_name}."""
    names = list(stats)
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_sur = {}
    for n in names:
        p = _parse_name(n)
        if p:
            by_sur.setdefault(p[0], []).append(n)
    for ns in by_sur.values():
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                if find(ns[i]) != find(ns[j]) and _can_merge(ns[i], ns[j], stats):
                    union(ns[i], ns[j])

    clusters = {}
    for n in names:
        clusters.setdefault(find(n), []).append(n)
    canon = {}
    for members in clusters.values():
        def score(m):
            p = _parse_name(m)
            full = sum(1 for t in (p[1] if p else []) if len(t) >= 2)
            return (full, stats[m]["count"], len(m))
        best = max(members, key=score)
        for m in members:
            canon[m] = best
    return canon


def clean_dept(d):
    """Trim affiliation tails the parser left on a department name, e.g.
    'Mathematics at University of Texas at Arlington' -> 'Mathematics'."""
    if not d:
        return d
    d = re.split(r"\s+(?:at|in)\s+(?:the\s+)?[A-Z]", d)[0]
    return d.strip(" .;-,") or None

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "papers.db")
OUT = os.path.join(HERE, "dashboard.html")
CONFIG = json.load(open(os.path.join(HERE, "config.json")))
ORG_NAMES = {o["id"]: o["name"] for o in CONFIG["organizations"]}
THEME_NAMES = {t["id"]: t["name"] for t in CONFIG.get("themes", [])}
THEME_KW = {t["id"]: [k.strip().lower() for k in t["kw"]] for t in CONFIG.get("themes", [])}
GROUPS = CONFIG.get("groups", {})


def map_group(orgs, dept):
    """Map an author's department to a College (UTA) / service line (Cook)."""
    if not dept:
        return None
    d = dept.lower()
    for org in orgs:
        for rule in GROUPS.get(org, []):
            if any(m in d for m in rule["match"]):
                return rule["group"]
    return "Other"

# Generic MeSH "check tags" that dominate but say nothing about research area.
GENERIC_MESH = {
    "Humans", "Animals", "Male", "Female", "Adult", "Aged", "Aged, 80 and over",
    "Middle Aged", "Adolescent", "Child", "Child, Preschool", "Infant",
    "Infant, Newborn", "Young Adult", "Pregnancy", "Mice", "Rats",
    "Retrospective Studies", "Prospective Studies", "Cross-Sectional Studies",
    "Cohort Studies", "Follow-Up Studies", "Treatment Outcome", "Risk Factors",
    "Time Factors", "Reproducibility of Results", "United States", "Surveys and Questionnaires",
}


def build_payload():
    con = sqlite3.connect(DB_PATH)
    today = datetime.date.today().isoformat()
    last_run = con.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()
    last_run = last_run[0] if last_run else today

    jifmap = {issn: jif for issn, jif in
              con.execute("SELECT issn, jif FROM journals WHERE jif IS NOT NULL")}

    papers = []
    authstats = {}   # raw author name -> {count, orcids} for conservative merging
    for row in con.execute(
        "SELECT pmid,title,journal,pub_year,entry_date,doi,topic,orgs,mesh,authors,issn,is_genetics,first_seen,themes,study_type,nct,citations,rcr,recent_cit FROM papers"
    ):
        (pmid, title, journal, year, entry, doi, topic, orgs, mesh, authors, issn, is_gen,
         first_seen, themes, stype, nct, cit, rcr, rec_cit) = row
        orgs = json.loads(orgs)
        if not orgs:
            continue
        authors = json.loads(authors)
        for a in authors:
            if a["orgs"]:
                st = authstats.setdefault(a["name"], {"count": 0, "orcids": set()})
                st["count"] += 1
                if a.get("orcid"):
                    st["orcids"].add(a["orcid"])
        # keep only authors affiliated with a tracked org -> these power the leaderboards
        org_authors = []
        for a in authors:
            if not a["orgs"]:
                continue
            dept = clean_dept(a.get("dept"))
            org_authors.append({"n": a["name"], "f": a["is_first"], "l": a["is_last"],
                                "o": a["orgs"], "d": dept, "cg": map_group(a["orgs"], dept)})
        mesh = [m for m in json.loads(mesh) if m not in GENERIC_MESH]
        tags = mesh[:6] if mesh else ([topic] if topic else [])
        jif = jifmap.get(issn)
        themes = json.loads(themes or "[]")
        nctn = len(json.loads(nct or "[]"))
        papers.append({
            "p": pmid, "t": title, "j": journal, "y": year, "e": entry,
            "doi": doi, "o": orgs, "a": org_authors, "tg": tags,
            "n": len(authors),               # total author count (hyperauthorship filter)
            "if": round(jif, 2) if jif is not None else None,  # open IF-equivalent (filter on full value)
            "g": 1 if is_gen else 0,          # genetics paper?
            "fs": first_seen,                 # date our tracker first saw it
            "th": themes,                     # research themes
            "st": stype or "Other",           # study-type bucket
            "nt": nctn,                       # number of ClinicalTrials.gov IDs
            "c": cit,                         # all-time citation count (may be null)
            "rc": rec_cit,                    # citations earned in the last ~2 years ("hot now")
            "r": round(rcr, 2) if rcr is not None else None,  # field-normalized RCR
        })
    con.close()

    # collapse name variants of the same person (conservative) and relabel everywhere
    canon = build_canonical_map(authstats)
    merged = sum(1 for k, v in canon.items() if k != v)
    for p in papers:
        for a in p["a"]:
            a["n"] = canon.get(a["n"], a["n"])
    print(f"  author merging: {len(authstats)} name variants -> "
          f"{len(set(canon.values()))} people ({merged} variants merged)")

    papers.sort(key=lambda x: (x["e"] or ""), reverse=True)
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_run": last_run, "today": today, "year": datetime.date.today().year,
        "orgs": ORG_NAMES, "themes": THEME_NAMES, "theme_kw": THEME_KW, "papers": papers,
    }


def main():
    payload = build_payload()
    htmlout = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(htmlout)
    print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB, {len(payload['papers'])} papers)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Output — Cook Children's & UT Arlington</title>
<style>
  :root{
    --bg:#0f1419; --panel:#1a2230; --panel2:#222d3d; --line:#2c3a4f;
    --txt:#e7edf5; --muted:#a6b8d1; --accent:#4ea1ff; --cook:#ff7a59; --uta:#5ad1a8;
    --bar:#33425a; --txt2:#c4d2e6;
  }
  html.light{
    --bg:#f4f7fb; --panel:#ffffff; --panel2:#eef2f8; --line:#d6e0ec;
    --txt:#1a2230; --muted:#566377; --accent:#2570d4; --cook:#cf5430; --uta:#1f9c77;
    --bar:#dde6f0; --txt2:#33425a;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:var(--bg);color:var(--txt)}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  header{padding:20px 26px;border-bottom:1px solid var(--line);
         display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px}
  h1{margin:0;font-size:20px;font-weight:650}
  .sub{color:var(--muted);font-size:12.5px;margin-top:3px}
  .wrap{padding:18px 26px;max-width:1280px;margin:0 auto}
  .controls{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:18px;
            background:var(--panel);padding:14px 16px;border-radius:12px;border:1px solid var(--line)}
  .cgroup{display:flex;flex-direction:column;gap:6px}
  .clabel{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
  .btns{display:flex;gap:6px;flex-wrap:wrap}
  .btn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
       padding:5px 11px;border-radius:8px;cursor:pointer;font-size:12.5px}
  .btn:hover{border-color:var(--accent)}
  .btn.active{background:var(--accent);border-color:var(--accent);color:#06121f;font-weight:600}
  .btn.cook.active{background:var(--cook);border-color:var(--cook)}
  .btn.uta.active{background:var(--uta);border-color:var(--uta)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card .n{font-size:26px;font-weight:700}
  .card .k{color:var(--muted);font-size:12px;margin-top:2px}
  .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
  @media(max-width:980px){.grid{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
  .panel h2{margin:0 0 12px;font-size:13.5px;font-weight:650;letter-spacing:.02em}
  .panel h2 .hint{color:var(--muted);font-weight:400;font-size:11.5px;margin-left:6px}
  .row{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:13px}
  .row .lab{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row .barwrap{flex:1.4;background:var(--bar);border-radius:5px;height:14px;overflow:hidden}
  .row .barfill{display:block;height:100%;background:var(--accent);border-radius:5px}
  .row .v{width:34px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
  .tag{display:inline-block;font-size:11px;padding:1px 5px;border-radius:4px;margin-left:5px;vertical-align:middle}
  .tag.cook{background:rgba(255,122,89,.18);color:var(--cook)}
  .tag.uta{background:rgba(90,209,168,.18);color:var(--uta)}
  table{width:100%;border-collapse:collapse}
  .plist{max-height:560px;overflow:auto}
  .pitem{padding:10px 0;border-bottom:1px solid var(--line)}
  .pitem .pt{font-weight:550;font-size:13.5px}
  .pitem .pm{color:var(--muted);font-size:12px;margin-top:2px}
  .pitem .pa{font-size:12px;margin-top:3px;color:var(--txt2)}
  .pitem b{color:var(--txt)}
  select,input{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
               border-radius:8px;padding:5px 9px;font-size:12.5px}
  .flex{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--muted)}
  .chart{display:flex;align-items:flex-end;gap:6px;height:130px;margin-top:8px}
  .chart .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:4px}
  .chart .cb{width:100%;background:var(--accent);border-radius:4px 4px 0 0;min-height:2px}
  .chart .cl{font-size:11px;color:var(--muted)}
  .chart .cv{font-size:11px;color:var(--muted)}
  .sechead{margin:26px 0 4px;font-size:16px;font-weight:650}
  .secsub{color:var(--muted);font-size:12.5px;margin-bottom:14px}
  .delta{font-size:12px;margin-left:7px;font-weight:600}
  .up{color:var(--uta)} .down{color:var(--cook)} .flat{color:var(--muted)}
  .atab{width:100%;border-collapse:collapse;font-size:12.5px}
  .atab th{text-align:left;color:var(--muted);font-weight:500;padding:5px 6px;border-bottom:1px solid var(--line);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
  .atab td{padding:5px 6px;border-bottom:1px solid var(--line)}
  .atab td.num{text-align:right;font-variant-numeric:tabular-nums}
  .atab tr:hover td{background:var(--panel2)}
  .badge{font-size:10px;background:var(--uta);color:#06121f;border-radius:4px;padding:1px 5px;margin-left:6px;font-weight:600;vertical-align:middle}
  .alink{cursor:pointer;border-bottom:1px dotted var(--muted)}
  .alink:hover{color:var(--accent);border-bottom-color:var(--accent)}
  .selrow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;align-items:center}
  .live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--uta);box-shadow:0 0 0 0 rgba(90,209,168,.6);animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(90,209,168,.5)}70%{box-shadow:0 0 0 7px rgba(90,209,168,0)}100%{box-shadow:0 0 0 0 rgba(90,209,168,0)}}
  .stack{display:flex;align-items:flex-end;gap:6px;height:150px;margin-top:10px}
  .stack .scol{flex:1;display:flex;flex-direction:column-reverse;height:100%;border-radius:4px;overflow:hidden}
  .stack .seg{width:100%}
  .stklabels{display:flex;gap:6px;margin-top:4px}
  .stklabels .sl{flex:1;text-align:center;font-size:11px;color:var(--muted)}
  .legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11.5px;color:var(--muted)}
  .legend span{display:inline-flex;align-items:center;gap:5px}
  .legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
  .modal{position:fixed;inset:0;background:rgba(4,9,16,.72);display:none;z-index:50;padding:30px;overflow:auto}
  .modal.show{display:block}
  .mbox{max-width:760px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px 24px}
  .mbox .x{float:right;cursor:pointer;color:var(--muted);font-size:22px;line-height:1}
  .mbox h3{margin:0 0 4px;font-size:18px}
  .mstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(95px,1fr));gap:10px;margin:14px 0}
  .mstats .ms{background:var(--panel2);border-radius:8px;padding:8px 10px}
  .mstats .ms .mn{font-size:19px;font-weight:700}
  .mstats .ms .mk{font-size:11px;color:var(--muted)}
  .mplist{max-height:300px;overflow:auto;margin-top:8px}
  .cloud{display:flex;flex-wrap:wrap;gap:2px 12px;align-items:baseline;line-height:1.7;padding:2px 0}
  .cloud span{color:var(--accent);font-weight:500;cursor:default}
  .alink:hover{background:var(--panel2);border-radius:3px}
  .tag.cook::before{content:"\25C6 "}
  .tag.uta::before{content:"\25B2 "}
  .topnav{position:sticky;top:0;z-index:30;background:var(--bg);border-bottom:1px solid var(--line);display:flex;gap:4px;padding:8px 26px;flex-wrap:wrap;align-items:center}
  .topnav a{color:var(--muted);font-size:12.5px;padding:4px 10px;border-radius:7px;text-decoration:none}
  .topnav a:hover{background:var(--panel2);color:var(--txt);text-decoration:none}
  .topnav .nlabel{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-right:4px}
  .exec{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin-bottom:14px}
  .exec h2{margin:0 0 8px;font-size:15px;font-weight:650}
  .exec .lead{font-size:14px;line-height:1.55;margin-bottom:14px;color:var(--txt2)}
  .exec .lead b{color:var(--txt)}
  .bigcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:8px}
  .bc{background:var(--panel2);border-radius:10px;padding:12px 14px}
  .bc .n{font-size:27px;font-weight:700}
  .bc .k{font-size:12px;color:var(--muted);margin-top:2px}
  .chip{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:2px 10px;font-size:12px;margin:0 6px 6px 0}
  .chip .xx{cursor:pointer;color:var(--muted);font-weight:700}
  .chip .xx:hover{color:var(--cook)}
  .helptip{cursor:help;border-bottom:1px dotted var(--muted)}
  [id^="sec-"]{scroll-margin-top:56px}
  .sm{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
  .smcell{background:var(--panel2);border-radius:10px;padding:10px 12px}
  .smcell .smt{font-size:12.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .smcell .smn{font-size:11px;color:var(--muted);margin-bottom:4px}
  .smcell .chart{height:54px;gap:3px;margin-top:0}
  .smcell .chart .cv{display:none}
  @media print{
    html.light,html{--bg:#fff;--panel:#fff;--panel2:#f4f4f4;--line:#ccc;--txt:#000;--muted:#444;--bar:#ddd;--txt2:#222}
    body{background:#fff}
    .topnav,.controls,#activeFilters,.btns,#csvBtn,#copyLink,#pdfBtn,#themeToggle,#authorSearch,.live{display:none !important}
    .panel,.exec,.card,.bc{break-inside:avoid;border:1px solid #ccc}
    .plist,.mplist{max-height:none !important;overflow:visible !important}
    a{color:#000;text-decoration:none}
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>Research Output Dashboard</h1>
    <div class="sub">Cook Children's &amp; UT Arlington · published papers indexed in PubMed</div>
  </div>
  <div class="sub" id="meta"></div>
</header>
<nav class="topnav">
  <span class="nlabel">Jump to</span>
  <a href="#sec-exec">Executive</a>
  <a href="#sec-overview">Overview</a>
  <a href="#sec-people">People</a>
  <a href="#sec-departments">Departments</a>
  <a href="#sec-explore">Explore</a>
  <a href="#sec-trends">Trends</a>
  <button class="btn" id="themeToggle" style="margin-left:auto;font-size:12px">☀ Light mode</button>
</nav>
<div class="wrap">
  <div class="exec" id="sec-exec">
    <h2>Executive snapshot <span class="hint" style="font-weight:400" id="execScope"></span></h2>
    <div class="lead" id="execLead"></div>
    <div class="bigcards" id="execCards"></div>
  </div>
  <div class="sub" style="margin-bottom:14px;line-height:1.5;border-left:3px solid var(--accent);padding-left:10px">
    <b>Coverage:</b> PubMed-indexed journal articles only — books, conference papers, patents, and grants are <b>not</b> included (so fields that publish mainly in those venues are undercounted).
    <b>Citations:</b> OpenAlex. <b>RCR</b> (field-normalized impact): NIH iCite, where 1.0 = the average NIH-funded paper — use it to compare across disciplines fairly.
    <b>Journal impact:</b> OpenAlex 2-yr mean citedness (close to Impact Factor; not the official Clarivate value).
    <br><b>Author &amp; group metrics are affiliation-scoped:</b> they count only papers carrying a Cook Children's / UT Arlington affiliation — <b>not</b> an individual's full career. A researcher who recently joined (e.g. in 2026) shows only papers published since, even if they have a long prior record elsewhere.
  </div>
  <div class="controls">
    <div class="cgroup">
      <span class="clabel">Organization</span>
      <div class="btns" id="orgBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Time window <span class="hint" style="font-weight:400;text-transform:none;letter-spacing:0">· click multiple years to combine them</span></span>
      <div class="btns" id="timeBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Journal impact <span title="Open IF-equivalent: OpenAlex 2-year mean citedness. Same formula as Impact Factor on open citation data. Papers in journals with no available metric are hidden when a threshold is on." style="cursor:help;color:var(--accent)">&#9432;</span></span>
      <div class="btns" id="ifBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Research theme <span class="hint" id="themeHint" style="font-weight:400"></span></span>
      <div class="btns" id="themeBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Study type</span>
      <div class="btns"><select id="stypeSel"></select></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Split breakdown by</span>
      <div class="btns" id="splitBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Find an author</span>
      <div class="btns"><input id="authorSearch" list="authorList" placeholder="type a name…" style="min-width:190px"><datalist id="authorList"></datalist></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Options</span>
      <div class="btns">
        <label class="btn helptip" style="cursor:pointer" title="Papers with more than 50 authors (e.g. large physics/genomics consortia) can dominate the counts. Excluding them reflects individual and typical-team output."><input type="checkbox" id="hyper" checked style="vertical-align:middle;margin-right:5px">Exclude mega-collaboration papers (&gt;50 authors)</label>
        <button class="btn" id="copyLink">🔗 Copy link to this view</button>
        <button class="btn" id="csvBtn">⬇ Download CSV</button>
        <button class="btn" id="pdfBtn">🖨 Print / Save as PDF</button>
      </div>
    </div>
  </div>

  <div id="activeFilters" style="margin:0 0 14px"></div>

  <div id="sec-overview"></div>
  <div class="cards" id="cards"></div>
  <div class="sub" style="margin:-8px 0 14px;font-size:11.5px">Citations from OpenAlex (may differ from Google Scholar). “Clinical trials” = papers PubMed tags as trials/RCTs. <b>Tip:</b> click any author name or department to open its profile/scorecard.</div>

  <div class="panel">
    <h2>What's new <span class="hint">papers added to PubMed in the last 14 days · current organization filter</span>
      <span id="newCount" class="badge" style="background:var(--accent)"></span></h2>
    <div class="plist" id="whatsnew" style="max-height:230px"></div>
  </div>

  <div class="panel">
    <h2>Papers per year <span class="hint">by publication year · current organization filter</span></h2>
    <div class="chart" id="chart"></div>
  </div>

  <div id="sec-people"></div>
  <div class="grid">
    <div class="panel"><h2>Most published <span class="hint">any authorship</span></h2><div id="lbAny"></div></div>
    <div class="panel"><h2>Most first-author <span class="hint helptip" title="First author — usually the person who led the day-to-day work (often a trainee or early-career researcher).">lead author</span></h2><div id="lbFirst"></div></div>
    <div class="panel"><h2>Most last-author <span class="hint helptip" title="Last (senior) author — typically the lab head / principal investigator overseeing the work.">senior author</span></h2><div id="lbLast"></div></div>
  </div>

  <div id="sec-departments"></div>
  <div class="panel"><h2>What the work is about <span class="hint">most frequent words in paper titles · current filter</span></h2><div class="cloud" id="cloudMain"></div></div>

  <div class="panel"><h2>Most cited papers <span class="hint" id="citHint"></span>
    <span style="float:right;display:flex;gap:6px"><button class="btn helptip" id="citAll" data-cm="all" title="Rank by total lifetime citations">All-time</button><button class="btn helptip" id="citRecent" data-cm="recent" title="Rank by citations earned in the last ~2 years — current momentum, not lifetime totals">⚡ Hot now</button></span></h2>
    <div class="plist" id="mostcited" style="max-height:320px"></div></div>

  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="panel"><h2 id="splitTitle">Breakdown</h2><div id="splitBox"></div></div>
    <div class="panel">
      <h2>Recent papers <span class="hint">newest first</span></h2>
      <div class="flex" style="margin-bottom:8px">
        <input id="search" placeholder="filter by author / title / journal…" style="flex:1">
      </div>
      <div class="plist" id="plist"></div>
    </div>
  </div>
  <div class="sechead" id="sec-explore">Explore &amp; customize</div>
  <div class="secsub">Build any chart you like and inspect impact tiers. These respond to all the filters above.</div>
  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="panel">
      <h2>Build-your-own chart</h2>
      <div class="selrow">
        <label class="muted" style="font-size:12px">Show&nbsp;<select id="cMetric"></select></label>
        <label class="muted" style="font-size:12px">broken down by&nbsp;<select id="cDim"></select></label>
      </div>
      <div id="customChart"></div>
    </div>
    <div class="panel">
      <h2>Journal impact tiers <span class="hint">share of papers by impact band, over time</span></h2>
      <div id="tierBars" style="margin-bottom:6px"></div>
      <div class="stack" id="tierStack"></div>
      <div class="stklabels" id="tierStackLabels"></div>
      <div class="legend" id="tierLegend"></div>
    </div>
  </div>

  <div class="sechead" id="sec-trends">Leadership analytics &amp; trends</div>
  <div class="secsub" id="secsub">Respects the Organization, Impact and Genetics filters above; spans all years (ignores the time window). Year-over-year cards compare the last complete year.</div>

  <div class="cards" id="kpis"></div>

  <div class="grid">
    <div class="panel"><h2>Average journal impact <span class="hint">by year · IF-equivalent</span></h2><div class="chart" id="chartIF"></div></div>
    <div class="panel"><h2>High-impact share <span class="hint">% of papers with IF&gt;7, by year</span></h2><div class="chart" id="chartHi"></div></div>
    <div class="panel"><h2>Cook &harr; UT Arlington collaboration <span class="hint">joint papers by year</span></h2><div class="chart" id="chartCollab"></div></div>
  </div>

  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="panel"><h2>Top departments <span class="hint">output &amp; quality · current filter</span></h2>
      <table class="atab"><thead><tr><th>Department</th><th class="num">Papers</th><th class="num"><span class="helptip" title="Journal impact: OpenAlex 2-year mean citedness (≈ Impact Factor)">Mean IF</span></th><th class="num"><span class="helptip" title="Relative Citation Ratio (NIH iCite): field- and time-normalized; 1.0 = the average NIH-funded paper. Best for comparing across disciplines.">RCR</span></th><th class="num">High-impact</th></tr></thead><tbody id="deptTable"></tbody></table></div>
    <div class="panel"><h2>Rising authors <span class="hint">most active in the last 24 months</span></h2>
      <table class="atab"><thead><tr><th>Author</th><th class="num">Last 24 mo</th><th class="num">Total</th></tr></thead><tbody id="rising"></tbody></table></div>
  </div>

  <div class="panel"><h2>Output trend by group <span class="hint">papers per year for each college / service line · current filters</span></h2><div class="sm" id="smGroups"></div></div>

  <div class="sub" style="margin-top:18px">
    <b>How to read time windows:</b> <i>Today / Last 7 / Last 30 days</i> count papers newly
    added to PubMed in that span. The <i>year</i> buttons count papers by their publication year.
    Author affiliation is detected per-author, so first/last-author tallies reflect who at each
    organization actually led or was senior on the paper. Department &amp; topic are auto-derived
    and approximate.
  </div>
</div>

<div class="modal" id="authorModal"><div class="mbox" id="authorBox"></div></div>

<script>
const DATA = __DATA__;
const ORG = DATA.orgs;
const state = {org:"all", rel:"all", years:[], split:"dept", search:"", hyper:true, ifmin:0, themes:[], stype:"all", cmetric:"papers", cdim:"year", citmode:"all", theme:"dark"};
const THEMES = DATA.themes || {};
const HYPER_MAX = 50;  // papers with more authors than this are "mega-collaborations"
const IF_LEVELS = [[0,"Any"],[3,"&gt; 3"],[7,"&gt; 7"],[20,"&gt; 20"]];

// ---- time predicates
function daysAgo(n){const d=new Date(DATA.today+"T00:00:00");d.setDate(d.getDate()-n);return d.toISOString().slice(0,10);}
// relative windows (single-select) vs years (multi-select)
const RELS = [["today","Today",p=>p.e===DATA.today],
              ["w","Last 7 days",p=>p.e&&p.e>=daysAgo(7)],
              ["m","Last 30 days",p=>p.e&&p.e>=daysAgo(30)],
              ["all","All time",p=>true]];
const YEARS = [];
for(let y=DATA.year;y>=2020;y--) YEARS.push(y);
const SPLITS=[["dept","Department"],["college","College/Line"],["theme","Theme"],["study","Study type"],["topic","Topic"],["journal","Journal"]];

function orgOk(p){return state.org==="all"||p.o.includes(state.org);}
function timeOk(p){
  if(state.years.length) return state.years.includes(p.y);   // one or more years selected
  const r=RELS.find(t=>t[0]===state.rel);
  return r?r[2](p):true;
}
function authOrgOk(a){return state.org==="all"||a.o.includes(state.org);}

function ifOk(p){return state.ifmin===0 || (p.if!=null && p.if>state.ifmin);}
function hyperOk(p){return !state.hyper || p.n<=HYPER_MAX;}
function themeOk(p){return state.themes.length===0 || state.themes.some(t=>p.th.includes(t));}
function studyOk(p){
  if(state.stype==="all")return true;
  if(state.stype==="trials")return p.st==="Randomized controlled trial"||p.st==="Clinical trial";
  return p.st===state.stype;
}
function filtered(){return DATA.papers.filter(p=>orgOk(p)&&timeOk(p)&&ifOk(p)&&hyperOk(p)&&themeOk(p)&&studyOk(p));}

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
const THEME_KW=DATA.theme_kw||{};
// "why this theme": which keywords matched in the visible text (title + topic tags); else "abstract"
function whyTheme(p,tid){
  const hay=((p.t||"")+" "+(p.tg||[]).join(" ")).toLowerCase();
  const hits=(THEME_KW[tid]||[]).filter(k=>hay.includes(k));
  return hits.length?("matched: "+hits.join(", ")):"matched in the abstract";
}
function themeTags(p){
  return (p.th||[]).map(t=>`<span class="tag" style="background:rgba(78,161,255,.16);color:var(--accent)" title="Theme: ${esc(THEMES[t]||t)} — ${esc(whyTheme(p,t))}">${esc(THEMES[t]||t)}</span>`).join("");
}
// a paper is cross-college if its org-authors span 2+ named colleges/service lines
function paperColleges(p){return [...new Set(p.a.filter(authOrgOk).map(a=>a.cg).filter(c=>c&&c!=="Other"))];}
function crossTag(p){return paperColleges(p).length>=2?`<span class="tag" style="background:rgba(245,196,91,.18);color:#f5c45b" title="Cross-college: ${esc(paperColleges(p).join(" + "))}">cross-college</span>`:"";}

// ---- title word cloud (semantic snapshot from paper titles) ----
const STOP=new Set("the a an of in on for and or to with by from as at is are was were be been being this that these those it its their our we us you your using use used uses via into between among across after before during over under within without due per also however not no nor only than then thus more most less least very can may might will would should could has have had do does did but if so such which who whom whose what when where why how all any each both either neither some many few much several other others novel new study studies analysis analyses results result based case cases report reports review reviews effect effects role toward towards versus vs et al cohort pilot data approach assessment evaluation comparison compared associated association outcomes outcome findings finding evidence potential significant high low patient patients see seen states united show shows".split(/\s+/));
function titleWords(papers){const m={};papers.forEach(p=>{(p.t||"").toLowerCase().split(/[^a-z0-9]+/).forEach(w=>{if(w.length<3||STOP.has(w)||/^\d+$/.test(w))return;m[w]=(m[w]||0)+1;});});return Object.entries(m).sort((a,b)=>b[1]-a[1]).slice(0,45);}
function cloudHTML(papers){const ws=titleWords(papers);if(ws.length<3)return '<span class="muted">Not enough titles in this selection.</span>';const max=ws[0][1],min=ws[ws.length-1][1],lo=Math.sqrt(min),hi=Math.sqrt(max);const t=c=>hi>lo?(Math.sqrt(c)-lo)/(hi-lo):0.5;return ws.map(([w,c])=>`<span title="${esc(w)}: ${c} papers" style="font-size:${11+Math.round(t(c)*19)}px;opacity:${(0.55+0.45*t(c)).toFixed(2)}">${esc(w)}</span>`).join(" ");}

// ---- leaderboards
function leaderboard(papers, kind){
  const m={};
  papers.forEach(p=>{
    p.a.forEach(a=>{
    if(!authOrgOk(a))return;
    if(kind==="first"&&!a.f)return;
    if(kind==="last"&&!a.l)return;
    if(!m[a.n])m[a.n]={n:a.n,c:0,o:new Set()};
    m[a.n].c++; a.o.forEach(o=>m[a.n].o.add(o));
    });
  });
  return Object.values(m).sort((x,y)=>y.c-x.c).slice(0,15);
}
function renderLB(id,rows){
  const max=rows.length?rows[0].c:1;
  document.getElementById(id).innerHTML = rows.length? rows.map(r=>{
    const tags=[...r.o].map(o=>`<span class="tag ${o}">${esc(ORG[o])}</span>`).join("");
    return `<div class="row"><span class="lab"><span class="alink" data-n="${esc(r.n)}">${esc(r.n)}</span>${tags}</span>
      <span class="barwrap"><span class="barfill" style="width:${100*r.c/max}%"></span></span>
      <span class="v">${r.c}</span></div>`;
  }).join("") : `<div class="muted">No papers in this window.</div>`;
}

// ---- generic split (dept/college are click-through to a group scorecard)
function splitDef(){
  switch(state.split){
    case "college": return [p=>[...new Set(p.a.filter(authOrgOk).map(a=>a.cg).filter(Boolean))], "college / service line", "college"];
    case "theme":   return [p=>(p.th||[]).map(t=>THEMES[t]||t), "research theme", null];
    case "study":   return [p=>[p.st||"Other"], "study type", null];
    case "topic":   return [p=>p.tg||[], "topic", null];
    case "journal": return [p=>p.j?[p.j]:[], "journal", null];
    default:        return [p=>[...new Set(p.a.filter(authOrgOk).map(a=>a.d).filter(Boolean))], "department", "dept"];
  }
}
function renderSplit(papers){
  const [keysFn, title, clickKind]=splitDef();
  const m={};
  papers.forEach(p=>keysFn(p).forEach(k=>{m[k]=(m[k]||0)+1;}));
  document.getElementById("splitTitle").innerHTML=`Breakdown by ${title} <span class="hint">current filter${clickKind?" · click a row to open its scorecard":""}</span>`;
  const rows=Object.entries(m).sort((a,b)=>b[1]-a[1]).slice(0,18);
  const max=rows.length?rows[0][1]:1;
  document.getElementById("splitBox").innerHTML = rows.length? rows.map(([k,v])=>
    `<div class="row"><span class="lab${clickKind?" alink":""}" ${clickKind?`data-grp="${clickKind}" data-val="${esc(k)}"`:""}>${esc(k)}</span>
     <span class="barwrap"><span class="barfill" style="width:${100*v/max}%"></span></span>
     <span class="v">${v}</span></div>`).join("") : `<div class="muted">No data in this window.</div>`;
}

// ---- year chart (respects org filter only, so the trend is always visible)
function renderChart(){
  const yrs={};
  DATA.papers.filter(p=>orgOk(p)&&ifOk(p)&&hyperOk(p)&&themeOk(p)&&studyOk(p)).forEach(p=>{if(p.y&&p.y>=2016)yrs[p.y]=(yrs[p.y]||0)+1;});
  const keys=Object.keys(yrs).map(Number).sort();
  const max=Math.max(1,...Object.values(yrs));
  document.getElementById("chart").innerHTML=keys.map(y=>{
    const v=yrs[y];const sel=state.years.includes(y);
    return `<div class="col" title="${y}: ${v}">
      <span class="cv">${v}</span>
      <div class="cb" style="height:${Math.round(70*v/max)}px;${sel?'background:var(--cook)':''}"></div>
      <span class="cl">${y}</span></div>`;
  }).join("");
}

// ---- cards
function renderCards(papers){
  const newToday=DATA.papers.filter(p=>orgOk(p)&&p.e===DATA.today).length;
  const new7=DATA.papers.filter(p=>orgOk(p)&&p.e&&p.e>=daysAgo(7)).length;
  const thisYr=DATA.papers.filter(p=>orgOk(p)&&p.y===DATA.year).length;
  const auth=new Set();papers.forEach(p=>p.a.forEach(a=>{if(authOrgOk(a))auth.add(a.n);}));
  let cites=0,trials=0;papers.forEach(p=>{cites+=(p.c||0);if(p.st==="Randomized controlled trial"||p.st==="Clinical trial")trials++;});
  const cards=[
    [papers.length,"papers in selection"],
    [new7,"new last 7 days"],
    [thisYr,"published this year"],
    [auth.size,"distinct authors"],
    [cites,"total citations"],
    [trials,"clinical trials"],
  ];
  document.getElementById("cards").innerHTML=cards.map(c=>
    `<div class="card"><div class="n">${c[0].toLocaleString()}</div><div class="k">${c[1]}</div></div>`).join("");
}

// ---- recent list
function renderList(papers){
  const q=state.search.toLowerCase();
  let rows=papers;
  if(q)rows=papers.filter(p=>(p.t+" "+p.j+" "+p.a.map(a=>a.n).join(" ")).toLowerCase().includes(q));
  rows=rows.slice(0,200);
  document.getElementById("plist").innerHTML=rows.map(p=>{
    const names=p.a.map(a=>`<b class="alink" data-n="${esc(a.n)}">${esc(a.n)}</b>${a.f?" (first)":a.l?" (last)":""}`).join(", ")||"<span class='muted'>—</span>";
    const tags=p.o.map(o=>`<span class="tag ${o}">${esc(ORG[o])}</span>`).join("");
    const url=p.doi?("https://doi.org/"+p.doi):("https://pubmed.ncbi.nlm.nih.gov/"+p.p);
    const ifb=p.if!=null?` · IF≈${p.if.toFixed(1)}`:"";
    const cb=p.c!=null?` · cited ${p.c}`:"";
    const sb=(p.st&&p.st!=="Other")?` · ${esc(p.st)}`:"";
    const gb=p.g?` · <span style="color:var(--uta)">genetics</span>`:"";
    return `<div class="pitem">
      <div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a> ${tags} ${themeTags(p)} ${crossTag(p)}</div>
      <div class="pm">${esc(p.j)} · ${p.y||"n/a"} · added ${p.e||"n/a"}${ifb}${cb}${sb} · PMID ${p.p}</div>
      <div class="pa">${names}</div></div>`;
  }).join("")||"<div class='muted'>No papers match.</div>";
}

// ---- leadership analytics (respect org/impact/genetics, span all years) ----
function analyticsBase(){return DATA.papers.filter(p=>orgOk(p)&&ifOk(p)&&hyperOk(p)&&themeOk(p)&&studyOk(p));}
function mean(arr){return arr.length?arr.reduce((a,b)=>a+b,0)/arr.length:0;}
function isCollab(p){return p.o.includes("cook")&&p.o.includes("uta");}

function yearAgg(papers){
  const a={};
  papers.forEach(p=>{
    if(!p.y||p.y<2016||p.y>DATA.year)return;
    const y=a[p.y]||(a[p.y]={n:0,ifs:[],hi:0,collab:0});
    y.n++; if(p.if!=null){y.ifs.push(p.if); if(p.if>7)y.hi++;} if(isCollab(p))y.collab++;
  });
  return a;
}
function cols(id,keys,vals,disp){
  const max=Math.max(1,...vals);
  document.getElementById(id).innerHTML=keys.map((k,i)=>
    `<div class="col" title="${k}: ${disp(vals[i])}"><span class="cv">${disp(vals[i])}</span>`
    +`<div class="cb" style="height:${Math.round(70*vals[i]/max)}px"></div><span class="cl">${k}</span></div>`).join("");
}
function deltaHTML(cur,prev,kind){
  if(prev==null||prev===0&&cur===0) return '<span class="delta flat">–</span>';
  let d,txt,cls;
  if(kind==="pct"){d=cur-prev; txt=(d>=0?"+":"")+d.toFixed(0)+" pts";}
  else if(kind==="abs"){d=cur-prev; txt=(d>=0?"+":"")+d.toFixed(1);}
  else if(kind==="count"){d=cur-prev; txt=(d>=0?"+":"")+d;}
  else {d=prev?((cur-prev)/prev*100):0; txt=(d>=0?"+":"")+d.toFixed(0)+"%";}
  cls=d>0?"up":(d<0?"down":"flat");
  const arrow=d>0?"▲":(d<0?"▼":"");
  return `<span class="delta ${cls}">${arrow} ${txt}</span>`;
}
function renderAnalytics(){
  const base=analyticsBase();
  const a=yearAgg(base);
  const keys=Object.keys(a).map(Number).sort((x,y)=>x-y);
  const cy=DATA.year, y1=cy-1, y0=cy-2;       // last complete year vs prior
  const g=y=>a[y]||{n:0,ifs:[],hi:0,collab:0};
  const c=g(y1), p=g(y0);
  const avgC=mean(c.ifs), avgP=mean(p.ifs);
  const hiC=c.n?100*c.hi/c.n:0, hiP=p.n?100*p.hi/p.n:0;
  const cards=[
    ["Publications "+y1, c.n.toLocaleString(), deltaHTML(c.n,p.n,"rel"), "vs "+y0],
    ["Avg journal impact "+y1, avgC.toFixed(1), deltaHTML(avgC,avgP,"abs"), "vs "+y0],
    ["High-impact share "+y1, hiC.toFixed(0)+"%", deltaHTML(hiC,hiP,"pct"), "IF>7 · vs "+y0],
    ["Cook↔UTA papers "+y1, c.collab, deltaHTML(c.collab,p.collab,"count"), "joint · vs "+y0],
  ];
  document.getElementById("kpis").innerHTML=cards.map(k=>
    `<div class="card"><div class="n" style="font-size:24px">${k[1]}<span style="font-size:13px">${k[2]}</span></div>`
    +`<div class="k">${k[0]} <span style="opacity:.7">(${k[3]})</span></div></div>`).join("");

  cols("chartIF",keys,keys.map(y=>mean(a[y].ifs)),v=>v.toFixed(1));
  cols("chartHi",keys,keys.map(y=>a[y].n?100*a[y].hi/a[y].n:0),v=>Math.round(v)+"%");
  cols("chartCollab",keys,keys.map(y=>a[y].collab),v=>v);

  // top departments (count distinct papers per dept)
  const dep={};
  base.forEach(pp=>{
    const ds=new Set(pp.a.filter(authOrgOk).map(x=>x.d).filter(Boolean));
    ds.forEach(d=>{const o=dep[d]||(dep[d]={n:0,ifs:[],hi:0,rcrs:[]}); o.n++; if(pp.if!=null){o.ifs.push(pp.if); if(pp.if>7)o.hi++;} if(pp.r!=null)o.rcrs.push(pp.r);});
  });
  const drows=Object.entries(dep).sort((x,y)=>y[1].n-x[1].n).slice(0,12);
  document.getElementById("deptTable").innerHTML=drows.length?drows.map(([d,o])=>
    `<tr><td><span class="alink" data-grp="dept" data-val="${esc(d)}">${esc(d)}</span></td><td class="num">${o.n}</td><td class="num">${o.ifs.length?mean(o.ifs).toFixed(1):"–"}</td><td class="num">${o.rcrs.length?mean(o.rcrs).toFixed(2):"–"}</td><td class="num">${o.hi}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">No data.</td></tr>`;

  // rising authors: most papers in last 24 months; "new" if first appeared in that window
  const cutoff=daysAgo(730), au={};
  base.forEach(pp=>{
    const recent = pp.e && pp.e>=cutoff;
    pp.a.forEach(x=>{ if(!authOrgOk(x))return;
      const o=au[x.n]||(au[x.n]={tot:0,recent:0,first:"9999-99-99"});
      o.tot++; if(recent)o.recent++; if((pp.e||"9999")<o.first)o.first=pp.e||o.first;
    });
  });
  const arows=Object.entries(au).filter(([,o])=>o.recent>0).sort((x,y)=>y[1].recent-x[1].recent).slice(0,12);
  document.getElementById("rising").innerHTML=arows.length?arows.map(([n,o])=>
    `<tr><td><span class="alink" data-n="${esc(n)}">${esc(n)}</span>${o.first>=cutoff?'<span class="badge">new</span>':''}</td><td class="num">${o.recent}</td><td class="num">${o.tot}</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">No recent activity in this selection.</td></tr>`;

  // concentration (top-10 authors' share of total authorships)
  const totals=Object.values(au).map(o=>o.tot).sort((x,y)=>y-x);
  const sum=totals.reduce((s,v)=>s+v,0);
  const top10=totals.slice(0,10).reduce((s,v)=>s+v,0);
  const conc=sum?Math.round(100*top10/sum):0;
  document.getElementById("secsub").innerHTML=
    `Respects the Organization, Impact, Theme and Study-type filters above; spans all years (ignores the time window). `
    +`<b>Research concentration:</b> the top 10 authors account for <b>${conc}%</b> of output in the current selection `
    +`(${Object.keys(au).length.toLocaleString()} distinct authors).`;
}

// ---- build-your-own chart ----
const METRICS={
  papers:["number of papers", g=>g.papers, v=>v.toLocaleString()],
  avgif:["average journal impact", g=>g.ifs.length?mean(g.ifs):0, v=>v.toFixed(1)],
  citations:["total citations", g=>g.cit, v=>v.toLocaleString()],
  meancit:["mean citations / paper", g=>g.papers?g.cit/g.papers:0, v=>v.toFixed(1)],
  meanrcr:["mean RCR (field-normalized)", g=>g.rcrs.length?mean(g.rcrs):0, v=>v.toFixed(2)],
  authors:["distinct authors", g=>g.auth.size, v=>v.toLocaleString()],
  collab:["Cook↔UTA joint papers", g=>g.collab, v=>v],
  hishare:["high-impact share (IF>7)", g=>g.papers?100*g.hi/g.papers:0, v=>Math.round(v)+"%"],
};
const DIMS={
  year:["year", p=>p.y?[String(p.y)]:[]],
  dept:["department", p=>[...new Set(p.a.filter(authOrgOk).map(a=>a.d).filter(Boolean))]],
  college:["college / service line", p=>[...new Set(p.a.filter(authOrgOk).map(a=>a.cg).filter(Boolean))]],
  theme:["research theme", p=>(p.th||[]).map(t=>THEMES[t]||t)],
  study:["study type", p=>[p.st||"Other"]],
  topic:["topic", p=>p.tg||[]],
  journal:["journal", p=>p.j?[p.j]:[]],
  org:["organization", p=>p.o.map(o=>ORG[o])],
};
function groupBy(papers, dimFn){
  const G={};
  papers.forEach(p=>{
    dimFn(p).forEach(k=>{
      const g=G[k]||(G[k]={papers:0,ifs:[],auth:new Set(),collab:0,gen:0,hi:0,cit:0,rcrs:[]});
      g.papers++; if(p.if!=null){g.ifs.push(p.if); if(p.if>7)g.hi++;}
      if(isCollab(p))g.collab++; if(p.g)g.gen++; g.cit+=(p.c||0); if(p.r!=null)g.rcrs.push(p.r);
      p.a.forEach(a=>{if(authOrgOk(a))g.auth.add(a.n);});
    });
  });
  return G;
}
function renderCustom(){
  const papers=filtered();
  const G=groupBy(papers, DIMS[state.cdim][1]);
  const fn=METRICS[state.cmetric][1], disp=METRICS[state.cmetric][2];
  const ratio=["avgif","hishare","meancit","meanrcr"].includes(state.cmetric);
  let rows=Object.entries(G).map(([k,g])=>[k, fn(g), g.papers]);
  if(ratio && state.cdim!=="year" && state.cdim!=="org") rows=rows.filter(r=>r[2]>=3); // avoid 1-paper spikes
  if(state.cdim==="year") rows.sort((a,b)=>a[0].localeCompare(b[0]));
  else rows.sort((a,b)=>b[1]-a[1]);
  rows=rows.slice(0,20);
  const max=Math.max(1,...rows.map(r=>r[1]));
  document.getElementById("customChart").innerHTML = rows.length? rows.map(([k,v,np])=>
    `<div class="row"><span class="lab" title="${esc(k)} (${np} papers)">${esc(k)}</span>`
    +`<span class="barwrap"><span class="barfill" style="width:${100*v/max}%"></span></span>`
    +`<span class="v" style="width:58px">${disp(v)}</span></div>`).join("")
    : "<div class='muted'>No data for this selection.</div>";
}

// ---- journal impact tiers ----
const TIERS=[["20+","#4ea1ff",v=>v>20],["7–20","#5ad1a8",v=>v>7&&v<=20],
             ["3–7","#f5c45b",v=>v>3&&v<=7],["0–3","#7488a8",v=>v<=3]];
function renderTiers(){
  const papers=filtered();
  const tot=[0,0,0,0]; let unk=0;
  papers.forEach(p=>{ if(p.if==null){unk++;return;} TIERS.forEach((t,i)=>{if(t[2](p.if))tot[i]++;}); });
  const sum=tot.reduce((a,b)=>a+b,0)||1;
  document.getElementById("tierBars").innerHTML=TIERS.map((t,i)=>
    `<div class="row"><span class="lab"><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${t[1]};margin-right:6px"></i>IF ${t[0]}</span>`
    +`<span class="barwrap"><span class="barfill" style="width:${100*tot[i]/Math.max(...tot,1)}%;background:${t[1]}"></span></span>`
    +`<span class="v" style="width:54px">${tot[i]} (${Math.round(100*tot[i]/sum)}%)</span></div>`).join("")
    + (unk?`<div class="muted" style="font-size:11px;margin-top:4px">${unk} papers have no available impact metric (not shown above).</div>`:"");
  // 100%-stacked by year
  const byYear={};
  papers.forEach(p=>{ if(p.if==null||!p.y||p.y<2018||p.y>DATA.year)return;
    const a=byYear[p.y]||(byYear[p.y]=[0,0,0,0]); TIERS.forEach((t,i)=>{if(t[2](p.if))a[i]++;}); });
  const yrs=Object.keys(byYear).map(Number).sort();
  document.getElementById("tierStack").innerHTML=yrs.map(y=>{
    const a=byYear[y], s=a.reduce((x,z)=>x+z,0)||1;
    const segs=TIERS.map((t,i)=>`<div class="seg" style="height:${100*a[i]/s}%;background:${t[1]}"></div>`).join("");
    return `<div class="scol" title="${y}: ${s} papers with a metric">${segs}</div>`;
  }).join("");
  document.getElementById("tierStackLabels").innerHTML=yrs.map(y=>`<div class="sl">${y}</div>`).join("");
  document.getElementById("tierLegend").innerHTML=TIERS.map(t=>`<span><i style="background:${t[1]}"></i>IF ${t[0]}</span>`).join("");
}

// ---- what's new feed ----
function renderWhatsNew(){
  const cutoff=daysAgo(14);
  const rows=DATA.papers.filter(p=>orgOk(p)&&p.e&&p.e>=cutoff).slice(0,40);
  document.getElementById("newCount").textContent=rows.length;
  document.getElementById("whatsnew").innerHTML=rows.length?rows.map(p=>{
    const tags=p.o.map(o=>`<span class="tag ${o}">${esc(ORG[o])}</span>`).join("");
    const url=p.doi?("https://doi.org/"+p.doi):("https://pubmed.ncbi.nlm.nih.gov/"+p.p);
    const ifb=p.if!=null?` · IF≈${p.if.toFixed(1)}`:"", cb=p.c!=null?` · cited ${p.c}`:"";
    return `<div class="pitem"><div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a> ${tags}</div>
      <div class="pm">${esc(p.j)}${ifb}${cb} · added ${p.e}${p.g?' · <span style="color:var(--uta)">genetics</span>':''}</div></div>`;
  }).join(""):"<div class='muted'>No papers added in the last 14 days for this selection.</div>";
}

// ---- most cited papers ----
function renderMostCited(papers){
  const recent=state.citmode==="recent";
  const key=p=>recent?p.rc:p.c;
  document.getElementById("citHint").textContent=recent
    ? "current filter · citations earned in the last ~2 years"
    : "current filter · all-time citations (OpenAlex)";
  document.getElementById("citAll").classList.toggle("active",!recent);
  document.getElementById("citRecent").classList.toggle("active",recent);
  const rows=papers.filter(p=>key(p)!=null).slice().sort((a,b)=>key(b)-key(a)).slice(0,15);
  document.getElementById("mostcited").innerHTML=rows.length?rows.map(p=>{
    const tags=p.o.map(o=>`<span class="tag ${o}">${esc(ORG[o])}</span>`).join("");
    const url=p.doi?("https://doi.org/"+p.doi):("https://pubmed.ncbi.nlm.nih.gov/"+p.p);
    const prim=recent?`${p.rc} recent`:`cited ${p.c}`;
    const sec=recent?(p.c!=null?` · ${p.c} all-time`:""):(p.rc!=null?` · ${p.rc} recent`:"");
    return `<div class="pitem"><div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a> ${tags}</div>
      <div class="pm"><b style="color:var(--accent)">${prim}</b>${sec} · ${esc(p.j)} · ${p.y||"n/a"}${p.if!=null?` · IF≈${p.if.toFixed(1)}`:""}</div></div>`;
  }).join(""):"<div class='muted'>No citation data in this selection.</div>";
}

// ---- per-author drill-down ----
function openAuthor(name){
  const ps=DATA.papers.filter(p=>p.a.some(a=>a.n===name));
  if(!ps.length)return;
  let first=0,last=0,hi=0,cit=0; const ifs=[],orgs=new Set(),yrs={},citList=[],rcrs=[];
  ps.forEach(p=>{
    const me=p.a.find(a=>a.n===name);
    if(me){ if(me.f)first++; if(me.l)last++; me.o.forEach(o=>orgs.add(o)); }
    if(p.if!=null){ifs.push(p.if); if(p.if>7)hi++;}
    if(p.c!=null){cit+=p.c; citList.push(p.c);}
    if(p.r!=null)rcrs.push(p.r);
    if(p.y)yrs[p.y]=(yrs[p.y]||0)+1;
  });
  citList.sort((a,b)=>b-a);
  let hidx=0; while(hidx<citList.length && citList[hidx]>=hidx+1) hidx++;
  const tags=[...orgs].map(o=>`<span class="tag ${o}">${esc(ORG[o])}</span>`).join("");
  const cols=[...new Set(ps.map(p=>{const me=p.a.find(a=>a.n===name);return me&&me.cg;}).filter(c=>c&&c!=="Other"))];
  const cross=ps.filter(p=>paperColleges(p).length>=2).length;
  const yk=Object.keys(yrs).map(Number).sort(); const ymax=Math.max(1,...Object.values(yrs));
  const chart=yk.map(y=>`<div class="col" title="${y}: ${yrs[y]}"><span class="cv">${yrs[y]}</span><div class="cb" style="height:${Math.round(70*yrs[y]/ymax)}px"></div><span class="cl">${y}</span></div>`).join("");
  const stat=(n,k)=>`<div class="ms"><div class="mn">${n}</div><div class="mk">${k}</div></div>`;
  const plist=ps.slice().sort((a,b)=>(b.e||"").localeCompare(a.e||"")).slice(0,60).map(p=>{
    const me=p.a.find(a=>a.n===name); const role=me&&me.f?" (first)":me&&me.l?" (last)":"";
    const url=p.doi?("https://doi.org/"+p.doi):("https://pubmed.ncbi.nlm.nih.gov/"+p.p);
    const ifb=p.if!=null?` · IF≈${p.if.toFixed(1)}`:"", cb=p.c!=null?` · cited ${p.c}`:"";
    return `<div class="pitem"><div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a>${role}</div>
      <div class="pm">${esc(p.j)} · ${p.y||"n/a"}${ifb}${cb}${p.g?' · genetics':''}</div></div>`;
  }).join("");
  document.getElementById("authorBox").innerHTML=
    `<span class="x" onclick="closeAuthor()">&times;</span>
     <h3>${esc(name)} ${tags}</h3>
     <div class="muted" style="font-size:12px">⚠ Affiliation-scoped: only papers this person published <b>under a Cook Children's or UT Arlington affiliation</b> — <b>not</b> their full career output. Someone who recently joined (or has left) will show only their affiliated years.</div>
     ${cols.length?`<div class="muted" style="font-size:12px;margin-top:5px">Groups: ${cols.map(esc).join(" · ")}${cross?` · <b style="color:#f5c45b">${cross}</b> cross-college paper${cross>1?"s":""}`:""}</div>`:""}
     <div class="mstats">
       ${stat(ps.length,"papers")}${stat(first,"first-author")}${stat(last,"last-author")}
       ${stat(ifs.length?mean(ifs).toFixed(1):"–","avg impact")}${stat(hi,"high-impact")}
       ${stat(cit.toLocaleString(),"citations")}${stat(hidx,"h-index")}${stat(rcrs.length?mean(rcrs).toFixed(2):"–","mean RCR")}
     </div>
     <div class="hint" style="font-size:11px">Papers per year</div>
     <div class="chart" style="height:110px">${chart}</div>
     <div class="hint" style="font-size:11px;margin-top:14px">What their work is about</div>
     <div class="cloud">${cloudHTML(ps)}</div>
     <div class="hint" style="font-size:11px;margin-top:14px">Papers (newest first)</div>
     <div class="mplist">${plist}</div>`;
  document.getElementById("authorModal").classList.add("show");
}
function closeAuthor(){document.getElementById("authorModal").classList.remove("show");}

// ---- department / college group scorecard (reuses the modal shell) ----
function openGroup(kind, value){
  const sel = kind==="college" ? (a=>a.cg===value) : (a=>a.d===value);
  const ps=DATA.papers.filter(p=>orgOk(p) && p.a.some(a=>authOrgOk(a)&&sel(a)));
  if(!ps.length)return;
  const ifs=[],rcrs=[],auth=new Set(),yrs={},ac={}; let hi=0,cit=0,trials=0;
  ps.forEach(p=>{
    if(p.if!=null){ifs.push(p.if); if(p.if>7)hi++;}
    if(p.r!=null)rcrs.push(p.r);
    cit+=(p.c||0);
    if(p.st==="Randomized controlled trial"||p.st==="Clinical trial")trials++;
    if(p.y)yrs[p.y]=(yrs[p.y]||0)+1;
    p.a.forEach(a=>{if(authOrgOk(a)&&sel(a)){auth.add(a.n);ac[a.n]=(ac[a.n]||0)+1;}});
  });
  const depts=[...new Set(ps.flatMap(p=>p.a.filter(a=>authOrgOk(a)&&sel(a)).map(a=>a.d).filter(Boolean)))].sort();
  const yk=Object.keys(yrs).map(Number).sort(), ymax=Math.max(1,...Object.values(yrs));
  const chart=yk.map(y=>`<div class="col" title="${y}: ${yrs[y]}"><span class="cv">${yrs[y]}</span><div class="cb" style="height:${Math.round(70*yrs[y]/ymax)}px"></div><span class="cl">${y}</span></div>`).join("");
  const stat=(n,k)=>`<div class="ms"><div class="mn">${n}</div><div class="mk">${k}</div></div>`;
  const acS=Object.entries(ac).sort((x,y)=>y[1]-x[1]), acMax=acS.length?acS[0][1]:1;
  const topA=acS.slice(0,10).map(([n,c])=>`<div class="row"><span class="lab alink" data-n="${esc(n)}">${esc(n)}</span><span class="barwrap"><span class="barfill" style="width:${100*c/acMax}%"></span></span><span class="v">${c}</span></div>`).join("");
  const plist=ps.slice().sort((a,b)=>(b.c||0)-(a.c||0)).slice(0,40).map(p=>{
    const url=p.doi?("https://doi.org/"+p.doi):("https://pubmed.ncbi.nlm.nih.gov/"+p.p);
    const ifb=p.if!=null?` · IF≈${p.if.toFixed(1)}`:"", cb=p.c!=null?` · cited ${p.c}`:"";
    return `<div class="pitem"><div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a></div><div class="pm">${esc(p.j)} · ${p.y||"n/a"}${ifb}${cb}</div></div>`;
  }).join("");
  document.getElementById("authorBox").innerHTML=
    `<span class="x" onclick="closeAuthor()">&times;</span>
     <h3>${esc(value)}</h3>
     <div class="muted" style="font-size:12px">${kind==="college"?"College / service line":"Department"} scorecard · all tracked years${state.org!=="all"?" · "+esc(ORG[state.org]):""} · counts only papers with a tracked affiliation.</div>
     <div class="mstats">
       ${stat(ps.length.toLocaleString(),"papers")}${stat(auth.size,"people")}
       ${stat(ifs.length?mean(ifs).toFixed(1):"–","avg impact")}${stat(rcrs.length?mean(rcrs).toFixed(2):"–","mean RCR")}
       ${stat(hi,"high-impact")}${stat(cit.toLocaleString(),"citations")}${stat(trials,"trials")}
     </div>
     <div class="hint" style="font-size:11px">Papers per year</div>
     <div class="chart" style="height:110px">${chart}</div>
     <div class="hint" style="font-size:11px;margin-top:14px">Top contributors</div>
     <div>${topA||'<span class="muted">none</span>'}</div>
     <div class="hint" style="font-size:11px;margin-top:14px">What the group works on</div>
     <div class="cloud">${cloudHTML(ps)}</div>
     <div class="hint" style="font-size:11px;margin-top:14px">Departments included${value==="Other"?" — review these to refine the mapping in config.json":""}</div>
     <div class="muted" style="font-size:12px">${depts.length?depts.slice(0,60).map(esc).join(" · "):"—"}</div>
     <div class="hint" style="font-size:11px;margin-top:14px">Most-cited papers</div>
     <div class="mplist">${plist}</div>`;
  document.getElementById("authorModal").classList.add("show");
}

// ---- executive snapshot (institutional view: respects org + mega-collab toggle only) ----
function renderExec(){
  const sc=DATA.papers.filter(p=>orgOk(p)&&hyperOk(p));
  const cy=DATA.year, y1=cy-1, y0=cy-2;
  const cnt=y=>sc.filter(p=>p.y===y).length;
  const collab=y=>sc.filter(p=>p.y===y&&isCollab(p)).length;
  const totalCit=sc.reduce((s,p)=>s+(p.c||0),0);
  const auth=new Set(); sc.forEach(p=>p.a.forEach(a=>{if(authOrgOk(a))auth.add(a.n);}));
  const pubsY1=cnt(y1),pubsY0=cnt(y0), pct=pubsY0?Math.round((pubsY1-pubsY0)/pubsY0*100):0;
  const orgName=state.org==="all"?"Cook Children's & UT Arlington":ORG[state.org];
  document.getElementById("execScope").textContent="· "+orgName+(state.hyper?" · excl. mega-collaborations":"");
  document.getElementById("execLead").innerHTML=
    `In <b>${y1}</b>, ${esc(orgName)} published <b>${pubsY1.toLocaleString()}</b> papers `
    +`(<b class="${pct>=0?'up':'down'}">${pct>=0?'+':''}${pct}%</b> vs ${y0}); the tracked corpus has earned `
    +`<b>${totalCit.toLocaleString()}</b> citations to date, and Cook&harr;UT&nbsp;Arlington collaboration reached `
    +`<b>${collab(y1)}</b> joint papers in ${y1}. <span class="muted">Affiliation-scoped institutional snapshot — independent of the time/theme filters below.</span>`;
  const cards=[
    [sc.length.toLocaleString(),"papers tracked"],
    [pubsY1.toLocaleString()+` <span style="font-size:14px" class="${pct>=0?'up':'down'}">${pct>=0?'▲':'▼'} ${Math.abs(pct)}%</span>`,"publications "+y1],
    [totalCit.toLocaleString(),"total citations"],
    [collab(y1),"Cook↔UTA papers "+y1],
    [auth.size.toLocaleString(),"distinct authors"],
  ];
  document.getElementById("execCards").innerHTML=cards.map(c=>`<div class="bc"><div class="n">${c[0]}</div><div class="k">${c[1]}</div></div>`).join("");
}

// ---- active-filter chips + reset ----
function renderActiveFilters(){
  const chips=[];
  if(state.org!=="all")chips.push(["org",ORG[state.org]]);
  if(state.years.length)chips.push(["years","Years: "+state.years.slice().sort().join(", ")]);
  else if(state.rel!=="all")chips.push(["rel",{today:"Today",w:"Last 7 days",m:"Last 30 days"}[state.rel]||state.rel]);
  if(state.ifmin>0)chips.push(["if","Impact > "+state.ifmin]);
  state.themes.forEach(t=>chips.push(["theme:"+t,THEMES[t]||t]));
  if(state.stype!=="all")chips.push(["stype",state.stype==="trials"?"Clinical trials":state.stype]);
  if(!state.hyper)chips.push(["hyper","Incl. mega-collaborations"]);
  const box=document.getElementById("activeFilters");
  if(!chips.length){box.innerHTML='<span class="muted" style="font-size:12px">No filters active — showing the full tracked corpus.</span>';return;}
  box.innerHTML='<span class="muted" style="font-size:12px;margin-right:6px">Active filters:</span>'
    +chips.map(c=>`<span class="chip">${esc(c[1])} <span class="xx" data-clr="${c[0]}" title="remove">×</span></span>`).join("")
    +`<button class="btn" id="resetBtn" style="font-size:12px">Reset all</button>`;
}
function resetFilters(){state.org="all";state.rel="all";state.years=[];state.ifmin=0;state.themes=[];state.stype="all";state.hyper=true;sync();render();}

// ---- small-multiples: papers/year for each college / service line ----
function renderSmallMultiples(papers){
  const groups={};
  papers.forEach(p=>{
    const gs=new Set(p.a.filter(authOrgOk).map(a=>a.cg).filter(Boolean));
    gs.forEach(g=>{
      const o=groups[g]||(groups[g]={tot:0,yr:{}});
      o.tot++; if(p.y)o.yr[p.y]=(o.yr[p.y]||0)+1;
    });
  });
  const top=Object.entries(groups).sort((a,b)=>b[1].tot-a[1].tot).slice(0,12);
  const years=[]; for(let y=2018;y<=DATA.year;y++)years.push(y);
  document.getElementById("smGroups").innerHTML=top.length?top.map(([g,o])=>{
    const vals=years.map(y=>o.yr[y]||0), mx=Math.max(1,...vals);
    const bars=years.map((y,i)=>`<div class="col" title="${y}: ${vals[i]}"><div class="cb" style="height:${Math.round(48*vals[i]/mx)}px"></div><span class="cl">${String(y).slice(2)}</span></div>`).join("");
    return `<div class="smcell"><div class="smt alink" data-grp="college" data-val="${esc(g)}">${esc(g)}</div><div class="smn">${o.tot} papers</div><div class="chart">${bars}</div></div>`;
  }).join(""):"<div class='muted'>No data in this selection.</div>";
}

function render(){
  const papers=filtered();
  renderExec();
  renderActiveFilters();
  renderCards(papers);
  renderChart();
  renderLB("lbAny",leaderboard(papers,"any"));
  renderLB("lbFirst",leaderboard(papers,"first"));
  renderLB("lbLast",leaderboard(papers,"last"));
  renderSplit(papers);
  document.getElementById("cloudMain").innerHTML=cloudHTML(papers);
  renderMostCited(papers);
  renderList(papers);
  renderWhatsNew();
  renderCustom();
  renderTiers();
  renderAnalytics();
  renderSmallMultiples(papers);
  saveState();
  saveHash();
}

// ---- build controls
function mkBtns(containerId, items, key, cls){
  const c=document.getElementById(containerId);
  c.innerHTML=items.map(it=>`<button class="btn ${cls||''} ${it[0]==='all'&&key==='org'?'':''}" data-v="${it[0]}">${it[1]}</button>`).join("");
  c.querySelectorAll(".btn").forEach(b=>b.onclick=()=>{state[key]=b.dataset.v;sync();render();});
}
function sync(){
  document.querySelectorAll("#orgBtns .btn").forEach(b=>b.classList.toggle("active",b.dataset.v===state.org));
  document.querySelectorAll("#timeBtns .btn[data-rel]").forEach(b=>b.classList.toggle("active",state.years.length===0&&b.dataset.rel===state.rel));
  document.querySelectorAll("#timeBtns .btn[data-year]").forEach(b=>b.classList.toggle("active",state.years.includes(Number(b.dataset.year))));
  document.querySelectorAll("#splitBtns .btn").forEach(b=>b.classList.toggle("active",b.dataset.v===state.split));
  document.querySelectorAll("#ifBtns .btn").forEach(b=>b.classList.toggle("active",Number(b.dataset.v)===state.ifmin));
  // recolor org buttons
  const cookB=document.querySelector('#orgBtns .btn[data-v="cook"]');if(cookB)cookB.classList.add("cook");
  const utaB=document.querySelector('#orgBtns .btn[data-v="uta"]');if(utaB)utaB.classList.add("uta");
  // reflect state into inputs (matters after restoring from localStorage)
  document.querySelectorAll("#themeBtns .btn").forEach(b=>b.classList.toggle("active",state.themes.includes(b.dataset.v)));
  const ss=document.getElementById("stypeSel"); if(ss)ss.value=state.stype;
  const hy=document.getElementById("hyper"); if(hy)hy.checked=state.hyper;
  const cm=document.getElementById("cMetric"); if(cm)cm.value=state.cmetric;
  const cd=document.getElementById("cDim"); if(cd)cd.value=state.cdim;
}
function saveState(){try{localStorage.setItem("cookuta_state",JSON.stringify(state));}catch(e){}}
function restoreState(){try{const s=JSON.parse(localStorage.getItem("cookuta_state")||"{}");Object.keys(s).forEach(k=>{if(k in state)state[k]=s[k];});}catch(e){}}

const orgItems=[["all","All organizations"],...Object.entries(ORG)];
mkBtns("orgBtns",orgItems,"org");
mkBtns("splitBtns",SPLITS,"split");
// time window: relative buttons (single-select) + year buttons (multi-select)
document.getElementById("timeBtns").innerHTML=
  RELS.map(r=>`<button class="btn" data-rel="${r[0]}">${r[1]}</button>`).join("")
  +`<span style="width:10px"></span>`
  +YEARS.map(y=>`<button class="btn" data-year="${y}">${y}</button>`).join("");
document.querySelectorAll("#timeBtns .btn[data-rel]").forEach(b=>b.onclick=()=>{state.rel=b.dataset.rel;state.years=[];sync();render();});
document.querySelectorAll("#timeBtns .btn[data-year]").forEach(b=>b.onclick=()=>{
  const y=Number(b.dataset.year), i=state.years.indexOf(y);
  if(i<0)state.years.push(y); else state.years.splice(i,1);
  if(!state.years.length)state.rel="all";   // deselecting the last year falls back to all time
  sync();render();
});
// impact-factor radio buttons (store the numeric threshold)
document.getElementById("ifBtns").innerHTML=IF_LEVELS.map(l=>`<button class="btn" data-v="${l[0]}">${l[1]}</button>`).join("");
document.querySelectorAll("#ifBtns .btn").forEach(b=>b.onclick=()=>{state.ifmin=Number(b.dataset.v);sync();render();});
// research-theme chips (multi-select, OR)
document.getElementById("themeBtns").innerHTML=Object.entries(THEMES).map(([k,v])=>`<button class="btn" data-v="${k}">${v}</button>`).join("");
document.querySelectorAll("#themeBtns .btn").forEach(b=>b.onclick=()=>{const v=b.dataset.v;const i=state.themes.indexOf(v);if(i<0)state.themes.push(v);else state.themes.splice(i,1);sync();render();});
document.getElementById("themeHint").textContent="(any selected)";
// study-type selector
const STYPES=[["all","All study types"],["trials","Clinical trials (incl. RCT)"],["Randomized controlled trial","Randomized controlled trial"],["Clinical trial","Clinical trial"],["Meta-analysis","Meta-analysis"],["Systematic review","Systematic review"],["Review","Review"],["Observational study","Observational study"],["Case report","Case report"]];
document.getElementById("stypeSel").innerHTML=STYPES.map(s=>`<option value="${s[0]}">${s[1]}</option>`).join("");
document.getElementById("stypeSel").onchange=e=>{state.stype=e.target.value;render();saveState();};
document.getElementById("search").oninput=e=>{state.search=e.target.value;renderList(filtered());};
document.getElementById("hyper").onchange=e=>{state.hyper=e.target.checked;render();};
// most-cited mode toggle (all-time vs hot now)
document.querySelectorAll("#citAll,#citRecent").forEach(b=>b.onclick=()=>{state.citmode=b.dataset.cm;renderMostCited(filtered());saveState();});
// light / dark theme toggle
function applyTheme(){const light=state.theme==="light";document.documentElement.classList.toggle("light",light);document.getElementById("themeToggle").textContent=light?"🌙 Dark mode":"☀ Light mode";}
document.getElementById("themeToggle").onclick=()=>{state.theme=state.theme==="light"?"dark":"light";applyTheme();saveState();};
// print / save-as-PDF
document.getElementById("pdfBtn").onclick=()=>window.print();
// active-filter chips: remove one, or reset all
document.getElementById("activeFilters").addEventListener("click",e=>{
  if(e.target.id==="resetBtn"){resetFilters();return;}
  const x=e.target.closest(".xx"); if(!x)return;
  const c=x.dataset.clr;
  if(c==="org")state.org="all"; else if(c==="years")state.years=[]; else if(c==="rel")state.rel="all";
  else if(c==="if")state.ifmin=0; else if(c==="stype")state.stype="all"; else if(c==="hyper")state.hyper=true;
  else if(c.indexOf("theme:")===0)state.themes=state.themes.filter(t=>t!==c.slice(6));
  sync();render();
});

// build-your-own chart selectors
document.getElementById("cMetric").innerHTML=Object.entries(METRICS).map(([k,v])=>`<option value="${k}">${v[0]}</option>`).join("");
document.getElementById("cDim").innerHTML=Object.entries(DIMS).map(([k,v])=>`<option value="${k}">${v[0]}</option>`).join("");
document.getElementById("cMetric").onchange=e=>{state.cmetric=e.target.value;renderCustom();saveState();};
document.getElementById("cDim").onchange=e=>{state.cdim=e.target.value;renderCustom();saveState();};

// author drill-down: clicking any author name opens their profile
document.addEventListener("click",e=>{const t=e.target.closest(".alink");if(!t)return;if(t.dataset.grp)openGroup(t.dataset.grp,t.dataset.val);else if(t.dataset.n)openAuthor(t.dataset.n);});
document.getElementById("authorModal").addEventListener("click",e=>{if(e.target.id==="authorModal")closeAuthor();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeAuthor();});

// find-an-author search -> opens that person's profile
const ALL_AUTHORS=[...new Set(DATA.papers.flatMap(p=>p.a.map(a=>a.n)))].sort();
document.getElementById("authorList").innerHTML=ALL_AUTHORS.map(n=>`<option value="${esc(n)}"></option>`).join("");
function tryOpenAuthor(v){const m=ALL_AUTHORS.find(n=>n.toLowerCase()===v.toLowerCase());if(m)openAuthor(m);}
document.getElementById("authorSearch").addEventListener("change",e=>tryOpenAuthor(e.target.value));
document.getElementById("authorSearch").addEventListener("keydown",e=>{if(e.key==="Enter")tryOpenAuthor(e.target.value);});

// copy a shareable link to the current view
document.getElementById("copyLink").onclick=()=>{
  const b=document.getElementById("copyLink");
  (navigator.clipboard?navigator.clipboard.writeText(location.href):Promise.reject())
    .then(()=>{b.textContent="✓ Link copied";setTimeout(()=>b.textContent="🔗 Copy link to this view",1800);})
    .catch(()=>{b.textContent="Copy this URL ↑";});
};

// download the current filtered selection as CSV
function csvCell(s){s=(s==null?"":String(s));return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
document.getElementById("csvBtn").onclick=()=>{
  const rows=filtered();
  const head=["pmid","title","journal","year","added","organizations","study_type","citations","RCR","journal_impact","themes","first_authors","last_authors","org_authors","doi"];
  const meta="# Cook Children's & UT Arlington research output — exported "+DATA.today+"; data as of "+DATA.generated_at+"; affiliation-scoped (PubMed only); "+rows.length+" rows";
  const lines=[meta, head.join(",")];
  rows.forEach(p=>{
    const fa=p.a.filter(a=>a.f).map(a=>a.n).join("; "), la=p.a.filter(a=>a.l).map(a=>a.n).join("; ");
    const oa=p.a.map(a=>a.n).join("; ");
    lines.push([p.p,p.t,p.j,p.y||"",p.e||"",p.o.map(o=>ORG[o]).join("; "),p.st||"",p.c==null?"":p.c,p.r==null?"":p.r,p.if==null?"":p.if,(p.th||[]).map(t=>THEMES[t]||t).join("; "),fa,la,oa,p.doi||""].map(csvCell).join(","));
  });
  const blob=new Blob([lines.join("\n")],{type:"text/csv"});
  const a=document.createElement("a");a.href=URL.createObjectURL(blob);
  a.download="research-output-"+(new Date().toISOString().slice(0,10))+".csv";a.click();
};

// deep-link: encode the active view in the URL so it can be shared
function saveHash(){try{const s={o:state.org,r:state.rel,y:state.years,im:state.ifmin,t:state.themes,st:state.stype,sp:state.split,h:state.hyper};history.replaceState(null,"","#"+encodeURIComponent(JSON.stringify(s)));}catch(e){}}
function loadHash(){try{if(location.hash.length>1){const s=JSON.parse(decodeURIComponent(location.hash.slice(1)));
  if(s.o!=null)state.org=s.o; if(s.r!==undefined)state.rel=s.r; if(s.y)state.years=s.y; if(s.im!=null)state.ifmin=s.im;
  if(s.t)state.themes=s.t; if(s.st)state.stype=s.st; if(s.sp)state.split=s.sp; if(s.h!=null)state.hyper=s.h; return true;}}catch(e){} return false;}

// auto-refresh: reload every 30 min to show the latest daily build (filters restored from URL/localStorage)
setTimeout(()=>location.reload(), 30*60*1000);

document.getElementById("meta").innerHTML=
  `<span class="live"><span class="dot"></span>live · auto-refreshes</span> &nbsp; Last updated <b>${DATA.generated_at}</b> · ${DATA.papers.length.toLocaleString()} papers tracked`;

restoreState();
loadHash();   // a shared URL overrides saved local state
applyTheme();
sync();render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
