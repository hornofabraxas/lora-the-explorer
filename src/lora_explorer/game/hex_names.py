import hashlib

_ADJ1 = [
    "Ancient", "Ashen", "Bitter", "Black", "Blazing", "Bleak", "Blind",
    "Bold", "Bone", "Brazen", "Bright", "Broken", "Bronze", "Buried",
    "Burning", "Hushed", "Charred", "Cold", "Copper", "Coral", "Crimson",
    "Crooked", "Cruel", "Cursed", "Dark", "Dead", "Deep", "Dire",
    "Distant", "Dread", "Dusk", "Dusty", "Elder", "Ember", "Eternal",
    "Fading", "Fallen", "False", "Far", "Feral", "Fierce", "Final",
    "First", "Fell", "Forgotten", "Forsaken", "Foul", "Frozen", "Ghost",
    "Gilt", "Glass", "Golden", "Grim", "Umber", "Hallowed", "Harsh",
    "Haunted", "Hidden", "High", "Hollow", "Howling", "Gaunt", "Iron",
    "Ivory", "Jade", "Jagged", "Last", "Lone", "Long", "Lost", "Low",
    "Mad", "Misty", "Moon", "Mournful", "Murk", "Wan", "Night",
    "Noble", "North", "Old", "Pale", "Phantom", "Quiet", "Raven", "Riven",
    "Red", "Rogue", "Ragged", "Ruin", "Rusted", "Sacred", "Salt",
    "Savage", "Scarlet", "Shadow", "Shattered", "Silent", "Silver",
    "Skull", "Smoke", "Sorrow", "South", "Split", "Star", "Steel",
    "Still", "Stone", "Storm", "Stray", "Sunken", "Swift", "Thorn",
    "Thunder", "Tide", "Torn", "Twin", "Twisted", "Veiled", "Violet",
    "Void", "Wailing", "Wander", "War", "Wasted", "Weary", "West",
    "Wicked", "Wild", "Wind", "Winter", "Witch", "Wolf", "Worn",
    "Wraith", "Wrath",
]

_ADJ2 = [
    "Amber", "Barren", "Blue", "Brackish", "Broad", "Cedar", "Chalk",
    "Basalt", "Clear", "Cliff", "Cloud", "Cobalt", "Crag", "Crystal",
    "Cinder", "Dawn", "Onyx", "Dune", "Dust", "Elm", "Fern", "Flint",
    "Fog", "Granite", "Gray", "Green", "Haze", "Heath", "Sable",
    "Verdant", "Indigo", "Juniper", "Kelp", "Lichen", "Lime", "Linden",
    "Maple", "Marble", "Marsh", "Mist", "Moss", "Bramble", "Oak", "Ochre",
    "Olive", "Opal", "Palm", "Peat", "Pine", "Quartz", "Rain", "Reed",
    "Root", "Rose", "Rust", "Sage", "Sand", "Silt", "Slate", "Snow",
    "Spruce", "Thorn", "Timber", "White", "Willow", "Aspen", "Birch",
    "Bone", "Brine", "Char", "Copper", "Coral", "Dark", "Deep", "Drift",
    "Ember", "Frost", "Glass", "Gold", "Fallow", "Iron", "Ivory", "Jet",
    "Long", "Low", "Pale", "Pearl", "Salt", "Shade", "Shell", "Silk",
    "Silver", "Steep", "Stone", "Storm", "Tallow", "Tar", "Hoar", "Wind",
]

_NOUNS = [
    "Anchorage", "Arch", "Bank", "Barrow", "Basin", "Bay", "Beach",
    "Beacon", "Bend", "Bluff", "Bog", "Breach", "Bridge", "Brook",
    "Butte", "Cairn", "Canyon", "Cape", "Channel", "Cliff", "Cove",
    "Creek", "Crest", "Crossing", "Dell", "Den", "Depths", "Warren",
    "Drift", "Dune", "Edge", "Expanse", "Falls", "Fen", "Field",
    "Fjord", "Flat", "Ford", "Forge", "Spire", "Gate", "Glade", "Glen",
    "Gorge", "Grave", "Grotto", "Grove", "Gulf", "Gulch", "Harbor",
    "Haven", "Headland", "Heath", "Heights", "Hill", "Hollow", "Horn",
    "Isle", "Jetty", "Keep", "Knoll", "Lagoon", "Landing", "Lea",
    "Ledge", "Marsh", "Maw", "Mesa", "Moor", "Narrows", "Notch",
    "Oasis", "Outpost", "Pass", "Peak", "Pier", "Pinnacle", "Pit",
    "Plain", "Plateau", "Point", "Pool", "Port", "Precipice", "Quarry",
    "Quay", "Rapids", "Ravine", "Reach", "Reef", "Refuge", "Ridge",
    "Rift", "Rise", "Rock", "Roost", "Ruin", "Run", "Saddle", "Sands",
    "Scar", "Shelf", "Shore", "Shoal", "Crypt", "Sound", "Spit",
    "Springs", "Stand", "Steppe", "Strait", "Summit", "Swamp",
    "Thicket", "Tor", "Trail", "Trough", "Vale", "Valley", "Vault",
    "Verge", "Vista", "Wake", "Wall", "Waste", "Watch", "Well", "Wharf",
    "Wilds", "Woods", "Wreck",
]


def hex_name(hex_id: str) -> str:
    digest = hashlib.sha256(hex_id.encode()).digest()
    i1 = int.from_bytes(digest[0:4], "big") % len(_ADJ1)
    i2 = int.from_bytes(digest[4:8], "big") % len(_ADJ2)
    i3 = int.from_bytes(digest[8:12], "big") % len(_NOUNS)
    return f"{_ADJ1[i1]} {_ADJ2[i2]} {_NOUNS[i3]}"
