#!/usr/bin/env python3
"""Build a self-contained dashboard.html from data/papers.db.

The dashboard is one file with the data embedded as JSON and all filtering done
in the browser -- no server, no internet needed. Just double-click it.
"""
import os, re, json, sqlite3, datetime, html

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
    for row in con.execute(
        "SELECT pmid,title,journal,pub_year,entry_date,doi,topic,orgs,mesh,authors,issn,is_genetics,first_seen FROM papers"
    ):
        pmid, title, journal, year, entry, doi, topic, orgs, mesh, authors, issn, is_gen, first_seen = row
        orgs = json.loads(orgs)
        if not orgs:
            continue
        authors = json.loads(authors)
        # keep only authors affiliated with a tracked org -> these power the leaderboards
        org_authors = [
            {"n": a["name"], "f": a["is_first"], "l": a["is_last"],
             "o": a["orgs"], "d": clean_dept(a.get("dept"))}
            for a in authors if a["orgs"]
        ]
        mesh = [m for m in json.loads(mesh) if m not in GENERIC_MESH]
        tags = mesh[:6] if mesh else ([topic] if topic else [])
        jif = jifmap.get(issn)
        papers.append({
            "p": pmid, "t": title, "j": journal, "y": year, "e": entry,
            "doi": doi, "o": orgs, "a": org_authors, "tg": tags,
            "n": len(authors),               # total author count (hyperauthorship filter)
            "if": round(jif, 2) if jif is not None else None,  # open IF-equivalent (filter on full value)
            "g": 1 if is_gen else 0,          # genetics paper?
            "fs": first_seen,                 # date our tracker first saw it
        })
    con.close()
    papers.sort(key=lambda x: (x["e"] or ""), reverse=True)
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_run": last_run, "today": today, "year": datetime.date.today().year,
        "orgs": ORG_NAMES, "papers": papers,
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
    --txt:#e7edf5; --muted:#8da2bd; --accent:#4ea1ff; --cook:#ff7a59; --uta:#5ad1a8;
    --bar:#33425a;
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
  .row .barfill{height:100%;background:var(--accent);border-radius:5px}
  .row .v{width:34px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
  .tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;margin-left:5px;vertical-align:middle}
  .tag.cook{background:rgba(255,122,89,.18);color:var(--cook)}
  .tag.uta{background:rgba(90,209,168,.18);color:var(--uta)}
  table{width:100%;border-collapse:collapse}
  .plist{max-height:560px;overflow:auto}
  .pitem{padding:10px 0;border-bottom:1px solid var(--line)}
  .pitem .pt{font-weight:550;font-size:13.5px}
  .pitem .pm{color:var(--muted);font-size:12px;margin-top:2px}
  .pitem .pa{font-size:12px;margin-top:3px;color:#c4d2e6}
  .pitem b{color:var(--txt)}
  select,input{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
               border-radius:8px;padding:5px 9px;font-size:12.5px}
  .flex{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--muted)}
  .chart{display:flex;align-items:flex-end;gap:6px;height:130px;margin-top:8px}
  .chart .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:4px}
  .chart .cb{width:100%;background:var(--accent);border-radius:4px 4px 0 0;min-height:2px}
  .chart .cl{font-size:10px;color:var(--muted)}
  .chart .cv{font-size:10px;color:var(--muted)}
  .sechead{margin:26px 0 4px;font-size:16px;font-weight:650}
  .secsub{color:var(--muted);font-size:12.5px;margin-bottom:14px}
  .delta{font-size:12px;margin-left:7px;font-weight:600}
  .up{color:var(--uta)} .down{color:var(--cook)} .flat{color:var(--muted)}
  .atab{width:100%;border-collapse:collapse;font-size:12.5px}
  .atab th{text-align:left;color:var(--muted);font-weight:500;padding:5px 6px;border-bottom:1px solid var(--line);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}
  .atab td{padding:5px 6px;border-bottom:1px solid var(--line)}
  .atab td.num{text-align:right;font-variant-numeric:tabular-nums}
  .atab tr:hover td{background:var(--panel2)}
  .badge{font-size:9px;background:var(--uta);color:#06121f;border-radius:4px;padding:1px 5px;margin-left:6px;font-weight:600;vertical-align:middle}
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
  .stklabels .sl{flex:1;text-align:center;font-size:10px;color:var(--muted)}
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
  .mstats .ms .mk{font-size:10.5px;color:var(--muted)}
  .mplist{max-height:300px;overflow:auto;margin-top:8px}
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
<div class="wrap">
  <div class="controls">
    <div class="cgroup">
      <span class="clabel">Organization</span>
      <div class="btns" id="orgBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Time window</span>
      <div class="btns" id="timeBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Journal impact <span title="Open IF-equivalent: OpenAlex 2-year mean citedness. Same formula as Impact Factor on open citation data. Papers in journals with no available metric are hidden when a threshold is on." style="cursor:help;color:var(--accent)">&#9432;</span></span>
      <div class="btns" id="ifBtns"></div>
    </div>
    <div class="cgroup">
      <span class="clabel">Genetics</span>
      <div class="btns">
        <label class="btn" id="genBtn" style="cursor:pointer">
          <input type="checkbox" id="genChk" style="vertical-align:middle;margin-right:5px">Genetics papers only</label>
      </div>
    </div>
    <div class="cgroup">
      <span class="clabel">Split breakdown by</span>
      <div class="btns" id="splitBtns"></div>
    </div>
  </div>

  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>What's new <span class="hint">papers added to PubMed in the last 14 days · current organization filter</span>
      <span id="newCount" class="badge" style="background:var(--accent)"></span></h2>
    <div class="plist" id="whatsnew" style="max-height:230px"></div>
  </div>

  <div class="panel">
    <h2>Papers per year <span class="hint">by publication year · current organization filter</span></h2>
    <div class="chart" id="chart"></div>
  </div>

  <div class="grid">
    <div class="panel"><h2>Most published <span class="hint">any authorship</span></h2>
      <label class="hint" style="display:block;margin:-6px 0 8px;cursor:pointer">
        <input type="checkbox" id="hyper" checked style="vertical-align:middle">
        exclude mega-collaboration papers (&gt;50 authors)</label>
      <div id="lbAny"></div></div>
    <div class="panel"><h2>Most first-author <span class="hint">lead author</span></h2><div id="lbFirst"></div></div>
    <div class="panel"><h2>Most last-author <span class="hint">senior author</span></h2><div id="lbLast"></div></div>
  </div>

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
  <div class="sechead">Explore &amp; customize</div>
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

  <div class="sechead">Leadership analytics &amp; trends</div>
  <div class="secsub" id="secsub">Respects the Organization, Impact and Genetics filters above; spans all years (ignores the time window). Year-over-year cards compare the last complete year.</div>

  <div class="cards" id="kpis"></div>

  <div class="grid">
    <div class="panel"><h2>Average journal impact <span class="hint">by year · IF-equivalent</span></h2><div class="chart" id="chartIF"></div></div>
    <div class="panel"><h2>High-impact share <span class="hint">% of papers with IF&gt;7, by year</span></h2><div class="chart" id="chartHi"></div></div>
    <div class="panel"><h2>Cook &harr; UT Arlington collaboration <span class="hint">joint papers by year</span></h2><div class="chart" id="chartCollab"></div></div>
  </div>

  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="panel"><h2>Top departments <span class="hint">output &amp; quality · current filter</span></h2>
      <table class="atab"><thead><tr><th>Department</th><th class="num">Papers</th><th class="num">Mean IF</th><th class="num">High-impact</th></tr></thead><tbody id="deptTable"></tbody></table></div>
    <div class="panel"><h2>Rising authors <span class="hint">most active in the last 24 months</span></h2>
      <table class="atab"><thead><tr><th>Author</th><th class="num">Last 24 mo</th><th class="num">Total</th></tr></thead><tbody id="rising"></tbody></table></div>
  </div>

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
const state = {org:"all", time:"all", split:"dept", search:"", hyper:true, ifmin:0, gen:false, cmetric:"papers", cdim:"year"};
const HYPER_MAX = 50;  // papers with more authors than this are "mega-collaborations"
const IF_LEVELS = [[0,"Any"],[3,"&gt; 3"],[7,"&gt; 7"],[20,"&gt; 20"]];

// ---- time predicates
function daysAgo(n){const d=new Date(DATA.today+"T00:00:00");d.setDate(d.getDate()-n);return d.toISOString().slice(0,10);}
const TIMES = [
  ["today","Today",     p=>p.e===DATA.today],
  ["w","Last 7 days",   p=>p.e&&p.e>=daysAgo(7)],
  ["m","Last 30 days",  p=>p.e&&p.e>=daysAgo(30)],
  ["ty","This year ("+DATA.year+")", p=>p.y===DATA.year],
  ["2025","2025",p=>p.y===2025],["2024","2024",p=>p.y===2024],
  ["2023","2023",p=>p.y===2023],["2022","2022",p=>p.y===2022],
  ["2021","2021",p=>p.y===2021],["2020","2020",p=>p.y===2020],
  ["all","All time",p=>true],
];
const SPLITS=[["dept","Department"],["topic","Topic"],["journal","Journal"]];

function orgOk(p){return state.org==="all"||p.o.includes(state.org);}
function timeOk(p){return TIMES.find(t=>t[0]===state.time)[2](p);}
function authOrgOk(a){return state.org==="all"||a.o.includes(state.org);}

function ifOk(p){return state.ifmin===0 || (p.if!=null && p.if>state.ifmin);}
function genOk(p){return !state.gen || p.g===1;}
function filtered(){return DATA.papers.filter(p=>orgOk(p)&&timeOk(p)&&ifOk(p)&&genOk(p));}

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

// ---- leaderboards
function leaderboard(papers, kind){
  const m={};
  papers.forEach(p=>{
    if(kind==="any" && state.hyper && p.n>HYPER_MAX) return;  // skip mega-collaborations
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

// ---- generic split
function renderSplit(papers){
  const m={};
  if(state.split==="journal"){
    papers.forEach(p=>{const k=p.j||"(unknown)";m[k]=(m[k]||0)+1;});
    document.getElementById("splitTitle").innerHTML='Breakdown by journal <span class="hint">current filter</span>';
  } else if(state.split==="topic"){
    papers.forEach(p=>p.tg.forEach(t=>{m[t]=(m[t]||0)+1;}));
    document.getElementById("splitTitle").innerHTML='Breakdown by topic <span class="hint">MeSH / keywords · a paper has several</span>';
  } else {
    papers.forEach(p=>p.a.forEach(a=>{if(authOrgOk(a)&&a.d){m[a.d]=(m[a.d]||0)+1;}}));
    document.getElementById("splitTitle").innerHTML='Breakdown by department <span class="hint">auto-derived · current filter</span>';
  }
  const rows=Object.entries(m).sort((a,b)=>b[1]-a[1]).slice(0,18);
  const max=rows.length?rows[0][1]:1;
  document.getElementById("splitBox").innerHTML = rows.length? rows.map(([k,v])=>
    `<div class="row"><span class="lab">${esc(k)}</span>
     <span class="barwrap"><span class="barfill" style="width:${100*v/max}%"></span></span>
     <span class="v">${v}</span></div>`).join("") : `<div class="muted">No data in this window.</div>`;
}

// ---- year chart (respects org filter only, so the trend is always visible)
function renderChart(){
  const yrs={};
  DATA.papers.filter(p=>orgOk(p)&&ifOk(p)&&genOk(p)).forEach(p=>{if(p.y&&p.y>=2016)yrs[p.y]=(yrs[p.y]||0)+1;});
  const keys=Object.keys(yrs).map(Number).sort();
  const max=Math.max(1,...Object.values(yrs));
  document.getElementById("chart").innerHTML=keys.map(y=>{
    const v=yrs[y];const sel=(String(y)===state.time||(state.time==="ty"&&y===DATA.year));
    return `<div class="col" title="${y}: ${v}">
      <span class="cv">${v}</span>
      <div class="cb" style="height:${100*v/max}%;${sel?'background:var(--cook)':''}"></div>
      <span class="cl">${y}</span></div>`;
  }).join("");
}

// ---- cards
function renderCards(papers){
  const newToday=DATA.papers.filter(p=>orgOk(p)&&p.e===DATA.today).length;
  const new7=DATA.papers.filter(p=>orgOk(p)&&p.e&&p.e>=daysAgo(7)).length;
  const thisYr=DATA.papers.filter(p=>orgOk(p)&&p.y===DATA.year).length;
  const auth=new Set();papers.forEach(p=>p.a.forEach(a=>{if(authOrgOk(a))auth.add(a.n);}));
  const cards=[
    [papers.length,"papers in selection"],
    [newToday,"new today"],
    [new7,"new last 7 days"],
    [thisYr,"published this year"],
    [auth.size,"distinct authors"],
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
    const gb=p.g?` · <span style="color:var(--uta)">genetics</span>`:"";
    return `<div class="pitem">
      <div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a> ${tags}</div>
      <div class="pm">${esc(p.j)} · ${p.y||"n/a"} · added ${p.e||"n/a"}${ifb}${gb} · PMID ${p.p}</div>
      <div class="pa">${names}</div></div>`;
  }).join("")||"<div class='muted'>No papers match.</div>";
}

// ---- leadership analytics (respect org/impact/genetics, span all years) ----
function analyticsBase(){return DATA.papers.filter(p=>orgOk(p)&&ifOk(p)&&genOk(p));}
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
    +`<div class="cb" style="height:${100*vals[i]/max}%"></div><span class="cl">${k}</span></div>`).join("");
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
    ds.forEach(d=>{const o=dep[d]||(dep[d]={n:0,ifs:[],hi:0}); o.n++; if(pp.if!=null){o.ifs.push(pp.if); if(pp.if>7)o.hi++;}});
  });
  const drows=Object.entries(dep).sort((x,y)=>y[1].n-x[1].n).slice(0,12);
  document.getElementById("deptTable").innerHTML=drows.length?drows.map(([d,o])=>
    `<tr><td>${esc(d)}</td><td class="num">${o.n}</td><td class="num">${o.ifs.length?mean(o.ifs).toFixed(1):"–"}</td><td class="num">${o.hi}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">No data.</td></tr>`;

  // rising authors: most papers in last 24 months; "new" if first appeared in that window
  const cutoff=daysAgo(730), au={};
  base.forEach(pp=>{
    if(state.hyper && pp.n>HYPER_MAX) return;   // exclude mega-collaboration papers from per-person tallies
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
    `Respects the Organization, Impact and Genetics filters above; spans all years (ignores the time window). `
    +`<b>Research concentration:</b> the top 10 authors account for <b>${conc}%</b> of output in the current selection `
    +`(${Object.keys(au).length.toLocaleString()} distinct authors).`;
}

// ---- build-your-own chart ----
const METRICS={
  papers:["number of papers", g=>g.papers, v=>v.toLocaleString()],
  avgif:["average journal impact", g=>g.ifs.length?mean(g.ifs):0, v=>v.toFixed(1)],
  authors:["distinct authors", g=>g.auth.size, v=>v.toLocaleString()],
  collab:["Cook↔UTA joint papers", g=>g.collab, v=>v],
  genshare:["genetics share", g=>g.papers?100*g.gen/g.papers:0, v=>Math.round(v)+"%"],
  hishare:["high-impact share (IF>7)", g=>g.papers?100*g.hi/g.papers:0, v=>Math.round(v)+"%"],
};
const DIMS={
  year:["year", p=>p.y?[String(p.y)]:[]],
  dept:["department", p=>[...new Set(p.a.filter(authOrgOk).map(a=>a.d).filter(Boolean))]],
  topic:["topic", p=>p.tg||[]],
  journal:["journal", p=>p.j?[p.j]:[]],
  org:["organization", p=>p.o.map(o=>ORG[o])],
};
function groupBy(papers, dimFn){
  const G={};
  papers.forEach(p=>{
    dimFn(p).forEach(k=>{
      const g=G[k]||(G[k]={papers:0,ifs:[],auth:new Set(),collab:0,gen:0,hi:0});
      g.papers++; if(p.if!=null){g.ifs.push(p.if); if(p.if>7)g.hi++;}
      if(isCollab(p))g.collab++; if(p.g)g.gen++;
      p.a.forEach(a=>{if(authOrgOk(a))g.auth.add(a.n);});
    });
  });
  return G;
}
function renderCustom(){
  const papers=filtered();
  const G=groupBy(papers, DIMS[state.cdim][1]);
  const fn=METRICS[state.cmetric][1], disp=METRICS[state.cmetric][2];
  const ratio=["avgif","genshare","hishare"].includes(state.cmetric);
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
    const ifb=p.if!=null?` · IF≈${p.if.toFixed(1)}`:"";
    return `<div class="pitem"><div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a> ${tags}</div>
      <div class="pm">${esc(p.j)}${ifb} · added ${p.e}${p.g?' · <span style="color:var(--uta)">genetics</span>':''}</div></div>`;
  }).join(""):"<div class='muted'>No papers added in the last 14 days for this selection.</div>";
}

// ---- per-author drill-down ----
function openAuthor(name){
  const ps=DATA.papers.filter(p=>p.a.some(a=>a.n===name));
  if(!ps.length)return;
  let first=0,last=0,hi=0; const ifs=[],orgs=new Set(),yrs={};
  ps.forEach(p=>{
    const me=p.a.find(a=>a.n===name);
    if(me){ if(me.f)first++; if(me.l)last++; me.o.forEach(o=>orgs.add(o)); }
    if(p.if!=null){ifs.push(p.if); if(p.if>7)hi++;}
    if(p.y)yrs[p.y]=(yrs[p.y]||0)+1;
  });
  const tags=[...orgs].map(o=>`<span class="tag ${o}">${esc(ORG[o])}</span>`).join("");
  const yk=Object.keys(yrs).map(Number).sort(); const ymax=Math.max(1,...Object.values(yrs));
  const chart=yk.map(y=>`<div class="col" title="${y}: ${yrs[y]}"><span class="cv">${yrs[y]}</span><div class="cb" style="height:${100*yrs[y]/ymax}%"></div><span class="cl">${y}</span></div>`).join("");
  const stat=(n,k)=>`<div class="ms"><div class="mn">${n}</div><div class="mk">${k}</div></div>`;
  const plist=ps.slice().sort((a,b)=>(b.e||"").localeCompare(a.e||"")).slice(0,60).map(p=>{
    const me=p.a.find(a=>a.n===name); const role=me&&me.f?" (first)":me&&me.l?" (last)":"";
    const url=p.doi?("https://doi.org/"+p.doi):("https://pubmed.ncbi.nlm.nih.gov/"+p.p);
    const ifb=p.if!=null?` · IF≈${p.if.toFixed(1)}`:"";
    return `<div class="pitem"><div class="pt"><a href="${url}" target="_blank">${esc(p.t)}</a>${role}</div>
      <div class="pm">${esc(p.j)} · ${p.y||"n/a"}${ifb}${p.g?' · genetics':''}</div></div>`;
  }).join("");
  document.getElementById("authorBox").innerHTML=
    `<span class="x" onclick="closeAuthor()">&times;</span>
     <h3>${esc(name)} ${tags}</h3>
     <div class="muted" style="font-size:12px">Full publication record across all tracked years.</div>
     <div class="mstats">
       ${stat(ps.length,"papers")}${stat(first,"first-author")}${stat(last,"last-author")}
       ${stat(ifs.length?mean(ifs).toFixed(1):"–","avg impact")}${stat(hi,"high-impact")}
     </div>
     <div class="hint" style="font-size:11px">Papers per year</div>
     <div class="chart" style="height:110px">${chart}</div>
     <div class="hint" style="font-size:11px;margin-top:12px">Papers (newest first)</div>
     <div class="mplist">${plist}</div>`;
  document.getElementById("authorModal").classList.add("show");
}
function closeAuthor(){document.getElementById("authorModal").classList.remove("show");}

function render(){
  const papers=filtered();
  renderCards(papers);
  renderChart();
  renderLB("lbAny",leaderboard(papers,"any"));
  renderLB("lbFirst",leaderboard(papers,"first"));
  renderLB("lbLast",leaderboard(papers,"last"));
  renderSplit(papers);
  renderList(papers);
  renderWhatsNew();
  renderCustom();
  renderTiers();
  renderAnalytics();
  saveState();
}

// ---- build controls
function mkBtns(containerId, items, key, cls){
  const c=document.getElementById(containerId);
  c.innerHTML=items.map(it=>`<button class="btn ${cls||''} ${it[0]==='all'&&key==='org'?'':''}" data-v="${it[0]}">${it[1]}</button>`).join("");
  c.querySelectorAll(".btn").forEach(b=>b.onclick=()=>{state[key]=b.dataset.v;sync();render();});
}
function sync(){
  document.querySelectorAll("#orgBtns .btn").forEach(b=>b.classList.toggle("active",b.dataset.v===state.org));
  document.querySelectorAll("#timeBtns .btn").forEach(b=>b.classList.toggle("active",b.dataset.v===state.time));
  document.querySelectorAll("#splitBtns .btn").forEach(b=>b.classList.toggle("active",b.dataset.v===state.split));
  document.querySelectorAll("#ifBtns .btn").forEach(b=>b.classList.toggle("active",Number(b.dataset.v)===state.ifmin));
  document.getElementById("genBtn").classList.toggle("active",state.gen);
  // recolor org buttons
  const cookB=document.querySelector('#orgBtns .btn[data-v="cook"]');if(cookB)cookB.classList.add("cook");
  const utaB=document.querySelector('#orgBtns .btn[data-v="uta"]');if(utaB)utaB.classList.add("uta");
  // reflect state into inputs (matters after restoring from localStorage)
  const gc=document.getElementById("genChk"); if(gc)gc.checked=state.gen;
  const hy=document.getElementById("hyper"); if(hy)hy.checked=state.hyper;
  const cm=document.getElementById("cMetric"); if(cm)cm.value=state.cmetric;
  const cd=document.getElementById("cDim"); if(cd)cd.value=state.cdim;
}
function saveState(){try{localStorage.setItem("cookuta_state",JSON.stringify(state));}catch(e){}}
function restoreState(){try{const s=JSON.parse(localStorage.getItem("cookuta_state")||"{}");Object.keys(s).forEach(k=>{if(k in state)state[k]=s[k];});}catch(e){}}

const orgItems=[["all","All organizations"],...Object.entries(ORG)];
mkBtns("orgBtns",orgItems,"org");
mkBtns("timeBtns",TIMES.map(t=>[t[0],t[1]]),"time");
mkBtns("splitBtns",SPLITS,"split");
// impact-factor radio buttons (store the numeric threshold)
document.getElementById("ifBtns").innerHTML=IF_LEVELS.map(l=>`<button class="btn" data-v="${l[0]}">${l[1]}</button>`).join("");
document.querySelectorAll("#ifBtns .btn").forEach(b=>b.onclick=()=>{state.ifmin=Number(b.dataset.v);sync();render();});
document.getElementById("genChk").onchange=e=>{state.gen=e.target.checked;sync();render();};
document.getElementById("search").oninput=e=>{state.search=e.target.value;renderList(filtered());};
document.getElementById("hyper").onchange=e=>{state.hyper=e.target.checked;render();};

// build-your-own chart selectors
document.getElementById("cMetric").innerHTML=Object.entries(METRICS).map(([k,v])=>`<option value="${k}">${v[0]}</option>`).join("");
document.getElementById("cDim").innerHTML=Object.entries(DIMS).map(([k,v])=>`<option value="${k}">${v[0]}</option>`).join("");
document.getElementById("cMetric").onchange=e=>{state.cmetric=e.target.value;renderCustom();saveState();};
document.getElementById("cDim").onchange=e=>{state.cdim=e.target.value;renderCustom();saveState();};

// author drill-down: clicking any author name opens their profile
document.addEventListener("click",e=>{const t=e.target.closest(".alink");if(t&&t.dataset.n)openAuthor(t.dataset.n);});
document.getElementById("authorModal").addEventListener("click",e=>{if(e.target.id==="authorModal")closeAuthor();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeAuthor();});

// auto-refresh: reload every 30 min to show the latest daily build (filters are restored from localStorage)
setTimeout(()=>location.reload(), 30*60*1000);

document.getElementById("meta").innerHTML=
  `<span class="live"><span class="dot"></span>live · auto-refreshes</span> &nbsp; Last updated <b>${DATA.generated_at}</b> · ${DATA.papers.length.toLocaleString()} papers tracked`;

restoreState();
sync();render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
