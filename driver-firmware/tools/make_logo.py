"""Draw the application mark.

The mark is a square of five stripes: blue, green, yellow, orange, red,
top to bottom. The About box in the application draws the same square next
to the wordmark, from the same five colours, so the window icon and the
lockup are one mark and not two.

Every size is drawn at its own resolution rather than scaled down from one
large image. Five stripes do not divide evenly into 16 pixels, and a
resampled edge turns the one thing this mark has, a hard boundary between
two saturated colours, into a smear.

Writes logo.png, used for the window and taskbar icon, and icon.ico, used
by tools/make_desktop_shortcut.ps1.

Usage:
    python tools/make_logo.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

# Top to bottom. Keep in step with LOGO_STRIPES in tools/spectrometer/app.py.
STRIPES = ("#0099ff", "#7cde00", "#fefe00", "#ff8000", "#ff0000")

# Sizes Windows asks an .ico for, from the 16 px tray entry to the 256 px
# tile. A missing size is scaled by the shell, which is the smear this file
# exists to avoid.
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

PNG_SIZE = 512

OUT_DIR = Path(__file__).resolve().parent / "spectrometer"


def draw(size):
    """One square, drawn at exactly `size` pixels."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(img)
    n = len(STRIPES)
    for i, colour in enumerate(STRIPES):
        # Each edge comes from the exact fraction. Adding a rounded stripe
        # height n times would leave a gap or an overlap at the bottom.
        y0 = round(size * i / n)
        y1 = round(size * (i + 1) / n)
        pen.rectangle([0, y0, size - 1, y1 - 1], fill=colour)
    return img


def main():
    png = OUT_DIR / "logo.png"
    draw(PNG_SIZE).save(png)
    print(f"wrote {png} ({PNG_SIZE}x{PNG_SIZE})")

    ico = OUT_DIR / "icon.ico"
    frames = [draw(s) for s in ICO_SIZES]
    frames[-1].save(ico, format="ICO",
                    sizes=[(s, s) for s in ICO_SIZES],
                    append_images=frames[:-1])
    print(f"wrote {ico} ({', '.join(str(s) for s in ICO_SIZES)})")


if __name__ == "__main__":
    main()
