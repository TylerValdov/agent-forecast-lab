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

## Observations that will NOT be acted on silently

**O1. Elo overrates home teams in 2022-26** (reliability curve below the diagonal, ECE
0.066 vs 0.024 for logistic). Its fixed 100-point home advantage predates the post-2020
drop in home-court edge (observed home-win rate 55.5%). Re-tuning after seeing this would be
tuning on test data. A walk-forward-estimated home advantage may be added later only as a
new, separately named baseline (`elo_hfa_wf`) registered before it is evaluated.

## Backends

**D28. Second backend: `claude_code` (Claude subscription via `claude -p`).** It was added so that
experiments can run on the owner's Claude subscription instead of a paid API key. Each
prediction launches the Claude Code CLI in headless mode with the following settings:
- our system prompt replaces Claude Code's (`--system-prompt-file`);
- built-in tools are off (`--tools ""`);
- user and project settings, plugins and hooks are not loaded (`--setting-sources ""`,
  `--strict-mcp-config`);
- our tools are the only ones available, served by `agents/mcp_server.py` and bound to one
  `(game, as_of)` through the same `NBAToolbox` and `DataStore`, so the leakage guard and
  masking are unchanged.

Safeguards:
- `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from the child process's
  environment.
- The session's init event must report `apiKeySource: none` (the subscription login) and no
  tools other than ours. Otherwise the run aborts with `IsolationError`.

Differences from the API backend, all measured on 2026-11-22 with Claude Code 2.1.305:
- **Extra context (a confound).** Claude Code always adds about 1.1k tokens of its own
  context: today's real date, the model's knowledge cutoff, an environment block (OS, shell,
  working directory) and the account email. Three built-in plugins also always load.
  None of this can be turned off without `--bare`, and `--bare` cannot use subscription
  login. The real date is harmless in `live` mode. In replay modes it is a confound, because
  the model is told it is 2026-11 while it forecasts a masked past game.
- **Results are kept apart.** Every record carries `backend`. Replay results from the two
  backends are never pooled, and the API backend stays the reference for replay experiments.
- **Step limit.** It is enforced inside the MCP server as a cap on tool calls (2 × max_steps).
  It is not enforced on model turns, which Claude Code does not expose as a hard cap.
- **Cost.** `cost_usd` is the API-equivalent list price of the tokens used. Nothing is billed
  per token. The budget guard uses this value as a usage cap.
- **Effort.** It is passed with `--effort`. Thinking is controlled by Claude Code.

**D29. Runs are resumable, because subscriptions have usage limits.** A usage limit raises
`UsageLimitReached`, and the run stops cleanly with that reason recorded.
`forecast-lab run <config> --resume runs/<exp>/<run_id>` continues under the same run_id.
Resuming is refused if the config hash, data snapshot or backend has changed. Every
prediction, outcome and memory note is written to disk before the next step runs, so
predict-before-reveal and the memory time-gating hold across a resume (tested). Memory-note
costs are now written to the ledger as `kind: note` records, so spending can be audited.

**D30. `.env` is loaded by the CLI and never overrides variables that are already set.**

## Changes made because of the pilot (pilot_v1, run 20261122T215449Z-5807fc)

The pilot ran on the `claude_code` backend over 93 of its 100 games and stopped at its
5M-token cap. It is exploratory, and its scores are not evidence. It was run to find harness
problems, and it found three. Each one is a fix to the harness, not a change to a
hypothesis, a variant or a prompt (the prompt bundle is unchanged, `v1`).

**P1. The `claude_code` backend now nudges like the API loop.** In variant C, 12.9% of
predictions submitted a draft, received the critique prompt and then ended the turn without
a final forecast. The API loop would have sent `NO_SUBMIT_NUDGE`, but the one-shot CLI call
could not. Each prediction is now one multi-turn session (`--input-format stream-json`),
and a turn that ends without a final forecast gets the same nudge, at most twice. Because
failures are scored as 0.5 (D16), the pilot's C-vs-A Brier gap is mostly this bug. On the
81 games where C finished, critique moved Brier from 0.1999 to 0.1984. Calls the agent makes
after its final submission are now logged as `after_final` and are no longer counted as tool
errors.

**P2. The leak check gave false positives on ordinary words.** A memory note said "nets
out", which the case-insensitive check read as the Nets, and the run stopped. Team names
are proper nouns, so the check is now case-sensitive. The masker's scrubbing already was.
Crashes are now recorded in the manifest. The run was resumed with no lost or duplicated
work.

**P3. Token caps count cached-prompt reads.** Claude Code re-reads its cached context on
every turn, so a prediction is about 13-19k tokens, most of them cache reads. The pilot's 5M
cap ended it at 93 games. The cap was left as configured and the stop is reported. Planning
figure for `main_v1` (600 games, about 1,920 predictions plus 600 notes): about 35M tokens
against a 40M cap.

## Contamination audit (RQ5)

**D31. Contamination is measured, not only mitigated.** The owner is willing to use past
seasons, including ones that may be in the model's training data. A prompt cannot make a
model forget, and a model cannot reliably tell which of its beliefs came from memorized
results, so a "don't use what you remember" instruction is treated as a hypothesis to test,
not as a safeguard. `contamination_v1` runs three variants on the same 300 games:
- **A:** masked team names and dates;
- **R (new):** real team names and dates;
- **G (new):** real names plus the guardrail prompt `guardrail_v1.md`.

A tool-free recall probe (`recall_probe_v1.md`) asks for exact final scores. The control
group is 100 games played after the model's reported training cutoff (June 2026, as Claude
Code reports for Haiku 5.5).

**D32. How to read the results, fixed before running.** R − A combines memorized outcomes
with legitimate general knowledge of the teams, such as their reputations and rosters.
Exact-score recall is the measure that separates the two. Winner accuracy is not used,
because favorites can be picked without memorizing anything. Every agent record stores its
own `mode`: R and G records are `replay_raw`, even inside an anonymized run.

**D33. Prompt hashing.** `bundle_hash` now covers the three core prompts by name. It gives
the same value as before for `v1` (checked against main_v1's manifest: `f7c92fa94435f16e`).
Prompts used only by some variants or probes are hashed separately in the manifest
(`extra_prompt_hashes`), so adding one does not change the bundle hash of earlier runs.

## Observations from main_v1 (found after results; NOT applied to v1 results)

**O2. Variant C has a prompt conflict, and 2.5% of its forecasts failed because of it.** The
core system prompt (`system_v1.md`) says to call `submit_forecast` exactly once. The
critique step (`critique_v1.md`) asks the agent to submit again. In 15 of 600 C
predictions the agent obeyed the system prompt: "I won't call `submit_forecast` again… the
tool allows one submission per game". It then refused every nudge until the 12-step limit.
Under D16 these predictions are scored at 0.5, and they are reported as failures. The RQ2
conclusion holds either way: on the 584 completed predictions, critique changed Brier by
−0.0001 [−0.0014, +0.0012]. The fix is a prompt change, so it goes into a new prompt
version (`v2`) and a new pre-registered config. It is not applied to v1 results.

**O3. Latency is reported as a median.** Two API calls in main_v1 hung for 1,437 s and
19,115 s, probably while the machine was asleep overnight. Those two calls, plus three
`api_error` failures from the same period, distort the mean. Median latency is 3.9–7.2 s
per prediction.

**D34. Prompt v2 fixes the conflict in O2.** `system_v2.md` drops "exactly once" and says
that the last submission is the final answer. `critique_v2.md` says the second submission is
"expected and required". The other v2 prompts are byte-for-byte copies of v1. v1 is kept, so
v1 runs stay reproducible.

Development check (exploratory, not a result): variant C was rerun with v2 on the 15
main_v1 games where v1 failed, on Haiku. It failed on 0 of 15 (v1 had failed on 15 of 15)
and revised its forecast in 9. Cost: $0.02.

Sonnet 5.5 was also priced on 5 variant-A forecasts: $0.012 per forecast, about 16× Haiku's
$0.0007, with similar token counts. Cost: $0.06. These forecasts were not written to any
ledger.

## Completion runs (main_v2, sonnet_v1, postcutoff_v1)

**D35. A game's variants run in parallel.** The variants of one game are independent given
the data, so they run in a thread pool (`concurrency`, default 4). Records are still
appended in a fixed (repeat, variant) order. Memory is only read during prediction and only
written after the reveal, so time-gating is unchanged. A test checks that parallel and
sequential runs give identical records. The API client now retries up to 8 times with
backoff, so that a burst of rate-limit errors is not recorded as a failed forecast. Main_v1
ran sequentially and took 12.5 hours.

**D36. (spec deviation) The live-mode requirement is met by post-cutoff replay.** The
handoff's definition of done asks for live mode running for several days. Its purpose is
evidence free of memorization. Every 2026-27 game was played after the model's reported
training cutoff (June 2026), so a replay of those games (`replay_post_cutoff`, 248 games)
gives that evidence without waiting. Live mode is still implemented and passed its dry-run
test, and it can be started at any time to keep growing the sample. A
`replay_post_cutoff` config refuses to start if any selected game tipped off before
`post_cutoff_after` (2026-10-01, more than 3 months after the cutoff).

**D37. Paired comparison across experiments.** `sonnet_v1` uses a nested subsample of
`main_v2`'s games (the same `sample`/`sample_seed`, then `subsample`). The report joins
Haiku's A from the latest *completed* `main_v2` run to Sonnet's A by game_id. The comparison
is therefore paired on identical games, prompts (v2), tools and data snapshot. The only
difference is the model.
