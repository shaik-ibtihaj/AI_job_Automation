"""Load local profile/preferences JSON and print a review summary."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigurationError(ValueError):
    """A configuration file is missing, malformed, or has invalid fields."""


def load_json_object(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot load JSON '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected a JSON object in '{path}'.")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ConfigurationError("Unsupported or missing schema_version; expected 1.")
    return value


def _require_string_list(value: dict, field: str) -> None:
    items = value.get(field)
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.strip() for x in items):
        raise ConfigurationError(f"'{field}' must be a nonempty list of strings.")


def load_job_preferences(path: str | Path | None = None) -> dict:
    value = load_json_object(path if path is not None else PROJECT_ROOT / "config/job_preferences.json")
    for field in ("roles", "countries", "seniority", "work_types", "keywords"):
        _require_string_list(value, field)
    exclusions = value.get("seniority_exclusions")
    if not isinstance(exclusions, dict):
        raise ConfigurationError("Missing seniority_exclusions policy.")
    _require_string_list(exclusions, "titles")
    explanation = exclusions.get("exception")
    if (exclusions.get("manual_review_required_for_exception") is not True
            or not isinstance(explanation, str) or not explanation.strip()):
        raise ConfigurationError("Seniority exceptions must require manual review and an explanation.")
    policy = value.get("evidence_policy")
    if not isinstance(policy, dict) or any(
        policy.get(field) is not False
        for field in ("assume_visa_sponsorship_eligibility", "assume_language_proficiency")
    ):
        raise ConfigurationError("Visa eligibility and language proficiency must not be assumed.")
    return value


def load_candidate_profile(path: str | Path | None = None) -> dict:
    value = load_json_object(path if path is not None else PROJECT_ROOT / "data/candidate_profile.json")
    for field in ("education", "work_experience", "technical_skills", "projects", "certifications", "languages", "other_qualifications", "manual_verification_required"):
        if not isinstance(value.get(field), list):
            raise ConfigurationError(f"'{field}' must be a list.")
    if not isinstance(value.get("source"), dict) or not isinstance(value.get("verification_status"), str):
        raise ConfigurationError("Candidate profile needs source metadata and verification_status.")
    return value


def format_summary(profile: dict, preferences: dict) -> str:
    lines = ["Candidate profile", f"Status: {profile['verification_status']}"]
    for field in ("education", "work_experience", "technical_skills", "projects", "certifications", "languages", "other_qualifications"):
        lines.append(f"{field.replace('_', ' ').title()}:")
        for item in profile[field]:
            lines.append(f"  - {item.get('summary', str(item)) if isinstance(item, dict) else item}")
        if not profile[field]:
            lines.append("  No resume-supported entries recorded.")
    lines.append("Manual verification required:")
    lines.extend(f"  - {item}" for item in profile["manual_verification_required"])
    lines.append("\nJob preferences")
    for field in ("roles", "countries", "seniority", "work_types", "keywords"):
        lines.append(f"{field.replace('_', ' ').title()}: {', '.join(preferences[field])}")
    lines.append("Exclude: " + ", ".join(preferences["seniority_exclusions"]["titles"]))
    lines.append("Exception: " + preferences["seniority_exclusions"]["exception"])
    lines.append("Visa eligibility and language proficiency require evidence; neither is assumed.")
    return "\n".join(lines)


def main() -> int:
    try:
        print(format_summary(load_candidate_profile(), load_job_preferences()))
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
