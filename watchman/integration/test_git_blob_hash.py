# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the LICENSE file.

import hashlib
import json
import os

from watchman.integration.lib import WatchmanTestCase


@WatchmanTestCase.expand_matrix
class TestGitBlobHash(WatchmanTestCase.WatchmanTestCase):
    def test_vectors_and_mutation(self):
        capabilities = self.watchmanCommand(
            "version",
            {
                "required": ["field-content.gitsha1hex"],
            },
        )["capabilities"]
        self.assertTrue(capabilities["field-content.gitsha1hex"])
        root = self.mkdtemp()
        # Reference object IDs from git hash-object --stdin.
        vectors = {
            "empty": (b"", "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"),
            "binary": (
                bytes(range(256)),
                "c86626638e0bc8cf47ca49bb1525b40e9737ee64",
            ),
            "large": (
                bytes(range(256)) * 8193,
                "30641955362d837442b592f004641a3db7c34fa2",
            ),
        }
        for name, (data, _) in vectors.items():
            with open(os.path.join(root, name), "wb") as f:
                f.write(data)
        self.watchmanCommand("watch", root)

        def check():
            result = self.watchmanCommand(
                "query",
                root,
                {
                    "expression": ["type", "f"],
                    "fields": ["name", "content.sha1hex", "content.gitsha1hex"],
                },
            )
            for row in result["files"]:
                data, expected_git_hash = vectors[row["name"]]
                self.assertEqual(
                    row["content.sha1hex"], hashlib.sha1(data).hexdigest()
                )
                self.assertEqual(row["content.gitsha1hex"], expected_git_hash)
            self.assertEqual(len(result["files"]), len(vectors))
            return result

        check()
        stats = self.watchmanCommand("debug-contenthash", root)
        self.assertEqual(stats["size"], 2 * len(vectors))
        check()
        self.assertEqual(
            self.watchmanCommand("debug-contenthash", root)["cacheStore"],
            stats["cacheStore"],
        )
        vectors["binary"] = (
            b"replaced",
            "8b204eb8fd87bd5f4ea426571fa910c9c02127f8",
        )
        with open(os.path.join(root, "binary"), "wb") as f:
            f.write(vectors["binary"][0])
        before_delete = check()
        os.unlink(os.path.join(root, "binary"))
        deleted = self.watchmanCommand(
            "query",
            root,
            {
                "since": before_delete["clock"],
                "expression": ["name", "binary"],
                "fields": [
                    "name",
                    "exists",
                    "content.sha1hex",
                    "content.gitsha1hex",
                ],
            },
        )
        self.assertEqual(
            deleted["files"],
            [
                {
                    "name": "binary",
                    "exists": False,
                    "content.sha1hex": None,
                    "content.gitsha1hex": None,
                }
            ],
        )

    def test_cache_isolation_and_limit(self):
        root = self.mkdtemp()
        with open(os.path.join(root, ".watchmanconfig"), "w") as f:
            json.dump({"content_hash_max_items": 1}, f)
        data = b"hello\n"
        with open(os.path.join(root, "file"), "wb") as f:
            f.write(data)
        self.watchmanCommand("watch", root)
        for field, expected in (
            ("content.sha1hex", hashlib.sha1(data).hexdigest()),
            ("content.gitsha1hex", "ce013625030ba8dba906f756967f9e9ca394464a"),
            ("content.sha1hex", hashlib.sha1(data).hexdigest()),
        ):
            result = self.watchmanCommand(
                "query",
                root,
                {
                    "expression": ["name", "file"],
                    "fields": [field],
                },
            )
            self.assertEqual(result["files"], [expected])
            self.assertEqual(
                self.watchmanCommand("debug-contenthash", root)["size"], 1
            )
        self.assertEqual(
            self.watchmanCommand("debug-contenthash", root)["cacheStore"], 3
        )

    def test_non_regular_files(self):
        root = self.mkdtemp()
        os.mkdir(os.path.join(root, "directory"))
        with open(os.path.join(root, "file"), "w") as f:
            f.write("hello")
        names = ["directory"]
        if os.name != "nt":
            os.symlink("file", os.path.join(root, "symlink"))
            names.append("symlink")
        self.watchmanCommand("watch", root)
        result = self.watchmanCommand(
            "query",
            root,
            {
                "expression": ["anyof"] + [["name", name] for name in names],
                "fields": ["name", "content.gitsha1hex"],
            },
        )
        self.assertEqual(len(result["files"]), len(names))
        for row in result["files"]:
            self.assertIsNone(row["content.gitsha1hex"])


@WatchmanTestCase.expand_matrix
class TestGitBlobHashSubscribe(WatchmanTestCase.WatchmanTestCase):
    def requiresPersistentSession(self):
        return True

    def test_atomic_replace_and_delete(self):
        root = self.mkdtemp()
        os.mkdir(os.path.join(root, "nested"))
        path = os.path.join(root, "nested", "file")
        original = b"original"
        with open(path, "wb") as f:
            f.write(original)
        self.watchmanCommand("watch", root)
        self.watchmanCommand(
            "subscribe",
            root,
            "hashes",
            {
                "expression": ["name", "nested/file", "wholename"],
                "fields": [
                    "name",
                    "exists",
                    "content.sha1hex",
                    "content.gitsha1hex",
                ],
            },
        )

        def expect(data, git_hash):
            expected = {
                "name": "nested/file",
                "exists": data is not None,
                "content.sha1hex": (
                    hashlib.sha1(data).hexdigest() if data is not None else None
                ),
                "content.gitsha1hex": git_hash,
            }
            packets = self.waitForSub(
                "hashes",
                root,
                accept=lambda packets: any(
                    expected in packet.get("files", []) for packet in packets
                ),
            )
            self.assertIsNotNone(packets)

        expect(original, "94f3610c08588440112ed977376f26a8fba169b0")
        # An editor-style atomic replacement must invalidate both cached hashes.
        replacement = os.path.join(self.mkdtemp(), "replacement")
        changed = b"new contents after atomic replacement\x00\xff"
        with open(replacement, "wb") as f:
            f.write(changed)
        os.replace(replacement, path)
        expect(changed, "d72da05dfc564f316815ca5efd393c0d10ea5ea9")
        os.unlink(path)
        expect(None, None)
