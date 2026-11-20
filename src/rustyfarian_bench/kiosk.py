"""The bench kiosk: draw the status file on the SPI panel's framebuffer.

`rustyfarian-bench kiosk` reads the record `serve` keeps (see `status`), turns it into drawing
primitives (`kiosk_layout`) and paints them as RGB565 onto the panel. It draws only when the
primitives change. This module needs Pillow (the `kiosk` extra); `just display-smoke` renders it on
the controller into tmp/display/ with a fake framebuffer, which BENCH_SYSFS_GRAPHICS and
BENCH_DEV_DIR point it at.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import kiosk_layout as kl
from .status import Status, parse_status

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FB_POLL_SECONDS = 5
MIN_FONT = 12

log = logging.getLogger("rustyfarian-bench-kiosk")

Render = Callable[[tuple[kl.Primitive, ...], int, int], Image.Image]


class UnsupportedFramebuffer(Exception):
    """The panel is not 16 bits per pixel; nothing else is drawn."""


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


def fitted_font(
    text: str, size: int, max_width: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """DejaVu Sans Bold at `size`, shrunk until `text` fits `max_width`, or Pillow's own font."""
    while True:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        try:
            font = ImageFont.truetype(str(FONT), size)
        except OSError:
            font = ImageFont.load_default(size)
        if font.getlength(text) <= max_width or size - 2 < MIN_FONT:
            return font
        size -= 2


def _text_width(item: kl.Text, width: int) -> int:
    """Room for the text on the panel, by its anchor, so a long line shrinks instead of clipping."""
    if item.anchor.startswith("r"):
        return item.x
    if item.anchor.startswith("m"):
        return 2 * min(item.x, width - item.x)
    return width - item.x


def fit(item: kl.Text) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    """The font and text to draw `item` within its `max_width`, or within the panel without one.

    The font shrinks first; text that is still too wide at the smallest size loses characters
    from its end, marked with an ellipsis; every layout column is wider than the ellipsis alone.
    """
    limit = item.max_width if item.max_width is not None else _text_width(item, kl.WIDTH)
    font = fitted_font(item.text, item.size, limit)
    text = item.text
    n = len(text)
    while n > 0 and font.getlength(text) > limit:
        n -= 1
        text = item.text[:n].rstrip() + "…"
    return font, text


def paint(prims: tuple[kl.Primitive, ...], width: int, height: int) -> Image.Image:
    """The primitives drawn on a black canvas of the panel's size."""
    image = Image.new("RGB", (kl.WIDTH, kl.HEIGHT), kl.BACKGROUND)
    draw = ImageDraw.Draw(image)
    for item in prims:
        if isinstance(item, kl.Rect):
            if item.x1 > item.x0 and item.y1 > item.y0:
                draw.rectangle((item.x0, item.y0, item.x1 - 1, item.y1 - 1), fill=item.fill)
        elif isinstance(item, kl.Line):
            draw.line((item.x0, item.y0, item.x1, item.y1), fill=item.fill, width=item.width)
        else:
            font, text = fit(item)
            draw.text((item.x, item.y), text, font=font, fill=item.fill, anchor=item.anchor)
    if (width, height) != image.size:
        image = image.resize((width, height))
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


def write_frame(device: Path, data: bytes) -> None:
    """Write one frame to the device node."""
    # no O_CREAT: a device node that is not there yet must not become a plain file
    with open(device, "wb", opener=lambda path, _: os.open(path, os.O_WRONLY)) as dev:
        dev.write(data)


def _read_status(path: Path) -> Status | None:
    """The record, or None when the file is missing, unreadable or garbage."""
    try:
        return parse_status(path.read_bytes())
    except OSError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def run(
    status_path: Path,
    *,
    title: str,
    address: str,
    version: str,
    fb_match: str,
    sysfs: Path,
    dev_dir: Path,
    png_dir: Path | None = None,
    once: bool = False,
    poll: float = 1.0,
    now: Callable[[], datetime] = _now,
    wait: Callable[[float], bool] | None = None,
    render: Render = paint,
) -> int:
    """Draw the status until `wait` returns True, then the farewell; the exit code.

    `version` is shown when the record has none (no readable file). `wait(seconds)` is the
    loop's sleep and its stop: it returns True once the kiosk must stop. With `once` the screen
    is drawn one time and the farewell is not.
    """
    wait = wait or threading.Event().wait
    fb = find_framebuffer(sysfs, fb_match)
    if fb is None and once:
        log.error("no framebuffer named *%s* under %s", fb_match, sysfs)
        return 1
    if fb is None:
        log.info("waiting for the panel framebuffer (*%s*)", fb_match)
    while fb is None:
        if wait(FB_POLL_SECONDS):
            return 0
        fb = find_framebuffer(sysfs, fb_match)

    def draw(prims: tuple[kl.Primitive, ...], png_name: str) -> None:
        width, height, bpp, stride = geometry(fb)
        if bpp != 16:
            raise UnsupportedFramebuffer(f"{fb.name} has {bpp} bits per pixel, expected 16")
        image = render(prims, width, height)
        if png_dir is not None:
            png_dir.mkdir(parents=True, exist_ok=True)
            image.save(png_dir / png_name)
        write_frame(dev_dir / fb.name, to_rgb565(image, stride))

    last: tuple[kl.Primitive, ...] | None = None
    failing = False
    while True:
        status = _read_status(status_path)
        shown = status.version if status else version
        prims = kl.layout(status, now(), title=title, address=address, version=shown)
        if prims != last:
            try:
                draw(prims, "kiosk.png")
            except UnsupportedFramebuffer as exc:
                log.error("%s", exc)
                return 1
            except (OSError, ValueError) as exc:
                # the node is not there yet, or udev has not handed it to group video yet
                if not failing:
                    log.warning("cannot draw on %s: %s; retrying", fb.name, exc)
                failing = True
                if once:
                    return 1
            else:
                failing = False
                last = prims
                log.info("drawn on %s: %s", fb.name, kl.classify(status, now()).value)
        if once:
            return 0
        if wait(poll):
            break
    try:
        draw(kl.farewell(title), "kiosk-farewell.png")
    except (OSError, ValueError, UnsupportedFramebuffer) as exc:
        # the panel going away first must not fail the stop
        log.warning("no farewell drawn: %s", exc)
    return 0
