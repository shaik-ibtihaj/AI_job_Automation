"""Local job intake and review preparation. No network access or submissions."""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.profile_config import (
    ConfigurationError, PROJECT_ROOT, load_candidate_profile, load_job_preferences,
)

CSV_COLUMNS = ("job_title", "company_name", "application_url", "linkedin_job_url", "location", "date_found")
APPLICATION_TYPES = {"easy_apply", "external", "unknown"}
TRACKING_PARAMETERS = {"trk", "trackingid", "refid", "ref", "lipi"}


class JobPipelineError(ValueError):
    """Invalid input or output that requires correction before processing."""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JobPipelineError(f"Cannot read JSON '{path}': {exc}") from exc


def _text(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.casefold()).strip()


def _contains(text: str, phrase: str) -> bool:
    return bool(_text(phrase)) and f" {_text(phrase)} " in f" {_text(text)} "


def validate_url(value, field: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise JobPipelineError(f"{field} must be an HTTP(S) URL without whitespace, or blank.")
    try:
        parts = urlsplit(value)
        _ = parts.port  # Validate a supplied port without opening a connection.
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Invalid HTTP(S) URL")
        if field == "linkedin_job_url":
            host = parts.hostname.lower()
            if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
                raise ValueError("Expected a LinkedIn host")
            if not parts.path.startswith("/jobs/view/"):
                raise ValueError("Expected an actual /jobs/view/ posting URL")
    except ValueError as exc:
        raise JobPipelineError(f"Invalid {field}: {exc}") from exc
    return value


def normalize_url(value: str) -> str:
    """Normalize identity only; exported URLs remain source values.

    Only known tracking parameters are removed. Functional query values,
    path casing, and application tokens are preserved.
    """
    if not value:
        return ""
    validate_url(value, "URL")
    parts = urlsplit(value)
    host = parts.hostname.lower()
    port = parts.port
    if port and not (parts.scheme == "https" and port == 443 or parts.scheme == "http" and port == 80):
        host += f":{port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMETERS]
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def validate_job(record: dict, number: int = 1) -> dict:
    if not isinstance(record, dict):
        raise JobPipelineError(f"Job {number} must be an object.")
    job = {}
    for field in ("job_title", "company_name", "location", "job_description"):
        value = record.get(field)
        if not isinstance(value, str) or (field != "location" and not value.strip()):
            raise JobPipelineError(f"Job {number}: {field} must be a {'nonempty ' if field != 'location' else ''}string.")
        job[field] = value.strip()
    for field in ("linkedin_job_url", "application_url"):
        job[field] = validate_url(record.get(field), field)
    method = record.get("application_type", "unknown")
    if not isinstance(method, str) or method not in APPLICATION_TYPES:
        raise JobPipelineError(f"Job {number}: application_type must be easy_apply, external, or unknown.")
    job["application_type"] = method
    if method == "external" and job["application_url"]:
        host = urlsplit(job["application_url"]).hostname.lower()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            job["application_url"] = ""
    job["application_type_evidence"] = [method] if method != "unknown" else []
    job["classification_basis"] = "source_stated" if "application_type" in record else "not_supplied"
    for field in ("source", "job_id"):
        value = record.get(field, "local_input" if field == "source" else "")
        if not isinstance(value, str):
            raise JobPipelineError(f"Job {number}: {field} must be a string.")
        job[field] = value.strip()
    if not job["source"]:
        raise JobPipelineError(f"Job {number}: source must be nonempty.")
    questions = record.get("screening_questions", [])
    if not isinstance(questions, list) or not all(isinstance(q, str) and q.strip() for q in questions):
        raise JobPipelineError(f"Job {number}: screening_questions must be a list of nonempty strings.")
    job["screening_questions"] = list(dict.fromkeys(q.strip() for q in questions))
    job["screening_questions_supplied"] = "screening_questions" in record
    job["identity_aliases"] = job_identities(job)
    return job


def load_jobs(path: str | Path) -> list[dict]:
    value = _read_json(Path(path))
    # An envelope can identify fixtures or the authorized adapter's provenance.
    if isinstance(value, dict):
        value = value.get("jobs")
    if not isinstance(value, list):
        raise JobPipelineError("Input must be a list of jobs or an object with a jobs list.")
    for i, record in enumerate(value, start=1):
        validate_job(record, i)
    # Leave optional-field presence intact for process_jobs to normalize once.
    return value


def job_identities(job: dict) -> list[str]:
    aliases = []
    if job.get("job_id"):
        aliases.append("source:" + json.dumps([job["source"], job["job_id"]], ensure_ascii=True))
    link = job.get("linkedin_job_url", "")
    if link:
        path = urlsplit(link).path.rstrip("/").split("/")[-1]
        match = re.search(r"(?:^|-)(\d+)$", path)
        aliases.append("linkedin:" + match.group(1) if match else "posting:" + normalize_url(link))
    # Shared company portals do not establish identity across different postings.
    if not aliases and job.get("application_url"):
        aliases.append("application:" + json.dumps([
            normalize_url(job["application_url"]), job["job_title"], job["company_name"], job["location"]
        ]))
    if not aliases:
        content = [job.get(f, "") for f in ("job_title", "company_name", "location", "job_description")]
        aliases.append("exact_record:" + hashlib.sha256(json.dumps(content).encode()).hexdigest())
    return aliases


def deduplicate_jobs(jobs: list[dict]) -> list[dict]:
    """Merge overlapping aliases, including transitive duplicates and conflicts."""
    groups: list[dict] = []
    for source_job in jobs:
        merged = copy.deepcopy(source_job)
        # A bridge record can connect two previously separate groups.
        while True:
            overlap = next((j for j, other in enumerate(groups)
                            if set(merged["identity_aliases"]) & set(other["identity_aliases"])), None)
            if overlap is None:
                break
            previous = groups.pop(overlap)
            for field in ("identity_aliases", "screening_questions", "application_type_evidence"):
                merged[field] = list(dict.fromkeys(previous[field] + merged[field]))
            for field in ("application_url", "linkedin_job_url", "job_id"):
                merged[field] = merged[field] or previous[field]
            merged["screening_questions_supplied"] |= previous["screening_questions_supplied"]
        methods = merged["application_type_evidence"]
        merged["application_type"] = methods[0] if len(methods) == 1 else "unknown"
        if len(methods) > 1:
            merged["classification_basis"] = "conflicting_source_methods_manual_review"
        groups.append(merged)
    return groups


def classify_application(job: dict) -> str:
    """Classify only from explicit intake evidence, never from job prose."""
    return job["application_type"]


def supported_skills(profile: dict) -> list[dict]:
    """Require literal resume evidence; do not promote self-reports to verification."""
    supported = []
    seen = set()
    for group in profile["technical_skills"]:
        if not isinstance(group, dict) or group.get("verification_status") in {"rejected", "unverified", "unsupported"}:
            continue
        for skill in group.get("skills", []):
            if not isinstance(skill, str) or _text(skill) in seen:
                continue
            evidence = [e for e in group.get("evidence", []) if isinstance(e, dict)
                        and type(e.get("page_number")) is int and e["page_number"] > 0
                        and isinstance(e.get("excerpt"), str) and _contains(e["excerpt"], skill)]
            if evidence:
                supported.append({"skill": skill, "verification_status": group.get("verification_status", "resume_stated"),
                                  "evidence": evidence})
                seen.add(_text(skill))
    return supported


def match_job(job: dict, profile: dict, preferences: dict, min_score: int = 50) -> dict:
    title, description, location = job["job_title"], job["job_description"], job["location"]
    combined = title + " " + description
    skills = supported_skills(profile)
    matched = [s for s in skills if _contains(combined, s["skill"])
               or s["skill"] == "ETL/ELT" and (_contains(combined, "ETL") or _contains(combined, "ELT"))]
    role_matches = [r for r in preferences["roles"] if _contains(title, r)
                    or r == "ML Engineer" and _contains(title, "Machine Learning Engineer")
                    or r == "AI Engineer" and _contains(title, "Artificial Intelligence Engineer")]
    excluded = [r for r in preferences["seniority_exclusions"]["titles"] if _contains(title, r)
                or r == "Senior" and bool(re.search(r"\bsr\.?\b", title, re.I))]
    junior = [s for s in preferences["seniority"] if _contains(title, s) or _contains(description, s)]
    countries = [c for c in preferences["countries"] if _contains(location, c)]
    # No city-to-country inference and no global eligibility inference from 'Remote'.
    points = {"skills": min(40, len(matched) * 8), "title": 30 if role_matches else 0,
              "seniority": 15 if junior and not excluded else 0, "location": 15 if countries else 0}
    review = []
    if matched and any(s["verification_status"] != "independently_verified" for s in matched):
        review.append("Skill evidence is resume-supported, not independently verified; confirm proficiency before answering screening questions.")
    if not countries:
        review.append("Preferred country is not established by the location text; confirm work location and remote-country restrictions.")
    if not junior:
        review.append("Preferred junior/entry-level/graduate seniority is not explicit; review actual requirements.")
    if excluded:
        review.append("Excluded senior title requires a genuine requirements match and manual exception approval; it is not automatically queued.")
    if profile.get("work_authorization") is None or profile.get("visa_sponsorship_eligibility") is None:
        review.append("Work authorization and/or visa sponsorship eligibility is unknown. No eligibility is assumed.")
    if re.search(r"\b(language|fluent|fluency|native|cefr|swedish|german|finnish|norwegian|polish)\b", description, re.I):
        review.append("Check source language requirements against current, confirmed proficiency; no language equivalence is assumed.")
    if re.search(r"\b\d+\s*\+?\s*years?\b", description, re.I):
        review.append("The posting mentions years of experience. No experience total is inferred from overlapping roles.")
    if job["application_type"] == "external" and not job["application_url"]:
        review.append("Direct application URL is missing; retain the source LinkedIn URL without inventing a destination.")
    if not job["linkedin_job_url"]:
        review.append("LinkedIn posting URL is missing; confirm the posting before using it.")
    score = sum(points.values())
    eligible = score >= min_score and bool(role_matches) and bool(countries) and not excluded
    reasons = [f"Skills: {points['skills']}/40 ({len(matched)} evidenced keyword matches; 8 points each, capped at 40).",
               f"Title: {points['title']}/30; matched preferences: {', '.join(role_matches) or 'none'}.",
               f"Seniority: {points['seniority']}/15; preferred markers: {', '.join(junior) or 'none'}.",
               f"Location: {points['location']}/15; explicit preferred countries: {', '.join(countries) or 'none'}."]
    return {"score": score, "max_score": 100, "components": points, "matched_skills": matched,
            "explanation": reasons, "manual_review_required": review,
            "eligible_for_review": eligible, "eligibility_status": "not_determined",
            "review_threshold": min_score, "excluded_seniority": excluded,
            "note": "Keyword relevance is not proof that all requirements or eligibility conditions are satisfied."}


def verify_profile_source(profile: dict, root: Path) -> str:
    """Verify the local source fingerprint and skill quotes before using them."""
    source = profile["source"]
    if not isinstance(source.get("file"), str) or not source.get("sha256") or not source.get("extraction_artifact"):
        raise JobPipelineError("Profile requires resume file, sha256, and extraction_artifact metadata.")
    resume = root / source["file"]
    try:
        fingerprint = hashlib.sha256(resume.read_bytes()).hexdigest()
    except OSError as exc:
        raise JobPipelineError(f"Cannot read profile resume: {exc}") from exc
    extraction = _read_json(root / source["extraction_artifact"])
    if not isinstance(extraction, dict) or source["sha256"] != fingerprint or extraction.get("sha256") != fingerprint:
        raise JobPipelineError("Resume/profile/extraction fingerprints differ. Re-extract and review the profile.")
    try:
        pages = {p["page_number"]: p["text"] for p in extraction["pages"]}
        for group in profile["technical_skills"]:
            for item in group.get("evidence", []):
                if not item["excerpt"] or item["excerpt"] not in pages[item["page_number"]]:
                    raise ValueError("A skill quote does not match the extraction")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise JobPipelineError(f"Invalid skill source evidence: {exc}") from exc
    return source["file"]


def _csv_rows(path: Path) -> tuple[list[dict], bytes]:
    if not path.exists():
        return [], b""
    try:
        content = path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != list(CSV_COLUMNS):
            raise JobPipelineError("Existing external CSV has an invalid header; it was not overwritten.")
        rows = list(reader)
        for row in rows:
            if set(row) != set(CSV_COLUMNS) or any(v is None for v in row.values()):
                raise JobPipelineError("Existing CSV contains malformed rows.")
            if not row["job_title"].strip() or not row["company_name"].strip():
                raise JobPipelineError("Existing CSV is missing a job title or company.")
            if date.fromisoformat(row["date_found"]).isoformat() != row["date_found"]:
                raise ValueError("Expected YYYY-MM-DD")
            validate_url(row["application_url"], "application_url")
            validate_url(row["linkedin_job_url"], "linkedin_job_url")
        return rows, content
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise JobPipelineError(f"Cannot safely update external CSV: {exc}") from exc


def prepare_external_csv(jobs: list[dict], output_dir: Path, today: str) -> tuple[str, dict, int]:
    """Retain one CSV; a private identity index handles stable IDs without URLs."""
    rows, previous_bytes = _csv_rows(output_dir / "external_apply_jobs.csv")
    index_path = output_dir / "external_job_ids.json"
    if index_path.exists():
        index = _read_json(index_path)
        if (not isinstance(index, dict) or index.get("schema_version") != 1
                or index.get("csv_sha256") != hashlib.sha256(previous_bytes).hexdigest()
                or not isinstance(index.get("row_identities"), list)
                or len(index["row_identities"]) != len(rows)):
            raise JobPipelineError("External CSV and identity index disagree; restore a consistent pair before updating.")
        identities = index["row_identities"]
        if not all(isinstance(a, list) and a and all(isinstance(s, str) for s in a) for a in identities):
            raise JobPipelineError("Invalid external CSV identity index.")
    else:
        identities = [job_identities(row) for row in rows]
    # Also clean duplicate rows already present in a valid external CSV.
    clean_rows, clean_ids = [], []
    for row, aliases in zip(rows, identities):
        row, aliases = dict(row), list(aliases)
        while True:
            position = next((i for i, known in enumerate(clean_ids) if set(aliases) & set(known)), None)
            if position is None:
                break
            previous = clean_rows.pop(position)
            aliases = list(dict.fromkeys(clean_ids.pop(position) + aliases))
            row["date_found"] = min(row["date_found"], previous["date_found"])
            for field in ("application_url", "linkedin_job_url"):
                row[field] = row[field] or previous[field]
        clean_rows.append(row)
        clean_ids.append(aliases)
    rows, identities = clean_rows, clean_ids
    for job in jobs:
        aliases = set(job["identity_aliases"])
        matches = [i for i, known in enumerate(identities) if aliases & set(known)]
        if job["application_type"] != "external":
            # A newly supplied classification must not leave a non-external row behind.
            for i in reversed(matches):
                rows.pop(i)
                identities.pop(i)
            continue
        old = [rows[i] for i in matches]
        row = {field: job.get(field, "") for field in CSV_COLUMNS}
        row["date_found"] = min([today] + [r["date_found"] for r in old])
        for field in ("application_url", "linkedin_job_url"):
            row[field] = row[field] or next((r[field] for r in old if r[field]), "")
        all_aliases = list(dict.fromkeys(job["identity_aliases"] + [a for i in matches for a in identities[i]]))
        for i in reversed(matches):
            rows.pop(i)
            identities.pop(i)
        rows.append(row)
        identities.append(all_aliases)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = stream.getvalue()
    index = {"schema_version": 1, "csv_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
             "row_identities": identities,
             "purpose": "Posting deduplication only; contains no application or response status."}
    return content, index, len(rows)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".job_pipeline_", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def process_jobs(records: list[dict], profile: dict, preferences: dict, *, resume_path: str,
                 output_dir: str | Path, min_score: int = 50, today: str | None = None) -> dict:
    """Reusable intake for records supplied by a separately authorized adapter.

    Callers must verify profile source integrity before this function. This engine
    trusts intake provenance; it neither grants provider authorization nor fetches URLs.
    JSON queues are snapshots of this input. The external CSV accumulates across runs.
    """
    if not isinstance(records, list):
        raise JobPipelineError("Job records must be a list.")
    if type(min_score) is not int or not 0 <= min_score <= 100:
        raise JobPipelineError("min_score must be an integer between 0 and 100.")
    today = today or datetime.now(timezone.utc).date().isoformat()
    try:
        if date.fromisoformat(today).isoformat() != today:
            raise ValueError("Expected YYYY-MM-DD")
    except (ValueError, TypeError) as exc:
        raise JobPipelineError("today must be a YYYY-MM-DD date.") from exc
    normalized = [validate_job(record, i) for i, record in enumerate(records, start=1)]
    jobs = deduplicate_jobs(normalized)
    output_dir = Path(output_dir)
    easy, unknown, results = [], [], []
    open_items = [{k: item[k] for k in ("id", "question", "affected_fields") if k in item}
                  for item in profile.get("manual_review_items", []) if item.get("status") == "open"]
    for job in jobs:
        match = match_job(job, profile, preferences, min_score)
        results.append({"job_title": job["job_title"], "company_name": job["company_name"],
                        "application_type": classify_application(job), "match": match})
        entry = {"job": {k: v for k, v in job.items() if k != "identity_aliases"}, "match": match,
                 "resume_path": resume_path, "profile_review_items": open_items,
                 "unanswered_screening_questions": [{"question": q, "answer": None} for q in job["screening_questions"]]}
        if not job["screening_questions_supplied"]:
            entry["screening_questions_note"] = "Not supplied by source; unknown, not assumed to be absent. Inspect the form manually."
        if job["application_type"] == "easy_apply" and match["eligible_for_review"]:
            entry["review_state"] = "needs_manual_review"
            easy.append(entry)
        elif job["application_type"] == "unknown":
            entry["review_state"] = "needs_application_method_confirmation"
            unknown.append(entry)
    # Validate existing exports before creating or replacing any outputs.
    csv_text, identity_index, csv_count = prepare_external_csv(jobs, output_dir, today)
    outputs = {
        "external_apply_jobs.csv": csv_text,
        "external_job_ids.json": json.dumps(identity_index, indent=2, ensure_ascii=False) + "\n",
        "easy_apply_review_queue.json": json.dumps({"schema_version": 1, "jobs": easy}, indent=2, ensure_ascii=False) + "\n",
        "unknown_application_jobs.json": json.dumps({"schema_version": 1, "jobs": unknown}, indent=2, ensure_ascii=False) + "\n",
    }
    for name, text in outputs.items():
        _atomic_write(output_dir / name, text)
    return {"input_count": len(records), "unique_count": len(jobs), "duplicates_removed": len(records) - len(jobs),
            "external_csv_rows": csv_count, "easy_apply_review_count": len(easy), "unknown_review_count": len(unknown),
            "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Local JSON containing authorized or synthetic jobs")
    parser.add_argument("--min-score", type=int, default=50, help="Easy Apply review threshold, 0-100 (default: 50)")
    args = parser.parse_args(argv)
    try:
        profile = load_candidate_profile()
        preferences = load_job_preferences()
        resume_path = verify_profile_source(profile, PROJECT_ROOT)
        jobs = _read_json(args.input)
        if isinstance(jobs, dict):
            if jobs.get("synthetic") is True:
                print("SYNTHETIC INPUT: sample jobs and URLs are fictional and are not fetched.")
            jobs = jobs.get("jobs")
        result = process_jobs(jobs, profile, preferences, resume_path=resume_path,
                              output_dir=PROJECT_ROOT / "output", min_score=args.min_score)
    except (JobPipelineError, ConfigurationError, OSError) as exc:
        print(f"Job processing failed: {exc}", file=sys.stderr)
        return 1
    print(f"Processed {result['input_count']} records; {result['unique_count']} unique; {result['duplicates_removed']} duplicates removed.")
    print(f"External CSV rows: {result['external_csv_rows']}; Easy Apply review: {result['easy_apply_review_count']}; unknown method: {result['unknown_review_count']}.")
    for entry in result["results"]:
        match = entry["match"]
        print(f"{entry['job_title']} | {entry['company_name']} | {entry['application_type']} | relevance {match['score']}/100")
        for reason in match["explanation"] + match["manual_review_required"]:
            print(f"  {reason}")
    print("Output: output/external_apply_jobs.csv, easy_apply_review_queue.json, unknown_application_jobs.json, external_job_ids.json")
    print("Review only. No network requests, form answers, submissions, or response tracking performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
