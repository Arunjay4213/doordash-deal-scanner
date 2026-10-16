# DoorDash deal scanner

An unattended agent that finds the cheapest real meal near one address each day and emails a short report.
Prices are all-in (food + fees + tax, before tip).
Everything goes through the official DoorDash CLI (`dd-cli`), and it runs on a small Linux VM under a systemd timer.

## What it does

1. Census lists every store DoorDash offers near the anchor address, with its distance.
2. Bulk fetch pulls each store's menu and promotions through `dd-cli --json-output` and caches the raw envelopes.
3. Extract and score flattens the menus into items and sorts the items into meal-equivalents.
   It then models fees and promos per cart and ranks carts by cost per meal, once for delivery and once for e-bike pickup.
4. Verify builds real carts for the top candidates and runs `order preview` to get DoorDash's own quote, which it compares to the prediction.
   It deletes every cart it created.
5. Report emails the ranked deals with their predicted and verified totals.

The fee model is fitted to real checkout previews, and in-sample it reproduces DoorDash's quotes to within about a cent.

## Layout

```
daily_scan.sh          orchestrates one full run (stages 1-5)
deal-hunt-prompt.md    the mission spec: filters, meal-count rules, known-deal exclusions, report format
CLAUDE.md              operating manual for the agent that runs on the box (safety rules, token handling, mail)
COOKBOOK.md            verified dd-cli recipes with the exact JSON paths each command returns
dd-cli-issues.md       every dd-cli defect and quirk observed while building this, with repro commands
price_cart.sh          build, preview, and delete one pricing cart safely
send_mail.sh           send the report over Gmail SMTP, SES as fallback
scanner/               census, extract, score, and the fitted fee model (see scanner/README.md)
systemd/               the timer and service units
```

## Safety rules

The agent never submits an order and never touches a cart it did not create.
Every cart it does create is deleted before the run finishes.
The full list is in CLAUDE.md.

## Configuration

The anchor address, coordinates, and email address are kept out of this repo on purpose.
The scanner reads the account's default saved address with `dd-cli address list`, takes coordinates from `DD_LAT` and `DD_LNG`, and sends the report to `DEALSCAN_EMAIL`.
The dd-cli access token lives in `~/.dd_token` and the Gmail app password in `~/.gmail_app_pw`, both outside the repo.
