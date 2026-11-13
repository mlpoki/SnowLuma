# Fork release mirror

Run **Mirror Upstream Release** manually from `main` in `mlpoki/SnowLuma` Actions.
There are no version inputs. Each run resolves upstream latest exactly once,
then locks that release ID, its original Git tag object and commit, and six
Windows/Linux full/lite assets. A newer latest release during the run does not
change its selected version.

The runner uses Python's standard library and Git. Publication uses the
repository secret `RELEASE_MIRROR_TOKEN`, populated from the owner's authorized
GitHub CLI OAuth token with `repo` and `workflow` scopes. This credential has
the CLI token's repository access scope. The owner approved storing it for this
workflow after the first real run encountered GitHub's workflow permission check.
`GITHUB_TOKEN` cannot receive the Workflows write permission required to copy an
upstream tag whose workflow differs from fork main. Application dependency installation,
compilation, npm publication and Docker dispatch are absent from this workflow.
The separate manual Docker/npm workflows are independent of the mirror path.

The original lightweight or annotated tag and reachable Git objects are copied
unchanged. `main` retains the fork's workflow implementation. Conflicting tags,
changed inputs, conflicting asset bytes and existing releases without the
mirror provenance marker stop publication. No existing tag is overwritten.

New releases remain drafts until all six uploads are byte-verified. A failed
run retains its draft and evidence artifact. Rerun the workflow to reuse
identical assets and supplement missing ones. Existing public releases can be
partially supplemented if an upload fails. Conflicting or incomplete same-name
assets cause a reported stop. Unrelated assets are retained.

The workflow tests missing releases/assets/tags, HTTP failures, changed inputs,
content conflicts, interrupted upload recovery, public-release supplementation,
macOS exclusion, and idempotence using offline fixtures. Real local Git fixtures
inside the runner verify lightweight, annotated and nested tag object copying.
These fixtures do not exercise GitHub service outages or real concurrent edits.

The optional `v*` push trigger only works when the pushed tag's source tree
contains this workflow. An original upstream tag can contain an older workflow.
The supported first entry point is manual dispatch from `main`. Tags pushed
with the OAuth token could trigger the original upstream build workflow. The
mirror temporarily disables its own workflow around a missing-tag push and
reenables it in a `finally` block and an always-run cleanup step. Existing runs
continue while the workflow is disabled. The current run publishes directly.
If a runner is lost during the pause, enable `release.yml` before retrying.
No scheduled synchronization or upstream Actions invocation is used.

Each run retains `release-mirror-evidence-<run>-<attempt>` for 30 days, including
the release snapshot, upstream asset IDs, original tag object/commit, sizes,
SHA-256, fork asset IDs, uploaded/reused lists, and completion or failure status.
The job summary and Release provenance section include verification results.

与上游包一致，未重新编译，未单独验证运行功能。
