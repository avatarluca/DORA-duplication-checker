from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import tomllib
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from src.config import SOLR_SELECT_URL
from src.processor import analyze_institute, get_cache_status


logger = logging.getLogger("duplicate_checker")
LOG_PATH = Path("duplicate_checker.log")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(processName)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


@dataclass(frozen=True)
class ScheduleWindow:
    days: frozenset[int]
    start: datetime.time
    end: datetime.time


def parse_clock(value: Any, field: str) -> datetime.time:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a time in HH:MM format")
    try:
        return datetime.datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid {field} {value!r}; expected HH:MM") from exc


def parse_schedule(raw_windows: Any) -> list[ScheduleWindow]:
    if not isinstance(raw_windows, list):
        raise ValueError("schedule.windows must be a list of window tables")

    windows = []
    for index, raw in enumerate(raw_windows, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"schedule window {index} must be a table")
        raw_days = raw.get("days")
        if not isinstance(raw_days, list) or not raw_days:
            raise ValueError(f"schedule window {index} needs a non-empty days list")
        days = set()
        for day in raw_days:
            if not isinstance(day, str) or day.strip().lower()[:3] not in WEEKDAYS:
                raise ValueError(f"Unknown weekday {day!r} in schedule window {index}")
            days.add(WEEKDAYS[day.strip().lower()[:3]])
        start = parse_clock(raw.get("start"), f"window {index} start")
        end = parse_clock(raw.get("end"), f"window {index} end")
        if start == end:
            raise ValueError(f"Schedule window {index} cannot have identical start and end times")
        windows.append(ScheduleWindow(frozenset(days), start, end))
    return windows


def daily_window(start: str, end: str) -> list[ScheduleWindow]:
    return [ScheduleWindow(frozenset(range(7)), parse_clock(start, "start-time"), parse_clock(end, "end-time"))]


def active_window(
    now: datetime.datetime, windows: list[ScheduleWindow]
) -> datetime.datetime | None:
    for window in windows:
        for start_date in (now.date(), now.date() - datetime.timedelta(days=1)):
            if start_date.weekday() not in window.days:
                continue
            start = datetime.datetime.combine(start_date, window.start)
            end_date = start_date + datetime.timedelta(days=window.end <= window.start)
            end = datetime.datetime.combine(end_date, window.end)
            if start <= now < end:
                return end
    return None


def next_window_start(
    now: datetime.datetime, windows: list[ScheduleWindow]
) -> datetime.datetime:
    candidates = []
    for offset in range(8):
        start_date = now.date() + datetime.timedelta(days=offset)
        for window in windows:
            if start_date.weekday() in window.days:
                candidate = datetime.datetime.combine(start_date, window.start)
                if candidate > now:
                    candidates.append(candidate)
    if not candidates:
        raise ValueError("No future start found in schedule.windows")
    return min(candidates)


def wait_for_window(windows: list[ScheduleWindow]) -> datetime.datetime:
    last_announced_start: datetime.datetime | None = None
    while True:
        now = datetime.datetime.now()
        end = active_window(now, windows)
        if end is not None:
            logger.info("Entering configured run window, ending at %s", end.isoformat())
            return end
        start = next_window_start(now, windows)
        seconds = max(1, (start - now).total_seconds())
        if start != last_announced_start:
            logger.info("Waiting for next run window at %s", start.isoformat())
            print(f"Next run window starts at {start:%a %Y-%m-%d %H:%M}; waiting.")
            last_announced_start = start
        time.sleep(min(60, seconds))


def print_status(
    institutes: list[str], year_range: str, solr_url: str, *, fuzzy: bool
) -> tuple[int, bool]:
    rows = []
    failed = False
    for institute in institutes:
        try:
            rows.append(
                (institute, get_cache_status(institute, year_range, solr_url, fuzzy=fuzzy))
            )
        except Exception as exc:
            failed = True
            logger.exception("Could not get cache status for %s", institute)
            print(f"[!] Could not get status for {institute}: {exc}", file=sys.stderr)

    print("\nCache status (current Solr records):")
    for institute, status in rows:
        percent = 100 * status["cached"] / status["total"] if status["total"] else 100.0
        exact_cross = status["exact_cross_pid"]
        exact_intra = status["exact_duplicates"] - exact_cross
        logger.info(
            "Status %s: %d/%d cached, %d pending, exact_duplicates=%d",
            institute,
            status["cached"],
            status["total"],
            status["pending"],
            status["exact_duplicates"],
        )
        if fuzzy:
            logger.info(
                "Status %s: fuzzy_duplicates=%d",
                institute,
                status["fuzzy_duplicates"],
            )
        print(
            f"  {institute}: {status['cached']}/{status['total']} cached "
            f"({percent:.1f}%), {status['pending']} open, "
            f"{status['empty']} cached with no streams, {status['streams']} streams"
        )
        print(
            f"    Exact duplicate groups: {status['exact_duplicates']} "
            f"({exact_intra} intra-PID, {exact_cross} cross-PID)"
        )
        if fuzzy:
            fuzzy_cross = status["fuzzy_cross_pid"]
            fuzzy_intra = status["fuzzy_duplicates"] - fuzzy_cross
            print(
                f"    Fuzzy duplicate groups: {status['fuzzy_duplicates']} "
                f"({fuzzy_intra} intra-PID, {fuzzy_cross} cross-PID)"
            )
    if rows:
        total = sum(status["total"] for _, status in rows)
        cached = sum(status["cached"] for _, status in rows)
        pending = sum(status["pending"] for _, status in rows)
        empty = sum(status["empty"] for _, status in rows)
        exact = sum(status["exact_duplicates"] for _, status in rows)
        exact_cross = sum(status["exact_cross_pid"] for _, status in rows)
        percent = 100 * cached / total if total else 100.0
        print(
            f"  Total: {cached}/{total} cached ({percent:.1f}%), "
            f"{pending} open, {empty} empty"
        )
        print(
            f"    Exact duplicate groups: {exact} "
            f"({exact - exact_cross} intra-PID, {exact_cross} cross-PID)"
        )
        if fuzzy:
            fuzzy_total = sum(status["fuzzy_duplicates"] for _, status in rows)
            fuzzy_cross = sum(status["fuzzy_cross_pid"] for _, status in rows)
            print(
                f"    Fuzzy duplicate groups: {fuzzy_total} "
                f"({fuzzy_total - fuzzy_cross} intra-PID, {fuzzy_cross} cross-PID)"
            )
    complete = bool(rows) and all(status["pending"] == 0 for _, status in rows)
    logger.info(
        "Cache status: complete=%s pending=%d institutes=%d",
        complete,
        sum(status["pending"] for _, status in rows),
        len(rows),
    )
    return (1 if failed else 0), complete


def run_analysis(
    institutes: list[str],
    year_range: str,
    solr_url: str,
    workers: int,
    args: argparse.Namespace,
    max_time_sec: float,
) -> int:
    print(f"Running duplicate check for institutes: {', '.join(institutes)}")
    logger.info("Starting scan for institutes: %s", ", ".join(institutes))
    print(
        f"    - Fuzzy: {args.fuzzy} | Force: {args.force} | Delay: {args.delay}s "
        f"| Max Run: {max_time_sec / 3600:.2f}h"
    )

    all_results = []
    failed = False
    with ProcessPoolExecutor(max_workers=min(len(institutes), 4)) as executor:
        futures = {
            executor.submit(
                analyze_institute,
                inst,
                year_range,
                solr_url,
                workers,
                args.fuzzy,
                args.force,
                args.delay,
                max_time_sec,
            ): inst
            for inst in institutes
        }
        for future in as_completed(futures):
            try:
                all_results.append(future.result())
            except Exception as exc:
                failed = True
                logger.exception("Institute scan failed: %s", futures[future])
                print(f"[!] Error processing institute {futures[future]}: {exc}", file=sys.stderr)

    total_streams = sum(r["all_refs_count"] for r in all_results)
    combined_duplicates = [group for result in all_results for group in result["duplicates"]]
    exact_groups = [group for group in combined_duplicates if group.match_type == "exact_sha256"]
    fuzzy_groups = [group for group in combined_duplicates if group.match_type == "text_fuzzy"]
    exact_cross = sum(1 for group in exact_groups if group.is_cross_pid)
    fuzzy_cross = sum(1 for group in fuzzy_groups if group.is_cross_pid)

    print("\nOverall Analysis Completed Across All Institutes:")
    print(f"    - Total file streams checked: {total_streams}")
    print(f"    - Total duplicate groups: {len(combined_duplicates)}")
    print(f"            - EXACT MATCHES (SHA256): {len(exact_groups)}")
    print(f"            - Intra-article: {len(exact_groups) - exact_cross}")
    print(f"            - Cross-PID: {exact_cross}")
    print(f"    - FUZZY TEXT MATCHES (82%): {len(fuzzy_groups)}")
    print(f"            - Intra-article: {len(fuzzy_groups) - fuzzy_cross}")
    print(f"            - Cross-PID: {fuzzy_cross}")

    if combined_duplicates:
        json_data = [
            {
                "match_key": group.match_key,
                "size_bytes": group.size,
                "match_type": group.match_type,
                "is_cross_pid": group.is_cross_pid,
                "pids": list(group.pids),
                "streams": [asdict(stream) for stream in group.streams],
            }
            for group in combined_duplicates
        ]
        Path(args.output_json).write_text(
            json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        with open(args.output_csv, "w", encoding="utf-8") as output_file:
            output_file.write("Match_Key;Groesse_Bytes;Match_Type;Is_Cross_PID;PID;DSID;Stream_Typ;Label\n")
            for group in combined_duplicates:
                for stream in group.streams:
                    output_file.write(
                        f"{group.match_key};{group.size};{group.match_type};{group.is_cross_pid};"
                        f"{stream.pid};{stream.dsid};{stream.stream_type};{stream.label}\n"
                    )
        print(f"    - JSON report saved to: {args.output_json}")
        print(f"    - CSV report saved to:  {args.output_csv}")

    logger.info(
        "Scan finished: streams=%d duplicate_groups=%d failed=%s",
        total_streams,
        len(combined_duplicates),
        failed,
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-institute PDF duplicate checker via DORA & Solr with caching and scheduling."
    )
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--fuzzy", action="store_true", help="Enable text fuzzy matching (82%) for PDFs with similar sizes")
    parser.add_argument("--force", action="store_true", help="Ignore existing cache backups and rescan everything from scratch")
    parser.add_argument("--delay", type=float, default=5.0, help="Delay in seconds between individual PDF downloads (default: 5.0s)")
    parser.add_argument("--max-hours", type=float, default=10.0, help="Maximum runtime in hours before graceful stop (default: 10.0h)")
    parser.add_argument("--status", action="store_true", help="Show cache and duplicate statistics without scanning")
    parser.add_argument("--repeat", action="store_true", help="Keep running and scan once in each configured schedule window")
    parser.add_argument("--start-time", default=None, help="One-off daily window start in HH:MM format")
    parser.add_argument("--end-time", default=None, help="One-off daily window end in HH:MM format")
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
    args.output_json = set_cfg.get("output_json", "instituts_duplikate.json")
    args.output_csv = set_cfg.get("output_csv", "instituts_duplikate.csv")

    if not institutes:
        print("[!] No institutes configured.", file=sys.stderr)
        return 1
    if workers < 1:
        print("[!] settings.workers must be at least 1.", file=sys.stderr)
        return 1
    if (args.start_time is None) != (args.end_time is None):
        print("[!] --start-time and --end-time must be provided together.", file=sys.stderr)
        return 1
    if args.status and (args.repeat or args.start_time or args.end_time):
        print("[!] --status cannot be combined with repeat or time-window options.", file=sys.stderr)
        return 1

    if args.status:
        status_code, _ = print_status(
            institutes, year_range, solr_url, fuzzy=args.fuzzy
        )
        return status_code

    try:
        configured_windows = parse_schedule(cfg.get("schedule", {}).get("windows", []))
        windows = daily_window(args.start_time, args.end_time) if args.start_time else configured_windows
    except (AttributeError, ValueError) as exc:
        print(f"[!] Invalid schedule: {exc}", file=sys.stderr)
        return 1

    if args.repeat and not windows:
        print("[!] --repeat requires at least one schedule.windows entry in the config.", file=sys.stderr)
        return 1

    if args.repeat or args.start_time:
        if args.repeat:
            status_code, complete = print_status(
                institutes, year_range, solr_url, fuzzy=args.fuzzy
            )
            if status_code:
                return status_code
            if complete:
                print("All configured institutes are at 100%; repeat mode is stopping.")
                logger.info("All institutes already complete; repeat mode stopping")
                return 0
        while True:
            window_end = wait_for_window(windows)
            if datetime.datetime.now() >= window_end:
                continue
            seconds_left = max(1, (window_end - datetime.datetime.now()).total_seconds())
            max_seconds = args.max_hours * 3600 if args.max_hours > 0 else seconds_left
            result = run_analysis(
                institutes,
                year_range,
                solr_url,
                workers,
                args,
                min(max_seconds, seconds_left),
            )
            if not args.repeat:
                return result
            status_code, complete = print_status(
                institutes, year_range, solr_url, fuzzy=args.fuzzy
            )
            if status_code:
                return status_code
            if complete:
                print("All configured institutes are at 100%; repeat mode is stopping.")
                logger.info("All institutes complete; repeat mode stopping")
                return result
    return run_analysis(
        institutes,
        year_range,
        solr_url,
        workers,
        args,
        args.max_hours * 3600,
    )


if __name__ == "__main__":
    configure_logging()
    logger.info("Duplicate checker started")
    try:
        exit_code = main()
    except KeyboardInterrupt:
        logger.warning("Duplicate checker interrupted by user")
        exit_code = 130
    except Exception:
        logger.exception("Duplicate checker terminated unexpectedly")
        exit_code = 1
    logger.info("Duplicate checker exiting with code %d", exit_code)
    logging.shutdown()
    sys.exit(exit_code)