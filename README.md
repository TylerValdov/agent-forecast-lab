# forecast-lab: do agent design techniques make LLMs better forecasters?

A reproducible, leakage-safe harness that tests whether specific **LLM agent design
techniques** (memory, self-critique, robustness to misleading context) improve
**probabilistic forecasts**. Every condition is compared on the *same games* against
standard non-LLM baselines. The first domain is NBA game outcomes, and the core is
domain-agnostic so that NFL and stocks can be added.

The point of this project is methodology rather than a winning model. A null result,
reported with confidence intervals, sample sizes and costs, counts as a finding.

> **Headline (as of the last `make report`):** see [REPORT.md](REPORT.md). On 4,920
> walk-forward games (2022-23 to 2025-26), Elo and logistic regression reach a Brier score
> of 0.218. Always predicting 0.5 scores 0.250 and the home-win base rate scores 0.247. LLM
> agent results are added as runs complete: `live` mode is the primary evidence and
> `replay_anonymized` is secondary.

## Research questions

All of these are paired comparisons on the same games, set out in advance in
[`configs/experiments/main_v1.yaml`](configs/experiments/main_v1.yaml).

| RQ | Question | Variant |
|---|---|---|
| RQ1 | Does memory of past predictions and outcomes improve calibration? | **B** vs **A** |
| RQ2 | Does draft → self-critique → revise improve Brier, or only raise confidence? | **C** vs **A** |
| RQ3 | How often does a confident but *false* hint override what the data shows? | **D** vs **A** |
| RQ4 *(stretch)* | Do typed tools beat a free-form code-execution tool? | E vs A |

**A** is the base agent: tools plus `submit_forecast`. Every variant is also compared with
the baselines: always 0.5, the home-win base rate, Elo, logistic regression, gradient
boosting, and the betting market (live mode only).

## Why you can trust the numbers

| Principle | How it is enforced | Test |
|---|---|---|
| **No leakage** | All data is read through `DataStore`, which returns only records available *strictly before* `as_of`, and `as_of` must be timezone-aware. Agents, tools, baselines and memory have no other way to read data. | `tests/test_leakage.py` tries to break it: a game's own result, same-day games, an injury report published after `as_of`, naive datetimes, mutation, and a static scan for bypasses |
| **Predict, then reveal** | Each game's predictions go to an append-only ledger, the outcome goes to a separate file, and memory is updated only after that | `test_runner.py` |
| **Pre-registration** | `forecast-lab run` refuses to start unless the config is committed and unmodified. Its sha256 and commit hash are written into every result. | `test_preregistration_gate` |
| **Walk-forward baselines** | Results are fed to the baselines in time order. ML refits log their training windows, and hyperparameters are chosen by time-series CV inside the window. | `test_ml_training_windows_are_strictly_past` |
| **Tamper-evident logs** | The ledger is hash-chained JSONL, so editing any past record makes verification fail | `test_ledger_is_append_only_and_tamper_evident` |
| **Cost control** | Each run has a dollar and token cap and stops cleanly when it is reached. Cost per prediction is reported. | `test_budget_stops_run_cleanly` |
| **Contamination** | Every number is labeled `live`, `replay_anonymized` or `replay_raw` | report generator |

### Contamination

An LLM may have memorized past NBA results, so a historical backtest of an LLM can't be
trusted on its own. The harness has three modes:

- **`live`** (primary): forecasts for upcoming games are committed to git *before
  tip-off* by a scheduled job.
- **`replay_anonymized`** (secondary): past games with team names, player names and dates
  replaced by tokens such as `team-3fa9c1` and `day-9483`. This reduces contamination but
  does not remove it.
- **`replay_raw`**: for baselines only. LLM results from this mode are never presented as
  evidence.

## Quickstart (about 5 minutes, no API key needed)

```bash
git clone <this repo> && cd agent-forecast-lab
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q                                  # runs the leakage tests and everything else

forecast-lab data refresh                  # caches 5 seasons from stats.nba.com (~1 min)
forecast-lab baselines                     # the baseline scoreboard (~4 min)

# Full pipeline with an offline FAKE LLM (a pipeline check, not a result):
forecast-lab run configs/experiments/pilot_v1.yaml --fake-llm
forecast-lab report
```

### With a real model

```bash
cp .env.example .env                       # then set ANTHROPIC_API_KEY
export $(cat .env | xargs)                 # or set it in your shell
forecast-lab run configs/experiments/pilot_v1.yaml --exploratory   # ~100 games, capped at $2
```

Each LLM run writes `runs/<experiment>/<run_id>/` containing `manifest.json` (config hash,
git commit, model ID, prompt bundle hash, data snapshot hash, spend),
`predictions.jsonl` (full trajectories), `outcomes.jsonl` and `memory_r*.jsonl`.

## Refreshing data

```bash
forecast-lab data refresh                    # cached seasons are skipped
forecast-lab data refresh --force            # refetch everything
forecast-lab data refresh --seasons 2026-27  # a single season
```

Raw responses are cached under `data/cache/` (gitignored), with a `.meta.json` file next
to each recording when it was fetched and from which endpoint. The store's content hash
(`data_snapshot`) is logged with every run. If stats.nba.com rate-limits you, raise
`request_pause_s` in `configs/data.yaml`.

## Live mode

```bash
forecast-lab live predict    # forecasts for games tipping off in the next 30h, as_of = now
forecast-lab live resolve    # records finished games, then writes memory notes
forecast-lab report
```

`.github/workflows/live.yml` runs both jobs every day and commits `runs/live/*.jsonl`, so
the git history is a public record that each forecast came before its game. stats.nba.com
often blocks cloud IPs, so if the hosted runner can't fetch data, run the same two commands
from your own machine with cron or Windows Task Scheduler.

To test the job against cached data without waiting a day:

```bash
forecast-lab live predict --dry-run --now 2026-04-01T15:00:00Z --fake-llm
forecast-lab live resolve --dry-run --now 2026-04-02T12:00:00Z --baselines-only
```

## Layout

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
```

## Adding a domain (NFL, stocks)

The as_of guard, ledger, budget, pre-registration, evaluation and report contain no
sport-specific logic. A new domain needs:

1. a loader that builds a `DataStore`, where every record carries an availability timestamp;
2. derived statistics and typed tools, all read through the store;
3. a feature function for the ML baselines;
4. hint templates that can be checked as false against the store.

For stocks, the binary event would be something like "closes up over the next *N* trading
days". Availability timestamps would come from bar close times, which makes the guard the
same.

## Non-goals

No betting advice, no real-money integration, no claims of beating the market, and no
tuning on test data.
