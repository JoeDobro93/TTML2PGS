import os
from PIL import Image, ImageDraw, ImageFont


def create_icon():
    # Ensure resources directory exists
    if not os.path.exists("resources"):
        os.makedirs("resources")

    # Image settings
    size = (256, 256)
    # Dark grey background to match your theme
    bg_color = (53, 53, 53)
    # Accent blue for the text
    text_color = (60, 160, 240)

    img = Image.new('RGBA', size, bg_color)
    draw = ImageDraw.Draw(img)

    # Draw a stylized "Subtitle" box at the bottom
    # (White bar representing a subtitle line)
    rect_x0, rect_y0 = 40, 190
    rect_x1, rect_y1 = 216, 215
    draw.rectangle([rect_x0, rect_y0, rect_x1, rect_y1], fill=(220, 220, 220))

    # Draw a second smaller line below it
    draw.rectangle([60, 225, 196, 235], fill=(220, 220, 220))

    # Draw "T2P" text
    # Since we can't guarantee a font, we'll draw simple block shapes or just text
    # Let's try drawing text if a default font is found, otherwise shapes.
    try:
        # standard windows font
        font = ImageFont.truetype("arial.ttf", 100)
        text = "T2P"

        # Calculate text position to center it
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        x = (size[0] - text_w) / 2
        y = (size[1] - text_h) / 2 - 20  # Shift up slightly

        draw.text((x, y), text, font=font, fill=text_color)
    except:
        # Fallback: Draw a large "T" shape manually if arial is missing
        draw.rectangle([88, 40, 168, 80], fill=text_color)  # Top bar
        draw.rectangle([108, 80, 148, 170], fill=text_color)  # Vertical bar

    # Save as .ico
    img.save("resources/icon.ico", format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("Icon saved to resources/icon.ico")


if __name__ == "__main__":
    try:
        create_icon()
    except ImportError:
        print("Please install Pillow to generate the icon: pip install Pillow")