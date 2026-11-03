from __future__ import annotations

import argparse
import json
import sys
import tomllib
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from src.config import SOLR_SELECT_URL
from src.processor import analyze_institute


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-institute PDF duplicate checker via DORA & Solr with Caching & Rate-Lmiting.")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--fuzzy", action="store_true", help="Enable text fuzzy matching (82%) for PDFs with similar sizes")
    parser.add_argument("--force", action="store_true", help="Ignore existing cache backups and rescan everything from scratch")
    parser.add_argument("--delay", type=float, default=5.0, help="Delay in seconds between individual PDF downloads (default: 5.0s)")
    parser.add_argument("--max-hours", type=float, default=10.0, help="Maximum runtime in hours before graceful stop (default: 10.0h)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[!] Config file {config_path} not found.", file=sys.stderr)
        return 1

    with config_path.open("rb") as f:
        cfg = tomllib.load(f)

    inst_cfg = cfg.get("institute", {})
    set_cfg = cfg.get("settings", {})

    inst_names = inst_cfg.get("names", inst_cfg.get("name", "eawag"))
    if isinstance(inst_names, str):
        institutes = [inst_names]
    else:
        institutes = [str(i) for i in inst_names]

    year_range = str(inst_cfg.get("year_range", ""))
    solr_url = set_cfg.get("solr_url", SOLR_SELECT_URL)
    workers = int(set_cfg.get("workers", 2)) 
    json_out = set_cfg.get("output_json", "instituts_duplikate.json")
    csv_out = set_cfg.get("output_csv", "instituts_duplikate.csv")
    max_time_sec = args.max_hours * 3600

    print(f"Running duplicate check for institutes: {', '.join(institutes)}")
    print(f"    - Fuzzy: {args.fuzzy} | Force: {args.force} | Delay: {args.delay}s | Max Run: {args.max_hours}h")

    all_results = []
    
    with ProcessPoolExecutor(max_workers=min(len(institutes), 4)) as executor:
        futures = {
            executor.submit(analyze_institute, inst, year_range, solr_url, workers, args.fuzzy, args.force, args.delay, max_time_sec): inst
            for inst in institutes
        }
        for future in as_completed(futures):
            try:
                res = future.result()
                all_results.append(res)
            except Exception as exc:
                print(f"[!] Error processing institute {futures[future]}: {exc}", file=sys.stderr)

    total_streams = sum(r["all_refs_count"] for r in all_results)
    combined_duplicates = []
    for r in all_results:
        combined_duplicates.extend(r["duplicates"])

    exact_groups = [g for g in combined_duplicates if g.match_type == "exact_sha256"]
    fuzzy_groups = [g for g in combined_duplicates if g.match_type == "text_fuzzy"]

    exact_cross = sum(1 for g in exact_groups if g.is_cross_pid)
    exact_intra = len(exact_groups) - exact_cross

    fuzzy_cross = sum(1 for g in fuzzy_groups if g.is_cross_pid)
    fuzzy_intra = len(fuzzy_groups) - fuzzy_cross

    print(f"\nOverall Analysis Completd Across All Institutes:")
    print(f"    - Total file streams checked: {total_streams}")
    print(f"    - Total duplicate groups: {len(combined_duplicates)}")
    print(f"            - EXACT MATCHES (SHA256): {len(exact_groups)}")
    print(f"            - Intra-article: {exact_intra}")
    print(f"            - Cross-PID: {exact_cross}")
    print(f"    - FUZZY TEXT MATCHES (82%): {len(fuzzy_groups)}")
    print(f"            - Intra-article: {fuzzy_intra}")
    print(f"            - Cross-PID: {fuzzy_cross}")

    if combined_duplicates:
        json_data = [
            {
                "match_key": g.match_key,
                "size_bytes": g.size,
                "match_type": g.match_type,
                "is_cross_pid": g.is_cross_pid,
                "pids": list(g.pids),
                "streams": [asdict(s) for s in g.streams],
            }
            for g in combined_duplicates
        ]
        Path(json_out).write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

        with open(csv_out, "w", encoding="utf-8") as f:
            f.write("Match_Key;Groesse_Bytes;Match_Type;Is_Cross_PID;PID;DSID;Stream_Typ;Label\n")
            for g in combined_duplicates:
                for s in g.streams:
                    f.write(f"{g.match_key};{g.size};{g.match_type};{g.is_cross_pid};{s.pid};{s.dsid};{s.stream_type};{s.label}\n")

        print(f"    - JSON report saved to: {json_out}")
        print(f"    - CSV report saved to:  {csv_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())