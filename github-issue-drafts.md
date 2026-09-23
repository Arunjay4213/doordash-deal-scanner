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

---

# Filed 2026-09-23 as upstream issues #132 to #137

Checked against all 122 tracker issues on 2026-09-23 before filing.

- #132 export-token: token exchange returns 401 after a successful browser sign-in (new, seen 2026-09-19, v0.2.3 and v0.2.5)
- #133 promo list --help says promos can only be applied in the browser, but promo apply exists (defect 8)
- #134 Pickup quotes list a "DashPass Pickup Benefit" promotion that carries no discount (defect 34)
- #135 Feature request: list wallet promotions without --store-id (defect 9)
- #136 --help on service commands fails with "Keychain unavailable" on Linux unless DD_CLI_ACCESS_TOKEN is set (defect 2)
- #137 Feature request: expiry dates on promotions (defect 5)

Not filed, and why:

- Defects 16, 18, 20, 21, 24, 25, 27, 29 (ITEM_MUTATION): already ours, #99 to #103.
- Defect 14 and 15 (rate limits): #94 asks for documented limits and separate read/mutation budgets.
- Defect 17 (empty-cart preview succeeds): #75.
- Defect 28 (pickup quote at a no-pickup store): #42.
- Defect 36 (menu prices are delivery prices): #125.
- Defect 10 (search misses a nearby store by name): #40.
- Defect 22/23 (nested_options shape): #85 covers the nested-options confusion.
- Defect 19 (menu price below the required-option minimum): fixed. The 2026-09-05 menu for store 379439 lists Spaghetti & Meatballs at 11.95 with price_varies true.
- Defect 29 (has_required_modifiers false negative): fixed. The 2026-09-16 menu for store 606420 reports true for item 27314085105.
- Defect 26 (add-items joins an existing cart): v0.2.5 help now documents the append behavior and the cart list pre-flight.
- Defect 28 (restaurant-item-details has no option groups): v0.2.5 help promises options[]; not re-tested, token exchange is broken (#132).
- Defects 1, 6, 12: last seen on v0.2.1 in August, no recent evidence, not re-testable until #132 is fixed.
- Defects 3, 4: macOS keychain paths; the Mac is gone.

## Body of 1-export-token-401

Version: v0.2.5 and v0.2.3, linux-amd64. First seen 2026-09-19.

`dd-cli export-token` fails at the last step. The browser sign-in completes, DoorDash redirects to the local callback with a code, and the code-for-token exchange returns 401.

Verbose log from v0.2.5 (`export-token -v`), code and verifier redacted by the CLI:

```
[OAuth callback] request line: GET /oauth2/callback?state=...&code=[REDACTED]&action=Login&method=Email HTTP/1.1
[OAuth token] POST https://unified-gateway.doordash.com/identity-bff/v1/oauth2/token
[OAuth token] request body: {"grant_type": "authorization_code", "client_id": "1862661064320741379", "code": "[REDACTED]", "redirect_uri": "http://localhost:4180/oauth2/callback", "code_verifier": "[REDACTED]"}
[OAuth token] response status: 401
[OAuth token] response headers: {'content-type': 'application/json', 'content-length': '144', 'x-request-id': '4867c8bc-00ad-4cae-a3c5-cc850278adad'}
Error: Authentication/Authorization failed
```

That request was at 2026-09-19 18:06:40 UTC. The response body is the generic one:

```
{"code":"","correlation_id":"...","localized_title":"","localized_message":"Unauthorized","internal_message":"Unauthorized","detail":null}
```

A deliberately wrong code gets the exact same body, so the real code is being rejected outright. The exchange was retried in five separate sign-in sessions on 2026-09-19 with the same result.

Setup: dd-cli runs on a Linux server with no browser. The browser is on a laptop, and the callback reaches the server over an SSH tunnel to port 4180. This is the documented headless path, and it worked with v0.2.3 on 2026-09-14. The same v0.2.3 binary fails now, so this looks like a change on the server side around the v0.2.5 release rather than in the CLI.

Impact: on Linux, `login` needs a keychain and `export-token` is the only way to get a token. With this failure a headless install has no way to sign in at all.

Requests:

1. Confirm whether the token endpoint or the client registration changed on 2026-09-18 or 2026-09-19.
2. On `-v`, print the response body of a failed token exchange. Today it prints only its length, which hides the reason.

## Body of 2-help-needs-token

Version: v0.2.5, linux-amd64. Verified 2026-09-19.

On Linux, `--help` on any service command fails unless a token is set:

```
$ dd-cli search --help
Error: Keychain unavailable. dd-cli requires keychain access to securely store credentials, or set the DD_CLI_ACCESS_TOKEN env var to run in a headless environment.
```

Set `DD_CLI_ACCESS_TOKEN` to any value, even an expired token, and the same command prints the help text. So the credential check runs before help parsing, and help never actually needs the token.

This matters for agents. They read `--help` to learn flags before they have a token, and the error tells them the tool is broken. It also blocks reading help on a fresh install to learn how to get a token in the first place.

Request: handle `-h` and `--help` before the credential gate, on every command.

## Body of 3-promo-list-help

Version: v0.2.5, linux-amd64. Verified 2026-09-19.

`dd-cli promo list --help` says:

```
For agents — applying an eligible promo:
  To apply an eligible promo that is not already on a cart,
  point the consumer at DoorDash's browser checkout:
    dd-cli order checkout-url --cart-uuid <id>
  Ask them to use the "Promo code" field on the checkout
  page to enter the code.
```

But `dd-cli promo apply` exists, is listed in `dd-cli promo --help`, and applies campaign promos to a cart. I have used it since v0.2.1.

An agent that follows the help text sends the consumer to the browser for something the CLI can do. That defeats the point of the CLI for that step.

Request: change the section to point at `promo apply`, and keep `order checkout-url` only as the fallback for codes `promo apply` rejects.

## Body of 4-pickup-benefit-badge

Version: v0.2.3, linux-amd64. Observed 2026-09-01 at five stores: 800730, 36042509, 31014406, 289146, 350727.

Every `order preview --fulfillment pickup` quote listed a promotion:

```
"promotions": [{"title": "DashPass Pickup Benefit", "campaign_id": "c45c15f8-19dc-4673-8642-b60f4218d095"}]
```

But `line_items` held only SUBTOTAL, TAX and a $0.00 SERVICE_FEE. There was no PROMOTION_DISCOUNT line, and the total was exactly subtotal plus tax. The same cart quoted for delivery listed no promotions at all.

So `quote.promotions` on a pickup quote is a membership badge, not money off. Nothing in the object says that. A caller that reads `promotions` as "a promo attached to this cart" reports a discount that does not exist.

Requests:

1. Mark promotions that carry no discount, for example with `discount_cents: 0` or a `type` field, or leave them out of `quote.promotions`.
2. Document that the only reliable discount signal is a PROMOTION_DISCOUNT line item.

## Body of 5-wallet-promo-listing

Version: v0.2.5, linux-amd64.

`promo list` requires `--store-id`. Wallet promotions are account-wide, but the only way to find them is to call `promo list` once per store and look for `source: "wallet"`.

For a consumer with a BOGO or "$5 off $12" code in their wallet, an agent asked "where can I use my promo?" has to probe every store nearby. That is one call per store against a rate-limited API (#94), and it still misses stores it did not think to check.

Request: let `promo list` run without `--store-id`, or add a `promo wallet` command, returning the consumer's wallet campaigns with their conditions and, where the server knows them, the eligible store ids.

## Body of 6-promo-expiry

Version: v0.2.5, linux-amd64.

Promotion objects carry no end date. `promo list` returns `ad_group_id, ad_id, campaign_id, code, description, discount_cents, is_applied, source, title`. The `promotions[]` block on `menu` returns `code, description, title`. Across 464 cached store responses there is no field named like expiry, end, or valid-until anywhere in the payload.

Without it an agent cannot tell a consumer "this BOGO ends tonight" or rank deals by urgency. It also cannot tell whether a cached promotion is still live without re-fetching the store, which costs calls on a rate-limited API (#94).

Request: add an `expires_at` (or `end_time`) field to promotion objects in `promo list` and in `menu.promotions[]`, null when the server has no end date.
