# commercebuild-dev-swarm

Runtime repository for the commercebuild development swarm: scheduler, review intake, and worker orchestration scripts. Canonical change record for the initial implementation is the issue in this repository; source request: https://github.com/commercebuildteam/codex-custom-skills/issues/64

## Install the review-only service

The source migration and scheduler are tracked at https://github.com/commercebuildteam/commercebuild-dev-swarm/issues/1. Install from a clean, reviewed commit on `main`. The installer copies that commit to `~/.local/share/commercebuild-dev-swarm/versions/<SHA>`, points `current` to it, links the versioned reviewer role into OpenCode's agent directory, and stages a LaunchAgent outside `~/Library/LaunchAgents`. Installation doesn't start the service, including at the next login. It doesn't change an existing review service or copy developer queue entries.

```sh
python3 scripts/install_review.py install --model 'openai/<available-review-model>'
python3 scripts/install_review.py status
```

Use an available, authorized non-OpenRouter model in the first command. To retain settled review signatures and discovery snapshots from an inactive older OpenCode scheduler, add `--migrate-from ~/.agents/state/commercebuild-opencode-swarm` to the install command. The legacy state remains untouched. Active workers, claims, pending jobs, and developer work never migrate. Fresh GitHub discovery recreates eligible review jobs in the isolated `~/.agents/state/commercebuild-dev-review-swarm` directory. The installer refuses migration while a swarm service or launcher is active or claims remain unresolved.

After the reviewed install is ready and the existing review supervisor has been stopped through its own supported procedure, the reviewer can explicitly start this service:

```sh
python3 scripts/install_review.py activate
python3 scripts/install_review.py status
```

Installation and activation check the staged commit against remote `main` and require an approved, merged pull request for its exact head. Both steps compare the staged scripts and reviewer role with that commit's Git tree, including an existing version directory. Activation also checks the staged LaunchAgent configuration before starting it, along with competing swarm services, the Codex registration, the `commercebuild` GitHub identity, and OpenCode availability. It then places the staged LaunchAgent in `~/Library/LaunchAgents` and starts it. The LaunchAgent runs `review-loop` with two slots and no auto-permission flag. That mode uses only the reviewer agent; developer queue entries and the coordinator aren't dispatched. Promotion, PR review request, and assigned issue intake run on independent 60-second polling threads, including when both slots are full. Ordering is promotions first, then verified Donavon review requests, then the ordinary ticket priority. A Donavon request goes ahead of every ticket priority, including critical and security work. The two slots are the total worker cap: there's no separate review cap, and one worker per repository runs at a time. The reviewer agent can post findings as `commercebuild`; it can't approve, merge, deploy, or replace bhalecb's formal review.

For a graceful stop, create `~/.agents/state/commercebuild-dev-review-swarm/stop-dispatch`. Running reviews finish before the process exits. Run `python3 scripts/install_review.py status` until `running` is false, then `python3 scripts/install_review.py deactivate` to unload launchd before an upgrade. Install the new clean reviewed commit, remove the stop marker, and activate again when approved. The old version remains in `versions/` for rollback by reinstalling its source commit.

Inspect `launchd.stdout.log`, `launchd.stderr.log`, and `runtime.json` in the isolated state directory. To generate a dashboard from that same state, run:

```sh
COMMERCEBUILD_SWARM_STATE="$HOME/.agents/state/commercebuild-dev-review-swarm" python3 scripts/swarm-dashboard.py generate
```

Installation and local tests don't prove a live restart, 60-second GitHub intake, reviewer execution, or capacity behavior. Verify those on the installed host before closing https://github.com/commercebuildteam/commercebuild-dev-swarm/issues/1.
