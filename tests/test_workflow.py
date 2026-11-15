"""Submission/discovery tests are isolated mocks; never access LinkedIn."""
import copy
import csv
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.application_rules import (approval_material, approval_valid, default_settings,
    platform_confirmation, prepare_form, validate_settings)
from src.discovery import AccessBlocked, Page, UnavailableLinkedIn, build_searches, discover
from src.profile_config import load_job_preferences
from src.job_pipeline import JobPipelineError
from src.run_store import AlreadyRunning, RunLock, Store
from src.workflow import (EXTERNAL_COLUMNS, export_outputs, ingest, observed_method,
                          read_intake, run_workflow, main)


STAMP = "2026-11-15T10:00:00+00:00"


def record(number="9910000001", method="easy_apply", **changes):
    url = f"https://www.linkedin.com/jobs/view/{number}/"
    job = {"source": "mock_permitted_adapter", "job_id": number,
           "job_title": "Junior Data Engineer", "company_name": "Fixture Company",
           "location": "Sweden", "job_description": "Junior Python, SQL, Docker role.",
           "linkedin_job_url": url, "application_url": "https://fixture.example.test/jobs/one" if method == "external" else "",
           "application_type": method, "screening_questions": ["Work authorization?"],
           "application_method_evidence": {"method": method, "linkedin_job_url": url,
                                           "application_url": "https://fixture.example.test/jobs/one" if method == "external" else "",
                                           "observed_at": STAMP, "observation": "Explicit method observed in mock fixture."}}
    job.update(changes)
    return job


def profile():
    return {"source": {"file": "resumes/fixture.pdf"},
            "technical_skills": [{"skills": ["Python", "SQL", "Docker"],
                                  "verification_status": "resume_stated_pending_manual_review",
                                  "evidence": [{"page_number": 1, "excerpt": "Python, SQL, Docker"}]}],
            "manual_review_items": [], "work_authorization": None, "visa_sponsorship_eligibility": None}


class MockDiscovery:
    permitted = True
    name = "MOCK_discovery_no_network"

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_page(self, search, cursor):
        self.calls.append((search.key, cursor))
        page = self.pages[cursor]
        if isinstance(page, Exception):
            raise page
        return page


class MockApplication:
    permitted = True
    name = "MOCK_submission_no_network"

    def __init__(self, result="confirmed", questions=None):
        self.result, self.calls, self.inspections = result, [], []
        self.questions = questions if questions is not None else [
            {"id": "authorization", "question": "Work authorization?", "kind": "answer", "required": True}]

    def inspect(self, job):
        self.inspections.append(job)
        return {"method": "easy_apply", "job_url": job["linkedin_job_url"], "complete": True,
                "questions": copy.deepcopy(self.questions)}

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        if self.result == "confirmed":
            return {"confirmation": {"type": "platform_confirmation_message", "application_submitted": True,
                    "platform_url": kwargs["job"]["linkedin_job_url"], "observed_at": STAMP,
                    "message": "Your application was submitted.", "receipt_reference": "fixture-confirmation"}}
        return self.result


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "resumes").mkdir()
        (self.root / "config").mkdir()
        (self.root / "resumes/fixture.pdf").write_bytes(b"synthetic-not-a-real-resume")
        self.preferences, self.profile = load_job_preferences(), profile()
        (self.root / "data/candidate_profile.json").write_text(json.dumps(self.profile))
        self.settings = default_settings(self.preferences)
        self.settings["mode"] = "live"
        self.settings["country_authorizations"]["Sweden"] = {"confirmed": True, "eligible": True}
        self.settings["answers"] = {"Work authorization?": {"kind": "answer", "value": "Yes", "approved": True}}
        self.store = Store(self.root / "data/state.sqlite3")
        self.addCleanup(self.store.db.close)
        self.counts = {"new_records": 0, "updated_existing": 0, "duplicates_in_batch": 0}

    def ingest(self, jobs):
        ingest(self.store, jobs, self.profile, self.preferences, self.settings, self.counts)

    def run_flow(self, application=None, mode="live", **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_workflow(self.root, self.store, self.profile, self.preferences, self.settings,
                                application=application, mode=mode, approval_ok=True, **kwargs)

    def status(self):
        return self.store.all_jobs()[0]["outcome"]

    def test_explicit_observed_method_required(self):
        job = record()
        self.assertEqual(observed_method(job), "easy_apply")
        job.pop("application_method_evidence")
        self.assertEqual(observed_method(job), "unknown")
        self.ingest([job])
        self.run_flow()
        self.assertEqual(self.status(), "unknown_method")

    def test_wrong_job_or_invalid_observation_is_unknown(self):
        for evidence in ({"method": "external"}, {"method": "easy_apply", "observation": "observed",
                "observed_at": STAMP, "linkedin_job_url": "https://www.linkedin.com/jobs/view/123/"}):
            self.assertEqual(observed_method(record(application_method_evidence=evidence)), "unknown")

    def test_all_routes_and_expanded_csv_only_external(self):
        self.ingest([record(), record("9910000002", "external"), record("9910000003", "unknown")])
        self.run_flow()
        with (self.root / "output/external_apply_jobs.csv").open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, list(EXTERNAL_COLUMNS))
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "external_application_required")
        self.assertTrue(json.loads(rows[0]["matching_evidence"]))
        self.assertEqual(len(list((self.root / "output").glob("*.csv"))), 1)

    def test_unknown_screening_answer_skips_job_and_continues(self):
        self.ingest([record(), record("9910000002", "external")])
        self.settings["answers"] = {}
        app = MockApplication()
        report = self.run_flow(app)
        self.assertEqual(app.calls, [])
        self.assertEqual(report["outcome_totals"]["needs_input"], 1)
        self.assertEqual(report["outcome_totals"]["external_application_required"], 1)

    def test_dry_run_never_calls_submission(self):
        self.ingest([record()])
        app = MockApplication()
        self.run_flow(app, mode="dry-run")
        self.assertEqual(app.calls, [])
        self.assertEqual(self.status(), "dry_run_ready")

    def test_approval_absence_blocks_live_submission(self):
        self.ingest([record()])
        app = MockApplication()
        with redirect_stdout(io.StringIO()):
            run_workflow(self.root, self.store, self.profile, self.preferences, self.settings,
                         application=app, mode="live", approval_ok=False)
        self.assertEqual(app.calls, [])
        self.assertEqual(self.status(), "needs_input")

    def test_unknown_country_eligibility_blocks_without_guessing(self):
        self.ingest([record()])
        self.settings["country_authorizations"] = {}
        app = MockApplication()
        self.run_flow(app)
        self.assertEqual(app.calls, [])
        self.assertEqual(self.status(), "needs_input")

    def test_confirmed_submission_persists_evidence_and_does_not_repeat(self):
        self.ingest([record()])
        app = MockApplication()
        first = self.run_flow(app)
        self.assertEqual(self.status(), "submitted_with_confirmation")
        self.assertEqual(first["counts"]["confirmed_this_run"], 1)
        self.assertIn("fixture-confirmation", self.store.all_jobs()[0]["confirmation"])
        self.ingest([record(job_title="Graduate Data Engineer")])
        second = self.run_flow(app)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(second["counts"]["submission_attempts"], 0)
        self.assertEqual(second["counts"]["confirmed_this_run"], 0)

    def test_ambiguous_success_flag_is_not_confirmation_or_retried(self):
        self.ingest([record()])
        app = MockApplication(result={"success": True, "http_status": 200})
        self.run_flow(app)
        self.assertEqual(self.status(), "outcome_uncertain")
        self.run_flow(app)
        self.assertEqual(len(app.calls), 1)

    def test_timeout_after_submit_is_uncertain_and_redacted(self):
        self.ingest([record()])
        app = MockApplication(result=TimeoutError("secret-token-must-not-be-logged"))
        report = self.run_flow(app)
        self.assertEqual(self.status(), "outcome_uncertain")
        self.assertNotIn("secret-token", json.dumps(report))

    def test_interrupted_submission_recovered_without_retry(self):
        self.ingest([record()])
        app = MockApplication(result=KeyboardInterrupt())
        report = self.run_flow(app)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["counts"]["submission_attempts"], 1)
        self.assertEqual(self.status(), "outcome_uncertain")
        next_app = MockApplication()
        self.run_flow(next_app)
        self.assertEqual(next_app.calls, [])

    def test_reopen_database_after_crash_prevents_retry(self):
        self.ingest([record()])
        key = self.store.all_jobs()[0]["key"]
        self.store.begin_submission(key)
        with Store(self.root / "data/state.sqlite3") as restarted:
            self.assertEqual(restarted.recover_interrupted(), 1)
            self.assertEqual(restarted.get(key)["outcome"], "outcome_uncertain")
            with self.assertRaises(RuntimeError):
                restarted.begin_submission(key)

    def test_duplicate_urls_and_ids_across_runs_and_bridge(self):
        a = record(job_id="alpha", linkedin_job_url="")
        a["application_type"] = "unknown"
        self.ingest([a, record(job_id="beta")])
        self.ingest([record(job_id="alpha")])
        self.assertEqual(len(self.store.all_jobs()), 1)
        self.assertEqual(self.counts["updated_existing"], 1)

    def test_repeat_external_updates_keep_original_discovery_time(self):
        self.ingest([record(method="external")])
        first_time = self.store.all_jobs()[0]["first_seen"]
        self.run_flow()
        self.ingest([record(method="external", job_title="Graduate Data Engineer", application_url="")])
        self.run_flow()
        with (self.root / "output/external_apply_jobs.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["discovered_at"], first_time)
        self.assertEqual(rows[0]["application_url"], "https://fixture.example.test/jobs/one")

    def test_missing_external_url_is_empty(self):
        self.ingest([record(method="external", application_url="")])
        self.run_flow()
        with (self.root / "output/external_apply_jobs.csv").open(newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["application_url"], "")
        self.assertTrue(row["linkedin_job_url"])

    def test_job_limit_does_not_submit_beyond_approved_count(self):
        self.ingest([record(), record("9910000002")])
        self.settings["limits"]["max_applications"] = 1
        app = MockApplication()
        report = self.run_flow(app)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(report["outcome_totals"]["needs_input"], 1)

    def test_unapproved_consent_and_new_declarations_are_not_answered(self):
        self.ingest([record()])
        app = MockApplication(questions=[{"id": "consent", "question": "I accept new terms", "kind": "consent"}])
        self.run_flow(app)
        self.assertEqual(app.calls, [])
        self.assertEqual(self.status(), "needs_input")

    def test_preparation_failure_does_not_stop_other_jobs_or_leak_secret(self):
        class BrokenInspect(MockApplication):
            def inspect(self, job):
                raise ValueError("password-private")
        self.ingest([record(), record("9910000002", "external")])
        report = self.run_flow(BrokenInspect())
        self.assertEqual(report["outcome_totals"]["failed"], 1)
        self.assertEqual(report["outcome_totals"]["external_application_required"], 1)
        self.assertNotIn("password-private", json.dumps(report))

    def test_resume_change_during_inspection_invalidates_live_preparation(self):
        root = self.root
        class ChangedResume(MockApplication):
            def inspect(self, job):
                result = super().inspect(job)
                (root / "resumes/fixture.pdf").write_bytes(b"unapproved replacement")
                return result
        self.ingest([record()])
        app = ChangedResume()
        self.run_flow(app)
        self.assertEqual(app.calls, [])
        self.assertEqual(self.status(), "needs_input")

    def test_default_capability_blocker_is_accurate_not_fake_discovery(self):
        report = self.run_flow()
        self.assertEqual(report["discovery"]["pages_read"], 0)
        self.assertFalse(report["capabilities"]["live_discovery_verified"])
        self.assertFalse(report["capabilities"]["live_submission_verified"])
        self.assertTrue(report["blockers"])

    def test_retained_real_listings_are_loaded_without_manual_input(self):
        path = self.root / "data/linkedin_jobs_review.json"
        content = json.dumps({"jobs": [record(method="unknown")]})
        path.write_text(content)
        report = self.run_flow()
        self.assertEqual(report["counts"]["retained_records_read"], 1)
        self.assertEqual(report["discovery"]["records_received"], 0)
        self.assertEqual(path.read_text(), content)

    def test_fixtures_cannot_contaminate_live_input_or_outputs(self):
        path = self.root / "data/fixture.json"
        path.write_text(json.dumps({"synthetic": True, "jobs": [record()]}))
        with self.assertRaises(JobPipelineError):
            read_intake(path)
        with redirect_stdout(io.StringIO()):
            report = run_workflow(self.root, self.store, self.profile, self.preferences, self.settings,
                input_paths=[path], fixture=True, mode="dry-run")
        self.assertEqual(report["data_kind"], "synthetic_fixture")
        self.assertFalse((self.root / "output/external_apply_jobs.csv").exists())
        self.assertTrue((self.root / "output/fixtures/external_apply_jobs.csv").exists())

    def test_invalid_existing_csv_is_preserved_on_failure(self):
        output = self.root / "output"
        output.mkdir()
        path = output / "external_apply_jobs.csv"
        path.write_text("not,a,recognized,schema\n")
        report = self.run_flow()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["exports_written"])
        self.assertEqual(path.read_text(), "not,a,recognized,schema\n")

    def test_legacy_synthetic_csv_archived_as_json_not_live_results(self):
        output = self.root / "output"
        output.mkdir()
        demo = record(method="external")
        (self.root / "data/sample_jobs.json").write_text(json.dumps({"synthetic": True, "jobs": [demo]}))
        from src.job_pipeline import CSV_COLUMNS
        with (output / "external_apply_jobs.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerow({k: demo.get(k, "2026-11-10") for k in CSV_COLUMNS})
        self.run_flow()
        self.assertEqual(self.store.all_jobs(), [])
        self.assertTrue((self.root / "data/legacy_output_archive.json").exists())

    def test_obsolete_demo_index_retired_only_when_exactly_archived(self):
        from src.workflow import retire_archived_identity_index
        output = self.root / "output"
        output.mkdir()
        path = output / "external_job_ids.json"
        path.write_text('{"demo": 1}')
        archive = self.root / "data/legacy_output_archive.json"
        archive.write_text(json.dumps({"files": {path.name: {"demo": 2}}}))
        retire_archived_identity_index(self.root)
        self.assertTrue(path.exists())
        archive.write_text(json.dumps({"files": {path.name: {"demo": 1}}}))
        retire_archived_identity_index(self.root)
        self.assertFalse(path.exists())
        self.assertTrue(archive.exists())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.db")
        self.addCleanup(self.store.db.close)
        self.searches = build_searches(load_job_preferences())[:2]
        self.limits = {"max_jobs": 10, "max_searches": 2, "max_pages_per_search": 2}

    def test_searches_include_all_saved_filters(self):
        search = self.searches[0]
        self.assertTrue(search.role and search.location and search.seniority and search.work_types and search.keywords)

    def test_pagination_and_completed_search_checkpoint(self):
        adapter = MockDiscovery({None: Page([record()], "two"), "two": Page([record("9910000002")])})
        received = []
        report = discover(adapter, self.searches, self.limits, self.store, received.append)
        self.assertEqual(report["searches_completed_this_run"], 2)
        self.assertEqual(report["pages_read"], 4)
        self.assertFalse(report["incomplete"])
        again = discover(adapter, self.searches, self.limits, self.store, received.append)
        self.assertEqual(again["searches_already_completed"], 2)
        self.assertEqual(again["pages_read"], 0)

    def test_page_limit_resumes_next_cursor(self):
        adapter = MockDiscovery({None: Page([record()], "next"), "next": Page([])})
        self.limits["max_pages_per_search"] = 1
        self.limits["max_searches"] = 1
        first = discover(adapter, self.searches, self.limits, self.store, lambda record: None)
        self.assertIn("max_pages_per_search", first["limits_reached"])
        self.assertTrue(first["incomplete"])
        discover(adapter, self.searches, self.limits, self.store, lambda record: None)
        self.assertEqual(adapter.calls[1][1], "next")

    def test_partial_page_job_limit_replays_page_instead_of_losing_records(self):
        self.limits["max_jobs"] = 1
        adapter = MockDiscovery({None: Page([record(), record("9910000002")])})
        report = discover(adapter, self.searches[:1], self.limits, self.store, lambda record: None)
        self.assertIn("max_jobs", report["limits_reached"])
        self.assertFalse(self.store.search_progress(self.searches[0].key).get("complete"))
        received = []
        again = discover(adapter, self.searches[:1], self.limits, self.store, received.append)
        self.assertEqual(received[0]["job_id"], "9910000002")
        self.assertEqual(again["searches_completed_this_run"], 1)

    def test_rate_limit_or_challenge_stops_without_retry_and_redacts_tokens(self):
        adapter = MockDiscovery({None: AccessBlocked("CAPTCHA cookie=secret")})
        report = discover(adapter, self.searches, self.limits, self.store, lambda record: None)
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(report["incomplete"])
        self.assertNotIn("cookie=secret", json.dumps(report))

    def test_repeated_cursor_stops_incomplete(self):
        adapter = MockDiscovery({None: Page([], "same"), "same": Page([], "same")})
        self.limits["max_pages_per_search"] = 10
        report = discover(adapter, self.searches, self.limits, self.store, lambda record: None)
        self.assertTrue(report["errors"])
        self.assertEqual(len(adapter.calls), 2)

    def test_changed_partial_page_does_not_silently_skip_records(self):
        self.limits["max_jobs"] = 1
        adapter = MockDiscovery({None: Page([record(), record("9910000002")])})
        discover(adapter, self.searches[:1], self.limits, self.store, lambda record: None)
        adapter.pages[None] = Page([record("9910000003"), record("9910000002")])
        accepted = []
        report = discover(adapter, self.searches[:1], self.limits, self.store, accepted.append)
        self.assertEqual(accepted, [])
        self.assertTrue(report["errors"])
        self.assertTrue(report["incomplete"])


class ApprovalAndSafetyTests(unittest.TestCase):
    def test_exact_question_and_kind_and_all_steps_are_required(self):
        settings = default_settings(load_job_preferences())
        settings["answers"] = {"I accept terms": {"value": "Yes", "kind": "answer", "approved": True}}
        form = {"method": "easy_apply", "complete": True, "questions": [
            {"id": "one", "question": "I accept terms", "kind": "consent"}]}
        self.assertTrue(prepare_form(form, settings)[1])
        settings["answers"]["I accept terms"]["kind"] = "consent"
        self.assertFalse(prepare_form(form, settings)[1])
        form["complete"] = False
        self.assertTrue(prepare_form(form, settings)[1])

    def test_confirmation_requires_correlated_linkedin_platform_evidence(self):
        job = record()
        valid = MockApplication().submit(job=job)
        self.assertTrue(platform_confirmation(valid, job))
        for patch_values in ({"platform_url": "https://evil.test/jobs/view/9910000001/"},
                             {"platform_url": "https://www.linkedin.com/jobs/view/123/"},
                             {"application_submitted": False}, {"message": ""}, {"observed_at": "bad"}):
            bad = copy.deepcopy(valid)
            bad["confirmation"].update(patch_values)
            self.assertIsNone(platform_confirmation(bad, job))

    def test_approval_invalidated_by_resume_profile_preferences_answers_rules_or_mode_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "resumes").mkdir()
            (root / "resumes/fixture.pdf").write_bytes(b"fixture")
            (root / "data/candidate_profile.json").write_text("{}")
            p, preferences = profile(), load_job_preferences()
            settings = default_settings(preferences)
            settings["approval"] = {"approved": True, "fingerprints": approval_material(root, p, preferences, settings)}
            self.assertTrue(approval_valid(root, p, preferences, settings))
            for key, value in (("mode", "live"), ("answers", {"new": "answer"}), ("rules", {"min_score": 90})):
                modified = copy.deepcopy(settings)
                modified[key] = value
                self.assertFalse(approval_valid(root, p, preferences, modified))
            changed = copy.deepcopy(preferences)
            changed["roles"].append("Different Role")
            self.assertFalse(approval_valid(root, p, changed, settings))
            (root / "data/candidate_profile.json").write_text('{"changed":true}')
            self.assertFalse(approval_valid(root, p, preferences, settings))
            (root / "data/candidate_profile.json").write_text("{}")
            (root / "resumes/fixture.pdf").write_bytes(b"changed")
            self.assertFalse(approval_valid(root, p, preferences, settings))

    def test_simultaneous_process_run_lock_and_crash_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            code = "from src.run_store import RunLock,AlreadyRunning; import sys\ntry:\n with RunLock(sys.argv[1]): pass\nexcept AlreadyRunning: sys.exit(2)"
            with RunLock(path):
                result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
            # The filename still exists; stale filenames must not prevent restart.
            with RunLock(path):
                pass

    def test_additional_form_decision_and_unavailable_choices_block(self):
        settings = default_settings(load_job_preferences())
        settings["answers"] = {"Question": {"value": "Yes", "kind": "answer", "approved": True}}
        form = {"method": "easy_apply", "complete": True, "questions": [
            {"id": "q", "question": "Question", "kind": "answer", "choices": ["No"]}]}
        self.assertTrue(prepare_form(form, settings)[1])
        form["questions"][0]["choices"] = ["Yes"]
        form["unapproved_decisions"] = True
        self.assertTrue(prepare_form(form, settings)[1])

    def test_credentials_cannot_be_saved_as_reusable_answers(self):
        settings = default_settings(load_job_preferences())
        settings["answers"] = {"LinkedIn password?": {"value": "secret", "kind": "answer", "approved": True}}
        with self.assertRaises(ValueError):
            validate_settings(settings)

    def test_initial_setup_only_approves_explicit_shown_settings(self):
        from src.application_rules import setup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "resumes").mkdir()
            (root / "resumes/fixture.pdf").write_bytes(b"fixture")
            (root / "data/candidate_profile.json").write_text("{}")
            p, preferences = profile(), load_job_preferences()
            responses = iter([""] * 13 + ["APPROVE DRY RUN"])
            with redirect_stdout(io.StringIO()):
                settings = setup(root, p, preferences, default_settings(preferences), prompt=lambda _: next(responses))
            self.assertEqual(settings["answers"], {})
            self.assertEqual(settings["country_authorizations"], {})
            self.assertEqual(settings["mode"], "dry-run")
            self.assertTrue(approval_valid(root, p, preferences, settings))

    def test_startup_error_writes_report_without_overwriting_job_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = main(["--root", str(root), "--non-interactive"])
            self.assertEqual(status, 1)
            report = json.loads((root / "output/last_run_report.json").read_text())
            self.assertEqual(report["status"], "startup_failed")
            self.assertFalse((root / "output/external_apply_jobs.csv").exists())


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".venv/Scripts").mkdir(parents=True)
        (self.root / ".venv/Scripts/python.exe").touch()

    def test_existing_environment_runs_without_installing(self):
        from src import bootstrap
        with patch.object(bootstrap, "ROOT", self.root), patch.object(bootstrap.subprocess, "run") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 2)]
            self.assertEqual(bootstrap.main(["--non-interactive"]), 2)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(any("pip" in call.args[0] for call in run.call_args_list))

    def test_dependency_failure_stops_before_workflow_with_clear_error(self):
        from src import bootstrap
        output = io.StringIO()
        with patch.object(bootstrap, "ROOT", self.root), patch.object(bootstrap.subprocess, "run") as run, redirect_stdout(output):
            run.side_effect = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 1)]
            self.assertEqual(bootstrap.main([]), 1)
            self.assertEqual(run.call_count, 2)
            self.assertIn("Dependency setup failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
