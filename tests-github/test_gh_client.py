from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src" / "github" / "app"))

from gh_client import (
    GitHubClientError,
    GitHubReadOnlyClient,
    GitHubRepoNotAllowedError,
    UNTRUSTED_NOTE,
)
from gh_config import parse_config


class MockHttpResponse:
    def __init__(self, routes: dict[str, tuple[int, Any]]):
        self.routes = routes
        self.calls = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        # Match longer path patterns first (e.g. /issues/10/comments before /issues/10)
        sorted_routes = sorted(self.routes.items(), key=lambda item: len(item[0]), reverse=True)
        for path_sub, (status, data) in sorted_routes:
            if path_sub in url:
                if isinstance(data, (dict, list)):
                    return status, json.dumps(data).encode("utf-8")
                return status, str(data).encode("utf-8")
        return 404, b'{"message": "Not Found"}'


class TestGitHubReadOnlyClient(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config({
            "allowed_repositories": ["MasterMk2/hermes-cos-infra"],
            "file_content_max_bytes": 100,
        })

    def test_mutation_methods_do_not_exist(self):
        client = GitHubReadOnlyClient(self.cfg)
        for forbidden in ("post", "put", "patch", "delete", "create_pr", "merge", "commit", "push", "close_issue"):
            self.assertFalse(hasattr(client, forbidden), f"Forbidden mutation method '{forbidden}' exists on read-only client!")

    def test_repo_allowlist_enforced(self):
        client = GitHubReadOnlyClient(self.cfg)
        with self.assertRaises(GitHubRepoNotAllowedError):
            client.get_repo_status("evil-attacker/repo")
        with self.assertRaises(GitHubRepoNotAllowedError):
            client.list_commits("other/allowed")

    def test_get_repo_status(self):
        mock_http = MockHttpResponse({
            "/repos/MasterMk2/hermes-cos-infra": (200, {
                "description": "Hermes infrastructure",
                "default_branch": "main",
                "open_issues_count": 2,
                "visibility": "private",
                "pushed_at": "2026-09-05T12:00:00Z",
            })
        })
        client = GitHubReadOnlyClient(self.cfg, token="test-mock-token", http_client=mock_http)
        res = client.get_repo_status("MasterMk2/hermes-cos-infra")

        self.assertEqual(res["repository"], "MasterMk2/hermes-cos-infra")
        self.assertEqual(res["default_branch"], "main")
        self.assertEqual(res["open_issues_count"], 2)
        self.assertIn("untrusted external evidence", res["note"])
        self.assertIn("Authorization", mock_http.calls[0]["headers"])
        self.assertEqual(mock_http.calls[0]["headers"]["Authorization"], "Bearer test-mock-token")

    def test_list_commits(self):
        mock_http = MockHttpResponse({
            "/commits": (200, [
                {
                    "sha": "abc12345",
                    "commit": {
                        "message": "Phase H.2: complete Gmail intelligence\n\nFull report inside.",
                        "author": {"name": "MasterMk2", "email": "dev@test.invalid", "date": "2026-09-05T14:00:00Z"},
                    },
                    "html_url": "https://github.com/MasterMk2/hermes-cos-infra/commit/abc12345",
                }
            ])
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        res = client.list_commits("MasterMk2/hermes-cos-infra", limit=5)

        self.assertEqual(res["total_returned"], 1)
        self.assertEqual(res["commits"][0]["sha"], "abc12345")
        self.assertIn("Phase H.2", res["commits"][0]["message"])
        self.assertIn("per_page=5", mock_http.calls[0]["url"])

    def test_get_commit(self):
        mock_http = MockHttpResponse({
            "/commits/abc12345": (200, {
                "sha": "abc12345",
                "commit": {"message": "fix something", "author": {"name": "Alice", "date": "2026-09-05T10:00:00Z"}},
                "stats": {"total": 10, "additions": 8, "deletions": 2},
                "files": [{"filename": "main.py", "status": "modified", "additions": 8, "deletions": 2, "patch": "@@ -1 +1 @@"}],
            })
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        res = client.get_commit("MasterMk2/hermes-cos-infra", "abc12345")

        self.assertEqual(res["sha"], "abc12345")
        self.assertEqual(res["stats"]["total"], 10)
        self.assertEqual(len(res["files"]), 1)
        self.assertEqual(res["files"][0]["filename"], "main.py")

    def test_get_file_content_and_directory(self):
        file_bytes = b"Hello from repo README content"
        b64_content = base64.b64encode(file_bytes).decode("ascii")
        mock_http = MockHttpResponse({
            "/contents/README.md": (200, {
                "type": "file",
                "size": len(file_bytes),
                "encoding": "base64",
                "content": b64_content,
                "sha": "sha-readme",
            }),
            "/contents/docs": (200, [
                {"name": "doc1.md", "path": "docs/doc1.md", "type": "file", "size": 120},
                {"name": "sub", "path": "docs/sub", "type": "dir", "size": 0},
            ]),
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        
        # Test file reading
        res_file = client.get_file("MasterMk2/hermes-cos-infra", "README.md")
        self.assertEqual(res_file["type"], "file")
        self.assertEqual(res_file["content"], "Hello from repo README content")

        # Test directory listing
        res_dir = client.get_file("MasterMk2/hermes-cos-infra", "docs")
        self.assertEqual(res_dir["type"], "directory")
        self.assertEqual(len(res_dir["entries"]), 2)

    def test_get_file_size_cap(self):
        long_bytes = b"A" * 200
        b64 = base64.b64encode(long_bytes).decode("ascii")
        mock_http = MockHttpResponse({
            "/contents/huge.txt": (200, {"type": "file", "size": 200, "encoding": "base64", "content": b64}),
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        res = client.get_file("MasterMk2/hermes-cos-infra", "huge.txt")
        self.assertIn("truncated", res["content"])
        self.assertTrue(len(res["content"]) < 150)

    def test_list_and_get_prs(self):
        mock_http = MockHttpResponse({
            "/pulls/42": (200, {
                "number": 42,
                "title": "Add GitHub read capability",
                "body": "PR description body",
                "state": "open",
                "user": {"login": "MasterMk2"},
                "head": {"ref": "feature/h3"},
                "base": {"ref": "main"},
                "additions": 100,
                "deletions": 5,
                "changed_files": 3,
            }),
            "/issues/42/comments": (200, [
                {"user": {"login": "reviewer"}, "body": "LGTM!", "created_at": "2026-09-05T11:00:00Z"}
            ]),
            "/pulls": (200, [
                {"number": 42, "title": "Add GitHub read capability", "state": "open", "user": {"login": "MasterMk2"}}
            ]),
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        prs = client.list_prs("MasterMk2/hermes-cos-infra")
        self.assertEqual(prs["total_returned"], 1)
        self.assertEqual(prs["pull_requests"][0]["number"], 42)

        pr = client.get_pr("MasterMk2/hermes-cos-infra", 42)
        self.assertEqual(pr["number"], 42)
        self.assertEqual(pr["head_branch"], "feature/h3")
        self.assertEqual(len(pr["comments"]), 1)
        self.assertEqual(pr["comments"][0]["body"], "LGTM!")

    def test_list_and_get_issues(self):
        mock_http = MockHttpResponse({
            "/issues/10": (200, {
                "number": 10,
                "title": "Bug in query",
                "body": "Issue body text",
                "state": "open",
                "user": {"login": "reporter"},
                "created_at": "2026-09-05T09:00:00Z",
            }),
            "/issues/10/comments": (200, []),
            "/issues": (200, [
                {"number": 10, "title": "Bug in query", "state": "open", "user": {"login": "reporter"}},
                {"number": 42, "title": "PR issue", "pull_request": {}}, # Should be filtered out
            ]),
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        issues = client.list_issues("MasterMk2/hermes-cos-infra")
        self.assertEqual(issues["total_returned"], 1)
        self.assertEqual(issues["issues"][0]["number"], 10)

        issue = client.get_issue("MasterMk2/hermes-cos-infra", 10)
        self.assertEqual(issue["number"], 10)
        self.assertEqual(issue["title"], "Bug in query")

    def test_auth_and_rate_limit_errors(self):
        mock_http = MockHttpResponse({
            "/repos/MasterMk2/hermes-cos-infra": (401, b'{"message": "Bad credentials"}'),
        })
        client = GitHubReadOnlyClient(self.cfg, http_client=mock_http)
        with self.assertRaises(GitHubClientError) as ctx:
            client.get_repo_status("MasterMk2/hermes-cos-infra")
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()