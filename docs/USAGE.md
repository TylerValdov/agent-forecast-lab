# Usage and engineering notes

This file holds everything that is not part of the research story in the [README](../README.md): how to install and run the harness, how the leakage and pre-registration safeguards work, how the code is laid out, and how to add a new domain. Full numeric results are in [REPORT.md](../REPORT.md), and every design decision is in [DECISIONS.md](DECISIONS.md).

## Why you can trust the numbers

| Principle | How it is enforced | Test |
|---|---|---|
| **No leakage** | All data is read through `DataStore`, which returns only records available *strictly before* `as_of`, and `as_of` must be timezone-aware. Agents, tools, baselines and memory have no other way to read data. | `tests/test_leakage.py` tries to break it: a game's own result, same-day games, an injury report published after `as_of`, naive datetimes, mutation, and a static scan for bypasses |
| **Predict, then reveal** | Each game's predictions go to an append-only ledger, the outcome goes to a separate file, and memory is updated only after that | `test_runner.py` |
| **Pre-registration** | `forecast-lab run` refuses to start unless the config is committed and unmodified. Its sha256 and commit hash are written into every result. | `test_preregistration_gate` |
| **Walk-forward baselines** | Results are fed to the baselines in time order. ML refits log their training windows, and hyperparameters are chosen by time-series CV inside the window. | `test_ml_training_windows_are_strictly_past` |
| **Tamper-evident logs** | The ledger is hash-chained JSONL, so editing any past record makes verification fail | `test_ledger_is_append_only_and_tamper_evident` |
| **Cost control** | Each run has a dollar and token cap and stops cleanly when it is reached. Cost per prediction is reported. | `test_budget_stops_run_cleanly` |
| **Contamination** | Every number is labeled `live`, `replay_anonymized`, `replay_post_cutoff` or `replay_raw` | report generator |

### Contamination modes

An LLM may have memorized past NBA results, so a historical backtest of an LLM can't be trusted on its own. The harness has these modes:

- **`live`**: forecasts for upcoming games are committed to git *before tip-off* by a scheduled job. Clean by construction.
- **`replay_post_cutoff`**: past games that tipped off after the model's training cutoff. Clean by construction, and real team names are safe to show.
- **`replay_anonymized`**: past games with team names, player names and dates replaced by tokens such as `team-3fa9c1` and `day-9483`. This reduces contamination but does not remove it.
- **`replay_raw`**: real names on possibly-seen games. Used for baselines, and for the contamination test variants R and G. LLM results from this mode are never presented as evidence on their own.

## Quickstart (no API key needed)

Timings below were measured from a fresh clone on a home connection.

```bash
git clone https://github.com/TylerValdov/agent-forecast-lab && cd agent-forecast-lab
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                    # ~3 min
pytest -q                                  # 61 tests incl. the adversarial leakage tests, ~30 s

forecast-lab data refresh                  # caches 6 seasons from stats.nba.com, ~40 s
forecast-lab baselines --no-ml             # Elo / home-rate / coin-flip scoreboard, ~45 s
#   (drop --no-ml to add the walk-forward logistic + GBM models, ~4 min)

# Full pipeline with an offline FAKE LLM: checks the plumbing, produces no results.
forecast-lab run configs/experiments/pilot_v1.yaml --fake-llm   # ~5 s, writes runs_fake/
forecast-lab report runs_fake/pilot_v1/*/    # REPORT.md for that fake run (don't commit it)
```

A fresh clone rebuilds exactly the data used in the reported results. The historical snapshot hash `dd9e4a62ab14bbcf` printed by `data refresh` / recorded in each run's `manifest.json` matches, and Elo's Brier (0.2184) reproduces to four decimals.

### With a real model

```bash
cp .env.example .env                       # then set ANTHROPIC_API_KEY
export $(cat .env | xargs)                 # or set it in your shell
forecast-lab run configs/experiments/pilot_v1.yaml --exploratory   # ~100 games, capped at $2
```

### With your Claude subscription (no API key)

The `claude_code` backend runs the same agent through the Claude Code CLI (`claude -p`), which uses your Claude subscription and its usage limits. Log in once with `claude`, then:

```bash
forecast-lab run configs/experiments/pilot_v1.yaml --backend claude_code --exploratory
# if it stops at a usage limit, continue later under the same run id:
forecast-lab run configs/experiments/pilot_v1.yaml --backend claude_code --exploratory \
    --resume runs/pilot_v1/<run_id>
```

Your API key is stripped from the CLI's environment, and the run aborts if Claude Code reports anything other than subscription login, or any tool besides ours. Claude Code adds about 1.1k tokens of its own context, including today's date. That is fine for `live` mode, but it is a confound for replay, so results record their `backend` and the two backends are never pooled (see [DECISIONS](DECISIONS.md) D28).

Each LLM run writes `runs/<experiment>/<run_id>/` containing `manifest.json` (config hash, git commit, model ID, prompt bundle hash, data snapshot hash, spend), `predictions.jsonl` (full trajectories), `outcomes.jsonl` and `memory_r*.jsonl`.

## Refreshing data

```bash
forecast-lab data refresh                    # cached seasons are skipped
forecast-lab data refresh --force            # refetch everything
forecast-lab data refresh --seasons 2026-27  # a single season
```

Raw responses are cached under `data/cache/` (gitignored), with a `.meta.json` file next to each recording when it was fetched and from which endpoint. The store's content hash (`data_snapshot`) is logged with every run. If stats.nba.com rate-limits you, raise `request_pause_s` in `configs/data.yaml`.

## Live mode

```bash
forecast-lab live predict    # forecasts for games tipping off in the next 30h, as_of = now
forecast-lab live resolve    # records finished games, then writes memory notes
forecast-lab report
```

`.github/workflows/live.yml` runs both jobs every day and commits `runs/live/*.jsonl`, so the git history is a public record that each forecast came before its game. stats.nba.com often blocks cloud IPs, so if the hosted runner can't fetch data, run the same two commands from your own machine with cron or Windows Task Scheduler.

To test the job against cached data without waiting a day:

```bash
forecast-lab live predict --dry-run --now 2026-04-01T15:00:00Z --fake-llm
forecast-lab live resolve --dry-run --now 2026-04-02T12:00:00Z --baselines-only
```

## Code layout

```
src/forecast_lab/
  data/       schema, DataStore (the as_of guard), NBA loader + cache, Masker, derived stats
  baselines/  always-0.5, home rate, Elo, logistic/GBM (walk-forward), market adapter
  tools/      typed agent tools; each one is bound to (game, as_of) and passes through the Masker
  agents/     tool-use loop, versioned prompts, memory, hint generation, LLM client, fakes
  eval/       Brier, log loss, ECE, decomposition, paired bootstrap, Holm/BH, scoreboard
  runner/     configs, pre-registration gate, budget guard, replay + live runners
  ledger/     append-only hash-chained JSONL
  report/     REPORT.md + figures
configs/experiments/   pre-registered experiment configs
docs/DECISIONS.md      every design decision and departure from the spec, with reasons
docs/USAGE.md         this file
```

## Adding a domain (NFL, stocks)

The as_of guard, ledger, budget, pre-registration, evaluation and report contain no sport-specific logic. A new domain needs:

1. a loader that builds a `DataStore`, where every record carries an availability timestamp;
2. derived statistics and typed tools, all read through the store;
3. a feature function for the ML baselines;
4. hint templates that can be checked as false against the store.

For stocks, the binary event would be something like "closes up over the next *N* trading days". Availability timestamps would come from bar close times, which makes the guard the same.

## Non-goals

No betting advice, no real-money integration, no claims of beating the market, and no tuning on test data.
