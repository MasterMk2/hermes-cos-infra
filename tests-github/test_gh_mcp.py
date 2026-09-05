from __future__ import annotations

import asyncio
import json
import os
import sys
import types as pytypes
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "github" / "app"))

# Stub MCP transport classes if not present
if "mcp" not in sys.modules:
    try:
        import mcp
    except ImportError:
        class Server:
            def __init__(self, *_args, **_kwargs): pass
            def create_initialization_options(self): return None
            async def run(self, *_args): return None
        class Tool:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class ListToolsResult:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class TextContent:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class CallToolResult:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        mcp_mod = pytypes.ModuleType("mcp")
        mcp_types = pytypes.ModuleType("mcp.types")
        for cls in (Tool, ListToolsResult, TextContent, CallToolResult):
            setattr(mcp_types, cls.__name__, cls)
        lowlevel = pytypes.ModuleType("mcp.server.lowlevel"); lowlevel.Server = Server
        stdio = pytypes.ModuleType("mcp.server.stdio")
        async def stdio_server(): yield None, None
        stdio.stdio_server = stdio_server
        sys.modules.update({
            "mcp": mcp_mod,
            "mcp.types": mcp_types,
            "mcp.server": pytypes.ModuleType("mcp.server"),
            "mcp.server.lowlevel": lowlevel,
            "mcp.server.stdio": stdio,
        })

sys.path.insert(0, str(ROOT / "src" / "github" / "mcp"))
import github_mcp


class TestGitHubMcp(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_contains_only_readonly(self):
        res = await github_mcp._list_tools()
        tools = res.tools
        names = [t.name for t in tools]

        self.assertIn("github_repo_status", names)
        self.assertIn("github_list_commits", names)
        self.assertIn("github_get_commit", names)
        self.assertIn("github_get_file", names)
        self.assertIn("github_list_prs", names)
        self.assertIn("github_get_pr", names)
        self.assertIn("github_list_issues", names)
        self.assertIn("github_get_issue", names)

        # Confirm no mutation tools exist
        for name in names:
            for forbidden in ("write", "create", "update", "delete", "post", "patch", "merge", "push", "close"):
                self.assertNotIn(forbidden, name)

    async def test_call_tool_allowlist_enforcement(self):
        params = MagicMock()
        params.name = "github_repo_status"
        params.arguments = {"repo": "unauthorized/repo"}

        res = await github_mcp._call_tool(params=params)
        self.assertEqual(len(res.content), 1)
        data = json.loads(res.content[0].text)
        self.assertEqual(data.get("error"), "repository_not_allowed")

    async def test_call_tool_success_with_mock_client(self):
        params = MagicMock()
        params.name = "github_repo_status"
        params.arguments = {"repo": "MasterMk2/hermes-cos-infra"}

        with patch.object(github_mcp.CLIENT, "get_repo_status", return_value={"repository": "MasterMk2/hermes-cos-infra", "default_branch": "main", "note": "untrusted"}):
            res = await github_mcp._call_tool(params=params)
            self.assertEqual(len(res.content), 1)
            data = json.loads(res.content[0].text)
            self.assertEqual(data.get("repository"), "MasterMk2/hermes-cos-infra")
            self.assertEqual(data.get("default_branch"), "main")
            self.assertEqual(data.get("note"), "untrusted")


if __name__ == "__main__":
    unittest.main()