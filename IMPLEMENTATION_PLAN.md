# AI-Job-Automation implementation plan

## Scope and current foundation

Implement a terminal-started workflow that discovers matching LinkedIn jobs using authorized data access, deduplicates them, prepares Easy Apply applications for review on LinkedIn, and maintains one external-application CSV. The existing resume parser, candidate profile, exact evidence, preferences, and tests remain the foundation.

This file is a plan, not an implementation. No packages, browser automation, account connections, exports, or application submissions are added by this stage. Application-response tracking is excluded.

## 1. Establish the authorized access contract

Before choosing a browser or API package, identify a source whose permission covers automated LinkedIn job discovery and the underlying data. Confirm credentials/scopes, rate limits, permitted fields, retention rules, and source provenance. Do not assume a LinkedIn developer account, posting API, public page, or commercial scraping service grants applicant-side discovery access.

Check a small authorized sample for a stable job identifier, title, company, location, description/requirements, actual LinkedIn job URL, application-method evidence, and external application destination when present. Record missing capabilities explicitly. Only after access is established should a suitable adapter and its dependencies be selected. Future credentials belong in ignored local secrets, never source control.

Discovery permission does not imply permission to fill or submit Easy Apply forms. Validate those capabilities separately. If unsupported, prepare locally and hand off to the user for manual review and submission on LinkedIn. If live discovery itself is unavailable, stop that part and explain the gap; do not substitute unauthorized automation. Explicitly permitted local input can support adapter development without claiming automatic discovery works.

Use the official [LinkedIn automated activity guidance](https://www.linkedin.com/help/linkedin/answer/a1340567/automated-activity-on-linkedin?lang=en) and the selected provider's actual agreement. The documented [LinkedIn Job Posting API](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview) is for authorized posting integrations and is not evidence of applicant-side search or submission permissions.

## 2. Add the terminal orchestration and input checks

- Define a CLI entry point with the existing project virtual environment. Describe the exact command once implemented; do not present a nonexistent command as runnable.
- Reuse `load_candidate_profile`, `load_job_preferences`, and the resume parser/source fingerprint checks. Reject malformed configuration and report stale profile evidence.
- Distinguish resume-stated information, candidate confirmation, and independent documentary verification. Preserve open `manual_review_items` and all factual dates. Block only the affected answers or actions until the required evidence/confirmation is available.
- Derive discovery filters from preferences and profile-supported information. Keep the full private resume/profile local; transmit only the minimal permitted search criteria.

## 3. Normalize, match, and deduplicate jobs

Introduce an adapter interface and an internal job record containing source IDs/provenance, actual posting URLs, source description/requirements, application-method evidence, and discovery date. These are implementation concepts, not additional CSV schemas.

Match roles, skills, country, seniority, work type, and keywords to the existing JSON inputs. Explain which requirements are supported, missing, or unknown. Do not infer visa eligibility, language levels, experience totals, certifications, or screening answers. Apply the configured Senior/Lead/Principal/Director exception only when requirements genuinely match the profile and receive manual review.

Use stable LinkedIn/source job identifiers and verified canonical posting URLs for identity. Reconcile known aliases without stripping functional application tokens. Use title/company/location only to flag possible duplicates; similar titles may describe different openings. Deduplicate the current batch and compare external jobs against the existing CSV across repeated runs.

## 4. Verify and route the application method

Classify jobs as `easy_apply`, `external`, or `unknown` using explicit source evidence. Preserve the reason for classification in the local run context. Missing/ambiguous method data stays unresolved in the terminal and is not guessed into either route.

Verify application destinations only through permitted data or permitted access. Prefer direct company or employer-designated ATS application URLs. If a destination cannot be verified, retain the actual LinkedIn job URL when available and leave the missing destination blank. Never manufacture links or bypass a challenge/access restriction.

## 5. Prepare Easy Apply jobs for review

Use an in-memory terminal review queue showing the posting, resume selection, proposed field values with evidence references, and unanswered screening questions. Do not create an Easy Apply CSV or a separate job-export CSV.

Populate proposed answers only from confirmed, evidence-supported candidate information. Surface unresolved employment, degree, project, language, work-authorization, and other relevant review items. Request missing answers rather than generating plausible values. Resume keywords alone do not prove proficiency or eligibility.

The preparation flow may distinguish draft, needs-input, and ready-for-review states within the current run; these are not application-response tracking. Resume attachments and personal-data transfer occur only at the permitted application stage following user review. Where platform interaction is not authorized/supported, show the verified posting URL and reviewed materials for manual use on LinkedIn.

Require explicit review before any future submission step. Report success only after confirmation from the application platform, such as a receipt or explicit submitted message. A button click, HTTP success code, closed dialog, draft, timeout, or navigation is insufficient. Report ambiguous outcomes as unconfirmed and avoid a blind retry that could duplicate a submission. Do not create a status tracker, response history, interview tracker, email monitor, or status columns.

## 6. Maintain one external-application CSV

Create/update only `output/external_apply_jobs.csv` with this exact ordered header:

```csv
job_title,company_name,application_url,linkedin_job_url,location,date_found
```

- Include jobs confirmed to require external applications; do not include Easy Apply or unknown-method jobs.
- Prefer a verified direct company application URL, including the company's designated ATS. Otherwise retain a verified external destination. Leave unavailable URLs blank and report them for review; never infer them from company names.
- Use actual source values for title, company, location, and LinkedIn URL. Format `date_found` as the first discovery date in UTC (`YYYY-MM-DD`) and preserve it on updates.
- Read and validate the existing file, update matching postings, and append genuinely new ones. Preserve distinct openings and existing rows. Fail clearly on invalid input instead of silently overwriting it.
- Use standard CSV quoting and Windows-compatible UTF-8 output. Replace the file safely so an interrupted update cannot leave a partially written CSV. Produce no dated copies or alternate CSV exports.
- Preserve exactly the six columns. Add no application-status/response fields and perform no external-site application submission.

## 7. Validate the workflow before enabling live use

Retain and rerun the existing unit tests. Add focused tests when implementing the new components, using synthetic jobs and candidate data rather than private resume content or live accounts.

Acceptance checks:

1. The implemented CLI starts from the terminal and loads the existing profile/preferences without changing source evidence or dates.
2. Live discovery uses a configured, authorized adapter; unsupported access fails explicitly without unauthorized fallbacks.
3. Matching explains supported/unknown requirements; unresolved questions never receive invented answers.
4. Deduplication handles source IDs, URL aliases, repeat runs, and distinct openings with similar titles.
5. Explicit application-method evidence routes each job correctly; unknown methods require review.
6. Easy Apply preparation appears in the local terminal review flow and creates no CSV. Submission success requires a platform confirmation.
7. External jobs create/update exactly one CSV at the required path, with the exact header, verified-or-blank URLs, preserved first discovery dates, and no duplicate rows or response/status fields.
8. Rejected/captcha/access-control responses stop the affected access; no bypass is attempted.
9. Private resume, candidate profile, extraction, output, and secrets remain ignored and untracked by Git. No response-tracking feature is introduced.

## Next setup deliverable

An access-capability decision documenting the chosen authorized source, permissions, available job/application fields, rate limits, and whether Easy Apply interaction is supported. This decision comes before package installation or browser implementation. Until it is available, use the working local parser, profile review, and tests without claiming that live LinkedIn discovery is operational.
