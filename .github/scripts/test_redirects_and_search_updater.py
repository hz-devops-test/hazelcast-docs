#!/usr/bin/env python
import os
import sys
import unittest
import types
import logging
import json
import re
from unittest.mock import patch, MagicMock

def initialize_test_environment() -> None:
    if "antora_utils" not in sys.modules:
        class DynamicMockModule(types.ModuleType):
            def __getattr__(self, name):
                if name == "setup_logger":
                    return lambda name: logging.getLogger(name)
                return MagicMock()
        sys.modules["antora_utils"] = DynamicMockModule("antora_utils")

    global updater
    with patch("urllib.request.urlretrieve"):
        import redirects_and_search_updater as updater

initialize_test_environment()

class DynamicFileSimulator:
    def __init__(self, redirects_content: str, search_content: str):
        self.files = {
            "_redirects": redirects_content,
            "search-config.json": search_content
        }

    def open_stream(self, file_path: str, mode: str):
        return VirtualFileContext(self, file_path, mode)

class VirtualFileContext:
    def __init__(self, factory: DynamicFileSimulator, file_path: str, mode: str):
        self.factory = factory
        self.file_path = file_path
        self.mode = mode
        self.read_done = False
        self.local_buffer = []

    def read(self, *args, **kwargs) -> str:
        if ("r" in self.mode or "+" in self.mode) and not self.read_done:
            self.read_done = True
            return self.factory.files[self.file_path]
        return ""

    def readlines(self) -> list:
        content = self.read()
        if content:
            return [line + "\n" if not line.endswith("\n") else line for line in content.splitlines()]
        return []

    def write(self, data: str) -> int:
        self.local_buffer.append(data)
        return len(data)

    def writelines(self, lines: list) -> None:
        for line in lines:
            self.write(line)

    def seek(self, position: int, *args, **kwargs) -> None:
        if position == 0:
            self.local_buffer = []
            self.read_done = False

    def truncate(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if ("w" in self.mode or "+" in self.mode) and self.local_buffer:
            self.factory.files[self.file_path] = "".join(self.local_buffer)


class TestRedirectsAndSearchUpdater(unittest.TestCase):

    def get_redirects_template(self) -> str:
        return """# Standard Base Aliases

/hazelcast/latest/*  /hazelcast/5.7/:splat 307!  

/hazelcast/latest-dev/*  /hazelcast/5.8-snapshot/:splat 307!

# Secondary Static Routes

/cloud/latest/*  /cloud/default/:splat 302!
"""

    def get_search_template(self) -> str:
        docs_url: str = "https://docs.hazelcast.com/hazelcast/(?P<version>.*?)/"
        return json.dumps({
            "index_name": "prod_hazelcast_docs",
            "start_urls": [
                {
                    "url": "https://hazelcast.com",
                    "tags": ["cloud"],
                    "selectors_key": "cloud"
                },
                {
                    "url": docs_url,
                    "page_rank": 1,
                    "tags": ["hazelcast-5.8-snapshot"],
                    "variables": {
                        "version": ["5.8-snapshot"]
                    },
                    "selectors_key": "hz"
                },
                {
                    "url": docs_url,
                    "tags": ["hazelcast-5.7"],
                    "variables": {
                        "version": ["5.7"]
                    },
                    "selectors_key": "hz"
                },
                {
                    "url": "https://docs.hazelcast.com/management-center/(?P<version>.*?)/",
                    "tags": ["management-center-5.12-snapshot"],
                    "variables": {
                        "version": ["5.12-snapshot"]
                    },
                    "selectors_key": "mc"
                }
            ],
            "custom_settings": {
                "distinct": True
            }
        }, indent=2)

    @patch("builtins.open")
    def test_process_redirects(self, mock_open) -> None:
        simulator = DynamicFileSimulator(self.get_redirects_template(), self.get_search_template())
        mock_open.side_effect = simulator.open_stream

        updater.process_redirects(master_major_minor="5.9", rel_major_minor="5.8")

        final_redirects = simulator.files["_redirects"]
        
        self.assertTrue(final_redirects.startswith("# Standard Base Aliases\n"))
        self.assertTrue(final_redirects.endswith("# Secondary Static Routes\n\n/cloud/latest/*  /cloud/default/:splat 302!\n"))
        
        self.assertRegex(final_redirects, r"/hazelcast/latest/\*\s+/hazelcast/5\.8/")
        self.assertRegex(final_redirects, r"/hazelcast/latest-dev/\*\s+/hazelcast/5\.9-snapshot/")

    @patch("builtins.open")
    def test_process_redirects_failure(self, mock_open) -> None:
        bad_redirects_template = """# Missing target patterns entirely
/cloud/latest/*  /cloud/default/:splat 302!
"""
        simulator = DynamicFileSimulator(bad_redirects_template, self.get_search_template())
        mock_open.side_effect = simulator.open_stream

        with self.assertRaises(ValueError) as context:
            updater.process_redirects(master_major_minor="5.9", rel_major_minor="5.8")
            
        self.assertIn("Target pattern '/hazelcast/latest/*' was not found", str(context.exception))

    @patch("builtins.open")
    def test_process_search_config(self, mock_open) -> None:
        simulator = DynamicFileSimulator(self.get_redirects_template(), self.get_search_template())
        mock_open.side_effect = simulator.open_stream

        updater.process_search_config(master_major_minor="5.9", rel_major_minor="5.8")

        final_search = json.loads(simulator.files["search-config.json"])

        self.assertEqual(final_search["index_name"], "prod_hazelcast_docs")
        self.assertTrue(final_search["custom_settings"]["distinct"])

        urls = final_search["start_urls"]
        self.assertEqual(len(urls), 5)

        self.assertEqual(urls[0]["selectors_key"], "cloud")
        self.assertEqual(urls[4]["selectors_key"], "mc")

        self.assertEqual(urls[1]["tags"], ["hazelcast-5.9-snapshot"])
        self.assertEqual(urls[1]["variables"]["version"], ["5.9-snapshot"])
        self.assertEqual(urls[1]["page_rank"], 1)

        self.assertEqual(urls[2]["tags"], ["hazelcast-5.8"])
        self.assertEqual(urls[2]["variables"]["version"], ["5.8"])
        self.assertEqual(urls[2]["selectors_key"], "hz")
        self.assertNotIn("page_rank", urls[2])

        self.assertEqual(urls[3]["tags"], ["hazelcast-5.7"])
        self.assertEqual(urls[3]["variables"]["version"], ["5.7"])
        self.assertEqual(urls[3]["selectors_key"], "hz")

        self.assertEqual(urls[4]["tags"], ["management-center-5.12-snapshot"])
        self.assertEqual(urls[4]["variables"]["version"], ["5.12-snapshot"])
        self.assertEqual(urls[4]["selectors_key"], "mc")

    @patch("builtins.open")
    def test_process_search_config_failure(self, mock_open) -> None:
        bad_search_template = json.dumps({
            "index_name": "prod_hazelcast_docs",
            "start_urls": [
                {
                    "url": "https://hazelcast.com",
                    "tags": ["cloud"],
                    "selectors_key": "cloud"
                }
            ]
        })
        simulator = DynamicFileSimulator(self.get_redirects_template(), bad_search_template)
        mock_open.side_effect = simulator.open_stream

        with self.assertRaises(ValueError) as context:
            updater.process_search_config(master_major_minor="5.9", rel_major_minor="5.8")
            
        self.assertIn("Target hazelcast snapshot entry configuration block was not found", str(context.exception))

    def test_update_is_rel_major_minor_true(self) -> None:
        simulator = DynamicFileSimulator(self.get_redirects_template(), self.get_search_template())

        with patch("builtins.open", side_effect=simulator.open_stream) as mock_open, \
             patch("antora_utils.checkout_branch", return_value="update_search_branch_123") as mock_checkout, \
             patch("antora_utils.commit_changes") as mock_commit, \
             patch("antora_utils.create_github_pr") as mock_pr:

            updater.update(
                rel_major_minor="5.8",
                master_version="5.9.0-SNAPSHOT",
                master_major_minor="5.9",
                is_rel_major_minor="true"
            )

            mock_checkout.assert_called_once_with("update_redirects_search", "main")
            mock_commit.assert_called_once_with("main", "5.9.0-SNAPSHOT", ["_redirects", "search-config.json"], "update_search_branch_123")
            mock_pr.assert_called_once_with("main", "update_search_branch_123", "5.9.0-SNAPSHOT")

            final_redirects = simulator.files["_redirects"]
            self.assertRegex(final_redirects, r"/hazelcast/latest/\*\s+/hazelcast/5\.8/")

    def test_update_is_rel_major_minor_false(self) -> None:
        simulator = DynamicFileSimulator(self.get_redirects_template(), self.get_search_template())

        with patch("builtins.open", side_effect=simulator.open_stream), \
             patch("antora_utils.checkout_branch") as mock_checkout, \
             patch("redirects_and_search_updater.logger.info") as mock_info:

            updater.update(
                rel_major_minor="5.8",
                master_version="5.8.1-SNAPSHOT",
                master_major_minor="5.8",
                is_rel_major_minor="false"
            )

            mock_checkout.assert_not_called()
            mock_info.assert_called_once_with("Skip '_redirects' and 'search-config.json' updates for BETA or PATCH release")

    @patch("antora_utils.merge_github_pr")
    def test_merge_pull_requests_true(self, mock_merge) -> None:
        updater.merge_pull_requests(
            is_rel_major_minor="true",
            master_version="5.9.0-SNAPSHOT"
        )
        mock_merge.assert_called_once_with("main", "5.9.0-SNAPSHOT")

    @patch("antora_utils.merge_github_pr")
    def test_merge_pull_requests_false(self, mock_merge) -> None:
        with patch("redirects_and_search_updater.logger.info") as mock_info:
            updater.merge_pull_requests(
                is_rel_major_minor="false",
                master_version="5.8.2-SNAPSHOT"
            )

        mock_merge.assert_not_called()
        mock_info.assert_called_once_with("Skip '_redirects' and 'search-config.json' updates for BETA or PATCH release")

if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2))
