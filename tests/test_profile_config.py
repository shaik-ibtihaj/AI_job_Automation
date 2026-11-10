import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.profile_config import (
    ConfigurationError, format_summary, load_candidate_profile,
    load_job_preferences, load_json_object,
)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return self.path

    def test_project_preferences_match_requested_values(self):
        preferences = load_job_preferences()
        self.assertEqual(preferences["roles"], ["Data Engineer", "AI Engineer", "Software Engineer", "Full Stack Developer", "ML Engineer"])
        self.assertEqual(preferences["countries"], ["Sweden", "Finland", "Norway", "Germany", "Poland"])
        self.assertEqual(preferences["seniority"], ["Junior", "Entry-level", "Graduate"])
        self.assertEqual(preferences["work_types"], ["On-site", "Hybrid", "Remote"])
        self.assertEqual(preferences["keywords"], ["Python", "SQL", "ETL", "Data Engineering", "Machine Learning", "AI", "REST API", "Software Development"])
        self.assertEqual(preferences["seniority_exclusions"]["titles"], ["Senior", "Lead", "Principal", "Director"])

    def test_missing_file(self):
        with self.assertRaises(ConfigurationError):
            load_json_object(self.path)

    def test_invalid_json(self):
        self.path.write_text("{unfinished", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_json_object(self.path)

    def test_json_must_be_object(self):
        with self.assertRaises(ConfigurationError):
            load_json_object(self.write([]))

    def test_schema_version_is_required(self):
        for version in (None, 2, True):
            with self.subTest(version=version), self.assertRaises(ConfigurationError):
                load_json_object(self.write({"schema_version": version}))

    def test_invalid_preference_fields(self):
        preferences = load_job_preferences()
        for invalid in (None, [], "Python", [1], [""]):
            with self.subTest(invalid=invalid):
                value = copy.deepcopy(preferences)
                value["roles"] = invalid
                with self.assertRaises(ConfigurationError):
                    load_job_preferences(self.write(value))

    def test_seniority_exception_requires_review(self):
        preferences = load_job_preferences()
        preferences["seniority_exclusions"]["manual_review_required_for_exception"] = False
        with self.assertRaises(ConfigurationError):
            load_job_preferences(self.write(preferences))

    def test_seniority_exception_requires_readable_explanation(self):
        preferences = load_job_preferences()
        preferences["seniority_exclusions"]["exception"] = 123
        with self.assertRaises(ConfigurationError):
            load_job_preferences(self.write(preferences))

    def test_rejects_eligibility_and_language_assumptions(self):
        for field in ("assume_visa_sponsorship_eligibility", "assume_language_proficiency"):
            for invalid in (True, None, 0):
                with self.subTest(field=field, invalid=invalid):
                    preferences = load_job_preferences()
                    preferences["evidence_policy"][field] = invalid
                    with self.assertRaises(ConfigurationError):
                        load_job_preferences(self.write(preferences))

    def test_utf8_bom_is_supported(self):
        self.path.write_text('{"schema_version": 1}', encoding="utf-8-sig")
        self.assertEqual(load_json_object(self.path), {"schema_version": 1})

    def test_candidate_profile_requires_list_sections(self):
        with self.assertRaises(ConfigurationError):
            load_candidate_profile(self.write({"schema_version": 1}))

    def test_synthetic_candidate_profile_and_summary(self):
        profile = {
            "schema_version": 1, "source": {"file": "synthetic.pdf"},
            "verification_status": "resume_stated_pending_manual_review",
            **{field: [] for field in ("education", "work_experience", "technical_skills", "projects", "certifications", "languages", "other_qualifications")},
            "manual_verification_required": ["Confirm synthetic source."],
        }
        loaded = load_candidate_profile(self.write(profile))
        summary = format_summary(loaded, load_job_preferences())
        self.assertIn("Confirm synthetic source.", summary)
        self.assertIn("Sweden, Finland, Norway, Germany, Poland", summary)
        self.assertIn("No resume-supported entries recorded.", summary)


if __name__ == "__main__":
    unittest.main()
