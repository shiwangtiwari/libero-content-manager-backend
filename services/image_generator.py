"""
services/image_generator.py
----------------------------
Generates a LinkedIn post image card using Pillow (PIL).
No browser, no Playwright, no external APIs needed.
Runs entirely on Railway with zero latency.

Card spec: 1200x627px (LinkedIn optimal ratio)
Style: Dark navy background, indigo accent, clean typography
Fonts: DejaVu (always available on Debian/Railway)

Called by: routers/telegram.py handle_generate_image
Output: PNG file at /tmp/libero_card_<timestamp>.png
"""

from __future__ import annotations

import logging
import os
import textwrap
import time

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Card dimensions — LinkedIn optimal
CARD_W = 1200
CARD_H = 627

# Color palette — dark professional
COLOR_BG        = (11, 17, 32)       # very dark navy
COLOR_ACCENT    = (99, 102, 241)     # indigo
COLOR_ACCENT2   = (139, 92, 246)     # purple
COLOR_WHITE     = (255, 255, 255)
COLOR_SUBTEXT   = (148, 163, 184)    # slate-400
COLOR_DIM       = (71, 85, 105)      # slate-600
COLOR_CARD_BG   = (17, 24, 39)      # slightly lighter navy for inner card

# Font paths — available on Debian (Railway's OS)
_FONT_PATHS = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ],
}


def _load_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    """Load the first available font for the given style and size."""
    for path in _FONT_PATHS.get(style, _FONT_PATHS["regular"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # Last resort: PIL default bitmap font (always available)
    logger.warning(f"No TrueType font found for style={style}, using default")
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    current = []

    for word in words:
        test_line = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] > max_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)

    if current:
        lines.append(" ".join(current))

    return lines


def generate_linkedin_card(
    hook: str,
    topic: str,
    author: str = "Shiwang Tiwari",
    subtitle: str = "NextLeap PM Fellow | Developer → PM",
    hashtags: str = "#ProductManagement #DevToPM #NextLeap",
    save_path: str | None = None,
) -> str:
    """
    Generate a LinkedIn post image card.

    Args:
        hook: First line of the post — used as the main headline
        topic: Topic label shown as subtitle
        author: Author name shown at bottom
        subtitle: Author tagline
        hashtags: Hashtag string shown at bottom
        save_path: Output path. Defaults to /tmp/libero_card_<timestamp>.png

    Returns:
        Path to the saved PNG file.
    """
    if save_path is None:
        save_path = f"/tmp/libero_card_{int(time.time())}.png"

    logger.info(f"[image_generator] Generating card: hook='{hook[:40]}...'")

    # ── Canvas ────────────────────────────────────────────────────────────────
    img = Image.new("RGB", (CARD_W, CARD_H), color=COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ── Background gradient effect (horizontal bands) ─────────────────────────
    for y in range(CARD_H):
        alpha = y / CARD_H
        r = int(COLOR_BG[0] + (COLOR_CARD_BG[0] - COLOR_BG[0]) * alpha * 0.3)
        g = int(COLOR_BG[1] + (COLOR_CARD_BG[1] - COLOR_BG[1]) * alpha * 0.3)
        b = int(COLOR_BG[2] + (COLOR_CARD_BG[2] - COLOR_BG[2]) * alpha * 0.3)
        draw.line([(0, y), (CARD_W, y)], fill=(r, g, b))

    # ── Accent elements ───────────────────────────────────────────────────────
    # Left vertical bar
    draw.rectangle([56, 56, 72, CARD_H - 56], fill=COLOR_ACCENT)

    # Top-right decorative dots
    dot_x, dot_y = CARD_W - 100, 80
    for i in range(3):
        for j in range(3):
            cx = dot_x + i * 28
            cy = dot_y + j * 28
            draw.ellipse([cx, cy, cx + 10, cy + 10], fill=COLOR_DIM)

    # Bottom accent line
    draw.rectangle([56, CARD_H - 8, CARD_W - 56, CARD_H - 4], fill=COLOR_ACCENT2)

    # ── Fonts ─────────────────────────────────────────────────────────────────
    font_hook_large = _load_font("bold", 54)
    font_hook_medium = _load_font("bold", 44)
    font_topic = _load_font("regular", 26)
    font_author = _load_font("bold", 24)
    font_subtitle = _load_font("regular", 20)
    font_hashtag = _load_font("regular", 20)

    # ── Main headline (hook) ──────────────────────────────────────────────────
    text_x = 104
    text_max_w = CARD_W - 180

    # Choose font size based on hook length
    if len(hook) <= 60:
        hook_font = font_hook_large
    else:
        hook_font = font_hook_medium

    hook_lines = _wrap_text(hook, hook_font, text_max_w, draw)
    # Cap at 3 lines
    if len(hook_lines) > 3:
        hook_lines = hook_lines[:3]
        hook_lines[-1] = hook_lines[-1][:50] + "…"

    line_height = hook_font.size + 12
    hook_block_h = len(hook_lines) * line_height
    hook_y = max(90, (CARD_H // 2) - hook_block_h - 60)

    for line in hook_lines:
        draw.text((text_x, hook_y), line, font=hook_font, fill=COLOR_WHITE)
        hook_y += line_height

    # ── Divider line ──────────────────────────────────────────────────────────
    divider_y = hook_y + 28
    draw.rectangle([text_x, divider_y, text_x + 280, divider_y + 2], fill=COLOR_ACCENT)

    # ── Topic label ───────────────────────────────────────────────────────────
    topic_display = topic[:70] + "…" if len(topic) > 70 else topic
    draw.text((text_x, divider_y + 18), topic_display, font=font_topic, fill=COLOR_SUBTEXT)

    # ── Hashtags ──────────────────────────────────────────────────────────────
    hashtag_y = CARD_H - 110
    draw.text((text_x, hashtag_y), hashtags[:80], font=font_hashtag, fill=COLOR_ACCENT)

    # ── Author section ────────────────────────────────────────────────────────
    author_y = CARD_H - 72
    draw.text((text_x, author_y), author, font=font_author, fill=COLOR_WHITE)
    draw.text((text_x, author_y + 30), subtitle, font=font_subtitle, fill=COLOR_DIM)

    # ── Libero watermark (top right) ─────────────────────────────────────────
    watermark_font = _load_font("regular", 16)
    draw.text((CARD_W - 170, 62), "via Libero · libero.ai", font=watermark_font, fill=COLOR_DIM)

    # ── Save ──────────────────────────────────────────────────────────────────
    img.save(save_path, "PNG", optimize=True)
    file_size = os.path.getsize(save_path)
    logger.info(f"[image_generator] Saved: {save_path} ({file_size:,} bytes)")

    return save_path


def generate_from_post(post: dict, save_path: str | None = None) -> str:
    """
    Convenience wrapper — takes a Supabase post row and generates the card.
    Extracts hook, topic, and hashtags automatically.
    """
    content = post.get("content", "")
    signal_card = post.get("signal_card") or {}

    # Hook: first non-empty line of the post
    hook = next(
        (line.strip() for line in content.split("\n") if line.strip()),
        content[:80]
    )

    # Topic: from signal card
    topic = (
        signal_card.get("selected_topic")
        or signal_card.get("trigger", "")
        or "Product Management"
    )
    if len(topic) > 70:
        topic = topic[:67] + "…"

    # Hashtags: extract from post content (lines starting with #)
    hashtag_lines = [
        line.strip() for line in content.split("\n")
        if line.strip().startswith("#")
    ]
    hashtags = " ".join(hashtag_lines)[:80] if hashtag_lines else "#ProductManagement #DevToPM #NextLeap"

    return generate_linkedin_card(
        hook=hook,
        topic=topic,
        hashtags=hashtags,
        save_path=save_path,
    )
