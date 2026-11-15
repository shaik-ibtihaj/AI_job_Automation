# AI-Job-Automation

A Windows launcher and resumable local job/application workflow built around the existing resume parser, candidate profile, preferences, matching explanations, and deduplication.

## Actual capability status

The local workflow works. **Live LinkedIn discovery and Easy Apply submission are blocked and have not been verified.** No permitted applicant-side connector is configured, BrowserAct is not installed, and no browser session has been attached. An existing LinkedIn login alone does not provide permission to automate LinkedIn.

LinkedIn's [automated activity guidance](https://www.linkedin.com/help/linkedin/answer/a1341543) and [prohibited software guidance](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions) prohibit third-party software that automates its website. [Apply Connect](https://learn.microsoft.com/en-us/linkedin/talent/apply-connect) is an employer/ATS integration, not evidence of an applicant-side search or submission capability. Checked 2026-11-15. The available plugin search did not identify a suitable applicant integration. Installing a generic browser package would not resolve the permission blocker.

The default launcher reports this blocker explicitly and processes retained real listings, if present, without calling them fresh discoveries. It never substitutes synthetic jobs for live results. A permitted integration must be implemented and independently verified before live access can operate. The adapter interfaces and pagination/application orchestration are tested with mocks only.

## Start on Windows

Install Python 3.11 or newer if needed. Double-click **Start.bat** in this folder, or run these commands from Windows Command Prompt:

```bat
cd /d C:\Users\shaik\AI_job_Automation
Start.bat
```

The launcher creates `.venv` if absent and installs the dependencies in `requirements.txt` only when needed. Existing compatible dependencies are left alone. Network/environment failures produce an error and an installation command. No LinkedIn password is requested or stored.

Initial setup shows the resume path/fingerprint, saved preferences, outstanding profile questions, match rules, run limits, country-specific authorization confirmations, and reusable answers. Confirm only facts you know. Unknown values remain unknown. Enter each reusable question exactly as it appears, distinguish answers from consents/declarations, and explicitly approve its value. Passwords and access tokens are not screening answers.

Setup asks for **APPROVE DRY RUN** or **ENABLE LIVE APPLICATIONS**. Selecting live does not bypass the unavailable integration. Approvals apply to the displayed resume, current profile/preferences, rules, mode, and answer book. Changes invalidate approval and require setup again. Approving settings does not independently verify resume claims.

Useful commands:

```bat
Start.bat --setup
Start.bat --dry-run
Start.bat --live
Start.bat --non-interactive --dry-run
```

`--non-interactive` creates unapproved default settings when absent and reports missing approvals instead of prompting. Normal launching prompts for missing or stale approval. Exit codes: 0 completed; 1 failed/interrupted/startup error; 2 completed with capability/approval blockers. No real applications were submitted during implementation/testing.

## What runs without manually preparing JSON

1. Acquire an OS-held run lock. A second concurrent workflow fails clearly; the lock is released automatically when its process stops.
2. Load and validate the existing profile/preferences and verify the resume/extraction fingerprint and skill evidence.
3. Retain/import `data/linkedin_jobs_review.json` automatically, if present. This is cached review input, not account discovery. Existing records remain intact.
4. Ask the configured discovery adapter for saved role/location/seniority/work-type/keyword searches. The production adapter currently returns a capability blocker before any network or account access.
5. Deduplicate source IDs and posting URL identities, explain matches, and require observed evidence for Easy Apply or external classification. A supplied label without observation evidence stays unknown. An external destination also needs matching observation evidence; missing destinations stay blank.
6. Persist workflow outcomes, update the one live external CSV and review queues, and write JSON/text run reports.

A future permitted discovery adapter can follow supported cursors within the configured maximum searches, pages per search, and records per run. Checkpoints survive restart. Partially processed pages retain an offset and fingerprint; changed source pages stop incomplete rather than silently skipping data. Reports distinguish previous completions, new completions, pages read, limits, cached intake, and incomplete coverage. `Start.bat --reset-searches` explicitly resets coverage for a new scan; it never clears submission protection. No claim is made to cover every LinkedIn job.

## Application preparation and submission protection

Matching reuses the existing evidence-backed keyword scorer: skills up to 40 points, role title 30, seniority 15, explicit country 15. Scores explain relevance, not guaranteed eligibility. Unconfirmed qualifications, work authorization, language proficiency, salary and experience are never invented. Current literal title matching can under-score variants such as Fullstack or AI Engineering Consultant; source titles are preserved.

A permitted submission adapter must inspect the actual complete Easy Apply form. Only exact, approved reusable answers may populate it. Unknown questions, missing choices, new declarations/consents, unconfirmed country eligibility, or stale approvals hold the job for input while other jobs continue. Dry-run never calls submission. After a future integration is available, approved live rules can cover multiple matching applications without approval for every job.

Submission intent is committed before a submit call. Only correlated LinkedIn platform confirmation evidence can produce `submitted_with_confirmation`; an HTTP 200, success boolean, click, closed dialog or timeout cannot. Available confirmation evidence is retained privately. Interrupted/ambiguous attempts become `outcome_uncertain` and are never automatically retried. Confirmed jobs are also never resubmitted on restart. Idempotency keys are passed to the adapter; the adapter must check the expected form fingerprint before its side effect. Current production submission access remains unavailable.

External websites are never submitted to by this tool. Unknown methods remain in their own review queue.

## Private state and output files

```text
data/
  candidate_profile.json          Existing resume-supported profile; preserved
  resume_extraction.json          Existing exact PDF text; preserved
  linkedin_jobs_review.json       Retained real listings; preserved
  workflow.sqlite3                Durable jobs, identities, attempts, search checkpoints and runs
  legacy_output_archive.json      Original old exports, including synthetic demo data, archived as JSON
config/
  job_preferences.json            Existing preferences; preserved
  application_settings.json       Private rules, approvals and confirmed reusable answers
output/
  external_apply_jobs.csv         The only live CSV
  easy_apply_review_queue.json    Persistent Easy Apply outcomes and unresolved inputs
  unknown_application_jobs.json   Persistent jobs needing method confirmation
  last_run_report.json            Machine-readable counts, coverage, blockers, unresolved jobs and paths
  last_run_report.txt             Readable final summary
  fixtures/                      Explicit synthetic runs only, with separate state under data/fixtures/
```

The live CSV has this ordered header:

```csv
job_id,job_title,company_name,location,linkedin_job_url,application_url,match_score,matching_evidence,uncertainties,discovered_at,status
```

Only jobs with an observed external method are exported. The direct destination is preserved only when observed, never constructed from employer names. Repeated listings update one durable job and preserve its first discovery timestamp. Matching evidence and uncertainties are JSON values inside quoted CSV cells. Spreadsheet-formula-leading values are escaped for safe viewing; original records remain in private JSON/SQLite. Outputs are regenerated atomically from durable state.

Outcomes include `discovered`, `unknown_method`, `not_matched`, `needs_input`, `dry_run_ready`, `external_application_required`, `submission_in_progress`, `submitted_with_confirmation`, `failed`, and `outcome_uncertain`. These are **application/workflow outcomes**, separate from employer responses. Interviews, rejections, offers and email tracking are not implemented.

Earlier synthetic output is archived as JSON before generating live outputs. Old real records are retained for rechecking rather than discarded or assigned a guessed method. The old CSV identity sidecar is obsolete; durable identity state now lives in SQLite. Keep private state files; deleting the database discards restart/duplicate protection.

## Existing local intake interface

The existing matching, validation and deduplication functions in `src/job_pipeline.py` remain reusable. Its real-input CLI now uses the durable workflow and expanded CSV contract:

```bat
.venv\Scripts\python.exe -m src.job_pipeline --input data/linkedin_jobs_review.json
```

A synthetic input envelope must declare `synthetic: true`. The legacy sample command writes only to `output/fixtures/legacy_pipeline/`:

```bat
.venv\Scripts\python.exe -m src.job_pipeline --input data/sample_jobs.json
Start.bat --fixture --input data/sample_jobs.json --non-interactive
```

Fixture workflow mode always stays dry-run and uses separate state. It cannot connect a live adapter. These commands are development aids, not replacements for the blocked live LinkedIn capability.

## Resume and tests

Existing resume processing remains local:

```bat
.venv\Scripts\python.exe -m src.resume_parser resumes/master_resume.pdf --output data/resume_extraction.json
.venv\Scripts\python.exe -m src.profile_config
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The PDF parser reports malformed/encrypted/textless PDFs instead of inventing text. Candidate facts, exact source evidence, factual dates, manual review items, preferences, and the retained real listings are not rewritten by the workflow.

Tests use synthetic data and mocked adapters. They cover original functionality plus pagination/limits, checkpoint resume, classification evidence, approval invalidation, unknown questions and consents, routing, CSV updates, source identity merging, correlated confirmation, timeouts, interruption, process locking, and restart protection. No submission test accesses LinkedIn.

## Privacy

Resume files, candidate data, screening answers, approvals, SQLite state, session material and generated outputs are excluded from Git. The application stores no LinkedIn password and does not import browser cookies. Provider exception messages are not written to reports because they may contain credentials; reports retain exception types and safe local explanations. Confirmation evidence and queues are private and may contain personal information; do not share them publicly. Dependency downloads do not transmit the resume.
