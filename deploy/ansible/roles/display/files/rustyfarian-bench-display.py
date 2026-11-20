#!/usr/bin/python3
"""Draw the bench banner on the SPI panel's framebuffer.

Installed by the `display` role as /usr/local/bin/rustyfarian-bench-display and run by
rustyfarian-bench-display.service: wait for the panel's framebuffer, draw the title and the
installed version once, then sleep until the service is stopped. `just display-smoke` renders it
on the controller into tmp/display/ with a fake framebuffer; BENCH_SYSFS_GRAPHICS and
BENCH_DEV_DIR point it there.
"""

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MARGIN = 24
POLL_SECONDS = 5
MIN_FONT = 12
BACKGROUND = (0, 0, 0)
TITLE_COLOUR = (255, 255, 255)
SUBTITLE_COLOUR = (160, 160, 160)
ACCENT_COLOUR = (230, 110, 30)

log = logging.getLogger("rustyfarian-bench-display")


def find_framebuffer(sysfs: Path, match: str) -> Path | None:
    """The first framebuffer under sysfs whose driver name contains `match`."""
    for fb in sorted(sysfs.glob("fb[0-9]*")):
        if match in fb.joinpath("name").read_text():
            return fb
    return None


def geometry(fb: Path) -> tuple[int, int, int, int]:
    """Width, height, bits per pixel and bytes per row, from sysfs."""
    width, height = (int(v) for v in fb.joinpath("virtual_size").read_text().split(","))
    bpp = int(fb.joinpath("bits_per_pixel").read_text())
    stride = int(fb.joinpath("stride").read_text())
    return width, height, bpp, stride


def fitted_font(text: str, size: int, max_width: int) -> ImageFont.FreeTypeFont:
    """DejaVu Sans Bold at `size`, shrunk until `text` fits `max_width`, or Pillow's own font."""
    while True:
        try:
            font = ImageFont.truetype(str(FONT), size)
        except OSError:
            font = ImageFont.load_default(size)
        if font.getlength(text) <= max_width or size - 2 < MIN_FONT:
            return font
        size -= 2


def render(width: int, height: int, title: str, subtitle: str) -> Image.Image:
    """The banner: the title, an accent line and the subtitle, centred on black."""
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = fitted_font(title, height // 7, width - 2 * MARGIN)
    subtitle_font = fitted_font(
        subtitle, max(int(title_font.size * 0.6), MIN_FONT), width - 2 * MARGIN
    )
    centre = width / 2
    draw.text((centre, height * 0.38), title, font=title_font, fill=TITLE_COLOUR, anchor="mm")
    line_y = height * 0.55
    draw.line([(2 * MARGIN, line_y), (width - 2 * MARGIN, line_y)], fill=ACCENT_COLOUR, width=3)
    draw.text(
        (centre, height * 0.70), subtitle, font=subtitle_font, fill=SUBTITLE_COLOUR, anchor="mm"
    )
    return image


def to_rgb565(image: Image.Image, stride: int) -> bytes:
    """Pack into little-endian RGB565 rows of `stride` bytes, as the fbtft framebuffer wants."""
    red, green, blue = image.convert("RGB").split()
    # rrrrrggg gggbbbbb: the bit fields do not overlap, so a saturating add is an or
    high = ImageChops.add(red.point(lambda v: v & 0xF8), green.point(lambda v: v >> 5))
    low = ImageChops.add(green.point(lambda v: (v & 0x1C) << 3), blue.point(lambda v: v >> 3))
    rows = Image.merge("LA", (low, high)).tobytes()
    row_bytes = image.width * 2
    if stride == row_bytes:
        return rows
    padding = bytes(stride - row_bytes)
    return b"".join(rows[i : i + row_bytes] + padding for i in range(0, len(rows), row_bytes))


def draw_banner(fb: Path, dev_dir: Path, title: str, subtitle: str, png: Path | None) -> None:
    width, height, bpp, stride = geometry(fb)
    if bpp != 16:
        log.error("%s has %d bits per pixel, expected 16 (RGB565)", fb.name, bpp)
        sys.exit(1)
    image = render(width, height, title, subtitle)
    if png:
        image.save(png)
    # no O_CREAT: a device node that is not there yet must not become a plain file
    with open(dev_dir / fb.name, "wb", opener=lambda path, _: os.open(path, os.O_WRONLY)) as dev:
        dev.write(to_rgb565(image, stride))
    log.info("banner drawn on %s (%dx%d): %s, %s", fb.name, width, height, title, subtitle)


def main() -> None:
    parser = argparse.ArgumentParser(description="draw the bench banner on the SPI panel")
    # the defaults repeat deploy/ansible/roles/display/defaults/main.yml; the unit sets all three
    parser.add_argument("--title", default=os.environ.get("BENCH_TITLE", "rustyfarian bench"))
    parser.add_argument("--subtitle", default=os.environ.get("BENCH_VERSION", "unknown"))
    parser.add_argument(
        "--fb-match",
        default=os.environ.get("BENCH_FB_MATCH", "ili9486"),
        help="part of the panel's framebuffer name (default: %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="draw and exit instead of staying up")
    parser.add_argument("--png", type=Path, help="also save the banner as a PNG")
    args = parser.parse_args()
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    sysfs = Path(os.environ.get("BENCH_SYSFS_GRAPHICS", "/sys/class/graphics"))
    dev_dir = Path(os.environ.get("BENCH_DEV_DIR", "/dev"))

    fb = find_framebuffer(sysfs, args.fb_match)
    if fb is None:
        if args.once:
            log.error("no framebuffer named *%s* under %s", args.fb_match, sysfs)
            sys.exit(1)
        log.info("waiting for the panel framebuffer (*%s*)", args.fb_match)
        while fb is None:
            time.sleep(POLL_SECONDS)
            fb = find_framebuffer(sysfs, args.fb_match)
    while True:
        try:
            draw_banner(fb, dev_dir, args.title, args.subtitle, args.png)
            break
        except (FileNotFoundError, PermissionError) as exc:
            # the node is not there yet, or udev has not handed it to group video yet
            if args.once:
                raise
            log.info("%s; retrying in %d s", exc, POLL_SECONDS)
            time.sleep(POLL_SECONDS)
    if not args.once:
        signal.pause()


if __name__ == "__main__":
    main()
