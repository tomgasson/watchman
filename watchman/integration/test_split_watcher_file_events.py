# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the LICENSE file.

import concurrent.futures
import json
import os
import shutil
import sys

import pywatchman

from watchman.integration.lib import WatchmanTestCase


@WatchmanTestCase.expand_matrix
class TestSplitWatcherFileEvents(WatchmanTestCase.WatchmanTestCase):
    def checkOSApplicability(self):
        if sys.platform != "darwin":
            self.skipTest("requires macOS split watcher")

    def make_root(self, file_events=True):
        root = self.mkdtemp()
        with open(os.path.join(root, ".watchmanconfig"), "w") as f:
            json.dump(
                {
                    "prefer_split_fsevents_watcher": True,
                    "fsevents_watch_files": file_events,
                },
                f,
            )
        for i in range(8):
            os.mkdir(os.path.join(root, str(i)))
        watch = self.watchmanCommand("watch", root)
        self.assertEqual(watch["watcher"], "kqueue+fsevents")
        return root

    def test_root_and_nested_type_transitions(self):
        for file_events in (True, False):
            root = self.make_root(file_events)
            # Root cookies must still be observed through a kqueue directory
            # scan, while nested cookies use the configured FSEvents mode.
            for name in ("root-entry", "0/nested-entry"):
                path = os.path.join(root, name)
                before = self.watchmanCommand(
                    "query",
                    root,
                    {
                        "expression": ["false"],
                        "fields": ["name"],
                        "sync_timeout": 60000,
                    },
                )["clock"]
                with open(path, "w") as f:
                    f.write("file")
                created = self.watchmanCommand(
                    "query",
                    root,
                    {
                        "since": before,
                        "fields": ["name", "exists", "type"],
                        "sync_timeout": 60000,
                    },
                )
                rows = {f["name"]: f for f in created["files"]}
                self.assertEqual(rows[name]["type"], "f")
                os.unlink(path)
                os.mkdir(path)
                self.touchRelative(root, name, "child")
                directory = self.watchmanCommand(
                    "query",
                    root,
                    {
                        "since": created["clock"],
                        "fields": ["name", "exists", "type"],
                        "sync_timeout": 60000,
                    },
                )
                rows = {f["name"]: f for f in directory["files"]}
                self.assertEqual(rows[name]["type"], "d")
                self.assertTrue(rows[name + "/child"]["exists"])
                shutil.rmtree(path)
                deleted = self.watchmanCommand(
                    "query",
                    root,
                    {
                        "since": directory["clock"],
                        "fields": ["name", "exists"],
                        "sync_timeout": 60000,
                    },
                )
                rows = {f["name"]: f for f in deleted["files"]}
                self.assertFalse(rows[name]["exists"])
                self.assertFalse(rows[name + "/child"]["exists"])

    def test_publish_new_stream_during_queries(self):
        root = self.make_root()
        sock = self.watchmanInstance().getSockPath().legacy_sockpath()

        def barriers():
            client = pywatchman.client(sockpath=sock, timeout=30)
            try:
                for _ in range(30):
                    client.query(
                        "query",
                        root,
                        {
                            "expression": ["false"],
                            "fields": ["name"],
                            "sync_timeout": 10000,
                        },
                    )
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            running = [pool.submit(barriers) for _ in range(3)]
            for i in range(20):
                name = "new-stream-" + str(i)
                os.mkdir(os.path.join(root, name))
                self.touchRelative(root, name, "child")
                result = self.watchmanCommand(
                    "query",
                    root,
                    {
                        "expression": ["name", name + "/child", "wholename"],
                        "fields": ["name"],
                        "sync_timeout": 10000,
                    },
                )
                self.assertEqual(result["files"], [name + "/child"])
                # Other clients can still be creating cookies here. Keep the
                # directory until their barriers finish; deletion has its own
                # test and racing rmtree with cookie creation is not valid.
            for future in running:
                future.result()
