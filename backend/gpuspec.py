"""backend/gpuspec.py — how many FLOP/s a card can actually do.

Nothing in the monitoring stack reports FLOPS. `nvidia-smi` reports utilisation
— the fraction of time *any* kernel was resident — which tells you the card is
busy and nothing at all about how much arithmetic it is getting through. There
is no counter for achieved FLOP/s outside a profiler.

So what this module publishes is **theoretical peak**, computed from the one
formula every vendor agrees on:

    FP32 FLOP/s = 2 × shader cores × clock

The only input the driver won't give us is the shader-core count, so that comes
from a table of published specifications below. Two consequences, both
deliberate:

* **A card that isn't in the table publishes nothing.** Absent, not zero — the
  same rule the rest of this codebase applies to metrics a driver can't report.
  A fabricated 0 TFLOPS would read as "this card does no work".
* **Laptop and Max-Q parts never match a desktop entry.** "RTX 4090" and
  "RTX 4090 Laptop GPU" are different silicon (16384 vs 9728 cores) at
  different clocks. Names that look mobile are refused rather than guessed at,
  because a plausible-looking wrong number is worse than a blank.

Each entry stores the vendor's *rated* peak at the vendor's *rated* boost clock
rather than a cores×clock derivation done here, because the derivation is not
uniform across architectures: RDNA 3 dual-issues FP32 (AMD's quoted 61.4 TFLOPS
for a 7900 XTX is twice what cores×clock gives), and GeForce tensor cores halve
their FP32-accumulate rate against the datacentre parts. Storing the published
figure keeps this module honest without encoding an architecture matrix.

`fp16` is dense FP16 tensor throughput — no sparsity doubling — because that is
the number that governs LLM inference, and vendors love to quote the sparse one.
It is absent on cards with no tensor cores (Pascal, GCN, RDNA 1/2), which is a
fact worth showing rather than a gap worth filling.
"""
import re

# name → (shader cores, rated boost MHz, peak FP32 TFLOPS, dense FP16 tensor
# TFLOPS or None). Keys are already normalised by _norm(): lower case, vendor
# and marketing words stripped, single-spaced.
SPECS = {
    # ── NVIDIA GeForce: Blackwell ────────────────────────────────────────────
    "rtx 5090":        (21760, 2407, 104.8, 419.0),
    "rtx 5080":        (10752, 2617,  56.3, 225.0),
    "rtx 5070 ti":     (8960,  2452,  43.9, 176.0),
    "rtx 5070":        (6144,  2512,  30.9, 123.0),
    "rtx 5060 ti":     (4608,  2572,  23.7,  94.8),
    # ── NVIDIA GeForce: Ada Lovelace ─────────────────────────────────────────
    "rtx 4090":        (16384, 2520,  82.6, 165.2),
    "rtx 4080 super":  (10240, 2550,  52.2, 104.4),
    "rtx 4080":        (9728,  2505,  48.7,  97.5),
    "rtx 4070 ti super": (8448, 2610,  44.1,  88.2),
    "rtx 4070 ti":     (7680,  2610,  40.1,  80.2),
    "rtx 4070 super":  (7168,  2475,  35.5,  70.9),
    "rtx 4070":        (5888,  2475,  29.1,  58.3),
    "rtx 4060 ti":     (4352,  2535,  22.1,  44.1),
    "rtx 4060":        (3072,  2460,  15.1,  30.2),
    # ── NVIDIA GeForce: Ampere ───────────────────────────────────────────────
    "rtx 3090 ti":     (10752, 1860,  40.0,  80.0),
    "rtx 3090":        (10496, 1695,  35.6,  71.0),
    "rtx 3080 ti":     (10240, 1665,  34.1,  68.2),
    "rtx 3080":        (8704,  1710,  29.8,  59.5),
    "rtx 3070 ti":     (6144,  1770,  21.7,  43.5),
    "rtx 3070":        (5888,  1725,  20.3,  40.6),
    "rtx 3060 ti":     (4864,  1665,  16.2,  32.4),
    "rtx 3060":        (3584,  1777,  12.7,  25.5),
    "rtx 3050":        (2560,  1777,   9.1,  18.2),
    # ── NVIDIA GeForce: Turing / Pascal ──────────────────────────────────────
    "rtx 2080 ti":     (4352,  1545,  13.4,  53.8),
    "rtx 2080 super":  (3072,  1815,  11.2,  44.6),
    "rtx 2080":        (2944,  1710,  10.1,  40.3),
    "rtx 2070 super":  (2560,  1770,   9.1,  36.3),
    "rtx 2070":        (2304,  1620,   7.5,  29.9),
    "rtx 2060":        (1920,  1680,   6.5,  25.8),
    "gtx 1660 ti":     (1536,  1770,   5.4,  None),
    "gtx 1660 super":  (1408,  1785,   5.0,  None),
    "gtx 1650":        (896,   1665,   3.0,  None),
    "gtx 1080 ti":     (3584,  1582,  11.3,  None),
    "gtx 1080":        (2560,  1733,   8.9,  None),
    "gtx 1070":        (1920,  1683,   6.5,  None),
    "gtx 1060":        (1280,  1708,   4.4,  None),
    "titan rtx":       (4608,  1770,  16.3,  65.2),
    "titan v":         (5120,  1455,  14.9,  59.6),
    # ── NVIDIA workstation ───────────────────────────────────────────────────
    "quadro p1000":    (640,   1480,   1.9,  None),
    "quadro p2000":    (1024,  1480,   3.0,  None),
    "quadro p4000":    (1792,  1480,   5.3,  None),
    "quadro rtx 4000": (2304,  1545,   7.1,  28.5),
    "quadro rtx 5000": (3072,  1815,  11.2,  44.6),
    "rtx a2000":       (3328,  1200,   8.0,  16.0),
    "rtx a4000":       (6144,  1560,  19.2,  38.4),
    "rtx a4500":       (7168,  1650,  23.7,  47.3),
    "rtx a5000":       (8192,  1695,  27.8,  55.6),
    "rtx a6000":       (10752, 1800,  38.7,  77.4),
    "rtx 5000 ada":    (12800, 2550,  65.3, 130.5),
    "rtx 6000 ada":    (18176, 2505,  91.1, 182.5),
    # ── NVIDIA datacentre ────────────────────────────────────────────────────
    "tesla m40":       (3072,  1114,   6.8,  None),
    "tesla p40":       (3840,  1531,  11.8,  None),
    "tesla p100":      (3584,  1329,   9.5,  None),
    "tesla v100":      (5120,  1530,  15.7, 125.0),
    "tesla t4":        (2560,  1590,   8.1,  65.0),
    "a10":             (9216,  1695,  31.2,  62.5),
    "a40":             (10752, 1740,  37.4, 149.7),
    "a100":            (6912,  1410,  19.5, 312.0),
    "l4":              (7424,  2040,  30.3, 121.0),
    "l40s":            (18176, 2520,  91.6, 362.0),
    "h100 pcie":       (14592, 1755,  51.2, 756.0),
    "h100":            (16896, 1980,  66.9, 989.0),
    # ── AMD Radeon ───────────────────────────────────────────────────────────
    "rx 9070 xt":      (4096,  2970,  48.7,  97.3),
    "rx 9070":         (3584,  2520,  36.1,  72.3),
    "rx 7900 xtx":     (6144,  2500,  61.4, 122.8),
    "rx 7900 xt":      (5376,  2400,  51.6, 103.2),
    "rx 7900 gre":     (5120,  2245,  46.0,  91.9),
    "rx 7800 xt":      (3840,  2430,  37.3,  74.6),
    "rx 7700 xt":      (3456,  2544,  35.2,  70.3),
    "rx 7600":         (2048,  2655,  21.8,  43.5),
    "rx 6900 xt":      (5120,  2250,  23.0,  46.1),
    "rx 6800 xt":      (4608,  2250,  20.7,  41.5),
    "rx 6800":         (3840,  2105,  16.2,  32.3),
    "rx 6700 xt":      (2560,  2581,  13.2,  26.4),
    "rx 6600 xt":      (2048,  2589,  10.6,  21.2),
    "radeon vii":      (3840,  1750,  13.4,  26.9),
    "pro w6800":       (3840,  2320,  17.8,  35.6),
    "instinct mi100":  (7680,  1502,  23.1, 184.6),
    "instinct mi210":  (6656,  1700,  22.6, 181.0),
    "instinct mi250x": (14080, 1700,  47.9, 383.0),
    "instinct mi300x": (19456, 2100, 163.4, 1307.0),
    # ── Intel Arc ────────────────────────────────────────────────────────────
    "arc a770":        (4096,  2100,  19.7,  39.3),
    "arc a750":        (3584,  2050,  17.2,  34.4),
    "arc b580":        (2560,  2670,  13.7,  27.3),
}

# Marketing words that carry no model information. Stripped before lookup so
# "NVIDIA GeForce RTX 3090" and a probe's bare "RTX 3090" hit the same entry.
# "Radeon" is deliberately NOT here: it is the entire model name of the Radeon
# VII, and stripping it would leave that card unmatchable. It costs nothing to
# keep — lookup() searches for a key inside the normalised name rather than
# comparing the whole string, so leftover words never block a match.
_NOISE = re.compile(
    r"\b(nvidia|geforce|amd|advanced micro devices|ati|intel|corporation|corp|"
    r"graphics|graphics card|gpu|series|oem|rev\s*\w+)\b", re.I)
# Names that describe a mobile or cut-down part sharing a desktop model number.
# Never guessed at — see the module docstring.
_MOBILE = re.compile(r"\b(laptop|mobile|max-?q|notebook|mxm|for mobile)\b", re.I)
# A trailing capacity suffix ("RTX 3090 24GB") is packaging, not silicon.
_CAPACITY = re.compile(r"\b\d+\s*g[bi]\b", re.I)


def _norm(name):
    """A card name reduced to just its model identity, or "" when there isn't one."""
    s = _CAPACITY.sub(" ", str(name or ""))
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^0-9a-z ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def lookup(name):
    """The spec for `name` as {cores, boost_mhz, fp32, fp16}, or None.

    Longest key first so "rtx 4080 super" can never be answered by the
    "rtx 4080" entry that is a substring of it — the classic way a lookup table
    like this quietly reports the wrong card.
    """
    if not name or _MOBILE.search(str(name)):
        return None
    n = _norm(name)
    if not n:
        return None
    for key in _SPEC_KEYS:
        # Word-boundary match, not a bare substring: "a100" must not be found
        # inside "rtx a1000", and "l4" must not be found inside "l40s".
        if re.search(r"(?:^| )" + re.escape(key) + r"(?:$| )", n):
            cores, boost, fp32, fp16 = SPECS[key]
            return {"cores": cores, "boost_mhz": boost, "fp32": fp32, "fp16": fp16,
                    "model": key}
    return None


# Longest first, so a more specific model always wins over a prefix of itself.
_SPEC_KEYS = sorted(SPECS, key=lambda k: (-len(k), k))


def compute_for(name, clk_sm=None):
    """The compute block for one card, or None when the card isn't recognised.

    ``fp32``/``fp16`` are the rated peaks — what the card can do. ``fp32_now``
    scales the rated peak by the clock the card is running at this instant,
    which is why an idle card parked in P8 reads a couple of TFLOPS rather than
    its box figure. It is still a *peak*, not an achieved rate: it says "at this
    clock the card could do at most this much", and the UI labels it that way.

    ``clk_sm`` absent (a driver that won't report clocks) leaves ``fp32_now``
    absent too, rather than silently presenting the boost figure as live.
    """
    spec = lookup(name)
    if not spec:
        return None
    out = {"cores": spec["cores"], "boost_mhz": spec["boost_mhz"],
           "fp32": spec["fp32"]}
    if spec["fp16"]:
        out["fp16"] = spec["fp16"]
    if clk_sm and spec["boost_mhz"]:
        out["fp32_now"] = round(spec["fp32"] * (float(clk_sm) / spec["boost_mhz"]), 2)
    return out


def attach(cards):
    """Add a ``compute`` block to every recognised card in `cards`, in place.

    Called from the read paths rather than the sampler on purpose: this is
    derived from a static table plus a clock the card already reports, so there
    is nothing to store and nothing that can go stale in the database. Cards
    that aren't in the table are left exactly as they were — no key, so every
    consumer's "is this present?" check does the right thing.

    Recomputed on every call rather than cached on the dict: the fast lane
    updates the SAME card dicts in place every couple of seconds, so a block
    written once would pin ``fp32_now`` to whatever clock the card happened to
    be at the first time this ran.
    """
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        blk = compute_for(c.get("name"), c.get("clk_sm"))
        if blk:
            c["compute"] = blk
        else:
            c.pop("compute", None)
    return cards


def pooled(cards):
    """Box-level compute: the peaks of every recognised card, summed.

    ``cards_known``/``cards_total`` come along so the UI can say "3 of 4 cards"
    rather than presenting a partial sum as the whole machine. ``fp16`` is
    summed only when EVERY recognised card has tensor cores — a pool mixing a
    3090 with a P2000 has no meaningful FP16 tensor figure, and publishing the
    3090's alone would attribute the whole box's LLM throughput to one card.
    """
    blocks = [c.get("compute") for c in (cards or []) if isinstance(c, dict) and c.get("compute")]
    if not blocks:
        return None
    out = {
        "fp32": round(sum(b["fp32"] for b in blocks), 1),
        "cards_known": len(blocks),
        "cards_total": len(cards or []),
        "cores": sum(b["cores"] for b in blocks),
    }
    live = [b["fp32_now"] for b in blocks if b.get("fp32_now") is not None]
    if len(live) == len(blocks):
        out["fp32_now"] = round(sum(live), 2)
    if all(b.get("fp16") for b in blocks):
        out["fp16"] = round(sum(b["fp16"] for b in blocks), 1)
    return out
