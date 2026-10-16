# Madison deal scanner

A repeatable pipeline that finds the cheapest real meal near the anchor address in Madison WI and checks its numbers against real DoorDash checkout previews.

Every number in this repo comes from an account with DashPass active, located in Madison WI, and every total is pre-tip.

## Why this exists

Menu prices alone do not tell you what a meal costs.
Fees, promos, and tax move the final number by several dollars, and by a different amount at each store, so a cheap sandwich can lose to a pricier one once a $2.99 service fee and a $1.49 delivery fee are added.
The pipeline therefore predicts the full pre-tip total, checks a sample of those predictions against real checkout previews, and reports how often it was right.

## Pipeline

The stages run in order.
Each one writes a file that the next one reads, so any stage can be rerun on its own.

### 1. Census

This stage lists the stores near the address, sorted by distance, into `census_list.txt`.
The file is tab separated with five columns: `store_id`, `name`, `meters`, `miles`, `cuisines`.

Real sample:

```
592083	QQ Express	435m	0.27mi	chinese
28042387	Senjoy Chinese Cuisine	453m	0.28mi	chinese
658055	McDonald's	486m	0.30mi	burgers
```

`stores.tsv` is derived from it, with `store_id`, `name`, `distance_km`, where `distance_km` is the meters column divided by 1000.

The census sets the coverage for the whole scan.
It is incomplete, and the search tool cannot make it complete (see "Search cannot be trusted for coverage" below).

### 2. Bulk fetch

For every store in the census, this stage fetches the menu and the promo list and saves the raw `dd-cli --json-output` envelopes to a dated scan directory on dealbox.

The scan data is on that machine, not on the local one:

```
ssh dealbox   # the Linux box that runs the scanner
~/scan-20260810/menu_<storeid>.json
~/scan-20260810/promo_<storeid>.json
```

The payload is under `.structuredContent`.
Menu items are in `.structuredContent.items[]` with `item_id`, `name`, `category_name`, `price` (dollars, float), `is_orderable`, `has_required_modifiers`.
Promos are in `.structuredContent.promotions[]` with `campaign_id`, `title`, `description`, `source`.

Each call also writes a `.err` sidecar next to its `.json`, and an empty sidecar means the call was clean.
The 2026-08-10 scan has 115 `menu_*.json`, 115 `promo_*.json`, and 230 `.err` files, of which exactly 27 are non-empty.
Those 27 are the calls that hit the rate limit, which makes the sidecars the quickest way to see how much of a sweep actually landed.

The rate limit means this stage will not finish in one pass (see the rate limit note below).

### 3. Extract

`extract.py` turns the raw scan directory into two flat tables.
It is a deterministic, stdlib-only script with zero AI involvement.

```
python3 extract.py <scan_dir> [-o OUT_DIR]
```

`-o` defaults to `scan_dir`.
It writes:

- `items.tsv`: `store_id`, `item_id`, `name`, `category`, `price`, `orderable`, `req_mods`
- `promos.tsv`: `store_id`, `campaign_id`, `title`, `description`, `source`

Items whose `price` is not a number are dropped.
Some item names in the real data end in newlines, so every field has its whitespace collapsed before it is written; without that, one record would split across two TSV lines.
A store with zero promotions counts as a success.

Unusable menu files are counted, and the reason for each store is printed to stderr, so lost coverage shows up in the output.
This is a real run against the 2026-08-10 scan of 115 stores:

```
items.tsv : 5721 rows from 84 stores (31 skipped)
promos.tsv: 49 rows from 115 stores (0 skipped)
```

The 31 skipped menus break down like this:

```
  27 empty file (tool call produced no output)
   3 Something went wrong retrieving the menu . Please try again.
   1 No menu items found ID 45410997.
```

The 27 empty files come from the rate limit described below and do not mean 27 stores are broken.
That scan had usable menus for 84 of 115 stores, and 49 promos across 29 of the 115 stores.
Any report built on it must state the 84 of 115 figure.

### 4. Score

This stage ranks candidate meals by predicted pre-tip total using the fee and tax model below.
It is another plain script and costs $0.

### 5. Verify top-N

Only the top ranked candidates get a real cart and a real `order preview`.
This is the only stage that spends CLI calls, which is why it is limited to the top of the ranking.

Each prediction is compared against the verified total, and the error is recorded.

### 6. Append to calibration

Every verified preview is appended to `calibration/previews.tsv`.
The file grows with each scan and is never truncated.
Later scans are more accurate than earlier ones because the fee model is fit to these accumulated real observations.

## Accuracy protocol

Every scan must report the percentage of its predictions that landed within $0.50 of the verified checkout total.
The target is >= 95%.

The rules:

- The accuracy number comes from real previews pulled during that scan, never from the historical calibration file.
- A scan that verifies nothing has no accuracy number and does not count as a valid scan.
- Verified previews are appended to `calibration/previews.tsv` whether the prediction was right or wrong.
  Wrong predictions are the most valuable rows in the file.
- If accuracy drops below target, fix the fee model before shipping a recommendation.

## What the calibration data currently shows

`calibration/previews.tsv` holds 11 verified rows across 9 stores, covering both delivery and pickup.
Its columns are `store_id`, `fulfillment`, `subtotal`, `discount`, `service_fee`, `delivery_fee`, `tax`, `pretip_total`.

Two things are clear from the data.

### Tax is fully predictable

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

Tax applies to the fees as well as the food, which is why pickup tax is lower than delivery tax at the same store even though the food is identical.

### Service fee is the unpredictable part

The service fee is also where most of the price difference between stores comes from.
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

The two stores with a $2.99 service fee and a nonzero delivery fee (28054150, 658055) are also the two with no discount.
Those are the cases where the DashPass benefit does not apply, and they are why ranking by menu price alone gives the wrong answer.
The service fee cannot be derived from the menu, so pinning it down is the main job of the calibration file, and it is the reason verification cannot be skipped.

Pickup rows carry the same `subtotal` and `discount` as their delivery counterpart.
The verified pickup ground truth only gives the service fee, tax, and total, so the subtotal and discount were copied from the same store's delivery preview.
This was checked: for both pickup stores the identity `subtotal + discount + service + delivery + tax = pretip_total` holds exactly, and the pre-tax base matches the delivery row to the cent.

Every row satisfies that identity, with zero mismatches.

## Cost design

Almost none of the pipeline costs anything to run.

Census, bulk fetch, extract, and score cost $0.
They are plain Python scripts and shell loops, so they can be rerun as often as needed.
Bulk fetch does use CLI calls but no model tokens, and its output is cached on disk so each store is fetched once per scan.

Verification is the only stage that costs CLI calls.
Building carts and pulling previews is real API traffic against a rate limited service, so only the top-N candidates are verified.

Writing the report is the only LLM step.
A model reads the finished tables and writes the recommendation; it does not compute prices, rank stores, or come up with numbers of its own.
Every figure in a report must trace back to a row in `items.tsv`, `promos.tsv`, or `calibration/previews.tsv`.

Model reasoning is the expensive part, so it is used once at the end to write prose, and all the arithmetic in between is done exactly by scripts.

## Operational notes

Full detail is in `/home/arunj/projects/macbox/dd-cli-issues.md`.
Read that file before running a scan.
The notes below cover the issues that shape how the pipeline is built.

### Never submit orders

The pipeline only previews and never checks out.
`order preview` is the last call this pipeline is ever allowed to make on a cart it built.

### Never touch pre-existing carts

The account has real carts in it that the user created.
The pipeline only operates on carts it created in the current run, which it tracks by `cart_uuid`.
It must never delete, modify, or preview a cart it did not create.

### Search cannot be trusted for coverage

This is the biggest limit on the census stage.

- A query for an exact store name can miss a store that exists a mile away.
  `search -q 'The Mediterranean Joint'` never returned store 25060900, which is real and confirmed via `store-details` (issue 10).
- A query for a cuisine can return zero results when that cuisine is obviously present.
  `search -q 'mexican'` returned an empty list with `success: true`, while `search -q 'tacos'` at the same coordinates returned 10 Mexican restaurants (issue 11).
- A query for a chain with no local presence returns unrelated stores with no sign that nothing matched, so a caller cannot tell "not present" from "here are your results" (issue B).

So a store missing from search results may still exist.
The census is a lower bound on what exists, and the scan must say so.

The current calibration data shows this directly.
Three stores with verified previews (25060900, 36042509, 294406) are not in the 115 store census at all, which is why `stores.tsv` has no distance for them.

### Menu fetch hits an undocumented rate limit around 88 to 90 calls

A sequential sweep of 115 stores with 1s sleeps succeeded for calls 1 to 88 and then failed for every remaining call with `Rate limit exceeded for tool. Retry after 1323 seconds` (issue 14).

The saved scan shows this directly: 27 of the 230 `.err` sidecars are non-empty, and those 27 stores produce the 27 "empty file" skips in the extract run above.

Because the census is sorted by distance, all the failures landed on the farthest stores, which made them look like problems with those stores when the whole session had in fact been cut off.
Bulk fetch must checkpoint per store, treat rate limit errors as retryable instead of as store failures, and resume after roughly 22 minutes.

### Cart mutations have a separate, broader rate limit

`order preview` and `cart delete` can both fail with `Rate limit exceeded for user`, a different limit from the menu one (issue 15).
A cart the pipeline created was left undeletable for over 11 minutes.
So cleanup has to be a retry loop, and the pipeline keeps a persistent list of the carts it created so a later run can finish the cleanup.

### Promo results must always be re-previewed

`promo apply` returns the same `success: false` message whether the promo will actually discount the cart or not (issue 6).
For one store the discount then showed up on the next preview at -$7.39, and for another it never showed up.
Ignore the apply response and read the discount from `order preview`.

`promo apply` has also failed while silently duplicating the cart line item, which left 4 sandwiches where there had been 2 (issue 1).
Always re-read the cart after an apply and before previewing.

### Other quirks that affect parsing

- `order preview` returns `fulfillment_type: null` for delivery carts and only populates it for pickup (issue 12).
  Do not use that field to detect the fulfillment mode.
- A failed `cart add-items` still leaves a persistent empty cart behind (issue 7), and those need cleaning up too.
- Promo objects carry no expiry field, so deals cannot be ranked by urgency (issue 5).
- `promo list` requires `--store-id`, so wallet promos can only be found by probing stores one at a time (issue 9).
- `menu` sometimes returns an empty `store_name` (issue C), so names come from the census instead.
- For some stores the menu fetch fails outright with a server error, which kept one pizza store out of a comparison entirely.

## Data discrepancies to resolve

These are written down here instead of being quietly corrected.

1. The verified preview labelled "Sushi Express" carries `store_id=28054150`, but the census lists Sushi Express as `289117`, and `28054150` does not appear in the census at all.
   `calibration/previews.tsv` keeps `28054150` as given by the ground truth.
   The store id needs confirming with `store-details` before this row is used to fit the model for Sushi Express specifically.
   Its fee shape is valid data whichever store it belongs to.
2. Two verified previews were supplied without a store id.
   They were matched to the census by name: Naf Naf Grill is `23460971` and Ashirwad Indian Restaurant is `35842845`.
   `23460971` is independently corroborated in `dd-cli-issues.md`, which records a successful promo apply against it.
3. Stores `25060900`, `36042509`, and `294406` have verified previews but are absent from the census, per the search coverage gap above.

## Files

- `extract.py`: raw scan directory to `items.tsv` and `promos.tsv`
- `calibration/previews.tsv`: accumulated verified checkout previews, append only
- `/home/arunj/projects/macbox/dd-cli-issues.md`: full CLI bug and quirk log
