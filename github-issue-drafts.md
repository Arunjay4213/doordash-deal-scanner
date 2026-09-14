# Draft GitHub issues for doordash-oss/doordash-cli

All claims verified live on 2026-08-30, v0.2.3 linux-amd64.
Filed 2026-08-30 as upstream issues #99 #100 #101 #102 #103.

---

# DRAFT 1

**Title:** Promo error blames the store when the item is not eligible

Version: v0.2.3, linux-amd64

When a promo does not attach, the CLI always returns:

```
{"success":false,"discount_cents":0,
 "message":"Promotion did not attach in preview - store eligibility check likely failed.",
 "error_category":"promo_not_attached"}
```

The message points at the store. In my test the store was fine. Same store, same BOGO, ten minutes apart. A cart with an eligible sandwich got $7.39 off. A cart with a different item got the error above. Only the item changed.

The menu response now includes `items[].applicable_promotion_ids[]`. In my tests it predicted both results correctly. So the CLI already knows which items a promo covers.

Two requests:

1. If the item is not covered, say that. Something like "item not eligible for this promotion".
2. Document `applicable_promotion_ids`. I only found it by diffing raw responses.

---

# DRAFT 2

**Title:** cart add-items fails at 20 open carts, and the error tells you to retry

Version: v0.2.3, linux-amd64

An account can hold 20 open carts. Carts made in the phone app count too. At 20, every `cart add-items` fails, at any store. Exit code 1. Empty stdout, even with `--json-output`. stderr:

```
Error: TOOL_UNAVAILABLE: This request did not complete and the cause could not be identified.
Retry the same call unchanged in a few seconds. Do not rewrite the request to work around this -
the failure is not evidence that anything in it is wrong. If it keeps failing, stop and report the
failure rather than trying other tools, stores or items. (ref: 1e117a00968cbc632d00f27564e5bea3)
```

I deleted 9 carts and ran the same command. It worked right away. So the cause is the cart limit. But the error says the cause "could not be identified" and tells you to retry the same call. Retrying can never work here. Deleting a cart is the only fix, and the error says not to work around it.

Requests:

1. Return a JSON error that names the limit, with the cap and the current count.
2. Document the cap in `--help`. Show the count in `cart list`.

Not the same as #64 / #78 / #82. Those create a cart when an add fails. Here nothing is created and there is no stdout.

---

# DRAFT 3

**Title:** promo list and menu return different promotions for the same store

Version: v0.2.3, linux-amd64

Same store, same minute:

- `promo list --store-id X` returned 1 promotion.
- `menu --store-id X` returned 3 in its `promotions[]` block. The same one plus two BOGOs.

One of the promos that `promo list` left out applied itself on my next `order preview`. The discount was $7.39, and `discount_banner_details` named its code. `promo apply` also accepted that code.

The help text for `promo list` says a short response means nothing else is eligible for that store and consumer right now. That was wrong here. A promo it left out applied minutes later.

Request: make `promo list` return the same campaigns the menu response has, or document what it leaves out and why. Today the only safe way to price a cart is a full preview. That costs extra calls on a rate-limited API (#94).

---

# DRAFT 4

**Title:** map_item_subtotal is always $0.00

Version: v0.2.3, linux-amd64

On a delivery quote for a cart with items, `quote.map_item_subtotal` reads $0.00. The rest of the response is priced correctly. Five previews today, four stores:

```
map_item_subtotal   SUBTOTAL line   total_before_tip
$0.00               $11.85          $19.86
$0.00               $11.50          $15.80
$0.00               $14.78          $8.84   (BOGO applied, -$7.39)
$0.00               $19.18          $21.28
$0.00               $7.15           $14.91
```

The field is a complete money object set to zero. There is no error in the response. Code that reads it gets a wrong number with no warning. The real subtotal is in `line_items`, but nothing says to use that instead.

Request: fill the field or remove it. A $0.00 that means "not computed" looks the same as a real $0.00.

#75 covers a $0.00 total on an empty cart. My carts were full.

---

# DRAFT 5

**Title:** Missing-required-option error claims a server failure, and leaks an empty cart

Version: v0.2.3, linux-amd64

Add an item without a required choice, like a size. The error says:

```
ITEM_MUTATION_UNAVAILABLE: A DoorDash service ... could not be reached ... This is a server-side failure
```

Both claims are wrong. The same response includes a full `required_options[]` array with every choice and its price. The server understood the request fine. It only needed a size picked. The wording sends you off debugging connectivity.

The failed add also creates a real cart. The error payload shows a new `cart_uuid` with `"items":[], "items_count":0`. Empty carts count against the 20-cart limit (see my other issue). A script that retries failed adds slowly fills the account with junk carts.

Requests:

1. Say what happened: "required option group 'Pick Your Size' not selected". The response already contains this.
2. Do not create a cart when the add fails. Or mark the cart as created-but-empty so callers know to delete it.
