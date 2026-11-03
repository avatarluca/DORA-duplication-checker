# DORA DeDup
## Usage
Copy `config.example.toml` to `config.toml` and edit it to your needs.
Also fill out the vals in `src/example.config.py` and rename it to `config.py`.
## Cmds
`python run.py --fuzzy --delay 5 --max-hours 10`

Runs with fuzzy matching (step 2), 5s pause between requests (rate limiting) and max 10 hours of runtime.


`python run.py --fuzzy --delay 5 --start-time "20:00" --end-time "06:00"`

Runs with fuzzy matching (step 2), 5s pause between requests (rate limiting) and only during the time window from 20:00 to 06:00 (overnight).
