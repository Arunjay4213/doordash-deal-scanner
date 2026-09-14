# Madison deal scanner

A repeatable pipeline that finds the cheapest real meal near the anchor address in Madison WI, and proves its numbers against real DoorDash checkout previews.

Account context for every number in this repo: DashPass is active, location is Madison WI, and all totals are pre-tip.

## Why this exists

Menu prices alone do not tell you what a meal costs.
Fees, promos, and tax move the final number by several dollars, and they move it differently at each store.
A store with a cheap sandwich can lose to a pricier one once a $2.99 service fee and a $1.49 delivery fee land on it.
So the pipeline predicts the full pre-tip total, then checks a sample of those predictions against real checkout previews and reports how often it was right.

## Pipeline

The stages run in order. Each one writes a file the next one reads, so any stage can be rerun on its own.

### 1. Census

Enumerate the stores that exist near the address, sorted by distance.
The census lives at `census_list.txt` and is tab separated with five columns: `store_id`, `name`, `meters`, `miles`, `cuisines`.

Real sample:

```
592083	QQ Express	435m	0.27mi	chinese
28042387	Senjoy Chinese Cuisine	453m	0.28mi	chinese
658055	McDonald's	486m	0.30mi	burgers
```

`stores.tsv` derives from this: `store_id`, `name`, `distance_km`, where `distance_km` is the meters column divided by 1000.

The census is the coverage list for the whole scan.
It is not complete, and it cannot be made complete with the search tool.
See "Search cannot be trusted for coverage" below.

### 2. Bulk fetch

For every store in the census, fetch the menu and the promo list and save the raw `dd-cli --json-output` envelopes to a dated scan directory on the Mac.

Scan data lives on the Mac, not locally:

```
ssh dealbox   # the Linux box that runs the scanner
~/scan-20260810/menu_<storeid>.json
~/scan-20260810/promo_<storeid>.json
```

Payload sits under `.structuredContent`.
Menu items are under `.structuredContent.items[]` with `item_id`, `name`, `category_name`, `price` (dollars, float), `is_orderable`, `has_required_modifiers`.
Promos are under `.structuredContent.promotions[]` with `campaign_id`, `title`, `description`, `source`.

Each call also writes a `.err` sidecar next to its `.json`.
Empty sidecars mean the call was clean.
In the 2026-08-10 scan there were 115 `menu_*.json`, 115 `promo_*.json`, 230 `.err` files, and exactly 27 of those sidecars were non-empty.
Those 27 are the rate limited tail, so the sidecars are the fastest way to see how much of a sweep actually landed.

This stage is rate limited and will not finish in one pass. See the rate limit note below.

### 3. Extract

`extract.py` turns the raw scan directory into two flat tables.
It is deterministic, stdlib only, and has zero AI involvement.

```
python3 extract.py <scan_dir> [-o OUT_DIR]
```

`-o` defaults to `scan_dir`. Outputs:

- `items.tsv`: `store_id`, `item_id`, `name`, `category`, `price`, `orderable`, `req_mods`
- `promos.tsv`: `store_id`, `campaign_id`, `title`, `description`, `source`

Items whose `price` is not a number are dropped.
Item names in the real data contain trailing newlines, so all fields are whitespace collapsed before being written, otherwise a single record would split across two TSV lines.
A store with zero promotions is a valid result and is counted as a success, not a failure.

Unusable menu files are counted and the reason is printed to stderr per store, so coverage loss is visible instead of silent.
Real run against the 2026-08-10 scan of 115 stores:

```
items.tsv : 5721 rows from 84 stores (31 skipped)
promos.tsv: 49 rows from 115 stores (0 skipped)
```

The 31 skipped menus break down as:

```
  27 empty file (tool call produced no output)
   3 Something went wrong retrieving the menu . Please try again.
   1 No menu items found ID 45410997.
```

The 27 empty files are the rate limit wall described below, not 27 broken stores.
Usable menu coverage for that scan was 84 of 115 stores, and 49 promos were found across 29 of the 115 stores.
Any report built on that scan must state the 84 of 115 figure.

### 4. Score

Rank candidate meals by predicted pre-tip total using the fee and tax model below.
Also a plain script, also $0.

### 5. Verify top-N

Take the top ranked candidates only, build a real cart for each, and pull a real `order preview`.
This is the only stage that spends CLI calls, so it is deliberately limited to the top of the ranking rather than the whole census.

Compare each prediction against the verified total and record the error.

### 6. Append to calibration

Every verified preview is appended to `calibration/previews.tsv`.
The calibration file grows with each scan and never gets truncated.
That is what makes later scans more accurate than earlier ones: the fee model is fit to accumulated real observations, not to guesses.

## Accuracy protocol

Every scan must report the percentage of its predictions that landed within $0.50 of the verified checkout total.

Target is >= 95%.

Rules:

- The accuracy number is computed from real previews pulled during that scan, not from the historical calibration file.
- A scan that verifies nothing reports no accuracy number and is not a valid scan.
- Verified previews are appended to `calibration/previews.tsv` whether the prediction was right or wrong.
  Wrong predictions are the most valuable rows in the file.
- If accuracy drops below target, fix the fee model before shipping a recommendation.

## What the calibration data currently shows

`calibration/previews.tsv` holds 11 verified rows across 9 stores, covering both delivery and pickup.
Columns: `store_id`, `fulfillment`, `subtotal`, `discount`, `service_fee`, `delivery_fee`, `tax`, `pretip_total`.

Two facts fall straight out of the data.

**Tax is fully predictable.**
Tax is 5.5% of the post-discount subtotal plus the service and delivery fees.
Checked against all 11 rows, the maximum error is $0.00:

```
  1150945 delivery base= 11.86 pred_tax= 0.65 actual= 0.65 err=0.00
  1150945 pickup   base= 10.87 pred_tax= 0.60 actual= 0.60 err=0.00
 25060900 delivery base= 10.73 pred_tax= 0.59 actual= 0.59 err=0.00
 25060900 pickup   base=  9.74 pred_tax= 0.54 actual= 0.54 err=0.00
 36042509 delivery base=  8.38 pred_tax= 0.46 actual= 0.46 err=0.00
 34162385 delivery base= 16.60 pred_tax= 0.91 actual= 0.91 err=0.00
 23460971 delivery base= 13.99 pred_tax= 0.77 actual= 0.77 err=0.00
 28054150 delivery base= 15.23 pred_tax= 0.84 actual= 0.84 err=0.00
   658055 delivery base= 15.37 pred_tax= 0.85 actual= 0.85 err=0.00
 35842845 delivery base= 20.95 pred_tax= 1.15 actual= 1.15 err=0.00
   294406 delivery base= 22.66 pred_tax= 1.25 actual= 1.25 err=0.00

max abs tax error = $0.00 over 11 rows
```

Note that tax applies to the fees, not just the food.
This is why pickup tax is lower than delivery tax at the same store even though the food is identical.

**Service fee is the unpredictable part, and it is where the money is.**
As a percentage of the post-discount subtotal, the observed service fee ranges from 5.0% to 27.5%:

```
    store ful         net   svc%   tax%  disc%
  1150945 delivery  10.87   9.11   5.98   25.0
 25060900 delivery   9.74  10.16   6.06   25.0
 36042509 delivery   7.39  13.40   6.22   50.0
 34162385 delivery  15.29   8.57   5.95   41.6
 23460971 delivery  13.00   7.62   5.92   20.0
 28054150 delivery  11.75  25.45   7.15   -0.0
   658055 delivery  10.89  27.46   7.81   -0.0
 35842845 delivery  19.95   5.01   5.76   -0.0
   294406 delivery  21.58   5.00   5.79   -0.0
```

The two stores with a $2.99 service fee and a nonzero delivery fee (28054150, 658055) are the two that also show no discount.
Those are the DashPass-benefit-absent cases and they are the reason menu price ranking alone is wrong.
Because the service fee cannot be derived from the menu, it is the term the calibration file exists to pin down, and it is why verification cannot be skipped.

Pickup rows carry the same `subtotal` and `discount` as their delivery counterpart.
The verified pickup ground truth gives only service fee, tax, and total, so subtotal and discount were carried across from the same store's delivery preview.
That is confirmed rather than assumed: for both pickup stores the identity `subtotal + discount + service + delivery + tax = pretip_total` holds exactly, and the pre-tax base matches the delivery row to the cent.

Every row satisfies that identity with zero mismatches.

## Cost design

The pipeline is built so that almost none of it costs anything.

- **Census, bulk fetch, extract, score: $0.**
  These are plain Python scripts and shell loops.
  They can be rerun as many times as needed without burning budget.
  Bulk fetch costs CLI calls but no model tokens, and its output is cached on disk so it is fetched once per scan.
- **Verification: the only stage that costs CLI calls.**
  Cart building and previews are real API traffic against a rate limited service.
  This is why only the top-N candidates get verified, not the whole census.
- **Report composition: the only LLM step.**
  A model reads the finished tables and writes the recommendation.
  It does not compute prices, rank stores, or invent numbers.
  Every figure in a report must trace back to a row in `items.tsv`, `promos.tsv`, or `calibration/previews.tsv`.

The design goal is that the expensive resource, model reasoning, is spent once at the end on prose, not repeatedly in the middle on arithmetic a script does exactly.

## Operational notes

Full detail is in `/home/arunj/projects/macbox/dd-cli-issues.md`.
Read that file before running a scan.
The items below are the ones that change how the pipeline is built.

### Never submit orders

The pipeline previews. It does not check out.
`order preview` is the last call this pipeline is ever allowed to make on a cart it built.

### Never touch pre-existing carts

The account has real carts in it that the user created.
Only operate on carts this pipeline created in this run, tracked by `cart_uuid`.
Never delete, modify, or preview a cart the pipeline did not create.

### Search cannot be trusted for coverage

This is the single most important constraint on the census stage.

- An exact store name query can miss a store that exists a mile away.
  `search -q 'The Mediterranean Joint'` never returned store 25060900, which is real and confirmed via `store-details` (issue 10).
- A generic cuisine query can return zero results for a cuisine that is obviously present.
  `search -q 'mexican'` returned an empty list with `success: true`, while `search -q 'tacos'` at the same coordinates returned 10 Mexican restaurants (issue 11).
- Queries for chains with no local presence return unrelated stores rather than a no-match signal, so a caller cannot tell "not present" from "here are your results" (issue B).

Consequence: absence from search results is not evidence of absence.
The census is a floor on what exists, never a complete list, and the scan must say so.

This shows up directly in the current calibration data.
Three stores with verified previews (25060900, 36042509, 294406) are not in the 115 store census at all, which is why `stores.tsv` has no distance for them.

### Menu fetch hits an undocumented rate limit around 88 to 90 calls

A sequential sweep of 115 stores with 1s sleeps succeeded for calls 1 to 88 and then failed for every remaining call with `Rate limit exceeded for tool. Retry after 1323 seconds` (issue 14).

This is directly visible in the saved scan: 27 of the 230 `.err` sidecars are non-empty, and those 27 stores produce the 27 "empty file" skips in the extract run above.

Because the census is distance sorted, the failures all landed in the far-distance tail and looked like a per-store defect rather than a session-wide wall.
Do not misread that pattern.
Bulk fetch must checkpoint per store, treat rate limit errors as retryable rather than as store failures, and resume after roughly 22 minutes.

### Cart mutations have a separate, broader rate limit

`order preview` and `cart delete` can both fail with `Rate limit exceeded for user`, a different limit from the menu one (issue 15).
A cart the pipeline created was left undeletable for over 11 minutes.
Cleanup must therefore be a retry loop, and the pipeline must keep a persistent list of carts it created so cleanup can resume in a later run.

### Promo results must always be re-previewed

`promo apply` returns the same `success: false` message whether the promo will actually discount the cart or not (issue 6).
For one store the discount then appeared on the next preview at -$7.39, for another it never appeared.
Never trust the apply response. Read the discount off `order preview`.

`promo apply` has also been observed to fail and silently duplicate the cart line item, leaving 4 sandwiches where there were 2 (issue 1).
Always re-read the cart after an apply, before previewing.

### Other quirks that affect parsing

- `order preview` returns `fulfillment_type: null` for delivery carts and only populates it for pickup (issue 12).
  Do not detect fulfillment mode from that field.
- A failed `cart add-items` still leaves a persistent empty cart behind (issue 7).
  Those need cleaning up too.
- Promo objects carry no expiry field, so deals cannot be ranked by urgency (issue 5).
- `promo list` requires `--store-id`, so wallet promos can only be found by probing stores one at a time (issue 9).
- `menu` sometimes returns an empty `store_name` (issue C). Take names from the census instead.
- Menu fetch fails outright for some stores with a server error, which blocked one pizza store out of a comparison entirely.

## Data discrepancies to resolve

Recorded rather than silently corrected.

1. The verified preview labelled "Sushi Express" carries `store_id=28054150`, but the census lists Sushi Express as `289117`, and `28054150` does not appear in the census at all.
   `calibration/previews.tsv` keeps `28054150` as given by the ground truth.
   The store id needs confirming with `store-details` before this row is used to fit the model for Sushi Express specifically.
   Its fee shape is still valid data regardless of which store it belongs to.
2. Two verified previews were supplied without a store id.
   These were resolved from the census by name: Naf Naf Grill is `23460971` and Ashirwad Indian Restaurant is `35842845`.
   `23460971` is independently corroborated in `dd-cli-issues.md`, which records a successful promo apply against it.
3. Stores `25060900`, `36042509`, and `294406` have verified previews but are absent from the census, per the search coverage gap above.

## Files

- `extract.py`: raw scan directory to `items.tsv` and `promos.tsv`
- `calibration/previews.tsv`: accumulated verified checkout previews, append only
- `/home/arunj/projects/macbox/dd-cli-issues.md`: full CLI bug and quirk log
