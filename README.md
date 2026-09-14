# DoorDash deal scanner

An unattended agent that finds the cheapest real meal near one address every day, priced all-in (food + fees + tax, before tip), and emails a short report.
It is built entirely on the official DoorDash CLI (`dd-cli`) and runs on a small Linux VM under a systemd timer.

## What it does

1. Census: enumerate every store DoorDash offers near the anchor address, with distance.
2. Bulk fetch: pull each store's menu and promotions through `dd-cli --json-output` and cache the raw envelopes.
3. Extract and score: flatten menus into items, classify items into meal-equivalents, model fees and promos per cart, and rank carts by cost per meal for delivery and for e-bike pickup.
4. Verify: build real carts for the top candidates, run `order preview` to get DoorDash's own quote, compare it to the prediction, and delete every cart it created.
5. Report: email the ranked deals with predicted and verified totals.

The fee model is fitted against real checkout previews and reproduces DoorDash quotes to within about a cent in-sample.

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

The agent never submits an order, never touches a cart it did not create, and deletes every cart it creates before finishing.
See CLAUDE.md for the full list.

## Configuration

The anchor address, coordinates, and email address are deliberately not in this repo.
The scanner reads the account's default saved address through `dd-cli address list`, coordinates come from `DD_LAT` and `DD_LNG`, and the report recipient from `DEALSCAN_EMAIL`.
The dd-cli access token lives in `~/.dd_token` and the Gmail app password in `~/.gmail_app_pw`, both outside the repo.
