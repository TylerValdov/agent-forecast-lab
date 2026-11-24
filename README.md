# forecast-lab: do agent design techniques make LLMs better forecasters?

A reproducible, leakage-safe harness that tests whether specific **LLM agent design
techniques** (memory, self-critique, robustness to misleading context) improve
**probabilistic forecasts**. Every condition is compared on the *same games* against
standard non-LLM baselines. The first domain is NBA game outcomes, and the core is
domain-agnostic so that NFL and stocks can be added.

The point of this project is methodology rather than a winning model. A null result,
reported with confidence intervals, sample sizes and costs, counts as a finding.

## Headline findings

**Primary experiment** ([`main_v2`](configs/experiments/main_v2.yaml), pre-registered): Claude Haiku 5.5, `replay_anonymized`, 1,000 games from Nov 2024 to Apr 2026. All results are paired on the same games and scored with Brier (lower is better). Intervals are 95% paired-bootstrap CIs, and p-values are Holm-corrected across 5 comparisons. Full tables are in [REPORT.md](REPORT.md).

| Question | Result | Verdict |
|---|---|---|
| Agent vs baselines | Agent A 0.2059 · Elo 0.2066 · logistic 0.2067 · GBM 0.2087 · coin flip 0.2500 | The LLM agent with simple stat tools **ties the classical models**. A − logistic is −0.0008 [−0.0043, +0.0026]. |
| RQ1 memory (B − A) | +0.0019 [−0.0007, +0.0045], p_holm 0.71 | **No benefit.** In the post-cutoff test (below), memory makes forecasts **significantly worse**. |
| RQ2 self-critique (C − A) | +0.0011 [−0.0009, +0.0030]; draft → final +0.0007 | **No benefit.** Critique revises 72% of forecasts and makes them less confident, with no accuracy gain. |
| RQ3 false hint (D − A, 300 games) | Shifts forecasts toward the hint by +0.014; flips the predicted winner in 5.0% of games; Brier +0.0032 [−0.0023, +0.0088] | **Mostly robust.** The false "back-to-back" hint is the most persuasive (+0.032). |
| Model size: Sonnet 5.5 − Haiku 5.5 (400-game nested subsample) | −0.0023 [−0.0070, +0.0025], p_holm 0.69 | **Not significantly better** at about 17× the cost per forecast ($0.012 vs $0.0007). |

**Contamination-free replication** ([`postcutoff_v1`](configs/experiments/postcutoff_v1.yaml), pre-registered): 248 games from the 2026-27 season. All tip off after the model's training cutoff, so real team names are safe to show.

| Question | Result |
|---|---|
| RQ1 memory (B − A) | **+0.0082 [+0.0021, +0.0141], p_holm 0.03.** Memory significantly hurts. This is the only comparison across all experiments that survives correction. |
| RQ2 self-critique (C − A) | +0.0008 [−0.0034, +0.0048], no effect again |
| Agent A vs Elo / logistic | +0.0009 / +0.0117, both not significant |

*Why memory hurts (exploratory; this was not pre-registered):* 62% of the post-cutoff memory notes name a specific team, compared with 0% in masked replay. Consecutive notes also give opposite advice ("shrink toward the base rate" vs "trust the form gap"). The agent appears to learn from single-game noise. See [DECISIONS](docs/DECISIONS.md) O5.

**Contamination audit (RQ5):** a no-tools recall probe asked for exact final scores.
- **Haiku:** across two runs, 2 of 300 experiment-era games recalled exactly vs 0 of 100 post-cutoff controls in each run.
- **Sonnet:** 1.3% vs 1.0% for the controls.
- **Real names vs masked:** +0.001 [−0.004, +0.006]. An "ignore what you remember" guardrail adds +0.002 [−0.001, +0.006].

There is **no meaningful evidence of memorized results**, and the guardrail does nothing measurable.

**Bottom line:** memory, self-critique and a bigger model were each tested against a plain tool-using agent on the same games. None of them improved its NBA forecasts, and memory made them worse. The plain agent is about as good as a logistic regression on Elo and rest features. A null result is still a result.

Total cost: **$12.17** in API spend for the 5 reported experiments, covering 7,338 forecasts, plus two small runs on a Claude subscription. Every ledger is in the release zip (`runs.zip`) and can be re-verified.

## Research questions

All of these are paired comparisons on the same games, set out in advance in
[`configs/experiments/main_v2.yaml`](configs/experiments/main_v2.yaml) (main_v1 was the earlier, smaller run).

| RQ | Question | Variant |
|---|---|---|
| RQ1 | Does memory of past predictions and outcomes improve calibration? | **B** vs **A** |
| RQ2 | Does draft → self-critique → revise improve Brier, or only raise confidence? | **C** vs **A** |
| RQ3 | How often does a confident but *false* hint override what the data shows? | **D** vs **A** |
| RQ4 *(stretch)* | Do typed tools beat a free-form code-execution tool? | E vs A |
| RQ5 | Do forecasts on past games lean on memorized outcomes, and does telling the agent to ignore them help? | **R** (real names) vs **A** (masked); **G** (R + guardrail) vs **R**; plus a no-tools recall probe |

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

A fresh clone rebuilds exactly the data used in the reported results. The historical
snapshot hash `dd9e4a62ab14bbcf` printed by `data refresh` / recorded in each run's
`manifest.json` matches, and Elo's Brier (0.2184) reproduces to four decimals.

### With a real model

```bash
cp .env.example .env                       # then set ANTHROPIC_API_KEY
export $(cat .env | xargs)                 # or set it in your shell
forecast-lab run configs/experiments/pilot_v1.yaml --exploratory   # ~100 games, capped at $2
```

### With your Claude subscription (no API key)

The `claude_code` backend runs the same agent through the Claude Code CLI (`claude -p`),
which uses your Claude subscription and its usage limits. Log in once with `claude`, then:

```bash
forecast-lab run configs/experiments/pilot_v1.yaml --backend claude_code --exploratory
# if it stops at a usage limit, continue later under the same run id:
forecast-lab run configs/experiments/pilot_v1.yaml --backend claude_code --exploratory     --resume runs/pilot_v1/<run_id>
```

Your API key is stripped from the CLI's environment, and the run aborts if Claude Code
reports anything other than subscription login, or any tool besides ours. Claude Code adds
about 1.1k tokens of its own context, including today's date. That is fine for `live`
mode, but it is a confound for replay, so results record their `backend` and the two
backends are never pooled (see [DECISIONS](docs/DECISIONS.md) D28).

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
