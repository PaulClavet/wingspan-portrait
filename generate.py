#!/usr/bin/env python3
"""
Turn the WINGSPAN Delivery Services wing logo into an interactive ASCII portrait,
the same treatment given to Hypatia's landing-page art:

  * The wing is "painted" as a grid of colour-styled monospace glyphs.
  * EVE phrases (cloaky ship names, torpedo/bomb modules, WINGSPAN catchphrases)
    are woven INTO the orange feathers as hidden glyph runs.
  * Click the wing and it decloaks: the whole image dims and one phrase lights
    up gold, tracing through the wing. Click again for the next transmission.

No external deps beyond Pillow. Usage:
    python3 generate.py                 # write wingspan-portrait.html (+ .txt preview)
    python3 generate.py --preview       # just print the plain glyph wing to stdout
    python3 generate.py --cols 190      # tune resolution
"""
import argparse, base64, html, json, struct, zlib
from PIL import Image
from phrases import PHRASES

# ---- tunables -------------------------------------------------------------
SRC          = "assets/wingspan-logo-resize.png"
OUT_HTML     = "index.html"            # served at the GitHub Pages root
OUT_TXT      = "wingspan-portrait.txt"
DEFAULT_COLS = 200
CHAR_WH      = 0.52     # monospace glyph advance / line-height (width:height)
BG_THRESH    = 0.14     # ink coverage below this == background (void)
PAD          = 6        # px padding around the ink bounding box before sampling
ORANGE       = (245, 133, 38)   # WINGSPAN brand orange (#F58526)

# The camouflage trick (lifted from Hypatia): the wing is NOT filled with solid
# blocks. It's a noisy field of letters/digits/punctuation in mixed case, ordered
# light -> dense. A hidden letter is then statistically identical to a filler
# letter — same alphabet, same case rule, same colour — so it only shows when lit.
RAMP = ("`.'·,:;\"^!|Iil*r/\\<>()[]{}_+=?~-"   # light: thin punctuation
        "cvxznutfjsy7213"                            # mid: lowercase + slim digits
        "eaoLJTYUCFP4Z5wmq"                          # heavier lowercase / open caps
        "pdbkhg69OQ0DGHSAE8RNBWM")                   # dense caps + round digits
RAMP_LO = 0.16    # ink cells never drop below here, so the wing has no holes
RAMP_TOP = 0.88   # ...nor all the way to the heaviest glyph: keeps the field airy
SPREAD  = 0.42    # how far the per-cell noise pushes a glyph off its coverage

# faint void texture (a sparse starfield); mostly spaces so the wing floats
VOID_GLYPHS = "   .   :   '    .   `      "

MARKER_OK = lambda c: c.isalnum()   # letters & digits glow; spaces/punct = gaps


def rnd(x, y, salt=0):
    """Deterministic pseudo-random in [0,1) — stable rebuilds, no Math.random."""
    h = (x * 73856093) ^ (y * 19349663) ^ (salt * 0x9e3779b1)
    h &= 0xFFFFFFFF
    h = ((h ^ (h >> 16)) * 0x45d9f3b) & 0xFFFFFFFF
    h = ((h ^ (h >> 16)) * 0x45d9f3b) & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFFFFFF) / 0x100000000


def cell_hash(x, y):
    return rnd(x, y, 11)


def load_grid(cols):
    im = Image.open(SRC).convert("RGBA")
    # crop to the ink (non-white) bounding box so the wing fills the frame
    px = im.load()
    W, H = im.size
    minx, miny, maxx, maxy = W, H, 0, 0
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a >= 40 and not (r > 238 and g > 238 and b > 238):
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    minx = max(0, minx - PAD); miny = max(0, miny - PAD)
    maxx = min(W, maxx + PAD); maxy = min(H, maxy + PAD)
    crop = im.crop((minx, miny, maxx + 1, maxy + 1))
    cw, ch = crop.size

    rows = max(1, round(ch / cw * cols * CHAR_WH))
    # flatten transparency onto white (matches the logo's white canvas) so the
    # average sample of an edge cell blends toward white == low coverage
    bg = Image.new("RGBA", crop.size, (255, 255, 255, 255))
    crop = Image.alpha_composite(bg, crop).convert("RGB")
    small = crop.resize((cols, rows), Image.LANCZOS)
    return small, cols, rows


def coverage(rgb):
    """0 (white/void) .. 1 (solid orange ink), from distance below white."""
    r, g, b = rgb
    return min(1.0, (255 - min(r, g, b)) / 217.0)


def ink_color(c, x, y):
    """Vivid orange for an ink cell; dimmer at the feathered edges (low c)."""
    v = 0.50 + 0.50 * c
    jitter = (cell_hash(x, y) - 0.5) * 18
    out = []
    for ch in ORANGE:
        val = ch * v + jitter
        out.append(max(28, min(255, int(val))))
    return tuple(out)


def void_color(x, y):
    n = cell_hash(x * 3 + 1, y * 7 + 2)
    base = 16 + int(n * 12)              # 16..28 — barely above the #0e0e10 bg
    return (base, base, base + 3)


def ramp_glyph(c, x, y):
    """A coverage-driven glyph, jittered by per-cell noise so every region holds
    glyphs of every weight (no smooth gradient to read the hidden text against)."""
    frac = RAMP_LO + c * (RAMP_TOP - RAMP_LO) + (rnd(x, y, 1) - 0.5) * SPREAD
    frac = min(1.0, max(RAMP_LO, frac))
    i = int(frac * (len(RAMP) - 1) + 0.5)
    return RAMP[min(len(RAMP) - 1, max(0, i))]


def cased(ch, c, x, y):
    """Case a hidden letter the way Hypatia did: denser/brighter cells lean
    UPPER, fainter cells lower, with ~18% noise so case never betrays the text."""
    if not ch.isalpha():
        return ch
    upper = (c + (rnd(x, y, 2) - 0.5) * 0.6) > 0.62
    if rnd(x, y, 7) < 0.18:        # random flips, matched to the filler's mix
        upper = not upper
    return ch.upper() if upper else ch.lower()


def build_cells(small, cols, rows):
    """Return a row-major list of cell dicts and the list of writable indices."""
    px = small.load()
    cells = []
    writable = []
    for y in range(rows):
        for x in range(cols):
            c = coverage(px[x, y])
            idx = len(cells)
            if c < BG_THRESH:
                g = VOID_GLYPHS[int(cell_hash(x, y) * len(VOID_GLYPHS)) % len(VOID_GLYPHS)]
                cells.append({"g": g, "col": void_color(x, y), "ink": False,
                              "q": None, "c": c, "x": x, "y": y})
            else:
                cells.append({"g": ramp_glyph(c, x, y), "col": ink_color(c, x, y),
                              "ink": True, "c": c, "x": x, "y": y, "q": None})
                writable.append(idx)
    return cells, writable


def weave(cells, writable, phrases):
    """
    Spread phrases evenly across the writable (ink) cells. Each phrase gets its
    own contiguous segment; its letters/digits become marker glyphs (data-q=i),
    spaces & punctuation become unlit gap glyphs, the rest of the segment stays
    plain orange ink. Returns the count actually placed.
    """
    M = len(writable)
    P = len(phrases)
    placed = 0
    longest = max(len(t) for t, _ in phrases)
    if M < P * (longest + 2):
        print(f"  ! tight fit: {M} ink cells for {P} phrases "
              f"(longest {longest}); raise --cols if glyphs collide")
    for i, (text, _tag) in enumerate(phrases):
        seg_start = (i * M) // P
        w = seg_start
        any_marker = False
        for ch in text:
            if w >= M:
                break
            cell = cells[writable[w]]
            if MARKER_OK(ch):
                # the hidden letter, cased to blend into the surrounding field
                cell["g"] = cased(ch, cell["c"], cell["x"], cell["y"])
                cell["q"] = i
                any_marker = True
            else:
                # space / punctuation -> unlit gap, same noisy field as the filler
                cell["g"] = ramp_glyph(cell["c"], cell["x"], cell["y"])
                cell["q"] = None
            w += 1
        if any_marker:
            placed += 1
    return placed


def render_pre(cells, cols, rows):
    """row-major cells -> the <pre> innerHTML of coloured spans."""
    out = []
    for y in range(rows):
        for x in range(cols):
            cell = cells[y * cols + x]
            r, g, b = cell["col"]
            ch = html.escape(cell["g"])
            if cell["q"] is not None:
                out.append(f'<span class="m" data-q="{cell["q"]}" '
                           f'style="color:#{r:02x}{g:02x}{b:02x}">{ch}</span>')
            else:
                out.append(f'<span style="color:#{r:02x}{g:02x}{b:02x}">{ch}</span>')
        out.append("\n")
    return "".join(out)


def plain_preview(cells, cols, rows):
    lines = []
    for y in range(rows):
        lines.append("".join(cells[y * cols + x]["g"] for x in range(cols)))
    return "\n".join(lines)


HTML_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>WINGSPAN Delivery Services</title>
<style>
  :root {{ --orange: #f58526; }}
  html {{ background: #0b0b0e; }}
  body {{
    background: #0b0b0e;
    color: #c8ccd2;
    font-family: "JetBrains Mono", "Fira Code", Menlo, Consolas, monospace;
    display: flex; flex-direction: column; align-items: center;
    padding: 2rem 1rem; gap: 1.1rem; margin: 0;
  }}
  pre {{ margin: 0; line-height: 1.0; letter-spacing: 0; }}
  .wordmark {{ color: var(--orange); font-size: 13px; line-height: 1.15;
               margin: 0 auto; display: table;
               text-shadow: 0 0 14px rgba(245,133,38,.4); }}
  .tagline {{ color: #6f7682; font-size: 12px; letter-spacing: .42em;
              text-align: center; text-transform: uppercase; }}

  /* Performance: never animate the thousands of glyph spans. One overlay dims
     the whole wing; only the active phrase's spans are raised + recoloured. */
  #stage {{ position: relative; z-index: 0; display: inline-block; }}
  #art {{ font-size: 8px; cursor: pointer; display: block; white-space: pre; }}
  #dim {{
    position: absolute; inset: 0; background: #07070a; opacity: 0;
    z-index: 1; pointer-events: none; transition: opacity 1.1s ease;
  }}
  #stage.lit #dim {{ opacity: .88; }}
  #art .active {{
    position: relative; z-index: 2;
    color: #fff0cf !important;
    text-shadow: 0 0 3px #ffae3a, 0 0 9px rgba(255,138,20,.9);
    transition: color .5s ease, text-shadow .5s ease;
  }}

  .caption {{
    min-height: 3.4rem; max-width: 64ch; text-align: center;
    display: flex; flex-direction: column; gap: .35rem; justify-content: center;
    opacity: 0; transition: opacity .9s ease;
  }}
  .caption .q {{ color: #ffe6c2; font-size: 16px; letter-spacing: .02em; }}
  .caption .a {{ color: var(--orange); font-size: 11px; letter-spacing: .16em;
                 text-transform: uppercase; }}
  .counter {{ color: #3a3d44; font-size: 10px; letter-spacing: .22em;
              text-transform: uppercase; }}
  .clearbtn {{
    margin-top: .1rem; background: none; border: 1px solid #2a2c32; color: #555;
    font-family: inherit; font-size: 10px; letter-spacing: .24em;
    text-transform: uppercase; padding: .35rem 1rem; border-radius: 2px;
    cursor: pointer; opacity: 0; pointer-events: none;
    transition: opacity .4s ease, color .25s ease, border-color .25s ease;
  }}
  .clearbtn.show {{ opacity: 1; pointer-events: auto; }}
  .clearbtn:hover {{ color: var(--orange); border-color: var(--orange); }}
</style>
</head>
<body>
  <pre class="wordmark">{wordmark}</pre>
  <div class="tagline">Delivery Services &middot; New Eden's #1 Delivery Corp</div>

  <div id="stage">
    <pre id="art">{art}</pre>
    <div id="dim"></div>
  </div>

  <div class="caption" id="caption"></div>
  <div class="counter" id="counter">click the wing to decloak a transmission</div>
  <button class="clearbtn" id="clear">Recloak</button>

<script>
  const PHRASES = {phrases_json};
  const HOLD = 14000;
  const stage = document.getElementById('stage');
  const art = document.getElementById('art');
  const caption = document.getElementById('caption');
  const counter = document.getElementById('counter');
  const clearBtn = document.getElementById('clear');

  const groups = {{}};
  art.querySelectorAll('.m').forEach(el => {{
    (groups[el.dataset.q] || (groups[el.dataset.q] = [])).push(el);
  }});

  // Decloak in a random order — a shuffled run through every phrase, reshuffled
  // once the run is exhausted, so you see all of them but never in a fixed order.
  function shuffle(a) {{
    for (let i = a.length - 1; i > 0; i--) {{
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }}
    return a;
  }}
  let order = shuffle([...Array(PHRASES.length).keys()]);
  let pos = -1, faded = true, timer = null, activeEls = [];

  function advance() {{
    pos++;
    if (pos >= order.length) {{ shuffle(order); pos = 0; }}
    light(order[pos]);
  }}

  function clearActive() {{
    for (const e of activeEls) e.classList.remove('active');
    activeEls = [];
  }}
  function light(i) {{
    clearActive();
    activeEls = groups[i] || [];
    for (const e of activeEls) e.classList.add('active');
    stage.classList.add('lit');
    const p = PHRASES[i];
    caption.innerHTML = '<div class="q">' + p.text + '</div>' +
                        '<div class="a">' + p.tag + '</div>';
    caption.style.opacity = '1';
    counter.textContent = (pos + 1) + ' / ' + order.length + '  \\u00b7  click for the next';
    clearBtn.classList.add('show');
    faded = false;
    clearTimeout(timer);
    timer = setTimeout(fade, HOLD);
  }}
  function fade() {{
    faded = true;
    clearTimeout(timer);
    stage.classList.remove('lit');
    clearActive();
    caption.style.opacity = '0';
    counter.textContent = 'click the wing to bring it back';
    clearBtn.classList.remove('show');
  }}

  art.addEventListener('click', () => {{
    if (faded) (pos < 0) ? advance() : light(order[pos]);  // re-show the faded one
    else advance();                                        // still reading -> next
  }});
  clearBtn.addEventListener('click', (e) => {{ e.stopPropagation(); fade(); }});
  document.addEventListener('click', (e) => {{
    if (faded) return;
    const t = e.target;
    if (t instanceof Node && (art.contains(t) || clearBtn.contains(t))) return;
    fade();
  }});
</script>
</body>
</html>
"""

# ASCII wordmark for the header (NOT part of the wing art). Rendered with
# pyfiglet when available; otherwise this baked-in 'standard'-font copy is used,
# so the generator still runs on a bare Python without the venv.
WORDMARK_FALLBACK = r"""__        _____ _   _  ____ ____  ____   _    _   _
\ \      / /_ _| \ | |/ ___/ ___||  _ \ / \  | \ | |
 \ \ /\ / / | ||  \| | |  _\___ \| |_) / _ \ |  \| |
  \ V  V /  | || |\  | |_| |___) |  __/ ___ \| |\  |
   \_/\_/  |___|_| \_|\____|____/|_| /_/   \_\_| \_|"""


def make_wordmark():
    try:
        import pyfiglet
        return pyfiglet.figlet_format("WINGSPAN", font="standard").rstrip("\n")
    except Exception:
        return WORDMARK_FALLBACK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", type=int, default=DEFAULT_COLS)
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()

    small, cols, rows = load_grid(args.cols)
    cells, writable = build_cells(small, cols, rows)
    print(f"grid {cols}x{rows} = {cols*rows} cells, {len(writable)} ink cells")

    if args.preview:
        # preview before weaving so we judge the wing shape itself
        print(plain_preview(cells, cols, rows))
        return

    placed = weave(cells, writable, PHRASES)
    print(f"wove {placed}/{len(PHRASES)} phrases into the wing")

    art = render_pre(cells, cols, rows)
    phrases_json = json.dumps([{"text": t, "tag": g} for t, g in PHRASES],
                              ensure_ascii=False)
    wm = html.escape(make_wordmark())
    out = HTML_TMPL.format(wordmark=wm, art=art, phrases_json=phrases_json)
    with open(OUT_HTML, "w") as f:
        f.write(out)
    with open(OUT_TXT, "w") as f:
        f.write(plain_preview(cells, cols, rows))
    import os
    print(f"wrote {OUT_HTML} ({os.path.getsize(OUT_HTML)//1024} KB) and {OUT_TXT}")


if __name__ == "__main__":
    main()
