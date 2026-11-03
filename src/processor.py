from __future__ import annotations
from src.fetch import _http_get, fetch_institute_pids, fetch_stream_bytes, rels_int_url
from src.model import DuplicateGroup, FileStreamRef
from src.parser import parse_rels_int_supplements
from src.progress import Progress
from src.config import PDF_DSIDS, SIZE_PCT, SIZE_ABS, TEXT_FUZZY_MIN_CHARS, TEXT_FUZZY_RATIO

import hashlib
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import pymupdf


def extract_pdf_text(data: bytes, limit: int = 12000) -> str:
    if not data.startswith(b"%PDF"):
        return ""
    try:
        pymupdf.TOOLS.mupdf_display_errors(False)
        
        doc = pymupdf.open(stream=data, filetype="pdf")
        parts = []
        for index, page in enumerate(doc):
            try:
                parts.append(page.get_text("text") or "")
            except Exception:
                pass
            if index >= 9:
                break
        doc.close()
        text = " ".join(parts)
        return " ".join(text.split()).lower()[:limit]
    except Exception:
        return ""


def sizes_similar(left: int, right: int, *, pct: float = SIZE_PCT, abs_bytes: int = SIZE_ABS) -> bool:
    if left <= 0 or right <= 0:
        return False
    delta = abs(left - right)
    if delta == 0:
        return True
    band = max(abs_bytes, int(max(left, right) * pct))
    return delta <= band


def process_doc(doc: dict[str, Any], extract_text: bool, delay: float) -> list[FileStreamRef]:
    pid = doc.get("PID")
    if not pid:
        return []

    refs: list[FileStreamRef] = []
    seen_dsids = set()

    def _add_stream(dsid: str, label: str, stream_type: str):
        data = fetch_stream_bytes(pid, dsid)
        if not data:
            return
        sha256 = hashlib.sha256(data).hexdigest()
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        text = extract_pdf_text(data) if extract_text else ""
        refs.append(
            FileStreamRef(
                pid=pid,
                dsid=dsid,
                label=label,
                stream_type=stream_type,
                size=len(data),
                sha256=sha256,
                md5=md5,
                text=text,
            )
        )
        if delay > 0:
            time.sleep(delay)

    for dsid in PDF_DSIDS:
        if not doc.get(f"fedora_datastream_info_{dsid}_ID_mt"):
            continue
        seen_dsids.add(dsid)
        labels = doc.get(f"fedora_datastream_version_{dsid}_LABEL_ms") or []
        label = labels[0] if labels else dsid
        _add_stream(dsid, label, "primary_pdf")

    try:
        rels_bytes = _http_get(rels_int_url(pid), accept="application/rdf+xml")
        supp_dsids = parse_rels_int_supplements(rels_bytes)
        for dsid in supp_dsids:
            if dsid in seen_dsids:
                continue
            seen_dsids.add(dsid)
            _add_stream(dsid, dsid, "supplemental")
    except Exception:
        pass

    return refs


def analyze_institute(institute: str, year_range: str, solr_url: str, workers: int, fuzzy: bool, force: bool, delay: float, max_time_sec: float) -> dict[str, Any]:
    start_time = time.time()
    docs = fetch_institute_pids(institute, year_range, solr_url)
    if not docs:
        return {"institute": institute, "all_refs_count": 0, "duplicates": []}

    cache_path = Path(f"cache_{institute}.json")
    cached_data: dict[str, list[dict[str, Any]]] = {}

    if not force and cache_path.exists():
        try:
            cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"Loaded {len(cached_data)} cached publication records for [{institute}] from {cache_path.name}")
        except Exception:
            cached_data = {}

    docs_to_process = [d for d in docs if d.get("PID") not in cached_data]
    print(f"Publications in [{institute}]: Total={len(docs)}, Already Cached={len(cached_data)}, To Scan={len(docs_to_process)}")

    if docs_to_process:
        progress = Progress(len(docs_to_process), label=f"scan {institute}")
        done_count = 0
        cache_lock = threading.Lock()
        time_limit_reached = False

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_doc, doc, fuzzy, delay): doc.get("PID") for doc in docs_to_process}
            for future in as_completed(futures):
                if max_time_sec > 0 and (time.time() - start_time) > max_time_sec:
                    time_limit_reached = True
                    break

                pid = futures[future]
                try:
                    res = future.result()
                    streams_dict = [asdict(r) for r in res] if res else []
                    with cache_lock:
                        cached_data[pid] = streams_dict
                except Exception:
                    with cache_lock:
                        cached_data[pid] = []
                
                done_count += 1
                progress.update(done_count, extra=pid)

                if done_count % 25 == 0:
                    with cache_lock:
                        cache_path.write_text(json.dumps(cached_data, indent=2, ensure_ascii=False), encoding="utf-8")

        progress.finish()
        cache_path.write_text(json.dumps(cached_data, indent=2, ensure_ascii=False), encoding="utf-8")
        if time_limit_reached:
            print(f"[!] Time limit reached for [{institute}]. Progress saved to cache. You can resume later.")

    all_refs: list[FileStreamRef] = []
    for pid, streams in cached_data.items():
        for s in streams:
            all_refs.append(FileStreamRef(**s))

    hash_map: dict[str, list[FileStreamRef]] = defaultdict(list)
    claimed_refs: set[int] = set()
    duplicates: list[DuplicateGroup] = []

    for ref in all_refs:
        hash_map[ref.sha256].append(ref)

    for sha256, refs in hash_map.items():
        if len(refs) > 1:
            for r in refs:
                claimed_refs.add(id(r))
            pids_set = {r.pid for r in refs}
            duplicates.append(
                DuplicateGroup(
                    match_key=sha256,
                    size=refs[0].size,
                    match_type="exact_sha256",
                    streams=refs,
                    is_cross_pid=len(pids_set) > 1,
                    pids=pids_set,
                )
            )

    if fuzzy:
        unclaimed = [r for r in all_refs if id(r) not in claimed_refs]
        
        for i, left in enumerate(unclaimed):
            if id(left) in claimed_refs:
                continue
            if len(left.text) < TEXT_FUZZY_MIN_CHARS:
                continue
                
            group_list = [left]
            
            for right in unclaimed[i + 1:]:
                if id(right) in claimed_refs:
                    continue
                if len(right.text) < TEXT_FUZZY_MIN_CHARS:
                    continue
                if not sizes_similar(left.size, right.size):
                    continue
                
                ratio = SequenceMatcher(None, left.text, right.text).ratio()
                if ratio >= TEXT_FUZZY_RATIO:
                    claimed_refs.add(id(right))
                    group_list.append(right)
            
            if len(group_list) > 1:
                claimed_refs.add(id(left))
                for r in group_list:
                    r.match_method = "text_fuzzy"
                pids_set = {r.pid for r in group_list}
                duplicates.append(
                    DuplicateGroup(
                        match_key=f"fuzzy_{left.sha256[:8]}",
                        size=left.size,
                        match_type="text_fuzzy",
                        streams=group_list,
                        is_cross_pid=len(pids_set) > 1,
                        pids=pids_set,
                    )
                )

    return {
        "institute": institute,
        "all_refs_count": len(all_refs),
        "duplicates": duplicates,
    }