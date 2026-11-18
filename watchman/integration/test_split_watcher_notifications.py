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
class TestSplitWatcherNotifications(WatchmanTestCase.WatchmanTestCase):
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

    def test_concurrent_barriers(self):
        self.check_concurrent_barriers(True)

    def test_concurrent_barriers_directory_events(self):
        self.check_concurrent_barriers(False)

    def check_concurrent_barriers(self, file_events):
        root = self.make_root(file_events)
        sock = self.watchmanInstance().getSockPath().legacy_sockpath()

        def worker(index):
            client = pywatchman.client(sockpath=sock, timeout=60)
            try:
                # A query captures its clock while holding the view lock. Plain
                # clock can read a tick while that IO batch is still running.
                clock = client.query(
                    "query",
                    root,
                    {
                        "expression": ["false"],
                        "fields": ["name"],
                        "sync_timeout": 60000,
                    },
                )["clock"]

                def delta():
                    nonlocal clock
                    result = client.query(
                        "query",
                        root,
                        {
                            "since": clock,
                            "fields": ["name", "exists"],
                            "expression": ["dirname", str(index)],
                            "sync_timeout": 60000,
                        },
                    )
                    self.assertFalse(result["is_fresh_instance"])
                    clock = result["clock"]
                    return {f["name"]: f["exists"] for f in result["files"]}

                for iteration in range(8):
                    old = f"{index}/file-{iteration}"
                    new = old + "-renamed"
                    with open(os.path.join(root, old), "w") as f:
                        f.write(str(iteration))
                    self.assertTrue(delta()[old])
                    os.rename(os.path.join(root, old), os.path.join(root, new))
                    changed = delta()
                    self.assertFalse(changed[old])
                    self.assertTrue(changed[new])
                    os.unlink(os.path.join(root, new))
                    self.assertFalse(delta()[new])
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))

    def test_remove_and_recreate_stream(self):
        root = self.make_root()
        directory = os.path.join(root, "0")
        self.touchRelative(root, "0", "before")
        before = self.watchmanCommand("query", root, {"fields": ["name"]})
        shutil.rmtree(directory)
        deleted = self.watchmanCommand(
            "query",
            root,
            {
                "since": before["clock"],
                "fields": ["name", "exists"],
            },
        )
        changed = {f["name"]: f["exists"] for f in deleted["files"]}
        self.assertFalse(changed["0/before"])
        os.mkdir(directory)
        self.touchRelative(root, "0", "after")
        self.watchmanCommand(
            "debug-kqueue-and-fsevents-recrawl", root, directory
        )
        self.assertFileList(
            root,
            [".watchmanconfig", "0", "0/after"] + [str(i) for i in range(1, 8)],
        )
