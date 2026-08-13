# Deploy on unRAID — step by step

Two halves:

1. **First-time setup** — manual, once.
2. **Auto-deploy** — pull-based, like Argo CD boiled down to one machine:
   git is the desired state, an agent on the server polls and reconciles.
   GitHub has **no** access to the server — no SSH keys in CI, no inbound
   webhooks, no open ports. The server only has *read* access the other way.

Every step ends with a **Check** — don't move on until it's green.

## How it hangs together

```
push to main ──► GitHub Actions: tests + hygiene ──► image to GHCR, tagged sha-<commit>
                                                            ▲
   unRAID (every 5 min, User Scripts):                      │ pull (read-only)
   git fetch ── new commit? ── does the sha- image exist? ───┘
        │                          │
        │                          └─ no: CI still running or failed → wait
        └─ yes: smoke-test it — `both --dry-run` against the LIVE Grocy and HA
                    │
                    ├─ exit 0  → git reset --hard, pin the image, record the commit
                    └─ exit ≠0 → promote nothing; the box keeps running the last good commit
```

The security properties, in order of how much they carry:

- **Pull, not push.** There is no route into the server to deploy. CI doesn't
  know the server exists; it just puts an image on a shelf.
- **The tests are the gate.** If one fails, no image is built, and the agent
  has literally nothing to roll out.
- **The dry run is the second gate.** A cron worker has no container to
  healthcheck, so `both --dry-run` stands in — and it's a stronger check than
  a healthcheck, because it exercises the real credentials, the real API
  shapes and the real reconcile logic before any write is permitted.
- **Everything is pinned to a commit SHA.** The agent deploys `sha-<commit>`,
  never `latest`. The cron scripts and the image they run come from the same
  commit.
- **The server holds no GitHub credentials at all.** Public repo → https
  clone, no deploy key. Public package → no token. The only secrets on the box
  are the Grocy and HA tokens the worker itself needs.

**Rollback needs no code and no rollback path.** Nothing runs continuously, so
a failed deploy is simply one that never happened: the pin file and the git
tree both stay where they were.

## What you need up front

- **unRAID 7.x** with terminal access (the `>_` icon top-right, or SSH as root).
- **The repo pushed to GitHub** (`ninkaninus/HA-setup`), and its GHCR package
  made public — see step 4.
- **User Scripts** from the **Apps** tab (Community Applications).
- **Don't install git.** Stock unRAID has none, and that's accounted for: both
  the first clone and the agent run git in a throwaway `alpine/git` container
  when the command is missing.

Filesystem layout, which every command below assumes:

```
/mnt/user/appdata/ha-setup/
  repo/                    the git clone — the agent keeps it at origin/main
  secrets/                 tokens, chmod 700 — NEVER in git
    grocy-lists.env
  state/
    grocy-lists.image      the pinned ghcr.io/…:sha-<commit> the cron jobs run
  .deploy-state            last commit that passed its smoke test
  deploy.log               the agent's log
  grocy-lists.log          the worker's log
```

`/root` on unRAID lives in RAM and does not survive a reboot, which is why the
secrets live in appdata. They are **not** on `/boot` either: that's vfat, where
`chmod 600` is silently a no-op.

---

## Part 1: First-time setup

### Step 1 — directories and clone

Open the unRAID terminal:

```bash
mkdir -p /mnt/user/appdata/ha-setup/{secrets,state}
chmod 700 /mnt/user/appdata/ha-setup/secrets

docker run --rm -v /mnt/user/appdata/ha-setup:/work -w /work \
  alpine/git clone https://github.com/ninkaninus/HA-setup.git repo
```

**Check:** `ls /mnt/user/appdata/ha-setup/repo` shows `deploy`,
`grocy-to-keep-integration`, `.github`.

### Step 2 — the worker's credentials

**Migrating from the old manual install?** The env file already exists — move
it and skip the editing:

```bash
cp /boot/config/plugins/user.scripts/grocy-lists.env \
   /mnt/user/appdata/ha-setup/secrets/grocy-lists.env
chmod 600 /mnt/user/appdata/ha-setup/secrets/grocy-lists.env
```

(The old `/boot` path still works as a fallback, so nothing breaks in the
meantime. Delete it once this is running — a copy of a live token on a vfat
stick with no meaningful permissions is not somewhere it should stay.)

From scratch instead:

```bash
cd /mnt/user/appdata/ha-setup
cp repo/grocy-to-keep-integration/.env.example secrets/grocy-lists.env
nano secrets/grocy-lists.env      # GROCY_API_KEY and HA_TOKEN
chmod 600 secrets/grocy-lists.env
```

**Check:** `grep -c . /mnt/user/appdata/ha-setup/secrets/grocy-lists.env`
returns a number, and `GROCY_API_KEY=` / `HA_TOKEN=` are not blank.

### Step 3 — confirm the GHCR package is public

It already is: `ha-setup/grocy-lists` was published public by the first CI run,
inheriting the repo's visibility. Verified 2026-08-12 by fetching its manifest
anonymously — which is exactly what the box does, holding no credentials.

Worth confirming rather than assuming, because a private package fails at the
last possible moment: the agent pulls, gets `denied`, and reports "image not
published yet" forever.

```bash
gh api /user/packages?package_type=container \
  --jq '.[] | select(.name=="ha-setup/grocy-lists") | .visibility'   # -> public
```

If it ever reads `private` — GHCR's default for packages *not* linked to a
public repo — packages are **user-scoped**, so the setting lives under your
profile, not inside the repo:
`https://github.com/users/ninkaninus/packages/container/package/ha-setup%2Fgrocy-lists`
→ **Package settings** (gear, bottom right) → **Danger Zone → Change package
visibility → Public**.

**Check** (from the unRAID terminal — the authoritative answer, since the
package page can lag behind in the UI):

```bash
docker pull ghcr.io/ninkaninus/ha-setup/grocy-lists:latest
```

### Step 4 — run the agent once, by hand

```bash
/mnt/user/appdata/ha-setup/repo/deploy/autodeploy.sh
```

Expected output (SHAs will differ):

```
2026-08-12 21:03:12  deploying 664d505… (from ac73ad2)
2026-08-12 21:03:31  ok — 664d505… smoke-tested and pinned
```

Run it again straight away and it must be **completely silent** — a reconciled
state produces no output. Other outcomes:

- `image for … not published yet` → CI hasn't finished, or the tests failed.
- `FEJL: smoke test failed` → the image is fine but it can't talk to Grocy or
  HA with those credentials. The failing dry run's last 20 lines are printed
  underneath. Nothing was promoted; fix the env file and run it again.

**Check:** `cat /mnt/user/appdata/ha-setup/state/grocy-lists.image` prints a
`ghcr.io/…:sha-<commit>` ref.

---

## Part 2: The three User Scripts

unRAID → **Settings → User Scripts** → **Add New Script**, three times. Each
one is a stub pointing at a script *inside the repo*, so they update
themselves along with everything else.

| Script name | Schedule (Custom) |
|---|---|
| `ha-setup-deploy` | `*/5 * * * *` |
| `grocy-lists-sync` | `*/15 * * * *` |
| `grocy-lists-analyse` | `0 6 * * 1` |

These are the round minutes, which is what is installed. Note what that
implies: the deploy agent and the sync job fire together at :00, :15, :30 and
:45, and share those minutes with every other cron on the box, including
`allergiscan-deploy`. At this workload that is fine — one image pull and one
dry run.

**Once heavier jobs land here, offset them.** Cron cannot express an offset
step (`*/5` always counts from zero), so the minutes have to be listed:

| Script name | Offset schedule |
|---|---|
| `ha-setup-deploy` | `2,7,12,17,22,27,32,37,42,47,52,57 * * * *` |
| `grocy-lists-sync` | `4,19,34,49 * * * *` |
| `grocy-lists-analyse` | `23 6 * * 1` |

The sync minutes are chosen to miss every deploy minute. That matters because
a cron job reads the image pin *without* taking the deploy lock — so while the
schedules overlap, the atomic rename in `promote()` is the only thing
preventing a run from reading a half-written image ref. Do not remove it.

**`ha-setup-deploy`**

```bash
#!/bin/bash
#description=Pull-based auto-deploy of HA-setup. Log: /mnt/user/appdata/ha-setup/deploy.log
exec /mnt/user/appdata/ha-setup/repo/deploy/autodeploy.sh \
  >> /mnt/user/appdata/ha-setup/deploy.log 2>&1
```

**`grocy-lists-sync`**

```bash
#!/bin/bash
#description=Grocy below-minimum -> the shared Indkøb list. Log: /mnt/user/appdata/ha-setup/grocy-lists.log
exec /mnt/user/appdata/ha-setup/repo/grocy-to-keep-integration/deploy/grocy-lists-sync.sh \
  >> /mnt/user/appdata/ha-setup/grocy-lists.log 2>&1
```

**`grocy-lists-analyse`**

```bash
#!/bin/bash
#description=Weekly min_stock_amount suggestions. Log: /mnt/user/appdata/ha-setup/grocy-lists.log
exec /mnt/user/appdata/ha-setup/repo/grocy-to-keep-integration/deploy/grocy-lists-analyse.sh \
  >> /mnt/user/appdata/ha-setup/grocy-lists.log 2>&1
```

For each: **Save**, then in the dropdown beside the script choose **Custom**,
enter the cron expression, **Apply**.

**Check — do not skip this.** There was a known bug in User Scripts on some
unRAID 7.1.x builds where a Custom schedule showed in the UI but was never
written to cron. It hasn't been seen on 7.3.2. Check anyway; it takes ten
seconds, and the alternative is a set of jobs that look installed and never run:

```bash
grep -E 'ha-setup|grocy-lists' /etc/cron.d/root
```

Three lines, with the right expressions. If not: run `update_cron` and check
again. Still nothing → reopen the User Scripts page, set the schedule, Apply,
`update_cron`. Worth re-checking once after a server reboot.

**Final check of the whole chain:** make a harmless commit (a line in a
README), push, and wait. Within ~10 minutes:

```bash
tail -5 /mnt/user/appdata/ha-setup/deploy.log      # "ok — <new sha> smoke-tested and pinned"
cat /mnt/user/appdata/ha-setup/state/grocy-lists.image   # …:sha-<new sha>
```

### Cleaning up the old install

Once the above is green, the hand-built image and its source copy are dead
weight:

```bash
rm -rf /mnt/user/appdata/grocy-lists-src
rm -f /boot/config/plugins/user.scripts/grocy-lists.env
docker rmi grocy-lists          # the locally built, untagged one
```

Delete the two old User Scripts entries that had the script bodies pasted into
them, or you'll be running two schedulers against one worker.

---

## Everyday life

- **Deploy** = push (or merge) to main. 5–10 minutes later it's running.
- **Config changes** — tuning `LOOKBACK_DAYS`, `SAFETY_K` and friends — are
  edits to `secrets/grocy-lists.env` on the box, not commits. They take effect
  on the next run. Only `.env.example` is in git.
- **Manual rollback**: `git revert` the bad commit and push. The agent deploys
  the revert like any other change. History only moves forward.
- **Never edit files under `repo/`.** The next `git reset --hard` overwrites
  them. Everything the agent doesn't own lives in `secrets/` and `state/`,
  outside the clone, precisely so this is safe.
- **Adding a second worker**: a line in `UNITS` in `deploy/autodeploy.sh`, a
  build step in the CI workflow, and an env file in `secrets/`. Units are
  promoted all-or-nothing per commit, so they can never be out of step.

## Troubleshooting

| Symptom | Look here | Likely cause |
|---|---|---|
| `image for … not published yet` for more than 10 min | GitHub → Actions | A test failed — the image is deliberately not built |
| `docker pull` says `denied`/`unauthorized` | Step 3 | The package is still private |
| `FEJL: smoke test failed` | the 20 printed lines | Bad/expired HA token, Grocy unreachable, or a real bug in the new commit. Nothing was promoted |
| `FEJL: no env file` | Step 2 | `secrets/grocy-lists.env` missing or misnamed |
| `no pinned image for grocy-lists` | Step 4 | The cron ran before the agent's first successful deploy |
| Works by hand, never runs on its own | `grep grocy /etc/cron.d/root` | The User Scripts bug — run `update_cron` |
| Rows flicker on the list every 15 min | `grocy-lists.log` | Two schedulers on one worker — an HA `shell_command` left over alongside the cron |

Lines with `FEJL:` need hands. The agent only writes those when it could
neither deploy nor safely leave things alone.

## Deliberate omissions

- **No webhook/push deploy.** An inbound endpoint is a door, and the point of
  this shape is having as few doors as possible. Five-minute polling is fast
  enough (Argo CD itself polls every three).
- **No `latest` in production.** The pin file holds a SHA ref. `latest` exists
  only for manual pulls.
- **No `paths:` filter in CI.** Every commit on main publishes an image, even a
  docs-only one, because the agent deploys by commit SHA and would otherwise
  wait forever for a build that never happens. The layer cache is what keeps
  that cheap.
- **No image signing (cosign).** commit → CI → GHCR → digest is traceable
  enough for one household.
- **No secrets in CI.** The workflow uses the built-in `GITHUB_TOKEN` only.
  There is nothing to rotate and nothing to leak.
