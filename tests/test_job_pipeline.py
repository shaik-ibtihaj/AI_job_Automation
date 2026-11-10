import copy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO

from src.job_pipeline import (
    CSV_COLUMNS, JobPipelineError, classify_application, deduplicate_jobs,
    load_jobs, main, match_job, normalize_url, process_jobs, validate_job,
    verify_profile_source,
)
from src.profile_config import load_job_preferences


def profile_fixture():
    return {
        "technical_skills": [{"skills": ["Python", "SQL", "Docker", "Kubernetes"],
                              "verification_status": "resume_stated_pending_manual_review",
                              "evidence": [{"page_number": 1, "excerpt": "Skills: Python, SQL, Docker."}]}],
        "work_authorization": None, "visa_sponsorship_eligibility": None,
        "manual_review_items": [{"id": "synthetic-review", "status": "open", "question": "Confirm synthetic skill proficiency.", "affected_fields": ["/technical_skills"]}],
    }


def job_fixture(method="external", job_id="one", **changes):
    job = {
        "source": "synthetic_test", "job_id": job_id,
        "job_title": "Junior Data Engineer", "company_name": "Fictional Company",
        "location": "Sweden", "job_description": "Junior Python, SQL, Docker position.",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/9900000001/",
        "application_type": method, "application_url": "https://example.test/jobs/one",
    }
    job.update(changes)
    return job


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_fixture()
        self.preferences = load_job_preferences()

    def match(self, **changes):
        return match_job(validate_job(job_fixture(**changes)), self.profile, self.preferences)

    def test_explainable_score_and_no_invented_skills(self):
        result = self.match(job_description="Junior Python SQL Docker Kubernetes Java position.")
        self.assertEqual(result["components"], {"skills": 24, "title": 30, "seniority": 15, "location": 15})
        self.assertEqual(result["score"], 84)
        self.assertEqual([s["skill"] for s in result["matched_skills"]], ["Python", "SQL", "Docker"])
        self.assertEqual(len(result["explanation"]), 4)
        self.assertTrue(result["eligible_for_review"])
        self.assertEqual(result["eligibility_status"], "not_determined")

    def test_word_boundaries_avoid_sql_in_mysql(self):
        result = self.match(job_description="Junior MySQL role")
        self.assertEqual(result["matched_skills"], [])

    def test_preferences_do_not_invent_candidate_qualifications(self):
        self.profile["technical_skills"] = []
        self.assertEqual(self.match()["components"]["skills"], 0)

    def test_skill_without_evidence_or_with_rejected_status_gets_no_points(self):
        self.profile["technical_skills"][0]["evidence"] = []
        self.assertEqual(self.match()["components"]["skills"], 0)
        self.profile = profile_fixture()
        self.profile["technical_skills"][0]["verification_status"] = "rejected"
        self.assertEqual(self.match()["components"]["skills"], 0)

    def test_senior_and_sr_titles_need_manual_exception(self):
        for title in ("Senior Data Engineer", "Sr. Data Engineer", "Lead Data Engineer"):
            with self.subTest(title=title):
                result = self.match(job_title=title)
                self.assertFalse(result["eligible_for_review"])
                self.assertTrue(result["excluded_seniority"])

    def test_country_unknown_or_outside_preferences_is_not_review_eligible(self):
        for location in ("Remote", "Stockholm", "Toronto, Canada", ""):
            with self.subTest(location=location):
                result = self.match(location=location)
                self.assertEqual(result["components"]["location"], 0)
                self.assertFalse(result["eligible_for_review"])
                self.assertTrue(result["manual_review_required"])

    def test_unrelated_role_is_not_eligible(self):
        self.assertFalse(self.match(job_title="Junior Sales Associate")["eligible_for_review"])

    def test_uncertain_authorization_language_and_years_are_flagged(self):
        result = self.match(job_description="Junior Python position, 3+ years, fluent German.")
        review = " ".join(result["manual_review_required"])
        self.assertIn("visa", review)
        self.assertIn("language", review)
        self.assertIn("experience total", review)
        self.assertIn("not independently verified", review)

    def test_ml_title_alias_and_skill_cap(self):
        result = self.match(job_title="Graduate Machine Learning Engineer")
        self.assertEqual(result["components"]["title"], 30)
        self.assertLessEqual(result["score"], 100)


class IntakeAndIdentityTests(unittest.TestCase):
    def test_classification_uses_explicit_method_only(self):
        for method in ("easy_apply", "external", "unknown"):
            with self.subTest(method=method):
                job = validate_job(job_fixture(method, job_description="This prose says Easy Apply."))
                self.assertEqual(classify_application(job), method)
        job = job_fixture()
        del job["application_type"]
        self.assertEqual(classify_application(validate_job(job)), "unknown")

    def test_normalization_preserves_functional_query_tokens(self):
        value = normalize_url("https://EXAMPLE.test/Jobs/One/?token=a&job=7&utm_source=sample#section")
        self.assertEqual(value, "https://example.test/Jobs/One?job=7&token=a")

    def test_duplicate_linkedin_id_across_domains_and_tracking(self):
        a = validate_job(job_fixture(job_id="", linkedin_job_url="https://se.linkedin.com/jobs/view/role-9900000001?trk=x"))
        b = validate_job(job_fixture(job_id="different", linkedin_job_url="https://www.linkedin.com/jobs/view/9900000001/"))
        self.assertEqual(len(deduplicate_jobs([a, b])), 1)

    def test_stable_id_without_links_deduplicates(self):
        a = validate_job(job_fixture(linkedin_job_url="", application_url=""))
        b = validate_job(job_fixture(linkedin_job_url="", application_url="", job_title="Updated Junior Data Engineer"))
        self.assertEqual(len(deduplicate_jobs([a, b])), 1)

    def test_transitive_aliases_merge_and_conflicts_go_to_unknown(self):
        a = validate_job(job_fixture("external", "alpha", linkedin_job_url=""))
        b = validate_job(job_fixture("easy_apply", "beta"))
        bridge = validate_job(job_fixture("external", "alpha"))
        result = deduplicate_jobs([a, b, bridge])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["application_type"], "unknown")
        self.assertIn("conflicting", result[0]["classification_basis"])

    def test_distinct_postings_are_not_deduplicated_by_title(self):
        a = validate_job(job_fixture(job_id="alpha"))
        b = validate_job(job_fixture(job_id="beta", linkedin_job_url="https://www.linkedin.com/jobs/view/9900000002"))
        self.assertEqual(len(deduplicate_jobs([a, b])), 2)

    def test_invalid_records_and_links(self):
        invalid = [None, [], {**job_fixture(), "job_title": ""}, {**job_fixture(), "company_name": 42},
                   {**job_fixture(), "job_description": None}, {**job_fixture(), "location": None},
                   {**job_fixture(), "application_type": "easy"}, {**job_fixture(), "application_url": "javascript:bad"},
                   {**job_fixture(), "application_url": "https://a.test:bad"},
                   {**job_fixture(), "linkedin_job_url": "https://linkedin.com.evil.test/jobs/view/1"},
                   {**job_fixture(), "screening_questions": [None]}, {**job_fixture(), "job_id": True}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(JobPipelineError):
                validate_job(value)

    def test_load_list_and_envelope_and_reject_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            for content in ([job_fixture()], {"jobs": [job_fixture()]}):
                path.write_text(json.dumps(content), encoding="utf-8")
                self.assertEqual(len(load_jobs(path)), 1)
            for content in ('{"jobs": 12}', '[] trailing', '{}'):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(JobPipelineError):
                    load_jobs(path)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.profile = profile_fixture()
        self.preferences = load_job_preferences()

    def run_pipeline(self, jobs, **kwargs):
        return process_jobs(jobs, self.profile, self.preferences, resume_path="resumes/synthetic.pdf",
                            output_dir=self.output, **kwargs)

    def rows(self):
        with (self.output / "external_apply_jobs.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, list(CSV_COLUMNS))
            return list(reader)

    def test_all_routes_only_one_csv_and_no_answers_or_submissions(self):
        result = self.run_pipeline([
            job_fixture(),
            job_fixture("easy_apply", "two", linkedin_job_url="https://www.linkedin.com/jobs/view/9900000002", screening_questions=["Can you work here?"]),
            job_fixture("unknown", "three", linkedin_job_url="https://www.linkedin.com/jobs/view/9900000003"),
        ])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(list(self.output.glob("*.csv"))), 1)
        easy = json.loads((self.output / "easy_apply_review_queue.json").read_text())
        item = easy["jobs"][0]
        self.assertEqual(item["resume_path"], "resumes/synthetic.pdf")
        self.assertEqual(item["unanswered_screening_questions"], [{"question": "Can you work here?", "answer": None}])
        self.assertTrue(item["profile_review_items"])
        self.assertNotIn("submitted", json.dumps(easy))
        self.assertEqual(result["unknown_review_count"], 1)
        unknown = json.loads((self.output / "unknown_application_jobs.json").read_text())
        self.assertEqual(unknown["jobs"][0]["review_state"], "needs_application_method_confirmation")

    def test_missing_direct_url_retains_linkedin_and_empty_application_url(self):
        self.run_pipeline([job_fixture(application_url=None)])
        self.assertEqual(self.rows()[0]["application_url"], "")
        self.assertEqual(self.rows()[0]["linkedin_job_url"], job_fixture()["linkedin_job_url"])

    def test_linkedin_destination_is_not_exported_as_a_direct_company_url(self):
        self.run_pipeline([job_fixture(application_url=job_fixture()["linkedin_job_url"])])
        self.assertEqual(self.rows()[0]["application_url"], "")

    def test_missing_both_links_are_not_invented(self):
        self.run_pipeline([job_fixture(linkedin_job_url="", application_url="")])
        row = self.rows()[0]
        self.assertEqual((row["application_url"], row["linkedin_job_url"]), ("", ""))

    def test_repeated_updates_preserve_first_date_and_known_url(self):
        self.run_pipeline([job_fixture()], today="2026-11-10")
        self.run_pipeline([job_fixture(application_url="", job_title="Graduate Data Engineer")], today="2026-11-11")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_found"], "2026-11-10")
        self.assertEqual(rows[0]["job_title"], "Graduate Data Engineer")
        self.assertEqual(rows[0]["application_url"], job_fixture()["application_url"])

    def test_stable_ids_without_links_survive_repeat_runs_and_title_changes(self):
        jobs = [job_fixture(job_id=i, linkedin_job_url="", application_url="") for i in ("alpha", "beta")]
        self.run_pipeline(jobs, today="2026-11-10")
        jobs[0]["job_title"] = "Graduate Data Engineer"
        self.run_pipeline(jobs, today="2026-11-11")
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["date_found"] == "2026-11-10" for row in self.rows()))

    def test_reclassification_removes_nonexternal_from_csv(self):
        self.run_pipeline([job_fixture()])
        self.run_pipeline([job_fixture("easy_apply")])
        self.assertEqual(self.rows(), [])

    def test_unknown_screening_questions_are_not_assumed_absent(self):
        self.run_pipeline([job_fixture("easy_apply")])
        queue = json.loads((self.output / "easy_apply_review_queue.json").read_text())
        self.assertIn("Not supplied", queue["jobs"][0]["screening_questions_note"])

    def test_low_relevance_and_senior_easy_apply_not_queued(self):
        result = self.run_pipeline([job_fixture("easy_apply", job_title="Senior Data Engineer")])
        self.assertEqual(result["easy_apply_review_count"], 0)
        self.assertTrue(result["results"][0]["match"]["excluded_seniority"])

    def test_invalid_intake_leaves_existing_outputs_unchanged(self):
        self.run_pipeline([job_fixture()])
        snapshots = {p.name: p.read_bytes() for p in self.output.iterdir()}
        for invalid in ([job_fixture(), {"bad": "record"}], None):
            with self.assertRaises(JobPipelineError):
                self.run_pipeline(invalid)
        self.assertEqual(snapshots, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_malformed_csv_or_stale_index_is_not_overwritten(self):
        self.run_pipeline([job_fixture()])
        path = self.output / "external_apply_jobs.csv"
        path.write_text("bad,header\n", encoding="utf-8")
        with self.assertRaises(JobPipelineError):
            self.run_pipeline([job_fixture()])
        self.assertEqual(path.read_text(), "bad,header\n")
        path.write_text(",".join(CSV_COLUMNS) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(JobPipelineError, "identity index disagree"):
            self.run_pipeline([job_fixture()])

    def test_csv_quoting_and_unicode_round_trip(self):
        self.run_pipeline([job_fixture(company_name='Fictional, "Company"', location='G\u00f6teborg, Sweden')])
        row = self.rows()[0]
        self.assertEqual(row["company_name"], 'Fictional, "Company"')
        self.assertEqual(row["location"], 'G\u00f6teborg, Sweden')

    def test_existing_csv_duplicates_are_removed_without_losing_first_date(self):
        path = self.output / "external_apply_jobs.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            row = {key: job_fixture().get(key, "") for key in CSV_COLUMNS}
            writer.writerow({**row, "date_found": "2026-11-09"})
            writer.writerow({**row, "date_found": "2026-11-10"})
        self.run_pipeline([], today="2026-11-11")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["date_found"], "2026-11-09")

    def test_load_then_process_preserves_unknown_screening_question_state(self):
        path = self.output / "input.json"
        path.write_text(json.dumps([job_fixture("easy_apply")]), encoding="utf-8")
        self.run_pipeline(load_jobs(path))
        queue = json.loads((self.output / "easy_apply_review_queue.json").read_text())
        self.assertIn("screening_questions_note", queue["jobs"][0])

    def test_invalid_threshold_and_date_are_rejected(self):
        for kwargs in ({"min_score": -1}, {"min_score": True}, {"today": "not-a-date"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(JobPipelineError):
                self.run_pipeline([], **kwargs)


class SourceIntegrityTests(unittest.TestCase):
    def test_profile_fingerprint_and_exact_skill_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "synthetic.pdf"
            resume.write_bytes(b"synthetic source fingerprint fixture")
            sha = hashlib.sha256(resume.read_bytes()).hexdigest()
            extraction = root / "extraction.json"
            extraction.write_text(json.dumps({"sha256": sha, "pages": [{"page_number": 1, "text": "Skills: Python, SQL, Docker."}]}))
            profile = profile_fixture()
            profile["source"] = {"file": resume.name, "sha256": sha, "extraction_artifact": extraction.name}
            self.assertEqual(verify_profile_source(profile, root), resume.name)
            changed = copy.deepcopy(profile)
            changed["technical_skills"][0]["evidence"][0]["excerpt"] = "Invented skill"
            with self.assertRaises(JobPipelineError):
                verify_profile_source(changed, root)
            resume.write_bytes(b"changed")
            with self.assertRaisesRegex(JobPipelineError, "fingerprints differ"):
                verify_profile_source(profile, root)

    def test_cli_bad_input_has_nonzero_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            error = StringIO()
            with redirect_stderr(error):
                status = main(["--input", str(Path(directory) / "missing.json")])
            self.assertEqual(status, 1)
            self.assertIn("Job processing failed", error.getvalue())


if __name__ == "__main__":
    unittest.main()
