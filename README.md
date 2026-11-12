# DORA DeDup

Edit `config.toml` to select institutes and configure the Solr endpoint.

## Commands

Show how much of the current Solr result is cached without scanning PDFs:

```powershell
python run.py --status
```

Run one scan (cached publications are skipped):

```powershell
python run.py --fuzzy --delay 5 --max-hours 10
```

To keep the process running and scan during configured weekly windows, add one
or more `[[schedule.windows]]` entries to `config.toml` and explicitly start
repeat mode:

```powershell
python run.py --repeat --fuzzy --delay 5 --max-hours 10
```

For example, scan Saturday and Sunday during the day, plus Monday night through
Tuesday morning:

```toml
[[schedule.windows]]
days = ["sat", "sun"]
start = "08:00"
end = "18:00"

[[schedule.windows]]
days = ["mon"]
start = "20:00"
end = "06:00"
```

The status also reports exact SHA256 duplicate groups (intra-/cross-PID).
Add `--fuzzy` to include fuzzy-text duplicate groups; this can take longer on a
large cache. Status is based on the PIDs returned by Solr at the time of the
query.
