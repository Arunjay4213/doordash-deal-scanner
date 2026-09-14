# dd-cli bugs and issues observed (v0.2.1, macOS arm64 on EC2 mac2.metal)

Status legend: NEW = not on the GitHub tracker as of 2026-08-10 (checked all 64 issues). COVERED = already reported upstream.

## NEW - candidates to file

### 1. `promo apply` fails but silently duplicates the cart line item
Date: 2026-08-10.
Command: `promo apply` for a BOGO wallet campaign on a Popeyes cart (store 36042509) holding 2x Spicy Chicken Sandwich.
Expected: promo attaches, or a clean failure with the cart unchanged.
Observed: response said the promo "did not attach", AND the cart line was silently duplicated (cart then held 4 sandwiches).
A pricing agent that does not re-read the cart would preview/submit double the food.
Related but distinct from #32 (add-items silently DROPS items; this is apply DUPLICATING them).

### 2. `-h`/`--help` on service commands requires keychain access
Date: 2026-08-10.
Command: `dd-cli order history -h` with the login keychain locked.
Expected: help text (help should never need credentials).
Observed: `Error: Keychain unavailable. dd-cli requires keychain access to securely store credentials`.
Help works once the keychain is unlocked, so the credential gate wraps even `-h` parsing on service commands.

### 3. `login` completes the OAuth browser round-trip, then discards the token if the keychain is unusable
Date: 2026-08-10.
Command: `dd-cli login` on a headless Mac where the user-level login keychain did not exist yet (fresh EC2 macOS never logged into the GUI).
Expected: fail fast BEFORE the browser flow ("no usable keychain"), or offer a fallback store.
Observed: full OAuth flow succeeds (user sees the success page, token response received), then `Error: Keychain unavailable` and the token is thrown away; the user must redo the entire browser flow after fixing the keychain.
Also: the error text talks about "storing" credentials even when the failure is on read operations - misleading.
Adjacent to #15/#22/#36 (headless auth stories) but none report the token-discard-after-success behavior.

### 4. Headless `login` spews raw AppleScript errors while trying to open a browser
Date: 2026-08-10.
Command: `dd-cli login` over SSH (no GUI session).
Expected: detect no-GUI and print only the "visit this URL" line.
Observed, verbatim, mixed into output:
  `0:349: execution error: An error of type -10810 has occurred. (-10810)`
  `69:77: execution error: Can't get application "chrome". (-1728)`
  `70:78: execution error: Can't get application "firefox". (-1728)`
Cosmetic, but noisy for the agents this tool targets.

### 5. Promo/campaign objects expose no expiry dates in JSON (enhancement)
Date: 2026-08-10.
Commands: promo/campaign listing surfaces during a deal scan.
Expected: some `expires_at`-like field so an agent can rank deals by urgency.
Observed: no expiry anywhere in the JSON envelope.

### 6. `promo apply` failure message is unreliable in both directions
Date: 2026-08-10.
Commands: `promo apply` on a Popeyes cart (store 36042509, BOGO campaign 41e036c1-...) and on a Subway cart (store 294406, BOGO campaign 02c07652-...).
Expected: `success` reflects whether the promo will discount the cart.
Observed: both calls returned the identical `success: false` / "Promotion did not attach in preview - store eligibility check likely failed".
For Popeyes the discount then DID appear on the next `order preview` (-$7.39); for Subway it never appeared.
Same message for opposite outcomes, so an agent must always re-preview to learn the truth.

### 7. `cart add-items` leaks a persistent empty cart when every item fails validation
Date: 2026-08-08 and 2026-08-10.
Command: `cart add-items` with items missing required options (stores 1150945, 23460971, 289117, 2827319).
Expected: a failed add leaves no cart behind.
Observed: response is `success: false` ("1 item failed to add") but a new `cart_uuid` is created and the empty cart persists in `cart list` until explicitly deleted.
Distinct from tracker #32 (silent drops): this is about the empty-cart side effect of a reported failure.

### 8. `promo list` help claims browser checkout is the only way to apply a promo
Date: 2026-08-10.
Command: `dd-cli promo list -h`.
Expected: help that matches the CLI's own capabilities.
Observed: the "For agents - applying an eligible promo" section says to use `order checkout-url` plus manual browser entry, but `dd-cli promo apply` exists and successfully applied campaign promos (stores 1150945 and 23460971 on 2026-08-08).
Docs-vs-behavior mismatch inside the CLI's own help.

### 9. No account-wide wallet promo listing (enhancement)
Date: 2026-08-10.
Command: `dd-cli promo list -h`.
Expected (for deal research): a way to enumerate the consumer's wallet promotions across all stores.
Observed: `--store-id` is required, so wallet promos can only be discovered by probing store ids one at a time (O(stores) calls).

### 10. `search` on an exact/near-exact store name misses a real, nearby store
Date: 2026-08-10.
Command: `search -q 'The Mediterranean Joint' --lat <ANCHOR_LAT> --lng <ANCHOR_LNG> --limit 10`.
Expected: an exact-name query for a store that exists and is ~1 mile away (store 25060900, confirmed via a pre-existing cart and `store-details`) should surface that store, ideally as the top hit.
Observed: the top 10 results were "Mediterranean Cafe" (a different, unrelated store, 1150945) plus nine other stores with no obvious name relation ("Mad Seafood Boiler", "Cheba Hut", "Paul's Pel'meni", etc.) - "The Mediterranean Joint" itself never appeared.
Distinct from the existing "chain absent -> unrelated results" note (item B below): here the target store DOES exist nearby and the query text nearly matches its name verbatim, yet it's still missing from the ranked list.

### 11. Generic cuisine search returns zero results while a synonym returns 10
Date: 2026-08-10.
Command: `search -q 'mexican' --lat <ANCHOR_LAT> --lng <ANCHOR_LNG> --limit 10`.
Expected: some results, since Mexican restaurants are demonstrably nearby (confirmed seconds later via `search -q 'tacos'` at the same coordinates, which returned 10 hits: QDOBA Mexican Eats, Chipotle Mexican Grill, Luchador Tequila & Taco Bar, A La Brasa Mexican Grill, Los Gemelos Restaurant, El Rancho Mexican Grill, La Hacienda, etc.; two more Mexican spots - Laredo's Mexican Restaurant and La Penca Madison LLC - were already visible in `cart list` from pre-existing carts).
Observed: `search -q 'mexican'` returned `"stores":[],"success":true,"message":"No restaurants found for \"mexican\" near this location. Try a different search term or cuisine."` - a clean success envelope with zero results for a cuisine that is obviously well represented in the radius. An agent trusting this response would wrongly conclude Mexican food isn't available nearby.

### 12. `order preview` never returns `fulfillment_type: "DELIVERY"` - only `"PICKUP"` or `null`
Date: 2026-08-10.
Command: `order preview --cart-uuid <id>` and `order preview --cart-uuid <id> --fulfillment delivery` on a delivery-mode cart (store 25060900).
Expected per the command's own `-h` text: `quote.store_order_cart.fulfillment_type` is "a string ('DELIVERY' or 'PICKUP')".
Observed: for a cart in delivery mode (`is_consumer_pickup: false`), `fulfillment_type` came back JSON `null` both with no `--fulfillment` flag and with `--fulfillment delivery` passed explicitly. Only after `--fulfillment pickup` did the field populate, as `"PICKUP"`. So the field is reliable for pickup but silently null for delivery - a caller following the docstring to detect fulfillment mode would misread every delivery cart.

### 13. `store-details` returns `distance_meters: null` for a store found seconds earlier via `search`
Date: 2026-08-10.
Command: `store-details --store-id 25060900` (The Mediterranean Joint), immediately after confirming the store via a pre-existing cart at the same coordinates.
Expected: some populated distance, or at least consistency with `search`'s "best-effort" framing for similar geo fields.
Observed: `"distance_meters": null` in the response, while `printable_address` and `delivery_time` were both populated. Minor - `search` already carries distance for stores it returns - but worth noting since `store-details` is the documented path to disambiguate a store's location.

## Observed, plausibly by design - watch, do not file yet

### A. BOGO wallet campaign auto-attaches on delivery preview but not pickup preview
Popeyes BOGO attached automatically on delivery `order preview`, but pickup preview priced full menu (higher in-store base price, no BOGO).
May be genuine deal terms (delivery-only BOGO), but nothing in the JSON says so - an agent cannot tell "not eligible" from "silently not applied".
Related tracker themes: #10 (DashPass not applied in preview), #42 (pickup preview oddities).

### B. `search` returns unrelated stores instead of a no-match signal
Queries for chains with no nearby presence ("KFC", "Wendy's", "Taco Bell" at <ANCHOR_LAT>,<ANCHOR_LNG> on 2026-08-10) return unrelated stores (KFC query returned McDonald's, Butterbird, Wingstop) with no flag.
A caller cannot distinguish "chain not present" from real results without name-matching heuristics.
Possibly intended relevance-ranking behavior, but hostile to agents.
Reproduced again same day: "pizza hut" at <ANCHOR_LAT>,<ANCHOR_LNG> (near the anchor address, Madison) returned the same 5 generic pizza places as a plain "pizza" query (Toppers, Papa Johns, Domino's, Pizza Extreme, Eat the Best), and "little caesars" returned unrelated stores (Asian Noodle, A8 China, Raising Cane's, Cheba Hut) - no Little Caesars or Pizza Hut on the platform there, but nothing in the response says so.

### C. `menu` returns empty `store_name`
Stores 35842845 and 45410997 on 2026-08-08: `store_name: ""` in the menu envelope even though `search` shows their names fine.
Cosmetic JSON gap.

### 14. `menu` tool hits an undocumented rate limit after ~88-90 sequential calls in one scan session, silently blocking the rest of a coverage sweep
Date: 2026-08-10.
Command: a scripted loop of `dd-cli --json-output menu --store-id <id> --intent ...` across 115 census stores, 1s sleep between calls, run on the Mac.
Expected: either the limit is documented somewhere discoverable (`-h` text, error message with guidance) before an agent burns a scan budget on it, or menu calls simply succeed at this modest rate (~1 call/2-3s).
Observed: calls 1-88 succeeded; calls 89-115 (the entire remainder, corresponding to every store beyond ~3km in the distance-sorted census list) all failed identically with `Error: Rate limit exceeded for tool. Retry after 1323 seconds.` (retry window ~22 min). Nothing in `dd-cli menu -h` mentions a rate limit. Because the census list happens to be distance-sorted, the failure pattern looked at first like "menu fetch fails for all far-away stores" rather than "the tool got globally throttled partway through" - an agent could easily misdiagnose this as a per-store defect (matching the existing "COVERED" server-error family) rather than a session-wide budget it just exhausted.
Impact: silently truncated real coverage of a comprehensive sweep by ~23% (27 of 115 stores), all in the same tail segment, with no way to tell in advance how many calls remain before the wall hits.

### 15. `cart delete` (and other cart-mutating commands) can be blocked by a DIFFERENT, broader rate limit than `menu`'s - which creates a safety hazard for "always delete carts you create" workflows
Date: 2026-08-10.
Command: `dd-cli order preview --cart-uuid <id> --beautify` immediately followed by `dd-cli cart delete --cart-uuid <id>`, after roughly 30-40 cart add-items/preview/delete cycles during interactive pricing probes (a separate session from the earlier menu sweep).
Expected: cart cleanup (`cart delete`) should either never be rate-limited, or a caller instructed to "always delete carts you create for safety" needs some way to know a delete is guaranteed to go through eventually and roughly how long to wait.
Observed: `order preview` failed with `Error: Rate limit exceeded for user. Retry after 685 seconds.` - a different message shape ("for user" vs "for tool" from item 14) - and the immediately following `cart delete` on the SAME cart_uuid failed the same way (`Retry after 676 seconds`). This left a cart the agent created (Sunny Pho, store 1189168, 2x Vegan Sandwiches) undeletable for over 11 minutes. An agent bound by a strict "never leave a cart behind" safety rule has no compliant move available in that window except to wait it out and retry - the CLI gives no signal (before or during the block) that a cart-mutation command specifically, as opposed to a read like `menu`, can be walled off this way.

### 16. `cart add-items` returns a `cart_uuid` and creates a real, empty cart even when `success:false` and every item failed to add

Date: 2026-08-10.
Command: `dd-cli --json-output cart add-items --store-id 379439 --menu-id 1627417 --items-json '[{"item_id":"24166867000","item_name":"Spaghetti & Meatballs","quantity":2}]' --intent ...` (item has required modifiers that were not supplied).
Expected: either no cart is created when nothing could be added, or the response makes it unmistakable that the returned `cart_uuid` refers to an empty cart.
Observed: response carries `success:false`, `message:"Add to cart failed. 1 item failed to add."`, `cart.items_count:0`, `cart.items:[]` - but still a populated top-level `cart_uuid`. A caller that keys off `cart_uuid` being non-empty (a natural check) proceeds to `order preview` on an empty cart, and worse, silently leaks a real cart into the consumer's account that a "delete every cart you created" cleanup step will miss unless it tracks uuids from failed calls too. Reproduced identically at stores 289066, 600627, 379439, 379459.
Impact: direct safety hazard for cleanup-obligated agents; four stray carts were created in one run of four failed add calls.

### 17. `order preview` returns `success:true` with `quote.line_items:null` for an empty cart instead of an error

Date: 2026-08-10.
Command: `dd-cli --json-output order preview --cart-uuid f7dbbd4b-9e9b-47f8-8507-9b70314f463d --fulfillment delivery --intent ...` where the cart is empty (see item 16).
Expected: an error, or at minimum a non-null empty `line_items` array plus a message saying the cart is empty.
Observed: `success:true`, `message:"Order preview generated successfully"`, `error_category:""`, `error_message:""`, `warning:""`, `partial_response:false` - yet `quote.line_items` is `null`, `quote.map_item_subtotal` is `null`, and `quote.total_before_tip` is `$0.00`. The `quote` object also drops ~30 keys it carries on a real quote (no `line_items`, no `total_savings`, no `net_total_before_tip`). Any jq/iteration over `line_items` crashes with "Cannot iterate over null". A $0.00 pre-tip total reported as a successful quote is a dangerous value to hand downstream.

### 18. `promo list` reports store promotions as eligible (`is_applied:false`, no caveat) that `promo apply` then refuses with a generic "eligibility check likely failed"

Date: 2026-08-10.
Command: `dd-cli --json-output promo list --store-id 23235357 --intent ...` then `dd-cli --json-output promo apply --cart-uuid 11b9d1e4-7976-458e-a70b-2652c193f3d4 --promo-code b43e04f0-25f2-4487-891d-c4b44a7c8365 --intent ...` (cart: 2x Custom Oatmeal, $20.00 subtotal, at the same store).
Expected: `promo list` distinguishes promotions that are actually attachable to this cart from ones merely present in the consumer's wallet for this store, or `promo apply` explains WHICH eligibility check failed (item not eligible? subtotal minimum? first-order only?).
Observed: `promo list` returned "Buy 1, get 1 free" / "Buy One Get One on any eligible item" with `discount_cents:0`, `is_applied:false`, `source:"wallet"`, and `message:"Found 2 eligible promotions"` - the word "eligible" is doing unearned work. `promo apply` then returned `success:false`, `discount_cents:0`, `error_category:"promo_not_attached"`, `error_message:null`, `message:"Promotion did not attach in preview - store eligibility check likely failed."` The description says "any eligible item" but nothing in `promo list`, `menu`, or `restaurant-item-details` marks which items are eligible, so there is no way to construct a qualifying cart except trial and error. This is the single largest source of pricing error in our scanner: predicted $11.61-13.71, actual $22.16.

### 19. `menu` item `price` is the base price and can be far below the true minimum orderable price when required modifiers carry the cost

Date: 2026-08-10.
Command: `dd-cli --json-output menu --store-id 379439 --intent ...`, item `i_24166867000` "Spaghetti & Meatballs", compared against the `required_options` block returned by a failed `cart add-items` for the same item.
Expected: the menu payload exposes the required-modifier minimum (or a `price_range` / `min_price`) so a caller can compute what the item actually costs before building a cart.
Observed: `menu` reports `price: 7.15`. The item cannot be added at all without a "Pick Your Size" selection whose cheapest option is `Small` at `$11.95` - a 67% understatement. Same shape at QDOBA store 600627 (`Create Your Own Mini Bowl` listed $8.90, required "Choose Your Protein" cheapest option $8.90) and State Street Brats store 289066 (required "Basket Side*", cheapest $0.00 so no gap there). The required-options data clearly exists server-side, since `cart add-items` returns the full `required_options[]` array with per-option prices in its error payload - it is just absent from `menu`. Any price-ranking tool built on `menu` prices will systematically rank modifier-heavy chains far too cheap. Predicted $16.13-18.24, actual $26.48.

## COVERED upstream already - do not file

- Burger King store 482086 menu fetch failed twice with a server error → family of #14, #18, #48, #54 (menu failures for specific/complex stores). Same failure reproduced 2026-08-10 for Papa Johns Pizza store 25600501 near the anchor address, Madison (store_is_open: true both times, message "Something went wrong retrieving the menu ... Please try again", retried once per protocol, still failed) - blocked pricing that store out of the pizza comparison entirely.
- `--intent` required everywhere, undocumented → #39.
- No Linux build (why we run a Mac at all) → #9, #22, #50.
- Headless/device-code login gap in general → #15, #36, #22 (our specific token-discard variant is NEW, item 3 above).

### 20. Same wallet BOGO attaches to one item at a store and silently refuses another - `promo list` cannot tell you which

Date: 2026-08-11.
Store: Domino's 34162385, menu 67565146, wallet campaign 369d9447-4829-4058-93bb-fc779f1b73f9
("Buy 1, get 1 free" / "Delivery only"), `promo list` reporting `Found 1 eligible promotions`,
`is_applied:false`, `discount_cents:0`.
Expected: one signal that says which menu items the BOGO covers, or an apply error that names the
failed condition.
Observed, three delivery previews at the same store within ten minutes of each other:
  2x Build Your Own Pizza (i_25295246854, Medium/Hand Tossed) subtotal $26.18 -> PROMOTION_DISCOUNT
    -$10.89, total $17.51. BOGO attached.
  2x Custom Medium 2-Topping 12" Pizza (i_27545037948) subtotal $33.74 -> no discount, total $37.38.
  2x 5-Cheese Mac & Cheese (i_25295357524, Oven-Baked Pastas) subtotal $19.58 -> no discount,
    total $21.70.
So eligibility is per menu item, it is not exposed anywhere in `menu`, `promo list`, or
`restaurant-item-details`, and the two ineligible carts priced $19.87 and $10.33 above the
prediction that assumed the listed BOGO would apply. Concrete instance of item 18, but sharper:
here the SAME campaign demonstrably works and fails at the SAME store depending only on item id,
which rules out the "store eligibility check" the error message blames.
Impact for this scanner: any predicted total that leans on a BOGO is unsafe until previewed, even
when the same promo was verified at that store the day before.

### 21. `cart add-items` fails with an undiagnostic "Failed to create cart" once the account holds ~20 open carts

Date: 2026-08-13.
Command: `dd-cli --json-output cart add-items --store-id 36042509 --menu-id 82196752 --items-json '[{"item_id":"32877870578","item_name":"Spicy Chicken Sandwich","quantity":2}]' --fulfillment delivery --intent ...`
Expected: either the cart is created, or an error that names the actual constraint ("cart limit reached", with the limit).
Observed: bare `Error: Failed to add to cart: Failed to create cart..` on stderr - no JSON envelope at all, so no `success`, no `error_category`, no `cart_uuid`, nothing for an agent to branch on. The double period is verbatim.
The trigger is a per-account open-cart cap, not the store or the item. At the time of failure the account held 20 carts (13 pre-existing + 7 this run had created). The identical call for the identical item succeeded immediately after this run deleted its 7 carts, and priced correctly ($14.78 subtotal, -$7.39 BOGO, $8.84 pre-tip - to the cent identical to previous runs). Reproduced twice on Popeyes 36042509 and twice on Domino's 34162385 while at the cap, then succeeded for Popeyes after cleanup.
Impact for this scanner: the cap is shared with the carts the *consumer* created, so the headroom available to a probing agent is not knowable in advance and shrinks as the user's own carts accumulate. The failure looks exactly like a store-side outage, so a coverage sweep can silently drop its last few stores and blame the store. Mitigation now in use: delete probe carts eagerly rather than batching cleanup at the end.
Distinct from #7/#16 (those leak a cart on failure; this one creates nothing and returns no envelope).

### 22. Domino's `Build Your Own Pizza` rejects the required size option id that the error payload itself advertises

Date: 2026-08-13.
Store 34162385, menu 67565146, item `25295246854` "Build Your Own Pizza".
Expected: supplying `o_44211085693` ("Medium") satisfies the required group "Choose your size" (`e_9656483326`, min 1, max 1).
Observed: `success:false`, `message:"Add to cart failed. 1 item failed to add."`, and
`debugMessage:"Please select at least 1 options for Choose your size"` - even though the request carried exactly that option id. The error's own `required_options[]` block then lists `o_44211085693 Medium $2.20` as a valid choice for that group, so the server is rejecting the id it simultaneously recommends.
Tried both shapes, same result: nested (size -> crust -> seasoning, the exact payload that succeeded at this store on 2026-08-13 at 15:58 UTC) and flat (size only). Each failed attempt leaked an empty cart (defect 16); both were deleted.
Note `item_errors` renders as `[null]` under `jq -c '{message,item_errors:[.item_errors[]?.error_message]}'` because the field is `message`, not `error_message` - the useful text is one key over from where defect 7's shape suggests.
Impact: blocked the Domino's benchmark for this run entirely. Since the identical payload worked ~5 hours earlier the same day, this reads as a server-side menu/version change mid-day rather than a stale option id on our side, but nothing in the response distinguishes the two.

### 23. Defect 22 RESOLVED: `cart add-items` wants nested modifier groups as a FLAT `nested_options` list, not a nested tree

Date: 2026-08-14. Store 34162385, menu 67565146, item `25295246854` "Build Your Own Pizza".
Defect 22 recorded this item as unaddable. It is addable; the payload shape was wrong.
Observed: the nested/tree shape (size -> {crust -> {seasoning}}) fails with
`debugMessage:"Please select at least 1 options for Choose your crust"` - note the error names the
SECOND level, so the size selection WAS parsed and only the deeper levels were dropped. (On
2026-08-13 the same shape reported "Choose your size", which is what made it look like a rejected
size id.) Supplying all three ids as siblings in one flat `nested_options` array -
`[{Medium},{Hand Tossed},{Garlic Crust Seasoning}]` - succeeds immediately: subtotal $26.18, BOGO
campaign 369d9447 attached -$10.89, pre-tip $17.51, matching the 2026-08-11 benchmark to the cent.
So the server was never rejecting the id it advertised; the CLI silently discards nested
`nested_options` below the first level. `cart add-items -h` documents the nested shape for combos,
which is what misleads callers here.
Impact: this had blocked the Domino's benchmark for a whole run.

### 24. `order preview` returns `quote.map_item_subtotal` of $0.00 on a valid, non-empty quote

Date: 2026-08-14. Command: `order preview --cart-uuid <id> --fulfillment delivery` on a McDonald's
cart (store 658055) holding 2x Spicy McCrispy.
Expected: `map_item_subtotal` carries the cart subtotal, or is absent.
Observed: `map_item_subtotal.display_string == "$0.00"` while `line_items[]` with
`charge_id:"SUBTOTAL"` correctly reads $14.38 and `total_before_tip` reads $16.21. Reproduced on all
9 previews pulled this run. Adjacent to item 17 (where a $0.00 subtotal at least matched an empty
cart) but worse: here the field is $0.00 on a fully populated, correct quote, so a caller that reads
the obviously-named field gets a wrong number with no error. Read subtotal from
`line_items[] | select(.charge_id=="SUBTOTAL") | .final_money`, never from `map_item_subtotal`.

### 25. Wallet code TRIV10W29 ("DashPass Trivia: $5 off $12+") listed at 20+ stores, attaches at none

Date: 2026-08-14. `promo list` returns campaign daf3e303-1dd1-4d40-92b2-c5af7a6084f0 for 20+ Madison
stores. `promo apply --promo-code TRIV10W29` then refused at 658055 ($14.38 cart), 23583656 ($14.62)
and 34162385 ($19.58) - all comfortably over the stated $12 minimum - with the usual
`promo_not_attached` / "store eligibility check likely failed".
Another instance of item 18, recorded because this campaign is broad enough to distort a whole
ranking: it appears as unconfirmed upside on 20 of 39 ranked candidates.
Also reconfirms item 6 in the opposite direction the same run: at Cafe MiMi 36024959 `promo apply`
returned "Promotion did not attach in preview" and the next `order preview` showed
PROMOTION_DISCOUNT -$6.00. The apply response remains useless in both directions.

### 26. `cart add-items` silently JOINS a pre-existing cart at the same store instead of creating a new one

Date: 2026-08-16. Store 23460971 (Naf Naf Grill), menu 13297952.
Command: `cart add-items --store-id 23460971 --menu-id 13297952 --items-json '[{"item_id":"11328462523","item_name":"Six Pack Pitas","quantity":2}]' --fulfillment delivery`.
Expected: a new cart containing only the 2x Six Pack Pitas, or an explicit signal that an existing
cart at this store was reused.
Observed: `success:true`, `message:"Add to cart succeeded. Cart now has 2 items."`, and a
`cart_uuid` (bf328fca-d31a-4ad1-ba3e-5d2eb6638247) whose contents were
`[Build Your Own Bowl x1, Six Pack Pitas x2]`. The Build Your Own Bowl was NOT added by this run -
the account already had an open cart at that store. Nothing in the response distinguishes
"created a cart" from "appended to the consumer's existing cart": no `created:true/false`, and the
`cart.items` echo looks exactly like a normal multi-item add.
Consequences, both realized in this run:
  1. Pricing: the preview came back at subtotal $32.90 / pre-tip $32.23 instead of the $19.40
     subtotal the candidate called for, so the row does not test the prediction at all.
  2. Safety: a "delete every cart you created" cleanup step then deleted a cart the agent did not
     create, destroying the consumer's saved Build Your Own Bowl. There is no way to undo this and
     no way to have known in advance from the add-items response alone.
Mitigation now mandatory for this scanner: run `cart list` BEFORE any add-items, keep the set of
pre-existing `cart_uuid`s, and (a) never probe a store that already has an open cart, (b) never
delete a uuid that was in the pre-existing set - use `cart remove-item` on only your own line items.
Distinct from #7/#16/#21, which are all about carts that get created; this is about a cart that
does not.

### 27. `promo list` returns an EMPTY list at a store where a campaign discount then applies automatically

Date: 2026-08-19. Store 36042509 (Popeyes), cart 2x Spicy Chicken Sandwich (item 32877870578,
menu 82196752), delivery.
Command: `promo list --store-id 36042509` immediately before `order preview --cart-uuid <id>
--fulfillment delivery`.
Expected: `promo list` enumerates the BOGO campaign that is about to discount this cart, or the
preview shows no discount.
Observed: `promo list` returned `promotions: []` - not "did not attach", not "ineligible", simply
nothing. The very next `order preview` on that cart nonetheless returned
`PROMOTION_DISCOUNT -$7.39` with `quote.discount_banner_details[].promotion_id` =
`a1b05fe4-5986-482f-b23e-b2be3363dbf9`, which is one of the two BOGO campaigns this scanner's own
`promos.tsv` has recorded at this store for weeks. Pre-tip total $8.84, identical to every prior
run.
This is the mirror image of item 18: there, `promo list` over-reports (calls unattachable promos
"eligible"); here it under-reports to zero while the discount is real and applied. Both directions
mean the same thing for a caller - `promo list` cannot be used to predict cart pricing, and the
discount must be read off `order preview` every time.
Note also that an empty `promo list` is documented in `-h` as "a normal response, not an error:
it just means no campaign promos are eligible at that store for that consumer right now." That
sentence is demonstrably false here.
Impact for this scanner: a promo-driven prediction cannot be skipped just because `promo list` is
empty, so the empty case gives no call-budget saving.

### 28. `restaurant-item-details` returns the same flat item envelope as `menu` - no option groups, so required modifiers stay undiscoverable without a deliberately failed `cart add-items`

Date: 2026-08-25.
Command: `dd-cli --json-output restaurant-item-details --store-id 34162385 --item-id 25295246854 --menu-id 67565146 --intent ...` (Domino's "Build Your Own Pizza", `has_required_modifiers: true`).
Expected: an item-details command, as distinct from a bulk `menu` listing, exposes the item's modifier/option groups with their ids, so a caller can construct a valid `add-items` payload.
Observed: `.structuredContent.item` carries exactly the same 16 keys the `menu` listing already returns (`category_id`, `category_name`, `description`, `extras`, `has_modifiers`, `has_required_modifiers`, `image_url`, `is_orderable`, `is_popular`, `item_id`, `name`, `popular_modifications`, `popularity_rank`, `price`, `price_varies`, `unavailability_reason`) - no `option_groups`, no `options`, no `required_options`. `has_required_modifiers: true` therefore tells a caller that options are required while giving no way to learn what they are.
The only path to the option ids is to fire an `add-items` you expect to fail and read `item_errors[].required_options[]` out of the error payload - which also leaks an empty cart (defect 16). This makes defect 19 worse than recorded: the required-modifier data is not merely absent from `menu`, it is absent from the command whose entire purpose is per-item detail.
Impact for this scanner: any modifier-carrying item costs an extra call and a leaked cart before it can be priced at all.

### 29. `menu` reports `has_required_modifiers: false` for an item that `cart add-items` then refuses for a missing required option group

Date: 2026-08-25. Store 606420 (Taiwan Little Eats), menu 19750347, item `i_27314085105` "Shacha Fried Noodles沙茶炒麵".
Expected: `has_required_modifiers` is true whenever the server will reject a bare add.
Observed: the cached menu envelope carries `has_required_modifiers: false` (and `has_modifiers: true`), so the scanner treated the item as directly addable. `cart add-items` with the bare item then failed:
`Failed to add item [Shacha Fried Noodles沙茶炒麵]: INVALID_ARGUMENT: {"errorCode":"item_validation_error","debugMessage":"Please select at least 1 options for Choose a Flavor口味"...}`.
The error payload's `required_options[]` lists the group with `id: null`, `min_options: null`, `max_options: null` - only the child options carry ids (`o_45191872965 Original原味 $0.00`, `o_45191872966 Chicken Cutlet雞排 $6.50`, `o_45191872968 Pork Rib豬排 $6.50`). Re-adding with a flat `nested_options: [{"id":"o_45191872965",...}]` succeeded and priced $9.50 subtotal, so the item is fine; only the flag is wrong.
Distinct from defect 19 (there the flag was true and only the option data was missing). Here the boolean itself is false-negative, so a caller cannot even know to look.
Impact: one wasted add call plus a leaked empty cart per affected item, and `req_mods` in `items.tsv` cannot be trusted as a filter.

### 28. `order preview --fulfillment pickup` returns a fully-priced PICKUP quote at a store whose own `asap_pickup_available` is `false`

Date: 2026-08-30. Store 28087966 (Raising Cane's Chicken Fingers), cart ced60ea1 holding 1x Sandwich.
Expected: either the pickup preview refuses / warns when the store is not offering ASAP pickup, or the
positive pickup markers stay unset so a caller cannot mistake the quote for an orderable price.
Observed: `.structuredContent.quote.delivery_availability.asap_pickup_available` is `false` (and
`asap_pickup_minutes_range` is still populated, `[15,25]`), while the same response carries
`store_order_cart.fulfillment_type: "PICKUP"`, `store_order_cart.is_consumer_pickup: true`,
`total_before_tip: $9.27`, a `SERVICE_FEE` of $0.00 and no `DELIVERY_FEE` line - i.e. every positive
signal this scanner uses to confirm "this really is a pickup quote" says yes, and only the one
availability boolean says the store will not actually take a pickup order right now. An agent that
detects pickup the documented way (fulfillment_type / is_consumer_pickup) and does not separately read
`asap_pickup_available` will publish $9.27 as a real pickup price for a store that is delivery-only today.
Compare store 36042509 and 31014406 on the same day, where `asap_pickup_available` was `true` alongside
the same positive markers - nothing else in the payload distinguishes the two cases.
Note also `asap_pickup_available: false` is falsy, so the common jq idiom `(... // "null")` silently
renders it as "null" rather than "false"; use `| tostring`.

### 29. `cart add-items` ITEM_MUTATION_UNAVAILABLE leaks an empty cart and does not recover on the retry it recommends

Date: 2026-08-30. Store 606420 (Taiwan Little Eats), menu 19750347, item `27314085105`
"Shacha Fried Noodles" (cached menu `is_orderable: true`).
Expected: the advertised recovery ("Read the cart, then retry this item on its own in a few seconds")
works, or the failure does not create a cart.
Observed: both attempts failed identically with
`ITEM_MUTATION_UNAVAILABLE: A DoorDash service needed to add this item could not be reached`, and both
returned the SAME `cart_uuid` 4e011a0d-db84-4b81-8312-d54783c7380c - a real, empty cart (`cart show`
confirmed `items_count: 0`). So the error path both leaks a cart (instance of defect 16) and re-uses the
leaked one on retry (instance of defect 26, benignly here since the cart was ours). The cart was deleted.
Distinct from defect 16 only in that the advertised retry guidance is itself unreliable: two attempts a
minute apart, same result. Cost: the store was dropped from the scan in both lanes.

### 34. Pickup quotes list a "DashPass Pickup Benefit" promotion that carries no discount line
Date: 2026-09-01.
Command: `order preview --cart-uuid <uuid> --fulfillment pickup` at stores 800730, 36042509, 31014406, 289146, 350727.
Expected: if `.structuredContent.quote.promotions[]` names an applied promotion, a matching `PROMOTION_DISCOUNT` line item exists, or the promotion is marked as non-monetary.
Observed: every pickup quote returned `promotions: [{"title":"DashPass Pickup Benefit","campaign_id":"c45c15f8-19dc-4673-8642-b60f4218d095"}]` while `line_items` held only SUBTOTAL, TAX and SERVICE_FEE ($0.00) - no PROMOTION_DISCOUNT at all, and the total equals subtotal + tax exactly.
The same cart's DELIVERY quote lists no promotions.
So `quote.promotions` on a pickup quote is a benefit BADGE, not evidence of money off. A caller that reads promotions as "a promo attached" would report a discount that does not exist.
Reading the discount off the PROMOTION_DISCOUNT line item, as this repo does, is correct and stays correct.

### 35. `add-items` unavailability error is now unambiguous (improvement, not a defect)
Date: 2026-09-01.
Command: `cart add-items --store-id 606420 --menu-id 19750347 --items-json '[{"item_id":"27314085105","item_name":"Shacha Fried Noodles","quantity":1}]'`
Observed: `Error: ITEM_UNAVAILABLE: The store confirmed these items are not orderable right now: 27314085105 (Shacha Fried Noodles). This is a store-side stock or hours problem, not a problem with your request.`
This is the well-formed-request case, and the message now says so explicitly.
It no longer collides with the malformed-`items-json` failure documented earlier ("item(s) [unknown] are currently not available... store may be closed"), which still reports the id as `[unknown]`.
Practical rule: `[unknown]` in the error means check the JSON shape and the stripped `i_` prefix; a real item id echoed back means the store genuinely cannot sell it right now.
Confirms too that a cached `is_orderable: true` is not a live signal - store 606420's cached menu said orderable.

### 36. `menu` prices are DELIVERY prices only; the pickup quote reprices the same item lower, with no pickup price anywhere in the menu payload
Date: 2026-09-04.
Command: `menu --store-id 36042509` (item `i_32877870578` Spicy Chicken Sandwich, `price: 7.39`), then
`order preview --cart-uuid <uuid> --fulfillment pickup` on a cart of 2 of that item.
Expected: either the menu price holds in both fulfillment modes, or `menu` exposes the pickup price
(a `pickup_price` / price-by-mode field) so a caller can predict a pickup total.
Observed: the pickup quote priced the pair at a $11.98 subtotal, i.e. $5.99 each - 19% under the menu
price - while the delivery quote used $7.39 each. Same effect at Kosharie 800730 on the same day:
menu $8.49, pickup subtotal $14.98 for 2 = $7.49 each. Nothing in `menu`, `restaurant-item-details`
or the cart echo carries the in-store price, and `cart add-items` echoes the delivery price even for a
cart that will later be previewed as pickup.
Impact for this scanner: every pickup estimate built from menu prices is biased HIGH at these stores
(predicted $17.91 vs actual $15.80 at Kosharie today, the run's only accuracy miss). Biased in the
safe direction, but it makes the pickup lane's estimates unusable as prices, which is why only a real
pickup preview is ever reported.
Related to item A (a delivery-only promo silently not attaching on pickup): the same Popeyes preview
also dropped the BOGO, so the pair cost MORE picked up ($12.64) than delivered ($8.84).
