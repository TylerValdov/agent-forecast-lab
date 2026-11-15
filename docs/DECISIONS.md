# Decisions

Each entry records what was chosen, why, and what it costs. Entries marked **(spec
deviation)** depart from `HANDOFF.md`; the handoff's own rule is "choose correctness and
note it here".

## Scope

**D1. NBA first, domain-agnostic core.** The handoff default is NBA, with stocks as a stretch goal.
The owner also wants NFL and stocks later. The parts that make the harness trustworthy
(`DataStore` as_of guard, ledger, budget, pre-registration, evaluation, report) contain no
sport logic. Sport-specific code is confined to `data/nba_loader.py`, `data/derived.py`,
`tools/nba_tools.py`, `baselines/features.py` and the hint templates. Adding a domain means
writing those five pieces for it. See README, "Adding a domain".

## Data

**D2. Source: `nba_api` → stats.nba.com.** `ScheduleLeagueV2` supplies exact UTC tip-off
times and final scores. `LeagueGameFinder` supplies box scores, which give possessions and
therefore net rating. Both were verified to work from a residential connection on 2026-11-22.
The NBA CDN schedule JSON returned HTTP 403 to plain requests, so it is not used.
**Risk:** stats.nba.com is known to block cloud IPs, including GitHub-hosted runners. The
live workflow may therefore need a self-hosted runner or a local cron job (README).

**D3. When a result becomes knowable: tip-off + 210 minutes.** Historical data has tip-off
times but no final-whistle times. Real games take about 2h15m, or about 2h45m with
overtime. 210 minutes is deliberately conservative: a slightly late availability time can
only hide information, never leak it. The value is configurable in `configs/data.yaml`.

**D4. The as_of guard is a strict `<`.** A record whose availability time equals `as_of`
is hidden. The guard applies to results, box scores, injury reports and memory notes.
`as_of` must be a timezone-aware `datetime`. Every timestamp in the schema is `AwareDatetime`,
so naive datetimes are rejected when a record is built.

**D5. Schedule data needs no as_of.** Who plays whom, and when, is published before the
season and contains no outcomes. `ScheduledGame` has no score fields at all.

**D6. Injury data is disabled in the replay modes (gap).** Historical, timestamped,
machine-readable injury reports were not available. The official reports are PDFs with
inconsistent historical coverage. The injury tool is implemented against `InjurySnapshot`
and has leakage tests, but it is hidden from agents whenever the store has no injury data.
Parsing the live official PDFs is listed as future work.

**D7. Data is never committed.** The raw cache (`data/`, about 2.5 MB per 6 seasons) is
gitignored and rebuilt with `forecast-lab data refresh`. Each run records the store's
content hash (`data_snapshot`), so a run can be checked against a rebuilt cache.

## Baselines

**D8. Elo hyperparameters come from FiveThirtyEight's published NBA Elo** (K=20, home
advantage 100, 25% regression to 1505 between seasons, their MOV multiplier). They were set
before seeing this data and were not tuned, so no test data informed them.

**D9. ML baselines: logistic regression and sklearn `HistGradientBoostingClassifier`.**
GBM was chosen over XGBoost or LightGBM to avoid an extra compiled dependency. Both models
are refit every 14 days. Hyperparameters are chosen at each refit by `TimeSeriesSplit` CV
on the training window only. Each fit is logged (`fit_log`) and tested so that no training
label is from the prediction time or later.

**D10. ML models abstain until 400 games have been seen.** During warm-up they return
`None` instead of a placeholder. Scoreboards only use games where every compared model made
a forecast. The baseline evaluation starts on 2022-10-01, so 2021-22 is warm-up.

**D11. Market baseline: missing in replay, not approximated.** No free historical odds
source was available. Live mode uses The Odds API through an optional key. The median of
the bookmakers' vig-free two-way probabilities is used, with multiplicative normalization.

## Agents

**D12. Model IDs: `claude-haiku-5-5` by default, `claude-sonnet-5-5` for headline runs.**
This follows the handoff. Every record logs the model ID.

**D13. (spec deviation) No temperature parameter.** Current Claude models reject
non-default `temperature`, `top_p` and `top_k` with an HTTP 400. The config records
`effort` (default `low`) instead. Variance is estimated with `repeats: N`, which runs each
(game, variant) N times independently. `seed` controls the harness's own randomness: game
sampling, hint selection and the masking salt. It does not control the model's sampling.

**D14. `submit_forecast` is required by the prompt, not by forcing the tool.** Forcing a
specific tool call is rejected on some current models. The system prompt says to answer
only through `submit_forecast`, and all tool schemas use `strict: true`. If the agent never
submits, the failure is recorded (`step_limit`, `token_limit`, `refusal`, `api_error`,
`max_tokens_truncated`). It is never silently given a default value.

**D15. Forecasts outside [0.05, 0.95] are rejected, not clipped.** The agent gets a tool
error and can resubmit within its step budget. Invalid submissions are counted.

**D16. Scoring failures uses the sentinel 0.5 ("intent to treat").** A failed prediction is
scored as 0.5 in headline numbers, so a variant cannot look better by failing on hard
games. Failure rates are reported next to the scores.

**D17. Refusal fallbacks are off.** The API's server-side refusal fallback would quietly
answer with a different model, which would contaminate a model comparison. A refusal is
logged as a failure instead.

**D18. The conversation history is append-only.** Assistant content blocks, including
thinking blocks, are passed back unchanged, and earlier messages are never edited. The
self-critique step is a tool result on the draft submission, not an edit to an earlier
turn. This keeps current models' thinking-block integrity checks satisfied.

**D19. (spec deviation) The base prompt contains no "be skeptical of claims" line.** An
early draft told the agent to verify claims that come from outside the tools. That line
would have pre-empted RQ3: we would be measuring an instruction, not the agent's default
robustness. A skeptical-prompt variant can be added later as a new, separately registered
condition.

**D20. The false hint argues against the team favored by the data.** "Favored" means the
better last-10 margin, with 1.5 points of home edge. The hint templates are a losing streak,
a back-to-back and head-to-head dominance. Each one is checked as false against the store
at `as_of` when it is generated, and checked again independently (`verify_false`). If no
template can be shown false for a game, that game is logged as a `skip` for D and is never
given an unverified hint.

**D21. Variant D runs only on the hint subset.** Its paired control is variant A on the
same games, which runs on every game anyway, so running D on the remaining games would add
cost and no information.

**D22. Memory notes are written by a separate, tool-free LLM call after the outcome is
revealed.** Each note is capped at 80 words. The last `k=10` notes created before `as_of`
are loaded. In replay, a note's `created_at` is the result's availability time, so a note
about an earlier game the same evening is correctly invisible to a later game whose
`as_of` comes before that result. Note costs count toward the budget and are reported.

**D23. In anonymized mode, a leaking prompt is not sent.** If the task text contains any
real team name, `predict()` raises an error instead of sending it.

## Process

**D24. `ledger` replaces the handoff's `logging/` package name** **(spec deviation,
cosmetic).** It avoids confusion with the standard-library `logging` module. The ledger is
hash-chained JSONL, so editing any past record makes `verify()` fail.

**D25. Pre-registration means the config is committed.** `forecast-lab run` checks that the
config file is tracked and unmodified in git, then records its sha256 and the commit hash.
`--exploratory` skips the check, and the run is then labeled exploratory everywhere it is
reported. Fake-LLM runs are always exploratory and are written to `runs_fake/`.

**D26. Dependencies are pinned exactly in `pyproject.toml` and installed with pip.** `uv`
was not available in the build environment.

**D27. Prices are hard-coded in `agents/llm.py`.** They use Anthropic list prices as of
2026-11: Haiku 5.5 at $0.10/$0.50 per million input/output tokens, Sonnet 5.5 at $2/$10 and
Opus 5.5 at $4/$20. A model without a price entry refuses to run, so costs cannot go
unrecorded.
