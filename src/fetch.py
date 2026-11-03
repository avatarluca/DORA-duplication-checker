
from __future__ import annotations
from src.config import SOLR_SELECT_URL, USER_AGENT, PDF_DSIDS, ADMIN_DORA_BASE
from typing import Any

import json
import sys
import urllib.parse
import urllib.request


def _http_get(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_institute_pids(institute: str, year_range: str = "", solr_url: str = SOLR_SELECT_URL) -> list[dict[str, Any]]:
    institute = institute.strip().lower()
    clauses = [f"PID:{institute}\\:*"]
    
    if year_range:
        clauses.append(f"mods_originInfo_encoding_w3cdtf_keyDate_yes_dateIssued_dt:[{year_range}-01-01T00:00:00Z TO {year_range}-12-31T23:59:59.999Z]")

    query = " AND ".join(clauses)
    fields = ["PID", "fgs_label_s"]
    for dsid in PDF_DSIDS:
        fields.append(f"fedora_datastream_info_{dsid}_ID_mt")
        fields.append(f"fedora_datastream_version_{dsid}_LABEL_ms")

    params = {
        "q": query,
        "wt": "json",
        "rows": "50000",
        "fl": ",".join(fields),
    }
    url = f"{solr_url}?{urllib.parse.urlencode(params)}"
    
    print(f"Fetching PIDs from Solr for institute: {institute}...")
    try:
        payload = json.loads(_http_get(url, accept="application/json").decode("utf-8"))
        docs = payload.get("response", {}).get("docs", [])
        print(f"Received {len(docs)} records from Solr for {institute}.")
        return docs
    except Exception as exc:
        print(f"[!] Solr query error for {institute}: {exc}", file=sys.stderr)
        return []


def datastream_url(pid: str, dsid: str) -> str:
    institute, _, _ = pid.partition(":")
    quoted = urllib.parse.quote(pid, safe=":")
    return f"{ADMIN_DORA_BASE}/{institute}/islandora/object/{quoted}/datastream/{dsid}/view"


def rels_int_url(pid: str) -> str:
    institute, _, _ = pid.partition(":")
    quoted = urllib.parse.quote(pid, safe=":")
    return f"{ADMIN_DORA_BASE}/{institute}/islandora/object/{quoted}/datastream/RELS-INT/view"


def fetch_stream_bytes(pid: str, dsid: str) -> bytes | None:
    try:
        url = datastream_url(pid, dsid)
        return _http_get(url)
    except Exception:
        return None
