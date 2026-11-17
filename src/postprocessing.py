

from typing import Any

def postprocess_json_duplicates(json_data: list[dict[str, Any]], link_template: str) -> list[dict[str, Any]]:
    rows = []
    for entry in json_data:
        pids = entry.get("pids", [])
        row = []
        row.append(entry.get("match_key"))
        for pid in pids:
            institute, number = pid.split(":", 1)
            link = link_template.format(institute=institute, pid=number)
            row.append(pid)
            row.append(f'=HYPERLINK("{link}")')
        rows.append(row)
    return rows