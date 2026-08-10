"""
Natural-language -> ETC Eos OSC translator.

Rule-based only. No LLM, no API calls, no network — given a spoken phrase (as text
from Siri dictation) it returns a small set of OSC messages plus a human-readable
confirmation that the Shortcut speaks back.

Design choices (see docs/OSC_REFERENCE.md for the full command reference):
  * Single targets use the explicit, immediate-execute OSC forms
    (e.g. /eos/chan/5/full, /eos/chan/5 = 50.0). These are stateless and don't
    depend on the console's command line.
  * Ranges / lists / relative changes use the command-line form /eos/cmd "...#"
    because explicit per-channel OSC can't express "1 thru 8" in one message.
  * Safety verbs (blackout, restore, etc.) fire a USER-DEFINED Eos macro number
    from config["macro_map"] — reversible and under the programmer's control.
  * Destructive command-line verbs are never emitted by this parser; relay.py
    also enforces a whitelist as a second line of defence.

Public API:
    result = parse(phrase: str, config: dict) -> ParseResult
    ParseResult.ok           -> bool
    ParseResult.messages     -> list[(address: str, args: list)]
    ParseResult.confirmation -> str   (spoken back to the user)
"""

import re
from dataclasses import dataclass, field


@dataclass
class ParseResult:
    ok: bool
    confirmation: str
    messages: list = field(default_factory=list)  # list of (address, [args])


# --------------------------------------------------------------------------- #
# Number-word handling                                                         #
# --------------------------------------------------------------------------- #

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMWORDS = set(_ONES) | set(_TEENS) | set(_TENS) | {"hundred", "thousand"}


def _run_to_int(words):
    """Convert a run of number-words (e.g. ['twenty','three']) to an int (23)."""
    total = 0
    current = 0
    for w in words:
        if w in _ONES:
            current += _ONES[w]
        elif w in _TEENS:
            current += _TEENS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w == "thousand":
            total += (current or 1) * 1000
            current = 0
    return total + current


def normalize_numbers(text: str) -> str:
    """Replace spelled-out number runs with digits ('channel five' -> 'channel 5')."""
    tokens = text.split()
    out = []
    run = []
    for tok in tokens:
        if tok in _NUMWORDS:
            run.append(tok)
        else:
            if run:
                out.append(str(_run_to_int(run)))
                run = []
            out.append(tok)
    if run:
        out.append(str(_run_to_int(run)))
    return " ".join(out)


# --------------------------------------------------------------------------- #
# Dictation normalization                                                      #
# --------------------------------------------------------------------------- #

def normalize_dictation(raw: str) -> str:
    """
    Canonicalize a dictated phrase before rule matching: lowercase, strip the
    punctuation dictation inserts, digits for number words, and map the
    transcription variants Siri/Shortcuts produce onto the words the rules
    expect — "Q"/"que"/"queue"/"Q10" -> "cue", "snake" -> "sneak",
    "black out" -> "blackout", "channel to" -> "channel 2", "1-8" -> "1 thru 8".
    """
    text = (raw or "").lower().strip()

    # -- punctuation: keep decimals ("10.5") and cue-list refs ("2/10"),
    #    drop everything else ("Go to cue 10." / "press blind!"). Commas are
    #    isolated as tokens first because a comma between numbers is a list
    #    ("channels 1, 3 and 5") and must survive until after number words
    #    are converted to digits.
    text = re.sub(r"(\d),(\d{3})\b", r"\1\2", text)        # "1,001" -> "1001"
    text = re.sub(r"\s*,\s*", " , ", text)
    text = re.sub(r"[!?;:]+", " ", text)
    text = re.sub(r"\.(?!\d)", " ", text)                  # periods except decimals
    text = text.replace("@", " at ")                       # "channel 5 @ 50"
    text = re.sub(r"(\d)\s*%", r"\1 percent", text)        # "75%" -> "75 percent"
    text = re.sub(r"\b(?:please|thanks|thank you)\b", " ", text)

    # -- spelled-out numbers -> digits; "10 point 5" -> "10.5"
    text = normalize_numbers(text)
    text = re.sub(r"(\d)\s+point\s+(\d)", r"\1.\2", text)

    # -- a comma between numbers is a list connector; drop the rest
    text = re.sub(r"(\d) , (?=\d)", r"\1 and ", text)      # "1, 3, 5" -> "1 and 3 and 5"
    text = re.sub(r"\s*,\s*", " ", text)

    # -- "cue" comes back as Q / que / queue, sometimes glued to its number
    text = re.sub(r"\b(?:queue|que|q)\b", "cue", text)
    text = re.sub(r"\b(?:queue|que|q|cue)\.?(?=\d)", "cue ", text)   # "Q10" -> "cue 10"
    text = re.sub(r"\bcue ?list\b", "cuelist", text)
    text = re.sub(r"\bgo 2 (?=cue)", "go to ", text)                 # "go 2 cue 5"

    # -- number homophones right after a target word ("cue to" = "cue two");
    #    plurals ("channels to full") are excluded by the \b word boundary
    targets = r"\b(cue|chan|channel|group|sub|submaster|macro|preset|address)"
    text = re.sub(targets + r"(\d)", r"\1 \2", text)       # "channel5" -> "channel 5"
    text = re.sub(targets + r" (?:to|too)\b", r"\1 2", text)
    text = re.sub(targets + r" (?:for|fore)\b", r"\1 4", text)
    text = re.sub(targets + r" won\b", r"\1 1", text)
    text = re.sub(targets + r" ate\b", r"\1 8", text)

    # -- ranges: "1-8" and "threw" dictation forms
    text = re.sub(r"(\d)\s*-\s*(\d)", r"\1 thru \2", text)
    text = re.sub(r"\bthrew\b", "thru", text)

    # -- Eos words dictation splits in two or mishears
    text = re.sub(r"\bsnake\b", "sneak", text)
    text = re.sub(r"\bmicro\b", "macro", text)
    text = re.sub(r"\bblack ?out\b", "blackout", text)
    text = re.sub(r"\bhigh light\b", "highlight", text)
    text = re.sub(r"\blow light\b", "lowlight", text)
    text = re.sub(r"\bsub[ -]master(s?)\b", r"submaster\1", text)
    text = re.sub(r"\bun (park|block)\b", r"un\1", text)

    # -- collapse multi-word Eos verbs into single tokens so they parse as one action
    text = re.sub(r"\b(?:rem|ram|rim)[ _-]?dim\b", "rem_dim", text)
    text = re.sub(r"\bmake manual\b", "make_manual", text)

    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Vocabulary tables                                                            #
# --------------------------------------------------------------------------- #

# target keyword -> (osc path segment, Eos command-line keyword)
_TARGETS = [
    (re.compile(r"\b(?:channels?|chans?|ch)\b"), ("chan", "Chan")),
    (re.compile(r"\b(?:groups?|grp)\b"), ("group", "Group")),
    (re.compile(r"\b(?:submasters?|subs?)\b"), ("sub", "Sub")),
    (re.compile(r"\b(?:addresses|address|addr)\b"), ("addr", "Address")),
]

# approximate RGB (0..1). Documented as starting points; tune to taste.
_COLORS = {
    "red": (1.0, 0.0, 0.0), "green": (0.0, 1.0, 0.0), "blue": (0.0, 0.0, 1.0),
    "white": (1.0, 1.0, 1.0), "amber": (1.0, 0.75, 0.0), "orange": (1.0, 0.5, 0.0),
    "yellow": (1.0, 1.0, 0.0), "cyan": (0.0, 1.0, 1.0), "magenta": (1.0, 0.0, 1.0),
    "pink": (1.0, 0.4, 0.7), "purple": (0.5, 0.0, 0.5), "lavender": (0.6, 0.4, 0.8),
    "teal": (0.0, 0.5, 0.5), "warm": (1.0, 0.8, 0.6), "cool": (0.6, 0.8, 1.0),
}

_RANGE_WORDS = {"through", "thru", "-"}
_LIST_WORDS = {"and", "plus", "+", "&"}

# default console key names (overridable via config["key_map"])
_DEFAULT_KEYS = {"go": "go_0", "stop_back": "stop_back_main_cuelist"}

# Moving-light / parameter names recognised in natural phrasing
# (spoken word -> Eos parameter name used in OSC /param/<name> and command line).
_PARAMS = {
    "pan": "pan", "tilt": "tilt", "zoom": "zoom", "iris": "iris",
    "edge": "edge", "focus": "focus", "frost": "frost", "diffusion": "diffusion",
    "gobo": "gobo", "hue": "hue", "saturation": "saturation", "cto": "cto",
    "red": "red", "green": "green", "blue": "blue", "white": "white",
    "amber": "amber", "cyan": "cyan", "magenta": "magenta", "yellow": "yellow",
}
_PARAM_RX = re.compile(
    r"\b(" + "|".join(sorted(_PARAMS, key=len, reverse=True)) +
    r")\s+(-?\d+(?:\.\d+)?)\b")
# Bare parameter on the *current* selection: "gobo 3", "pan 50", "iris 20".
_BARE_PARAM_RX = re.compile(
    r"(" + "|".join(sorted(_PARAMS, key=len, reverse=True)) +
    r")\s+(-?\d+(?:\.\d+)?)")

# Action verbs that operate on a selection and take NO numeric value. Spoken
# word -> Eos command-line token. These reach commands that have no dedicated
# OSC address, by typing them onto the Eos command line (e.g. "Chan 5 Sneak#").
# Multi-word forms ("rem dim") are normalized to a single token in parse().
_ACTIONS = {
    "sneak": "Sneak", "highlight": "Highlight", "lowlight": "Lowlight",
    "park": "Park", "unpark": "Unpark", "mark": "Mark",
    "block": "Block", "unblock": "Unblock", "assert": "Assert",
    "capture": "Capture", "release": "Release", "query": "Query",
    "rem_dim": "Rem_Dim", "make_manual": "Make_Manual",
}

# Raw command-line passthrough: multi-word spoken phrases -> Eos tokens.
# Applied (longest first) before single-word translation in _spoken_to_cmd().
_CMD_PHRASES = [
    ("go to cue", "Go_To_Cue"), ("color palette", "Color_Palette"),
    ("colour palette", "Color_Palette"), ("intensity palette", "Intensity_Palette"),
    ("focus palette", "Focus_Palette"), ("beam palette", "Beam_Palette"),
    ("record only", "Record_Only"), ("cue only", "Cue_Only"),
    ("rem dim", "Rem_Dim"), ("make manual", "Make_Manual"),
    ("mark", "Mark"), ("about", "About"), ("undo", "Undo"),
]
_CMD_WORDS = {
    "channel": "Chan", "channels": "Chan", "chan": "Chan", "ch": "Chan",
    "group": "Group", "groups": "Group", "sub": "Sub", "submaster": "Sub",
    "subs": "Sub", "cue": "Cue", "preset": "Preset", "macro": "Macro",
    "address": "Address", "addr": "Address", "effect": "Effect", "fx": "Effect",
    "snapshot": "Snapshot", "curve": "Curve", "fixture": "Fixture",
    "thru": "Thru", "through": "Thru", "to": "Thru",
    "at": "At", "full": "Full", "out": "Out", "home": "Home",
    "and": "+", "plus": "+", "minus": "-",
    "record": "Record", "update": "Update", "delete": "Delete",
    "copy": "Copy", "move": "Move", "label": "Label", "block": "Block",
    "unblock": "Unblock", "assert": "Assert", "capture": "Capture",
    "release": "Release", "query": "Query",
    "sneak": "Sneak", "highlight": "Highlight", "lowlight": "Lowlight",
    "park": "Park", "unpark": "Unpark", "mark": "Mark",
    "intensity": "Intensity", "color": "Color", "colour": "Color",
    "focus": "Focus", "beam": "Beam", "time": "Time", "fan": "Fan",
    "enter": "#", "go": "Go", "stop": "Stop", "back": "Back",
    "next": "Next", "last": "Last", "select": "Select",
    "pan": "Pan", "tilt": "Tilt", "zoom": "Zoom", "iris": "Iris",
    "edge": "Edge", "frost": "Frost", "gobo": "Gobo", "hue": "Hue",
    "saturation": "Saturation", "diffusion": "Diffusion", "cto": "CTO",
    "red": "Red", "green": "Green", "blue": "Blue", "white": "White",
    "amber": "Amber", "cyan": "Cyan", "magenta": "Magenta", "yellow": "Yellow",
}

# Words that signal a genuine Eos command. Used to decide whether an otherwise
# unrecognized phrase should be passed through to the Eos command line as a
# fallback (so the FULL command set is reachable), rather than rejected.
_CMD_TRIGGERS = (
    {w for w, t in _CMD_WORDS.items() if t not in ("+", "-", "#", "Thru", "At")}
    | set(_ACTIONS)
    | set(_PARAMS)
    | {phrase.split()[0] for phrase, _ in _CMD_PHRASES}
)


def _has_eos_keyword(text: str) -> bool:
    """True if any whitespace token in `text` is a recognized Eos command word."""
    return any(tok in _CMD_TRIGGERS for tok in text.split())


def _spoken_to_cmd(spoken: str) -> str:
    """Translate a spoken Eos command into a command-line string for /eos/cmd."""
    s = " " + spoken.lower().strip() + " "
    for phrase, token in _CMD_PHRASES:
        s = s.replace(" " + phrase + " ", " " + token + " ")
    out = []
    for tok in s.split():
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?", tok):
            out.append(tok)                      # leave numbers (and list/cue refs)
        elif tok in _CMD_WORDS:
            out.append(_CMD_WORDS[tok])
        elif tok and tok[0].isupper():
            out.append(tok)                      # already-translated token
        else:
            out.append(tok.capitalize())         # best-effort for unknown words
    cmd = " ".join(out).strip()
    if not cmd.endswith("#"):
        cmd += "#"
    return cmd


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _first_int(tokens):
    for t in tokens:
        if re.fullmatch(r"\d+", t):
            return int(t)
    return None


# --------------------------------------------------------------------------- #
# Level / action parsing                                                       #
# --------------------------------------------------------------------------- #

# Siri frequently mis-dictates "palette" as "pallet", "palate", "pallette", etc.
# Match the common variants so a spoken palette recall still lands as a palette
# instead of falling through and being read as an intensity level.
_PAL = r"pal+[ae]t+e?s?"


def _parse_level(level_tokens):
    """
    Interpret the part of a phrase after the selector.
    Returns a (kind, value) tuple or None if nothing usable was found:
      ("named",  "full"|"out"|"home"|"min"|"max")
      ("level",  int 0..100)
      ("rel",    int)   relative +N  (negative for down)
      ("color",  (r,g,b))
      ("cp",     int)   color palette number
      ("ip"/"fp"/"bp", int)   intensity/focus/beam palette number
    """
    lv = " ".join(level_tokens)
    if not lv:
        return None
    # palettes: "color palette 2", "cp 2", "focus palette 3", "fp 3", etc.
    m = re.search(rf"\b(?:colou?r[ _]*(?:{_PAL})|cp)\s*(\d+)", lv)
    if m:
        return ("cp", int(m.group(1)))
    m = re.search(rf"\b(?:intensity[ _]*(?:{_PAL})|ip)\s*(\d+)", lv)
    if m:
        return ("ip", int(m.group(1)))
    m = re.search(rf"\b(?:focus[ _]*(?:{_PAL})|fp)\s*(\d+)", lv)
    if m:
        return ("fp", int(m.group(1)))
    m = re.search(rf"\b(?:beam[ _]*(?:{_PAL})|bp)\s*(\d+)", lv)
    if m:
        return ("bp", int(m.group(1)))
    # moving-light parameter: "pan 50", "tilt -20", "zoom 75", "red 100" ...
    m = _PARAM_RX.search(lv)
    if m:
        return ("param", (_PARAMS[m.group(1)], float(m.group(2))))
    # bare action verb following a selection: "channel 5 sneak", "group 2 mark"
    for word, tok in _ACTIONS.items():
        if re.search(rf"\b{re.escape(word)}\b", lv):
            return ("action", tok)
    if "full" in lv:
        return ("named", "full")
    if re.search(r"\b(out|off|zero)\b", lv):
        return ("named", "out")
    if "home" in lv:
        return ("named", "home")
    if re.search(r"\bhalf\b", lv):
        return ("level", 50)
    if re.search(r"\b(minimum|min)\b", lv):
        return ("named", "min")
    if re.search(r"\b(maximum|max)\b", lv):
        return ("named", "max")
    for name, rgb in _COLORS.items():
        if re.search(rf"\b{name}\b", lv):
            return ("color", rgb)
    if re.search(r"\b(up|raise|increase)\b", lv):
        n = _first_int(level_tokens)
        return ("rel", n if n is not None else 10)
    if re.search(r"\b(down|lower|decrease)\b", lv):
        n = _first_int(level_tokens)
        return ("rel", -(n if n is not None else 10))
    n = _first_int(level_tokens)
    if n is not None:
        return ("level", _clamp(n, 0, 100))
    return None


def _selector_and_rest(tokens):
    """
    From tokens after the target keyword, pull the selector.
    Returns (selector_dict, rest_tokens) or (None, _) if no leading number.
      selector_dict = {"kind": "single"|"range"|"list", "nums": [...]}
    """
    if not tokens or not re.fullmatch(r"\d+", tokens[0]):
        return None, tokens
    nums = [int(tokens[0])]
    i = 1
    kind = "single"
    while i < len(tokens):
        conn = tokens[i]
        if conn in _RANGE_WORDS and i + 1 < len(tokens) and re.fullmatch(r"\d+", tokens[i + 1]):
            kind = "range"
            nums.append(int(tokens[i + 1]))
            i += 2
            break  # a range is exactly two endpoints
        elif conn in _LIST_WORDS and i + 1 < len(tokens) and re.fullmatch(r"\d+", tokens[i + 1]):
            kind = "list"
            nums.append(int(tokens[i + 1]))
            i += 2
        else:
            break
    return {"kind": kind, "nums": nums}, tokens[i:]


# --------------------------------------------------------------------------- #
# OSC builders                                                                 #
# --------------------------------------------------------------------------- #

def _named_pct(level):
    return {"full": 100, "out": 0, "home": None, "min": None, "max": None}.get(level)


def _build_single(path, kw, n, level, label):
    """Build OSC for a single channel/group/sub/address target."""
    kind, val = level
    pretty_n = n

    # Action verbs (Sneak, Mark, Highlight, ...) have no dedicated OSC address;
    # type them onto the command line after the selection ("Chan 5 Sneak#").
    if kind == "action":
        return ([("/eos/cmd", [f"{kw} {n} {val}#"])],
                f"{label} {pretty_n} {val.replace('_', ' ').lower()}")

    if path == "sub":
        # submasters use 0.0..1.0
        if kind == "named" and val in ("full", "out"):
            return [(f"/eos/sub/{n}/{val}", [])], f"{label} {pretty_n} {val}"
        if kind == "level":
            return [(f"/eos/sub/{n}", [float(val) / 100.0])], f"{label} {pretty_n} to {val} percent"
        if kind == "rel":
            sign = "+" if val >= 0 else "-"
            return ([("/eos/cmd", [f"Sub {n} At {sign}{abs(val)}#"])],
                    f"{label} {pretty_n} {('up' if val>=0 else 'down')} {abs(val)}")
        return None, f"Submasters don't support that"

    # chan / group / addr (0..100)
    if kind == "named":
        return [(f"/eos/{path}/{n}/{val}", [])], f"{label} {pretty_n} {val}"
    if kind == "level":
        return [(f"/eos/{path}/{n}", [float(val)])], f"{label} {pretty_n} to {val} percent"
    if kind == "rel":
        sign = "+" if val >= 0 else "-"
        return ([("/eos/cmd", [f"{kw} {n} At {sign}{abs(val)}#"])],
                f"{label} {pretty_n} {('up' if val>=0 else 'down')} {abs(val)}")
    if kind == "color":
        # Channels expose an explicit color/rgb address (Eos gamut-maps it).
        # Groups do NOT (they only have /param/...), so use the command line.
        if path == "chan":
            r, g, b = val
            return ([(f"/eos/chan/{n}/color/rgb", [float(r), float(g), float(b)])],
                    f"{label} {pretty_n} color set")
        if path == "group":
            R, G, B = (int(round(x * 100)) for x in val)
            return ([("/eos/cmd", [f"Group {n} Red {R} Green {G} Blue {B}#"])],
                    f"{label} {pretty_n} color set")
        return None, "Color is only available on channels and groups"
    if kind in ("cp", "ip", "fp", "bp"):
        if path not in ("chan", "group"):
            return None, "Palettes apply to channels or groups"
        pal = {"cp": "Color_Palette", "ip": "Intensity_Palette",
               "fp": "Focus_Palette", "bp": "Beam_Palette"}[kind]
        pretty = {"cp": "color", "ip": "intensity", "fp": "focus", "bp": "beam"}[kind]
        return ([("/eos/cmd", [f"{kw} {n} {pal} {val}#"])],
                f"{label} {pretty_n} {pretty} palette {val}")
    if kind == "param":
        pname, pval = val
        if path not in ("chan", "group"):
            return None, "Parameters apply to channels or groups"
        return ([(f"/eos/{path}/{n}/param/{pname}", [float(pval)])],
                f"{label} {pretty_n} {pname} {pval:g}")
    return None, "Unrecognized level"


def _build_multi(kw, sel, level, label):
    """Build a single /eos/cmd command-line string for ranges and lists."""
    nums = sel["nums"]
    if sel["kind"] == "range":
        sel_str = f"{nums[0]} Thru {nums[1]}"
        sel_pretty = f"{nums[0]} through {nums[1]}"
    else:  # list
        sel_str = " + ".join(str(x) for x in nums)
        sel_pretty = " and ".join(str(x) for x in nums)

    kind, val = level
    if kind == "action":
        return ([("/eos/cmd", [f"{kw} {sel_str} {val}#"])],
                f"{label}s {sel_pretty} {val.replace('_', ' ').lower()}")
    if kind == "named":
        if val in ("full", "out", "home", "min", "max"):
            return ([("/eos/cmd", [f"{kw} {sel_str} At {val.capitalize()}#"])],
                    f"{label}s {sel_pretty} {val}")
        return None, "Unsupported level for a range"
    if kind == "level":
        return ([("/eos/cmd", [f"{kw} {sel_str} At {val}#"])],
                f"{label}s {sel_pretty} at {val} percent")
    if kind == "rel":
        sign = "+" if val >= 0 else "-"
        return ([("/eos/cmd", [f"{kw} {sel_str} At {sign}{abs(val)}#"])],
                f"{label}s {sel_pretty} {'up' if val>=0 else 'down'} {abs(val)}")
    if kind == "color":
        R, G, B = (int(round(x * 100)) for x in val)
        return ([("/eos/cmd", [f"{kw} {sel_str} Red {R} Green {G} Blue {B}#"])],
                f"{label}s {sel_pretty} color set")
    if kind in ("cp", "ip", "fp", "bp"):
        pal = {"cp": "Color_Palette", "ip": "Intensity_Palette",
               "fp": "Focus_Palette", "bp": "Beam_Palette"}[kind]
        pretty = {"cp": "color", "ip": "intensity", "fp": "focus", "bp": "beam"}[kind]
        return ([("/eos/cmd", [f"{kw} {sel_str} {pal} {val}#"])],
                f"{label}s {sel_pretty} {pretty} palette {val}")
    if kind == "param":
        pname, pval = val
        return ([("/eos/cmd", [f"{kw} {sel_str} {pname.capitalize()} {pval:g}#"])],
                f"{label}s {sel_pretty} {pname} {pval:g}")
    return None, "Unsupported for a range"


# --------------------------------------------------------------------------- #
# Top-level parse                                                              #
# --------------------------------------------------------------------------- #

def parse(phrase: str, config: dict) -> ParseResult:
    raw = (phrase or "").strip()
    if not raw:
        return ParseResult(False, "I didn't catch a command.")

    text = normalize_dictation(raw)
    if not text:
        return ParseResult(False, "I didn't catch a command.")

    macro_map = {k.lower(): v for k, v in (config.get("macro_map") or {}).items()}
    key_map = {**_DEFAULT_KEYS, **(config.get("key_map") or {})}

    # 1) user-defined named macros (blackout, restore, preshow, ...)
    for name, num in macro_map.items():
        if text == name or text.startswith(name + " ") or text.endswith(" " + name):
            return ParseResult(True, name.capitalize(),
                               [(f"/eos/macro/{num}/fire", [1.0])])

    # 2) connectivity test
    if text in ("ping", "test", "are you there"):
        return ParseResult(True, "Pong — relay is connected.",
                           [("/eos/ping", ["voice-relay"])])

    # 2b) RAW COMMAND-LINE PASSTHROUGH — say "command ..." / "console ..." / "raw ..."
    #     and the rest is translated straight into an Eos command line. This reaches
    #     ANY command the console has. (Destructive verbs are gated by the relay's
    #     destructive_policy.)
    m = re.fullmatch(r"(?:command|console|raw)\s+(.+)", text)
    if m:
        cmd = _spoken_to_cmd(m.group(1))
        return ParseResult(True, f"Command: {cmd.rstrip('#')}", [("/eos/cmd", [cmd])])

    # 3) playback transport
    if text in ("go", "go cue", "press go"):
        key = key_map["go"]
        return ParseResult(True, "Go",
                           [(f"/eos/key/{key}", [1.0]), (f"/eos/key/{key}", [0.0])])
    if re.fullmatch(r"(stop|back|stop back|hold|pause)", text):
        key = key_map["stop_back"]
        return ParseResult(True, "Stop/Back",
                           [(f"/eos/key/{key}", [1.0]), (f"/eos/key/{key}", [0.0])])

    # 3b) PRESS ANY KEY — "press live", "press blind", "press highlight", "press clear"
    m = re.fullmatch(r"(?:press|key|hit|push) (.+)", text)
    if m:
        spoken_key = m.group(1).strip()
        key = spoken_key.replace(" ", "_")
        return ParseResult(True, f"Press {spoken_key}",
                           [(f"/eos/key/{key}", [1.0]), (f"/eos/key/{key}", [0.0])])

    # 4a) GO TO CUE — jump to a cue with its recorded timing (Eos "Go_To_Cue").
    #     "go to cue 10", "goto cue 10.5", "jump to cue 3", "load cue 7"
    #     optional list: "go to cue 10 in list 2"
    m = re.fullmatch(
        r"(?:go ?to|goto|jump to|load) cue ([\d.]+)"
        r"(?: (?:in |on )?(?:list|cuelist) (\d+))?", text)
    if m:
        cue, lst = m.group(1), m.group(2)
        target = f"{lst}/{cue}" if lst else cue
        pretty = f"{cue} in list {lst}" if lst else cue
        return ParseResult(True, f"Go to cue {pretty}",
                           [("/eos/cmd", [f"Go_To_Cue {target}#"])])

    # 4b) FIRE CUE — assert a cue immediately. "fire cue 10", "cue 10", "cue 5 in list 2"
    m = re.fullmatch(r"(?:fire |run )?cue ([\d.]+) (?:in )?(?:list|cuelist) (\d+)", text)
    if m:
        cue, lst = m.group(1), m.group(2)
        return ParseResult(True, f"Fired cue {cue} in list {lst}",
                           [(f"/eos/cue/{lst}/{cue}/fire", [])])
    m = re.fullmatch(r"(?:fire |run )?cue ([\d.]+)(?:\s+go)?", text)
    if m:
        cue = m.group(1)
        return ParseResult(True, f"Fired cue {cue}",
                           [(f"/eos/cue/{cue}/fire", [])])

    # 4c) bare palette recall onto the CURRENT selection (no target named):
    #     "color palette 2", "cp 2", "focus palette 3", "fp 3", ...
    m = re.fullmatch(
        rf"(?:(colou?r|intensity|focus|beam)[ _]*(?:{_PAL})|(cp|ip|fp|bp))\s*(\d+)", text)
    if m:
        kindword = m.group(1) or m.group(2)
        num = m.group(3)
        pal = {"color": "Color_Palette", "colour": "Color_Palette",
               "intensity": "Intensity_Palette", "focus": "Focus_Palette",
               "beam": "Beam_Palette", "cp": "Color_Palette",
               "ip": "Intensity_Palette", "fp": "Focus_Palette",
               "bp": "Beam_Palette"}[kindword]
        return ParseResult(True, f"{pal.replace('_', ' ')} {num}",
                           [("/eos/cmd", [f"{pal} {num}#"])])

    # 5) fire macro / preset / palettes
    m = re.fullmatch(r"(?:fire |run )?macro (\d+)", text)
    if m:
        return ParseResult(True, f"Fired macro {m.group(1)}",
                           [(f"/eos/macro/{m.group(1)}/fire", [1.0])])
    m = re.fullmatch(r"(?:fire |recall )?preset (\d+)", text)
    if m:
        return ParseResult(True, f"Recalled preset {m.group(1)}",
                           [(f"/eos/preset/{m.group(1)}/fire", [1.0])])

    # 6) bump a submaster ("bump sub 4")
    m = re.fullmatch(r"bump (?:sub|submaster) (\d+)", text)
    if m:
        n = m.group(1)
        return ParseResult(True, f"Bumped submaster {n}",
                           [(f"/eos/sub/{n}/fire", [1.0]), (f"/eos/sub/{n}/fire", [0.0])])

    # 6b) BARE COMMANDS on the CURRENT selection (no target named) — these drive
    #     the full Eos vocabulary on whatever channels are already selected:
    #       action verbs: "sneak", "highlight", "mark", "park", "rem dim", ...
    #       parameters:   "gobo 3", "pan 50", "iris 20", "zoom 75"
    #       levels:       "full", "out", "home", "at 50", "75 percent"
    if text in _ACTIONS:
        tok = _ACTIONS[text]
        return ParseResult(True, tok.replace("_", " "), [("/eos/cmd", [f"{tok}#"])])
    m = _BARE_PARAM_RX.fullmatch(text)
    if m:
        pname, pval = _PARAMS[m.group(1)], m.group(2)
        return ParseResult(True, f"{pname} {pval}",
                           [("/eos/cmd", [f"{pname.capitalize()} {pval}#"])])
    m = re.fullmatch(r"(?:at )?(full|out|home|min|max)", text)
    if m:
        return ParseResult(True, m.group(1), [(f"/eos/at/{m.group(1)}", [])])
    m = re.fullmatch(r"at (\d+)(?: percent| %)?|(\d+) (?:percent|%)", text)
    if m:
        val = _clamp(int(m.group(1) or m.group(2)), 0, 100)
        return ParseResult(True, f"at {val} percent", [("/eos/at", [float(val)])])

    # 7) target + selector + level  (channels / groups / subs / addresses)
    for rx, (path, kw) in _TARGETS:
        m = rx.search(text)
        if not m:
            continue
        before = text[:m.start()].strip()
        after = text[m.end():].strip().split()
        sel, rest = _selector_and_rest(after)
        if sel is None:
            return ParseResult(False, f"I heard '{path}' but no number.")
        level = _parse_level(rest)
        if level is None and before:
            # the level/verb may have been spoken BEFORE the target:
            #   "sneak channel 5", "full group 2", "blue channel 7"
            level = _parse_level(before.split())
        if level is None:
            return ParseResult(False,
                               f"I heard '{' '.join([m.group(0)] + after)}' but no level.")
        label = {"chan": "Channel", "group": "Group",
                 "sub": "Submaster", "addr": "Address"}[path]
        if sel["kind"] == "single":
            msgs, conf = _build_single(path, kw, sel["nums"][0], level, label)
        else:
            msgs, conf = _build_multi(kw, sel, level, label)
        if msgs is None:
            return ParseResult(False, conf)
        return ParseResult(True, conf, msgs)

    # 8) FALLBACK — no specific rule matched, but if the phrase contains a
    #    recognized Eos command word, translate the whole thing onto the command
    #    line. This makes the ENTIRE Eos command set reachable by voice (any verb,
    #    target, or parameter the console understands) without the "command"
    #    prefix. The relay's destructive_policy still gates dangerous verbs.
    if _has_eos_keyword(text):
        cmd = _spoken_to_cmd(text)
        return ParseResult(True, f"Command: {cmd.rstrip('#')}", [("/eos/cmd", [cmd])])

    return ParseResult(
        False,
        "Sorry, I didn't understand that. Try things like "
        "'channel 5 at full', 'group 3 at 50 percent', 'sneak', or 'go'.",
    )
