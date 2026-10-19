# Plan: ctun VS Code extension, core first, add-ons on top

Status: core C1–C6 and add-ons 1–6 built as a prototype 2026-10-12/13, uncommitted on branch `feat/vscode-extension`. Waiting on the user gates (see "Add-on results"). Written 2026-10-12, restructured the same day into
a core and add-ons.

References:

- **Proposal page** <https://claude.ai/artifact/2ewvsrvu4NZeFc6QxaZyFW>: the
  vision for the maintainers, with clickable mockups of the core and the full
  view, the add-on cards, the ctun changes and four open questions. This plan
  implements that page; if the two disagree, ask the user.
- **Design canvas** <https://claude.ai/artifact/9QhZ5VzRSZqhjqZ7449tko>, page
  "Core and add-ons": artboard **Core** (what C1–C6 build), **Add-ons** (one
  card per add-on) and **Full** (the core with every add-on). The older pages
  v1–v3 are history only.

This is a first draft, not a production tool. The decisions below are the
parts that would be expensive to change later. Everything else may stay rough
and be fixed when it surfaces.

## Shape of the plan

- **Core:** a VS Code extension that shows what the ctun CLI already shows,
  with no change to ctun. It is usable on its own.
- **Add-ons:** each adds one feature on top of the core. Each starts only after
  the user says so. Add-on 1 (job records) is the base for 2–4; 5 and 6 need
  only the core.
- **Maintainer first:** the proposal page goes to the aimat-lab maintainers
  before any ctun change (0.0). The core can start before they answer.

## How to use this plan

- Work through the steps in order. Do one task at a time.
- After each task, check its **Done when**. Go on only when it passes.
- **Stop and ask the user** when a task says so, when a Phase 0 check gives an
  answer this plan did not expect, or when a fix would change a decision below.
- Never commit to `main`, push, open a PR, tag, or publish (PyPI, VS Code
  Marketplace) unless the user says so for that step.
- Each repo has its own rules. Read them first:
  - `cluster-tunnel/CLAUDE.md`: Conventional Commits, `encoding="utf-8"` on all
    text I/O, tests must not need a real cluster, `CHANGELOG.md` `[Unreleased]`.
  - `Prediction_Model/.claude/CLAUDE.md`.

## Decisions

Do not re-open these without the user.

### Core

| # | Decision |
|---|---|
| D1 | The extension is a thin TypeScript wrapper around the `ctun` CLI and its JSON output. It has no HTTP server, no own SSH, and never reads the workspace's files or git itself; everything comes from ctun. It sets `extensionKind: ["ui"]`. |
| D2 | The core needs **no change to ctun**. If a JSON output turns out to lack a field, that is a small ctun fix, decided when it surfaces. |
| D3 | The core follows the **Core** artboard; the end state is the **Full** artboard. One job list filtered by state toggles, an "Ended in" window and cluster chips, never tabs. The core shows only the columns ctun has today; add-ons fill in the rest (project, note, code, W&B, end time, queue estimate). |

### Add-ons

| # | Decision |
|---|---|
| D4 | **No new ctun commands.** `ctun run` gets optional `--project` and `--note`. When its output contains `Submitted batch job <N>`, ctun writes a job record. Its `--project` is the default project source (D13). |
| D5 | Records live **on the cluster**: `~/.ctun/jobs/<slurm cluster>/<job id>.json`, keyed by SLURM's own `ClusterName` (resolved on the cluster), never by the name in a machine's local config. Every machine finds the same folder. |
| D6 | Records are **write-once**: note and facts, never changed afterwards. The cluster script only writes and lists files. |
| D7 | Facts are collected by ctun, never written by the agent: commit, uncommitted files, pushed or not, submitting host, submit time, job name, working directory. ctun takes the working directory from SLURM (`scontrol show job <N>` → `WorkDir`) and runs git there. They describe the code **at submit time**. |
| D8 | A note is an **unreviewed draft** until the human confirms, edits or discards it in the extension. Reviews belong to the extension, not the record: they live in its `globalState`, which VS Code Settings Sync carries to the user's other machines. The agent has no command to write a review. |
| D9 | A note references commits and jobs only with explicit markers, `commit:<hash>` and `job:<id>`. ctun rejects a note whose marked reference it cannot find. |
| D10 | "Previous run" is the latest earlier record with the same project (from the active source, D13) and job name. The commit list between two runs comes from `ctun run -- git -C <workdir> log …`. |
| D11 | A job's W&B run is found through a W&B tag `slurm:<ClusterName>:<job id>`, appended at every start, so a resumed run carries the IDs of all its jobs. `ClusterName` is what the job sees as `SLURM_CLUSTER_NAME`; `ctun jobs --json` reports it per cluster as `slurm_cluster`. The older tag `slurm:<job id>` still matches on any cluster; the cluster tag wins. The run is searched in every project of the W&B entity (the first 100). Which metrics the extension shows is a setting. |
| D12 | **Libraries are add-ons of the extension, not of ctun.** The core knows no library. An add-on fills only three slots: a project per job (after each jobs refresh), lines in a job's details (when they open) and a row in the Setup view. The core draws them; an add-on never touches the webview or the table model. Each add-on is off by default (`ctun.addons.<id>.enabled`) and keeps its own settings and secrets. The interface is internal; it is exported from `activate()` when a second library needs it. W&B is the first add-on. |
| D13 | **One project source at a time.** `ctun.projects.source` is `record` (the record's `--project`, the default) or an add-on's ID, e.g. `wandb`. The sources never mix. A `/` in a project nests it (`htfm/multihead`); the W&B add-on nests a run's group below its project. If the named add-on is off, every job is Unassigned and the Setup view marks the setting as broken. |

## Phase 0 — checks before any code

Write the answers into "Phase 0 results" at the end of this file. Checks
marked **(user)** need the human.

**0.0 Maintainer feedback (user).** The user shares the proposal page with the
aimat-lab maintainers (it is private until shared). Record their answers to
its four questions here; they settle 0.1 (repo location), 0.4 (records
upstream, on by default or opt-in), the "Later" `jobs.sh` fields, and 0.3 /
Add-on 6 (secrets through a pipe). Until they answer, work only on the core.

### For the core

**0.1 Extension repo (user).** A new repo `ctun-vscode` (recommended: own
toolchain and release cycle) or a folder `vscode/` inside cluster-tunnel?
Follows the maintainers' answer to question 1; without one, start in a repo
under the user's own account.

**0.2 ctun on Windows.** In a VS Code terminal on Windows, run
`ctun --version`, `ctun status -j`, `ctun -t haicore jobs --json`. Record the
absolute path of `ctun` and whether the JSON is clean on stdout.

**0.3 Login from the extension.** Read `popup.py` and `pty_login.py`. Can
`pty_login` take the password and OTP from a pipe, without argv or env?
The answer decides whether Add-on 6 is possible without ctun changes.

### For the add-ons (answer before Add-on 1)

**0.4 Upstream or fork? (user)** Add-on 1 changes `ctun run` and writes files
on the cluster. Upstream to aimat-lab as PRs, or only in the fork `Luguza`?
Follows the maintainers' answer to question 2 (0.0), including whether records
are on by default or opt-in per cluster.

**0.5 Where the job's code comes from (user + cluster).** A git checkout on
the cluster (facts from `WorkDir`, D7), or rsync from a laptop (facts from the
local repo ctun runs in)? Also check that `scontrol show job <N>` shows
`WorkDir` on each cluster.

**0.6 SLURM names and homes (cluster).** Per cluster:
`scontrol show config | grep ClusterName`, `echo $HOME; df -T $HOME`, and
whether `$HOME` is the same on two login nodes.

**0.7 sbatch output.** From a past submission (no test job): expected
`Submitted batch job <N>`.

**0.8 W&B (user).** The W&B entity name for links, and whether storing a W&B
API key in VS Code's SecretStorage is fine.

**Gate:** 0.1–0.3 answered before the core; 0.0 and 0.4–0.8 before Add-on 1.

## Core — the extension (repo from 0.1)

Every task: `npm run compile` and `npm test` pass. Tests use fixture JSON,
never a real cluster.

### C1. Scaffold
- **Steps:** TypeScript extension with `extensionKind: ["ui"]`, a view
  container "Clusters", and a setting `ctun.path` (default: `ctun` on PATH).
- **Done when:** the empty extension starts in the Extension Development Host
  on Windows.

### C2. ctun adapter
- **Steps:** the one module that runs `ctun` and parses JSON (D1). It maps
  ctun's exit codes 10–12 to errors, has a timeout, and never puts a secret in
  argv.
- **Done when:** unit tests cover an exit code, malformed JSON and the timeout.

### C3. Clusters view
- **Steps:** a tree from `ctun status -j`: status dot, session start, budget
  (used/limit with a bar), commands sent, running/queued counts from
  `jobs --json`, "Log in" on logged-out clusters, "Log out" on live ones.
- **Done when:** it matches the Core artboard's sidebar, and a refresh command
  works.

### C4. Login and logout
- **Steps:** "Log in" runs `ctun -t X login --interactive` (the existing
  popup), then polls `ctun status -j` until the cluster is live. "Log out"
  runs `ctun -t X logout`.
- **Done when:** both work on Windows.

### C5. Jobs table
- **Steps:** the Core artboard's table from `ctun jobs --json` per live
  cluster:
  - one list, filtered by toggle buttons Running / Queued / Completed /
    Failed (incl. timeout, OOM, node fail) / Cancelled, and an "Ended in"
    drop-down (24 h / 7 d / 30 d, passed as `--since`; `--since 0` when all
    finished toggles are off, which skips `sacct`)
  - cluster chips
  - columns cluster·ID, job, state, elapsed / limit, partition · nodes:
    exactly the fields `ctun jobs --json` has today (no end time, exit code
    or pending reason; see "Later").
  - no expandable job details: the core has nothing to show beyond the row
  - refresh every 60 s while visible, plus a manual refresh (every refresh
    runs `sacct` on a login node, so not faster)
- **Done when:** fixture data renders all states, and toggles, window and
  chips combine as in the Core mockup on the proposal page.
- **Review:** the webview has a strict CSP, no remote scripts, and
  `postMessage` only.

### C6. Package
- **Steps:** `vsce package`, plus a README with install steps.
- **Done when:** the `.vsix` installs on Windows and on Linux. Do not publish.

**Core gate (user):** the user installs the `.vsix` and uses it for a day.
Then ask which add-on comes next.

## Add-ons (each needs the user's go-ahead)

Each add-on matches its card on the proposal page; together they give the
Full artboard.

### Add-on 1. Job records in ctun (repo: cluster-tunnel)

The base for Add-ons 2–4. No UI on its own.
- **Steps:**
  - `cluster_tunnel/records.py`: a `JobRecord` dataclass (schema_version,
    slurm_cluster, job_id, job_name, project, note, submitted_at,
    submitted_from, code, command). `from_json` accepts unknown keys, so later
    fields never break an older ctun.
  - `cluster_tunnel/scripts/records.sh`: `write` (temp file, then `mv`) and
    `list`, in the D5 folder.
  - `cluster_tunnel/facts.py`: the D7 facts. A failing fact is recorded as
    unknown with a warning; it never blocks a submission.
  - `ctun run`: optional `--project` and `--note`; after the command, write one
    record per `Submitted batch job <N>`. Without a job ID nothing changes.
    The command's output and exit code stay exactly as today.
  - `ctun jobs --json`: a `record` key per job (or `null`) and a top-level
    `records_without_job` list.
  - Docs: `skill/SKILL.md`, `skill/reference.md`, `CHANGELOG.md`.
- **Done when:** tests with a mocked `ssh` cover a record written with the
  right fields, a run without a job ID, and a failing fact. The existing `run`
  tests still pass.
- **Gate (user):** a job submitted from one machine shows its record in
  `ctun jobs --json` on a **second machine**. Then ask whether to push and open
  a PR.

### Add-on 2. Projects

- **Steps:** project grouping, a project filter in the table, and project
  counts in the Clusters view. Jobs without `--project` count as "Unassigned".
- **Done when:** fixtures with two projects and unassigned jobs render, and
  the filters combine.

### Add-on 3. Agent notes and review

- **ctun:** the D9 reference check on `--note`, with
  `ExitCode.NOTE_REFERENCE_INVALID = 13`; the error lists each unknown
  reference. Skill rules: put only facts from this session into `--note`,
  mark references, leave the note out when unsure.
- **Extension:** the note under each job name with its review state, Confirm /
  Edit / Discard stored in `globalState` with `setKeysForSync` (D8), a
  "To review · N" button next to the cluster chips that switches to review
  cards (with the checked references), the activity-bar badge and the
  status-bar count.
- **Done when:** a review on one machine shows on a second machine with
  Settings Sync on; fixtures cover no note and a discarded note.

### Add-on 4. Code facts and changes

- **Steps:** expandable job details (the core has none), with: commit,
  "not on remote" in amber, the
  submit-time hint (D7), the submitting host, the "first run after…" line and
  a collapsed commit list (D10).
- **Done when:** fixtures cover an unpushed commit and a failed previous run.

### Add-on 5. W&B

- **Prediction_Model:** append the tag `slurm:<SLURM_JOB_ID>` to the run's
  tags in `models/train_htfm.py`, after the `WandbLogger` is created; skip it
  without SLURM. Show the diff to the user; commit only if the user says so.
- **Extension:** API key in SecretStorage, run lookup by tag (D11), the metrics
  from the setting `ctun.wandb.metrics` (default `val/loss`,
  `val/mae_T_trans`), the epoch, and a "W&B ↗" link. Without a key: "W&B not
  configured".
- **Done when:** a resumed run keeps the tags of its earlier jobs, and a
  fixture API response renders.

### Add-on 6. Native login

- **Steps:** only if 0.3 said yes (otherwise it first needs `pty_login` to read
  secrets from a pipe): VS Code quick inputs for the budget and each ssh
  prompt (`password: true`), passed to ctun through the pipe.

### Later

- **Training view:** charts of the W&B history ("Training view" artboard).
- **Budget across machines:** the session record on the cluster, so all
  machines share one budget.
- **Unpushed-commit gate:** an option that refuses a submission of an
  unpushed commit.
- **Richer job columns:** end time, exit code, pending reason and submit time
  in `jobs.sh` and `ctun jobs --json`.
- **Queue insight for queued jobs:** estimated start (`squeue` `%S`, often
  `N/A`, show as an estimate), reason (`%r`) and priority (`%Q`) in the same
  `jobs.sh` call; the factor breakdown from `sprio -j <id> -l` only on demand,
  never on every refresh. First check what each cluster returns.
- **Ideas, not decided:** job actions (log, cancel, resubmit via `ctun run`),
  job-end notifications, upload/download in the Explorer, `ctun info` and
  `ctun logs` in the Clusters view.

### Proposed: command history across sessions (not built)

Today `cmdlog.py` keeps the commands of one session only. A fresh `login` and
`logout` delete the log, and the Clusters view shows only the count. The goal:
see every command the agent sent, also from earlier sessions.

- **ctun:**
  - Do not delete the log on `login` or `logout`. Tag each entry with its
    `session_id`.
  - `cmdlog.summary` counts only the entries of the current session, so
    `status` and "N sent this session" do not change.
  - On each write, drop entries older than 30 days, the longest "Ended in"
    window of the jobs table.
  - `ctun logs` shows the current session, as today; `ctun logs --since 30d`
    also shows earlier sessions.
  - Open: move the log from the cache dir to the data dir. The OS or the user
    may empty the cache at any time.
  - Docs: `skill/reference.md` (`logs`, `logout`) and `CHANGELOG.md`, which now
    promise "cleared on `logout` and on a fresh `login`".
- **Extension:** the "Commands" row expands to the list from
  `ctun -t <cluster> logs --json`, newest first, with the full command in the
  tooltip. It is a local call; no SSH.
- **Trade-off:**
  - Gain: an audit trail across sessions. A deleted file or an unexpected job
    can be traced back to the command that caused it.
  - Cost: commands can carry secrets in their arguments, e.g. a token or a
    password in a URL. Today they are gone at logout. With this change they stay
    on disk for up to 30 days, in plain text, readable by anyone with access to
    the user account.
  - Redaction is no fix: ctun cannot know which argument is a secret.
  - Possible mitigation, not decided: the retention as a config option, where
    `0` keeps today's behaviour.
  - The temporary log is a documented promise of ctun. Ask the maintainers
    before a PR.
- **Not covered:** the log cannot tell the agent from the human; every
  `ctun run` is logged. To tell them apart needs a marker the agent sets (an
  environment variable or a flag), a separate decision.
- **Size:** about 40 lines plus tests in ctun, and the expandable row in the
  extension.

### Proposed: the agent's output in the command log (not built)

The Commands panel (option G on the canvas
<https://claude.ai/artifact/VSi89YUN9TKZYeowa8qmLt>) shows the output of the
user's own commands. The goal: a click on an agent's command shows its output
too. Today `cmdlog.py` stores no output.

- **ctun:** `ctun run` already captures stdout in `ssh.run_capturing` to find
  `Submitted batch job <N>`. Write that text, the exit code and the duration
  into the log entry, capped at the last 200 lines or 64 KB. Do it in the same
  change that stores the submitted job IDs in the log entry.
- **Limits:**
  - Only stdout; stderr passes straight through. To capture it too, pipe it
    as well, which can change the order of stdout and stderr lines.
  - No output for `--tty` commands or for clusters with `job_records` off;
    they take the other code path.
- **Trade-off:** output can hold data, paths and secrets, more than a command
  line. Keep the output session-only (deleted at `logout`), also if the
  command history above keeps commands for 30 days.
- **Rejected:** re-running a command to show its output. An `sbatch` would
  submit again, and results change over time.

## Not in this plan

- **Budget-guard gap.** `budget.is_guarded` checks only the first command
  token, so `ctun run -- bash -c "cd … && ./submit.sh"` is not guarded. This
  is an existing ctun bug; fix it separately.

## Phase 0 results

**0.0** Not answered yet. The core went ahead without it, as planned.

**0.1** First decided 2026-10-12 as a folder `vscode/` in the fork. Changed by
the user 2026-10-13: the extension is its own public repo,
<https://github.com/Luguza/ctun-vscode> (no submodule; it uses only the ctun
CLI). The ctun changes stay on the local branch `feat/vscode-extension` here.

**0.2** Checked 2026-10-12 on Windows 11, ctun 0.5.0, Node 24.21, VS Code 1.141.

- `ctun` is `C:\Users\Luca\.local\bin\ctun.exe` (uv tool, installed from the
  local checkout). `ctun --version` → `0.5.0`.
- `ctun status -j` and `ctun -t haicore jobs --json` print clean JSON on
  stdout, exit 0. Nothing else goes to stdout.
- Found and fixed: `jobs --json` first returned `"error": "... $'\r': command
  not found"`. Cause: this checkout's `*.sh` files were CRLF in the working
  tree (index LF; the checkout predates `.gitattributes`). Restored them from
  the index and reinstalled the tool. The PyPI 0.5.0 wheel is LF, so users of
  the release are not affected. The extension must show `clusters[].error`.
- JSON shapes the core relies on:
  - `status -j`: a list of `{cluster, target, live, session{session_id,
    start_epoch, limit, unit} | null, limit, unit, commands{count,
    last_epoch}, used, used_error}`.
  - `jobs --json`: `{since_minutes, clusters: [{cluster, live, error, jobs:
    [{source: "active"|"done", jobid, state, elapsed, timelimit, partition,
    nodes, start, name}]}]}`. All job fields are strings. `state` is raw, e.g.
    `CANCELLED by 504768`, so the extension classifies by prefix.
  - Exit codes (`constants.ExitCode`): 10 `login_required`, 11
    `budget_exhausted`, 12 `budget_guard_error`, each with a
    `ctun-error: <marker>` line on stderr.

**0.3** No. `pty_login.login()` takes the secrets as arguments, and on
Windows `main()` gets them inside the program text on stdin. But `ctun login`
itself has no way to take them from a pipe: `--interactive` gets them from
the tkinter popup's own pipe, and the plain path runs `ssh` on the terminal.
Add-on 6 needs a small ctun change first, e.g. `ctun login --credentials-stdin`
that reads `{password, otp, limit}` as JSON and calls
`popup.login_with_password` (already separated from the dialog).

**0.4** Not answered by the maintainers. Provisional (user, 2026-10-12: "go
ahead with the most reasonable option"): build in the fork only, no PR.
Records are **on by default**, with a per-cluster opt-out `job_records: false`.

**0.5** Checked on haicore 2026-10-12: the jobs run from a **git checkout on
the cluster** (`/hkfs/work/workspace_haic/scratch/qa7606-prediction_model/
Prediction_Model`, `main...origin/main`), so D7 holds: facts from `WorkDir`.
`scontrol show job <N>` shows `WorkDir`; `squeue -o %Z` gives it too and copes
with job names that contain spaces, so `facts.sh` uses squeue, then sacct.

**0.6** haicore: `ClusterName = haic`. `$HOME=/home/iti/qa7606` on GPFS
(`/hkfs/home`), shared by all login nodes. git 2.47 and python3 exist on the
login node. bwuni3 not checked (no tunnel).

**0.7** `submit.sh` calls plain `sbatch`, which prints `Submitted batch job <N>`.
Not re-checked with a test job (no compute spent).

**0.8** Not answered. `models/train.py` sets no W&B entity, so the run lives
under the API key's default entity; project `htfm_multihead`. Provisional:
settings `ctun.wandb.entity` (empty = the key's default entity, looked up via
the API) and `ctun.wandb.project`; the key goes in SecretStorage.

Note: the plan names `models/train_htfm.py`; the file is now `models/train.py`.

## Open questions (decided provisionally, for the user to confirm)

1. Records on by default (opt-out per cluster), or opt-in? (0.4)
2. `records_without_job` sits in each cluster entry of `ctun jobs --json`,
   not at the top level: a record belongs to one cluster's folder.
3. D9 check timing: `--note` references are checked **before** the command
   runs, so a bad note never leaves a submitted job behind. `job:<id>` is
   checked with sacct/squeue and the records; `commit:<hash>` in the git repos
   of this project's earlier records. A project with no record yet cannot have
   commit references checked, so they are rejected (exit 13) with a hint.
4. "Previous run" (D10) is searched among the records ctun returns for the
   current window, not the whole history; older ones show as "no earlier run
   in this window".
5. Native login (Add-on 6) asks for the password and OTP up front, per the
   cluster's `requires_password` / `requires_otp`, not per actual ssh prompt.

## Core results

Built 2026-10-12, first in `vscode/`, now in <https://github.com/Luguza/ctun-vscode> (paths below are relative to that repo).
`npm test`: 20 unit tests pass. `npm run test:host`: the extension activates
in VS Code 1.141 on Windows.

- **C1** Scaffold: `extensionKind: ["ui"]`, view container "Clusters",
  `ctun.path`. The host test needs `ELECTRON_RUN_AS_NODE` unset (VS Code's
  terminals set it); `runTest.ts` does that.
- **C2** `src/ctun.ts`: `execFile` without a shell, exit codes 10–12 →
  `login_required` / `budget_exhausted` / `budget_guard_error`, timeout,
  missing executable, malformed JSON. No method takes a secret.
- **C3** `src/clustersView.ts`: a native tree, not a webview. Two departures
  from the artboard: the budget bar is a text bar (`▰▰▱▱…`), and "Log in" is
  an icon on the row plus a clickable "No tunnel. Log in…" row, since tree
  rows cannot hold a text button. Found: ctun measures budget `used` only
  for a targeted `ctun -t X status -j`, so a refresh runs one extra call per
  live guarded cluster. Status bar: "ctun · N of M connected" plus budgets.
- **C4** `src/login.ts`: login runs `ctun -t X login --interactive` (10 min
  timeout for the human), then polls `status` until live. Checked on Windows
  with the real ctun that the login window shows (it is not hidden by
  `windowsHide`), and the full login/logout flow with a fake ctun. **Not
  checked:** a real login with a password/OTP; that is for the core gate.
- **C5** `src/jobsTable.ts` builds the whole table model (filters, counts,
  `--since`, `0` when no finished toggle is on) and is unit-tested; the
  webview (`media/jobsTable.js`) only renders it and posts clicks back.
  CSP: `default-src 'none'`, styles from the extension only, one nonce'd
  script, no remote content. Refresh every 60 s while visible. Extra Slurm
  states are grouped too (COMPLETING, REQUEUED, PREEMPTED, …); a state no
  group covers is always shown.
- **C6** `npm run package` → `ctun-vscode-0.0.1.vsix` (20 KB). Installs on
  Windows (VS Code 1.141) and on Linux (VS Code Server 1.141 in WSL Ubuntu;
  the Linux desktop build lacks GUI libraries in WSL). `--skip-license`
  because the repo has no license yet. Not on the Marketplace. Publishing steps:
  `PUBLISHING.md` in that repo.

## Add-on results

Built 2026-10-13 after the user said to finish the whole plan without review
("go ahead with the most reasonable option"). The open questions above are the
choices made on the way. Tests: ctun `uv run pytest -q` 164 passed;
extension `npm test` 39 passed, `npm run test:host` passes; `.vsix` 34 KB.

- **Add-on 1** (ctun): `records.py`, `facts.py`, `scripts/records.sh`,
  `scripts/facts.sh`; `run --project/--note`; `ssh.run_capturing` streams the
  output and keeps it to find `Submitted batch job <N>`; `jobs --json` adds
  `record`, `records_without_job`, `records_error`; config `job_records`.
  Checked live on haicore (read-only): `facts.sh` for a queued job returns
  cluster `haic`, workdir, commit, pushed, 8 uncommitted files; `records.sh
  list` returns nothing yet. **Not done:** no record was written on the
  cluster (that needs a real submission), and the gate "a record shows on a
  second machine" is the user's.
- **Add-on 2**: project groups, project filter, Projects view with counts.
- **Add-on 3**: ctun `notes.py` + `scripts/note_refs.sh`, exit code 13,
  checked before the run (open question 3). Extension: note under each job,
  Confirm/Edit/Discard in `globalState` with `setKeysForSync`, "To review"
  cards, activity-bar badge, status-bar count. Edit uses an input box, not an
  inline text area. **Not checked:** Settings Sync across two machines.
- **Add-on 4**: details with commit, pushed (amber when not), uncommitted
  count, "as of submit time", submitter, previous run (D10, within the window,
  open question 4) and the commit list from `ctun run -- git log` on request.
- **Add-on 5**: Prediction_Model `models/train.py` appends `slurm:<id>` to
  the run's tags (5 lines, **uncommitted**; the cluster checkout needs it
  pushed and pulled). Extension: `wandb.ts` (GraphQL, run by tag, default
  entity via `viewer`), key in SecretStorage, settings `ctun.wandb.*`.
  Tested with a fixture API response only. **Not checked:** that W&B keeps
  the earlier tags of a resumed run (plan's Done-when); wandb is expected to
  restore tags on `resume="allow"`, but no run was made.
- **Add-on 6**: ctun `login --credentials-stdin` (JSON on stdin); extension
  setting `ctun.login.method: vscode` asks budget, OTP and password in input
  boxes and pipes them to ctun. Tested with mocks (ctun) and a stdin echo
  (adapter); no real login was made this way.
- Visual checks used a fake `ctun.exe` in the scratchpad and the test VS Code:
  grouped table, notes, review cards, Projects view, badge and status bar
  render; the details panel is covered by a jsdom renderer test.
- Not changed: the globally installed skill copy (`~/.claude/skills/
  cluster-tunnel/`), whose edits auto-push to the user's config repo; re-copy
  `skill/` after review.
