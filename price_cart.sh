#!/bin/bash
# price_cart.sh - deterministic DoorDash cart pricing via dd-cli.
#
#   price_cart.sh <spec.json>     (or: price_cart.sh - / spec on stdin)
#
# Spec:
#   {
#     "store_id": "36042509",
#     "items": [
#       { "item_id": "32877870578",
#         "item_name": "Spicy Chicken Sandwich",   # optional, display only
#         "menu_id": "82196752",
#         "quantity": 2,
#         "options": [ "<option_id>", {"id":"...","name":"...","quantity":1,
#                                      "options":[ ...nested... ]} ] }
#     ],
#     "promo": "auto" | "none" | "<campaign_id or promo code>"
#   }
#
# Emits one JSON object on stdout, then deletes the cart it created.
# NEVER submits or checks out. There is no code path here that can place an order.
#
# Exit codes: 0 ok | 1 failure (partial JSON emitted) | 2 store already has a cart
set -u
set -o pipefail

export PATH="$HOME/.local/bin:$PATH"

INTENT='Summary: Help the user find the best-value meal deals near their saved address.
user prompt/purpose: "i want it as a research tool that will help me find the absolute best deals for lunch and dinner for me"'

DELETE_TRIES=6      # cleanup passes before we give up
DELETE_SLEEP=3      # seconds between passes

STORE_ID=""
CART_UUID=""
CART_DELETED=false
EMITTED=false
WARNINGS='[]'
WORKDIR="$(mktemp -d)"

warn() { WARNINGS=$(jq -c --arg w "$1" '. + [$w]' <<<"$WARNINGS"); }
die()  { echo "price_cart: $*" >&2; exit 1; }

# ---------------------------------------------------------------- dd-cli wrapper
# All dd-cli output goes to a file; stderr is kept separate so a warning line
# cannot corrupt the JSON. --json-output is a GLOBAL flag and must precede the
# subcommand.
ddcli() {
  local out="$WORKDIR/out.json" rc
  dd-cli --json-output "$@" --intent "$INTENT" >"$out" 2>"$WORKDIR/err.txt"
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "dd-cli $1 ${2:-} failed (exit $rc): $(tr '\n' ' ' <"$WORKDIR/err.txt")" >&2
    return $rc
  fi
  if ! jq -e . "$out" >/dev/null 2>&1; then
    echo "dd-cli $1 ${2:-} returned non-JSON: $(head -c 300 "$out")" >&2
    return 1
  fi
  cat "$out"
}

# ------------------------------------------------------------------- cleanup
# The store was verified cart-free before we started, so ANY cart at this store
# now is one we created - safe to delete. Deletes sometimes "succeed" and then
# resurrect items into a fresh cart, so loop until cart list is actually empty.
cleanup_carts() {
  [ -n "$STORE_ID" ] || return 0
  local i listing uuids u
  for i in $(seq 1 $DELETE_TRIES); do
    listing=$(ddcli cart list --store-id "$STORE_ID" 2>/dev/null) || listing='{}'
    uuids=$(jq -r '.structuredContent.carts // [] | .[].cart_uuid' <<<"$listing")
    if [ -z "$uuids" ]; then
      CART_DELETED=true
      return 0
    fi
    for u in $uuids; do
      ddcli cart delete --cart-uuid "$u" >/dev/null 2>&1 || true
    done
    sleep $DELETE_SLEEP
  done
  CART_DELETED=false
  warn "cart cleanup did not reach zero carts at store $STORE_ID after $DELETE_TRIES passes"
  return 1
}

emit_partial() {
  jq -n --arg err "$1" --arg store "$STORE_ID" --arg cart "$CART_UUID" \
        --argjson deleted "$CART_DELETED" --argjson warnings "$WARNINGS" \
    '{error:$err, store_id:$store, cart_uuid:$cart,
      subtotal:null, discount:null, delivery_fee:null, service_fee:null,
      tax:null, total_before_tip:null, per_line:[], promo_title:null,
      cart_deleted:$deleted, warnings:$warnings}'
  EMITTED=true
}

on_exit() {
  local rc=$?
  if [ "$EMITTED" = false ]; then
    [ "$CART_DELETED" = false ] && [ -n "$CART_UUID" ] && cleanup_carts || true
    emit_partial "aborted with exit $rc"
  fi
  rm -rf "$WORKDIR"
  [ $rc -eq 0 ] || exit $rc
}
trap on_exit EXIT

# ---------------------------------------------------------------- read spec
if [ $# -ge 1 ] && [ "$1" != "-" ]; then
  [ -f "$1" ] || die "spec file not found: $1"
  SPEC=$(cat "$1")
else
  SPEC=$(cat)
fi
jq -e . >/dev/null 2>&1 <<<"$SPEC" || die "spec is not valid JSON"

STORE_ID=$(jq -r '.store_id // empty' <<<"$SPEC")
PROMO=$(jq -r '.promo // "auto"' <<<"$SPEC")
MENU_ID=$(jq -r '[.items[].menu_id] | map(select(. != null)) | unique | .[0] // empty' <<<"$SPEC")
[ -n "$STORE_ID" ] || die "spec.store_id is required"
[ -n "$MENU_ID" ]  || die "spec.items[].menu_id is required"
[ "$(jq '.items | length' <<<"$SPEC")" -gt 0 ] || die "spec.items must be non-empty"
[ "$(jq '[.items[].menu_id] | map(select(. != null)) | unique | length' <<<"$SPEC")" -le 1 ] \
  || die "all items must share one menu_id"

# Build --items-json. Options may be bare id strings or full objects; both are
# normalised to the {id,name,quantity,options[]} shape add-items expects.
ITEMS_JSON=$(jq -c '
  def norm_opts:
    map(
      if type == "string" then {id: ., name: ., quantity: 1}
      else
        {id: (.id|tostring), name: (.name // (.id|tostring)), quantity: (.quantity // 1)}
        + (if (.options // []) | length > 0 then {options: (.options | norm_opts)} else {} end)
      end
    );
  .items | map(
    {item_id: (.item_id|tostring),
     item_name: (.item_name // "Item"),
     quantity: (.quantity // 1)}
    + (if (.options // []) | length > 0 then {nested_options: (.options | norm_opts)} else {} end)
  )' <<<"$SPEC")

EXPECTED_QTY=$(jq '[.items[].quantity // 1] | add' <<<"$SPEC")

# ------------------------------------------------------- refuse if cart exists
EXISTING=$(ddcli cart list --store-id "$STORE_ID") || die "cart list failed"
N=$(jq -r '.structuredContent.carts // [] | length' <<<"$EXISTING")
if [ "$N" != "0" ]; then
  echo "price_cart: store $STORE_ID already has $N open cart(s) - refusing, creating one here would displace the user's cart:" >&2
  jq -r '.structuredContent.carts[] | "  \(.cart_uuid)  \(.store_name)  \(.items_count) item(s)"' <<<"$EXISTING" >&2
  EMITTED=true   # nothing was created, so nothing to clean up and nothing to print
  exit 2
fi

# ------------------------------------------------------------------ build cart
ADD=$(ddcli cart add-items --store-id "$STORE_ID" --menu-id "$MENU_ID" --items-json "$ITEMS_JSON") \
  || die "cart add-items failed"
CART_UUID=$(jq -r '.structuredContent.cart_uuid // empty' <<<"$ADD")
[ -n "$CART_UUID" ] || die "cart add-items returned no cart_uuid: $(jq -c '.structuredContent' <<<"$ADD")"
if [ "$(jq -r '.structuredContent.success' <<<"$ADD")" != "true" ]; then
  die "cart add-items reported failure: $(jq -c '.structuredContent.item_errors // .structuredContent' <<<"$ADD")"
fi

preview() {
  ddcli order preview --cart-uuid "$CART_UUID" --fulfillment delivery
}

PREVIEW=$(preview) || die "order preview failed"

# ---------------------------------------------------------------- promo policy
discount_cents() {
  jq -r '.structuredContent.quote.line_items[]? | select(.charge_id=="PROMOTION_DISCOUNT")
         | .final_money.unit_amount' <<<"$1" | head -1
}
applied_promos() {   # code<TAB>campaign<TAB>adgroup<TAB>ad
  jq -r '.structuredContent.quote.promotions[]? | [.code, .campaign_id, .ad_group_id, .ad_id] | @tsv' <<<"$1"
}

# Re-read the cart: `promo apply` has been seen to silently duplicate line items.
check_cart_quantity() {
  local got
  got=$(ddcli cart show --cart-uuid "$CART_UUID" 2>/dev/null \
        | jq -r '[.structuredContent.cart.items[]?.quantity] | add // 0')
  if [ -n "$got" ] && [ "${got%.*}" != "$EXPECTED_QTY" ]; then
    warn "cart quantity changed after promo apply: expected $EXPECTED_QTY, cart holds $got (known dd-cli duplication bug)"
  fi
}

apply_promo_row() {  # $1..$4 = code campaign ad_group ad
  local args=(promo apply --cart-uuid "$CART_UUID" --promo-code "$1")
  [ -n "${2:-}" ] && [ "$2" != "null" ] && args+=(--campaign-id "$2")
  [ -n "${3:-}" ] && [ "$3" != "null" ] && args+=(--ad-group-id "$3")
  [ -n "${4:-}" ] && [ "$4" != "null" ] && args+=(--ad-id "$4")
  # The apply response is unreliable in both directions - ignore it entirely and
  # read the truth off the next preview.
  ddcli "${args[@]}" >/dev/null 2>&1 || true
  check_cart_quantity
  PREVIEW=$(preview) || die "order preview failed after promo apply"
}

case "$PROMO" in
  none)
    if [ "$(discount_cents "$PREVIEW")" != "" ] && [ "$(discount_cents "$PREVIEW")" != "0" ]; then
      while IFS=$'\t' read -r code camp adg adid; do
        [ -n "$code" ] || continue
        args=(promo remove --cart-uuid "$CART_UUID" --promo-code "$code")
        [ -n "$camp" ] && [ "$camp" != "null" ] && args+=(--campaign-id "$camp")
        [ -n "$adg" ]  && [ "$adg"  != "null" ] && args+=(--ad-group-id "$adg")
        [ -n "$adid" ] && [ "$adid" != "null" ] && args+=(--ad-id "$adid")
        ddcli "${args[@]}" >/dev/null 2>&1 || true
      done < <(applied_promos "$PREVIEW")
      PREVIEW=$(preview) || die "order preview failed after promo remove"
      d=$(discount_cents "$PREVIEW")
      if [ -n "$d" ] && [ "$d" != "0" ]; then
        warn "promo=none but a discount is still attached; wallet promos auto-redeem and cannot always be removed"
      fi
    fi
    ;;
  auto)
    d=$(discount_cents "$PREVIEW")
    if [ -z "$d" ] || [ "$d" = "0" ]; then
      row=$(ddcli promo list --store-id "$STORE_ID" 2>/dev/null \
            | jq -r '.structuredContent.promotions[]? | [.code, .campaign_id, .ad_group_id, .ad_id] | @tsv' | head -1)
      if [ -n "$row" ]; then
        IFS=$'\t' read -r code camp adg adid <<<"$row"
        apply_promo_row "$code" "$camp" "$adg" "$adid"
      fi
    fi
    ;;
  *)
    row=$(ddcli promo list --store-id "$STORE_ID" 2>/dev/null \
          | jq -r --arg p "$PROMO" '.structuredContent.promotions[]?
                 | select(.code == $p or .campaign_id == $p)
                 | [.code, .campaign_id, .ad_group_id, .ad_id] | @tsv' | head -1)
    if [ -z "$row" ]; then
      warn "promo '$PROMO' not found in promo list for store $STORE_ID; applying it as a literal promo code"
      apply_promo_row "$PROMO" "" "" ""
    else
      IFS=$'\t' read -r code camp adg adid <<<"$row"
      apply_promo_row "$code" "$camp" "$adg" "$adid"
    fi
    ;;
esac

# ---------------------------------------------------------------- parse quote
# final_money.unit_amount is UNSIGNED (PROMOTION_DISCOUNT comes back positive),
# so read display_string, which carries the real sign, and strip $ and commas.
RESULT=$(jq -c \
  --arg store "$STORE_ID" --arg cart "$CART_UUID" --argjson warnings "$WARNINGS" '
  def money: if . == null then null else (.display_string | gsub("[$,]"; "")) end;
  .structuredContent.quote as $q
  | ($q.line_items // []) as $li
  | {
      store_id: $store,
      cart_uuid: $cart,
      subtotal:         ([$li[] | select(.charge_id=="SUBTOTAL")           | .final_money] | first | money),
      discount:         ([$li[] | select(.charge_id=="PROMOTION_DISCOUNT") | .final_money] | first | money),
      delivery_fee:     ([$li[] | select(.charge_id=="DELIVERY_FEE")       | .final_money] | first | money),
      service_fee:      ([$li[] | select(.charge_id=="SERVICE_FEE")        | .final_money] | first | money),
      tax:              ([$li[] | select(.charge_id=="TAX")                | .final_money] | first | money),
      total_before_tip: ($q.total_before_tip | money),
      per_line: [ $q.store_order_cart.orders[]?.order_items[]?
                  | {name: .item.name,
                     quantity: .quantity,
                     line_total: (.unit_price_monetary_fields | money)} ],
      promo_title: ([$q.promotions[]?.title] | first),
      is_dashpass_applied: $q.is_dashpass_applied,
      warnings: $warnings
    }' <<<"$PREVIEW") || die "could not parse preview quote"

# ------------------------------------------------------------------- teardown
cleanup_carts || true
RESULT=$(jq -c --argjson deleted "$CART_DELETED" --argjson warnings "$WARNINGS" \
         '.cart_deleted = $deleted | .warnings = $warnings' <<<"$RESULT")

echo "$RESULT"
EMITTED=true

[ "$CART_DELETED" = true ] || exit 1
exit 0
