"""Discovery contracts and honest coverage accounting; no LinkedIn scraping."""
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Protocol


LINKEDIN_BLOCKER = (
    "Live LinkedIn access unavailable: no permitted applicant discovery/submission connector "
    "is configured. LinkedIn prohibits third-party website automation. Existing browser login "
    "alone does not grant automation permission. No account was accessed."
)
POLICY_SOURCES = [
    "https://www.linkedin.com/help/linkedin/answer/a1341543",
    "https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions",
    "https://learn.microsoft.com/en-us/linkedin/talent/apply-connect",
]


class AccessBlocked(RuntimeError):
    """Access restriction, missing supported integration, CAPTCHA, or rate limit."""
    def __init__(self, message, reason_code="access_restricted"):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class Search:
    role: str
    location: str
    seniority: tuple[str, ...]
    work_types: tuple[str, ...]
    keywords: tuple[str, ...]

    @property
    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass
class Page:
    jobs: list[dict]
    next_cursor: str | None = None


class DiscoveryAdapter(Protocol):
    """An actual integration must establish permission and use its supported auth.

    No password, cookie import, browser debugging, or evasion is provided here.
    Returned records must include application_method_evidence when classified.
    """
    permitted: bool
    name: str

    def fetch_page(self, search: Search, cursor: str | None) -> Page: ...


class UnavailableLinkedIn:
    name = "linkedin_unavailable"
    permitted = False

    def fetch_page(self, search, cursor):
        raise AccessBlocked(LINKEDIN_BLOCKER)


def build_searches(preferences, locations=None):
    return [Search(role, location, tuple(preferences["seniority"]), tuple(preferences["work_types"]),
                   tuple(preferences["keywords"]))
            for role in preferences["roles"] for location in (locations or preferences["countries"])]


def discover(adapter, searches, limits, store, accept):
    """Checkpoint only after page jobs are durably accepted. Resume is idempotent.

    Cursors are adapter-owned opaque values; repeat cursors stop incomplete searches.
    Completed searches stay completed until a user explicitly resets discovery.
    """
    coverage = {"adapter": adapter.name, "searches_total": len(searches), "searches_completed_this_run": 0,
                "searches_already_completed": 0, "searches_attempted": 0, "pages_read": 0,
                "records_received": 0, "limits_reached": [], "errors": [], "incomplete": True}
    def finish():
        completed = coverage["searches_already_completed"] + coverage["searches_completed_this_run"]
        coverage["searches_remaining"] = len(searches) - completed
        coverage["incomplete"] = completed != len(searches)
        coverage["limits_reached"] = sorted(set(coverage["limits_reached"]))
        return coverage
    if not adapter.permitted:
        coverage["errors"].append(LINKEDIN_BLOCKER)
        return finish()
    for search in searches:
        progress = store.search_progress(search.key)
        if progress.get("complete"):
            coverage["searches_already_completed"] += 1
            continue
        if coverage["searches_attempted"] >= limits["max_searches"]:
            coverage["limits_reached"].append("max_searches")
            break
        if coverage["records_received"] >= limits["max_jobs"]:
            coverage["limits_reached"].append("max_jobs")
            break
        coverage["searches_attempted"] += 1
        cursor, page_count, seen = progress.get("cursor"), 0, set()
        offset, saved_hash = progress.get("page_offset", 0), progress.get("page_hash")
        try:
            while page_count < limits["max_pages_per_search"]:
                if cursor in seen:
                    raise AccessBlocked("Repeated pagination cursor; coverage is incomplete.", "repeated_cursor")
                seen.add(cursor)
                page = adapter.fetch_page(search, cursor)
                if not isinstance(page, Page) or not isinstance(page.jobs, list) or (
                        page.next_cursor is not None and not isinstance(page.next_cursor, str)):
                    raise AccessBlocked("Invalid page contract; coverage is incomplete.", "invalid_page")
                remaining = limits["max_jobs"] - coverage["records_received"]
                page_hash = hashlib.sha256(json.dumps(page.jobs, sort_keys=True).encode()).hexdigest()
                if offset and (saved_hash != page_hash or offset > len(page.jobs)):
                    raise AccessBlocked("Partially processed source page changed; reset search explicitly to recheck coverage.", "changed_partial_page")
                selected = page.jobs[offset:offset + remaining]
                for job in selected:
                    accept(job)
                coverage["records_received"] += len(selected)
                coverage["pages_read"] += 1
                page_count += 1
                if offset + len(selected) < len(page.jobs):
                    store.save_search(search.key, cursor, False, offset + len(selected), page_hash)
                    coverage["limits_reached"].append("max_jobs")
                    return finish()
                store.save_search(search.key, page.next_cursor, page.next_cursor is None)
                if page.next_cursor is None:
                    coverage["searches_completed_this_run"] += 1
                    break
                cursor = page.next_cursor
                offset, saved_hash = 0, None
                if coverage["records_received"] >= limits["max_jobs"]:
                    coverage["limits_reached"].append("max_jobs")
                    return finish()
            else:
                coverage["limits_reached"].append("max_pages_per_search")
        except Exception as exc:
            # Do not log provider exception text: it can contain cookies/tokens.
            reason = getattr(exc, "reason_code", "adapter_error")
            if reason not in {"access_restricted", "captcha", "rate_limit", "restriction", "not_authorized", "repeated_cursor", "invalid_page", "changed_partial_page"}:
                reason = "adapter_error"
            coverage["errors"].append(f"Search stopped ({type(exc).__name__}, {reason}); no bypass or automatic retry.")
            break
    return finish()
