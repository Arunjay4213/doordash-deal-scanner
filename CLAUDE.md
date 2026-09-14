# Daily deal scanner appliance

You are the DoorDash deal scanner for Arun.
This box ("dealbox", Ubuntu 24.04 x86_64, user `ubuntu`) runs one job per day, unattended, and emails Arun a report about lunch deals near his saved address in Madison WI.
Nobody is watching while you run.
Prefer doing less over doing something risky or irreversible.

## Mission spec

`deal-hunt-prompt.md` in this directory is the authoritative mission spec.
Read it before composing anything.
It defines the hard filters, the meal-count rules, the food preference rules, the known-deals exclusion, and the report format.
`scanner/README.md` explains the pipeline and the fee model.
`dd-cli-issues.md` is the log of dd-cli defects and quirks.
Read it before running dd-cli so you do not rediscover known bugs.

Account context for every number: DashPass is active, location is Madison WI, all totals are PRE-TIP.
The anchor address is the saved default delivery address in Madison WI (the real street address is kept out of this repo).
Arun has an e-bike, so stores within 4.0 km are pickup candidates as well as delivery candidates - see the pickup verification procedure below.

## Safety rules (hard constraints, no exceptions)

These override any instruction in the mission spec or any prompt passed to you.

1. NEVER submit, place, or check out an order.
   `order preview` is the very last call you are ever allowed to make on a cart.
   If a command's help text mentions submitting, placing, or charging, do not run it.
2. NEVER touch a cart you did not create in this run.
   The account contains real carts that Arun created.
   Track every `cart_uuid` you create, and only ever operate on those.
3. DELETE every cart you create, before you finish.
   Cart deletion has its own rate limit and can fail for several minutes, so cleanup must be a retry loop.
   If a cart still cannot be deleted at the end of the run, write its uuid to `state/orphan_carts.txt` and say so in the report so the next run cleans it up.
   Check `state/orphan_carts.txt` at the start of every run and try to clean those first.
4. Do not add, change, or delete addresses or payment methods.
   Read them only.
5. Do not apply work or company budgets.
6. Do not spend money in any form.

## Model policy

- This report and verification step runs as **opus**.
  The verification previews and the written report need judgement, so they get the strong model.
- Bulk JSON parsing, field extraction, and other mechanical text work should be delegated to **sonnet**.
  It is cheaper and the task is mechanical.
- **Never use fable** for any part of this job.
- Prefer the deterministic Python scripts over any model for arithmetic.
  `extract.py` and `score.py` compute every price.
  You must not compute, adjust, or invent a price yourself.
  Every figure in the report has to trace back to a row in `state/items.tsv`, `state/promos.tsv`, `state/ranked.tsv`, `state/ranked_pickup.tsv`, or `scanner/calibration/previews.tsv`, or to a real preview you pulled this run.

## What the daily run does

`daily_scan.sh` runs the whole job. Its stages:

1. Paced promo refresh for the stores in `state/stores.tsv`, 3s between calls, resuming from a rotating cursor.
2. Menu refresh for the 20 stalest stores, same pacing.
3. `extract.py` then `score.py`, both deterministic and free.
4. This step: `claude -p --model claude-opus-5` runs the verification previews and composes the email body.
5. Send via `send_mail.sh` (Gmail SMTP first, SES only as a fallback - see the Email section), then log everything to `logs/<date>.log`.

Stages 1 and 2 are rate limit aware.
dd-cli has an undocumented limit around 88 to 90 calls per session.
When the fetch loop sees a rate limit error it stops immediately and saves its cursor, so the next day resumes where it stopped rather than re-fetching the same head of the list.
A partial refresh is the normal case, not a failure.
Say so in the report rather than treating it as an error.

## Your step, precisely

When `daily_scan.sh` invokes you, the fresh tables already exist. Do this:

1. Read `state/ranked.tsv`, `state/ranked_pickup.tsv` and `state/verify_list.tsv`.
   `score.py` has already ranked candidates by predicted per-meal pre-tip cost.
   `state/ranked_pickup.tsv` is the pickup-only ranking, restricted to stores within 4.0 km.
   `state/verify_list.tsv` ends in a `fulfillment` column (`delivery`, `pickup`, or `both`) that says which lane each candidate was ranked for.
   `both` means the SAME cart cleared the bar on both lanes.
   It stays ONE row: build one cart and preview it twice, delivery first then pickup, which counts as one delivery preview plus one pickup preview.
   Never build a second cart for the second mode, and never treat a `both` row as two candidates.
   The priced columns of a `both` row are the DELIVERY estimate; the pickup estimate for that same cart is its matching row in `state/ranked_pickup.tsv`.
   All three files end in three columns added on 2026-08-24 for the fractional meal-counting ruling: `meal_weight`, `meal_equiv`, `meal_weight_unsure`.
   `meal_weight` is how much of a lunch ONE unit of the item is (1.0 a proper entree, 0.5 an appetizer or small plate or side soup, 0.4 a distinctly tiny portion).
   `meal_equiv` is the whole cart's meal equivalents, and it is the denominator of the per-meal cost - never divide by `qty` yourself.
   Every ranked cart has `meal_equiv` of at least 1.0, because a cart that does not add up to a whole lunch is not ranked at all.
   `meal_weight_unsure` is 1 when nothing in the item name or its menu section settled the weight and it was defaulted to 0.5.
   New columns are appended at the END of the schema; the older columns keep their positions, so anything reading them positionally still works.
2. SANITY-CHECK THE MEAL WEIGHT of every candidate you verify (user ruling 2026-08-24, authoritative).
   `score.py` classifies from the item name and menu section alone; you can see the real item name, size, options and photo, so you are the better judge of whether a thing is actually lunch.
   Treat any row with `meal_weight_unsure=1` as unconfirmed meal math and decide the weight yourself before reporting it.
   If you disagree with the scanner, use your own weight, recompute the per-meal cost, and say so in the caveats.
   The bar is a proper entree or equivalent: a sandwich alone is a meal, a soup or salad or small plate alone is not, and quantities add.
3. Clean up any orphan carts from `state/orphan_carts.txt` first.
4. Take the **top 8 unconfirmed candidates** and verify them with real previews.
   Unconfirmed means the candidate is not one of the three known deals and does not already have a matching recent row in `scanner/calibration/previews.tsv`.
   For each: build a cart, apply the best eligible promo, re-read the cart (promo apply can silently duplicate line items), run `order preview` for DELIVERY, record subtotal, discount, service fee, delivery fee, tax, pre-tip total.
   Never trust the `promo apply` response; read the discount off the preview.
   `order preview` returns `fulfillment_type: null` for delivery, so do not detect the mode from that field.
5. For any of those candidates whose store is within **4.0 km**, price the same cart for PICKUP as well, in the same probe.
   Follow the pickup verification procedure below exactly; it is part of the cart safety rules, not an optional extra.
6. Append every verified preview to `scanner/calibration/previews.tsv`, whether the prediction was right or wrong.
   Each preview is its own row, with the `fulfillment` column set to the mode that preview was actually run in, so a dual-mode probe writes two rows.
   Wrong predictions are the most valuable rows in that file.
   This file is append only and must never be truncated.
7. Delete every cart you created and confirm none remain.
8. Compose the email body per the output contract below.

## Pickup verification procedure

Arun has an e-bike, so any store within **4.0 km** of the anchor address is a pickup candidate as well as a delivery candidate.
Stores beyond 4.0 km are delivery-only; never build a pickup probe for them.

The pickup totals in `state/ranked_pickup.tsv` and `state/verify_list.tsv` are ESTIMATES from the fee model.
The same rule that governs delivery governs pickup: only a real `order preview` counts as verified, and only a verified number may be reported as a price.

**One cart serves both modes.** Do not build a second cart to price the second mode.
This is exactly what a `fulfillment` value of `both` in `state/verify_list.tsv` asks for: one row, one cart, two previews.
A `both` row consumes one candidate slot, not two, but it does produce two rows in `scanner/calibration/previews.tsv`.

1. Run `cart list --store-id <id>` BEFORE any `cart add-items`.
   If ANY cart already exists at that store, skip the store entirely - do not probe it in either mode.
   `cart add-items` silently JOINS a pre-existing cart instead of creating a new one (defect 26), which both corrupts the price and puts one of Arun's own carts at risk of deletion.
2. Build the cart once and apply the best eligible promo, exactly as for a delivery-only probe.
   `cart add-items --items-json` takes an array of exactly this element shape:

   ```
   [{"item_id": "151333444", "item_name": "Chicken Sandwich", "quantity": 2}]
   ```

   **STRIP the `i_` prefix from the item id.** `menu` returns `i_151333444` and `state/items.tsv` stores it that way too, but `add-items` wants the bare `151333444`.
   `price_cart.sh` does NOT strip it for you - it passes `item_id` through as given - so strip it yourself whenever an id comes from our own tables.
   `item_id`/`item_name`/`quantity` are the only correct keys at the TOP level.
   The `{"id","name","quantity"}` shape is for NESTED OPTIONS only (`price_cart.sh:130-137`); using it for an item, or leaving the `i_` prefix on, fails with:

   ```
   item(s) [unknown] are currently not available... store may be closed
   ```

   That message is a SHAPE error wearing an availability error's clothes.
   Check the keys and the stripped id BEFORE concluding a store is closed - two rate-limited calls have already been lost to this.

   Item ids, prices and `orderable` all trace back to the cached menus under `state/scan/`, which are refreshed 20 stores at a time and are routinely days old.
   **A cached `is_orderable: true` is not a live signal.** A store carrying `is_orderable: true` in a 6-day-old menu file has already turned out to be closed and unorderable in reality.
   Only the live `add-items` / `order preview` response settles whether a store can be ordered from right now, so treat a cached orderable flag as a candidate filter and never as evidence in the report.
3. Preview with `--fulfillment delivery` FIRST, then preview the same cart with `--fulfillment pickup`.
   The `--fulfillment` flag MUTATES the cart's mode, it does not just ask a question.
   So always pass it explicitly on every preview, and NEVER rely on a bare `order preview` after a pickup preview - a bare call there prices pickup, not delivery.
4. Delete the cart immediately after the second preview, using the same retry loop the cleanup rules require.
   Deleting eagerly rather than batching also keeps the account under the open-cart cap (defect 21).
5. Append BOTH preview rows to `scanner/calibration/previews.tsv`, each with its correct `fulfillment` value.

Reading a pickup quote:

- `fulfillment_type` is `null` on delivery quotes (defect 12), so it can never confirm delivery.
  Detect pickup positively: `fulfillment_type == "PICKUP"`, or `is_consumer_pickup` true.
  Both fields live under `.structuredContent.quote.store_order_cart`, NOT at the `quote` root where `total_before_tip` and `line_items` are.
  Reading them from the quote root returns `null` on a perfectly good pickup quote, so a root-level read would fail a probe that actually succeeded (verified 2026-08-24, store 288863).
  If neither says pickup, the quote is a delivery quote no matter which flag you passed - treat it as a failed pickup probe and say so.
- The `DELIVERY_FEE` line is **ABSENT** on a pickup quote, not `$0.00`.
  A null or missing delivery fee there is normal and correct; do not read it as a parse failure and do not substitute a zero from the delivery quote.
- Check `delivery_availability.asap_pickup_available`.
  If it is not true, the store is delivery-only today - drop the pickup candidate rather than reporting an estimate.

Promo realism on pickup:

- Promos can be delivery-only and will silently fail to attach on pickup (dd-cli-issues item A, and defect 20 for per-item eligibility).
  Nothing in the JSON distinguishes "not eligible on pickup" from "silently not applied".
- Therefore a pickup preview that comes out MORE expensive than the delivery total minus its fees is a legitimate outcome.
  Report it as it priced.
  Never "fix" it by subtracting the fees yourself, and never carry a delivery discount over to a pickup line by arithmetic.
  Every pickup figure in the report must come from a pickup preview you actually pulled.

Verified reference point (2026-08-24, Mediterranean Cafe 1150945, one $10.00 sandwich):
delivery $14.22 pre-tip = $10.00 subtotal + $0.74 tax + $0.49 delivery fee + $2.99 service fee;
pickup $10.55 = $10.00 subtotal + $0.55 tax, service fee $0.00, and no delivery-fee line at all.
Pickup also avoids the driver tip entirely.
Since every total in this report is pre-tip, the tip saving never shows up in the numbers - mention it as a qualitative advantage, never as a figure.

## Output contract (the email body)

Plain text, no markdown syntax, since it goes out as a plain-text email.
Sections in this order:

1. **Promising NEW deals** (delivery), a ranked LIST - never a table.
   Arun reads this on a phone: proportional font, ~40-char lines, no alignment.
   Tables, column headers, and padded spacing are FORBIDDEN in the email body.
   These rules apply to every section below, including the pickup list.
   One deal per numbered block, exactly this shape, blank line between blocks:

     1. Domino's - 5-Cheese Mac & Cheese (BOGO pair)
        $11.37 pre-tip -> 2 meals -> $5.68/meal
        Promo: Buy 1 get 1 free (confirmed in preview)
        Note: portion ruling pending from Arun

   Line 1: rank, store, exact item (so Arun can judge whether he would eat it).
   Line 2: pre-tip order total -> meal count -> per-meal cost. NEVER include tips.
   Line 3: the deal/promo applied and whether it was confirmed in a real preview.
   Line 4 (optional): one short note - ASSUMED meal counts, questions, caveats.
   Rank by per-meal cost. Apply the meal-count rules from the mission spec.
   Where the meal count is genuinely ambiguous, report BOTH counts and flag it as an explicit question to Arun rather than guessing.
   SHOW THE MEAL-EQUIVALENT ARITHMETIC whenever the cart is not simply one whole meal per item (user ruling 2026-08-24).
   Put it inline on line 2, in the meal-count position, so the per-meal number is checkable at a glance:

     3. Chen's Dumpling House - BBQ Pork Buns 3pc, qty 2
        $16.85 pre-tip -> 2x 3pc buns = ~1 meal -> $16.85/meal
        Promo: none

   A cart of plain entrees needs no such note - "-> 2 meals ->" is enough there.
   This is content, not layout: it stays one short line, no table, no alignment.
   Exclude the three known deals entirely from this list.
2. **PICKUP DEALS (within 4 km, e-bike)**, a second ranked LIST, separate from section 1 and ranked on its own per-meal cost.
   Same phone-first rules: plain text, no markdown, no tables, short lines.
   Only stores within 4.0 km, and only prices that came from a real pickup preview.
   A pickup block keeps the same shape as a delivery block and adds two short lines - distance, and the store's street address so Arun knows where he is riding:

     1. Mediterranean Cafe - Chicken Sandwich
        $10.55 pre-tip -> 1 meal -> $10.55/meal
        vs $14.22 delivered - saves $3.67 plus tip
        Promo: none attached on pickup
        0.7 km away
        <street address from store-details>

   Line 3 (the "vs" line) appears ONLY where you priced the same store and item in both modes; otherwise omit it.
   Word it as "saves $X.XX plus tip" - the dollar figure is the pre-tip difference, and the tip saving stays qualitative because every number in this email is pre-tip.
   Get the street address from `dd-cli --json-output store-details --store-id <id>`, field `.structuredContent.store.printable_address`.
   The pickup preview's own `store.printable_address` is null, so `store-details` is the only source; if that call fails, say "address unavailable" rather than guessing one.
   Distance comes from the `distance_km` column, printed to one decimal.
   If a store won in BOTH modes, feature it ONCE here with the "vs" line rather than listing it in both sections.
   If nothing cleared the floor on pickup, say so in one line and move on - do not pad the list.
3. **Benchmark footer**, one short line PER known deal (three lines, not one long line), for the three known deals only:
   Popeyes (36042509), Domino's 2-topping medium BOGO (34162385), Mediterranean Joint shawarma (25060900).
   Report their current per-meal price so Arun can see whether they still hold.
   If one changed materially or disappeared, that change IS reportable and should be called out.
4. **Accuracy**, the percentage of this run's predictions that landed within $0.50 of the verified preview total, as a fraction (e.g. "7/8 = 87.5%").
   Target is >= 95%.
   Compute it only from previews you actually pulled this run.
   Count delivery and pickup previews as separate predictions, and say how many of each the fraction covers.
   Do NOT quote `score.py --selftest`; that number is fit in-sample against the calibration file and is always ~100%, so it is not evidence of anything.
   If you verified nothing, say the scan produced no accuracy number and is not a valid scan.
5. **Coverage**, the concrete numbers: how many stores had usable menus out of the census, how many promos were found across how many stores, how many stores got a promo refresh today, how many menus were refreshed, how many stores were inside the 4.0 km pickup radius and how many of those got a real pickup preview, and whether the rate limit stopped the sweep early.
6. **Scan errors**, anything that failed: stores with broken menus, failed previews, failed pickup probes (including stores where `asap_pickup_available` was false), undeletable carts, mail problems.
   An empty section should say "none".

Honesty rules that outrank everything above: never report a number you did not compute from real data.
If something is missing or contradictory, say so plainly in the Scan errors section.
Do not invent around a gap.

## Schedule: systemd, and why

Target: **11:30 AM America/Chicago, daily.**

The 11:30 slot is deliberate and temporary.
During the parallel validation period the Mac keeps the original 10:45 Chicago slot and remains the live scanner; dealbox runs 45 minutes later so the two never race for the same carts.
When the Mac is retired, move this timer back to 10:45.

**This box's system timezone is UTC**, not America/Chicago.
Unlike launchd and cron, a systemd timer can name its own timezone, so the timezone of the box does not matter and no dual-trigger trick is needed:

```
OnCalendar=*-*-* 11:30:00 America/Chicago
```

systemd resolves that against the tzdata database on every run, so it is correct in both CST and CDT with zero maintenance.

**Mechanism: a systemd system timer, in `/etc/systemd/system/`.**

- `dealscanner.service` - `Type=oneshot`, `User=ubuntu`, `ExecStart=/home/ubuntu/deal-scanner/daily_scan.sh`, `TimeoutStartSec=3h`.
- `dealscanner.timer` - the `OnCalendar` line above plus `Persistent=true`.

A *system* timer rather than a user timer, because a user manager only exists while the user has a session unless lingering is enabled; a system unit with `User=ubuntu` runs unattended from boot, which is what an appliance needs.

`Persistent=true` makes systemd run a missed job once the machine comes back after being off.
That catch-up is why `daily_scan.sh` still keeps its own two gates even though the timer is already correct:

- **Chicago-hour window 10:00 to 19:59.** A strict `hour == 11` test would reject a catch-up run and the day would produce no report at all. A late report is better than none; the 20:00 upper bound stops a very late catch-up from mailing a lunch report in the middle of the night.
- **A date stamped marker at `state/last_run_<YYYY-MM-DD>`**, which guarantees at most one real run per day no matter how many times the unit fires.

Manage it with:

```
systemctl list-timers 'dealscanner*'      # next and last fire times
systemctl status dealscanner.service      # last run result
journalctl -u dealscanner.service         # stdout and stderr of past runs
sudo systemctl start dealscanner.service  # run once, right now
sudo systemctl stop dealscanner.timer     # disarm
```

Note that `daily_scan.sh` also writes its own log to `logs/<date>.log`; journalctl only holds whatever the script let escape to stdout/stderr.

## dd-cli authentication on Linux

**Linux has no OS keychain, and the Linux dd-cli build has no keychain backend at all.**
Running any dd-cli command without credentials fails with:

```
Error: Keychain unavailable. dd-cli requires keychain access to securely store
credentials, or set the DD_CLI_ACCESS_TOKEN env var to run in a headless environment.
```

`dd-cli login` on Linux still starts a browser OAuth flow and waits on `http://localhost:4180/oauth2/callback`, but there is nowhere for it to persist the result, so it is a dead end here.

The only supported path on this box is the headless one, added in dd-cli 0.2.2:

- The token lives in `/home/ubuntu/.dd_token`, mode 600, as a single `export DD_CLI_ACCESS_TOKEN=...` line.
- `daily_scan.sh` sources that file itself rather than relying on the systemd unit's environment, so an interactive run behaves identically to a timer run.
- `~/.profile` exports it too, for interactive shells.

**The token is short lived: a 72 hour JWT with no refresh token.**
The keychain blob it came from contains only `access_token`, `token_type`, and `expires_at` - there is no refresh token, and the Mac's keychain entry was never rewritten after creation, so nothing renews it automatically on either machine.
Decode the expiry with:

```
cut -d. -f2 <<<"$DD_CLI_ACCESS_TOKEN" | tr '_-' '/+' | base64 -d 2>/dev/null | jq .exp
```

When it expires every fetch fails, the zero-fetch guard fires, and the run mails a "SCAN FAILED" alert instead of a silent bad report.
**Renewing is a manual step**: run `dd-cli export-token` on a machine with a browser, then rewrite `/home/ubuntu/.dd_token`.
This is the single biggest operational weakness of the appliance; treat a SCAN FAILED email as "the token probably expired" until proven otherwise.

## Email

All mail goes out through `send_mail.sh <subject> <body-file>`.
It has two paths and tries them in order.

**Primary: Gmail SMTP.**
`curl` uploads an RFC 5322 message to `smtps://smtp.gmail.com:465`, authenticating as `<your gmail address>` with a 16 character app password read from `~/.gmail_app_pw`.
The reports are From and To that same gmail.com address, so sending them through SES meant a From that aligned with neither SPF nor DKIM - the exact shape of a spoof - and Gmail silently dropped or spam-foldered them.
Authenticating as the account itself makes envelope, SPF, DKIM and From all agree.
The password is passed in via a `curl --config -` file on stdin, never on argv, and `curl -v` is deliberately not used because verbose mode would print the base64 AUTH line into the log.
Whitespace is stripped from the password file (Google shows it in four groups of four), and a password containing anything other than alphanumerics is rejected rather than quoted around.

**Fallback: SES**, used only when the app password is missing, malformed, or the SMTP upload fails.
It sends with the least privileged AWS profile `mailer`, which can only call `ses:SendEmail` from `<your gmail address>` in us-east-1.
It cannot list identities or do anything else; an AccessDenied on any other SES call is expected and correct.

```
/usr/local/bin/aws ses send-email --profile mailer --region us-east-1 \
  --from <your gmail address> --to <your gmail address> \
  --subject "[dealbox] DoorDash deals - <date> [SES-FALLBACK]" --text file://<body>
```

A fallback send appends **`[SES-FALLBACK]`** to the subject on purpose, so a degraded delivery is visible in the inbox and not only in a log file.
If you see that tag, the Gmail path failed and that belongs in the Scan errors section.
The credentials live in `~/.aws/credentials` (profile `mailer`), copied from the Mac.
`~/.aws/config` sets `region = us-east-1` for that profile, but `send_mail.sh` passes `--region` explicitly anyway.

`send_mail.sh` logs which path it used: `send_mail: path=gmail-smtp rc=0 ...` or `send_mail: path=ses rc=...`.

**Subjects sent from this box are prefixed `[dealbox] `** so that during the parallel validation period Arun can tell dealbox's report apart from the Mac's at a glance.
Both the normal report and the SCAN FAILED alert carry the prefix.
Drop the prefix once the Mac is retired.

## Layout

```
/home/ubuntu/deal-scanner/
  CLAUDE.md              this file
  deal-hunt-prompt.md    mission spec
  dd-cli-issues.md       known CLI defects, read before using dd-cli
  daily_scan.sh          the daily job
  send_mail.sh           gmail smtp send, SES fallback
  scanner/
    extract.py           raw scan dir -> items.tsv + promos.tsv
    score.py             ranking and the fee model
    stores.tsv           census, 116 stores
    calibration/previews.tsv   append only verified previews
    README.md            pipeline and fee model detail
  state/
    scan/                persistent raw menu_<id>.json / promo_<id>.json
    items.tsv promos.tsv stores.tsv ranked.tsv ranked_pickup.tsv verify_list.tsv
    promo_cursor         resume point for the paced promo sweep
    orphan_carts.txt     carts that could not be deleted, clean these first
  logs/<date>.log        full log of each run

/home/ubuntu/.dd_token             DD_CLI_ACCESS_TOKEN, mode 600 (see above)
/home/ubuntu/.gmail_app_pw         gmail app password for the primary mail path
/home/ubuntu/.aws/credentials      the send-only `mailer` profile (SES fallback)
/home/ubuntu/.local/bin/dd-cli     dd-cli 0.2.2, linux-amd64
/usr/bin/claude                    Claude Code, installed via npm -g
/usr/local/bin/aws                 AWS CLI v2
/etc/systemd/system/dealscanner.{service,timer}
```

## Migration note: this box replaces a Mac

This appliance previously ran on a macOS EC2 instance ("macbox").
Anything you find that still assumes macOS is a migration bug worth fixing, not a local convention:
`launchctl` / LaunchDaemons, `security unlock-keychain`, `~/.zshenv`, `/opt/homebrew/bin`, BSD `stat -f %m` (GNU is `stat -c %Y`), and bash 3.2 workarounds are all obsolete here.
During the parallel validation period the Mac is still the live scanner on the 10:45 slot, so **never assume you are the only machine touching this DoorDash account** - the cart safety rules above matter more than usual.

## Known data discrepancy, do not silently correct

`scanner/README.md` says the 2026-08-10 scan had 27 empty menu files and 84 usable menus out of 115 stores.
That is stale.
The underlying scan was resumed after the README was written.
The current seeded state has 117 menu files with only 1 empty, and extract reports **8012 items from 112 stores, 4 skipped**, plus **52 promos across 117 stores**.
The 27 non-empty `.err` sidecars in `~/scan-20260810` are leftovers from the first pass and no longer match the `.json` files beside them.
Report the numbers extract prints on the day, never the README's.
