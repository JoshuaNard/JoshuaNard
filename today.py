from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
SVG_FILES = (Path("dark_mode.svg"),)
ENV_FILE = Path(".env")


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Load simple KEY=VALUE pairs from .env without overwriting existing env vars."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


@dataclass(frozen=True)
class ProfileConfig:
    """Easy-to-edit personal settings for the generated profile."""

    username: str = os.environ.get("PROFILE_USERNAME", "JoshuaNard")
    birthday: str = os.environ.get("PROFILE_BIRTHDAY", "2004-03-31")


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    name: str


@dataclass(frozen=True)
class GitHubStats:
    repos: int
    commits: int
    lines_of_code: int


def parse_date(value: str) -> dt.date:
    """Read a YYYY-MM-DD date from configuration."""
    return dt.date.fromisoformat(value)


def calculate_uptime(start_date: dt.date, today: dt.date | None = None) -> str:
    """Return elapsed years, months, and days without third-party date helpers."""
    current = today or dt.datetime.now(dt.timezone.utc).date()
    years = current.year - start_date.year
    months = current.month - start_date.month
    days = current.day - start_date.day

    if days < 0:
        previous_month = current.replace(day=1) - dt.timedelta(days=1)
        days += previous_month.day
        months -= 1

    if months < 0:
        months += 12
        years -= 1

    return f"{years} {plural('year', years)}, {months} {plural('month', months)}, {days} {plural('day', days)}"


def plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def github_token() -> str:
    """Prefer the Actions-provided token, but allow local .env ACCESS_TOKEN fallback."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Set GITHUB_TOKEN or ACCESS_TOKEN in your environment or .env file.")
    return token


def run_graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    """Execute a GitHub GraphQL request and return the data object."""
    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def fetch_profile_page(username: str, token: str, repo_cursor: str | None = None) -> dict[str, Any]:
    """Fetch account counts and one page of public owned repositories."""
    query = """
    query ProfileStats($login: String!, $repoCursor: String) {
      user(login: $login) {
        id
        createdAt
        repositories(first: 100, after: $repoCursor, ownerAffiliations: OWNER, privacy: PUBLIC) {
          totalCount
          nodes {
            nameWithOwner
            isFork
            defaultBranchRef {
              name
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """
    return run_graphql(query, {"login": username, "repoCursor": repo_cursor}, token)["user"]


def fetch_owned_repositories(username: str, token: str) -> tuple[int, str, str, list[RepositoryRef]]:
    """Return public repository count, user id, creation date, and default-branch repos."""
    first_page = fetch_profile_page(username, token)
    repos_count = int(first_page["repositories"]["totalCount"])
    user_id = str(first_page["id"])
    created_at = str(first_page["createdAt"])
    repos = repository_refs(first_page["repositories"]["nodes"])

    page_info = first_page["repositories"]["pageInfo"]
    while page_info["hasNextPage"]:
        page = fetch_profile_page(username, token, page_info["endCursor"])
        repos.extend(repository_refs(page["repositories"]["nodes"]))
        page_info = page["repositories"]["pageInfo"]

    return repos_count, user_id, created_at, repos


def repository_refs(nodes: list[dict[str, Any]]) -> list[RepositoryRef]:
    """Keep repositories where authored default-branch commits can be inspected."""
    refs: list[RepositoryRef] = []
    for node in nodes:
        if node["isFork"] or node["defaultBranchRef"] is None:
            continue
        owner, name = str(node["nameWithOwner"]).split("/", 1)
        refs.append(RepositoryRef(owner=owner, name=name))
    return refs


def fetch_commit_contributions(username: str, token: str, start_year: int) -> int:
    """Sum yearly commit contributions from account creation through today."""
    today = dt.datetime.now(dt.timezone.utc)
    total = 0

    query = """
    query CommitStats($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          restrictedContributionsCount
        }
      }
    }
    """

    for year in range(start_year, today.year + 1):
        start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc)
        if year == today.year:
            end = today

        data = run_graphql(
            query,
            {"login": username, "from": start.isoformat(), "to": end.isoformat()},
            token,
        )["user"]["contributionsCollection"]
        total += int(data["totalCommitContributions"]) + int(data["restrictedContributionsCount"])

    return total


def fetch_repository_line_changes(repo: RepositoryRef, user_id: str, token: str) -> int:
    """Sum additions and deletions from authored commits on a repository's default branch."""
    query = """
    query RepositoryLines($owner: String!, $name: String!, $authorId: ID!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor, author: {id: $authorId}) {
                nodes {
                  additions
                  deletions
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """
    total = 0
    cursor: str | None = None

    while True:
        data = run_graphql(
            query,
            {"owner": repo.owner, "name": repo.name, "authorId": user_id, "cursor": cursor},
            token,
        )["repository"]
        target = data.get("defaultBranchRef", {}).get("target") if data.get("defaultBranchRef") else None
        if not target or "history" not in target:
            return total

        history = target["history"]
        total += sum(int(node["additions"]) + int(node["deletions"]) for node in history["nodes"])
        page_info = history["pageInfo"]
        if not page_info["hasNextPage"]:
            return total
        cursor = page_info["endCursor"]


def fetch_lines_of_code(repos: list[RepositoryRef], user_id: str, token: str) -> int:
    """Estimate profile LOC as default-branch authored additions plus deletions."""
    return sum(fetch_repository_line_changes(repo, user_id, token) for repo in repos)


def account_creation_year(created_at: str) -> int:
    created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return created.year


def fetch_github_stats(config: ProfileConfig) -> GitHubStats:
    """Collect all dynamic GitHub values used by the SVG."""
    token = github_token()
    repos_count, user_id, created_at, repos = fetch_owned_repositories(config.username, token)
    return GitHubStats(
        repos=repos_count,
        commits=fetch_commit_contributions(config.username, token, account_creation_year(created_at)),
        lines_of_code=fetch_lines_of_code(repos, user_id, token),
    )


def dotted_line(label: str, value: int | str, width: int = 43) -> str:
    """Create Andrew-style dotted alignment rows that fit the right column."""
    text = f"{value:,}" if isinstance(value, int) else value
    dots = "." * max(2, width - len(label) - len(text) - 2)
    return f"{label} {dots} {text}"


def text_updates(config: ProfileConfig, stats: GitHubStats, today: dt.date | None = None) -> dict[str, str]:
    current = today or dt.datetime.now(dt.timezone.utc).date()
    return {
        "uptime": calculate_uptime(parse_date(config.birthday), current),
        "repos": f"{stats.repos:,}",
        "commits": f"{stats.commits:,}",
        "lines_of_code": f"{stats.lines_of_code:,}",
        "last_updated": f"Last Updated: {current.isoformat()}",
    }


def replace_element_text(element: ET.Element, text: str) -> None:
    """Replace an SVG tspan's nested content while preserving its SVG attributes."""
    attributes = dict(element.attrib)
    tail = element.tail
    element.clear()
    element.attrib.update(attributes)
    element.text = text
    element.tail = tail


def update_svg_text(svg_path: Path, updates: dict[str, str]) -> None:
    """Edit existing SVG text nodes by id instead of rebuilding the SVG."""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.parse(svg_path)
    root = tree.getroot()

    for element_id, text in updates.items():
        element = root.find(f".//*[@id='{element_id}']")
        if element is None:
            raise ValueError(f"Missing SVG element id '{element_id}' in {svg_path}")
        replace_element_text(element, text)

    tree.write(svg_path, encoding="utf-8", xml_declaration=True)


def main() -> None:
    config = ProfileConfig()
    stats = fetch_github_stats(config)
    updates = text_updates(config, stats)

    for svg_file in SVG_FILES:
        update_svg_text(svg_file, updates)


if __name__ == "__main__":
    main()