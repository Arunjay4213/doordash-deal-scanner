# dd-cli cookbook - verified recipes

dd-cli v0.2.1, macOS arm64, run on `macbox` as `ec2-user`.
Every command line below was run for real on 2026-08-10 and produced the output shown.
Copy-paste directly; do not re-derive flags with `-h`.

## Ground rules

`--json-output` is a **global** flag and must come **before** the subcommand.

```
dd-cli --json-output cart list --store-id 36042509 --intent "$INTENT"    # right
dd-cli cart list --json-output --store-id 36042509 --intent "$INTENT"    # wrong
```

`--intent` is required on every service command. Export it once:

```bash
INTENT='Summary: Help the user find the best-value meal deals near their saved address.
user prompt/purpose: "i want it as a research tool that will help me find the absolute best deals for lunch and dinner for me"'
```

Every response is wrapped in an envelope with keys `content`, `isError`, `structuredContent`.
Read data from `.structuredContent`. `.content` is text-rendered and useless for parsing.

The Mac's login shell is **zsh**, which does NOT word-split unquoted `$var`.
`for c in "cart list"; do dd-cli $c -h; done` fails with `No such command 'cart list'`.
Write commands out in full or use `${=c}`.

## Command reference

### search - find stores

```bash
dd-cli --json-output search -q "popeyes" --lat "$DD_LAT" --lng "$DD_LNG" --limit 5 --intent "$INTENT"
```

Flags: `-q/--query` (required), `--lat`, `--lng`, `--limit` (default 5).
Without lat/lng it falls back to `DD_LAT`/`DD_LNG` env, then a Cupertino default.
Read `.structuredContent.stores[].store_id`.

### menu - store menu and menu_id

```bash
dd-cli --json-output menu --store-id 36042509 --intent "$INTENT" > menu.json
jq -r '.structuredContent.menu_id' menu.json                       # 82196752
jq -r '.structuredContent.items[] | "\(.item_id)\t\(.name)\t\(.price)"' menu.json
```

Flags: `--store-id` (required).
`items[].item_id` comes back **with an `i_` prefix** (`i_32877870578`).
Strip `i_` before passing to `restaurant-item-details --item-id` or `cart add-items`.

### restaurant-item-details - option groups for one item

```bash
dd-cli --json-output restaurant-item-details \
  --store-id 36042509 --menu-id 82196752 --item-id 32877870570 --intent "$INTENT" > item.json
```

Flags: `--store-id`, `--menu-id`, `--item-id`, all required. Item id **without** the `i_` prefix.

Option groups live at `.structuredContent.item.extras[]` - note the `.item` level, it is
not at the top of `structuredContent`. Each group has `extra_id`, `min_num_options`,
`max_num_options` and `options[]`. Each option has `option_id`, `name`, `price`, and its
own `extras[]` for the next nesting level.

```bash
# top-level groups
jq -r '.structuredContent.item.extras[] |
  "GROUP \(.extra_id) min=\(.min_num_options) max=\(.max_num_options)",
  (.options[] | "    \(.option_id)  \(.name)  +\(.price//0)  subextras=\(.extras|length)")' item.json

# one level deeper, under a chosen option
jq -r '.structuredContent.item.extras[] | select(.extra_id=="e_10391377774")
  | .options[0].extras[] | "SUB \(.extra_id) min=\(.min_num_options)",
    (.options[] | "    \(.option_id)  \(.name)  +\(.price//0)")' item.json
```

`extra_id` is display grouping only - **never** send it to `cart add-items`. Send `option_id`.
`option_id` keeps its `o_` prefix when sent (verified: `o_46915589123` priced correctly).

### cart list - the pre-flight check

```bash
dd-cli --json-output cart list --intent "$INTENT"                         # all carts
dd-cli --json-output cart list --store-id 36042509 --intent "$INTENT"     # one store
```

Flags: `--store-id` (optional filter), `--beautify` (incompatible with `--json-output`).

```bash
jq -r '.structuredContent.carts[] | "\(.store_id)\t\(.store_name)\t\(.items_count)\t\(.cart_uuid)"'
```

`created_at` / `updated_at` are epoch **milliseconds** (13 digits), may be `null` on old carts.

### cart add-items - creates the cart

**There is no `cart create` command.** A cart is created implicitly by `cart add-items`
when you omit `--cart-uuid` and no open cart exists at that store.

```bash
dd-cli --json-output cart add-items \
  --store-id 36042509 --menu-id 82196752 --intent "$INTENT" \
  --items-json '[{"item_id":"32877870578","item_name":"Spicy Chicken Sandwich","quantity":2}]'
```

Flags: `--store-id`, `--menu-id`, `--items-json` required; `--cart-uuid`,
`--fulfillment delivery|pickup` (default delivery), `--group-cart`, `--spend-limit-cents` optional.

Read `.structuredContent.cart_uuid`, `.structuredContent.success`, and on partial failure
`.structuredContent.item_errors[]` (entries carry `error_message` and `required_options[]`).

`item_name` is display-only. A placeholder works - the backend substitutes the real menu name.
Verified: sent `"item_name":"Item"`, preview came back as `"Spicy Chicken Sandwich Combo"`.

#### nested_options JSON shape

Each option entry needs `id`, `name`, `quantity`. A choice that has its own required
selections carries them in `options[]`, recursively. **The nesting mirrors the menu:**
size contains crust, crust contains toppings.

```json
[{
  "item_id": "32877870570",
  "item_name": "Spicy Chicken Sandwich Combo",
  "quantity": 1,
  "nested_options": [
    { "id": "o_46915589122", "name": "Spicy Chicken Sandwich", "quantity": 1,
      "options": [
        { "id": "o_46915589123", "name": "Add Cheese", "quantity": 1 }
      ] },
    { "id": "o_46915589126", "name": "Regular Cajun Fries", "quantity": 1 },
    { "id": "o_46915589131", "name": "Regular Coca-Cola", "quantity": 1 }
  ]
}]
```

Verified result: combo $12.39 + Add Cheese $0.50 = subtotal **$12.89**.

Deeper verified chain (Domino's 34162385, BYO item 25295246854, menu 67565146), base $10.89:
Medium `o_44211085693` (+$2.20) -> under it Hand Tossed `o_44211085783` ($0) -> under that
Garlic Crust Seasoning `o_44211085868`, Premium Chicken `o_44211085800` (+$1.89, side Whole
`o_44211085802`), Onions `o_44211085820` (+$1.89, side Whole `o_44211085822`), Black Olives
`o_44211085832` (+$1.89, side Whole `o_44211085834`). The "Whole" side id is different for
every topping - never reuse one.

### cart show

```bash
dd-cli --json-output cart show --cart-uuid "$CU" --intent "$INTENT"
jq -r '.structuredContent.cart.items[] | "\(.id)\t\(.item_id)\t\(.name)\tqty=\(.quantity)"'
```

`items[].id` is the **cart-line** id (for `cart remove-item --cart-item-id`).
`items[].item_id` is the **menu** item id. Do not swap them. No pricing here.

### cart remove-item / cart delete

```bash
dd-cli --json-output cart remove-item --cart-uuid "$CU" --cart-item-id "$LINE_ID" --intent "$INTENT"
dd-cli --json-output cart delete --cart-uuid "$CU" --intent "$INTENT"
```

`cart delete` empties the cart and invalidates the uuid. Only `--cart-uuid` is required.

### promo list / apply / remove

```bash
dd-cli --json-output promo list --store-id 36042509 --intent "$INTENT"
jq -r '.structuredContent.promotions[]? | [.code,.campaign_id,.ad_group_id,.ad_id,.title] | @tsv'

# campaign promo: pass all four values from one promo list row
dd-cli --json-output promo apply --cart-uuid "$CU" \
  --promo-code "$CODE" --campaign-id "$CAMP" --ad-group-id "$ADG" --ad-id "$ADID" --intent "$INTENT"

# user-typed code: promo-code only, drop the three id flags
dd-cli --json-output promo apply --cart-uuid "$CU" --promo-code WELCOME20 --intent "$INTENT"

# remove mirrors apply - pass the same flags that were used to apply
dd-cli --json-output promo remove --cart-uuid "$CU" \
  --promo-code "$CODE" --campaign-id "$CAMP" --ad-group-id "$ADG" --ad-id "$ADID" --intent "$INTENT"
```

`--cart-uuid` and `--promo-code` are required; `--campaign-id`, `--ad-group-id`, `--ad-id`
are optional and only used for campaign promos.

### order preview - the only pricing source

```bash
dd-cli --json-output order preview --cart-uuid "$CU" --fulfillment delivery --intent "$INTENT"
```

Flags: `--cart-uuid` required; `--fulfillment delivery|pickup`, `--scheduled-time`,
`--priority`, `--include-work-benefits`, `--selected-budget-id`, `--no-apply-credits`,
`--beautify`.

Passing `--fulfillment` **mutates** the cart's mode, then re-prices in one shot.
Without it, preview is read-only.

`order preview` is the last call you may ever make on a cart. Never run `order submit`
or `order checkout-url`.

## Quote parsing - exact jq paths

Verified against a real Popeyes preview (store 36042509, 2x Spicy Chicken Sandwich):

```bash
Q='.structuredContent.quote'

# breakdown rows, keyed by charge_id
jq -r "$Q.line_items[] | \"\(.charge_id)\t\(.final_money.display_string)\""
# SUBTOTAL            $14.78
# TAX                 $0.46
# DELIVERY_FEE        $0.00
# SERVICE_FEE         $0.99
# PROMOTION_DISCOUNT  -$7.39

jq -r "$Q.total_before_tip.display_string"        # $8.84
jq -r "$Q.net_total_before_tip.display_string"    # $8.84  (same value)
jq -r "$Q.is_dashpass_applied"                    # true

# applied promo title, straight off the preview - no promo list call needed
jq -r "$Q.promotions[]? | \"\(.title)\t\(.campaign_id)\""   # Buy 1, get 1 free  a1b05fe4-...

# per-line items
jq -r "$Q.store_order_cart.orders[]?.order_items[]?
       | \"\(.item.name)\tqty=\(.quantity)\t\(.unit_price_monetary_fields.display_string)\""
# Spicy Chicken Sandwich  qty=2  $14.78
```

Read money as `final_money.display_string` and strip `$` and `,`:

```bash
jq -r '.display_string | gsub("[$,]";"")'
```

Other charge ids seen in the wild: `CREDITS`, `TAXES_AND_FEES`, `TIP`, `DISCOUNT`.
Not every cart has every row - always guard with `//` or `first`.

## Traps

**`final_money.unit_amount` is UNSIGNED.** `PROMOTION_DISCOUNT` came back as
`unit_amount: 1478 -> 739` positive with `display_string: "-$7.39"` and `sign: false`.
Summing `unit_amount` values gives a wrong total. Use `display_string` (it carries the
minus sign) or read the `sign` boolean.

**`order_items[].unit_price` is the LINE total, not the unit price.** For 2x a $7.39
sandwich, `unit_price_monetary_fields.display_string` is `$14.78`. In our sample
`unit_price` itself was literally `null`. Never compute `unit_price * quantity`.

**`cart add-items` merges.** Identical `item_id` + identical modifiers are summed into one
line. Cart had 2x sandwich -> `items_count: 1`, `quantity: 2.0`. There is no set-quantity
call; to reach a target N, either add the delta or `cart remove-item` then re-add.

**`cart delete` can resurrect.** A delete can report `success: true` and items reappear as
a new cart. Cleanup must be a loop: `cart list --store-id X`, delete every uuid returned,
sleep, repeat until the list is empty. Do not trust a single delete.

**Creating a cart at an occupied store DISPLACES the user's cart.** One open cart per store
is the hard backend limit. `cart add-items` without `--cart-uuid` silently appends to
whatever is already there. Always run `cart list --store-id X` first and refuse if non-empty.

**Wallet promos auto-attach on delivery preview.** At Popeyes, `promo list` returned an
**empty** `promotions[]`, yet the preview came back with `PROMOTION_DISCOUNT -$7.39` and
`quote.promotions[0].title = "Buy 1, get 1 free"`. So an empty `promo list` does not mean
no discount, and you often need no `promo apply` at all.

**`promo remove` usually works, but verify via preview.** An early 2026-08-10 run saw it
fail on an auto-redeemed BOGO ("store policy may require it"); a same-day retest detached
cleanly at two stores, giving true no-promo baselines, and re-apply restored the promo.
Treat a stuck remove as possible but rare; the re-preview after removal is the proof.

**`promo apply` status has flip-flopped - trust the preview, not the message.** An early
2026-08-10 run returned `success: false` for a promo that then attached, and once appeared
to duplicate a cart line (2 sandwiches became 4); a same-day retest showed the success flag
accurate 3/3 with no duplication. Cheap insurance: re-read `cart show` after an apply and
read the discount only off the preview's `PROMOTION_DISCOUNT` line.
Known failure-message defect that DOES persist: apply blames a generic "store eligibility
check" even when the real cause is a condition `promo list` itself displayed (e.g. "$5 off
on $30+" applied to a $7.50 cart). Check the promo description for minimums before
concluding a promo is broken.

**`discount_banner_details[].discount_details_message` is not the promo discount.** It read
"Saving $9.88 with DashPass + Deals" while `PROMOTION_DISCOUNT` was $7.39 - the extra $2.49
is the DashPass-waived delivery fee. Use the `PROMOTION_DISCOUNT` line for the promo amount.

**Rate limit** around 88-90 dd-cli calls per session. Budget calls; a `price_cart.sh` run
costs 5-8.

**`-h` needs the keychain.** Help on service commands fails with "Keychain unavailable" if
the login keychain is locked. `~/.zshenv` unlocks it for SSH one-liners.

## price_cart.sh

```bash
~/deal-scanner/price_cart.sh spec.json      # or: echo "$SPEC" | ~/deal-scanner/price_cart.sh -
```

Spec:

```json
{
  "store_id": "36042509",
  "items": [
    { "item_id": "32877870578", "item_name": "Spicy Chicken Sandwich",
      "menu_id": "82196752", "quantity": 2,
      "options": [ "o_46915589126",
                   {"id":"o_46915589122","name":"Sandwich","quantity":1,
                    "options":[{"id":"o_46915589123","name":"Add Cheese","quantity":1}]} ] }
  ],
  "promo": "auto"
}
```

`options[]` accepts bare option-id strings or full objects; both are normalised to the
`nested_options` shape. `item_name` is optional. All items must share one `menu_id`.

`promo`: `"auto"` keeps whatever auto-attaches and applies the first `promo list` row only
if nothing attached; `"none"` tries to remove attached promos and warns if one sticks;
anything else is matched against `promo list` `code`/`campaign_id`, falling back to a
literal promo code.

Output (real run, 8.6s wall clock):

```json
{"store_id":"36042509","cart_uuid":"4a33b420-c91d-44e3-be08-51d59b4447ce","subtotal":"14.78","discount":"-7.39","delivery_fee":"0.00","service_fee":"0.99","tax":"0.46","total_before_tip":"8.84","per_line":[{"name":"Spicy Chicken Sandwich","quantity":2,"line_total":"14.78"}],"promo_title":"Buy 1, get 1 free","is_dashpass_applied":true,"warnings":[],"cart_deleted":true}
```

`discount` keeps the source's sign (negative). Missing rows come back `null`.

Exit codes: `0` priced and cleaned up, `1` failure (partial JSON on stdout, cleanup still
attempted via an EXIT trap), `2` store already has a cart - nothing was created or changed.

The script contains no call to `order submit` or `order checkout-url`.
