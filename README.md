# HA-setup

Home Assistant automation that runs outside Home Assistant — workers on the
unRAID box that talk to HA over its API, deployed from this repo.

This repo is **public and contains no secrets**. Every credential lives on the
box in `/mnt/user/appdata/ha-setup/secrets/`, and only `.env.example` files are
committed. A hygiene job in CI fails the build if that ever stops being true,
because `.gitignore` only protects you until someone runs `git add -f`.

## What's here

| Path | What |
|---|---|
| `grocy-to-keep-integration/` | `grocy_lists` — reconciles Grocy's below-minimum products onto the household's shared HA shopping list, and derives weekly `min_stock_amount` suggestions |
| `deploy/` | the pull-based deploy agent and the unRAID runbook |
| `.github/workflows/` | tests, secret hygiene, and the image build |

## How a change reaches the server

```
push to main ──► CI: tests + hygiene ──► ghcr.io/…:sha-<commit>
                                                 ▲
   unRAID, every 5 min:  git fetch ──────────────┘
        └─ new commit + image exists?
             └─ smoke-test it (`sync --dry-run`, live Grocy + HA, writes nothing)
                  ├─ exit 0  → pin the image, reset the tree to that commit
                  └─ exit ≠0 → promote nothing; keep running the last good commit
```

Git is the desired state; the server polls and reconciles. GitHub has no access
to the server — no keys in CI, no webhooks, no open ports. Deploying is just
pushing to main.

**Setup and troubleshooting: [`deploy/UNRAID.md`](deploy/UNRAID.md).**

## Working on it

```bash
cd grocy-to-keep-integration
pip install -r requirements.txt pytest
pytest tests -q
```

The tests are the deploy gate — a red test means no image is published and the
server has nothing to roll out. The second gate is on the server: a new image
only gets promoted after a real `--dry-run` against the live systems succeeds.
