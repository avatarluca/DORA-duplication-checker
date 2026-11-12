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

As an alternative, install a Windows Task Scheduler entry from the same
`schedule.windows` settings:

```powershell
python run.py --install-task --fuzzy --delay 5 --max-hours 10
```

This command explicitly creates the task `DORA-Duplicate-Checker`; it is not
installed unless you run this command yourself. The task runs as your Windows
user, uses the current Python executable and project folder, and reads the
same TOML schedule. Windows is allowed to wake the PC from standby for a
scheduled window, but missed starts are not run later if the PC was shut down.
If you change the configured windows, run `--install-task` again to update the
Windows triggers.
Remove the task explicitly with:

```powershell
python run.py --remove-task
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