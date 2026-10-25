"""StudyBridge Study Book: turn one subject's study sheets into a single printable PDF.

Layout of the book
  1. Cover            (logo, system name, subject, student, exam date, 3D island art)
  2. Contents         (clickable, with page numbers)
  3. About this book  (description, how to use it, what's inside)
  4. Chapters         (sections 1-8 + one-minute review, then the practice problems)
  5. Answer Key       (worked answers for every chapter's practice problems)
  6. Ending page

Everything comes from sheets that already exist in the database, so building a
book makes NO AI calls and costs nothing.
"""
import io
import re
import math
import textwrap
import datetime as _dt

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz
import markdown as md
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ------------------------------------------------------------------ page + style
PAGE = fitz.paper_rect("letter")          # 612 x 792 pt
MX = 58                                    # left/right margin
TOP = 74                                   # content top (below running header)
BOT = PAGE.height - 62                     # content bottom (above footer)
BAND_H = 190                               # chapter opener band height

COBALT = "#2f6bff"
CYAN = "#22d3ee"
INK = "#0b1220"
MUTED = "#5b6678"
LINE = "#d6deea"
SOFT = "#eef3ff"


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb255(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class BookError(Exception):
    pass


# ------------------------------------------------------------------ fonts
_FONTS = {}


def _font(code):
    """PyMuPDF font from the pymupdf-fonts package (FiraGO family)."""
    if code not in _FONTS:
        try:
            _FONTS[code] = fitz.Font(code)
        except Exception:
            _FONTS[code] = fitz.Font("helv" if code in ("figo", "figit") else "hebo")
    return _FONTS[code]


def _pil_font(size, bold=True):
    try:
        return ImageFont.truetype(io.BytesIO(_font("figbo" if bold else "figo").buffer), size)
    except Exception:
        return ImageFont.load_default()


def _story_css(archive):
    css = ""
    try:
        for code in ("figo", "figbo", "figit", "figbi"):
            css = fitz.css_for_pymupdf_font(code, CSS=css, archive=archive, name="sb")
        fam = "sb"
    except Exception:
        fam = "sans-serif"
    return css + f"""
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: {fam}; font-size: 10.6pt; line-height: 1.5; color: {INK}; }}
p {{ margin: 0 0 7pt 0; }}
ul, ol {{ margin: 0 0 8pt 0; padding-left: 17pt; }}
li {{ margin: 0 0 3pt 0; }}
strong, b {{ font-weight: bold; color: #081026; }}
h1 {{ font-size: 22pt; font-weight: bold; margin: 0 0 10pt 0; color: {INK}; }}
h2 {{ font-size: 14.5pt; font-weight: bold; color: {INK}; margin: 18pt 0 8pt 0;
      padding: 0 0 4pt 0; border-bottom: 1.4pt solid {COBALT}; }}
h2 .num {{ color: {COBALT}; }}
h3 {{ font-size: 12pt; font-weight: bold; color: {INK}; margin: 12pt 0 5pt 0; }}
h4 {{ font-size: 11pt; font-weight: bold; margin: 10pt 0 4pt 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 4pt 0 10pt 0; font-size: 9.6pt; }}
th {{ font-weight: bold; text-align: left; color: #1d3f9e; border-bottom: 1.4pt solid {COBALT}; }}
th, td {{ border: 0.7pt solid #c9d3e3; padding: 4pt 6pt; vertical-align: top; }}
code {{ background-color: #f1f4f9; font-size: 9.6pt; }}
pre {{ background-color: #f1f4f9; padding: 6pt; font-size: 9pt; }}
blockquote {{ margin: 6pt 0; padding: 4pt 10pt; border-left: 3pt solid {CYAN}; background-color: #f2fbfd; }}
hr {{ border: none; border-top: 0.7pt solid {LINE}; margin: 10pt 0; }}
.diag {{ text-align: center; margin: 6pt 0 12pt 0; }}
.diag-cap {{ font-size: 8.4pt; color: {MUTED}; text-align: center; margin: 0 0 12pt 0; }}
.flowtext {{ background-color: {SOFT}; padding: 6pt 10pt; border: 0.7pt solid #c9d3e3; }}
.review {{ background-color: #eefaf5; border: 1pt solid #9fe3c6; padding: 4pt 12pt 6pt 12pt; margin: 16pt 0 0 0; }}
.review h2 {{ border-bottom: none; color: #0a7a52; margin-top: 8pt; }}
.practice-head {{ margin-top: 0; }}
.practice-note {{ background-color: #fff7e8; border: 0.8pt solid #f3cf8a; padding: 6pt 10pt; font-size: 9.6pt; color: #6b4a08; margin: 0 0 10pt 0; }}
.ans-ch {{ font-size: 15pt; font-weight: bold; color: {INK}; margin: 20pt 0 4pt 0; padding: 0 0 4pt 0;
           border-bottom: 1.4pt solid {CYAN}; }}
.ans-ch .num {{ color: {COBALT}; }}
.ans-first {{ margin-top: 0; }}
.ans-sub {{ font-size: 9pt; color: {MUTED}; margin: 0 0 8pt 0; }}
.lead {{ font-size: 11.6pt; color: #233049; }}
.muted {{ color: {MUTED}; }}
.small {{ font-size: 9pt; }}
.steps td {{ border: none; padding: 5pt 6pt; }}
.stepnum {{ font-size: 16pt; font-weight: bold; color: {COBALT}; width: 30pt; }}
.stars {{ color: #e39b17; }}
.tag {{ font-size: 8.6pt; color: {MUTED}; }}
"""


# ------------------------------------------------------------------ text helpers
_EMOJI = re.compile("[\U0001F000-\U0001FAFF️‍⭐⬆⬇⬅➡]")
_SRC = re.compile(r"\s*\{src:(ai|mixed|material)\}")
_SEC = re.compile(r"^##\s+(?:(\d{1,2})\s*[.)]\s*)?(.+?)\s*$")
_MERMAID = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def clean(text):
    return _EMOJI.sub("", _SRC.sub("", text or ""))


def split_sheet(md_text):
    """Returns (title, [(num, title, body_md), ...]). Unnumbered sheets become one section."""
    text = clean(md_text).replace("\r\n", "\n")
    title, sections, cur = "", [], None
    for line in text.split("\n"):
        if not title and line.startswith("# "):
            title = line[2:].strip()
            continue
        m = _SEC.match(line)
        if m:
            cur = [int(m.group(1) or 0), m.group(2).strip(), []]
            sections.append(cur)
            continue
        if cur is None:
            if line.strip():
                cur = [0, "", []]
                sections.append(cur)
            else:
                continue
        cur[2].append(line)
    return title, [(n, t, "\n".join(b).strip()) for n, t, b in sections]


def _kind(num, title):
    t = title.lower()
    if num == 10 or "answer" in t:
        return "answers"
    if num == 9 or "practice" in t:
        return "practice"
    if num == 11 or "one-minute" in t or "review" in t:
        return "review"
    return "content"


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ mermaid -> picture
_EDGE = re.compile(r"\s*(?:--\s*([^\-|>][^>]*?)\s*-->|-\.->|==>|-->|---|-\.-)\s*(?:\|([^|]*)\|)?\s*")
_NODE = re.compile(r"^([A-Za-z0-9_]+)\s*(.*)$")
_SKIP = ("subgraph", "end", "style", "classdef", "class ", "click", "linkstyle", "%%", "direction")


def _node_label(rest):
    rest = rest.strip()
    if not rest:
        return None
    lab = rest.strip("[](){}>/\\ ")
    lab = lab.strip('"').strip("'").strip()
    return lab or None


def parse_mermaid(code):
    lines = [ln.strip().rstrip(";") for ln in code.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    head = lines[0].lower()
    m = re.match(r"(flowchart|graph)\s+(lr|rl|td|tb|bt)", head)
    if not m:
        return None
    direction = "LR" if m.group(2) in ("lr", "rl") else "TD"
    labels, order, edges = {}, [], []

    def node(tok):
        tok = tok.strip()
        mm = _NODE.match(tok)
        if not mm:
            return None
        nid, rest = mm.group(1), mm.group(2)
        lab = _node_label(rest)
        if nid not in labels:
            labels[nid] = lab or nid
            order.append(nid)
        elif lab:
            labels[nid] = lab
        return nid

    for ln in lines[1:]:
        low = ln.lower()
        if any(low.startswith(s) for s in _SKIP):
            continue
        parts = _EDGE.split(ln)
        # re.split with 2 groups -> node, g1, g2, node, g1, g2, node ...
        toks = parts[0::3]
        labs = [(parts[i] or parts[i + 1] or "") for i in range(1, len(parts) - 1, 3)]
        prev = None
        for i, tok in enumerate(toks):
            ids = [node(t) for t in tok.split("&") if t.strip()]
            ids = [x for x in ids if x]
            if prev is not None:
                for a in prev:
                    for b in ids:
                        edges.append((a, b, (labs[i - 1] or "").strip()))
            if ids:
                prev = ids
    if not order:
        return None
    return {"dir": direction, "labels": labels, "order": order, "edges": edges}


_FILLS = [("#eaf1ff", "#2f6bff"), ("#e3f9fd", "#0ea5c6"), ("#e8faf2", "#12a372"),
          ("#fff4e3", "#d98a0b"), ("#f0f2f6", "#5b6678")]


def render_mermaid_png(g):
    """Draw a parsed flowchart as a clean PNG. Returns (png_bytes, width_px, height_px)."""
    labels, order, edges = g["labels"], g["order"], g["edges"]
    # layering: topological order (cycles broken at the earliest node), then longest path
    indeg = {n: 0 for n in order}
    for a_, b_, _l in edges:
        if a_ != b_:
            indeg[b_] += 1
    remaining, topo = list(order), []
    while remaining:
        pick = next((n for n in remaining if indeg[n] == 0), remaining[0])
        remaining.remove(pick)
        topo.append(pick)
        for a_, b_, _l in edges:
            if a_ == pick and b_ in remaining:
                indeg[b_] -= 1
    rank = {n: i for i, n in enumerate(topo)}
    layer = {n: 0 for n in order}
    for n in topo:
        for a_, b_, _l in edges:
            if b_ == n and rank[a_] < rank[n]:
                layer[n] = max(layer[n], layer[a_] + 1)
    used = sorted(set(layer.values()))
    layer = {n: used.index(v) for n, v in layer.items()}
    nl = max(layer.values()) + 1
    direction = g["dir"]
    if direction == "LR" and nl > 6:
        direction = "TD"
    layers = [[n for n in order if layer[n] == i] for i in range(nl)]

    S = 2  # supersample
    f = _pil_font(24 * S, bold=True)
    fl = _pil_font(18 * S, bold=False)
    pad_x, pad_y, lh = 22 * S, 16 * S, 30 * S
    box = {}
    meas = ImageDraw.Draw(Image.new("L", (10, 10)))
    for n in order:
        lines = textwrap.wrap(labels[n], 18) or [labels[n]]
        w = max(meas.textlength(t, font=f) for t in lines) + 2 * pad_x
        h = len(lines) * lh + 2 * pad_y - 6 * S
        box[n] = [max(w, 150 * S), h, lines]

    gap_in = 28 * S
    gap_between = (120 if any(l for _a, _b, l in edges) else 84) * S
    pos = {}
    if direction == "LR":
        col_w = [max(box[n][0] for n in L) for L in layers]
        col_h = [sum(box[n][1] for n in L) + gap_in * (len(L) - 1) for L in layers]
        H = max(col_h) + 40 * S
        W = sum(col_w) + gap_between * (nl - 1) + 40 * S
        x = 20 * S
        for i, L in enumerate(layers):
            y = (H - col_h[i]) / 2
            for n in L:
                bw, bh, _ = box[n]
                pos[n] = (x + (col_w[i] - bw) / 2, y, bw, bh)
                y += bh + gap_in
            x += col_w[i] + gap_between
    else:
        row_h = [max(box[n][1] for n in L) for L in layers]
        row_w = [sum(box[n][0] for n in L) + gap_in * (len(L) - 1) for L in layers]
        W = max(row_w) + 40 * S
        H = sum(row_h) + (gap_between * 0.8) * (nl - 1) + 40 * S
        y = 20 * S
        for i, L in enumerate(layers):
            x = (W - row_w[i]) / 2
            for n in L:
                bw, bh, _ = box[n]
                pos[n] = (x, y + (row_h[i] - bh) / 2, bw, bh)
                x += bw + gap_in
            y += row_h[i] + gap_between * 0.8
    # back edges (pointing to an earlier or the same layer) go around the outside
    back = [(a_, b_, l_) for a_, b_, l_ in edges if a_ != b_ and layer[b_] <= layer[a_]]
    lane = 34 * S
    if back:
        if direction == "LR":
            H += lane * (len(back) + 1)
        else:
            W += lane * (len(back) + 1)
    W, H = int(W), int(H)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    def centre(n):
        x, y, w, h = pos[n]
        return x + w / 2, y + h / 2

    def clip(n, tx, ty):
        """Point where the line from n's centre towards (tx,ty) leaves n's box."""
        x, y, w, h = pos[n]
        cx, cy = x + w / 2, y + h / 2
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return cx, cy
        sx = (w / 2 + 4 * S) / abs(dx) if dx else 1e9
        sy = (h / 2 + 4 * S) / abs(dy) if dy else 1e9
        s = min(sx, sy)
        return cx + dx * s, cy + dy * s

    arrow = _rgb255("#6b7a93")
    L = 16 * S

    def head(p_from, p_to):
        ang = math.atan2(p_to[1] - p_from[1], p_to[0] - p_from[0])
        d.polygon([p_to, (p_to[0] - L * math.cos(ang - 0.42), p_to[1] - L * math.sin(ang - 0.42)),
                   (p_to[0] - L * math.cos(ang + 0.42), p_to[1] - L * math.sin(ang + 0.42))], fill=arrow)

    labels_to_draw = []
    bottom = max(pos[n][1] + pos[n][3] for n in order)
    right = max(pos[n][0] + pos[n][2] for n in order)
    k = 0
    for a, b, lab in edges:
        if a == b:
            continue
        ax, ay, aw, ah = pos[a]
        bx, by, bw, bh = pos[b]
        if (a, b, lab) in back:
            k += 1
            if direction == "LR":
                yy = bottom + lane * k
                pts = [(ax + aw / 2, ay + ah + 4 * S), (ax + aw / 2, yy), (bx + bw / 2, yy), (bx + bw / 2, by + bh + 6 * S)]
            else:
                xx = right + lane * k
                pts = [(ax + aw + 4 * S, ay + ah / 2), (xx, ay + ah / 2), (xx, by + bh / 2), (bx + bw + 6 * S, by + bh / 2)]
            d.line(pts, fill=arrow, width=3 * S, joint="curve")
            head(pts[-2], pts[-1])
            if lab:
                labels_to_draw.append(((pts[1][0] + pts[2][0]) / 2, (pts[1][1] + pts[2][1]) / 2, lab))
            continue
        p1 = clip(a, *centre(b))
        p2 = clip(b, *centre(a))
        d.line([p1, p2], fill=arrow, width=3 * S)
        head(p1, p2)
        if lab:
            labels_to_draw.append(((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, lab))
    for mx, my, lab in labels_to_draw:
        tw = d.textlength(lab, font=fl)
        d.rounded_rectangle([mx - tw / 2 - 8 * S, my - 14 * S, mx + tw / 2 + 8 * S, my + 14 * S],
                            radius=8 * S, fill="white", outline=_rgb255("#c9d3e3"), width=S)
        d.text((mx, my), lab, font=fl, fill=_rgb255(MUTED), anchor="mm")

    for n in order:
        x, y, w, h = pos[n]
        fill, edge = _FILLS[layer[n] % len(_FILLS)]
        d.rounded_rectangle([x + 3 * S, y + 5 * S, x + w + 3 * S, y + h + 5 * S], radius=16 * S,
                            fill=_rgb255("#e4e9f1"))
        d.rounded_rectangle([x, y, x + w, y + h], radius=16 * S, fill=_rgb255(fill),
                            outline=_rgb255(edge), width=3 * S)
        lines = box[n][2]
        ty = y + h / 2 - (len(lines) - 1) * lh / 2
        for t in lines:
            d.text((x + w / 2, ty), t, font=f, fill=_rgb255(INK), anchor="mm")
            ty += lh
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue(), W, H


def _flow_text(g):
    """Fallback when a diagram can't be drawn: 'A -> B -> C'."""
    return " → ".join(g["labels"][n] for n in g["order"])


# ------------------------------------------------------------------ markdown -> story html
def md_to_story_html(text, archive, diag_counter):
    diagrams = {}

    def stash(m):
        key = f"@@DIAG{len(diagrams)}@@"
        diagrams[key] = m.group(1)
        return f"\n\n{key}\n\n"

    text = _MERMAID.sub(stash, text or "")
    html = md.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    for key, code in diagrams.items():
        block = ""
        g = parse_mermaid(code)
        if g:
            try:
                png, w, h = render_mermaid_png(g)
                name = f"diag{diag_counter[0]}.png"
                diag_counter[0] += 1
                archive.add((png, name))
                disp_w = w / (2 * 2.5)          # 2x supersample, ~10pt labels
                disp_w = min(disp_w, PAGE.width - 2 * MX - 8)
                disp_h = h * disp_w / w
                if disp_h > 430:
                    disp_w *= 430 / disp_h
                block = (f'<div class="diag"><img src="{name}" style="width:{disp_w:.0f}px"/></div>')
            except Exception:
                block = f'<p class="flowtext">{_esc(_flow_text(g))}</p>'
        html = html.replace(f"<p>{key}</p>", block).replace(key, block)
    return html


def _section_html(num, title, body, archive, dc):
    head = f'<span class="num">{num}.</span> {_esc(title)}' if num else _esc(title)
    return f"<h2>{head}</h2>\n" + md_to_story_html(body, archive, dc)


# ------------------------------------------------------------------ story -> pdf pages
def _flow(html, css, archive, first_top=TOP):
    """Lay out html on as many pages as needed. Returns (pdf_bytes, {element_id: page_index})."""
    story = fitz.Story(html=html, user_css=css, archive=archive)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    ids = {}

    def cb(p):
        if getattr(p, "id", None) and (p.open_close & 1) and p.id not in ids:
            ids[p.id] = p.page

    more, pno = 1, 0
    while more:
        dev = writer.begin_page(PAGE)
        top = first_top if pno == 0 else TOP
        more, _ = story.place(fitz.Rect(MX, top, PAGE.width - MX, BOT))
        story.element_positions(cb, {"page": pno})
        story.draw(dev)
        writer.end_page()
        pno += 1
        if pno > 400:
            break
    writer.close()
    return buf.getvalue(), ids


def _measure(html, css, archive):
    st = fitz.Story(html=html, user_css=css, archive=archive)
    _more, filled = st.place(fitz.Rect(0, 0, PAGE.width - 2 * MX, 50000))
    return fitz.Rect(filled).y1


def _flow_blocks(blocks, css, archive, first_top=TOP, gap=10):
    """Lay out html blocks top to bottom; a block that fits on a page is never split across
    two pages (MuPDF ignores page-break-inside). A block taller than a whole page flows.
    Returns pdf bytes."""
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    dev = writer.begin_page(PAGE)
    pno, y = 0, first_top
    page_top = first_top

    def new_page():
        nonlocal dev, pno, y, page_top
        writer.end_page()
        dev = writer.begin_page(PAGE)
        pno += 1
        y = page_top = TOP

    for html in blocks:
        if not html:
            continue
        h = _measure(html, css, archive)
        if y + h > BOT and y > page_top + 0.5 and h <= BOT - TOP:
            new_page()
        st = fitz.Story(html=html, user_css=css, archive=archive)
        while True:
            more, filled = st.place(fitz.Rect(MX, y, PAGE.width - MX, BOT))
            st.draw(dev)
            if not more:
                y = fitz.Rect(filled).y1 + gap
                break
            new_page()
    writer.end_page()
    writer.close()
    return buf.getvalue()


# ------------------------------------------------------------------ Pillow art
def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _grad_row(stops, w):
    row = Image.new("RGB", (w, 1))
    px = row.load()
    for x in range(w):
        t = x / max(1, w - 1)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                px[x, 0] = _lerp(c0, c1, (t - t0) / max(1e-6, t1 - t0))
                break
    return row


def _mix(c1, c2, t):
    return _lerp(c1, c2, t)


def _logo(size):
    """The StudyBridge mark: glossy cobalt->cyan rounded square (same as the app)."""
    S = 4
    s = size * S
    grad = Image.new("RGB", (s, s))
    gp = grad.load()
    a, b, c = _rgb255("#4f8cff"), _rgb255("#2f6bff"), _rgb255("#22d3ee")
    for y in range(s):
        for x in range(0, s, 2):
            t = (x * 0.55 + y * 0.45) / s
            col = _lerp(a, b, t / 0.45) if t < 0.45 else _lerp(b, c, (t - 0.45) / 0.55)
            gp[x, y] = col
            if x + 1 < s:
                gp[x + 1, y] = col
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.3), fill=255)
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    # glossy top highlight
    gl = Image.new("L", (s, s), 0)
    ImageDraw.Draw(gl).ellipse([-s * 0.3, -s * 0.9, s * 1.3, s * 0.45], fill=70)
    gl = Image.composite(gl, Image.new("L", (s, s), 0), mask)
    tile.paste(Image.new("RGBA", (s, s), (255, 255, 255, 255)), (0, 0), gl)
    return tile.resize((size, size), Image.LANCZOS)


def _cylinder(img, cx, top, w, th, d, c1, c2, c3, icon=None, ripple=False, S=1):
    """3D island (same look as the app's path islands)."""
    W, H = img.size
    c1, c2, c3 = _rgb255(c1), _rgb255(c2), _rgb255(c3)
    dark = _mix(c3, (0, 0, 0), 0.3)
    base_cy = top + th / 2 + d
    # ground shadow
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).ellipse([cx - w * 0.5, base_cy + th * 0.05, cx + w * 0.5, base_cy + th * 0.55], fill=150)
    sh = sh.filter(ImageFilter.GaussianBlur(10 * S))
    img.paste((2, 4, 10), (0, 0), sh)
    ring_cy = top + th / 2 + d * 0.92      # a little below the middle, so the cylinder rises above the rings
    front_rings = None
    if ripple:
        # rings orbit the cylinder: back half drawn now (behind), front half after the cylinder
        def rings(start, end):
            rl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            dr = ImageDraw.Draw(rl)
            for k, a in ((1.35, 230), (1.85, 150), (2.4, 80)):
                dr.arc([cx - w / 2 * k, ring_cy - th / 2 * k, cx + w / 2 * k, ring_cy + th / 2 * k],
                       start, end, fill=(56, 213, 255, a), width=int(3 * S))
            return rl.filter(ImageFilter.GaussianBlur(6 * S)), rl
        glow, rl = rings(180, 360)
        img.paste(glow, (0, 0), glow)
        img.paste(rl, (0, 0), rl)
        front_rings = rings(0, 180)
    # side wall
    side_h = int(th / 2 + d)
    row = _grad_row([(0, dark), (0.12, c3), (0.34, _mix(c2, c3, 0.2)), (0.44, _mix(c2, (255, 255, 255), 0.3)),
                     (0.54, c2), (0.84, c3), (1, dark)], int(w))
    wall = row.resize((int(w), side_h + int(th)))
    m = Image.new("L", wall.size, 0)
    md_ = ImageDraw.Draw(m)
    md_.rectangle([0, 0, w, side_h], fill=255)
    md_.ellipse([0, side_h - th / 2, w - 1, side_h + th / 2], fill=255)
    img.paste(wall, (int(cx - w / 2), int(top + th / 2)), m)
    # top face: radial from c1 (centre) to c2 (rim)
    tf = Image.new("RGB", (int(w), int(th)), c2)
    td = ImageDraw.Draw(tf)
    for i in range(24):
        t = i / 23
        col = _mix(c2, c1, t)
        k = 1 - t * 0.85
        td.ellipse([w / 2 - w / 2 * k, th / 2 - th / 2 * k, w / 2 + w / 2 * k, th / 2 + th / 2 * k], fill=col)
    tm = Image.new("L", tf.size, 0)
    ImageDraw.Draw(tm).ellipse([0, 0, w - 1, th - 1], fill=255)
    img.paste(tf, (int(cx - w / 2), int(top)), tm)
    # highlight + rim
    hl = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hl).ellipse([cx - w * 0.32, top + th * 0.08, cx + w * 0.05, top + th * 0.45], fill=110)
    hl = hl.filter(ImageFilter.GaussianBlur(8 * S))
    img.paste((255, 255, 255), (0, 0), hl)
    rim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse([cx - w / 2, top, cx + w / 2, top + th], outline=(255, 255, 255, 110),
                                width=max(1, int(2 * S)))
    img.paste(rim, (0, 0), rim)
    if front_rings:
        glow, rl = front_rings
        img.paste(glow, (0, 0), glow)
        img.paste(rl, (0, 0), rl)
    # icon on the top face
    dr = ImageDraw.Draw(img)
    icx, icy = cx, top + th / 2
    if icon == "check":
        s = th * 0.28
        dr.line([(icx - s, icy), (icx - s * 0.3, icy + s * 0.55), (icx + s, icy - s * 0.6)], fill="white",
                width=int(5 * S), joint="curve")
    elif icon == "play":
        s = th * 0.24
        dr.polygon([(icx - s * 0.7, icy - s), (icx - s * 0.7, icy + s), (icx + s, icy)], fill="white")
    elif icon:
        f = _pil_font(int(th * 0.42))
        dr.text((icx, icy), str(icon), font=f, fill="white", anchor="mm")


def _art_background(W, H, S, seed_shift=0):
    img = Image.new("RGB", (W, H), _rgb255("#05070d"))
    # aurora blobs, blurred at low resolution for speed
    q = 8
    small = Image.new("RGB", (W // q, H // q), _rgb255("#05070d"))
    for (fx, fy, fr, col, a) in [(0.15, 0.08 + seed_shift, 0.55, "#2f6bff", 95), (0.95, 0.2, 0.45, "#22d3ee", 55),
                                 (0.75, 0.95, 0.55, "#123a9e", 110), (0.05, 0.85, 0.35, "#12a372", 40)]:
        lay = Image.new("L", small.size, 0)
        r = fr * W / q
        ImageDraw.Draw(lay).ellipse([fx * W / q - r, fy * H / q - r, fx * W / q + r, fy * H / q + r], fill=a)
        lay = lay.filter(ImageFilter.GaussianBlur(r * 0.5))
        small.paste(_rgb255(col), (0, 0), lay)
    img = small.resize((W, H), Image.BICUBIC)
    # faint grid, fading out from the top
    grid = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(grid)
    step = 44 * S
    for x in range(0, W, step):
        gd.line([(x, 0), (x, H)], fill=16, width=max(1, S // 2))
    for y in range(0, H, step):
        gd.line([(0, y), (W, y)], fill=16, width=max(1, S // 2))
    fade = Image.new("L", (W, H), 0)
    ImageDraw.Draw(fade).ellipse([-W * 0.3, -H * 0.4, W * 1.3, H * 0.7], fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(120 * S))
    grid = Image.composite(grid, Image.new("L", (W, H), 0), fade)
    img.paste((180, 200, 230), (0, 0), grid)
    return img


def cover_art(n_chapters, n_done):
    """Background for the cover: aurora + a trail of 3D islands."""
    S = 2
    W, H = int(PAGE.width * 2.5 * S / 2) * 2 // 2, int(PAGE.height * 2.5 * S / 2) * 2 // 2
    W, H = 1530, 1980
    img = _art_background(W, H, S)
    # dashed trail + islands in the lower right
    n = max(3, min(5, n_chapters))
    pts = []
    for i in range(n):
        x = W * 0.68 + math.sin(i * 1.1 + 0.4) * W * 0.12
        y = H * 0.34 + i * H * 0.125
        pts.append((x, y))
    dr = ImageDraw.Draw(img)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        segs = 14
        for k in range(segs):
            if k % 2:
                continue
            t0, t1 = k / segs, (k + 0.6) / segs
            dr.line([(x0 + (x1 - x0) * t0, y0 + 40 + (y1 - y0) * t0), (x0 + (x1 - x0) * t1, y0 + 40 + (y1 - y0) * t1)],
                    fill=(70, 110, 190), width=4)
    cur = min(n_done, n - 1)
    for i, (x, y) in enumerate(pts):
        wv = 150 if i == cur else 118
        if i < cur:
            cols, icon = ("#a6ffe0", "#22d494", "#0b6b4a"), "check"
        elif i == cur:
            cols, icon = ("#d6f5ff", "#2f8bff", "#123a9e"), "play"
        else:
            cols, icon = ("#b4bfcf", "#4a5568", "#171d27"), str(i + 1)
        _cylinder(img, x, y - wv * 0.28, wv, wv * 0.56, wv * 0.26, *cols, icon=icon, ripple=(i == cur), S=S)
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def ending_art():
    S = 2
    W, H = 1530, 1980
    img = _art_background(W, H, S, seed_shift=0.25)
    # a finished trail: all islands done, final one glowing
    xs = [W * 0.2, W * 0.35, W * 0.5, W * 0.65, W * 0.8]
    for i, x in enumerate(xs):
        y = H * 0.74 - math.sin(i * 0.9) * 40
        last = i == len(xs) - 1
        wv = 150 if last else 112
        cols = ("#d6f5ff", "#2f8bff", "#123a9e") if last else ("#a6ffe0", "#22d494", "#0b6b4a")
        _cylinder(img, x, y - wv * 0.28, wv, wv * 0.56, wv * 0.26, *cols, icon="check", ripple=last, S=S)
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def band_art(label_color="#2f6bff"):
    """Dark strip for chapter / answer-key openers."""
    W, H = 1530, int(BAND_H * 2.5)
    img = _art_background(W, H, 2)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def _png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ------------------------------------------------------------------ drawing text with fitz
def _text(page, x, y, s, size=11, code="figo", color=INK, align="left", maxw=None):
    font = _font(code)
    s = s or ""
    if maxw:
        while s and font.text_length(s, fontsize=size) > maxw:
            s = s[:-2].rstrip() + "…" if len(s) > 2 else ""
            if s.endswith("……"):
                s = s[:-1]
    w = font.text_length(s, fontsize=size)
    if align == "center":
        x -= w / 2
    elif align == "right":
        x -= w
    tw = fitz.TextWriter(page.rect)
    tw.append((x, y), s, font=font, fontsize=size)
    tw.write_text(page, color=_rgb(color))
    return w


def _wrap(s, size, code, maxw):
    font = _font(code)
    words, lines, cur = (s or "").split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if font.text_length(t, fontsize=size) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def _fmt_date(iso):
    try:
        d = _dt.date.fromisoformat(iso)
        return d.strftime("%B %-d, %Y") if hasattr(d, "strftime") else iso
    except Exception:
        return iso or ""


def _fmt_date_safe(iso):
    try:
        d = _dt.date.fromisoformat(iso)
        return f"{d.strftime('%B')} {d.day}, {d.year}"
    except Exception:
        return iso or ""


def _short_date(iso):
    try:
        d = _dt.date.fromisoformat(iso)
        return f"{d.strftime('%b')} {d.day}"
    except Exception:
        return iso or ""


# ------------------------------------------------------------------ data
def load_book_data(conn, user_id, subject_id):
    subj = conn.execute("SELECT * FROM subjects WHERE id=? AND user_id=?", (subject_id, user_id)).fetchone()
    if not subj:
        raise BookError("Subject not found.")
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    keys = subj.keys()
    plan = conn.execute("SELECT id FROM plans WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    plan_days = {}
    if plan:
        try:
            for pi in conn.execute("SELECT chapter_id, day FROM plan_items WHERE plan_id=? AND chapter_id IS NOT NULL "
                                   "ORDER BY day", (plan["id"],)).fetchall():
                plan_days.setdefault(pi["chapter_id"], [])
                if pi["day"] not in plan_days[pi["chapter_id"]]:
                    plan_days[pi["chapter_id"]].append(pi["day"])
        except Exception:
            pass
    chapters = []
    for c in conn.execute("SELECT * FROM chapters WHERE subject_id=? ORDER BY order_index, id", (subject_id,)).fetchall():
        if "in_scope" in c.keys() and c["in_scope"] == 0:
            continue
        s = conn.execute("SELECT * FROM summaries WHERE chapter_id=? ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone()
        if not s or not (s["content_md"] or "").strip():
            continue
        title, secs = split_sheet(s["content_md"])
        chapters.append({
            "id": c["id"], "name": c["name"], "sections": secs,
            "weight": (c["weight"] if "weight" in c.keys() and c["weight"] else 2),
            "ai": bool(s["is_ai_generated"]),
            "studied": bool(s["studied"]) if "studied" in s.keys() else False,
            "days": plan_days.get(c["id"], []),
        })
    if not chapters:
        raise BookError("This subject has no study sheets yet. Build the sheets first, then make the book.")
    return {
        "subject": subj["name"],
        "level": (subj["level"] if "level" in keys else "") or "",
        "exam_date": subj["exam_date"] or "",
        "student": (user["name"] if user else "") or "",
        "grade": (user["grade"] if user else "") or "",
        "chapters": chapters,
    }


# ------------------------------------------------------------------ html builders
def _stars(n):
    n = max(1, min(3, int(n or 2)))
    return "★" * n + "☆" * (3 - n)


_WEIGHT = {1: "Lower priority", 2: "Standard", 3: "High priority"}


def _count_items(body):
    return len(re.findall(r"^\s*\d+[.)]\s+", body or "", flags=re.M))


def about_html(data, refs, archive, dc):
    ch = data["chapters"]
    n_prac = sum(_count_items(b) for c in ch for n, t, b in c["sections"] if _kind(n, t) == "practice")
    exam = data["exam_date"]
    days_left = ""
    try:
        dl = (_dt.date.fromisoformat(exam) - _dt.date.today()).days
        if dl >= 0:
            days_left = f" &mdash; {dl} day{'s' if dl != 1 else ''} from the day this book was made"
    except Exception:
        pass
    who = _esc(data["student"]) or "you"
    rows = []
    for i, c in enumerate(ch, 1):
        pr = sum(_count_items(b) for n, t, b in c["sections"] if _kind(n, t) == "practice")
        days = ", ".join(_short_date(d) for d in c["days"][:3]) or "&ndash;"
        rows.append(f"<tr><td>{i}</td><td><b>{_esc(c['name'])}</b></td><td class='stars'>{_stars(c['weight'])}</td>"
                    f"<td>{pr or '&ndash;'}</td><td>{days}</td><td>{refs.get(f'ch{i}', '')}</td></tr>")
    ai_note = ""
    if any(c["ai"] for c in ch):
        ai_note = ("<p class='small muted'>Chapters marked <i>AI-written</i> were built from standard curriculum "
                   "knowledge because no class files were added for them, so double-check them against your class notes.</p>")
    return f"""
<h1>About this book</h1>
<p class="lead">This is <b>{who}'s</b> complete study book for <b>{_esc(data['subject'])}</b>. StudyBridge turned the
class materials into {len(ch)} chapter{'s' if len(ch) != 1 else ''} of study notes, then gathered them here in one place
with {n_prac or 'the'} practice problems and a full answer key at the back.</p>
<p>{('The exam is on <b>' + _fmt_date_safe(exam) + '</b>' + days_left + '.') if exam else 'No exam date was set for this subject.'}
Each chapter follows the same order, so once you know the layout of one, you know them all.</p>

<h2>How to use this book</h2>
<table class="steps">
<tr><td class="stepnum">1</td><td><b>Read the chapter.</b> Start with the overview and key concepts, then work through
the main summary, the diagrams and the worked examples.</td></tr>
<tr><td class="stepnum">2</td><td><b>Check the traps.</b> The misconceptions, must-memorize and exam-focus sections
show what tests actually ask and where students lose points.</td></tr>
<tr><td class="stepnum">3</td><td><b>Try the practice problems.</b> They are at the end of every chapter. Do them
without looking back first.</td></tr>
<tr><td class="stepnum">4</td><td><b>Mark your answers.</b> The Answer Key (page {refs.get('answers', '')}) has a worked
solution for every problem. Redo anything you missed a day later.</td></tr>
</table>

<h2>What's inside</h2>
<table>
<tr><th>#</th><th>Chapter</th><th>Difficulty</th><th>Practice</th><th>Study days</th><th>Page</th></tr>
{''.join(rows)}
</table>
<p class="small muted">Difficulty stars come from the importance you set for each chapter in StudyBridge.
Study days come from your study plan.</p>
{ai_note}
"""


def chapter_html(i, c, refs, archive, dc):
    parts, practice, review = [], [], []
    for n, t, b in c["sections"]:
        k = _kind(n, t)
        if k == "answers":
            continue
        if k == "practice":
            practice.append((n, t, b))
        elif k == "review":
            review.append((n, t, b))
        else:
            parts.append(_section_html(n, t, b, archive, dc))
    for n, t, b in review:
        parts.append(f'<div class="review">{_section_html(n, t, b, archive, dc)}</div>')
    prac = []
    for j, (n, t, b) in enumerate(practice):
        ans_page = refs.get(f"ans{i}", "")
        head = f'<span class="num">{n}.</span> {_esc(t)}' if n else _esc(t)
        prac.append(f'<h2 class="practice-head">{head}</h2>'
                    f'<p class="practice-note">Work these out on your own first. Full worked answers are in the '
                    f'Answer Key on page {ans_page}.</p>' + md_to_story_html(b, archive, dc))
    return "\n".join(parts), "\n".join(prac)


def answers_html(data, archive, dc):
    out = []
    for i, c in enumerate(data["chapters"], 1):
        ans = [(n, t, b) for n, t, b in c["sections"] if _kind(n, t) == "answers"]
        cls = "ans-ch ans-first" if i == 1 else "ans-ch"
        out.append(f'<p class="{cls}" id="ans{i}"><span class="num">Chapter {i}</span> &nbsp;{_esc(c["name"])}</p>')
        if ans:
            for n, t, b in ans:
                out.append(md_to_story_html(b, archive, dc))
        else:
            out.append('<p class="muted">This chapter\'s sheet has no answer section.</p>')
    return "\n".join(out)


# ------------------------------------------------------------------ page drawing
_XREFS = {}


def _img(page, rect, png):
    """Insert an image; the same picture is stored once in the PDF and reused."""
    key = (id(page.parent), id(png))
    x = _XREFS.get(key)
    if x:
        page.insert_image(rect, xref=x)
    else:
        _XREFS[key] = page.insert_image(rect, stream=png)


def _draw_logo(page, rect, png):
    _img(page, rect, png)


def draw_cover(page, data, art, logo_png):
    _img(page, page.rect, art)
    _draw_logo(page, fitz.Rect(MX, 58, MX + 34, 92), logo_png)
    _text(page, MX + 46, 82, "StudyBridge", 20, "figbo", "#ffffff")
    _text(page, MX, 206, "STUDY BOOK", 10.5, "figbo", "#7fb2ff")
    fitz_page_rule(page, MX, 216, MX + 40, "#22d3ee", 2)
    y = 262
    for line in _wrap(data["subject"], 40, "figbo", 330)[:3]:
        _text(page, MX, y, line, 40, "figbo", "#ffffff")
        y += 46
    sub = f"{len(data['chapters'])} chapters  ·  practice problems  ·  full answer key"
    _text(page, MX, y + 4, sub, 11.5, "figo", "#aab4c6")
    y += 70
    rows = []
    if data["student"]:
        rows.append(("STUDENT", data["student"] + (f"  ·  Grade {data['grade']}" if data["grade"] else "")))
    if data["exam_date"]:
        rows.append(("EXAM DATE", _fmt_date_safe(data["exam_date"])))
    if data["level"]:
        rows.append(("COURSE LEVEL", data["level"]))
    for k, v in rows:
        _text(page, MX, y, k, 8.5, "figbo", "#6d788c")
        _text(page, MX, y + 17, v, 13, "figbo", "#f2f5fa", maxw=300)
        y += 46
    today = _dt.date.today()
    _text(page, MX, PAGE.height - 50, f"Made with StudyBridge  ·  {today.strftime('%B')} {today.day}, {today.year}",
          9, "figo", "#6d788c")


def fitz_page_rule(page, x0, y, x1, color, w=0.8):
    page.draw_line((x0, y), (x1, y), color=_rgb(color), width=w)


def draw_contents(page, entries, first):
    """entries: [(level, title, page_no, target_index)]"""
    if first:
        _text(page, MX, 96, "Contents", 26, "figbo", INK)
        fitz_page_rule(page, MX, 110, MX + 44, COBALT, 2.2)
        y = 150
    else:
        y = 96
    links = []
    for lvl, title, pno, target in entries:
        if lvl == 1:
            size, code, col, x = 12.5, "figbo", INK, MX
            y += 8
        else:
            size, code, col, x = 10, "figo", MUTED, MX + 24
        right = PAGE.width - MX
        tw = _text(page, x, y, title, size, code, col, maxw=right - x - 60)
        pw = _font(code).text_length(str(pno), fontsize=size)
        _text(page, right, y, str(pno), size, code, col if lvl == 1 else INK, align="right")
        page.draw_line((x + tw + 6, y - 1), (right - pw - 6, y - 1), color=_rgb("#b9c3d3"), width=0.8,
                       dashes="[0.8 3] 0")
        links.append((fitz.Rect(x, y - size, right, y + 4), target))
        y += 24 if lvl == 1 else 18
    return links


def draw_running(page, left, right, pno):
    """Header + footer on normal pages."""
    fitz_page_rule(page, MX, 46, PAGE.width - MX, LINE, 0.7)
    _text(page, MX, 38, left, 8.5, "figbo", MUTED, maxw=260)
    _text(page, PAGE.width - MX, 38, right, 8.5, "figo", MUTED, align="right", maxw=240)
    fitz_page_rule(page, MX, PAGE.height - 42, PAGE.width - MX, LINE, 0.7)
    _text(page, PAGE.width - MX, PAGE.height - 28, str(pno), 9, "figbo", INK, align="right")
    _text(page, MX, PAGE.height - 28, "StudyBridge", 8.5, "figo", "#9aa6b8")


def draw_band(page, band_png, kicker, title, meta, logo_png):
    r = fitz.Rect(0, 0, PAGE.width, BAND_H)
    _img(page, r, band_png)
    _draw_logo(page, fitz.Rect(MX, 40, MX + 20, 60), logo_png)
    _text(page, MX + 28, 55, "StudyBridge", 10, "figbo", "#ffffff")
    _text(page, MX, 104, kicker, 10, "figbo", "#7fb2ff")
    y = 136
    lines = _wrap(title, 26, "figbo", PAGE.width - 2 * MX)
    if len(lines) > 1:
        lines = _wrap(title, 20, "figbo", PAGE.width - 2 * MX)[:2]
        size = 20
    else:
        size = 26
    for ln in lines:
        _text(page, MX, y, ln, size, "figbo", "#ffffff")
        y += size + 4
    if meta:
        _text(page, MX, BAND_H - 18, meta, 9.5, "figo", "#aab4c6", maxw=PAGE.width - 2 * MX)


def draw_ending(page, data, art, logo_png):
    _img(page, page.rect, art)
    cx = PAGE.width / 2
    _draw_logo(page, fitz.Rect(cx - 30, 110, cx + 30, 170), logo_png)
    _text(page, cx, 204, "StudyBridge", 16, "figbo", "#ffffff", align="center")
    _text(page, cx, 290, "That's the whole book.", 30, "figbo", "#ffffff", align="center")
    _text(page, cx, 322, "Now close it and try to explain each chapter out loud.", 12, "figo", "#aab4c6",
          align="center")
    checks = ["Read every chapter", "Tried every practice problem", "Checked the Answer Key",
              "Reviewed your weak spots in StudyBridge"]
    y = 380
    font = _font("figo")
    colw = max(font.text_length(s, fontsize=12) for s in checks) + 30
    x0 = cx - colw / 2
    for s in checks:
        page.draw_rect(fitz.Rect(x0, y - 11, x0 + 13, y + 2), color=_rgb("#7fb2ff"), width=1.1, radius=0.2)
        _text(page, x0 + 24, y, s, 12, "figo", "#f2f5fa")
        y += 28
    if data["exam_date"]:
        try:
            dl = (_dt.date.fromisoformat(data["exam_date"]) - _dt.date.today()).days
            msg = f"Exam: {_fmt_date_safe(data['exam_date'])}" + (f"  ·  {dl} days to go" if dl > 0 else "")
        except Exception:
            msg = f"Exam: {data['exam_date']}"
        _text(page, cx, y + 26, msg, 12.5, "figbo", "#7fe0ff", align="center")
    _text(page, cx, PAGE.height - 48, f"{data['subject']}  ·  Study Book  ·  Made with StudyBridge",
          9, "figo", "#6d788c", align="center")


# ------------------------------------------------------------------ assemble
def build_book(conn, user_id, subject_id):
    """Returns (pdf_bytes, filename). Raises BookError with a friendly message."""
    data = load_book_data(conn, user_id, subject_id)
    ch = data["chapters"]
    n_done = sum(1 for c in ch if c["studied"])
    logo_png = _png(_logo(160))
    cov = cover_art(len(ch), n_done)
    end = ending_art()
    band = band_art()

    # contents entries (fixed list; only page numbers change between passes)
    def toc_entries(pg):
        e = [(1, "About this book", pg.get("about", 0))]
        for i, c in enumerate(ch, 1):
            e.append((1, f"{i}.  {c['name']}", pg.get(f"ch{i}", 0)))
            if pg.get(f"prac{i}"):
                e.append((2, "Practice problems", pg.get(f"prac{i}", 0)))
        e.append((1, "Answer Key", pg.get("answers", 0)))
        for i, c in enumerate(ch, 1):
            e.append((2, f"Chapter {i}  ·  {c['name']}", pg.get(f"ans{i}", 0)))
        return e

    per_first, per_next = 24, 28
    n_entries = len(toc_entries({"prac1": 1}) ) + len(ch)
    toc_pages = 1 if n_entries <= per_first else 1 + math.ceil((n_entries - per_first) / per_next)

    refs = {}
    for _pass in range(3):
        archive = fitz.Archive()
        css = _story_css(archive)
        dc = [0]
        flows = []  # (key, pdf_bytes, ids, first_top)
        about_pdf, _ = _flow(about_html(data, refs, archive, dc), css, archive)
        flows.append(("about", about_pdf, {}))
        for i, c in enumerate(ch, 1):
            main_html, prac_html = chapter_html(i, c, refs, archive, dc)
            pdf, ids = _flow(main_html, css, archive, first_top=BAND_H + 26)
            flows.append((f"ch{i}", pdf, ids))
            if prac_html:
                pdf, ids = _flow(prac_html, css, archive)
                flows.append((f"prac{i}", pdf, ids))
        ans_pdf, ans_ids = _flow(answers_html(data, archive, dc), css, archive, first_top=BAND_H + 26)
        flows.append(("answers", ans_pdf, ans_ids))

        pg = {}
        cur = 1 + toc_pages + 1         # cover=1, contents, then first flow page
        for key, pdf, ids in flows:
            pg[key] = cur
            for eid, off in ids.items():
                pg[eid] = cur + off
            cur += fitz.open("pdf", pdf).page_count
        new_refs = {k: v for k, v in pg.items()}
        if new_refs == refs:
            break
        refs = new_refs
    pg = refs

    doc = fitz.open()
    _XREFS.clear()
    # cover
    p = doc.new_page(width=PAGE.width, height=PAGE.height)
    draw_cover(p, data, cov, logo_png)
    # contents placeholder pages
    for _ in range(toc_pages):
        doc.new_page(width=PAGE.width, height=PAGE.height)
    # flows
    starts = {}
    for key, pdf, ids in flows:
        starts[key] = doc.page_count
        src = fitz.open("pdf", pdf)
        doc.insert_pdf(src)
    # ending
    p = doc.new_page(width=PAGE.width, height=PAGE.height)
    draw_ending(p, data, end, logo_png)

    # running headers/footers + openers
    order = [k for k, _, _ in flows]
    for idx, key in enumerate(order):
        a = starts[key]
        b = starts[order[idx + 1]] if idx + 1 < len(order) else doc.page_count - 1
        if key == "about":
            right = "About this book"
        elif key == "answers":
            right = "Answer Key"
        else:
            i = int(key[4:] if key.startswith("prac") else key[2:])
            right = f"Chapter {i}  ·  {ch[i - 1]['name']}"
        for pno in range(a, b):
            page = doc[pno]
            if pno == a and (key == "answers" or key.startswith("ch")):
                if key == "answers":
                    draw_band(page, band, "BACK OF THE BOOK", "Answer Key",
                              "Worked solutions for every chapter's practice problems.", logo_png)
                else:
                    i = int(key[2:])
                    c = ch[i - 1]
                    meta = [f"Difficulty {_stars(c['weight'])}"]
                    if c["days"]:
                        meta.append("Study days: " + ", ".join(_short_date(d) for d in c["days"][:4]))
                    if c["ai"]:
                        meta.append("AI-written from standard curriculum")
                    draw_band(page, band, f"CHAPTER {i:02d}", c["name"], "   ·   ".join(meta), logo_png)
                _text(page, PAGE.width - MX, PAGE.height - 28, str(pno + 1), 9, "figbo", INK, align="right")
            else:
                draw_running(page, f"{data['subject']}  ·  Study Book", right, pno + 1)

    # contents pages
    entries = toc_entries(pg)
    chunks = [entries[:per_first]]
    rest = entries[per_first:]
    while rest:
        chunks.append(rest[:per_next])
        rest = rest[per_next:]
    for k in range(toc_pages):
        page = doc[1 + k]
        chunk = chunks[k] if k < len(chunks) else []
        links = draw_contents(page, [(l, t, n, n - 1) for l, t, n in chunk], first=(k == 0))
        for rect, target in links:
            page.insert_link({"kind": fitz.LINK_GOTO, "from": rect, "page": target, "to": fitz.Point(0, 0)})
        draw_running(page, f"{data['subject']}  ·  Study Book", "Contents", k + 2)

    # PDF bookmarks
    toc = [[1, "Cover", 1], [1, "Contents", 2]]
    for l, t, n in entries:
        toc.append([l, t.replace("  ", " "), n])
    toc.append([1, "The end", doc.page_count])
    try:
        doc.set_toc(toc)
    except Exception:
        pass
    doc.set_metadata({"title": f"{data['subject']} - Study Book", "author": data["student"],
                      "creator": "StudyBridge", "producer": "StudyBridge", "subject": "Study book"})
    try:
        doc.subset_fonts()
    except Exception:
        pass
    out = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    safe = re.sub(r"[^A-Za-z0-9 _.-]+", "", data["subject"]).strip() or "Subject"
    return out, f"StudyBridge - {safe} - Study Book.pdf"
