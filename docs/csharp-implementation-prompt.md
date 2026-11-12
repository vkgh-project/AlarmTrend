# Ready-to-paste prompt: build AlarmTrend in C# from scratch

Copy the prompt below into an AI coding chat. It is self-contained and does not require access to this Python project.

---

## Prompt

Act as a senior business analyst, software architect, statistician, security engineer, and C#/.NET developer. Build a complete, production-quality **AlarmTrend** application in C# from scratch, using only this prompt as the product and technical specification. This prompt is self-contained. Do not inspect, read, copy, port, quote, infer requirements from, or depend on any existing Python implementation, source files, documentation, tests, templates, database, or generated artifacts. Do not modify or delete any pre-existing application. Create the new solution in a separate clean directory named `AlarmTrend.DotNet/`.

### 1. Implementation approach

Treat the requirements below as authoritative and complete. If a low-level implementation choice is not specified, choose a safe, maintainable default and document it; do not search for legacy code or stop to ask questions that can be resolved with reasonable assumptions. Begin with a concise architecture and milestone plan, then implement the entire application without waiting for approval. Do not stop after analysis, scaffolding, or a plan.

### 2. Product and business requirements

AlarmTrend is a Ukrainian-language, mobile-first dashboard for Kyiv air-raid alert status and historical statistical estimates. Its only automatic data source is the public HTML history at:

`https://kyiv.digital/storage/air-alert/stats.html`

The page contains a full-history HTML table, newest rows first. Each event row has a local timestamp in `HH:MM DD.MM.YY` format, a type (`Повітряна тривога` for start or `Відбій` for end), and sometimes a duration such as hours/minutes. It is a full snapshot, not an incremental API. The configured analysis start date defaults to **2026-09-01**. Users need a quick current status, transparent uncertainty, historical trends, manual correction, and explainable probabilistic forecasts. Forecasts are statistical estimates and must never be described as safety instructions or guarantees. Preserve the warning: **“Статистична оцінка. Орієнтуйтеся на офіційні оповіщення.”**

The new application must provide these user-facing capabilities:

1. **Dashboard `/`**: current state (`active`, `clear`, or `unknown`), last successful source check, whether data/status is stale or errored, probability of a new alert within 15/30/60 minutes while currently clear, expected number of new alerts through Kyiv midnight with an 80% interval, recent alert-duration estimate, and—while an alert is active—conditional remaining-time median and 50% interval plus chance it remains active for another 60 minutes. Mark source/status/forecast stale when its successful observation is older than twice the configured 300-second interval. Show trends for alert starts and alert minutes per day over 14, 7, and 3 days versus the corresponding preceding periods; label incomplete periods rather than treating them as zero.
2. **Calculation page `/calculation`**: explain the models and show the actual latest evaluation, selected model for each task, training period/counts, model/artifact identifiers, and training-queue status. Do not fabricate metrics when no evaluation exists.
3. **Eight-day timetable `/timetable`**: eight Kyiv-local dates horizontally (today and the previous seven calendar dates), clock hours vertically, with visually distinct confirmed alert periods, confirmed no-alert periods, and unknown/uncovered/ambiguous periods. Keep actual event timing, including transitions within an hour. Include a legend and accessible labels/tooltips. The dashboard’s “Перевірено:” line links to this page.
4. **Manual admin workflow**: login, logout, add an alert that began now, add a start/end at a supplied timestamp, correct a prior manual record, cancel a manual record, and review recent manual entries and whether they were reconciled with source data. Never require admin configuration for the public dashboard; if admin secrets are absent, admin pages return an explanatory unavailable response.
5. **Status API `/api/status`**: expose the same stored status/forecast state as the dashboard without triggering sync, fitting, or inference.
6. **Windows 11 desktop launch**: provide a reliable `win-x64` self-contained publish/build and a double-click launcher that binds only to loopback (`127.0.0.1`), opens the dashboard in the default browser, and keeps its writable data under `%LOCALAPPDATA%\AlarmTrend\data`. Use a browser-based dashboard; do not substitute an unrelated native UI.

### 3. Technology and solution architecture

Use **C# with ASP.NET Core Razor Pages on .NET 10 LTS** by default. Confirm the installed SDK and current Microsoft support policy before selecting the target framework; do not select a preview. .NET 10 is the preferred LTS target; use a different supported target only if the actual deployment requirement demands it and explain why.

Create a cleanly separated solution, for example:

- `src/AlarmTrend.Core`: domain types, time/event rules, forecast contracts, interfaces
- `src/AlarmTrend.Infrastructure`: SQLite repositories/migrations, HTTP source client/parser, model-artifact storage, locking
- `src/AlarmTrend.Web`: ASP.NET Core Razor Pages and read-only status/API endpoints
- `src/AlarmTrend.Worker`: CLI commands and background synchronization, inference, evaluation, and training
- optional `src/AlarmTrend.Desktop`: a small Windows launcher/orchestrator if it materially simplifies one-click desktop startup
- `tests/`: unit, integration, migration, and web tests

Use dependency injection and options/configuration binding. Keep request handlers thin. Use `Microsoft.Data.Sqlite` (or Dapper on top of it) where explicit SQL best supports the new schema, triggers, transactions, and concurrency rules. Use a maintained IANA time-zone implementation such as NodaTime, or prove that the selected .NET APIs preserve every Europe/Kyiv DST rule on Windows and Linux. Select maintained, compatible NuGet packages for HTML parsing and numerical optimization; verify versions and licenses rather than guessing. Keep the model math in C#; do not shell out to Python or call a hosted AI/model API.

Create the solution and all tests only inside the new `AlarmTrend.DotNet/` directory. Do not read from, edit, or delete any files outside that directory. Do not make the new app depend on Python or any pre-existing app/database/artifact.

Provide a deployment/publishing recommendation for ASP.NET Core and Windows 11. State runtime, OS, network, storage, and background-worker requirements; do not claim compatibility with a hosting service unless you have verified that it runs ASP.NET Core and long-lived worker processes.

### 4. Configuration and local data

Provide the following `ALARMTREND_` settings (dotenv file plus environment-variable overrides):

- `ENV_FILE`: optional absolute path to a private dotenv file
- `DATA_DIR`: required to be absolute when supplied; SQLite, locks, and model artifacts live here, outside public static content
- `START_DATE`: default `2026-09-01`
- `SOURCE_URL`: default source URL above
- `SECRET_KEY`: session signing secret
- `ADMIN_PASSWORD_HASH`: password hash only; never store the plaintext password
- `SECURE_COOKIE`: default true; may be false for explicit local HTTP use
- `INTERVAL`: default 300 seconds; reject values below 60
- `TIMEOUT`: default 30 seconds; valid range 1–120 seconds

For a Windows desktop build, default persistent data to `%LOCALAPPDATA%\AlarmTrend\data`; never write into the executable bundle, source directory, or public static directory. Keep database/model data outside the publish directory. Use structured logging with timestamps and actionable error messages; do not log secrets or credentials.

### 5. New SQLite schema and data safety

Create a new SQLite database schema from this specification; do not open or import a database from another application. Store timestamps as ISO-8601 UTC text, except local-calendar aggregate dates which use `yyyy-MM-dd`. Apply idempotent, versioned migrations using `PRAGMA user_version`; refuse to run if the database schema version is newer than this application. Use transactions for multi-row updates. Create at least these tables and fields:

- `meta(key PRIMARY KEY, value)`: source validators, coverage start, last successful check, active model/evaluation metadata, and forecast queue marker.
- `events(id, stamp_utc, kind[start|end], origin[source|manual], precision, text_duration_minutes, enabled, reconciled_to, raw_source_row)`: source and user event ledger; keep soft-disabled records.
- `intervals(start_utc PRIMARY KEY, end_utc NULL, origin, valid)`: paired alert intervals; null end means active/open; `valid=0` means status is uncertain.
- `issues(id, stamp_utc NULL, category, message)` and `source_issues(id, stamp_utc, message, raw_row)`: diagnostics and ambiguous input; never fabricate an event to fill a gap.
- `daily(day_local PRIMARY KEY, starts, alert_minutes, complete)`: Kyiv calendar-day aggregates.
- `audit(id, at_utc, action, payload_json)`: append-only user/source correction trail.
- `sync_runs(id, at_utc, success, http_status, changed_count, message)`: one row per sync attempt.
- `forecasts(id, at_utc, model_id, payload_json)`: append-only issued forecast snapshots. Add SQLite triggers rejecting update/delete.
- `login_attempts(at_utc, address)`: rate-limit state.
- `model_runs(id PRIMARY KEY, trained_at_utc, training_end_utc, fingerprint, artifact_path, metadata_json)`.
- `training_jobs(id PRIMARY KEY CHECK id=1, requested_at_utc, fingerprint, state, message)`: single coalescing train request.

Keep all archival history; never apply a rolling retention delete. Write an online SQLite backup command that verifies the copy with `PRAGMA integrity_check`. Never silently drop tables, audit rows, forecasts, or model artifacts.

Important semantics:

- Source events have `source` origin; user events have `manual` origin, second precision, and may be disabled rather than deleted.
- Historical source corrections update/disable records and create audit entries; never erase the audit trail. If a new full snapshot contains over 20% fewer event rows than the previous valid snapshot, reject it as suspicious and retain the last working history until reviewed.
- Reconcile a manual event to a source event only when exactly one same-kind candidate exists less than 60 seconds away and there is no competing match. Keep ambiguous matches visible as conflicts; do not merge merely similar timestamps.
- Source timestamps have no UTC offset: convert ordinary Kyiv-local times using the IANA `Europe/Kyiv` zone. Never guess through a DST fold/gap; retain the original row as a source issue and mark affected periods/days unknown or incomplete.
- Pair starts and ends chronologically in UTC. An open alert stays open. Duplicate starts, unmatched ends, zero/negative duration, future timestamps, and source ambiguities must not be turned into confidently clear/active intervals. Preserve raw ambiguous source rows and mark affected periods/days unknown or incomplete.
- Compare source text duration to timestamp-derived duration with a one-minute tolerance; this discrepancy is diagnostic and does not by itself invent a different event time.
- Only call a local day complete when source coverage and successful observation cover it and no relevant unresolved issue invalidates it. A quiet gap in an imperfect source cannot be certified as no alert.
- Keep all archival history; do not implement a rolling 90-day delete.
- All stored timestamps are ISO UTC. All user-facing and calendar analytics use `Europe/Kyiv`; correctly handle 23/25-hour days, ambiguous/nonexistent local times, and Kyiv midnight. Reject ambiguous manual local times unless the user supplies an explicit UTC offset.

### 6. Source synchronization and worker behavior

Implement one conditional GET against the source snapshot. Send `If-Modified-Since` and/or `If-None-Match` when known. On `304`, skip parsing but refresh the successful-check time and recompute any due predictions. On `200`, validate UTF-8 HTML and expected table structure, parse the known Ukrainian event types and duration, and upsert changes idempotently. Preserve cached history on network, HTTP, parsing, or validation failures. Use a 10-second connect timeout, a configurable read timeout (default 30 seconds), and at most two retries with increasing backoff for transport failures, HTTP 429, and 5xx; do not retry indefinitely. Record HTTP status, change count, and error details in `sync_runs`. Update last-success time only on a successful response.

Reject suspicious snapshots, including an invalid/empty table and a history reduction greater than 20%, without replacing working data. Respect the full-snapshot nature of the source and its current newest-first row order. Use one cross-process synchronization lock shared with manual writes. If it is busy, safely skip the duplicate worker run. Use transactional DB updates and release locks on process exit/crash.

Preserve these operational commands as an equivalent .NET CLI (names can be adapted but document a direct mapping):

- initialize/migrate database
- one-shot source sync
- recurring worker sync (default every 300 seconds)
- train model now
- recurring training-queue processor (checks about every 60 seconds)
- recalculate forecasts locally without a network fetch
- run chronological evaluation without changing the selected model
- export the latest real evaluation to JSON/Markdown
- create and validate a SQLite backup
- generate/configure an admin password hash safely
- serve locally, and desktop mode that opens a browser

The browser must only read already-issued/cached forecasts. A web request must never call the source, train a model, or perform inference. Manual changes only enqueue a forecast recalculation. The forecast worker checks that queue about every five seconds. The separate trainer must not block sync or forecasting. Ordinary model retraining is throttled to no more than once per 24 hours when history is unchanged; corrections inside the fitted history can bypass that throttle.

### 7. Forecasting behavior that must remain equivalent

Treat forecast algorithms as a core product requirement, not replaceable implementation details. Implement and document the specified formulas, alternatives, and safeguards below:

**Alert-start probability**

- Horizons are 15, 30, and 60 minutes and apply only when the current status is confirmed clear.
- Historical analogs use previous complete days at the same Kyiv-local clock time, exclude already-active analogs, and require the full outcome horizon to be known. Require at least 14 eligible analogs. Use the shared Jeffreys estimate `(k + 0.5) / (n + 1)`; the probabilities must be monotone by construction.
- Candidate model: L2 logistic regression with sine/cosine local time-of-day and weekend features. Once training history reaches 45 complete days, add `log(1 + alert starts in previous 24 hours)`; after 90 complete days, also add previous-six-hour alert starts, elapsed time since the latest alert end, and alert minutes in the previous 24 hours. Normalize dependent time slices so each calendar day has equal total weight. Tune L2 only within the training window from `{0.01, 0.1, 1}`. Refit scalers on training data only. Purge examples whose label horizon crosses a train/evaluation boundary. Apply equal-weight PAVA to the three predictions; never sort/relabel horizons.
- Unknown or active status suppresses onset probabilities; unknown status also suppresses duration/remaining forecasts that require a confirmed active interval.

**Number of new alerts through the next Kyiv midnight**

- Target is the number of new alert starts strictly after the prediction time and before the next local midnight. Do not count an already-active alert as a future start.
- Historical fallback uses same-local-time counts from previous complete days, requires 14 analogs, reports the mean/rounded expectation and empirical discrete 10th/90th percentiles including zero.
- Candidates are Poisson and NB2 regression with log link, L2 penalty 0.05, cyclic time, weekend, `log(1 + starts in previous 24h)`, and actual remaining-day exposure. NB2 dispersion is estimated by likelihood. Do not multiply a daily average by the remaining fraction of a day as a substitute.

**Alert duration and remaining time**

- Preserve Kaplan–Meier, Weibull AFT, and log-normal AFT candidates with right-censored observations. Do not treat an open/unknown interval as an observed alert end.
- The historical fallback uses KM for the median and 25th/75th percentiles; use at least 10 observed completions. Parametric AFT uses regularized censored likelihood and adds cyclic start-time/weekend features only after at least 60 completed alerts; below that threshold use intercept-only parametric distributions.
- For an alert already active for `t`, compute conditional remaining-time quantiles from `S(t+h) = S(t) * (1-q)` and chance of another 60 minutes as `S(t+60)/S(t)`. Never estimate remaining duration by subtracting elapsed time from the overall median.
- Return fallback/insufficient-data status when KM has no supported tail, AFT survival is below 1%, fitting is numerically unstable, or the sample-size requirements above are unmet.

**Feature timeline, evaluation, and model selection**

- Require at least 21 complete calendar days before fitting/evaluating candidates. Build a 300-second UTC timeline (or configured worker interval) while deriving local-calendar features using Europe/Kyiv. DST days have 276 or 300 five-minute slices. Do not leak future starts/ends into features; the only duration feature is elapsed time already observed. Exclude incomplete days, unknown 24-hour lookbacks, and labels crossing data gaps.
- Compare candidates with expanding chronological folds: initially 21 calendar days for training, then test the next seven days and expand. Do not shuffle. Fit scalers and tune regularization only on training time. Compare candidates on identical eligible evaluation points.
- Start probability metrics: Brier, log loss, and five calibration bins separately per horizon. Count metrics: expected-count MAE, 80% interval coverage and width. Duration/remaining metrics: normalized IPCW MAE, 50% coverage and width; estimate censoring with reverse KM on training data only and enforce `G >= 0.05`. Censored ends are not observed failures. Preserve per-alert cluster weighting for repeated active-alert slices and the 60-minute survival Brier score.
- Model choice is separate per task (onset, count, survival; survival combines total-duration and conditional-remaining MAE). A complex candidate may replace historical fallback only with at least three comparable folds, positive mean gain, >2% gain in at least 75% of folds, no onset log-loss regression on any fold, and no >5 percentage-point deterioration in interval-coverage error for count/survival. Do not ensemble candidates. When guardrails fail, retain the historical model.

### 8. Training artifacts and forecast archive

Preserve the `DATA_DIR/models` pattern using versioned JSON artifacts that include model parameters, scalers, regularization, fit window, eligible counts, censoring/training metadata, per-fold metrics, errors, selected model per task, and elapsed/CPU/data-size diagnostics. Validate artifact version and a SHA-256 content/history signature before using it. A correction to data inside the fit window invalidates that artifact; later events beyond the training cutoff do not.

Write artifacts atomically. Maintain `current.json` and `previous.json` pointers with hashes. A failed training or pointer replacement must leave the previous working artifact usable and the queue retryable. Register successful runs in `model_runs` and store the latest evaluation for the calculation page. Preserve immutable, append-only issued forecast snapshots and never recalculate them inside a web request. Use `previous.json`/historical fallback safely when the current artifact is missing, invalid, or incompatible.

### 9. Web security and UI

Recreate the Ukrainian labels and responsive dark visual style; retain semantic HTML, keyboard-accessible links/forms, and clear loading/stale/unknown states. Keep the eight-day timetable legible at narrow mobile widths. Use server-side Razor Pages with encoded output and parameterized SQL.

Preserve security behavior: cryptographically strong session signing, HttpOnly and SameSite=Strict cookies, Secure cookies when HTTPS is configured, two-hour admin session, CSRF tokens on every POST, login attempt throttling (five failed attempts per address per 15 minutes), no user enumeration, maximum request-body size, and headers equivalent to `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, same-origin referrer policy, and a restrictive Content Security Policy. Admin secret/hash missing means public pages still work and admin actions return unavailable. Never expose source HTML, secrets, stack traces, internal paths, or password material in API responses/logs.

Use ASP.NET Core Identity's password hasher or a maintained Argon2id library. Store only salted, slow password hashes. Provide a safe CLI command for creating/resetting the admin hash; never accept or persist plaintext credentials in configuration or logs.

### 10. Acceptance criteria and delivery

Implement in vertical slices and keep the application runnable at each milestone. Write automated tests; use local fixture HTML and temporary SQLite databases so normal test runs never depend on the live Kyiv Digital site. At minimum cover:

- parsing known rows, malformed pages, 304/200, retries, suspicious history reduction, cached-data preservation, and idempotent source updates
- event pairing, duplicates, unmatched ends, open alerts, text-duration discrepancies, manual reconciliation/cancellation/audit
- Europe/Kyiv DST gaps/folds, 23/25-hour days, midnight boundaries, UTC storage, and 300-second timeline behavior
- completeness/unknown periods, all forecast formulas and sample-size/fallback guards, monotonic P15/P30/P60, no future leakage, interval calibration, censoring/IPCW, temporal folds, model-selection rules
- artifact hash/version/signature rejection, atomic current/previous replacement, retry after failure, append-only forecasts, database migration and backup integrity
- web route responses, public-without-admin-config behavior, CSRF, rate limit, response headers, manual workflow, and timetable rendering
- Windows x64 publish launches, opens loopback only, finds bundled Razor/static assets, writes only to LocalAppData, and shuts down cleanly

Do not commit, push, deploy, or write outside `AlarmTrend.DotNet/`. Do not claim completion merely because the solution compiles. Run the relevant tests/build, report exactly what passed/failed, document limitations and deployment constraints, and finish with actual run/publish commands and output paths.

---

## End of prompt

The recommended default is .NET 10 LTS; confirm the current support lifecycle and installed SDK before starting: [Microsoft .NET support policy](https://dotnet.microsoft.com/en-us/platform/support/policy).
