from __future__ import annotations
from src.fetch import _http_get, fetch_institute_pids, fetch_stream_bytes, rels_int_url
from src.model import DuplicateGroup, FileStreamRef
from src.parser import parse_rels_int_supplements
from src.progress import Progress
from src.config import PDF_DSIDS, SIZE_PCT, SIZE_ABS, TEXT_FUZZY_MIN_CHARS, TEXT_FUZZY_RATIO

import hashlib
import json
import logging
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import pymupdf


logger = logging.getLogger("duplicate_checker")


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


def load_cached_data(cache_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not cache_path.exists():
        return {}
    try:
        cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read cache file {cache_path}: {exc}") from exc
    if not isinstance(cached_data, dict) or any(
        not isinstance(pid, str)
        or not isinstance(streams, list)
        or any(not isinstance(stream, dict) for stream in streams)
        for pid, streams in cached_data.items()
    ):
        raise RuntimeError(f"Cache file {cache_path} has an unexpected format")
    return cached_data


def get_cache_status(
    institute: str, year_range: str, solr_url: str, *, fuzzy: bool
) -> dict[str, int]:
    docs = fetch_institute_pids(institute, year_range, solr_url, raise_errors=True)
    pids = {str(doc["PID"]) for doc in docs if doc.get("PID")}
    cached_data = load_cached_data(Path(f"cache_{institute}.json"))
    cached_pids = pids.intersection(cached_data)
    empty_cached = sum(not cached_data[pid] for pid in cached_pids)
    all_refs = [
        FileStreamRef(**stream)
        for pid in cached_pids
        for stream in cached_data[pid]
    ]
    duplicates = find_duplicates(all_refs, fuzzy=fuzzy)
    exact = [group for group in duplicates if group.match_type == "exact_sha256"]
    fuzzy_groups = [group for group in duplicates if group.match_type == "text_fuzzy"]
    return {
        "total": len(pids),
        "cached": len(cached_pids),
        "pending": len(pids) - len(cached_pids),
        "empty": empty_cached,
        "streams": len(all_refs),
        "exact_duplicates": len(exact),
        "exact_cross_pid": sum(group.is_cross_pid for group in exact),
        "fuzzy_duplicates": len(fuzzy_groups),
        "fuzzy_cross_pid": sum(group.is_cross_pid for group in fuzzy_groups),
    }


def find_duplicates(
    all_refs: list[FileStreamRef], *, fuzzy: bool
) -> list[DuplicateGroup]:
    hash_map: dict[str, list[FileStreamRef]] = defaultdict(list)
    claimed_refs: set[int] = set()
    duplicates: list[DuplicateGroup] = []

    for ref in all_refs:
        hash_map[ref.sha256].append(ref)

    for sha256, refs in hash_map.items():
        if len(refs) > 1:
            claimed_refs.update(id(ref) for ref in refs)
            pids_set = {ref.pid for ref in refs}
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
        unclaimed = [ref for ref in all_refs if id(ref) not in claimed_refs]
        for index, left in enumerate(unclaimed):
            if id(left) in claimed_refs or len(left.text) < TEXT_FUZZY_MIN_CHARS:
                continue
            group_list = [left]
            for right in unclaimed[index + 1:]:
                if id(right) in claimed_refs or len(right.text) < TEXT_FUZZY_MIN_CHARS:
                    continue
                if not sizes_similar(left.size, right.size):
                    continue
                if SequenceMatcher(None, left.text, right.text).ratio() >= TEXT_FUZZY_RATIO:
                    claimed_refs.add(id(right))
                    group_list.append(right)
            if len(group_list) > 1:
                claimed_refs.add(id(left))
                for ref in group_list:
                    ref.match_method = "text_fuzzy"
                pids_set = {ref.pid for ref in group_list}
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
    return duplicates


def analyze_institute(
    institute: str,
    year_range: str,
    solr_url: str,
    workers: int,
    fuzzy: bool,
    force: bool,
    delay: float,
    max_time_sec: float,
) -> dict[str, Any]:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            filename=Path.cwd() / "duplicate_checker.log",
            level=logging.INFO,
            format="%(asctime)s %(processName)s %(levelname)s %(message)s",
            encoding="utf-8",
        )
    start_time_run = time.time()
    logger.info("Starting institute scan: %s", institute)
    docs = fetch_institute_pids(institute, year_range, solr_url, raise_errors=True)
    if not docs:
        logger.warning("Solr returned no records for %s", institute)
        return {"institute": institute, "all_refs_count": 0, "duplicates": []}

    cache_path = Path(f"cache_{institute}.json")
    cached_data: dict[str, list[dict[str, Any]]] = {}

    if not force:
        cached_data = load_cached_data(cache_path)
        if cached_data:
            print(f"Loaded {len(cached_data)} cached publication records for [{institute}] from {cache_path.name}")
            logger.info("Loaded %d cached records for %s", len(cached_data), institute)

    docs_to_process = [d for d in docs if d.get("PID") and d.get("PID") not in cached_data]
    print(f"Publications in [{institute}]: Total={len(docs)}, Already Cached={len(cached_data)}, To Scan={len(docs_to_process)}")
    logger.info(
        "%s: Solr total=%d cached=%d pending=%d",
        institute,
        len(docs),
        len(cached_data),
        len(docs_to_process),
    )

    if docs_to_process:
        progress = Progress(len(docs_to_process), label=f"scan {institute}")
        done_count = 0
        time_limit_reached = False
        doc_iterator = iter(docs_to_process)
        futures = {}
        exhausted = False

        def save_result(future, pid: str | None) -> None:
            nonlocal done_count
            try:
                res = future.result()
                cached_data[pid] = [asdict(ref) for ref in res] if res else []
            except Exception as exc:
                print(f"[!] Error processing {pid} for [{institute}]: {exc}")
                logger.exception("Failed processing PID %s for %s", pid, institute)
                cached_data.pop(pid, None)
            done_count += 1
            progress.update(done_count, extra=pid or "")
            if done_count % 25 == 0:
                cache_path.write_text(
                    json.dumps(cached_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info("Saved cache checkpoint for %s after %d records", institute, done_count)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            while futures or not exhausted:
                while len(futures) < max(1, workers) and not exhausted:
                    if max_time_sec > 0 and time.time() - start_time_run >= max_time_sec:
                        time_limit_reached = True
                        break
                    try:
                        doc = next(doc_iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    future = executor.submit(process_doc, doc, fuzzy, delay)
                    futures[future] = doc.get("PID")

                if not futures:
                    break

                timeout = None
                if max_time_sec > 0:
                    timeout = max(0, max_time_sec - (time.time() - start_time_run))
                completed, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
                if not completed:
                    time_limit_reached = True
                    break

                for future in completed:
                    pid = futures.pop(future)
                    save_result(future, pid)

            if time_limit_reached:
                for future, pid in list(futures.items()):
                    save_result(future, pid)
                futures.clear()

        if done_count == len(docs_to_process):
            progress.finish()
        else:
            progress.update(done_count, extra="paused at time limit", newline=True)
        cache_path.write_text(json.dumps(cached_data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved cache for %s (%d records)", institute, len(cached_data))
        if time_limit_reached:
            print(f"[!] Time limit reached for [{institute}]. Progress saved to cache. You can resume later.")
            logger.info("Time limit reached for %s; scan will resume from cache", institute)

    all_refs: list[FileStreamRef] = []
    for pid, streams in cached_data.items():
        for s in streams:
            all_refs.append(FileStreamRef(**s))

    duplicates = find_duplicates(all_refs, fuzzy=fuzzy)
    logger.info(
        "Completed institute scan %s: streams=%d duplicate_groups=%d",
        institute,
        len(all_refs),
        len(duplicates),
    )

    return {
        "institute": institute,
        "all_refs_count": len(all_refs),
        "duplicates": duplicates,
    }