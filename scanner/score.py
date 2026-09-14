#!/usr/bin/env python3
"""Score every scanned store for cheapest real meal after promos.

Inputs (all TSV, in this directory unless overridden):
  items.tsv                store_id item_id name category price orderable req_mods
  promos.tsv               store_id campaign_id title description source
  stores.tsv               store_id name distance_km
  calibration/previews.tsv store_id fulfillment subtotal discount service_fee
                           delivery_fee tax pretip_total
  calibration/promo_confirmed.tsv
                           campaign_id store_id confirmed_date status evidence
                           (status: confirmed | refuted; anything absent from
                           this file is UNCONFIRMED)
  excluded_stores.tsv      store_id name reason - stores the user has
                           permanently ruled out. Dropped before ranking, from
                           BOTH lanes. This is the authoritative exclusion:
                           daily_scan.sh's fetch-time skip only stops new data
                           arriving, it cannot remove menu/promo JSON already
                           cached in state/scan from before the ruling.

Outputs:
  ranked.tsv            best candidate per store, sorted by per-meal low bound
  ranked_pickup.tsv     the same candidates re-priced as PICKUP orders, for the
                        stores within PICKUP_MAX_KM (e-bike range). Same column
                        schema as ranked.tsv plus two trailing columns,
                        fulfillment (always "pickup") and delivery_rank_per_meal
                        (the same store+item's delivery ranking number, so the
                        saving is readable without joining the two files).
  verify_list.tsv       top 12 delivery candidates whose RANKING bound beats the
                        best verified real total ($11.32), plus up to 6 pickup
                        candidates that clear the same bar - these need a real
                        checkout preview. Carries one extra trailing column
                        beyond the ranked.tsv schema, fulfillment.
  unparsed_promos.tsv   promo titles the parser could not turn into math

PICKUP LANE
  A pickup order pays no service fee and no delivery fee, and is taxed on the
  food alone, so for a store inside e-bike range it can beat the same cart
  delivered by several dollars. Those numbers are ESTIMATES and nothing else:
  many DoorDash promos are delivery-only and simply fail to attach on a pickup
  cart, with no error, so a real pickup preview can come back HIGHER than the
  estimate here. The verification stage settles it - see the fulfillment column
  of verify_list.tsv.

  The one case that is not left to the preview is a promo whose own description
  says "delivery only". Promo already parses that out; on the pickup lane its
  discount is dropped rather than pretended into the total. Everything else
  about the promo math is identical between the two lanes.

Every candidate cart must also keep its predicted pre-tip ORDER total at or under
ORDER_CEILING ($20.00). For a store whose fees are not pinned by a real preview
the HIGH bound is what has to fit, so the ceiling holds even in the worst fee
case. Per-meal cost is still the ranking metric, but it only ranks carts that
already clear the ceiling.

Five rulings from deal-hunt-prompt.md and dd-cli-issues.md are enforced here:

  MEAL WEIGHT (deal-hunt-prompt.md, MEAL-COUNT RULES, user ruling 2026-08-24)
      Ranking is per MEAL EQUIVALENT, not per menu item. Every item carries a
      weight - a proper entree is 1.0, an appetizer or small plate or side soup
      is 0.4-0.5, a drink or dessert is 0.0 - and a cart ranks only once its
      quantities add up to a whole meal. This is what stops an $8.42 order of
      3pc pork buns being reported as an $8.42 lunch. Entree-sized noodle soups
      (pho, ramen, udon) are entrees; a bisque is not. See meal_weight().

  PORTION (deal-hunt-prompt.md, MEAL-COUNT RULES)
      A small protein portion with no side (3-4 tenders, a few wings, nuggets)
      is not a meal and is dropped unless the name or its menu section carries a
      side/combo word. A sushi roll counts as such a portion under the same
      ruling that rolls are not meals, and so does the hot-dog family (hot dog,
      brat, bare sausage, slider) - a protein in a bun with nothing beside it is
      a light item. Items labelled mini/small/junior stay in but are flagged for
      a human look.

  PREFERENCE (deal-hunt-prompt.md, Food preference rules)
      A vegan/veggie variant of a normally-meat item is never the ranked
      candidate. The standard variant is priced instead; when the diet variant
      is genuinely the cheaper deal it rides along in the alt_variant_* columns
      so both prices are visible. Items far outside the user's meat-forward
      taste profile are flagged and sorted below on-profile candidates.

  PROMO REALISM (dd-cli-issues.md defect 18)
      promo list calls wallet promos "eligible" that promo apply then refuses.
      So a promo only moves the predicted total if calibration/promo_confirmed
      .tsv records a real preview where it applied. An unconfirmed promo is
      priced as UPSIDE only (upside_* columns); a refuted one is dropped.

  MODIFIER MINIMUMS (dd-cli-issues.md defect 19)
      menu price is a base price. For req_mods=1 items the true minimum
      orderable price is unknown and can be far higher, so the base price is
      treated strictly as a LOWER bound: the high bound is widened by
      REQ_MOD_UPLIFT and such a candidate is ranked on its HIGH bound, never on
      a base price that may not be orderable at all.

Usage:
  python3 score.py                 # score, write outputs
  python3 score.py --selftest      # fit fee model, check it against calibration
  python3 score.py --coverage      # list stores needing a menu refetch
  python3 score.py --dir FIXDIR    # run against another data directory

Python 3 stdlib only.
"""

import argparse
import math
import os
import re
import sys

# ---------------------------------------------------------------- constants

BEST_VERIFIED_TOTAL = 11.32   # store 25060900 delivery, real preview
SERVICE_FLOOR = 0.99          # observed minimum service fee above the threshold
SERVICE_HIGH = 2.99           # observed service fee below the threshold

# DashPass free-delivery threshold, tested against the PRE-discount subtotal.
# This is a platform policy, not a store attribute, which is why it is a
# constant rather than something fitted: it is the same $12 for every store, so
# a leave-one-out refit that "forgets" a store does not forget the threshold.
# Verified against every delivery row in scanner/calibration/previews.tsv:
#   subtotal >= 12.00 -> delivery_fee 0.00 on 75/75 rows, and
#                        service_fee == max(0.99, service_rate * subtotal) on 75/75
#   subtotal <  12.00 -> delivery_fee 0.49-3.99 (store specific) on 50/50 rows,
#                        service_fee 2.99 on 49/50 (store 37487521 charges 3.12)
# The split is on subtotal alone. Testing subtotal+discount instead breaks it on
# 20 rows, so the discount does NOT count toward the threshold.
DASHPASS_MIN_SUBTOTAL = 12.00
MEAL_MIN_PRICE = 7.00
MEAL_MAX_PRICE = 20.00
VERIFY_N = 12
ORDER_CEILING = 20.00         # hard cap on the predicted pre-tip ORDER total

# Pickup lane. The user is at the anchor address in Madison WI and rides an e-bike,
# so a store within this radius is a realistic pickup, not a walk that costs more
# in time than it saves in fees.
PICKUP_MAX_KM = 4.0
PICKUP_VERIFY_N = 6           # pickup rows appended to verify_list.tsv
# The bar the leave-one-out check has to clear. It is an INTERVAL hit rate, so
# 100% is the right target: the [low, high] band is sold as a bracket, and a
# bracket that misses is not a bracket. Widening the band to buy a pass is not
# free - the same band is what the order ceiling and the ranking are tested on.
LOO_TARGET = 100.0

# defect 19: the widest base-price-to-true-minimum gap actually observed is
# Spaghetti & Meatballs at store 379439, menu $7.15 vs cheapest required "Pick
# Your Size" option $11.95 = 1.671x. Until a menu call exposes required-option
# prices this is the honest worst case for any req_mods=1 item.
REQ_MOD_UPLIFT = 11.95 / 7.15

# Words that mean "this is not a whole meal". Each is matched case-insensitively
# at a word start (so "bread" also catches "Breads" but not "shortbread"), against
# the category name and - unless listed in CATEGORY_ONLY - the item name too.
# CATEGORY_ONLY words are ones that appear innocently inside real meal names
# ("2-Topping Pizza", "Breaded Chicken"), so only the menu section may use them.
NOT_A_MEAL = {
    "drinks": ["drink", "beverage", "soda", "coffee", "latte", "espresso",
               "tea", "juice", "smoothie", "lemonade", "water", "shake", "milk",
               "coke", "pepsi", "sprite", "bottled", "brew", "chai", "matcha",
               "mocha", "cappuccino", "americano", "refresher", "frappe",
               "kombucha", "cider", "seltzer", "boba",
               # a Mango Lassi was ranking as a $7.99 lunch on 2026-08-24: no
               # drink word appears in either its name or its "Lassi" section
               "lassi", "horchata", "agua fresca", "milkshake", "mocktail"],
    "sides":  ["side", "fries", "appetizer", "starter", "chips", "small plate",
               "bread", "naan", "roti", "paratha", "topperstix", "breadstick",
               "soup", "toast", "bagel", "pretzel",
               # plain starch on its own is a side, not a meal
               "white rice", "steamed rice", "brown rice", "plain rice",
               # authoritative meal-count rule: rolls and dim sum are not meals
               "spring roll", "veggie roll", "egg roll", "dim sum",
               # plain bread on its own is a side, not a meal (user ruling
               # 2026-08-18: "i could just buy a loaf of bread")
               "pita bread"],
    "desserts": ["dessert", "cake", "cookie", "brownie", "ice cream", "gelato",
                 "custard",
                 # "cake" cannot match "Cupcake" - there is no word boundary in
                 # the middle of it - which is how "Dave's Double Chocolate Chip
                 # Cupcake" reached ranked.tsv as a meal on 2026-08-24
                 "cupcake", "cheesecake", "donut", "pastr", "bakery", "sweets",
                 "cinnamon", "churro", "pudding", "croissant", "muffin",
                 "scone", "macaron", "tiramisu", "mochi", "baklava", "flan"],
    "sauces": ["sauce", "dip", "dipping", "dressing", "condiment", "salsa",
               "syrup", "seasoning"],
    "kids":   ["kids", "kid's", "children", "happy meal"],
    "extras": ["extra", "add-on", "add on", "topping", "pantry", "merch",
               "gift card", "utensil", "catering", "party pack", "family meal",
               "combo pack",
               # a bulk multi-pack is never ONE meal, whatever it contains
               # (user ruling 2026-08-18, from Naf Naf "Six Pack Pitas").
               # Both spellings: menus write "6 Pack" and "6-Pack".
               "six pack", "six-pack", "6 pack", "6-pack",
               "ten pack", "ten-pack", "10 pack", "10-pack",
               "twelve pack", "twelve-pack", "12 pack", "12-pack",
               "pack of"],
}
CATEGORY_ONLY = {"topping", "extra", "pantry", "bakery", "side", "soup", "dip"}
# guards that stop a keyword from firing on an unrelated longer word
SUFFIX_GUARD = {"bread": r"(?!ed)"}   # "Breaded Chicken" is a meal, "Breads" is not
_KEYWORD_RE = {
    bucket: [(w, re.compile(r"\b" + re.escape(w) + SUFFIX_GUARD.get(w, ""), re.I))
             for w in words]
    for bucket, words in NOT_A_MEAL.items()
}

PIZZA_RE = re.compile(r'(\d{2})\s*(?:"|”|″|-?\s*inch|-?\s*in\b)', re.I)

# ---- PORTION rule ---------------------------------------------------------
# A bare protein portion is not a meal. It becomes one when the name says a
# side, a starch or a combo comes with it, so the portion words only disqualify
# an item when no MEAL_MAKER word appears in the same name.
# "roll" is here on the authoritative ruling that rolls are not meals; a single
# sushi/rice roll is the same shape of thing - a small portion with no side.
#
# The hot-dog family ("hot dog", "dog", "brat", "sausage", ...) is here on the
# same ruling as the tenders one in deal-hunt-prompt.md: a bare protein in a bun
# with nothing beside it is a light item, not a lunch. A live run ranked MOOYAH's
# "Build Your Own All-Beef Hot Dog" as a meal while the report itself said it was
# "not really a full meal", so the rule was being applied in prose but not in
# code. A hot dog that names a side or a combo is still rescued by MEAL_MAKER.
#
# Some of these words also appear inside real meals, so they carry a trailing
# guard in PORTION_GUARD: "brat" must not fire on the "Brathaus Originals" menu
# section, and "dog" must not fire mid-word.
PORTION_WORDS = ["tender", "strip", "nugget", "popcorn chicken", "wing",
                 "drumstick", "meatball", "slider", "taquito", "mozzarella stick",
                 "shrimp basket", "chicken bites", "boneless", "roll",
                 "hot dog", "hotdog", "corn dog", "dog", "brat", "bratwurst",
                 "sausage", "kielbasa", "wiener", "hot link"]
# Trailing guards, applied after the word. Default is no trailing boundary (so
# "tender" still catches "tenders"); these need one to stay off longer words.
PORTION_GUARD = {"dog": r"s?\b", "brat": r"s?\b", "sausage": r"s?\b",
                 "hot dog": r"s?\b", "hotdog": r"s?\b", "corn dog": r"s?\b",
                 "kielbasa": r"s?\b", "wiener": r"s?\b", "hot link": r"s?\b",
                 "bratwurst": r"s?\b"}
# "pizza", "noodle", "soba", "chazuke", "burrito", "gyro", "pasta" and "burger"
# are meal shapes in their own right. They matter now because the hot-dog family
# added "sausage", an ingredient word that otherwise drags a sausage pizza and a
# sausage noodle bowl down with it. They also fix three items the old list was
# already dropping wrongly: Spaghetti with Meatballs and two meatball Pho bowls.
MEAL_MAKER = ["combo", "meal", "plate", "platter", "dinner", "box", "with fries",
              "and fries", "w/ fries", "with side", "and side", "fried rice",
              "with rice", "over rice", "rice bowl", "rice plate", "bowl",
              "sandwich", "wrap", "sub", "basket", "feast", "entree", "bento",
              "pizza", "noodle", "soba", "chazuke", "burrito", "gyro", "pasta",
              "burger"]

# MEAL-COUNT ruling: a BOGO pair of fast-food-size sandwiches is ONE meal,
# because both get eaten in one sitting. Whether a given 2-sandwich cart is one
# meal or two is exactly the "genuinely unsure" case the rules say to report
# BOTH ways, so such a cart carries a meal count band instead of one number.
# Menu price is the only size proxy in the data, and it is a blunt one: the
# Popeyes sandwiches this ruling is written about run $7.39-9.59, while the
# Mediterranean Cafe Chicken Sandwich (a full plate-size meal) is $10.00. The
# cut sits between them, so anything at or under $9.99 counts as fast-food-size.
FASTFOOD_SANDWICH_MAX = 9.99
FASTFOOD_WORDS = ["sandwich", "burger", "cheeseburger", "slider", "hot dog"]
_FASTFOOD_RE = None            # built below, after _word_re exists
# Size words that do not disqualify anything but do need a human eyeball.
SIZE_FLAG_WORDS = ["mini", "small", "junior", "jr.", "jr ", "petite", "lil",
                   "little", "half", "snack", "slim", "single"]

# ---- PREFERENCE rules -----------------------------------------------------
# Explicit dietary markers only. A niche variant of a normally-meat item is
# never the ranked pick; the standard variant is priced instead.
DIET_VARIANT_WORDS = ["vegan", "vegetarian", "veggie", "plant-based",
                      "plant based", "impossible", "beyond meat", "beyond burger",
                      "meatless", "tofu", "no meat"]
# Taste anchors read off the user's order history in deal-hunt-prompt.md:
# gyro/shawarma bowls and plates, butter chicken, chicken sandwiches, pizza,
# chicken bowls - meat-forward mainstream meals.
TASTE_ANCHORS = ["gyro", "shawarma", "kebab", "kabob", "kabab", "falafel plate",
                 "butter chicken", "tikka", "masala", "curry", "biryani",
                 "korma", "vindaloo", "saag", "chicken", "beef", "steak", "lamb",
                 "pork", "bacon", "turkey", "sausage", "gholame", "pizza",
                 "burger", "sandwich", "sub", "wrap", "burrito", "taco",
                 "quesadilla", "bowl", "plate", "platter", "sushi", "roll",
                 "ramen", "pho", "noodle", "pad thai", "fried rice", "pasta",
                 "sushi", "salmon", "shrimp", "fish", "gyros"]
# Things the prompt names as outside the profile - juice-bar oatmeal and
# dessert-adjacent items - plus close neighbours of them.
OFF_PROFILE_WORDS = ["oatmeal", "acai", "açai", "granola", "parfait", "yogurt",
                     "chia", "overnight oats", "protein shake", "juice",
                     "smoothie", "cold pressed", "wellness", "detox"]

# ---- MEAL WEIGHT: fractional meal counting --------------------------------
# USER RULING 2026-08-24 (authoritative). The scanner used to count one menu
# item as one meal, so a $8.42 order of "Chinese BBQ Pork Buns (3pcs)" ranked as
# an $8.42 lunch. An appetizer is not lunch. Every item now carries a MEAL
# WEIGHT, and a cart is ranked on cost per meal EQUIVALENT:
#
#   1.0   a proper entree or equivalent - burger, sandwich, pho bowl, entree
#         plate. A sandwich ALONE counts as a meal.
#   0.5   appetizer, small plate, side, non-entree soup, salad-as-a-side. Soup
#         alone, salad alone or a small plate alone is NOT a meal, but
#         quantities add: 2 x (3pc pork buns) is about one real meal.
#   0.4   a distinctly tiny portion - a dip, a couple of dumplings, one skewer.
#         Three of these make a lunch, not two.
#   0.0   drinks, desserts, sauces. Never food-for-lunch at any quantity.
#
# A cart ranks only if its meal equivalents reach MEAL_EQUIV_MIN.
#
# HOW THIS COMPOSES with the older MEAL-COUNT RULES above (BOGO pairs, large
# pizzas): those rules turn ONE item into MORE than one meal, these weights turn
# one item into LESS than one. An item is never both, so the multi-meal rules
# are applied only to weight-1.0 items - see meal_equiv_band().
MEAL_WEIGHT_ENTREE = 1.0
MEAL_WEIGHT_SMALL = 0.5
MEAL_WEIGHT_TINY = 0.4
MEAL_WEIGHT_NONE = 0.0
MEAL_EQUIV_MIN = 1.0          # a cart below this is not a lunch, so it is not ranked

# ENTREE-SOUP EXCEPTION (user ruling): an entree-sized noodle soup IS an entree.
# Pho, ramen, udon, soba and big noodle soups are 1.0; a bisque or a cup/bowl of
# side soup is not. Checked FIRST, against name and menu section both, because
# the word "soup" would otherwise drag a $19.50 Pho Dac Biet down to a side.
# Tuned against the 2026-08-24 items.tsv, where the Ramen and Udon menu sections
# carry item names that are bare proteins ("Beef", "Chicken") - so the section
# name is the only evidence those are entrees.
ENTREE_SOUP_WORDS = ["pho", "phở", "ramen", "udon", "soba", "noodle soup",
                     "rice noodle soup", "vermicelli soup", "laksa", "khao soi",
                     "bun bo", "bun cha", "bun rieu", "bun thit", "hu tieu",
                     "mi quang", "beef noodle", "wonton noodle", "pozole",
                     "menudo", "birria soup", "kalguksu", "jjamppong"]

# STRUCTURAL entree words: they describe the SERVING FORM, which is what makes
# something lunch. A form word in the ITEM NAME outranks any small-plate word in
# the same name ("Chicken Wing Plate" is a plate, not wings), which keeps this
# consistent with the PORTION rule's MEAL_MAKER rescue above.
ENTREE_FORM_WORDS = ["plate", "platter", "combo", "combination", "dinner",
                     "meal", "entree", "entrée", "bento", "box lunch", "bowl",
                     "sandwich", "burger", "cheeseburger", "wrap", "burrito",
                     "sub", "hoagie", "grinder", "panini", "torta", "gyro",
                     "feast", "with rice", "over rice", "with fries",
                     "and fries", "w/ fries", "with side", "and side",
                     "lunch special", "dinner special", "blue plate"]

# ENTREE DISH words: named dishes that are a lunch on their own. Read off the
# 2026-08-24 items.tsv survivor set, which is heavy on Chinese-American,
# Vietnamese, Mexican, Indian, Japanese and deli menus.
ENTREE_DISH_WORDS = ["pizza", "calzone", "stromboli", "pasta", "spaghetti",
                     "lasagna", "fettuccine", "penne", "ravioli", "alfredo",
                     "parmigiana", "parmesan", "cheesesteak", "philly",
                     "lo mein", "chow mein", "chow fun", "mei fun", "pad thai",
                     "pad see ew", "drunken noodle", "fried rice", "egg foo young",
                     "teriyaki", "hibachi", "donburi", "katsu", "curry", "curries",
                     "masala", "tikka", "biryani", "korma", "vindaloo", "saag",
                     "tandoori", "shawarma", "kebab", "kabob", "kabab", "kofta",
                     "enchilada", "chimichanga", "quesadilla", "fajita", "tostada",
                     "nachos", "chile relleno", "carnitas", "barbacoa", "birria",
                     "mac & cheese", "mac and cheese", "macaroni", "meatloaf",
                     "pot pie", "stir fry", "stir-fry", "fried chicken",
                     "orange chicken", "sesame chicken", "general tso",
                     "kung pao", "mongolian", "szechuan", "sweet and sour",
                     "moo shu", "egg foo", "poke", "chirashi", "unagi don",
                     "banh mi", "cheesesteak", "reuben", "club", "blt",
                     "steak frites", "schnitzel", "shepherd", "jambalaya",
                     "etouffee", "gumbo dinner", "burrito bowl", "rice plate",
                     # breakfast and brunch plates: the 2026-08-24 data has a
                     # whole block of these sitting in sections named only
                     # "BREAKFAST" or "Weekend Brunch", so the dish name is the
                     # evidence. A brunch plate is a full meal.
                     "huevos", "chilaquiles", "biscuits & gravy",
                     "biscuits and gravy", "chicken & waffle",
                     # "chicken & waffle" and not a bare "waffle": a pandan or
                     # taro waffle is a sweet snack, and bare "waffle" promoted
                     # exactly those to a full meal on the 2026-08-24 run
                     "chicken and waffle", "omelet", "omelette",
                     "benedict", "huarache", "scramble", "hash"]

# ENTREE SALADS. The ruling puts "salads-as-sides" at 0.4-0.5 but a salad built
# on a protein is lunch. Rather than leave the whole Salads section to the
# uncertain default - 52 items on 2026-08-24, the single biggest block of it -
# the protein decides: "Mediterranean Chicken Salad" is a meal, "Kale Caesar"
# and "Classic Garden" are not.
SALAD_WORDS = ["salad", "salads"]
SALAD_PROTEIN_WORDS = ["chicken", "steak", "beef", "salmon", "shrimp", "tuna",
                       "turkey", "bacon", "chorizo", "carnitas", "cobb",
                       "chef salad", "gyro", "shawarma", "falafel", "ahi"]

# SUB-MEAL words in the ITEM NAME. These are the ones that actually rescued the
# 2026-08-24 top-5 problem: the offending items sat in menu sections with no
# meal signal at all ("EVERY DAY SPECIALS", "Sharing", "Deli Menu",
# "Buns & Pancake"), so the NAME is the only evidence available.
SUB_MEAL_NAME_WORDS = {
    MEAL_WEIGHT_SMALL: [
        # non-entree soups: a bisque or a chowder is not lunch (the entree-soup
        # exception above has already claimed pho/ramen/udon by this point)
        "bisque", "chowder", "consomme", "consommé", "miso soup", "egg drop",
        "hot and sour", "hot & sour", "wonton soup", "tom yum", "tom kha",
        # spelled "Avgolemon Chicken Rice Soup" in the 2026-08-24 data, so the
        # keyword stops before the final vowel to catch both spellings
        "avgolemon", "gazpacho", "borscht", "minestrone", "broth",
        # raw bar / cold appetizers
        "ceviche", "aguachile", "carpaccio", "tartare", "crudo", "poke nachos",
        "oysters", "shrimp cocktail", "cocktail de camaron",
        # dim sum, buns and dumpling plates
        "bun", "buns", "bao", "steamed bun", "pancake", "dumpling", "potsticker",
        "pot sticker", "gyoza", "shumai", "siu mai", "har gow", "wonton",
        "empanada", "samosa", "pakora", "spanakopita", "arepa", "pupusa",
        # small plates and shares
        "brussel", "brussels sprout", "edamame", "hummus", "baba ganoush",
        "guacamole", "queso", "nacho chips", "elote", "esquites",
        "calamari", "crab rangoon", "rangoon", "jalapeno popper",
        "mozzarella", "onion ring", "tater tot", "hush puppy", "fried pickle",
        "garlic knot", "satay", "skewer", "brochette", "anticucho",
        "tostones", "maduros", "yuca", "plantain", "croqueta", "falafel bites",
        # deli / butcher counter portions - a quarter leg is a side of protein,
        # not a plate. "leg" alone is too broad ("Braised Pork Leg with White
        # Rice" is an entree), so the quarter qualifier is required.
        "qtr leg", "qtr. leg", "quarter leg", "half chicken", "chicken half",
        "sausage link", "kielbasa link",
        # sushi a la carte: nigiri and sashimi by the piece are not a meal
        "nigiri", "sashimi", "hand roll", "temaki",
        # sushi-bar "salads" are cold appetizers, whatever protein is in the
        # name. Listed here so they are settled BEFORE the entree-salad rule
        # below promotes them on the strength of the word "salmon".
        "salmon skin", "seaweed salad", "wakame", "kani salad", "sunomono",
        "squid salad", "octopus salad",
        # sides that reached the survivor set with no side word in the section
        "coleslaw", "cole slaw", "potato salad", "mashed potato", "mac salad",
        "side salad", "garden salad", "caesar salad", "greek salad",
        "house salad", "green salad",
    ],
    MEAL_WEIGHT_TINY: [
        # a dip, a couple of pieces, a single skewer: three of these make lunch
        "dip", "salsa", "chips and", "chips &", "queso dip", "single skewer",
        "1 pc", "1pc", "2 pc", "2pc", "2 pcs", "2pcs", "one piece", "two piece",
        "mini taco", "street taco", "single taco", "slice of",
    ],
}

# SUB-MEAL words in the MENU SECTION. Weaker evidence than the name, so these
# are consulted only after the name has had its say. Most obvious ones
# ("Appetizers", "Sides", "Soup") are already dropped outright by NOT_A_MEAL;
# these are the sections that survive it and still are not lunch.
SUB_MEAL_CATEGORY_WORDS = ["sharing", "shareable", "share plate", "small plate",
                           "tapas", "dim sum", "bites", "nibble", "finger food",
                           "a la carte", "à la carte", "raw bar", "cold bar",
                           "sashimi", "nigiri", "antojito", "botana", "mezze",
                           "meze", "banchan", "sampler", "for the table",
                           "deli menu", "deli counter", "butcher", "by the pound",
                           "half pound", "whole pound", "crepes & waffles",
                           "buns & pancake", "che",
                           # these sections are already dropped outright by
                           # NOT_A_MEAL, so listing them changes no ranking. They
                           # are here so meal_weight() is honest when called on
                           # its own, e.g. by a spot check or the verifier.
                           "appetiz", "starter", "side", "snack", "soup"]

# STRUCTURAL entree words in the MENU SECTION. Last positive signal before the
# uncertain default.
ENTREE_CATEGORY_WORDS = ["entree", "entrée", "entrees", "entrées", "main",
                         "dinner", "lunch", "plate", "platter", "combo",
                         "specialt", "chef's special", "house special",
                         "sandwich", "burger", "pizza", "pasta", "noodle",
                         "rice", "curry", "curries", "taco", "burrito",
                         "teriyaki", "hibachi", "donburi", "bowl", "wrap",
                         "steak", "carne", "pollo", "favorites", "classics",
                         "traditional dish", "entree salad",
                         # A menu section named for a bare protein is that
                         # menu's section of protein ENTREES - "Beef Curries",
                         # "Chicken", "Seafood / 海鮮" on the 2026-08-24 Chinese
                         # and Indian menus all hold full dishes. The genuinely
                         # small ones ("Seafood Boil Half Pound") are caught by
                         # the sub-meal section words above, which run first.
                         "breakfast", "brunch", "benn", "cuisine", "seafood",
                         "beef", "pork", "chicken", "lamb", "vegetarian",
                         "vegetable", "duos", "de la casa", "traditional"]


def _word_re(words, guards=None):
    guards = guards or {}
    return [(w, re.compile(r"\b" + re.escape(w) + guards.get(w, ""), re.I))
            for w in words]


_PORTION_RE = _word_re(PORTION_WORDS, PORTION_GUARD)
_MEAL_MAKER_RE = _word_re(MEAL_MAKER)
_SIZE_FLAG_RE = _word_re(SIZE_FLAG_WORDS)
_DIET_RE = _word_re(DIET_VARIANT_WORDS)
_ANCHOR_RE = _word_re(TASTE_ANCHORS)
_OFF_PROFILE_RE = _word_re(OFF_PROFILE_WORDS)
_FASTFOOD_RE = _word_re(FASTFOOD_WORDS)

_ENTREE_SOUP_RE = _word_re(ENTREE_SOUP_WORDS)
_ENTREE_FORM_RE = _word_re(ENTREE_FORM_WORDS)
_ENTREE_DISH_RE = _word_re(ENTREE_DISH_WORDS)
_SUB_MEAL_NAME_RE = {w: _word_re(words)
                     for w, words in SUB_MEAL_NAME_WORDS.items()}
_SUB_MEAL_CAT_RE = _word_re(SUB_MEAL_CATEGORY_WORDS)
_ENTREE_CAT_RE = _word_re(ENTREE_CATEGORY_WORDS)
_SALAD_RE = _word_re(SALAD_WORDS)
_SALAD_PROTEIN_RE = _word_re(SALAD_PROTEIN_WORDS)


def _hit(rxs, text):
    for word, rx in rxs:
        if rx.search(text):
            return word
    return None


def portion_only(name, category=""):
    """PORTION rule: bare protein portion, no side or combo named -> not a meal.

    The portion word may sit in either the item name or the menu section, because
    a menu says "Rolls" or "Chicken & Wings" once at the top and then lists bare
    item names under it ("CALIFORNIA", "Loaded Chicken - Crispy Bacon"). Reading
    the name alone lets exactly those through, which is how a sushi roll - an
    item the authoritative rules call not-a-meal - reached the ranked list.
    A MEAL_MAKER word in either field still rescues the item, so a "Sandwiches"
    or "Rice Bowl" section is never touched.

    Returns the portion word that disqualified the item, or None.
    """
    word = _hit(_PORTION_RE, name) or _hit(_PORTION_RE, category)
    if word is None:
        return None
    if _hit(_MEAL_MAKER_RE, name) or _hit(_MEAL_MAKER_RE, category):
        return None
    return word


def size_flag(name, category=""):
    """Mini/small-labelled item: keep it, but say so.

    Same reasoning as portion_only - a "Mini Bowls" section labels its items as
    surely as the item names do.
    """
    return _hit(_SIZE_FLAG_RE, name) or _hit(_SIZE_FLAG_RE, category)


def diet_variant(name, category):
    """Vegan/veggie variant of a normally-meat item."""
    return _hit(_DIET_RE, name) or _hit(_DIET_RE, category)


def off_profile(name, category):
    """True when the item is outside the meat-forward mainstream taste profile.

    An explicit off-profile word (juice-bar oatmeal and friends) demotes an item
    even if it also matches an anchor; otherwise an item is off-profile only
    when it matches no anchor at all.
    """
    word = _hit(_OFF_PROFILE_RE, name) or _hit(_OFF_PROFILE_RE, category)
    if word:
        return word
    if _hit(_ANCHOR_RE, name) or _hit(_ANCHOR_RE, category):
        return None
    return "no taste anchor in name"


# ---------------------------------------------------------------- tsv io

def read_tsv(path):
    with open(path, encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        rows = []
        for line in f:
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            parts += [""] * (len(header) - len(parts))
            rows.append(dict(zip(header, parts)))
    return rows


def write_tsv(path, header, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(header) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(h, "")) for h in header) + "\n")


def money(x):
    return f"{x:.2f}"


def meals_fmt(x):
    """Meal counts are fractional since the 2026-08-24 weighting ruling.

    Whole counts still print as "1" and "2" so the older columns read exactly as
    they did; a fractional one prints as "0.5" / "1.5" rather than "0.50".
    """
    return f"{float(x):g}"


# ---------------------------------------------------------------- promo math

class Promo:
    """A promotion turned into arithmetic.

    kind: pct | spend_save | flat_off | bogo | delivery | UNPARSED
    """

    def __init__(self, store_id, campaign_id, title, description, source):
        self.store_id = store_id
        self.campaign_id = campaign_id
        self.title = title
        self.description = description
        self.source = source
        self.kind = "UNPARSED"
        self.pct = 0.0
        self.cap = None
        self.min_subtotal = 0.0
        self.amount = 0.0
        self.delivery_only = False
        self.note = ""
        # defect 18: promo list says "eligible" for promos that promo apply
        # refuses. Nothing is believed until a real preview showed it applying.
        self.status = "unconfirmed"
        self.confirmed_date = ""
        self._parse()

    @property
    def confirmed(self):
        return self.status == "confirmed"

    # "Up to $8 off" appears in the description, sometimes in the title
    def _cap(self):
        for text in (self.description, self.title):
            m = re.search(r"up to \$(\d+(?:\.\d{1,2})?)", text, re.I)
            if m:
                return float(m.group(1))
        return None

    def _parse(self):
        t = self.title.strip()
        # strip merchandising prefixes: "Deals: ", "Happy Hour: "
        prefix = ""
        m = re.match(r"^(Deals|Happy Hour|Deal|Offers?)\s*:\s*(.*)$", t, re.I)
        if m:
            prefix, t = m.group(1), m.group(2)
        d = self.description

        # Buy 1, get 1 free
        if re.search(r"buy\s*(1|one)\s*,?\s*get\s*(1|one)\s*(free)?", t, re.I):
            self.kind = "bogo"
            self.delivery_only = bool(re.search(r"delivery only", d, re.I))
            self.note = "eligible items only"
            return

        # N% off on $M+   (optional cap)
        m = re.search(r"(\d+(?:\.\d+)?)%\s*off\s*on\s*\$(\d+(?:\.\d{1,2})?)\+?", t, re.I)
        if m:
            self.kind = "pct"
            self.pct = float(m.group(1)) / 100.0
            self.min_subtotal = float(m.group(2))
            self.cap = self._cap()
            return

        # N% off, up to $K   (no subtotal minimum stated)
        m = re.search(r"(\d+(?:\.\d+)?)%\s*off", t, re.I)
        if m:
            self.kind = "pct"
            self.pct = float(m.group(1)) / 100.0
            self.min_subtotal = 0.0
            self.cap = self._cap()
            if re.search(r"select items", t, re.I):
                self.note = "select items only - eligibility unverified"
            if prefix.lower().startswith("happy hour"):
                self.note = (self.note + "; " if self.note else "") + "happy hour window only"
            return

        # Spend $X, save $Y
        m = re.search(r"spend\s*\$(\d+(?:\.\d{1,2})?)\s*,?\s*save\s*\$(\d+(?:\.\d{1,2})?)", t, re.I)
        if m:
            self.kind = "spend_save"
            self.min_subtotal = float(m.group(1))
            self.amount = float(m.group(2))
            return

        # $X off (orders over $Y)
        m = re.search(r"\$(\d+(?:\.\d{1,2})?)\s*off", t, re.I)
        if m:
            self.kind = "flat_off"
            self.amount = float(m.group(1))
            m2 = re.search(r"(?:over|above|of)\s*\$(\d+(?:\.\d{1,2})?)", t + " " + d, re.I)
            self.min_subtotal = float(m2.group(1)) if m2 else 0.0
            return

        # $0 delivery fee
        if re.search(r"\$0\s*delivery", t, re.I):
            self.kind = "delivery"
            m2 = re.search(r"over \$(\d+(?:\.\d{1,2})?)", d, re.I)
            self.min_subtotal = float(m2.group(1)) if m2 else 0.0
            self.note = "no effect: DashPass already zeroes delivery fee"
            return

        # everything else stays UNPARSED and gets reported
        self.kind = "UNPARSED"

    def discount_on(self, subtotal, unit_price, qty):
        """Dollars off for this promo on an order of qty x unit_price.

        Returns None when the promo does not apply to this order.
        """
        if self.kind == "UNPARSED" or self.kind == "delivery":
            return None
        if subtotal < self.min_subtotal - 1e-9:
            return None
        if self.kind == "pct":
            d = self.pct * subtotal
            if self.cap is not None:
                d = min(d, self.cap)
            return d
        if self.kind == "spend_save":
            return self.amount
        if self.kind == "flat_off":
            return self.amount
        if self.kind == "bogo":
            # one free item for every two ordered
            if qty < 2:
                return None
            return unit_price * (qty // 2)
        return None

    def label(self):
        return self._math() + f" [{self.status}]"

    def _math(self):
        if self.kind == "pct":
            cap = f", cap ${self.cap:.2f}" if self.cap is not None else ""
            return f"pct {self.pct*100:.0f}% min ${self.min_subtotal:.2f}{cap}"
        if self.kind == "spend_save":
            return f"spend ${self.min_subtotal:.2f} save ${self.amount:.2f}"
        if self.kind == "flat_off":
            return f"${self.amount:.2f} off min ${self.min_subtotal:.2f}"
        if self.kind == "bogo":
            return "bogo" + (" (delivery only)" if self.delivery_only else "")
        return self.kind


def load_confirmations(path):
    """(store_id, campaign_id) -> (status, confirmed_date) from the promo ledger.

    Missing file or missing row both mean UNCONFIRMED, which is the safe state:
    the promo is never allowed to move a predicted total, only to be reported
    as upside.
    """
    if not os.path.exists(path):
        return {}
    out = {}
    for r in read_tsv(path):
        status = (r.get("status") or "confirmed").strip().lower()
        out[(r["store_id"], r["campaign_id"])] = (status, r.get("confirmed_date", ""))
    return out


# ------------------------------------------------------------ store exclusions

def exclusions_path(d):
    """Where to find excluded_stores.tsv.

    Same convention as the calibration file: prefer a copy inside the data dir,
    so a fixture directory can carry its own, and otherwise use the one that
    ships next to this script in scanner/.
    """
    local = os.path.join(d, "excluded_stores.tsv")
    if os.path.exists(local):
        return local
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "excluded_stores.tsv")


def load_exclusions(path):
    """store_ids the user has permanently ruled out, as a set of exact strings.

    File format (scanner/excluded_stores.tsv): store_id, name, reason - tab
    separated, with a header row.

    This deliberately does NOT go through read_tsv. read_tsv treats line 1 as a
    header unconditionally, so the day someone writes this file without one, the
    first banned store would be silently readmitted to the report. A ban has to
    fail closed: every line is data unless it literally says "store_id", so a
    headerless file still bans everything in it.

    store_ids are compared as opaque strings, never parsed as numbers.

    A missing file means no exclusions, which is what lets the scanner run
    against a fixture dir. A file that exists but cannot be read is not caught
    here and will raise - the wrong response to "I cannot tell if this store is
    banned" is not "assume it is fine".
    """
    if not os.path.exists(path):
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            sid = line.split("\t", 1)[0].strip()
            if not sid or sid == "store_id":
                continue          # blank line, or the header
            out.add(sid)
    return out


# ---------------------------------------------------------------- fee model

def taxable_base(subtotal, discount, service_fee, delivery_fee, fulfillment):
    """The amount DoorDash actually charges tax on, per fulfillment mode.

    Delivery taxes the fees as well as the food; pickup has no fees to tax, so
    it taxes the food alone. Both directions are checked live: Chen's Dumpling
    House on a $7.99 subtotal was quoted $0.66 tax with $2.99 service + $0.99
    delivery (0.66 / 11.97 = 5.51%), and the identical cart on pickup was quoted
    $0.44 (0.44 / 7.99 = 5.51%). One rate, two bases.

    This exists as a free function so that _fit() and total() cannot drift apart:
    the rate is fitted with exactly the expression the prediction later uses.
    """
    base = subtotal + discount
    if fulfillment != "pickup":
        base += service_fee + delivery_fee
    return base


class FeeModel:
    """Fee model fitted from real checkout previews.

    Fitted form (all of it checked in --selftest):
        taxable_base = subtotal + discount [+ service + delivery, delivery only]
        tax          = round(tax_rate * taxable_base, 2)

        subtotal >= DASHPASS_MIN_SUBTOTAL   (DashPass free-delivery threshold)
            service_fee  = max(SERVICE_FLOOR, service_rate * subtotal)
            delivery_fee = 0.00
        subtotal <  DASHPASS_MIN_SUBTOTAL
            service_fee  = the sub-threshold service fee that store was seen
                           charging, else SERVICE_HIGH
            delivery_fee = the sub-threshold delivery fee that store was seen
                           charging, else a [0.00, max_delivery_fee] bracket
        pickup       = no service fee, no delivery fee

    The threshold is the whole point of the fee half of this model. Fees are not
    a property of the store, they are a property of the CART: the same store
    charges $2.99 + $0.49 on an $11 cart and $0.99 + $0.00 on a $13 one. The
    previous version fitted one fee tier per store, which meant a store last
    previewed on a small cart had its small-cart fees applied to every cart
    afterwards - a flat ~$2.62 over-charge on any cart that clears $12.

    Nothing here is hardcoded from a store the model has not seen: the service
    rate, the sub-threshold service fallback and the delivery bracket are all
    read off the calibration set, so a leave-one-out refit really does forget
    the held-out store (see --selftest).

    A store observed more than once below the threshold keeps its LATEST
    sub-threshold observation, because fee tiers change over time and the newest
    preview is the best guide to the next order. Earlier rows that disagree are
    reported as superseded in --selftest. Above the threshold there is nothing
    store-specific left to supersede - the fees are formulaic.
    """

    def __init__(self, previews):
        self.previews = previews
        self.seen = set()     # every store_id with a delivery preview
        self.store = {}       # store_id -> {"service","delivery","row"} sub-threshold
        self.tax_rate = 0.0
        self.service_rate = 0.0
        self.naive_tax_rate = 0.0
        self.max_delivery_fee = 0.0
        self.max_sub_service_fee = SERVICE_HIGH
        self.superseded = set()   # indices of preview rows a later row overrides
        self._fit()

    def _fit(self):
        num = den = 0.0
        naive_den = 0.0
        rates = []
        sub_deliveries = []
        sub_services = []
        for i, p in enumerate(self.previews):
            sub, disc = float(p["subtotal"]), float(p["discount"])
            svc, dlv = float(p["service_fee"]), float(p["delivery_fee"])
            tax = float(p["tax"])
            # defect: the tax rate used to be accumulated over one single
            # expression for every row. It happened to give the right answer only
            # because every pickup row in the file carries 0.00 in both fee
            # columns, so the delivery expression collapsed to the pickup one by
            # accident. Going through taxable_base() makes each row contribute
            # its own fulfillment's base on purpose, so a pickup row that ever
            # records a fee cannot quietly poison the rate.
            num += tax
            den += taxable_base(sub, disc, svc, dlv, p["fulfillment"])
            naive_den += sub + disc
            if p["fulfillment"] != "delivery":
                continue
            sid = p["store_id"]
            self.seen.add(sid)
            if sub >= DASHPASS_MIN_SUBTOTAL:
                # Above the threshold the fee is formulaic and identical at every
                # store, so the only thing to learn is the service rate. Only
                # rows above the floor carry rate information; a row sitting at
                # the floor says the rate is anything at or below svc/sub.
                if svc > SERVICE_FLOOR + 1e-9:
                    rates.append(svc / sub)
                continue
            sub_deliveries.append(dlv)
            sub_services.append(svc)
            prev = self.store.get(sid)
            self.store[sid] = {"service": svc, "delivery": dlv, "row": i}
            # a store seen twice below the threshold at genuinely different fees:
            # the older row can no longer be reproduced, and saying so beats
            # hiding it
            if prev is not None and (abs(prev["delivery"] - dlv) > 1e-9
                                     or abs(prev["service"] - svc) > 1e-9):
                self.superseded.add(prev["row"])
        self.tax_rate = num / den
        self.naive_tax_rate = num / naive_den
        self.service_rate = sum(rates) / len(rates) if rates else 0.05
        self.service_rate_samples = len(rates)
        # The high bound for a store with no sub-threshold observation has to
        # allow the worst sub-threshold fees the calibration set has ever shown,
        # or it is not an upper bound at all. Fitted, never hardcoded, so
        # leave-one-out cannot leak.
        self.max_delivery_fee = max(sub_deliveries) if sub_deliveries else 0.0
        self.max_sub_service_fee = (max(sub_services) if sub_services
                                    else SERVICE_HIGH)

    def known(self, store_id):
        """True when this store has ever been previewed on delivery.

        Whether its FEES are pinned is a question about a cart, not a store -
        ask fee_known() for that.
        """
        return store_id in self.seen

    def fee_known(self, store_id, subtotal, fulfillment):
        """True when the fees for THIS cart are pinned rather than bracketed.

        Above the threshold nothing is bracketed for anyone: 75/75 calibration
        rows across 40+ distinct stores charge $0.00 delivery and the same
        service formula, with no store-to-store variation left to bracket.
        Below it, the delivery fee is store specific and only a sub-threshold
        preview of that store pins it.
        """
        if fulfillment == "pickup":
            return True
        if subtotal >= DASHPASS_MIN_SUBTOTAL:
            return True
        return store_id in self.store

    def fees(self, store_id, subtotal, fulfillment, bound):
        """(service, delivery) for a cart. bound is 'low', 'high' or 'known'."""
        if fulfillment == "pickup":
            return 0.0, 0.0
        if subtotal >= DASHPASS_MIN_SUBTOTAL:
            return max(SERVICE_FLOOR, self.service_rate * subtotal), 0.0
        info = self.store.get(store_id)
        if info is not None:
            return info["service"], info["delivery"]
        # No sub-threshold observation for this store: bracket BOTH fees. The
        # low end is not 0.00 for the service fee, because no sub-threshold row
        # in the calibration set has ever charged less than SERVICE_HIGH - a
        # lower "bound" there would be an invention, not a bound.
        if bound == "high":
            return self.max_sub_service_fee, self.max_delivery_fee
        return SERVICE_HIGH, 0.0

    def total(self, store_id, subtotal, discount, fulfillment="delivery", bound="known"):
        """Predicted pre-tip total. bound picks the direction of tax rounding.

        The tax carries an irreducible one-cent jitter: store 658055 was previewed
        twice on an identical $15.37 taxable base and charged $0.85 once and $0.84
        the other time. No tax rate can predict that, so a BOUND must absorb it -
        the low end rounds the tax down and the high end rounds it up. 'known'
        keeps nearest-rounding because it is a point prediction, not a bound, and
        the in-sample check wants to see the arithmetic exactly.
        """
        svc, dlv = self.fees(store_id, subtotal, fulfillment, bound)
        base = taxable_base(subtotal, discount, svc, dlv, fulfillment)
        raw = self.tax_rate * base
        if bound == "low":
            tax = math.floor(raw * 100.0) / 100.0
        elif bound == "high":
            tax = math.ceil(raw * 100.0) / 100.0
        else:
            tax = round(raw, 2)
        # pickup does not tax the fees, but it still has to PAY nothing for them,
        # so the payable total is the taxable base plus tax either way
        return {"service": svc, "delivery": dlv, "tax": round(tax, 2),
                "total": round(subtotal + discount + svc + dlv + tax, 2)}


# ---------------------------------------------------------------- meals

def excluded_as(name, category):
    """Return the bucket name that disqualifies this item, or None."""
    for bucket, words in _KEYWORD_RE.items():
        for word, rx in words:
            if rx.search(category):
                return bucket
            if word not in CATEGORY_ONLY and rx.search(name):
                return bucket
    return None


def meals_per_unit(name):
    """Authoritative rule: a medium (12") pizza is ONE meal; large (14"+) is two."""
    m = PIZZA_RE.search(name)
    if m and int(m.group(1)) >= 14:
        return 2
    return 1


def fastfood_sandwich(name, category, price):
    """A fast-food-size sandwich, where two of them may be a single meal."""
    if price > FASTFOOD_SANDWICH_MAX:
        return False
    return bool(_hit(_FASTFOOD_RE, name) or _hit(_FASTFOOD_RE, category))


def meal_weight(name, category):
    """MEAL WEIGHT for one unit of an item (user ruling 2026-08-24).

    Returns (weight, unsure, reason). weight is how much of a lunch one unit is:
    1.0 a proper entree, 0.5 an appetizer/small plate/side soup, 0.4 a distinctly
    tiny portion, 0.0 a drink/dessert/sauce. unsure is True when nothing in the
    item name or its menu section settles it, in which case the weight defaults
    to 0.5 and the row is flagged so the nightly verifier - which can see the
    real item name, size and photo - checks the meal math rather than trusting it.

    PRECEDENCE, strongest evidence first. Name evidence beats section evidence,
    because a menu section is written once for a whole page of items and the
    2026-08-24 data is full of sections that say nothing about meal size at all
    ("EVERY DAY SPECIALS", "Sharing", "Deli Menu", "House Creation"):

      1. entree-soup exception  pho/ramen/udon/big noodle soup is an entree
      2. not food for lunch     drink/dessert/sauce words -> 0.0
      3. entree FORM in name    "...Plate", "...Combo", "...Sandwich" -> 1.0
      4. sub-meal word in name  bisque, ceviche, bun, dumpling, nigiri -> 0.4/0.5
      5. sub-meal word in section
      6. salad                  a protein in the name makes it an entree salad
      7. entree DISH word in name or section
      8. entree FORM word in section
      9. nothing decisive       -> 0.5, unsure

    Step 3 sits above step 4 on purpose: a form word describes how the food is
    served, which is what decides whether it is lunch. "Chicken Wing Plate" is a
    plate. This is the same precedence the PORTION rule already uses when
    MEAL_MAKER rescues a portion word, so the two rules agree.

    Step 2 reuses excluded_as, so this function agrees with real_meals() about
    what counts as food at all. It inherits that function's one known wart: a
    combo that NAMES a drink ("Chicken Sandwich W/Fries&drink") reads as a
    drink. Such an item is already dropped before ranking, so the wart costs a
    candidate rather than inventing one, and matching real_meals is worth more
    here than disagreeing with it.
    """
    word = _hit(_ENTREE_SOUP_RE, name) or _hit(_ENTREE_SOUP_RE, category)
    if word:
        return MEAL_WEIGHT_ENTREE, False, f"entree_soup_{word}"

    bucket = excluded_as(name, category)
    if bucket in ("drinks", "desserts", "sauces"):
        return MEAL_WEIGHT_NONE, False, f"not_food_{bucket}"

    word = _hit(_ENTREE_FORM_RE, name)
    if word:
        return MEAL_WEIGHT_ENTREE, False, f"entree_form_name_{word}"

    for w in (MEAL_WEIGHT_TINY, MEAL_WEIGHT_SMALL):
        word = _hit(_SUB_MEAL_NAME_RE[w], name)
        if word:
            return w, False, f"sub_meal_name_{word}"

    word = _hit(_SUB_MEAL_CAT_RE, category)
    if word:
        return MEAL_WEIGHT_SMALL, False, f"sub_meal_section_{word}"

    # salads: the protein decides. This runs AFTER the section check so that a
    # salad listed under "SIDES" or "Soup & Salad" stays a side - the section
    # saying "side" is better evidence than the word "chicken" in the name.
    if _hit(_SALAD_RE, name) or _hit(_SALAD_RE, category):
        word = _hit(_SALAD_PROTEIN_RE, name)
        if word:
            return MEAL_WEIGHT_ENTREE, False, f"entree_salad_{word}"
        return MEAL_WEIGHT_SMALL, False, "side_salad_no_protein"

    word = _hit(_ENTREE_DISH_RE, name) or _hit(_ENTREE_DISH_RE, category)
    if word:
        return MEAL_WEIGHT_ENTREE, False, f"entree_dish_{word}"

    word = _hit(_ENTREE_CAT_RE, category)
    if word:
        return MEAL_WEIGHT_ENTREE, False, f"entree_section_{word}"

    return MEAL_WEIGHT_SMALL, True, "no_meal_size_evidence"


def meal_equiv_band(name, category, price, qty):
    """(equiv_min, equiv_max, weight, unsure) meal EQUIVALENTS for qty units.

    Composition with the older MEAL-COUNT RULES: those rules turn one item into
    MORE than one meal (a 14" pizza is two, a BOGO sandwich pair may be one or
    two). This weight turns one item into LESS than one. An item is never both,
    so the multi-meal band is computed only for a full-weight item; a sub-meal
    item is simply qty x weight, with no band of its own.

    A weight-1.0 item therefore comes out of here with exactly the numbers
    meal_band() gave before this rule existed, which is what keeps the per-meal
    cost of every pure-entree cart unchanged.
    """
    w, unsure, reason = meal_weight(name, category)
    if w >= MEAL_WEIGHT_ENTREE - 1e-9:
        lo, hi = meal_band(name, category, price, qty)
        return float(lo), float(hi), w, unsure
    return qty * w, qty * w, w, unsure


def meal_band(name, category, price, qty):
    """(meals_min, meals_max) for a cart of qty units.

    The two differ only for multi-unit fast-food sandwich carts, where the
    rules say a pair is one meal but a hungry reading says two. Reporting both
    is what the rules ask for when the count is genuinely unsure.
    """
    meals_max = qty * meals_per_unit(name)
    if qty >= 2 and fastfood_sandwich(name, category, price):
        return math.ceil(meals_max / 2), meals_max
    return meals_max, meals_max


def real_meals(items):
    keep, rejected = [], {}
    for it in items:
        try:
            price = float(it["price"])
        except ValueError:
            # malformed row (e.g. a stray newline split the record); report it
            rejected["malformed price"] = rejected.get("malformed price", 0) + 1
            continue
        if it["orderable"] != "1":
            rejected["not orderable"] = rejected.get("not orderable", 0) + 1
            continue
        if not (MEAL_MIN_PRICE <= price <= MEAL_MAX_PRICE):
            rejected["price out of $7-20"] = rejected.get("price out of $7-20", 0) + 1
            continue
        bucket = excluded_as(it["name"], it["category"])
        if bucket:
            rejected[bucket] = rejected.get(bucket, 0) + 1
            continue
        # PORTION rule: a small protein portion with no side is not a meal
        if portion_only(it["name"], it["category"]):
            rejected["portion without side"] = rejected.get("portion without side", 0) + 1
            continue
        keep.append(it)
    return keep, rejected


# ---------------------------------------------------------------- scoring

def score_candidate(fm, store_id, item, promo, qty, fulfillment="delivery"):
    """Price one cart as a [low, high] band.

    Two independent sources of width:
      fees      - unknown-fee stores are bracketed by the two observed tiers
      req_mods  - defect 19: the menu price is only a lower bound when required
                  modifiers carry the cost, so the high bound prices the same
                  cart at REQ_MOD_UPLIFT x the base price

    fulfillment="pickup" prices the identical cart with FeeModel's pickup branch:
    no service fee, no delivery fee, tax on the food alone. The promo math is
    left exactly as it is for delivery, which is the one place this estimate can
    be wrong in the expensive direction - see the PICKUP LANE note in the module
    docstring and pickup_lane() below.
    """
    price = float(item["price"])
    req_mods = item["req_mods"] == "1"
    price_high = round(price * REQ_MOD_UPLIFT, 2) if req_mods else price
    sub_low = round(price * qty, 2)
    sub_high = round(price_high * qty, 2)

    def disc(subtotal, unit):
        if promo is None:
            return 0.0
        d = promo.discount_on(subtotal, unit, qty)
        return None if d is None else -round(d, 2)

    # Eligibility is judged on the low subtotal: if the cheapest reading of the
    # cart does not clear the promo's minimum, do not assume the promo applies.
    discount = disc(sub_low, price)
    if discount is None:
        return None
    discount_high = disc(sub_high, price_high)
    if discount_high is None:
        discount_high = 0.0

    # A store BOGO is advertised per STORE, but eligibility is per ITEM, and
    # `promo list` does not say which items qualify. Assuming it attaches to
    # whatever is cheapest invents deals that do not exist: it priced Domino's
    # 5-Cheese Mac & Cheese at $5.68/meal for five consecutive runs against a
    # real $10.85. Until a preview confirms the campaign, the pessimistic bound
    # assumes it does NOT attach, and the candidate is ranked on that bound.
    bogo_unverified = (promo is not None and promo.kind == "bogo"
                       and promo.status != "confirmed")
    if bogo_unverified:
        discount_high = 0.0

    # A promo whose own description says "delivery only" is not a risk to note,
    # it is a statement of fact: it will not attach to a pickup cart. Pricing it
    # in regardless would put an invented deal at the TOP of the pickup lane -
    # and the cart it does that to is the Domino's 5-Cheese Mac & Cheese, the
    # same cart the comment above records as having faked a $5.68/meal deal for
    # five consecutive runs. So the discount comes off on this lane.
    #
    # This is narrow on purpose. It only fires on the flag Promo already parses
    # out of DoorDash's own text. Promos that are delivery-only WITHOUT saying so
    # are the documented risk the module docstring describes, and only a real
    # pickup preview settles those.
    promo_delivery_only = (fulfillment == "pickup" and promo is not None
                           and promo.delivery_only)
    if promo_delivery_only:
        discount = 0.0
        discount_high = 0.0

    # MEAL EQUIVALENTS (user ruling 2026-08-24). A cart that does not add up to
    # a whole lunch is not a candidate at all, however cheap it looks per item:
    # one order of 3pc pork buns is half a lunch, so it is dropped here and only
    # the qty-2 reading of the same item can rank. The quantity sweep in
    # best_per_store already tries every qty from 1 to max_qty, so the cart that
    # does reach a whole meal is found by the existing cart builder.
    meals_min, meals_max, weight, weight_unsure = meal_equiv_band(
        item["name"], item["category"], price, qty)
    if meals_min < MEAL_EQUIV_MIN - 1e-9:
        return None
    # Fees are pinned only if they are pinned at BOTH ends of the cart's price
    # band - a band that straddles $12 has one end bracketed and one end not.
    known = (fm.fee_known(store_id, sub_low, fulfillment)
             and fm.fee_known(store_id, sub_high, fulfillment))
    # Always price the band as a band. For a cart with fees pinned at both ends
    # the two ends differ only by the one-cent tax jitter; for a sub-threshold
    # cart at a store with no sub-threshold preview they also differ by the full
    # service AND delivery fee brackets.
    #
    # The band is two discrete scenarios, not a continuum: the cart is either at
    # its menu price or at the REQ_MOD_UPLIFT price, with the matching discount.
    # Total is NOT monotonic in subtotal any more - crossing $12 upward DROPS
    # about $2.62 of fees - so taking sub_low for the low end and sub_high for
    # the high end would invert the band on any req_mods cart that straddles the
    # threshold. Price both scenarios and let the cheapest and dearest speak.
    ends = [(sub_low, discount), (sub_high, discount_high)]
    lo = min((fm.total(store_id, s, d, fulfillment, "low") for s, d in ends),
             key=lambda r: r["total"])
    hi = max((fm.total(store_id, s, d, fulfillment, "high") for s, d in ends),
             key=lambda r: r["total"])
    c = {
        "store_id": store_id,
        # the source row, kept so the pickup lane can re-price this exact cart
        # without re-deriving the promo math (it is never written to a TSV)
        "item": item,
        "fulfillment": fulfillment,
        "item_id": item["item_id"],
        "item_name": item["name"],
        "category": item["category"],
        "price": price,
        "price_high": price_high,
        "qty": qty,
        "meals_low": meals_min,
        "meals_high": meals_max,
        "subtotal": sub_low,
        "subtotal_high": sub_high,
        "discount": discount,
        "discount_high": discount_high,
        "service_low": lo["service"], "service_high": hi["service"],
        "delivery_low": lo["delivery"], "delivery_high": hi["delivery"],
        "tax_low": lo["tax"], "tax_high": hi["tax"],
        "total_low": lo["total"], "total_high": hi["total"],
        # optimistic end: cheapest total spread over the MOST meals it can be
        # pessimistic end: dearest total spread over the FEWEST meals it can be
        "per_meal_low": lo["total"] / meals_max,
        "per_meal_high": hi["total"] / meals_min,
        "fee_known": "1" if known else "0",
        "req_mods": item["req_mods"],
        "promo": promo,
        "diet_variant": diet_variant(item["name"], item["category"]),
        "off_profile": off_profile(item["name"], item["category"]),
        "size_flag": size_flag(item["name"], item["category"]),
        "meal_weight": weight,
        "meal_equiv": meals_min,
        "meal_weight_unsure": weight_unsure,
    }
    # Rank on the pessimistic end whenever the optimistic end rests on something
    # unverified: a req_mods base price that may not be orderable at all, a meal
    # count that may really be half what it looks like, or - defect 1 - a store
    # whose fees no real preview has ever pinned. Ranking an unseen-fee store on
    # its optimistic total is what put MOOYAH at $9.27 against a real $12.96 and
    # pushed four verified stores off the top of the list.
    #
    # The "not known" arm is kept for pickup too even though pickup fees are
    # structurally zero rather than fitted, so an unseen store carries no fee
    # uncertainty on this lane. Keeping it costs at most the one-cent tax jitter
    # (with both fees at zero the two bounds are otherwise the same number) and
    # keeps one ranking rule instead of two.
    c["meal_count_unsure"] = meals_min != meals_max
    c["bogo_unverified"] = bogo_unverified
    c["promo_delivery_only"] = promo_delivery_only
    # meal_weight_unsure joins the same list: when nothing in the item name or
    # its menu section says whether the thing is an entree or an appetizer, the
    # 0.5 default is a guess, and a guess must not be what wins a ranking.
    c["rank_per_meal"] = (c["per_meal_high"]
                          if (req_mods or c["meal_count_unsure"] or not known
                              or bogo_unverified or weight_unsure)
                          else c["per_meal_low"])
    return c


def rank_key(c):
    """Sort key. Off-profile items never outrank an on-profile one."""
    return (1 if c["off_profile"] else 0, c["rank_per_meal"], c["per_meal_low"])


def within_ceiling(c):
    """True when the cart's predicted pre-tip order total fits under the cap.

    total_high is the number that has to fit: for a store with fees pinned by a
    real preview it equals total_low, and for an unknown-fee store it is the
    worst-case (uncovered, $2.99 service) prediction. Testing the high bound
    means a cart that clears the ceiling clears it under either fee tier.
    """
    return c["total_high"] <= ORDER_CEILING + 1e-9


def best_per_store(fm, items_by_store, promos_by_store, max_qty=4):
    """One ranked candidate per store, plus its alternates.

    The primary candidate is priced with CONFIRMED promos only (defect 18) and
    is a standard, non-diet-variant item (preference rules). Two extras hang off
    it when they exist:
      alt    the diet variant, when the diet variant really is the cheaper deal
      upside the same store priced with an UNCONFIRMED promo applied
    """
    out = []
    for store_id, items in items_by_store.items():
        meals, _ = real_meals(items)
        if not meals:
            continue
        promos = [p for p in promos_by_store.get(store_id, []) if p.kind != "UNPARSED"]
        confirmed = [p for p in promos if p.status == "confirmed"]
        unconfirmed = [p for p in promos if p.status == "unconfirmed"]
        # refuted promos (a real preview showed them NOT attaching) are dropped

        def sweep(promo_pool, want_diet):
            best = None
            for it in meals:
                if bool(diet_variant(it["name"], it["category"])) != want_diet:
                    continue
                for promo in promo_pool:
                    for qty in range(1, max_qty + 1):
                        c = score_candidate(fm, store_id, it, promo, qty)
                        if c is None:
                            continue
                        if not within_ceiling(c):
                            # too expensive as an ORDER, however cheap per meal
                            continue
                        if best is None or rank_key(c) < rank_key(best):
                            best = c
            return best

        base_pool = [None] + confirmed
        primary = sweep(base_pool, want_diet=False)
        diet_only = False
        if primary is None:
            # nothing but diet variants on this menu - rank one, but say so
            primary = sweep(base_pool, want_diet=True)
            diet_only = True
        if primary is None:
            continue

        alt = None if diet_only else sweep(base_pool, want_diet=True)
        if alt is not None and rank_key(alt) >= rank_key(primary):
            alt = None            # standard variant already wins, nothing to show

        upside = sweep(unconfirmed, want_diet=diet_only) if unconfirmed else None
        if upside is not None and upside["rank_per_meal"] >= primary["rank_per_meal"]:
            upside = None

        primary["diet_only"] = diet_only
        primary["alt"] = alt
        primary["upside"] = upside
        out.append(primary)
    out.sort(key=rank_key)
    return out


# ---------------------------------------------------------------- pickup lane

def store_km(stores, store_id):
    """distance_km for a store as a float, or None when it is not known.

    Not known means: the store is absent from the census, or its distance column
    is blank or unparseable. An unknown distance is never treated as near - a
    store has to be positively inside the radius to reach the pickup lane.
    """
    raw = stores.get(store_id, {}).get("distance_km", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _repriced(fm, c, fulfillment):
    """The same cart - store, item, quantity, promo - on another fulfillment."""
    if c is None:
        return None
    return score_candidate(fm, c["store_id"], c["item"], c["promo"], c["qty"],
                           fulfillment=fulfillment)


def pickup_lane(fm, cands, stores):
    """Re-price the ranked candidates as PICKUP orders, for near stores only.

    What this does: takes each store's already-chosen candidate cart (and its
    alt and upside carts) and prices that exact cart through FeeModel's pickup
    branch - no service fee, no delivery fee, tax on the food alone.

    What this does NOT do: re-choose the cart. The item, quantity and promo are
    whatever won on the delivery lane. Dropping the fees to zero lowers every
    total, so no cart that cleared ORDER_CEILING on delivery can fail it here,
    but a cart that was rejected as too expensive to have delivered is not
    reconsidered, and an item that would be the cheapest per meal only once the
    fees come off is not promoted. Re-running the full per-store sweep on the
    pickup lane would fix both; it is not done because these numbers are
    estimates that a real preview has to confirm anyway.

    KNOWN RISK, and the reason nothing here is presentable as a price: apart
    from promos that declare themselves delivery-only, which score_candidate
    drops on this lane, the promo math is the delivery lane's. Many DoorDash
    promos are delivery-only WITHOUT saying so anywhere the scanner can read.
    Such a promo does not error on a pickup cart, it simply does not attach, so
    the real pickup total can land HIGHER than the estimate below - by the whole
    discount. The previews the verification stage takes are authoritative;
    everything in ranked_pickup.tsv is a candidate for one.
    """
    out = []
    for c in cands:
        km = store_km(stores, c["store_id"])
        if km is None or km > PICKUP_MAX_KM:
            continue
        p = _repriced(fm, c, "pickup")
        if p is None or not within_ceiling(p):
            continue
        p["diet_only"] = c["diet_only"]
        alt = _repriced(fm, c["alt"], "pickup")
        if alt is not None and rank_key(alt) >= rank_key(p):
            alt = None            # standard variant already wins on this lane too
        p["alt"] = alt
        upside = _repriced(fm, c["upside"], "pickup")
        if upside is not None and upside["rank_per_meal"] >= p["rank_per_meal"]:
            upside = None
        p["upside"] = upside
        # carried into the extra ranked_pickup.tsv column so the saving against
        # having the same cart delivered is readable without a join
        p["delivery_rank_per_meal"] = c["rank_per_meal"]
        out.append(p)
    out.sort(key=rank_key)
    return out


# ---------------------------------------------------------------- selftest

def _insample(fm):
    """Refit-and-predict on the rows the model was fitted on.

    This is the optimistic number and it always will be: for any store that
    appears in the calibration set BELOW the threshold the model has memorised
    that store's exact sub-threshold fees, so for those rows it is really only
    checking the tax arithmetic. Above the threshold nothing is memorised - the
    fees are formulaic - so those rows are a real test of the fee formula. Kept
    because it catches arithmetic regressions either way, but --selftest reports
    the leave-one-out number as the headline, because that is the one that
    predicted the live run.
    """
    print("IN-SAMPLE (fitted on all rows, then asked about those same rows)")
    hdr = (f"{'store':>9} {'fulfil':<8} {'sub':>7} {'disc':>7} {'actual':>8} "
           f"{'pred':>8} {'err':>6}  note")
    print(hdr)
    errs, live_errs = [], []
    for i, p in enumerate(fm.previews):
        sid = p["store_id"]
        sub, disc = float(p["subtotal"]), float(p["discount"])
        actual = float(p["pretip_total"])
        pred = fm.total(sid, sub, disc, p["fulfillment"], "known")["total"]
        err = pred - actual
        errs.append(abs(err))
        stale = i in fm.superseded
        if not stale:
            live_errs.append(abs(err))
        note = "SUPERSEDED by a later preview of this store" if stale else ""
        print(f"{sid:>9} {p['fulfillment']:<8} {sub:>7.2f} {disc:>7.2f} "
              f"{actual:>8.2f} {pred:>8.2f} {err:>+6.2f}  {note}")
    mae = sum(errs) / len(errs)
    hit = sum(1 for e in errs if e <= 0.50)
    live_hit = sum(1 for e in live_errs if e <= 0.50)
    print()
    print(f"  mean absolute error            : ${mae:.4f}")
    print(f"  within $0.50, all rows         : "
          f"{hit/len(errs)*100:.1f}% ({hit}/{len(errs)})")
    print(f"  within $0.50, current rows only: "
          f"{live_hit/len(live_errs)*100:.1f}% ({live_hit}/{len(live_errs)}) "
          f"[{len(fm.superseded)} superseded row(s) excluded]")
    print("  NOTE: an in-sample hit on a SUB-THRESHOLD row is not evidence the")
    print("        model generalises - that store's fees were memorised from this")
    print("        very table. Above-threshold rows carry no memorised fee.")
    return live_hit == len(live_errs)


def _loo(fm):
    """Leave-one-out cross-validation, the honest accuracy number.

    For each calibration row the model is refitted from scratch on every row
    belonging to a DIFFERENT store, so the held-out store is genuinely unseen -
    not just that one preview, the whole store. Tax rate, covered service rate
    and the unseen-store delivery bracket are all refitted on the training fold,
    so nothing about the held-out store can leak in.

    The prediction for an unseen store is a band, not a point, so the metric
    that matters is whether the real total landed inside it. A model that widens
    its band to pass this test pays for it immediately: the same band drives the
    order ceiling and the ranking, so a lazy-wide bracket loses candidates.
    """
    print("LEAVE-ONE-OUT (store held out entirely, model refitted without it)")
    hdr = (f"{'store':>9} {'fulfil':<8} {'sub':>7} {'disc':>7} {'actual':>8} "
           f"{'pred_low':>9} {'pred_high':>9} {'mid_err':>8}  hit")
    print(hdr)
    errs, hits, skipped = [], 0, 0
    for p in fm.previews:
        sid = p["store_id"]
        train = [q for q in fm.previews if q["store_id"] != sid]
        if not train:
            skipped += 1
            continue
        m = FeeModel(train)
        sub, disc = float(p["subtotal"]), float(p["discount"])
        actual = float(p["pretip_total"])
        lo = m.total(sid, sub, disc, p["fulfillment"], "low")["total"]
        hi = m.total(sid, sub, disc, p["fulfillment"], "high")["total"]
        mid = (lo + hi) / 2.0
        err = abs(mid - actual)
        errs.append(err)
        inside = lo - 1e-9 <= actual <= hi + 1e-9
        hits += 1 if inside else 0
        print(f"{sid:>9} {p['fulfillment']:<8} {sub:>7.2f} {disc:>7.2f} "
              f"{actual:>8.2f} {lo:>9.2f} {hi:>9.2f} {mid-actual:>+8.2f}  "
              f"{'yes' if inside else 'NO'}")
    n = len(errs)
    mae = sum(errs) / n
    rate = hits / n * 100.0
    print()
    print(f"  interval hit rate (actual inside [low, high]) : "
          f"{rate:.1f}% ({hits}/{n})")
    print(f"  mean absolute error of the band midpoint      : ${mae:.4f}")
    if skipped:
        print(f"  rows skipped (no training data left)          : {skipped}")
    return rate


def selftest(fm):
    print("FEE MODEL FIT")
    print(f"  calibration rows          : {len(fm.previews)}")
    print(f"  tax rate (per-row base: +fees on delivery, food only on pickup) : "
          f"{fm.tax_rate*100:.4f}%")
    print(f"  tax rate (naive, on subtotal+discount only)             : "
          f"{fm.naive_tax_rate*100:.4f}%")
    print(f"  DashPass threshold        : subtotal >= "
          f"${DASHPASS_MIN_SUBTOTAL:.2f} (pre-discount)")
    print(f"  above threshold: service rate  : {fm.service_rate*100:.4f}% "
          f"of subtotal, floor ${SERVICE_FLOOR:.2f} "
          f"({fm.service_rate_samples} samples above the floor), delivery $0.00")
    print(f"  below threshold: service fee   : ${SERVICE_HIGH:.2f} default, "
          f"up to ${fm.max_sub_service_fee:.2f} on the high bound")
    print(f"  below threshold: delivery bracket for a store with no "
          f"sub-threshold preview : $0.00 to ${fm.max_delivery_fee:.2f} "
          f"(max observed, fitted not hardcoded)")
    print(f"  stores previewed on delivery   : {len(fm.seen)} "
          f"({len(fm.store)} of them below the threshold)")
    for sid, info in sorted(fm.store.items()):
        print(f"  store {sid:>9} sub-${DASHPASS_MIN_SUBTOTAL:.0f} "
              f"service=${info['service']:.2f} delivery=${info['delivery']:.2f}")
    print()
    insample_ok = _insample(fm)
    print()
    loo_rate = _loo(fm)
    print()
    print("VERDICT")
    print(f"  in-sample (optimistic, memorised fees) : "
          f"{'PASS' if insample_ok else 'FAIL'}")
    print(f"  leave-one-out interval hit rate        : {loo_rate:.1f}% "
          f"(target {LOO_TARGET:.0f}%)")
    ok = loo_rate >= LOO_TARGET and insample_ok
    print("  RESULT (headline is leave-one-out)     : "
          + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# ---------------------------------------------------------------- coverage

def coverage(d, fm):
    """Report stores whose menu data is missing, so they can be refetched.

    Two distinct gaps:
      no_items            in stores.tsv (the census) but zero rows in items.tsv
      missing_from_census calibrated in previews.tsv but absent from stores.tsv
    """
    stores = read_tsv(os.path.join(d, "stores.tsv"))
    items = read_tsv(os.path.join(d, "items.tsv"))

    # A permanently excluded store has no items on purpose. Reporting it as a
    # coverage gap would invite someone to "fix" it by refetching a store the
    # user has banned, so it is not a gap and is not listed.
    excluded = load_exclusions(exclusions_path(d))
    stores = [s for s in stores if s["store_id"] not in excluded]

    census = {s["store_id"]: s for s in stores}
    with_items = {it["store_id"] for it in items}

    no_items = [s for s in stores if s["store_id"] not in with_items]
    # ...and for the same reason an excluded store that appears in previews.tsv
    # (it was calibrated before the ruling) is not a missing census entry either
    missing = sorted({p["store_id"] for p in fm.previews
                      if p["store_id"] not in census and p["store_id"] not in excluded})

    print("COVERAGE GAPS")
    print(f"  stores in census        : {len(census)}")
    print(f"  stores with items       : {len(with_items)}")
    print()
    print(f"{'store_id':>9}  {'gap':<19} {'distance_km':>11}  name")
    for s in no_items:
        print(f"{s['store_id']:>9}  {'no_items':<19} {s.get('distance_km',''):>11}  "
              f"{s.get('name','')}")
    for sid in missing:
        print(f"{sid:>9}  {'missing_from_census':<19} {'':>11}  "
              f"(in calibration/previews.tsv)")
    print()
    print(f"  no_items            : {len(no_items)} store(s) need a menu refetch")
    print(f"  missing_from_census : {len(missing)} store(s) need a census entry")
    return 0


# ---------------------------------------------------------------- output rows

# The ranked.tsv schema. Downstream consumers read it positionally, so it is
# frozen: ranked_pickup.tsv and verify_list.tsv extend it with trailing columns
# and never reorder or drop one.
RANKED_HEADER = [
    "rank", "store_id", "store_name", "distance_km", "item_id", "item_name",
    "category", "price", "price_high", "qty", "meals_low", "meals_high",
    "promo_title",
    "promo_math", "promo_status",
    "subtotal", "subtotal_high", "discount", "service_low", "service_high",
    "delivery_low", "delivery_high",
    "tax_low", "tax_high", "total_low", "total_high",
    "per_meal_low", "per_meal_high", "rank_per_meal", "fee_known",
    "req_mods", "upside_promo", "upside_per_meal", "alt_variant_item",
    "alt_variant_per_meal", "flags",
    # appended 2026-08-24 for the fractional meal-counting ruling. New columns
    # go at the END so the positional readers of the older columns keep working.
    #   meal_weight        how much of a lunch ONE unit is (1.0 / 0.5 / 0.4)
    #   meal_equiv         meal equivalents in the whole cart = the denominator
    #                      of per_meal_high, and always >= MEAL_EQUIV_MIN
    #   meal_weight_unsure 1 when the weight is the 0.5 default rather than a
    #                      classified value - the verifier must check the meal
    #                      math on these rows before any number is reported
    "meal_weight", "meal_equiv", "meal_weight_unsure"]

PICKUP_HEADER = RANKED_HEADER + ["fulfillment", "delivery_rank_per_meal"]
VERIFY_HEADER = RANKED_HEADER + ["fulfillment"]


def build_rows(fm, cands, stores, fulfillment="delivery"):
    """ranked.tsv-shaped rows for one lane's candidates.

    Both lanes are built here so ranked_pickup.tsv cannot drift out of schema
    with ranked.tsv. Only the flags column differs between them.
    """
    rows = []
    for i, c in enumerate(cands, 1):
        st = stores.get(c["store_id"], {})
        promo = c["promo"]
        alt, upside = c["alt"], c["upside"]
        pickup = fulfillment == "pickup"
        flags = []
        if pickup:
            # the headline caveat, on every row, because a reader who sees only
            # this file has to see it: the promo may be delivery-only
            flags.append("PICKUP_ESTIMATE_promo_may_be_delivery_only_"
                         "real_total_can_be_higher_needs_pickup_preview")
        if c.get("promo_delivery_only"):
            # the promo columns still name the promo, so say plainly that its
            # money was NOT counted here
            flags.append("promo_description_says_DELIVERY_ONLY_discount_"
                         "NOT_applied_on_this_lane")
        if c["req_mods"] == "1":
            # defect 19: base price is a LOWER bound, true minimum unknown
            flags.append(f"req_mods_price_is_lower_bound_high_bound_x"
                         f"{REQ_MOD_UPLIFT:.2f}_ranked_on_high")
        if promo is not None and promo.note:
            flags.append(promo.note.replace("\t", " "))
        if c["fee_known"] == "0" and not pickup:
            # on the pickup lane there is nothing to bracket: both fees are zero
            # by the structure of a pickup order, not by a fit to observed rows
            flags.append(f"fees_unverified_cart_under_"
                         f"{DASHPASS_MIN_SUBTOTAL:.0f}_service_bracketed_"
                         f"{SERVICE_HIGH:.2f}_to_{fm.max_sub_service_fee:.2f}"
                         f"_delivery_bracketed_0.00_to_{fm.max_delivery_fee:.2f}"
                         "_ranked_on_high")
        if c["store_id"] not in stores:
            flags.append("store_not_in_census")
        if c["meal_count_unsure"]:
            flags.append(f"meal_count_unsure_{c['meals_low']}_or_{c['meals_high']}"
                         "_fastfood_sandwich_pair_may_be_one_meal_ranked_on_fewest")
        if c["meal_weight"] < MEAL_WEIGHT_ENTREE - 1e-9:
            # say the arithmetic out loud: the email has to show it whenever a
            # cart is not simply one whole meal per item
            flags.append(f"sub_meal_item_weight_{meals_fmt(c['meal_weight'])}"
                         f"_{c['qty']}x_{meals_fmt(c['meal_weight'])}"
                         f"_eq_{meals_fmt(c['meal_equiv'])}_meal_equivalents")
        if c["meal_weight_unsure"]:
            flags.append("meal_weight_unsure_defaulted_0.5_no_size_evidence_in_"
                         "name_or_menu_section_VERIFY_the_meal_math_ranked_on_high")
        if c["size_flag"]:
            flags.append(f"small_portion_check_size_word_{c['size_flag']}")
        if c["off_profile"]:
            flags.append(f"off_taste_profile_{c['off_profile'].replace(' ', '_')}"
                         "_downranked")
        if c["diet_only"]:
            flags.append("diet_variant_only_menu_no_standard_variant_found")
        if alt is not None:
            flags.append("cheap_variant_is_the_deal_see_alt_variant_columns")
        if upside is not None:
            flags.append(f"UNCONFIRMED_promo_upside_only_"
                         f"{upside['promo'].campaign_id[:8]}")
        rows.append({
            "rank": i, "store_id": c["store_id"],
            "store_name": st.get("name", "?"),
            "distance_km": st.get("distance_km", ""),
            "item_id": c["item_id"], "item_name": c["item_name"],
            "category": c["category"], "price": money(c["price"]),
            "price_high": money(c["price_high"]),
            "qty": c["qty"],
            "meals_low": meals_fmt(c["meals_low"]),
            "meals_high": meals_fmt(c["meals_high"]),
            "promo_title": promo.title if promo else "(none)",
            "promo_math": promo.label() if promo else "-",
            "promo_status": promo.status if promo else "no_promo_applied",
            "subtotal": money(c["subtotal"]),
            "subtotal_high": money(c["subtotal_high"]),
            "discount": money(c["discount"]),
            "service_low": money(c["service_low"]),
            "service_high": money(c["service_high"]),
            "delivery_low": money(c["delivery_low"]),
            "delivery_high": money(c["delivery_high"]),
            "tax_low": money(c["tax_low"]), "tax_high": money(c["tax_high"]),
            "total_low": money(c["total_low"]), "total_high": money(c["total_high"]),
            "per_meal_low": money(c["per_meal_low"]),
            "per_meal_high": money(c["per_meal_high"]),
            "rank_per_meal": money(c["rank_per_meal"]),
            "fee_known": c["fee_known"], "req_mods": c["req_mods"],
            "upside_promo": (f"{upside['promo'].title} ({upside['promo'].campaign_id})"
                             if upside else ""),
            "upside_per_meal": money(upside["per_meal_low"]) if upside else "",
            "alt_variant_item": (f"{alt['item_name']} [diet variant]" if alt else ""),
            "alt_variant_per_meal": money(alt["per_meal_low"]) if alt else "",
            "flags": "; ".join(flags),
            # trailing extras: written only into the files whose header asks for
            # them, because write_tsv selects by header
            "fulfillment": fulfillment,
            "delivery_rank_per_meal": (money(c["delivery_rank_per_meal"])
                                       if "delivery_rank_per_meal" in c else ""),
            "meal_weight": meals_fmt(c["meal_weight"]),
            "meal_equiv": meals_fmt(c["meal_equiv"]),
            "meal_weight_unsure": "1" if c["meal_weight_unsure"] else "0",
        })
    return rows


def build_verify_list(rows, pickup_rows):
    """verify_list.tsv rows: the delivery shortlist, then the pickup shortlist.

    The delivery half is unchanged from before the pickup lane existed - the top
    VERIFY_N candidates whose rank_per_meal beats BEST_VERIFIED_TOTAL - and now
    carries fulfillment=delivery.

    Then up to PICKUP_VERIFY_N pickup-lane candidates that clear the same bar are
    considered, in pickup-lane rank order. CONVENTION for a cart that qualifies
    on both lanes: it stays ONE row, and that row's fulfillment becomes "both"
    rather than a second row being appended. One row means the verifier builds
    one cart and previews it twice, which is what daily_scan.sh asks for anyway.
    The priced columns of a "both" row are the DELIVERY estimate - the pickup
    estimate for the same cart is the matching row of ranked_pickup.tsv.

    A "both" upgrade spends one of the PICKUP_VERIFY_N slots, because it still
    costs the verifier one extra pickup preview.

    The rank column of an appended pickup row is its rank within the PICKUP lane,
    so ranks repeat down the file. fulfillment is what tells the two apart.
    """
    verify = [dict(r, fulfillment="delivery") for r in rows
              if float(r["rank_per_meal"]) < BEST_VERIFIED_TOTAL][:VERIFY_N]
    by_cart = {(r["store_id"], r["item_id"]): r for r in verify}

    appended, considered = [], 0
    for pr in pickup_rows:
        if considered >= PICKUP_VERIFY_N:
            break
        if float(pr["rank_per_meal"]) >= BEST_VERIFIED_TOTAL:
            # rank_key sorts off-profile candidates last, so rank_per_meal is not
            # monotonic down the list: keep scanning rather than stopping here
            continue
        considered += 1
        same = by_cart.get((pr["store_id"], pr["item_id"]))
        if same is not None:
            same["fulfillment"] = "both"
            continue
        appended.append(dict(pr, fulfillment="pickup"))
    return verify + appended


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="directory holding items.tsv/promos.tsv/stores.tsv")
    ap.add_argument("--calibration", default=None,
                    help="path to previews.tsv (default <dir>/calibration/previews.tsv)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--coverage", action="store_true",
                    help="list stores with no items, and previewed stores not in the census")
    args = ap.parse_args()

    d = args.dir
    cal = args.calibration or os.path.join(d, "calibration", "previews.tsv")
    if not os.path.exists(cal):
        cal = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "calibration", "previews.tsv")
    fm = FeeModel(read_tsv(cal))

    if args.selftest:
        return selftest(fm)

    if args.coverage:
        return coverage(d, fm)

    items = read_tsv(os.path.join(d, "items.tsv"))
    promo_rows = read_tsv(os.path.join(d, "promos.tsv"))
    stores = {s["store_id"]: s for s in read_tsv(os.path.join(d, "stores.tsv"))}

    items_by_store = {}
    for it in items:
        items_by_store.setdefault(it["store_id"], []).append(it)

    confirmations = load_confirmations(
        os.path.join(os.path.dirname(cal), "promo_confirmed.tsv"))

    promos_by_store, unparsed = {}, []
    promo_status_counts = {"confirmed": 0, "unconfirmed": 0, "refuted": 0}
    for r in promo_rows:
        p = Promo(r["store_id"], r["campaign_id"], r["title"], r["description"],
                  r["source"])
        status, date = confirmations.get((p.store_id, p.campaign_id),
                                         ("unconfirmed", ""))
        p.status, p.confirmed_date = status, date
        promo_status_counts[status] = promo_status_counts.get(status, 0) + 1
        promos_by_store.setdefault(r["store_id"], []).append(p)
        if p.kind == "UNPARSED":
            unparsed.append(p)

    # Stores the user has permanently ruled out. daily_scan.sh also skips these
    # at fetch time, but that is only half a filter: it stops NEW data arriving
    # and does nothing about the menu_*.json and promo_*.json already sitting in
    # state/scan from before the ruling. extract.py globs that directory whole,
    # so those stale files re-entered items.tsv every single run - and because an
    # excluded store is never re-fetched, they could never age out either. That
    # is how 658055 reached rank 2 of ranked.tsv. This is the authoritative
    # filter: applied here, before ranking, nothing from an excluded store can
    # reach ranked.tsv, ranked_pickup.tsv or verify_list.tsv whatever is on disk.
    excl_path = exclusions_path(d)
    excluded = load_exclusions(excl_path)
    dropped_ids = sorted(sid for sid in excluded if sid in items_by_store)
    dropped_items = sum(len(items_by_store.get(sid, [])) for sid in excluded)
    for sid in excluded:
        items_by_store.pop(sid, None)
        promos_by_store.pop(sid, None)
        stores.pop(sid, None)     # keeps the census counts below honest too

    cands = best_per_store(fm, items_by_store, promos_by_store)

    rows = build_rows(fm, cands, stores, fulfillment="delivery")
    write_tsv(os.path.join(d, "ranked.tsv"), RANKED_HEADER, rows)

    # The pickup lane. Estimates only - see pickup_lane()'s docstring for why a
    # delivery-only promo can put the real total above these numbers.
    pickup_cands = pickup_lane(fm, cands, stores)
    pickup_rows = build_rows(fm, pickup_cands, stores, fulfillment="pickup")
    write_tsv(os.path.join(d, "ranked_pickup.tsv"), PICKUP_HEADER, pickup_rows)

    # Select on rank_per_meal, the same number the ranking uses. Selecting on
    # per_meal_low would let a req_mods=1 item onto the list on the strength of a
    # base price defect 19 says may not be orderable at all, and would likewise
    # admit a fast-food-sandwich cart on a meal count that may really be half
    # what it looks like. rank_per_meal already falls back to the high bound in
    # both of those cases and equals per_meal_low everywhere else.
    verify = build_verify_list(rows, pickup_rows)
    write_tsv(os.path.join(d, "verify_list.tsv"), VERIFY_HEADER, verify)

    write_tsv(os.path.join(d, "unparsed_promos.tsv"),
              ["store_id", "campaign_id", "title", "description", "source"],
              [{"store_id": p.store_id, "campaign_id": p.campaign_id,
                "title": p.title, "description": p.description,
                "source": p.source} for p in unparsed])

    n_meals = sum(len(real_meals(v)[0]) for v in items_by_store.values())
    print(f"excluded stores        : {len(excluded)} ruled out in {excl_path}; "
          f"dropped {len(dropped_ids)} candidate store(s) / {dropped_items} stale "
          f"item row(s) from BOTH lanes before ranking"
          + (f" [{', '.join(dropped_ids)}]" if dropped_ids
             else " (none of them had data in this scan)"))
    print(f"stores with items      : {len(items_by_store)}")
    print(f"items                  : {len(items)}  real meals: {n_meals}")
    print(f"promos                 : {len(promo_rows)}  unparsed: {len(unparsed)}")
    print(f"promo state (defect 18): confirmed {promo_status_counts['confirmed']} "
          f"(move the predicted total), unconfirmed "
          f"{promo_status_counts['unconfirmed']} (UPSIDE annotation only), "
          f"refuted {promo_status_counts['refuted']} (dropped)")
    for p in unparsed:
        print(f"  UNPARSED store={p.store_id} title={p.title!r} desc={p.description[:60]!r}")
    print(f"order ceiling          : ${ORDER_CEILING:.2f} on the pre-tip order total "
          f"(high bound where fees are unknown)")
    print(f"ranked candidates      : {len(rows)} -> ranked.tsv")
    n_delivery = sum(1 for r in verify if r["fulfillment"] in ("delivery", "both"))
    n_pickup = sum(1 for r in verify if r["fulfillment"] in ("pickup", "both"))
    print(f"needing real preview   : {len(verify)} row(s) -> verify_list.tsv "
          f"(ranking per-meal < ${BEST_VERIFIED_TOTAL:.2f}); "
          f"{n_delivery} delivery preview(s), {n_pickup} pickup preview(s); "
          f"the fulfillment column says which, and 'both' means one cart "
          f"previewed twice")

    near = sum(1 for sid in stores if store_km(stores, sid) is not None
               and store_km(stores, sid) <= PICKUP_MAX_KM)
    print()
    print(f"PICKUP LANE (<= {PICKUP_MAX_KM:.1f} km, e-bike range from the anchor address)")
    print(f"  pickup-eligible stores : {near} of {len(stores)} in the census are "
          f"in range; {len(pickup_rows)} of them have a ranked candidate "
          f"-> ranked_pickup.tsv")
    print(f"  ESTIMATES ONLY: $0 service fee, no delivery fee, tax on food only, "
          f"but the promo math is the delivery lane's. A promo that says "
          f"'delivery only' is dropped here; one that is delivery-only without "
          f"saying so cannot be detected and will NOT attach on pickup, so a real "
          f"pickup preview can come back HIGHER than these numbers. Verify before "
          f"reporting any of them as prices.")
    for pr in pickup_rows[:5]:
        print(f"  {pr['rank']:>2}. ${pr['total_low']:>6} est pickup total  "
              f"${pr['rank_per_meal']:>6}/meal  {pr['distance_km']:>5} km  "
              f"(delivery ${pr['delivery_rank_per_meal']}/meal)  "
              f"{pr['store_name']} - {pr['item_name']}")
    if not pickup_rows:
        print("  (no candidate store is inside the radius with a known distance)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
