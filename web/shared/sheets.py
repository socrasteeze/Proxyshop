"""
* Proxy Sheet Compiler
* Lays out card images on print-ready PDF pages: standard 63x88mm cards,
* 3x3 per page at 300 DPI, centered, with light cut guides.
* Must never import from `src/`. Requires Pillow.
"""
# Standard Library Imports
from pathlib import Path

# Third Party Imports
from PIL import Image, ImageDraw

DPI = 300

# Physical sizes in millimetres
CARD_MM = (63, 88)                     # standard MTG/Pokemon card face
PAPERS_MM = {
    'letter': (215.9, 279.4),
    'a4': (210.0, 297.0),
}

GRID = (3, 3)                          # columns x rows per page
GUIDE_COLOR = (160, 160, 160)


def _mm_to_px(mm: float) -> int:
    return round(mm / 25.4 * DPI)


def build_sheet_pdf(
    image_paths: list[Path],
    out_path: Path,
    paper: str = 'letter'
) -> int:
    """Compile card images into a multi-page proxy sheet PDF.

    Args:
        image_paths: One entry per physical card to print (repeat for copies).
        out_path: Destination .pdf path.
        paper: 'letter' or 'a4'.

    Returns:
        Number of pages written.

    Raises:
        ValueError: On empty input or unknown paper size.
    """
    if not image_paths:
        raise ValueError('No card images to compile')
    if paper not in PAPERS_MM:
        raise ValueError(f'Unknown paper size {paper!r} (use: {", ".join(PAPERS_MM)})')

    paper_px = (_mm_to_px(PAPERS_MM[paper][0]), _mm_to_px(PAPERS_MM[paper][1]))
    card_px = (_mm_to_px(CARD_MM[0]), _mm_to_px(CARD_MM[1]))
    cols, rows = GRID

    # Center the grid on the page
    grid_w, grid_h = cols * card_px[0], rows * card_px[1]
    margin_x = (paper_px[0] - grid_w) // 2
    margin_y = (paper_px[1] - grid_h) // 2
    if margin_x < 0 or margin_y < 0:
        raise ValueError('Card grid does not fit the selected paper size')

    per_page = cols * rows
    pages: list[Image.Image] = []
    for start in range(0, len(image_paths), per_page):
        page = Image.new('RGB', paper_px, 'white')
        draw = ImageDraw.Draw(page)
        batch = image_paths[start:start + per_page]
        for i, img_path in enumerate(batch):
            col, row = i % cols, i // cols
            x = margin_x + col * card_px[0]
            y = margin_y + row * card_px[1]
            with Image.open(img_path) as img:
                card = img.convert('RGB').resize(card_px, Image.LANCZOS)
            page.paste(card, (x, y))
        # Cut guides: hairlines along every grid boundary, outside the grid
        for c in range(cols + 1):
            x = margin_x + c * card_px[0]
            draw.line([(x, 0), (x, margin_y - 8)], fill=GUIDE_COLOR, width=1)
            draw.line([(x, paper_px[1] - margin_y + 8), (x, paper_px[1])],
                      fill=GUIDE_COLOR, width=1)
        for r in range(rows + 1):
            y = margin_y + r * card_px[1]
            draw.line([(0, y), (margin_x - 8, y)], fill=GUIDE_COLOR, width=1)
            draw.line([(paper_px[0] - margin_x + 8, y), (paper_px[0], y)],
                      fill=GUIDE_COLOR, width=1)
        pages.append(page)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(
        out_path, 'PDF', save_all=True,
        append_images=pages[1:], resolution=DPI)
    return len(pages)
