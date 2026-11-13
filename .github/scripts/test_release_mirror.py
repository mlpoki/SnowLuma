"""Offline behavior tests, run by the fork Actions before any publication."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from release_mirror import (
    FORK, MARKER, UPSTREAM, Missing, MirrorError, Tags, check_file,
    git, required_names, run, select_assets,
)


class FakeTags:
    def __init__(self):
        self.locked = {"object": "a" * 40, "commit": "b" * 40, "type": "tag"}
        self.fork = None
        self.changed = False
        self.missing = False
        self.writes = 0

    def lock(self, tag):
        if self.missing:
            raise MirrorError("Missing upstream tag")
        return self.locked.copy()

    def remote(self, remote, tag):
        return self.fork if remote == "fork" else {key: self.locked[key] for key in ("object", "commit")}

    def check(self, remote, tag, locked, allow_missing=False):
        if remote == "upstream" and self.changed:
            raise MirrorError("upstream tag conflict or changed input")
        if remote == "fork":
            if self.fork is None and allow_missing:
                return False
            if self.fork != {key: locked[key] for key in ("object", "commit")}:
                raise MirrorError("fork tag conflict")
        return True

    def sync(self, tag, locked):
        if not self.check("fork", tag, locked, True):
            self.writes += 1
            self.fork = {key: locked[key] for key in ("object", "commit")}


class FakeAPI:
    def __init__(self):
        self.source = {"id": 42, "tag_name": "v1.2.3", "draft": False, "prerelease": False,
                       "name": "Original title", "body": "Original notes\nhttps://upstream.example/update",
                       "html_url": f"https://github.com/{UPSTREAM}/releases/tag/v1.2.3"}
        self.source_assets = []
        self.bytes = {}
        for index, name in enumerate(required_names("v1.2.3"), 1):
            data = (name + " original content").encode()
            self.bytes[index] = data
            self.source_assets.append({"id": index, "name": name, "size": len(data),
                                       "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                                       "state": "uploaded", "updated_at": "fixed"})
        self.target = None
        self.target_assets = []
        self.next_id = 100
        self.latest_calls = 0
        self.writes = []
        self.fail_upload_at = None
        self.no_release = False
        self.http_error = False
        self.change_release = False
        self.change_assets = False
        self.corrupt_download = False
        self.workflow_state = "active"

    def request(self, method, path, data=None, binary=False, upload=False):
        parsed = urlparse(path)
        route = parsed.path
        if method == "GET":
            if route == f"/repos/{FORK}":
                return {"parent": {"full_name": UPSTREAM}}
            if route.endswith("/releases/latest"):
                self.latest_calls += 1
                if self.no_release:
                    raise Missing("HTTP 404")
                if self.http_error:
                    raise MirrorError("HTTP 403 rate limited")
                return copy.deepcopy(self.source)
            if route == f"/repos/{UPSTREAM}/releases/42":
                current = copy.deepcopy(self.source)
                if self.change_release:
                    current["tag_name"] = "v1.2.4"
                return current
            if route == f"/repos/{FORK}/releases":
                return [copy.deepcopy(self.target)] if self.target else []
            if route == f"/repos/{FORK}/releases/99":
                return copy.deepcopy(self.target)
            if route == f"/repos/{FORK}/actions/workflows/release.yml":
                return {"state": self.workflow_state}
        self.writes.append((method, path))
        if method == "PUT":
            self.workflow_state = "disabled_manually" if route.endswith("/disable") else "active"
            return None
        if upload:
            if len(self.target_assets) == self.fail_upload_at:
                raise MirrorError("Interrupted upload")
            name = parse_qs(parsed.query)["name"][0]
            asset = {"id": self.next_id, "name": name, "size": len(data), "state": "uploaded",
                     "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
            self.bytes[self.next_id] = data
            self.next_id += 1
            self.target_assets.append(asset)
            return copy.deepcopy(asset)
        if method == "POST":
            self.target = dict(data, id=99, html_url=f"https://github.com/{FORK}/releases/tag/v1.2.3")
            return copy.deepcopy(self.target)
        if method == "PATCH":
            self.target.update(data)
            return copy.deepcopy(self.target)
        raise AssertionError((method, path))

    def assets(self, repo, release_id):
        assets = copy.deepcopy(self.source_assets if repo == UPSTREAM else self.target_assets)
        if repo == UPSTREAM and self.change_assets and self.latest_calls and hasattr(self, "downloaded"):
            assets[0]["id"] += 1000
        return assets

    def download(self, repo, asset, destination):
        self.downloaded = True
        data = self.bytes[asset["id"]]
        destination.write_bytes(b"broken" if self.corrupt_download and repo == UPSTREAM else data)


class MirrorBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.evidence = self.directory / "evidence"
        self.api = FakeAPI()
        self.tags = FakeTags()

    def mirror(self):
        return run(self.api, self.tags, self.directory, self.evidence)

    def assert_stopped_without_writes(self, message):
        with self.assertRaisesRegex(MirrorError, message):
            self.mirror()
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.tags.writes, 0)

    def test_first_sync_and_identical_retry(self):
        first = self.mirror()
        ids = [asset["id"] for asset in self.api.target_assets]
        self.assertEqual(first["status"], "complete")
        self.assertEqual(len(first["uploaded"]), 6)
        self.assertEqual(self.api.latest_calls, 1)
        self.assertTrue(self.api.target["body"].startswith(self.api.source["body"]))
        self.assertIn(MARKER, self.api.target["body"])
        self.assertFalse(self.api.target["draft"])
        self.assertEqual(self.tags.writes, 1)
        self.assertEqual(self.api.workflow_state, "active")
        pauses = [path for method, path in self.api.writes if path.endswith("/disable")]
        self.assertEqual(len(pauses), 1)
        second = self.mirror()
        self.assertEqual(second["uploaded"], [])
        self.assertEqual(len(second["skipped"]), 6)
        self.assertEqual(ids, [asset["id"] for asset in self.api.target_assets])
        self.assertEqual(self.api.latest_calls, 2)
        pauses = [path for method, path in self.api.writes if path.endswith("/disable")]
        self.assertEqual(len(pauses), 1)

    def test_tag_push_failure_restores_workflow(self):
        def fail_sync(tag, locked):
            raise MirrorError("tag push denied")

        self.tags.sync = fail_sync
        with self.assertRaisesRegex(MirrorError, "tag push denied"):
            self.mirror()
        self.assertEqual(self.api.workflow_state, "active")
        self.assertIsNone(self.api.target)

    def test_no_formal_release(self):
        self.api.no_release = True
        self.assert_stopped_without_writes("no latest formal release")

    def test_http_403_is_distinct(self):
        self.api.http_error = True
        self.assert_stopped_without_writes("HTTP 403")

    def test_missing_asset(self):
        self.api.source_assets.pop()
        self.assert_stopped_without_writes("Actual assets")

    def test_duplicate_asset(self):
        self.api.source_assets.append(self.api.source_assets[0].copy())
        self.assert_stopped_without_writes("duplicated")

    def test_missing_tag(self):
        self.tags.missing = True
        self.assert_stopped_without_writes("Missing upstream tag")

    def test_conflicting_tag_same_commit_different_annotation(self):
        self.tags.fork = {"object": "c" * 40, "commit": self.tags.locked["commit"]}
        self.assert_stopped_without_writes("fork tag conflict")

    def test_changed_source_tag(self):
        self.tags.changed = True
        self.assert_stopped_without_writes("changed input")

    def test_changed_source_release(self):
        self.api.change_release = True
        self.assert_stopped_without_writes("release changed")

    def test_changed_source_asset(self):
        self.api.change_assets = True
        self.assert_stopped_without_writes("assets changed")

    def test_bad_download(self):
        self.api.corrupt_download = True
        self.assert_stopped_without_writes("Size mismatch")

    def test_same_size_wrong_digest(self):
        asset = self.api.source_assets[0]
        self.api.bytes[asset["id"]] = b"x" * asset["size"]
        self.assert_stopped_without_writes("Digest mismatch")

    def test_hand_written_release(self):
        self.api.target = {"id": 99, "tag_name": "v1.2.3", "body": "User notes"}
        self.assert_stopped_without_writes("hand-written")

    def test_partial_upload_resume_and_no_digest_fallback(self):
        self.api.fail_upload_at = 2
        with self.assertRaisesRegex(MirrorError, "Interrupted"):
            self.mirror()
        self.assertTrue(self.api.target["draft"])
        self.assertIn("incomplete", self.api.target["body"])
        partial = json.loads((self.evidence / "verification.json").read_text())
        self.assertEqual(partial["status"], "incomplete")
        self.assertEqual(len(partial["uploaded"]), 2)
        self.api.fail_upload_at = None
        for asset in self.api.target_assets:
            asset.pop("digest")
        report = self.mirror()
        self.assertEqual(len(report["skipped"]), 2)
        self.assertEqual(len(report["uploaded"]), 4)

    def test_conflicting_fork_bytes_prevents_all_writes(self):
        self.mirror()
        self.api.writes.clear()
        asset = self.api.target_assets[-1]
        self.api.bytes[asset["id"]] = b"x" * asset["size"]
        asset.pop("digest")
        with self.assertRaisesRegex(MirrorError, "Content conflict"):
            self.mirror()
        self.assertEqual(self.api.writes, [])

    def test_excludes_macos_preserves_unrelated_user_asset(self):
        self.api.source_assets.append(dict(self.api.source_assets[0], name="SnowLuma-v1.2.3-macos.zip", id=70))
        self.mirror()
        self.assertEqual(len(self.api.target_assets), 6)
        self.api.target_assets.append({"id": 800, "name": "user-notes.txt"})
        self.mirror()
        self.assertEqual(len(self.api.target_assets), 7)
        self.assertTrue(all("macos" not in asset["name"] for asset in self.api.target_assets))

    def test_existing_public_release_supplement_resume(self):
        self.mirror()
        self.api.target_assets.pop()
        report = self.mirror()
        self.assertEqual(len(report["uploaded"]), 1)
        self.assertEqual(len(report["skipped"]), 5)

    def test_prerelease_rejected(self):
        self.api.source["prerelease"] = True
        self.assert_stopped_without_writes("not a formal release")

    def test_untrusted_tag_path_rejected(self):
        with self.assertRaisesRegex(MirrorError, "Unsafe"):
            required_names("v1/../../unsafe")


class RealGitTags(unittest.TestCase):
    def test_lightweight_annotated_nested_objects_and_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, fork, isolated = [root / name for name in ("upstream", "fork", "isolated")]
            for directory in (upstream, fork, isolated):
                directory.mkdir()
            git("init", cwd=upstream)
            git("config", "user.name", "Mirror Fixture", cwd=upstream)
            git("config", "user.email", "fixture@example.invalid", cwd=upstream)
            (upstream / "source.txt").write_text("Original source")
            git("add", ".", cwd=upstream)
            git("commit", "-m", "fixture", cwd=upstream)
            git("tag", "v1.0.0", cwd=upstream)
            git("tag", "-a", "v1.0.1", "-m", "original annotation", cwd=upstream)
            git("tag", "-a", "v1.0.2", "v1.0.1", "-m", "nested annotation", cwd=upstream)
            git("init", "--bare", cwd=fork)
            tags = Tags(isolated, "fixture-token", str(upstream), str(fork))
            for tag in ("v1.0.0", "v1.0.1", "v1.0.2"):
                locked = tags.lock(tag)
                tags.check("upstream", tag, locked)
                tags.sync(tag, locked)
                tags.sync(tag, locked)
                self.assertEqual(git("rev-parse", f"refs/tags/{tag}", cwd=fork), locked["object"])
                self.assertEqual(git("cat-file", "-p", f"refs/tags/{tag}", cwd=fork),
                                 git("cat-file", "-p", f"refs/tags/{tag}", cwd=upstream))
            locked = tags.lock("v1.0.1")
            git("tag", "-f", "-a", "v1.0.1", "-m", "different annotation", cwd=upstream)
            with self.assertRaisesRegex(MirrorError, "changed input"):
                tags.check("upstream", "v1.0.1", locked)
            changed = {**locked, "object": "f" * 40}
            with self.assertRaisesRegex(MirrorError, "conflict"):
                tags.sync("v1.0.1", changed)
            self.assertEqual(git("rev-parse", "refs/tags/v1.0.1", cwd=fork), locked["object"])


if __name__ == "__main__":
    unittest.main()
