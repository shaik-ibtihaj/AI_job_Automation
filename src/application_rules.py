"""Local reusable approvals and submission guards. No password/session handling."""
import hashlib
import json
from pathlib import Path

from src.job_pipeline import _atomic_write, validate_url, job_identities
from src.run_store import utcnow


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def default_settings(preferences):
    return {
        "schema_version": 1, "mode": "dry-run",
        "limits": {"max_searches": 25, "max_pages_per_search": 3, "max_jobs": 100, "max_applications": 10},
        "locations": list(preferences["countries"]),
        "rules": {"min_score": 60, "require_country_authorization_confirmation": True},
        "country_authorizations": {}, "answers": {}, "approval": None,
    }


def validate_settings(settings):
    if not isinstance(settings, dict) or type(settings.get("schema_version")) is not int or settings.get("schema_version") != 1 or settings.get("mode") not in {"live", "dry-run"}:
        raise ValueError("Invalid workflow settings version or mode.")
    limits = settings.get("limits", {})
    for key in ("max_searches", "max_pages_per_search", "max_jobs", "max_applications"):
        if type(limits.get(key)) is not int or not 1 <= limits[key] <= 10000:
            raise ValueError(f"{key} must be between 1 and 10000.")
    rules = settings.get("rules", {})
    if type(rules.get("min_score")) is not int or not 0 <= rules["min_score"] <= 100:
        raise ValueError("min_score must be between 0 and 100.")
    if rules.get("require_country_authorization_confirmation") is not True:
        raise ValueError("Country authorization confirmation must remain required for live applications.")
    locations = settings.get("locations")
    if not isinstance(locations, list) or not locations or not all(isinstance(x, str) and x.strip() for x in locations):
        raise ValueError("At least one nonempty search location is required.")
    for field in ("answers", "country_authorizations"):
        if not isinstance(settings.get(field), dict):
            raise ValueError(f"{field} must be an object.")
    for country, authorization in settings["country_authorizations"].items():
        if (not isinstance(country, str) or not isinstance(authorization, dict)
                or authorization.get("confirmed") is not True or type(authorization.get("eligible")) is not bool):
            raise ValueError("Country eligibility entries require explicit confirmed=true and boolean eligible.")
    for question, answer in settings["answers"].items():
        if (not isinstance(question, str) or not question.strip() or not isinstance(answer, dict)
                or not isinstance(answer.get("value"), (str, bool))
                or answer.get("kind") not in {"answer", "consent", "declaration"}
                or answer.get("approved") is not True):
            raise ValueError("Each reusable answer needs exact question text, a value, approved=true and an explicit kind.")
        if any(word in question.casefold() for word in ("password", "session cookie", "access token")):
            raise ValueError("Passwords, session cookies and access tokens must not be stored as screening answers.")
    return settings


def approval_material(root, profile, preferences, settings):
    return {
        "resume_sha256": file_hash(root / profile["source"]["file"]),
        "profile_sha256": file_hash(root / "data/candidate_profile.json"),
        "preferences": fingerprint(preferences),
        "settings": fingerprint({k: v for k, v in settings.items() if k != "approval"}),
    }


def approval_valid(root, profile, preferences, settings):
    approval = settings.get("approval")
    return bool(isinstance(approval, dict) and approval.get("approved") is True
                and approval.get("fingerprints") == approval_material(root, profile, preferences, settings))


def save_settings(path, settings):
    validate_settings(settings)
    _atomic_write(Path(path), json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def setup(root, profile, preferences, settings, prompt=input):
    """User-run terminal setup; development/testing never invents these approvals."""
    print("Initial application approval settings (stored privately; no password requested).")
    print("Resume:", root / profile["source"]["file"])
    print("Resume SHA-256:", file_hash(root / profile["source"]["file"]))
    print("Roles:", ", ".join(preferences["roles"]))
    print("Countries:", ", ".join(preferences["countries"]))
    print("Seniority:", ", ".join(preferences["seniority"]), "| Work types:", ", ".join(preferences["work_types"]))
    print("Resume claims remain self-reported; approval is not independent verification.")
    for item in profile.get("manual_review_items", []):
        if item.get("status") == "open":
            print("Needs confirmation:", item["question"])
    mode = prompt("Mode: dry-run or live [dry-run]: ").strip() or "dry-run"
    if mode not in {"dry-run", "live"}:
        raise ValueError("Choose dry-run or live.")
    settings["mode"] = mode
    print("Live mode will remain blocked unless a supported, permitted submission integration is available.")
    locations = prompt("Search countries/locations (comma separated; Enter preserves saved preferences): ").strip()
    if locations:
        settings["locations"] = [x.strip() for x in locations.split(",") if x.strip()]
    for key, value in settings["limits"].items():
        text = prompt(f"{key} [{value}]: ").strip()
        if text:
            settings["limits"][key] = int(text)
    score = prompt(f"Minimum match score [{settings['rules']['min_score']}]: ").strip()
    if score:
        settings["rules"]["min_score"] = int(score)
    print("Country-specific authorization remains unknown unless you explicitly confirm eligibility.")
    for country in preferences["countries"]:
        answer = prompt(f"Confirmed eligible to work in {country}? yes/no/unknown [unknown]: ").strip().lower()
        if answer in {"yes", "no"}:
            settings["country_authorizations"][country] = {"confirmed": True, "eligible": answer == "yes", "basis": "candidate_confirmation"}
    print("Reusable answers are matched to exact question wording. Unknown answers stay blank.")
    while True:
        question = prompt("Exact screening question to approve (Enter finishes): ").strip()
        if not question:
            break
        kind = prompt("Kind: answer, consent, or declaration [answer]: ").strip() or "answer"
        answer = prompt("Your confirmed answer (never your LinkedIn password): ")
        if prompt(f"Approve this exact {kind} answer for reuse? Type YES: ").strip() == "YES":
            settings["answers"][question] = {"value": answer, "kind": kind, "approved": True}
    validate_settings(settings)
    print("Approved resume path:", profile["source"]["file"])
    print("Rules:", json.dumps(settings["rules"]))
    print("Limits:", json.dumps(settings["limits"]))
    print("Country confirmations:", json.dumps(settings["country_authorizations"]))
    print("Reusable answers:", json.dumps(settings["answers"], ensure_ascii=True))
    required = "ENABLE LIVE APPLICATIONS" if mode == "live" else "APPROVE DRY RUN"
    if prompt(f"Approve the shown resume, rules and answers? Type {required}: ").strip() != required:
        settings["approval"] = None
        print("Settings remain unapproved; live actions are disabled.")
    else:
        settings["approval"] = {"approved": True, "approved_at": utcnow(),
                                "fingerprints": approval_material(root, profile, preferences, settings)}
    return settings


class UnavailableSubmission:
    permitted = False
    name = "linkedin_submission_unavailable"

    def inspect(self, job):
        raise RuntimeError("No permitted LinkedIn application interaction is configured.")


def prepare_form(snapshot, settings):
    """Prepare only explicit, approved answers; changed consent wording never matches."""
    if not isinstance(snapshot, dict) or snapshot.get("method") != "easy_apply" or not isinstance(snapshot.get("questions"), list):
        return {}, ["Actual Easy Apply form/questions have not been observed."]
    answers, missing, ids = {}, [], set()
    for field in snapshot["questions"]:
        if (not isinstance(field, dict) or not isinstance(field.get("id"), str) or not field["id"]
                or field["id"] in ids or not isinstance(field.get("question"), str)
                or field.get("kind") not in {"answer", "consent", "declaration"}):
            return {}, ["Unsupported or ambiguous form field; manual inspection required."]
        ids.add(field["id"])
        approved = settings["answers"].get(field["question"])
        if not approved or approved.get("approved") is not True or approved.get("kind") != field["kind"]:
            missing.append(field["question"])
            continue
        value = approved["value"]
        if field.get("required", True) and (value is None or value == ""):
            missing.append(field["question"])
        elif "choices" in field and value not in field["choices"]:
            missing.append(field["question"] + " (approved answer is not an available choice)")
        elif field.get("acceptance_required") and value not in {True, "Yes", "yes", "YES"}:
            missing.append(field["question"] + " (required declaration not accepted)")
        else:
            answers[field["id"]] = value
    if snapshot.get("unapproved_decisions"):
        missing.append("Form contains an additional unapproved declaration, consent, or decision.")
    if snapshot.get("complete") is not True:
        missing.append("All form steps and final declarations have not been observed.")
    return answers, missing


def platform_confirmation(result, job):
    """A success flag/HTTP 200 is insufficient; require correlated platform evidence."""
    if not isinstance(result, dict):
        return None
    proof = result.get("confirmation")
    if (not isinstance(proof, dict) or proof.get("type") not in {"platform_receipt", "platform_confirmation_message"}
            or not isinstance(proof.get("message"), str) or not proof["message"].strip()
            or not isinstance(proof.get("observed_at"), str) or not proof["observed_at"]
            or not isinstance(proof.get("platform_url"), str) or proof.get("application_submitted") is not True
            or not job.get("linkedin_job_url")):
        return None
    try:
        from datetime import datetime
        validate_url(proof["platform_url"], "linkedin_job_url")
        datetime.fromisoformat(proof["observed_at"])
        proof_job = {**job, "job_id": "", "linkedin_job_url": proof["platform_url"]}
        # Compare stable posting IDs; source names and tracking parameters can vary.
        return proof if set(job_identities(proof_job)) & set(job_identities({**job, "job_id": ""})) else None
    except (ValueError, TypeError):
        return None
