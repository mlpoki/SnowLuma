# Fork release mirror

Run **Mirror Upstream Release** manually from `main` in `mlpoki/SnowLuma` Actions.
There are no version inputs. Each run resolves upstream latest exactly once,
then locks that release ID, its original Git tag object and commit, and six
Windows/Linux full/lite assets. A newer latest release during the run does not
change its selected version.

The runner uses Python's standard library and Git with the repository
`GITHUB_TOKEN` and `contents: write`. Application dependency installation,
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
with `GITHUB_TOKEN` do not initiate another run. The current run publishes
directly. No scheduled synchronization or upstream Actions invocation is used.

Each run retains `release-mirror-evidence-<run>-<attempt>` for 30 days, including
the release snapshot, upstream asset IDs, original tag object/commit, sizes,
SHA-256, fork asset IDs, uploaded/reused lists, and completion or failure status.
The job summary and Release provenance section include verification results.

与上游包一致，未重新编译，未单独验证运行功能。
