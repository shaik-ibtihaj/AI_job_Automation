"""Resumable local workflow with explicit live capability gates and final reports."""
import argparse
from collections import Counter
import csv
import io
import json
import sqlite3
from pathlib import Path
import sys

from src.application_rules import (
    UnavailableSubmission, approval_valid, default_settings, file_hash, fingerprint, platform_confirmation,
    prepare_form, save_settings, setup, validate_settings,
)
from src.discovery import LINKEDIN_BLOCKER, POLICY_SOURCES, UnavailableLinkedIn, build_searches, discover
from src.job_pipeline import (
    CSV_COLUMNS, JobPipelineError, _atomic_write, _read_json, deduplicate_jobs, job_identities,
    match_job, validate_job, validate_url, verify_profile_source,
)
from src.profile_config import PROJECT_ROOT, load_candidate_profile, load_job_preferences
from src.run_store import AlreadyRunning, PROTECTED, RunLock, Store, utcnow

EXTERNAL_COLUMNS = (
    "job_id", "job_title", "company_name", "location", "linkedin_job_url", "application_url",
    "match_score", "matching_evidence", "uncertainties", "discovered_at", "status",
)


def observed_method(raw):
    evidence = raw.get("application_method_evidence")
    method = raw.get("application_type", "unknown")
    if method == "unknown" or not isinstance(evidence, dict):
        return "unknown"
    if (evidence.get("method") != method or not evidence.get("observation")
            or not isinstance(evidence.get("observed_at"), str)):
        return "unknown"
    try:
        from datetime import datetime
        datetime.fromisoformat(evidence["observed_at"])
        validate_url(evidence.get("linkedin_job_url"), "linkedin_job_url")
        if not evidence.get("linkedin_job_url") or not raw.get("linkedin_job_url"):
            return "unknown"
        expected = set(job_identities({**raw, "job_id": ""}))
        actual = set(job_identities({**raw, "job_id": "", "linkedin_job_url": evidence["linkedin_job_url"]}))
        if not actual & expected:
            return "unknown"
    except (ValueError, TypeError, KeyError):
        return "unknown"
    return method


def ingest(store, records, profile, preferences, settings, counts):
    if not isinstance(records, list):
        raise JobPipelineError("Intake needs a jobs list.")
    prepared = []
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise JobPipelineError(f"Intake record {index + 1} is not an object.")
        job = validate_job(raw, index + 1)
        method = observed_method(raw)
        job["source_stated_application_type"] = job["application_type"]
        job["application_type"] = method
        job["application_type_evidence"] = [] if method == "unknown" else [method]
        job["method_observation"] = raw.get("application_method_evidence") if method != "unknown" else None
        if method == "external" and job["application_url"] and job["method_observation"].get("application_url") != job["application_url"]:
            job["application_url"] = ""
            job["destination_note"] = "External destination lacks matching observed evidence; no URL inferred."
        job["classification_basis"] = "observed_evidence" if method != "unknown" else "needs_observed_method_evidence"
        prepared.append(job)
    unique = deduplicate_jobs(prepared)
    counts["duplicates_in_batch"] += len(records) - len(unique)
    for job in unique:
        match = match_job(job, profile, preferences, settings["rules"]["min_score"])
        _, existed = store.upsert(job, match)
        counts["updated_existing" if existed else "new_records"] += 1


def read_intake(path, *, fixture=False):
    content = _read_json(Path(path))
    if isinstance(content, dict):
        synthetic = content.get("synthetic") is True
        records = content.get("jobs")
    else:
        synthetic, records = False, content
    if synthetic != fixture:
        raise JobPipelineError("Synthetic input must use --fixture; fixtures cannot enter live state or outputs.")
    if not isinstance(records, list):
        raise JobPipelineError("Intake file needs a jobs list.")
    return records


def migrate_legacy(root, store, accept, fixture=False):
    """Archive original exports as JSON; retain real records and isolate known demos."""
    if fixture or store.db.execute("SELECT 1 FROM metadata WHERE key='legacy_migrated'").fetchone():
        return
    output = root / "output"
    csv_path = output / "external_apply_jobs.csv"
    archived, real = {}, []
    if csv_path.exists():
        text = csv_path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != list(CSV_COLUMNS) and reader.fieldnames != list(EXTERNAL_COLUMNS):
            raise JobPipelineError("Existing CSV has an unrecognized schema; preserved without overwriting.")
        if reader.fieldnames == list(CSV_COLUMNS):
            samples = []
            sample_path = root / "data/sample_jobs.json"
            if sample_path.exists():
                sample = _read_json(sample_path)
                if isinstance(sample, dict) and sample.get("synthetic") is True:
                    samples = sample.get("jobs", [])
            for row in reader:
                if set(row) != set(CSV_COLUMNS) or any(v is None for v in row.values()):
                    raise JobPipelineError("Malformed legacy CSV; original preserved.")
                known_demo = any(all(row.get(k, "") == sample.get(k, "") for k in
                                    ("job_title", "company_name", "linkedin_job_url")) for sample in samples)
                if not known_demo:
                    real.append({**row, "source": "retained_legacy_csv", "application_type": "unknown",
                                 "job_description": "Retained prior external-job record. Requirements and current method require verification."})
            archived[csv_path.name] = text
    for name in ("easy_apply_review_queue.json", "unknown_application_jobs.json", "external_job_ids.json"):
        path = output / name
        if not path.exists():
            continue
        content = _read_json(path)
        archived[name] = content
        if name != "external_job_ids.json":
            for entry in content.get("jobs", []):
                job = entry.get("job", {})
                if job.get("source") != "synthetic_sample":
                    real.append(job)
    if archived:
        archive_path = root / "data/legacy_output_archive.json"
        if not archive_path.exists():
            _atomic_write(archive_path, json.dumps({"archived_at": utcnow(), "purpose": "Original exports, including synthetic demos; not live results.",
                                                   "files": archived}, indent=2) + "\n")
    if real:
        accept(real)
    store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('legacy_migrated','yes')")
    store.db.commit()


def _safe_cell(value):
    # Source data is retained in private JSON/SQLite. Prevent spreadsheet formulas.
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def retire_archived_identity_index(root):
    """Remove obsolete live-folder demo metadata only after verifying its archive."""
    path = root / "output/external_job_ids.json"
    archive = root / "data/legacy_output_archive.json"
    if path.exists() and archive.exists():
        archived = _read_json(archive).get("files", {}).get(path.name)
        if archived == _read_json(path):
            path.unlink()


def export_outputs(output, store):
    rows, easy, unknown, unresolved = [], [], [], []
    for saved in store.all_jobs():
        job, match = json.loads(saved["payload"]), json.loads(saved["match"])
        if saved["outcome"] == "external_application_required":
            rows.append({
                "job_id": job.get("job_id") or saved["key"], "job_title": job["job_title"],
                "company_name": job["company_name"], "location": job["location"],
                "linkedin_job_url": job["linkedin_job_url"], "application_url": job["application_url"],
                "match_score": match["score"], "matching_evidence": json.dumps(match["matched_skills"], ensure_ascii=True),
                "uncertainties": json.dumps(match["manual_review_required"], ensure_ascii=True),
                "discovered_at": saved["first_seen"], "status": saved["outcome"],
            })
        item = {"job_id": job.get("job_id") or saved["key"], "job": job, "match": match,
                "submission_status": saved["outcome"], "reason": saved["reason"],
                "preparation": json.loads(saved["preparation"]) if saved["preparation"] else None,
                "confirmation": json.loads(saved["confirmation"]) if saved["confirmation"] else None}
        if job["application_type"] == "unknown":
            unknown.append(item)
        elif job["application_type"] == "easy_apply":
            easy.append(item)
        if saved["outcome"] in {"unknown_method", "needs_input", "failed", "outcome_uncertain"}:
            unresolved.append({"job_id": item["job_id"], "reason": saved["reason"], "status": saved["outcome"]})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=EXTERNAL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: _safe_cell(v) for k, v in row.items()} for row in rows)
    _atomic_write(output / "external_apply_jobs.csv", stream.getvalue())
    _atomic_write(output / "easy_apply_review_queue.json", json.dumps({"schema_version": 2, "jobs": easy}, indent=2) + "\n")
    _atomic_write(output / "unknown_application_jobs.json", json.dumps({"schema_version": 2, "jobs": unknown}, indent=2) + "\n")
    return unresolved


def process_one(store, saved, profile, preferences, settings, root, mode, adapter, approval_ok, submission_budget, source_hashes):
    key = saved["key"]
    if saved["outcome"] in PROTECTED:
        return False
    job, match = json.loads(saved["payload"]), json.loads(saved["match"])
    # Refresh match after preference changes without changing stored identity/dates.
    match = match_job(job, profile, preferences, settings["rules"]["min_score"])
    store.db.execute("UPDATE jobs SET match=? WHERE key=?", (json.dumps(match), key))
    store.db.commit()
    if job["application_type"] == "external":
        store.outcome(key, "external_application_required", "Apply manually on the observed external site; never submitted here.")
        return False
    if job["application_type"] == "unknown":
        store.outcome(key, "unknown_method", "Application method lacks observed evidence; confirm Easy Apply or external destination.")
        return False
    if not match["eligible_for_review"]:
        store.outcome(key, "not_matched", "Below approved matching rules or excluded seniority/location; see matching explanation.")
        return False
    preparation = {"resume_path": profile["source"]["file"], "unanswered_questions": [],
                   "profile_review_items": [i for i in profile.get("manual_review_items", []) if i.get("status") == "open"]}
    if not adapter.permitted:
        preparation["unanswered_questions"] = job["screening_questions"]
        store.outcome(key, "needs_input", "Live Easy Apply form access is unavailable; questions/declarations cannot be verified.", preparation=preparation)
        return False
    snapshot = adapter.inspect(job)
    if not isinstance(snapshot, dict) or snapshot.get("job_url") != job["linkedin_job_url"]:
        store.outcome(key, "needs_input", "Actual form does not match the selected posting.", preparation=preparation)
        return False
    answers, missing = prepare_form(snapshot, settings)
    preparation["unanswered_questions"] = missing
    preparation["form_fingerprint"] = fingerprint(snapshot)
    # Values stay in private settings; queues/logs contain approved field IDs only.
    preparation["approved_answer_field_ids"] = list(answers)
    countries = [country for country in preferences["countries"] if country.casefold() in job["location"].casefold()]
    for country in countries:
        confirmed = settings["country_authorizations"].get(country, {})
        if confirmed.get("confirmed") is not True or confirmed.get("eligible") is not True:
            missing.append(f"Confirm eligibility to work in {country}; no work authorization is inferred.")
    if not approval_ok:
        missing.append("Approve the current resume, candidate profile, preferences, rules and exact reusable answers in setup.")
    if missing:
        store.outcome(key, "needs_input", "; ".join(missing), preparation=preparation)
        return False
    if mode == "dry-run":
        store.outcome(key, "dry_run_ready", "Prepared under approved settings; dry-run makes no submission.", preparation=preparation)
        return False
    if submission_budget <= 0:
        store.outcome(key, "needs_input", "Configured application limit reached; resume in a later run.", preparation=preparation)
        return False
    if any(not path.exists() or file_hash(path) != sha for path, sha in source_hashes.items()):
        store.outcome(key, "needs_input", "Approved resume/profile/preferences/settings changed during this run; review setup before submitting.", preparation=preparation)
        return False
    # Commit BEFORE side effect: a crash/timeout after this line never triggers a blind retry.
    attempt = store.begin_submission(key)
    try:
        result = adapter.submit(job=job, answers=answers, resume_path=root / profile["source"]["file"],
                                idempotency_key=attempt, expected_form_fingerprint=fingerprint(snapshot))
        proof = platform_confirmation(result, job)
        if proof:
            store.outcome(key, "submitted_with_confirmation", "Platform confirmed submission; evidence retained privately.", confirmation=proof, preparation=preparation)
            store.finish_attempt(attempt, "submitted_with_confirmation")
        else:
            store.outcome(key, "outcome_uncertain", "No correlated platform confirmation; check manually before any retry.", preparation=preparation)
            store.finish_attempt(attempt, "outcome_uncertain")
    except Exception:
        store.outcome(key, "outcome_uncertain", "Submission interaction failed after intent was persisted; check platform manually.", preparation=preparation)
        store.finish_attempt(attempt, "outcome_uncertain")
    return True


def run_workflow(root, store, profile, preferences, settings, *, mode="dry-run", discovery=None,
                 application=None, input_paths=(), fixture=False, reset_searches=False, approval_ok=False):
    output = root / "output" / "fixtures" if fixture else root / "output"
    discovery, application = discovery or UnavailableLinkedIn(), application or UnavailableSubmission()
    if fixture and (mode == "live" or discovery.permitted or application.permitted):
        raise ValueError("Fixture CLI runs never use permitted adapters or live mode.")
    run_id = store.start_run()
    initial_attempts = store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    initial_confirmed = {row["key"] for row in store.all_jobs() if row["outcome"] == "submitted_with_confirmation"}
    source_paths = [root / profile["source"]["file"], root / "data/candidate_profile.json",
                    root / "config/job_preferences.json", root / "config/application_settings.json"]
    source_hashes = {path: file_hash(path) for path in source_paths if path.exists()}
    report = {"schema_version": 1, "run_id": run_id, "started_at": utcnow(), "mode": mode,
              "data_kind": "synthetic_fixture" if fixture else "real_or_retained",
              "capabilities": {"live_discovery_verified": False, "live_submission_verified": False,
                               "discovery_adapter": discovery.name, "submission_adapter": application.name},
              "counts": {"new_records": 0, "updated_existing": 0, "duplicates_in_batch": 0,
                         "retained_records_read": 0, "submission_attempts": 0, "recovered_uncertain": store.recover_interrupted()},
              "blockers": [], "errors": [], "policy_sources": POLICY_SOURCES,
              "submission_status_is_separate_from_employer_responses": True}
    if not discovery.permitted:
        report["blockers"].append(LINKEDIN_BLOCKER)
    if not application.permitted:
        report["blockers"].append("Live Easy Apply submission not available or verified. No browser/session was attached.")
    if not approval_ok:
        report["blockers"].append("Initial approvals are absent or stale; run Start.bat --setup. No live submission is authorized.")
    accept = lambda records: ingest(store, records, profile, preferences, settings, report["counts"])
    status = "completed_with_blockers" if report["blockers"] else "completed"
    exports_allowed = False
    try:
        if reset_searches:
            store.reset_searches()
        migrate_legacy(root, store, accept, fixture)
        if not fixture:
            retire_archived_identity_index(root)
        exports_allowed = True
        paths = list(input_paths)
        retained = root / "data/linkedin_jobs_review.json"
        if not fixture and retained.exists() and retained not in paths:
            paths.append(retained)
        for path in paths:
            records = read_intake(path, fixture=fixture)
            accept(records)
            report["counts"]["retained_records_read"] += len(records)
        report["discovery"] = discover(discovery, build_searches(preferences, settings["locations"]), settings["limits"],
                                       store, lambda record: accept([record]))
        if report["discovery"]["errors"] and not report["blockers"]:
            status = "completed_with_blockers"
            report["blockers"].append("Discovery stopped incomplete; inspect coverage errors.")
        report["capabilities"]["live_discovery_verified"] = bool(discovery.permitted and report["discovery"]["pages_read"])
        for saved in store.all_jobs():
            try:
                submitted = process_one(store, saved, profile, preferences, settings, root, mode, application,
                                        approval_ok, settings["limits"]["max_applications"] - report["counts"]["submission_attempts"], source_hashes)
                report["counts"]["submission_attempts"] += int(submitted)
            except Exception as exc:
                store.outcome(saved["key"], "failed", f"Processing failed ({type(exc).__name__}); no exception text logged.")
                report["errors"].append({"job_key": saved["key"], "error_type": type(exc).__name__})
    except KeyboardInterrupt:
        status = "interrupted"
        report["errors"].append("Run stopped by user; in-flight attempts require manual confirmation.")
    except Exception as exc:
        status = "failed"
        report["errors"].append(f"Run failed ({type(exc).__name__}); input/state retained. No credentials logged.")
    finally:
        report["counts"]["recovered_uncertain"] += store.recover_interrupted()
        report["counts"]["submission_attempts"] = store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] - initial_attempts
        report["counts"]["confirmed_this_run"] = sum(row["outcome"] == "submitted_with_confirmation" and row["key"] not in initial_confirmed for row in store.all_jobs())
        if report["errors"] and status == "completed":
            status = "completed_with_errors"
        report["status"] = status
        report["finished_at"] = utcnow()
        report["outcome_totals"] = dict(Counter(row["outcome"] for row in store.all_jobs()))
        report["unresolved_items"] = export_outputs(output, store) if exports_allowed else []
        report["exports_written"] = exports_allowed
        report["output_paths"] = {name: str(output / name) for name in
                                  ("external_apply_jobs.csv", "easy_apply_review_queue.json", "unknown_application_jobs.json", "last_run_report.json", "last_run_report.txt")}
        store.finish_run(run_id, report)
        _atomic_write(output / "last_run_report.json", json.dumps(report, indent=2) + "\n")
        summary = format_report(report)
        _atomic_write(output / "last_run_report.txt", summary + "\n")
        print(summary)
    return report


def format_report(report):
    coverage = report.get("discovery", {})
    lines = [f"Run {report['run_id']} | {report['mode'].upper()} | {report['status']}",
             f"Data: {report['data_kind']}",
             f"Retained records read: {report['counts']['retained_records_read']} (not fresh discovery)",
             f"New unique records: {report['counts']['new_records']}; existing updates: {report['counts']['updated_existing']}",
             f"Live pages read: {coverage.get('pages_read', 0)}; records received: {coverage.get('records_received', 0)}",
             f"Searches completed this run: {coverage.get('searches_completed_this_run', 0)}; previously complete: {coverage.get('searches_already_completed', 0)}; planned: {coverage.get('searches_total', 0)}",
             f"Coverage incomplete: {coverage.get('incomplete', True)}; limits reached: {', '.join(coverage.get('limits_reached', [])) or 'none'}",
             f"Submission attempts this run: {report['counts']['submission_attempts']}",
             f"Platform-confirmed submissions this run: {report['counts']['confirmed_this_run']}",
             "Persisted submission/workflow outcomes: " + json.dumps(report["outcome_totals"], sort_keys=True),
             "Employer response tracking: not implemented."]
    lines += ["BLOCKED: " + reason for reason in report["blockers"]]
    lines += ["ERROR: " + (reason if isinstance(reason, str) else reason["error_type"]) for reason in report["errors"]]
    lines += [f"Needs review: {item['job_id']} | {item['status']} | {item['reason']}" for item in report["unresolved_items"]]
    lines += [f"Output: {name} -> {path}" for name, path in report["output_paths"].items()]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--reset-searches", action="store_true")
    parser.add_argument("--min-score", type=int)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        with RunLock(root / "data/.workflow.lock"):
            profile = load_candidate_profile(root / "data/candidate_profile.json")
            preferences = load_job_preferences(root / "config/job_preferences.json")
            verify_profile_source(profile, root)
            path = root / "config/application_settings.json"
            settings = _read_json(path) if path.exists() else default_settings(preferences)
            validate_settings(settings)
            if args.setup and args.non_interactive:
                raise ValueError("--setup needs an interactive terminal; remove --non-interactive.")
            needs_setup = not approval_valid(root, profile, preferences, settings)
            if args.setup or (needs_setup and not args.non_interactive and not args.fixture):
                settings = setup(root, profile, preferences, settings)
                save_settings(path, settings)
            elif not path.exists() and not args.fixture:
                save_settings(path, settings)
            mode = "live" if args.live else "dry-run" if args.dry_run else settings["mode"]
            if args.fixture:
                mode = "dry-run"
            if args.min_score is not None:
                settings["rules"]["min_score"] = args.min_score
                validate_settings(settings)
            approved = approval_valid(root, profile, preferences, settings) and mode == settings["mode"]
            db = root / ("data/fixtures/workflow.sqlite3" if args.fixture else "data/workflow.sqlite3")
            with Store(db) as store:
                report = run_workflow(root, store, profile, preferences, settings, mode=mode,
                                      input_paths=args.input, fixture=args.fixture, reset_searches=args.reset_searches, approval_ok=approved)
            return 1 if report["status"] in {"failed", "interrupted", "completed_with_errors"} else 2 if report["blockers"] else 0
    except (OSError, ValueError, AlreadyRunning, EOFError, sqlite3.Error) as exc:
        print(f"Workflow could not start ({type(exc).__name__}). Check input files, settings, dependencies and whether another run is active.", file=sys.stderr)
        if isinstance(exc, AlreadyRunning):
            print(str(exc), file=sys.stderr)
        else:
            # Do not replace a running job's report on lock contention. For local
            # preflight failures, make the failed startup distinguishable from an old run.
            detail = str(exc) if isinstance(exc, ValueError) else "Check local file permissions, input files and interactive setup availability."
            report = {"schema_version": 1, "status": "startup_failed", "finished_at": utcnow(),
                      "error_type": type(exc).__name__, "message": detail,
                      "live_discovery_verified": False, "live_submission_verified": False,
                      "submission_attempts": 0, "exports_written": False}
            print(detail, file=sys.stderr)
            try:
                output = root / ("output/fixtures" if args.fixture else "output")
                _atomic_write(output / "last_run_report.json", json.dumps(report, indent=2) + "\n")
                _atomic_write(output / "last_run_report.txt", "STARTUP FAILED: " + detail + "\nNo searches or submissions started. Previous job state was preserved.\n")
            except OSError:
                print("Could not write startup report; check output folder permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
