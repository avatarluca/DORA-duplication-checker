# DORA DeDup

## Commands

```powershell
python run.py --status
```
```powershell
python run.py --fuzzy --delay 5 --max-hours 10
```

```powershell
python run.py --repeat --fuzzy --delay 5 --max-hours 10
```

```powershell
python run.py --install-task --fuzzy --delay 5 --max-hours 10
```

```powershell
python run.py --remove-task
```

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