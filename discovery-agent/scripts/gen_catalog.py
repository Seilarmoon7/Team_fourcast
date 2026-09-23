"""Generate a 5,000-item Nordhem Home catalog as a Shopify product CSV (+ JSON for the agent).

Every item is classified by: Type (e.g. Sofas, Desks), room, style, color, material, price tier.
Deterministic (seeded) so re-running produces the same catalog and the same SKUs/handles.
"""
import csv, json, random, re

random.seed(20260923)
TARGET = 5000

STYLES = ["scandinavian", "mid-century", "boho", "minimalist", "modern", "industrial",
          "coastal", "traditional", "japandi", "farmhouse"]
STYLE_WORD = {"scandinavian": "Nordic", "mid-century": "Mid-Century", "boho": "Boho", "minimalist": "Minimal",
              "modern": "Modern", "industrial": "Industrial", "coastal": "Coastal", "traditional": "Classic",
              "japandi": "Japandi", "farmhouse": "Farmhouse"}
NAMES = """Aalto Alva Arne Asta Bergen Birk Bodil Bryn Dala Eira Elin Enok Espen Falk Fjord Frej Frida Gerd
Greta Halden Hanne Hedda Helle Hygge Idun Ingrid Isak Jarl Juni Kaja Kari Klara Lars Leif Liv Loke Lotta
Lund Lykke Maja Malin Mette Mira Nanna Njord Nora Odin Olav Ostra Pella Rune Saga Selma Sigrid Siri Skog
Solveig Stig Svea Tove Tyra Ulla Ulf Vala Vide Vika Ylva Yrsa Aske Brann Dagny Embla Fenja Gorm Hilde Inga
Kjell Linnea Mads Nils Oda Pia Ragna Sten Tor Unn Varg Viggo Asa Bo Eik Hav Kvist Lyng Mose Rim Sand Tang""".split()

# type: (room, category_level_1, singular, [materials], [colors], (min,max) price, [size/variant words], google category)
TYPES = {
 "Sofas":            ("living room", "Seating",  "Sofa",            ["linen","velvet","boucle","leather","cotton","wool"], ["oat","grey","sage","rust","navy","cream","charcoal","terracotta","mustard"], (549, 2890), ["2-Seater","3-Seater","Corner","Sleeper","Modular"], "Furniture > Sofas"),
 "Sectionals":       ("living room", "Seating",  "Sectional",       ["linen","velvet","boucle","leather","performance fabric"], ["oat","grey","sand","olive","charcoal","cream"], (1290, 4290), ["L-Shape","U-Shape","Chaise"], "Furniture > Sofas"),
 "Armchairs":        ("living room", "Seating",  "Armchair",        ["linen","velvet","boucle","leather","rattan","wool"], ["oat","rust","sage","mustard","ivory","teal","brown"], (249, 1290), ["Lounge","Wingback","Accent","Swivel"], "Furniture > Chairs > Arm Chairs, Recliners & Sleeper Chairs"),
 "Lounge Chairs":    ("living room", "Seating",  "Lounge Chair",    ["oak","walnut","rattan","leather","wool"], ["natural","walnut","black","cognac","cream"], (329, 1490), ["Low","Rocking","Easy"], "Furniture > Chairs"),
 "Ottomans & Poufs": ("living room", "Seating",  "Pouf",            ["wool","jute","leather","boucle","velvet"], ["natural","cream","rust","grey","olive","brown"], (69, 390), ["Round","Square","Knitted"], "Furniture > Ottomans"),
 "Coffee Tables":    ("living room", "Tables",   "Coffee Table",    ["oak","walnut","marble","travertine","glass","mango wood","ash"], ["natural","walnut","white","black","sand","smoked"], (149, 1290), ["Round","Oval","Rectangular","Nesting"], "Furniture > Tables > Accent Tables > Coffee Tables"),
 "Side Tables":      ("living room", "Tables",   "Side Table",      ["oak","walnut","marble","metal","rattan","mango wood"], ["natural","black","brass","white","walnut","terracotta"], (69, 490), ["Round","C-Shape","Pedestal","Tray"], "Furniture > Tables > Accent Tables > End Tables"),
 "Console Tables":   ("living room", "Tables",   "Console Table",   ["oak","walnut","metal","mango wood","marble"], ["natural","black","walnut","brass"], (189, 890), ["Slim","Two-Tier","Drawer"], "Furniture > Tables > Accent Tables > Sofa Tables"),
 "TV & Media Units": ("living room", "Storage",  "Media Console",   ["oak","walnut","ash","lacquered MDF","rattan"], ["natural","walnut","white","black","sage"], (249, 1490), ["Low","Wide","Fluted","Cane"], "Furniture > Entertainment Centers & TV Stands"),
 "Bookcases & Shelving": ("living room", "Storage", "Bookcase",     ["oak","walnut","metal","pine","ash"], ["natural","black","white","walnut"], (129, 990), ["Tall","Wide","Ladder","Wall-Mounted","Cube"], "Furniture > Shelving > Bookcases & Standing Shelves"),
 "Rugs":             ("living room", "Textiles", "Rug",             ["wool","jute","cotton","viscose","sisal"], ["rust","cream","grey","navy","sage","terracotta","natural","ivory","charcoal"], (79, 1190), ["5x8","6x9","8x10","9x12","Runner","Round"], "Home & Garden > Decor > Rugs"),
 "Floor Lamps":      ("living room", "Lighting", "Floor Lamp",      ["brass","rice paper","linen","metal","oak","rattan"], ["brass","black","white","natural","cream"], (89, 690), ["Arc","Tripod","Globe","Pleated","Reading"], "Home & Garden > Lighting > Lamps"),
 "Table Lamps":      ("living room", "Lighting", "Table Lamp",      ["ceramic","brass","glass","rice paper","stone"], ["cream","sand","sage","brass","white","terracotta","smoked"], (49, 390), ["Mushroom","Pleated","Globe","Dome","Mini"], "Home & Garden > Lighting > Lamps"),
 "Pendant Lights":   ("living room", "Lighting", "Pendant Light",   ["rattan","rice paper","glass","metal","linen"], ["natural","white","black","brass","smoked"], (69, 590), ["Large","Cluster","Dome","Lantern"], "Home & Garden > Lighting > Lighting Fixtures > Chandeliers"),
 "Throws & Blankets":("living room", "Textiles", "Throw",           ["wool","cotton","linen","boucle","cashmere blend"], ["cream","rust","oat","sage","charcoal","mustard","ivory"], (39, 290), ["Chunky Knit","Herringbone","Waffle","Fringed"], "Home & Garden > Linens & Bedding > Bedding > Blankets"),
 "Cushions":         ("living room", "Textiles", "Cushion Cover",   ["linen","velvet","boucle","wool","cotton"], ["rust","sage","cream","mustard","navy","terracotta","oat"], (19, 99), ["Square","Lumbar","Round","Set of 2"], "Home & Garden > Decor > Throw Pillows"),
 "Dining Tables":    ("dining room", "Tables",  "Dining Table",    ["oak","walnut","ash","marble","travertine","reclaimed pine"], ["natural","walnut","white","black","sand"], (390, 2890), ["4-Seat","6-Seat","8-Seat","Round","Extendable"], "Furniture > Tables > Kitchen & Dining Room Tables"),
 "Dining Chairs":    ("dining room", "Seating", "Dining Chair",    ["oak","walnut","beech","rattan","metal","velvet"], ["natural","black","walnut","sage","cream","rust"], (89, 590), ["Wishbone","Spindle","Upholstered","Cane","Set of 2"], "Furniture > Chairs > Kitchen & Dining Room Chairs"),
 "Dining Benches":   ("dining room", "Seating", "Dining Bench",    ["oak","walnut","pine","ash"], ["natural","walnut","black"], (190, 890), ["120cm","160cm","Upholstered"], "Furniture > Benches > Kitchen & Dining Benches"),
 "Bar Stools":       ("dining room", "Seating", "Bar Stool",       ["oak","metal","rattan","leather","walnut"], ["natural","black","brass","cognac","walnut"], (89, 490), ["Counter Height","Bar Height","Backless"], "Furniture > Chairs > Table & Bar Stools"),
 "Sideboards & Buffets": ("dining room", "Storage", "Sideboard",   ["oak","walnut","mango wood","ash","rattan"], ["natural","walnut","black","white","sage"], (390, 1990), ["Fluted","Cane","4-Door","Low"], "Furniture > Cabinets & Storage > Buffets & Sideboards"),
 "Tableware":        ("dining room", "Kitchen & Dining", "Dinner Set", ["stoneware","porcelain","ceramic"], ["cream","sand","sage","charcoal","white","terracotta"], (39, 290), ["12-Piece","16-Piece","Bowl Set","Plate Set"], "Home & Garden > Kitchen & Dining > Tableware"),
 "Beds":             ("bedroom", "Beds",        "Bed Frame",       ["oak","walnut","linen","velvet","rattan","metal"], ["natural","walnut","oat","grey","sage","black"], (490, 2490), ["Queen","King","Full","Platform","Canopy"], "Furniture > Beds & Accessories > Beds & Bed Frames"),
 "Headboards":       ("bedroom", "Beds",        "Headboard",       ["linen","velvet","boucle","rattan","oak"], ["oat","sage","rust","cream","natural"], (190, 790), ["Queen","King","Channel-Tufted","Arched"], "Furniture > Beds & Accessories > Headboards & Footboards"),
 "Nightstands":      ("bedroom", "Storage",     "Nightstand",      ["oak","walnut","ash","rattan","lacquered MDF"], ["natural","walnut","white","black","sage"], (99, 590), ["1-Drawer","2-Drawer","Floating","Open Shelf"], "Furniture > Tables > Nightstands"),
 "Dressers":         ("bedroom", "Storage",     "Dresser",         ["oak","walnut","ash","mango wood","pine"], ["natural","walnut","white","black"], (390, 1690), ["6-Drawer","4-Drawer","Tall","Wide"], "Furniture > Cabinets & Storage > Dressers"),
 "Wardrobes":        ("bedroom", "Storage",     "Wardrobe",        ["oak","walnut","pine","lacquered MDF"], ["natural","white","black","walnut"], (490, 2290), ["2-Door","3-Door","Sliding","Open"], "Furniture > Cabinets & Storage > Armoires & Wardrobes"),
 "Bedding":          ("bedroom", "Textiles",    "Duvet Cover Set", ["linen","cotton","percale","sateen"], ["white","oat","sage","rust","grey","navy","ivory"], (69, 390), ["Queen","King","Full","Twin"], "Home & Garden > Linens & Bedding > Bedding > Duvet Covers"),
 "Mirrors":          ("bedroom", "Decor",       "Mirror",          ["oak","brass","rattan","metal","travertine"], ["natural","brass","black","walnut","white"], (79, 690), ["Round","Arched","Full-Length","Wavy","Oval"], "Home & Garden > Decor > Mirrors"),
 "Desks":            ("home office", "Tables",  "Desk",            ["oak","walnut","ash","metal","pine"], ["natural","walnut","white","black"], (199, 1490), ["Writing","Standing","Corner","Compact","Executive"], "Furniture > Office Furniture > Desks"),
 "Office Chairs":    ("home office", "Seating", "Office Chair",    ["mesh","leather","boucle","oak","velvet"], ["black","grey","cognac","cream","sage"], (149, 990), ["Ergonomic","Task","Swivel","High-Back"], "Furniture > Office Furniture > Office Chairs"),
 "Filing & Storage": ("home office", "Storage", "Storage Cabinet", ["oak","metal","walnut","ash"], ["natural","black","white","sage","walnut"], (149, 790), ["2-Drawer","Mobile","Tall","Credenza"], "Furniture > Office Furniture > Filing Cabinets"),
 "Vases & Vessels":  ("living room", "Decor",   "Vase",            ["ceramic","stoneware","glass","terracotta"], ["cream","sand","sage","terracotta","smoked","black","white"], (19, 190), ["Tall","Bud","Amphora","Ribbed","Set of 3"], "Home & Garden > Decor > Vases"),
 "Wall Art":         ("living room", "Decor",   "Framed Print",    ["oak frame","black frame","canvas","linen"], ["natural","black","cream","sage","terracotta"], (49, 490), ["Abstract","Botanical","Landscape","Line Art","Set of 2"], "Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork"),
 "Candles & Holders":("living room", "Decor",   "Candle Holder",   ["brass","ceramic","glass","travertine"], ["brass","cream","smoked","black","sand"], (15, 120), ["Taper","Pillar","Hurricane","Set of 3"], "Home & Garden > Decor > Home Fragrance Accessories > Candle Holders"),
 "Plants & Planters":("living room", "Decor",   "Planter",         ["terracotta","ceramic","rattan","concrete","stoneware"], ["terracotta","cream","sage","charcoal","natural"], (19, 290), ["Small","Large","Footed","Hanging","Set of 3"], "Home & Garden > Decor > Planters"),
 "Curtains":         ("living room", "Textiles","Curtain Panel",   ["linen","cotton","velvet","sheer voile"], ["oat","white","sage","rust","grey","ivory"], (39, 290), ["84in","96in","108in","Blackout","Pair"], "Home & Garden > Decor > Window Treatments > Curtains & Drapes"),
 "Baskets & Storage":("living room", "Storage", "Storage Basket",  ["jute","seagrass","rattan","cotton rope","wicker"], ["natural","cream","black","brown"], (19, 190), ["Large","Lidded","Set of 3","Laundry"], "Home & Garden > Household Supplies > Storage & Organization > Storage Baskets"),
 "Outdoor Seating":  ("garden", "Outdoor",      "Outdoor Lounge Chair", ["teak","aluminium","rattan","rope"], ["natural","black","sand","olive","white"], (190, 1290), ["Lounge","Dining","Sun Lounger","Swing"], "Home & Garden > Lawn & Garden > Outdoor Living > Outdoor Furniture"),
 "Outdoor Tables":   ("garden", "Outdoor",      "Outdoor Table",   ["teak","aluminium","concrete","acacia"], ["natural","black","grey","white"], (190, 1490), ["Dining","Side","Coffee","Bistro"], "Home & Garden > Lawn & Garden > Outdoor Living > Outdoor Furniture"),
 "Outdoor Lighting": ("garden", "Outdoor",      "Outdoor Lantern", ["metal","rattan","glass","solar"], ["black","natural","brass","white"], (29, 290), ["Hanging","Solar","String","Floor"], "Home & Garden > Lighting > Outdoor Lighting"),
}
# weight = how many of each type (sofas/chairs/tables/desks deliberately well stocked)
WEIGHT = {t: 1.0 for t in TYPES}
WEIGHT.update({"Sofas": 2.2, "Armchairs": 1.8, "Dining Chairs": 1.8, "Coffee Tables": 1.6, "Dining Tables": 1.6,
               "Desks": 1.6, "Office Chairs": 1.4, "Beds": 1.5, "Rugs": 1.6, "Side Tables": 1.3, "Floor Lamps": 1.2,
               "Table Lamps": 1.2, "Nightstands": 1.2, "Bookcases & Shelving": 1.2, "Sideboards & Buffets": 1.1})
MAT_PRICE = {"walnut": 1.25, "marble": 1.3, "travertine": 1.35, "leather": 1.35, "velvet": 1.1, "boucle": 1.15,
             "teak": 1.2, "brass": 1.15, "cashmere blend": 1.4, "pine": 0.8, "mango wood": 0.9, "metal": 0.9,
             "jute": 0.85, "lacquered MDF": 0.8, "cotton": 0.9, "performance fabric": 1.05, "aluminium": 0.95}
STYLE_AFFINITY = {  # materials/colors that make a style assignment plausible
 "scandinavian": ["oak","ash","linen","wool","boucle","white","oat","cream","natural","beech","pine"],
 "mid-century": ["walnut","teak","leather","brass","mustard","rust","cognac","velvet"],
 "boho": ["rattan","jute","seagrass","terracotta","mango wood","wicker","rust","macrame","cane"],
 "minimalist": ["white","black","grey","metal","glass","lacquered MDF","concrete"],
 "modern": ["marble","glass","metal","velvet","black","smoked","boucle"],
 "industrial": ["metal","reclaimed pine","leather","black","charcoal","concrete"],
 "coastal": ["rattan","linen","white","navy","sand","seagrass","rope","teak"],
 "traditional": ["velvet","walnut","brass","navy","ivory","cotton"],
 "japandi": ["oak","ash","rice paper","stone","sand","charcoal","travertine","stoneware"],
 "farmhouse": ["pine","reclaimed pine","cotton","ceramic","cream","sage","wicker"],
}

def slug(s): return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def pick_style(mat, color):
    scores = {s: 1 + 3 * (mat in a) + 2 * (color in a) for s, a in STYLE_AFFINITY.items()}
    tot = sum(scores.values()); r = random.uniform(0, tot)
    for s, w in scores.items():
        r -= w
        if r <= 0: return s
    return "modern"

def describe(t, spec, name, variant, mat, color, style, room):
    singular = spec[2].lower()
    lines = {
      "Seating": f"Deep, supportive seating in {color} {mat}, built to be used every day.",
      "Tables": f"Solid {mat} with a {color} finish; sized for real life and easy to live with.",
      "Storage": f"Clean-lined {mat} storage in {color} that keeps the room calm.",
      "Lighting": f"Soft, warm light from a {color} {mat} shade — low glare, easy on the eyes.",
      "Textiles": f"Woven {mat} in {color}; adds texture and warmth without clutter.",
      "Beds": f"A {color} {mat} bed that feels like a hotel and looks like home.",
      "Decor": f"A finishing piece in {color} {mat} that pulls the room together.",
      "Kitchen & Dining": f"Everyday {mat} in {color}, dishwasher safe and made to stack.",
      "Outdoor": f"Weather-ready {mat} in {color} for patios, balconies and gardens.",
    }[spec[1]]
    return (f"<p>The {name} {variant} {singular} — {STYLE_WORD[style].lower()} {singular} for the {room}.</p>"
            f"<p>{lines}</p><ul><li>Style: {style}</li><li>Material: {mat}</li><li>Color: {color}</li>"
            f"<li>Room: {room}</li></ul>")

types = list(TYPES)
weights = [WEIGHT[t] for t in types]
counts = {t: 0 for t in types}
tw = sum(weights)
for t, w in zip(types, weights): counts[t] = int(TARGET * w / tw)
while sum(counts.values()) < TARGET: counts[random.choice(types)] += 1

items, titles = [], set()
for ti, t in enumerate(types):
    room, cat1, singular, mats, colors, (lo, hi), variants, gcat = TYPES[t]
    code = slug(t)[:4].rstrip("-") + f"{ti:02d}"
    n = 0
    while n < counts[t]:
        name = random.choice(NAMES); mat = random.choice(mats); color = random.choice(colors)
        variant = random.choice(variants); style = pick_style(mat, color)
        title = f"{name} {variant} {singular}" if random.random() < 0.55 else f"{name} {mat.title()} {singular}"
        title = f"{title} — {color.title()}"
        if title in titles: continue
        titles.add(title)
        base = random.uniform(lo, hi) * MAT_PRICE.get(mat, 1.0)
        price = max(9, round(base / 10) * 10 - 1) if base > 50 else round(base) - 0.01
        price = round(min(price, hi * 1.45), 2)
        tier = "budget" if price < lo + (hi - lo) * 0.3 else ("premium" if price > lo + (hi - lo) * 0.7 else "mid")
        n += 1
        sku = f"nh-{code}-{n:04d}"
        qty = 0 if random.random() < 0.04 else random.randint(3, 60)
        items.append({"sku": sku, "title": title, "handle": slug(title), "type": t, "category_level_1": cat1,
                      "room": room, "style": style, "color": color, "material": mat, "price": price,
                      "compare_at": round(price * random.choice([1.15, 1.2, 1.25]) , 2) if random.random() < 0.12 else None,
                      "price_tier": tier, "qty": qty, "google_category": gcat,
                      "description": describe(t, TYPES[t], name, variant, mat, color, style, room)})

assert len(items) == TARGET and len({i["handle"] for i in items}) == TARGET and len({i["sku"] for i in items}) == TARGET

HEAD = ["Handle","Title","Body (HTML)","Vendor","Product Category","Type","Tags","Published","Option1 Name","Option1 Value",
        "Variant SKU","Variant Grams","Variant Inventory Tracker","Variant Inventory Qty","Variant Inventory Policy",
        "Variant Fulfillment Service","Variant Price","Variant Compare At Price","Variant Requires Shipping","Variant Taxable",
        "Image Src","Image Position","Image Alt Text","Gift Card","SEO Title","SEO Description","Status"]
def row(i):
    tags = ", ".join([i["type"], i["category_level_1"], f"room:{i['room']}", f"style:{i['style']}", f"color:{i['color']}",
                      f"material:{i['material']}", f"tier:{i['price_tier']}", "nordhem"])
    plain = re.sub("<[^>]+>", " ", i["description"]); plain = re.sub(r"\s+", " ", plain).strip()
    return [i["handle"], i["title"], i["description"], "Nordhem Home", "", i["type"], tags, "TRUE",
            "Title", "Default Title", i["sku"], random.randint(500, 40000), "shopify", i["qty"], "deny", "manual",
            f"{i['price']:.2f}", f"{i['compare_at']:.2f}" if i["compare_at"] else "", "TRUE", "TRUE",
            "", "", "", "FALSE", i["title"], plain[:300], "active"]

# Shopify's importer handles ~15MB per file; split into 2 files to keep each import quick
for part, chunk in enumerate([items[:2500], items[2500:]], 1):
    with open(f"nordhem_products_part{part}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(HEAD)
        for i in chunk: w.writerow(row(i))
json.dump(items, open("nordhem_catalog_5000.json", "w"), indent=0)

import collections
print("total", len(items))
print(collections.Counter(i["room"] for i in items))
print(sorted(collections.Counter(i["type"] for i in items).items(), key=lambda x: -x[1])[:12])
print(collections.Counter(i["style"] for i in items))
