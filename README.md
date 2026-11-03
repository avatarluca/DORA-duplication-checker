# DORA DeDup
## Usage
Copy `config.example.toml` to `config.toml` and edit it to your needs.
Also fill out the vals in `src/example.config.py` and rename it to `config.py`.
## Cmds
`python run.py --fuzzy --delay 5 --max-hours 10`

Runs run_dedup with fuzzy matching (step 2), 5s pause between requests (rate limiting) and max 10 hours of runtime.
