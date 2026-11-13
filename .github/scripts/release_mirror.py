"""Mirror published upstream bytes and original Git objects on a hosted runner."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


UPSTREAM = "JosephTian876/SnowLuma"
FORK = "mlpoki/SnowLuma"
MARKER = "<!-- snowluma-release-mirror:v1 upstream=JosephTian876/SnowLuma -->"


class MirrorError(RuntimeError):
    pass


class Missing(MirrorError):
    pass


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(SafeRedirect())

    def request(self, method, path, data=None, binary=False, upload=False):
        host = "https://uploads.github.com" if upload else "https://api.github.com"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SnowLuma-release-mirror",
        }
        if upload:
            headers["Content-Type"] = "application/octet-stream"
        elif data is not None:
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(host + path, data, headers, method=method), timeout=180) as response:
                result = response.read()
                return result if binary else json.loads(result)
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:1000]
            if error.code == 404:
                raise Missing(f"HTTP 404 {method} {path}: {detail}") from error
            raise MirrorError(f"HTTP {error.code} {method} {path}: {detail}") from error

    def assets(self, repo, release_id):
        assets = []
        page = 1
        while True:
            batch = self.request("GET", f"/repos/{repo}/releases/{release_id}/assets?per_page=100&page={page}")
            assets.extend(batch)
            if len(batch) < 100:
                return assets
            page += 1

    def download(self, repo, asset, destination):
        destination.write_bytes(self.request("GET", f"/repos/{repo}/releases/assets/{asset['id']}", binary=True))


def git(*args, cwd, env=None):
    result = subprocess.run(["git", *args], cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise MirrorError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class Tags:
    def __init__(self, directory, token, upstream=None, fork=None):
        self.directory = directory
        self.token = token
        git("init", "--bare", cwd=directory)
        git("remote", "add", "upstream", upstream or f"https://github.com/{UPSTREAM}.git", cwd=directory)
        git("remote", "add", "fork", fork or f"https://github.com/{FORK}.git", cwd=directory)

    def remote(self, remote, tag):
        ref = f"refs/tags/{tag}"
        rows = git("ls-remote", remote, ref, ref + "^{}", cwd=self.directory)
        refs = dict(row.split("\t")[::-1] for row in rows.splitlines())
        if ref not in refs:
            return None
        return {"object": refs[ref], "commit": refs.get(ref + "^{}", refs[ref])}

    def lock(self, tag):
        git("check-ref-format", f"refs/tags/{tag}", cwd=self.directory)
        ref = f"refs/tags/{tag}"
        git("fetch", "--no-tags", "upstream", f"{ref}:{ref}", cwd=self.directory)
        return {
            "object": git("rev-parse", ref, cwd=self.directory),
            "commit": git("rev-parse", ref + "^{commit}", cwd=self.directory),
            "type": git("cat-file", "-t", ref, cwd=self.directory),
        }

    def check(self, remote, tag, locked, allow_missing=False):
        actual = self.remote(remote, tag)
        if actual is None and allow_missing:
            return False
        expected = {key: locked[key] for key in ("object", "commit")}
        if actual != expected:
            raise MirrorError(f"{remote} tag conflict or changed input for {tag}: expected {expected}, actual {actual}")
        return True

    def sync(self, tag, locked):
        if self.check("fork", tag, locked, allow_missing=True):
            return
        encoded = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        env = dict(os.environ, GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}")
        # Git refuses an existing tag update, including a concurrent conflicting creation.
        ref = f"refs/tags/{tag}"
        git("push", "fork", f"{ref}:{ref}", cwd=self.directory, env=env)
        self.check("fork", tag, locked)


def required_names(tag):
    if not re.fullmatch(r"v[0-9][A-Za-z0-9.+-]*", tag):
        raise MirrorError(f"Unsafe or unsupported upstream tag: {tag!r}")
    return [f"SnowLuma-{tag}-{platform}{lite}.{extension}"
            for platform, extension in (("win-x64", "zip"), ("linux-x64", "tar.gz"), ("linux-arm64", "tar.gz"))
            for lite in ("", "-lite")]


def select_assets(assets, tag):
    selected = []
    for name in required_names(tag):
        matches = [asset for asset in assets if asset["name"] == name]
        if len(matches) != 1 or matches[0]["size"] <= 0 or matches[0].get("state") != "uploaded":
            raise MirrorError(f"Required asset missing, duplicated or incomplete: {name}. Actual assets: "
                              + json.dumps(assets, ensure_ascii=False))
        selected.append(matches[0])
    if len({asset["id"] for asset in selected}) != 6:
        raise MirrorError("Duplicate upstream asset IDs")
    return selected


def identity(asset):
    return {key: asset.get(key) for key in ("id", "name", "size", "digest", "state", "updated_at")}


def check_file(path, asset, expected_hash=None):
    size = path.stat().st_size
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if size <= 0 or size != asset["size"]:
        raise MirrorError(f"Size mismatch for {asset['name']}: {size} != {asset['size']}")
    declared = asset.get("digest")
    if declared and declared != f"sha256:{digest}":
        raise MirrorError(f"Digest mismatch for {asset['name']}: {declared} != sha256:{digest}")
    if expected_hash and digest != expected_hash:
        raise MirrorError(f"Content conflict for {asset['name']}: {digest} != {expected_hash}")
    return digest


def find_release(api, tag):
    # List releases so incomplete drafts are visible on a retry.
    page = 1
    while True:
        batch = api.request("GET", f"/repos/{FORK}/releases?per_page=100&page={page}")
        found = [release for release in batch if release["tag_name"] == tag]
        if found:
            if len(found) != 1:
                raise MirrorError(f"Duplicate fork releases for {tag}")
            return found[0]
        if len(batch) < 100:
            return None
        page += 1


def assert_owned(release):
    if release is not None and MARKER not in (release.get("body") or ""):
        raise MirrorError(f"Existing hand-written release {release['id']} has no mirror source marker")


def run(api, tags, directory, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"status": "incomplete", "workflow_sha": os.getenv("WORKFLOW_SHA"),
              "run_url": f"https://github.com/{FORK}/actions/runs/{os.getenv('GITHUB_RUN_ID')}",
              "uploaded": [], "skipped": []}

    def save():
        (evidence / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    try:
        parent = api.request("GET", f"/repos/{FORK}")
        if parent.get("parent", {}).get("full_name") != UPSTREAM:
            raise MirrorError("Fork parent changed. Confirm official upstream before mirroring.")
        try:
            source = api.request("GET", f"/repos/{UPSTREAM}/releases/latest")
        except Missing as error:
            raise MirrorError("Upstream has no latest formal release (HTTP 404)") from error
        if source["draft"] or source["prerelease"]:
            raise MirrorError("Upstream latest is not a formal release")
        tag = source["tag_name"]
        assets = select_assets(api.assets(UPSTREAM, source["id"]), tag)
        locked = tags.lock(tag)
        report.update(upstream=source, selected_assets=assets, source_tag=locked)
        save()
        files = []
        for asset in assets:
            path = directory / asset["name"]
            api.download(UPSTREAM, asset, path)
            digest = check_file(path, asset)
            files.append({"upstream_name": asset["name"], "name": asset["name"],
                          "upstream_id": asset["id"], "size": asset["size"], "sha256": digest})
        report["files"] = files
        save()
        target = find_release(api, tag)
        assert_owned(target)

        def verify_target_assets(release):
            current = api.assets(FORK, release["id"])
            existing = {}
            for item in files:
                matches = [asset for asset in current if asset["name"] == item["name"]]
                if len(matches) > 1:
                    raise MirrorError(f"Duplicate fork asset {item['name']}")
                if matches:
                    asset = matches[0]
                    if asset["size"] != item["size"] or asset.get("state") != "uploaded":
                        raise MirrorError(f"Existing fork asset conflict: {item['name']}")
                    # Download even when API digest exists, verifying the actual published bytes.
                    path = directory / ("fork-" + item["name"])
                    api.download(FORK, asset, path)
                    check_file(path, asset, item["sha256"])
                    existing[item["name"]] = asset
            return existing

        existing = verify_target_assets(target) if target else {}

        def recheck_source():
            current = api.request("GET", f"/repos/{UPSTREAM}/releases/{source['id']}")
            keys = ("id", "tag_name", "draft", "prerelease", "name", "body", "html_url")
            if any(current.get(key) != source.get(key) for key in keys):
                raise MirrorError("Locked upstream release changed before publication")
            current_assets = select_assets(api.assets(UPSTREAM, source["id"]), tag)
            if [identity(a) for a in current_assets] != [identity(a) for a in assets]:
                raise MirrorError("Locked upstream assets changed before publication")
            tags.check("upstream", tag, locked)

        recheck_source()
        tags.check("fork", tag, locked, allow_missing=True)
        tags.sync(tag, locked)
        timestamp = datetime.now(timezone.utc).isoformat()
        body = (source.get("body") or "") + (
            f"\n\n---\n{MARKER}\n\n## Mirror provenance\n\n"
            f"- Upstream release [{source['tag_name']}]({source['html_url']})\n"
            f"- Upstream release ID `{source['id']}`\n"
            f"- Original tag `{tag}`, object `{locked['object']}`, commit `{locked['commit']}`\n"
            f"- Synchronized at `{timestamp}`\n"
            f"- Windows/Linux full and lite packages copied directly from upstream, without recompilation.\n"
            f"- 与上游包一致，未重新编译，未单独验证运行功能。\n"
            f"- Workflow commit `{report['workflow_sha']}`\n\n"
            "| Asset | Upstream ID | Bytes | SHA-256 |\n| --- | --- | --- | --- |\n"
        )
        body += "\n".join(f"| {f['name']} | {f['upstream_id']} | {f['size']} | `{f['sha256']}` |" for f in files)
        if target is None:
            target = api.request("POST", f"/repos/{FORK}/releases", {
                "tag_name": tag, "target_commitish": locked["commit"], "name": source["name"],
                "body": body + "\n\nMirror upload incomplete. This draft is retained for retry.",
                "draft": True, "prerelease": False,
            })
        report["fork_release_id"] = target["id"]
        report["fork_release_url"] = target["html_url"]
        save()
        for item in files:
            if item["name"] in existing:
                report["skipped"].append(item["name"])
            else:
                api.request("POST", f"/repos/{FORK}/releases/{target['id']}/assets?name={quote(item['name'])}",
                            (directory / item["name"]).read_bytes(), upload=True)
                report["uploaded"].append(item["name"])
            save()
        verified = verify_target_assets(target)
        if len(verified) != 6:
            raise MirrorError("Target release is incomplete after upload")
        recheck_source()
        tags.check("fork", tag, locked)
        latest_target = api.request("GET", f"/repos/{FORK}/releases/{target['id']}")
        assert_owned(latest_target)
        api.request("PATCH", f"/repos/{FORK}/releases/{target['id']}", {
            "name": source["name"], "body": body, "draft": False, "prerelease": False,
        })
        published = api.request("GET", f"/repos/{FORK}/releases/{target['id']}")
        tags.check("fork", tag, locked)
        if (published["draft"] or published["prerelease"] or published["tag_name"] != tag
                or published["name"] != source["name"] or published["body"] != body):
            raise MirrorError("Published release metadata verification failed")
        verified = verify_target_assets(published)
        if len(verified) != 6:
            raise MirrorError("Published release is incomplete")
        report.update(status="complete", fork_tag=tags.remote("fork", tag), fork_assets=list(verified.values()))
        save()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report
    except Exception as error:
        report["error"] = str(error)
        save()
        raise


def main():
    if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_REPOSITORY") != FORK:
        raise MirrorError(f"Run this publisher only in {FORK} GitHub Actions")
    root = Path(os.environ["RUNNER_TEMP"])
    evidence = root / "release-mirror-evidence"
    with tempfile.TemporaryDirectory(prefix="snowluma-mirror-", dir=root) as temporary:
        directory = Path(temporary)
        repo = directory / "source.git"
        repo.mkdir()
        try:
            report = run(GitHub(os.environ["GH_TOKEN"]), Tags(repo, os.environ["GH_TOKEN"]), directory, evidence)
        except Exception:
            summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
            with summary.open("a") as stream:
                stream.write("## Mirror incomplete\n\nInspect verification.json and uploaded/skipped lists in the evidence artifact. "
                             "A new release stays draft until verified. An existing public release may be partially supplemented.\n")
            raise
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
        stream.write(f"## Mirror verified\n\n[Release]({report['fork_release_url']})\n\n")
        stream.write("与上游包一致，未重新编译，未单独验证运行功能。\n\n")
        stream.write(f"Uploaded {len(report['uploaded'])}, reused {len(report['skipped'])} assets.\n\n")
        stream.write("```json\n" + json.dumps(report, indent=2, ensure_ascii=False) + "\n```\n")


if __name__ == "__main__":
    main()
