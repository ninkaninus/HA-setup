# Setup — the phone widget, then the cron

Google Keep is gone. The account is in Google's **Advanced Protection Program**,
which permanently disables App Passwords — the only way `gkeepapi` could obtain
a master token. There is no workaround, and staying in APP is the right call.
The household uses the Home Assistant list directly instead, which also drops a
fragile unofficial dependency.

---

## Part 1 — The list on her phone

### 1.1 Install the Home Assistant Companion app

Play Store → **Home Assistant**. Sign in and let it connect to your instance.

### 1.2 Add the to-do list widget

The Companion app ships a **To-do list** home-screen widget. On the OnePlus
10 Pro (OxygenOS):

1. Long-press an empty spot on the home screen → **Widgets**
2. Find **Home Assistant** → **To-do list**
3. Drag it onto the home screen
4. When the config screen appears, pick the **Indkøb** list

She can then tick items straight from the home screen, and the button in the
widget header opens the full list in the app for adding things.

### 1.3 What she can and can't do

She can **add whatever she likes** to `Indkøb` — the worker never touches rows
it didn't write. It only claims rows in its own shape:

```
Grøn pesto — 1/2 Glas      <- worker's: "name — have/min unit"
Blomster til bordet        <- hers, left strictly alone
```

The one way to confuse it is writing a manual item that happens to look like
`Something — 1/2 Liter`. The em-dash makes that unlikely by hand.

Row order is hers to set — press and hold a row to drag it. New rows are
appended at the bottom, so if she wants the Grocy block out of the way she can
drag it down once and it stays.

---

## Part 2 — The cron on unRAID

**The full procedure lives in [`../deploy/UNRAID.md`](../deploy/UNRAID.md)** —
it covers the whole repo, not just this worker. The short version:

The box no longer gets a hand-copied folder and a local `docker build`. It
clones this repo and runs a deploy agent every five minutes. The agent fetches
`origin/main`, checks whether CI published an image for that exact commit,
smoke-tests it with `sync --dry-run` against the live Grocy and Home Assistant,
and only then pins it for the cron jobs to use. A commit whose dry run fails is
never promoted, so the box keeps running the last one that worked.

What that changes for this worker:

- **Deploying a code change** = push to main. Nothing is built on the server.
- **Changing config** — `LOOKBACK_DAYS`, `SAFETY_K`, the lists — is an edit to
  `/mnt/user/appdata/ha-setup/secrets/grocy-lists.env` on the box. Only
  `.env.example` is in git; the real file never is.
- **The scripts in `deploy/`** are no longer pasted into the User Scripts
  editor. The User Scripts entries are three-line stubs that exec them out of
  the clone, so they update themselves.

The worker is still **stateless** — no volume to mount, all state lives in
Grocy and Home Assistant.

### Don't double-schedule

Pick the cron *or* an HA `shell_command` — not both. Two schedulers on one
worker gives you overlapping runs. `run-unit.sh` wraps the container in
`timeout` and `--rm` so a hung run can't stack containers, but that is a safety
net, not a licence to run two schedulers.

---

## What "working" looks like

- **Indkøb** (the default page of the `Grocy` dashboard, and the phone widget)
  holds her own items plus one row per product below its minimum, formatted
  `Grøn pesto — 1/2 Glas`. It stops changing once converged.
- **Forslag** — the second page — refreshes Mondays with whole-number minimum
  suggestions. Ticking one applies it to Grocy within 15 minutes.

Exit codes: `0` fine · `1` fatal.

## If something looks wrong

| Symptom | Cause |
|---|---|
| Her hand-added items disappeared | a manual row matched the worker's `name — have/min` shape; rename it |
| Rows keep re-appearing after ticking | the purchase isn't booked in Grocy, so stock is still below minimum |
| HTTP 403 from Grocy | Cloudflare rejecting the User-Agent; `USER_AGENT` in `.env` overrides it |
| A suggestion won't apply | its row text was edited by hand — the target value is parsed out of it |

## The thing that actually decides whether this works

All of it is downstream of purchases being booked into Grocy. If groceries
enter the house unrecorded, stock stays below minimum, the row never leaves the
list, and the household learns to ignore it. Barcode purchase flow on a phone is
what makes the model hold — if the first few weeks look noisy, the fix is
process, not code.
