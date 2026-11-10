# AI-Job-Automation

AI-Job-Automation is a terminal-started Python application for discovering relevant LinkedIn jobs through an authorized data-access method, matching them to my candidate profile and preferences, and preparing applications for my review.

## Planned features

- **Discover and match LinkedIn jobs:** Use authorized job data with `data/candidate_profile.json` and `config/job_preferences.json`.
- **Deduplicate and classify:** Identify unique postings and confirm whether their application method is Easy Apply, external, or unknown.
- **Prepare Easy Apply for review:** Use the resume and confirmed, evidence-supported candidate information in a terminal review workflow for submission on LinkedIn. No separate Easy Apply CSV is created.
- **Export external applications:** Create or update the single file `output/external_apply_jobs.csv` with verified application links.

Application-response tracking, such as interviews, rejections, offers, and email monitoring, is outside this stage's scope.

## Project status

This stage provides local PDF text extraction, candidate profile storage, job preferences, and configuration validation. Job search and application submission are not implemented.

The required workflow below is a specification for future implementation. This documentation update installs no packages, creates no job exports, and adds no browser automation. See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the implementation sequence and acceptance criteria.

## Required workflow

1. Start the application from the terminal. Load the candidate profile, preferences, and resume, and check source fingerprints and unresolved manual review items.
2. Discover LinkedIn jobs through a configured, authorized data source. Use profile-supported skills and the preferred roles, countries, seniority, work types, and keywords to evaluate matches. Do not send the private resume or full candidate profile to the discovery provider.
3. Remove duplicate postings and verify each application method using explicit source information. Do not infer Easy Apply from the job title, employer, or presence of a LinkedIn URL. Keep unknown application methods pending clarification in the terminal.
4. For Easy Apply jobs, prepare the resume selection, evidence-supported field values, and screening questions in a local review queue. Use only candidate-confirmed facts for answers; resume-stated information is not automatically independently verified. Leave unknown or disputed answers unresolved and request review. Submission occurs on LinkedIn through a separately authorized interaction method, or by the user manually if such access is unavailable.
5. For jobs confirmed to require an external application, create or update only `output/external_apply_jobs.csv`. These jobs are handed off for application on company websites; this workflow does not submit external applications.
6. Review before submission. Preparation and opening a form do not establish that an application was submitted. A platform confirmation is required before reporting submission success; absent confirmation, report the outcome as unconfirmed. Do not implement application-response tracking.

The future terminal entry point will be specified during implementation. The currently runnable commands below are the resume parser, profile summary, and tests.

## External application CSV contract

The only CSV produced by this workflow is `output/external_apply_jobs.csv`, with these columns in this order:

```csv
job_title,company_name,application_url,linkedin_job_url,location,date_found
```

| Column | Meaning |
|---|---|
| `job_title` | Job title supplied by the authorized source. |
| `company_name` | Employer name supplied by the source. |
| `application_url` | Verified direct company application URL, including an employer-designated ATS link, when available. Otherwise use a verified external application destination; leave blank if none is known. |
| `linkedin_job_url` | Actual LinkedIn posting URL supplied or verified by the source. Never construct one from a title or employer name. |
| `location` | Location or remote designation as supplied; do not infer a country. |
| `date_found` | First discovery date in UTC, formatted `YYYY-MM-DD`; preserve it when updating an existing row. |

Prefer the direct application destination over redirects when that destination is available from authorized data. Never invent URLs or bypass access controls to resolve them. Update existing rows without duplicating them across runs; preserve distinct openings with similar titles. Missing URLs remain blank and are surfaced for manual review. Do not create per-run CSV files, an Easy Apply CSV, or application-status columns. A CSV row records discovery, not submission.

## Next technical setup step: authorized source access

Select and validate a source that explicitly permits automated discovery of LinkedIn jobs and supplies the required job fields, LinkedIn posting URLs, application methods, and external destinations where available. Confirm its access terms, authentication/scopes, rate limits, and permitted uses before choosing an integration library. A third-party provider must have authorization for the underlying data; an advertised scraping service is not sufficient.

LinkedIn describes restrictions on third-party scraping and website automation in its [automated activity guidance](https://www.linkedin.com/help/linkedin/answer/a1340567/automated-activity-on-linkedin?lang=en). Its documented [Job Posting API](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview) supports authorized posting integrations; it must not be assumed to provide applicant-side job search or Easy Apply submission access.

Record discovery permissions separately from any permission to interact with Easy Apply. If a suitable source is unavailable, report the missing capability and pause live discovery; an explicitly permitted local import may support development but does not fulfill automatic discovery. Do not work around restrictions, captchas, or access controls. No accounts are connected during this documentation stage.

## Project structure

```text
AI_job_Automation/
|-- src/                 # resume_parser.py and profile_config.py
|-- config/              # job_preferences.json
|-- data/                # Private job and application data (ignored by Git)
|-- resumes/             # Private resume files (ignored by Git)
|-- output/              # Future external_apply_jobs.csv; private and ignored
|-- tests/               # Application tests
|-- .venv/               # Local Python virtual environment (ignored by Git)
|-- .gitignore
|-- requirements.txt     # pypdf dependency
|-- IMPLEMENTATION_PLAN.md # High-level workflow implementation plan
`-- README.md
```

Each project folder contains a `.gitkeep` placeholder so Git can preserve the empty structure. Keep private resumes in `resumes/`, application data in `data/`, and generated files in `output/`. Their contents are ignored except for the placeholders. Local databases, `.env` files, and Python caches are also ignored.

## Windows setup

Prerequisites: Python 3.11 or later and Git available in your terminal.

Open PowerShell in the project folder. The initial setup has already created `.venv` and initialized the local Git repository. To recreate the environment if needed:

```powershell
python -m venv .venv
```

Activate the environment:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

If PowerShell blocks activation, use the environment's interpreter directly; no execution policy change is required:

```powershell
.\.venv\Scripts\python.exe --version
```

Install the PDF reader into the environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If downloads fail with WinError 10013 in a restricted session, run installation in your own local PowerShell terminal. Do not treat skipped PDF tests as successful extraction verification.

Verify the environment and repository:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print('Virtual environment:', sys.prefix != sys.base_prefix)"
git rev-parse --show-toplevel
git status --short
```

To stage the initial project files and folder placeholders for your first commit:

```powershell
git add .gitignore README.md requirements.txt src/.gitkeep data/.gitkeep resumes/.gitkeep output/.gitkeep tests/.gitkeep
```

Use `deactivate` to leave an activated environment. No external accounts, GitHub repository, or application submissions are configured.

## Resume extraction and candidate profile

Place the private resume at `resumes/master_resume.pdf`, then run locally:

```powershell
.\.venv\Scripts\python.exe -m src.resume_parser resumes/master_resume.pdf --output data/resume_extraction.json
.\.venv\Scripts\python.exe -m src.profile_config
```

Extraction writes each page's text, page numbers, a SHA-256 source fingerprint, and warnings to the ignored evidence artifact. Missing files, invalid PDFs, locked documents, and entirely textless PDFs produce explicit errors and a nonzero exit status. Textless pages in mixed documents produce manual review warnings. The parser performs no OCR and never fills gaps. See the [pypdf documentation](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) for text extraction limitations.

Compare extracted text with the original PDF. Populate `data/candidate_profile.json` only with resume-supported education/degrees, experience, skills, projects, and other qualifications. Each entry should include a readable `summary` and `evidence` with a page number and exact excerpt. Resume claims are not independently verified. Keep ambiguous details in `manual_verification_required`; do not infer skills, dates, certifications, total years of experience, language levels, or visa eligibility.

Extraction does not overwrite the curated candidate profile. If the PDF changes, re-extract and review against its fingerprint. Check the profile's `verification_status`: a blocked profile with empty lists contains no candidate facts and does not establish absence of qualifications.

## Job preferences

`config/job_preferences.json` stores the requested roles, countries, Junior/Entry-level/Graduate seniority, work types, and keywords. Senior, Lead, Principal, and Director roles are excluded unless actual requirements genuinely match resume-supported qualifications and receive manual review. Visa sponsorship eligibility and language proficiency are not assumed. These are stored policies only; no job-search engine is implemented.

## Tests and privacy checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git check-ignore resumes/master_resume.pdf data/candidate_profile.json data/resume_extraction.json
git ls-files
```

Tests use synthetic PDFs and candidate data, never private resume content. They cover text extraction, multiple pages, blank/scanned-style pages, malformed files, encryption, configuration loading, and policies against unsupported eligibility/language assumptions. PDF extraction tests are explicitly skipped if `pypdf` is unavailable.

Resume processing runs locally. There are no resume uploads, external account connections, LinkedIn integrations, or application submissions. Installing dependencies downloads packages without sending the resume.

Ignore rules cover private folder contents, extracted resume artifacts, `.env` secrets, key files, application record folders, databases, and caches. Keep private artifacts in the designated folders. Ignore rules do not untrack previously committed files; review `git ls-files` and `git status` before committing. Staging may require your own terminal when the sandbox denies writes to `.git/index.lock`.
