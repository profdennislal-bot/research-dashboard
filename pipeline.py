#!/usr/bin/env python3
"""
Research-output pipeline for Cook Children's + UT Arlington.

Strategy (the efficient "fetch-broadly, verify-locally" approach):
  1. Ask PubMed for ALL candidate papers via affiliation search (cheap, one query per org).
  2. Download full metadata in batches.
  3. Locally parse each author's own affiliation, verify the org match, and attribute
     first/last/any authorship to the right organization.
  4. Derive department (from affiliation text) and topic (MeSH -> keywords -> journal).
  5. Store everything in a local SQLite database (data/papers.db).

Usage:
  python3 pipeline.py backfill     # one-time: pull the full history
  python3 pipeline.py update       # daily: pull anything added in the last 45 days, upsert

Stdlib only -- no pip installs, so it runs reliably on a schedule.
"""

import sys, os, re, json, time, sqlite3, datetime, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "papers.db")
CONFIG = json.load(open(os.path.join(HERE, "config.json")))
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EMAIL = CONFIG.get("contact_email", "")
TOOL = CONFIG.get("tool_name", "research-dashboard")

GEN_KW = CONFIG.get("genetics_keywords", ["gene", "genetic", "genetics", "genomics", "mutation"])
GENETICS_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in GEN_KW) + r")\b", re.IGNORECASE)

# research themes: each id -> list of lowercase substrings to look for in title+abstract+mesh+keywords
THEMES = [(t["id"], [k.lower() for k in t["kw"]]) for t in CONFIG.get("themes", [])]

# map a PubMed PublicationType list to one evidence-level bucket
def study_type(pub_types):
    p = " | ".join(pub_types).lower()
    if "meta-analysis" in p:
        return "Meta-analysis"
    if "systematic review" in p:
        return "Systematic review"
    if "randomized controlled trial" in p:
        return "Randomized controlled trial"
    if "clinical trial" in p or "controlled clinical trial" in p:
        return "Clinical trial"
    if "observational study" in p or "comparative study" in p:
        return "Observational study"
    if "case reports" in p:
        return "Case report"
    if "review" in p:
        return "Review"
    return "Other"

TRIAL_TYPES = {"Randomized controlled trial", "Clinical trial"}

DEPT_RE = re.compile(
    r"\b(?:Department|Dept\.?|Division|Center|Centre|Institute|School|"
    r"College|Section|Laboratory|Lab|Program|Programme|Unit) of ([^,;\.\(]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- HTTP helpers
def _get(url, retries=4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": TOOL})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa
            last = e
            time.sleep(2 + i * 2)
    raise last


def _post(url, data, retries=4):
    last = None
    body = urllib.parse.urlencode(data).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": TOOL})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa
            last = e
            time.sleep(2 + i * 2)
    raise last


def esearch_ids(term, reldate=None):
    """Return all PMIDs for a term. If reldate given, restrict to last N days (by entry date)."""
    ids, retstart, retmax = [], 0, 5000
    while True:
        params = {
            "db": "pubmed", "term": term, "retmode": "json",
            "retmax": retmax, "retstart": retstart, "tool": TOOL, "email": EMAIL,
        }
        if reldate:
            params["reldate"] = reldate
            params["datetype"] = "edat"
        url = EUTILS + "/esearch.fcgi?" + urllib.parse.urlencode(params)
        res = json.loads(_get(url))["esearchresult"]
        batch = res.get("idlist", [])
        ids.extend(batch)
        total = int(res.get("count", 0))
        retstart += retmax
        time.sleep(0.4)
        if retstart >= total or not batch:
            break
    return ids


def efetch_xml(pmids):
    """Yield parsed <PubmedArticle> elements for a list of PMIDs, batched."""
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i + 200]
        data = {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml",
                "tool": TOOL, "email": EMAIL}
        raw = _post(EUTILS + "/efetch.fcgi", data)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for art in root.findall(".//PubmedArticle"):
            yield art
        time.sleep(0.4)


# ---------------------------------------------------------------- parsing
def _text(el):
    return "".join(el.itertext()).strip() if el is not None else None


def _parse_pubyear(art):
    # Prefer explicit publication year; fall back to ArticleDate / PubMedPubDate.
    for path in [".//Journal//PubDate/Year", ".//ArticleDate/Year",
                 ".//PubmedData/History/PubMedPubDate[@PubStatus='pubmed']/Year"]:
        y = art.findtext(path)
        if y and y.isdigit():
            return int(y)
    medline = art.findtext(".//Journal//PubDate/MedlineDate")  # e.g. "2023 Spring"
    if medline:
        m = re.search(r"\b(19|20)\d{2}\b", medline)
        if m:
            return int(m.group(0))
    return None


def _parse_entrydate(art):
    """Date the paper entered PubMed (what 'new today/this week' is based on)."""
    h = art.find(".//PubmedData/History/PubMedPubDate[@PubStatus='entrez']")
    if h is None:
        h = art.find(".//PubmedData/History/PubMedPubDate[@PubStatus='pubmed']")
    if h is not None:
        y, m, d = h.findtext("Year"), h.findtext("Month"), h.findtext("Day")
        if y:
            return f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
    return None


def _extract_dept(aff):
    if not aff:
        return None
    m = DEPT_RE.search(aff)
    if m:
        dept = m.group(1).strip()
        dept = re.split(r"\b(?:and the|at the)\b", dept)[0].strip()
        return dept[:80]
    return None


def _match_org(aff):
    """Return list of org ids whose match-phrases appear in this affiliation string."""
    if not aff:
        return []
    low = aff.lower()
    hits = []
    for org in CONFIG["organizations"]:
        if any(p in low for p in org["match"]):
            hits.append(org["id"])
    return hits


def parse_article(art):
    pmid = art.findtext(".//PMID")
    if not pmid:
        return None
    title = _text(art.find(".//ArticleTitle")) or "(no title)"
    journal = art.findtext(".//Journal/Title") or art.findtext(".//Journal/ISOAbbreviation") or ""
    pub_year = _parse_pubyear(art)
    entry_date = _parse_entrydate(art)

    # ISSN -> used to look up the journal's impact metric (prefer the linking ISSN)
    issn = art.findtext(".//MedlineJournalInfo/ISSNLinking")
    if not issn:
        issn = art.findtext(".//Journal/ISSN")
    issn = (issn or "").strip().upper() or None

    # abstract (all sections joined) -> used for the genetics keyword test
    abstract = " ".join(_text(a) or "" for a in art.findall(".//Abstract/AbstractText"))
    is_genetics = 1 if GENETICS_RE.search(title + " " + abstract) else 0

    # publication types -> study-type bucket + clinical-trial registry IDs
    pub_types = [pt.text for pt in art.findall(".//PublicationTypeList/PublicationType") if pt.text]
    stype = study_type(pub_types)
    nct = sorted({a.text for a in art.findall(".//DataBankList/DataBank/AccessionNumberList/AccessionNumber")
                  if a.text and a.text.upper().startswith("NCT")})

    doi = None
    for eid in art.findall(".//ELocationID"):
        if eid.get("EIdType") == "doi":
            doi = eid.text
    if not doi:
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = aid.text

    # ---- authors with per-author affiliation + org attribution
    authors = []
    author_els = art.findall(".//AuthorList/Author")
    n = len(author_els)
    for idx, a in enumerate(author_els):
        last = a.findtext("LastName")
        fore = a.findtext("ForeName") or a.findtext("Initials")
        collective = a.findtext("CollectiveName")
        if not last and collective:
            name = collective
        elif last:
            name = f"{fore} {last}".strip() if fore else last
        else:
            continue
        affs = [aff.text for aff in a.findall(".//Affiliation") if aff.text]
        aff_join = " | ".join(affs)
        orcid = None
        for ident in a.findall("Identifier"):
            if ident.get("Source") == "ORCID":
                orcid = re.sub(r"[^0-9X]", "", (ident.text or "").upper())[-16:]
        org_ids = _match_org(aff_join)
        authors.append({
            "name": name,
            "key": (orcid or (name.lower().strip())),
            "orcid": orcid,
            "pos": idx,
            "is_first": idx == 0,
            "is_last": idx == n - 1 and n > 1,
            "aff": aff_join,
            "orgs": org_ids,
            "dept": _extract_dept(aff_join),
        })

    paper_orgs = sorted({o for au in authors for o in au["orgs"]})
    if not paper_orgs:
        # Affiliation may sit on the article (older records) rather than per-author.
        art_aff = " | ".join(t.text for t in art.findall(".//AffiliationInfo/Affiliation") if t.text)
        paper_orgs = sorted(set(_match_org(art_aff)))

    # ---- MeSH / keywords / topic
    mesh = []
    for mh in art.findall(".//MeshHeading/DescriptorName"):
        if mh.text:
            mesh.append({"term": mh.text, "major": mh.get("MajorTopicYN") == "Y"})
    keywords = [k.text for k in art.findall(".//KeywordList/Keyword") if k.text]

    major = [m["term"] for m in mesh if m["major"]]
    if major:
        topic = major[0]
    elif mesh:
        topic = mesh[0]["term"]
    elif keywords:
        topic = keywords[0]
    else:
        topic = journal or "Uncategorized"

    hay = (title + " " + abstract + " " + " ".join(m["term"] for m in mesh)
           + " " + " ".join(keywords)).lower()
    themes = [tid for tid, kws in THEMES if any(k in hay for k in kws)]

    return {
        "pmid": pmid, "title": title, "journal": journal, "pub_year": pub_year,
        "entry_date": entry_date, "doi": doi, "authors": authors,
        "mesh": [m["term"] for m in mesh], "keywords": keywords, "topic": topic,
        "orgs": paper_orgs, "issn": issn, "is_genetics": is_genetics,
        "themes": themes, "pub_types": pub_types, "study_type": stype, "nct": nct,
    }


# ---------------------------------------------------------------- storage
def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS papers (
        pmid TEXT PRIMARY KEY,
        title TEXT, journal TEXT, pub_year INTEGER,
        entry_date TEXT, doi TEXT, topic TEXT,
        orgs TEXT, mesh TEXT, keywords TEXT, authors TEXT,
        first_seen TEXT, issn TEXT, is_genetics INTEGER DEFAULT 0,
        themes TEXT, pub_types TEXT, study_type TEXT, nct TEXT,
        citations INTEGER, cit_updated TEXT, rcr REAL, rcr_updated TEXT,
        recent_cit INTEGER, annotations TEXT, ann_updated TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_year ON papers(pub_year);
    CREATE INDEX IF NOT EXISTS idx_entry ON papers(entry_date);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS journals (
        issn TEXT PRIMARY KEY, name TEXT, jif REAL, updated TEXT
    );
    """)
    # add columns if upgrading an older DB (ignore if they already exist)
    for col, decl in [("issn", "TEXT"), ("is_genetics", "INTEGER DEFAULT 0"),
                      ("themes", "TEXT"), ("pub_types", "TEXT"), ("study_type", "TEXT"),
                      ("nct", "TEXT"), ("citations", "INTEGER"), ("cit_updated", "TEXT"),
                      ("rcr", "REAL"), ("rcr_updated", "TEXT"), ("recent_cit", "INTEGER"),
                      ("annotations", "TEXT"), ("ann_updated", "TEXT")]:
        try:
            con.execute(f"ALTER TABLE papers ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    con.commit()


def upsert(con, p, today):
    cur = con.execute("SELECT first_seen FROM papers WHERE pmid=?", (p["pmid"],))
    row = cur.fetchone()
    first_seen = row[0] if row else today
    con.execute("""
        INSERT INTO papers (pmid,title,journal,pub_year,entry_date,doi,topic,orgs,mesh,keywords,authors,first_seen,issn,is_genetics,themes,pub_types,study_type,nct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pmid) DO UPDATE SET
            title=excluded.title, journal=excluded.journal, pub_year=excluded.pub_year,
            entry_date=excluded.entry_date, doi=excluded.doi, topic=excluded.topic,
            orgs=excluded.orgs, mesh=excluded.mesh, keywords=excluded.keywords,
            authors=excluded.authors, issn=excluded.issn, is_genetics=excluded.is_genetics,
            themes=excluded.themes, pub_types=excluded.pub_types,
            study_type=excluded.study_type, nct=excluded.nct
    """, (
        p["pmid"], p["title"], p["journal"], p["pub_year"], p["entry_date"], p["doi"],
        p["topic"], json.dumps(p["orgs"]), json.dumps(p["mesh"]),
        json.dumps(p["keywords"]), json.dumps(p["authors"]), first_seen,
        p["issn"], p["is_genetics"], json.dumps(p["themes"]), json.dumps(p["pub_types"]),
        p["study_type"], json.dumps(p["nct"]),
    ))
    return row is None  # True if newly inserted


# ------------------------------------------------ journal impact factors (OpenAlex)
def update_impact_factors(con):
    """Fetch an open IF-equivalent (OpenAlex 2-year mean citedness) for any journal
    we don't have yet, or whose value is older than 60 days. Results are cached in
    the `journals` table so daily runs only look up brand-new journals."""
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    have = {r[0]: r[1] for r in con.execute("SELECT issn, updated FROM journals")}
    issns = [r[0] for r in con.execute(
        "SELECT DISTINCT issn FROM papers WHERE issn IS NOT NULL")]
    todo = [i for i in issns if i not in have or (have[i] or "") < cutoff]
    if not todo:
        print("  impact factors: cache up to date", flush=True)
        return
    print(f"  impact factors: looking up {len(todo)} journals via OpenAlex (batched 50/request)...", flush=True)
    BATCH = 50
    done = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        url = ("https://api.openalex.org/sources?per-page=50"
               f"&mailto={urllib.parse.quote(EMAIL)}"
               f"&select=display_name,issn,issn_l,summary_stats,counts_by_year"
               f"&filter=issn:{'|'.join(batch)}")
        recent_years = {datetime.date.today().year - 1, datetime.date.today().year - 2}
        found = {}
        try:
            results = json.loads(_get(url, retries=3)).get("results", [])
            for src in results:
                jif = (src.get("summary_stats") or {}).get("2yr_mean_citedness")
                # recency guard: defunct/tiny journals produce absurd averages from a tiny
                # denominator -> require real recent output before trusting the metric.
                recent = sum(c.get("works_count", 0) for c in (src.get("counts_by_year") or [])
                             if c.get("year") in recent_years)
                if recent < 5:
                    jif = None
                name = src.get("display_name")
                ids = list(src.get("issn") or [])
                if src.get("issn_l"):
                    ids.append(src["issn_l"])
                for isn in ids:
                    found[(isn or "").upper()] = (name, jif)
        except Exception:  # noqa  -- transient; queried ISSNs get stored as NULL, retried next run via cutoff
            pass
        for isn in batch:
            name, jif = found.get(isn, (None, None))
            con.execute(
                "INSERT INTO journals(issn,name,jif,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(issn) DO UPDATE SET name=excluded.name,jif=excluded.jif,updated=excluded.updated",
                (isn, name, jif, today))
        done += len(batch)
        con.commit()
        print(f"    ...{done}/{len(todo)} journals", flush=True)
        time.sleep(0.2)
    matched = con.execute("SELECT COUNT(*) FROM journals WHERE jif IS NOT NULL").fetchone()[0]
    print(f"  impact factors: {matched} journals now have a metric", flush=True)


# ------------------------------------------------ citation counts (OpenAlex)
def update_citations(con, full=False):
    """Fetch per-paper citation counts from OpenAlex (free), batched by PMID.
    full=True refreshes everything; otherwise only papers missing a count or
    whose count is older than 30 days."""
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    if full:
        rows = con.execute("SELECT pmid FROM papers").fetchall()
    else:
        rows = con.execute(
            "SELECT pmid FROM papers WHERE citations IS NULL OR cit_updated IS NULL OR cit_updated < ?",
            (cutoff,)).fetchall()
    todo = [r[0] for r in rows]
    if not todo:
        print("  citations: cache up to date", flush=True)
        return
    print(f"  citations: looking up {len(todo)} papers via OpenAlex (batched 50)...", flush=True)
    done = 0
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        url = ("https://api.openalex.org/works?per-page=50"
               f"&mailto={urllib.parse.quote(EMAIL)}&select=ids,cited_by_count,counts_by_year"
               f"&filter=pmid:{'|'.join(batch)}")
        recent_years = {datetime.date.today().year, datetime.date.today().year - 1}
        found = {}
        try:
            for w in json.loads(_get(url, retries=3)).get("results", []):
                pm = (w.get("ids") or {}).get("pmid", "")
                m = re.search(r"(\d+)$", pm or "")
                if m:
                    recent = sum(c.get("cited_by_count", 0) for c in (w.get("counts_by_year") or [])
                                 if c.get("year") in recent_years)
                    found[m.group(1)] = (w.get("cited_by_count", 0), recent)
        except Exception:  # noqa
            pass
        for pmid in batch:
            cby, rec = found.get(pmid, (None, None))
            con.execute("UPDATE papers SET citations=?, recent_cit=?, cit_updated=? WHERE pmid=?",
                        (cby, rec, today, pmid))
        done += len(batch)
        if done % 500 == 0 or done == len(todo):
            con.commit()
            print(f"    ...{done}/{len(todo)} papers", flush=True)
        time.sleep(0.15)
    con.commit()


# ------------------------------------------------ field-normalized impact (NIH iCite RCR)
def update_rcr(con, full=False):
    """Relative Citation Ratio from NIH iCite (free) -- a field- and time-normalized
    citation metric where 1.0 = average NIH-funded paper in the same field/year.
    Lets you compare impact fairly across disciplines (chemistry vs social work)."""
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    if full:
        rows = con.execute("SELECT pmid FROM papers").fetchall()
    else:
        rows = con.execute(
            "SELECT pmid FROM papers WHERE rcr_updated IS NULL OR rcr_updated < ?",
            (cutoff,)).fetchall()
    todo = [r[0] for r in rows]
    if not todo:
        print("  RCR: cache up to date", flush=True)
        return
    print(f"  RCR (NIH iCite): looking up {len(todo)} papers (batched 200)...", flush=True)
    done = 0
    for i in range(0, len(todo), 200):
        batch = todo[i:i + 200]
        url = "https://icite.od.nih.gov/api/pubs?pmids=" + ",".join(batch)
        found = {}
        try:
            for rec in json.loads(_get(url, retries=3)).get("data", []):
                pm = str(rec.get("pmid"))
                found[pm] = rec.get("relative_citation_ratio")
        except Exception:  # noqa
            pass
        for pmid in batch:
            con.execute("UPDATE papers SET rcr=?, rcr_updated=? WHERE pmid=?",
                        (found.get(pmid), today, pmid))
        done += len(batch)
        con.commit()
        if done % 1000 == 0 or done == len(todo):
            print(f"    ...{done}/{len(todo)} papers", flush=True)
        time.sleep(0.2)
    matched = con.execute("SELECT COUNT(*) FROM papers WHERE rcr IS NOT NULL").fetchone()[0]
    print(f"  RCR: {matched} papers have a field-normalized score", flush=True)


# ------------------------------------------------ bioconcept annotations (NCBI PubTator3)
PT_BUCKET = {"Gene": "g", "Disease": "d", "Chemical": "c",
             "Mutation": "v", "Variant": "v", "DNAMutation": "v",
             "ProteinMutation": "v", "SNP": "v", "DNAAcidChange": "v"}


def update_annotations(con, full=False):
    """Per-paper normalized bioconcepts (genes, diseases, chemicals, variants) from
    NCBI PubTator3. Biomedical only -- non-bio papers simply get no entities. Cached."""
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    if full:
        rows = con.execute("SELECT pmid FROM papers").fetchall()
    else:
        rows = con.execute(
            "SELECT pmid FROM papers WHERE ann_updated IS NULL OR ann_updated < ?", (cutoff,)).fetchall()
    todo = [r[0] for r in rows]
    if not todo:
        print("  annotations: cache up to date", flush=True)
        return
    print(f"  annotations (PubTator3): looking up {len(todo)} papers (batched 100)...", flush=True)
    base = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
    done = 0
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        url = base + "?pmids=" + ",".join(batch)
        per = {pmid: {} for pmid in batch}   # pmid -> bucket -> {id: [count, text]}
        try:
            docs = json.loads(_get(url, retries=3)).get("PubTator3", [])
            for doc in docs:
                pm = str(doc.get("pmid") or doc.get("id") or "")
                if pm not in per:
                    continue
                for pas in doc.get("passages", []):
                    for a in pas.get("annotations", []):
                        inf = a.get("infons", {})
                        b = PT_BUCKET.get(inf.get("type"))
                        txt = (a.get("text") or "").strip()
                        if not b or not txt:
                            continue
                        key = inf.get("identifier") or txt.lower()
                        slot = per[pm].setdefault(b, {})
                        if key in slot:
                            slot[key][0] += 1
                        else:
                            slot[key] = [1, txt]
        except Exception:  # noqa
            pass
        for pmid in batch:
            out = {}
            for b, slot in per[pmid].items():
                top = sorted(slot.values(), key=lambda x: -x[0])[:8]
                out[b] = [t[1] for t in top]
            con.execute("UPDATE papers SET annotations=?, ann_updated=? WHERE pmid=?",
                        (json.dumps(out) if out else None, today, pmid))
        done += len(batch)
        con.commit()
        if done % 500 == 0 or done == len(todo):
            print(f"    ...{done}/{len(todo)} papers", flush=True)
        time.sleep(0.34)
    n = con.execute("SELECT COUNT(*) FROM papers WHERE annotations IS NOT NULL").fetchone()[0]
    print(f"  annotations: {n} papers have bioconcepts", flush=True)


# ---------------------------------------------------------------- run
def run(mode):
    today = datetime.date.today().isoformat()
    reldate = None if mode == "backfill" else 45
    con = sqlite3.connect(DB_PATH)
    init_db(con)

    all_ids = set()
    for org in CONFIG["organizations"]:
        ids = esearch_ids(org["query"], reldate=reldate)
        print(f"  [{org['id']}] PubMed returned {len(ids)} candidate PMIDs", flush=True)
        all_ids.update(ids)
    all_ids = list(all_ids)
    print(f"Total unique candidate PMIDs: {len(all_ids)}", flush=True)

    new_count, kept, dropped = 0, 0, 0
    for art in efetch_xml(all_ids):
        p = parse_article(art)
        if not p:
            continue
        if not p["orgs"]:        # candidate that didn't actually verify -> skip
            dropped += 1
            continue
        if upsert(con, p, today):
            new_count += 1
        kept += 1
        if kept % 500 == 0:
            con.commit()
            print(f"  ...processed {kept} verified papers", flush=True)
    update_impact_factors(con)
    update_citations(con, full=(mode == "backfill"))
    update_rcr(con, full=(mode == "backfill"))
    update_annotations(con, full=(mode == "backfill"))
    con.execute("INSERT INTO meta(key,value) VALUES('last_run',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today + "T" + datetime.datetime.now().strftime("%H:%M"),))
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    con.close()
    print(f"Done. Verified+stored this run: {kept} (new: {new_count}), "
          f"dropped as false-match: {dropped}. DB now holds {total} papers.", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "update"
    if mode not in ("backfill", "update"):
        print("usage: python3 pipeline.py [backfill|update]")
        sys.exit(1)
    print(f"=== pipeline {mode} @ {datetime.datetime.now()} ===", flush=True)
    run(mode)
