import os

STICKIED_TOKEN = os.environ.get('STICKIED_TOKEN')
MONGODB_URI = os.environ.get('MONGODB_URI')
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')

DATA_DIR = 'data'
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

SITE_URL = "https://vadrifts.onrender.com"
DEFAULT_OG_IMAGE = "https://i.imgur.com/ePueN25.png"

def make_meta(title, description, path="", override_title=False, image=None, keywords=None):
    url = f"{SITE_URL}{path}"
    og_image = image or DEFAULT_OG_IMAGE
    parts = []
    if override_title:
        parts.append(f'<title>{title}</title>')
    parts.append(f'<meta name="description" content="{description}">')
    if keywords:
        parts.append(f'<meta name="keywords" content="{keywords}">')
    parts.append('<meta name="robots" content="index, follow, max-image-preview:large">')
    parts.append(f'<link rel="canonical" href="{url}">')
    parts.append(f'<meta property="og:title" content="{title}">')
    parts.append(f'<meta property="og:description" content="{description}">')
    parts.append(f'<meta property="og:url" content="{url}">')
    parts.append('<meta property="og:type" content="website">')
    parts.append('<meta property="og:site_name" content="Vadrifts">')
    parts.append(f'<meta property="og:image" content="{og_image}">')
    parts.append('<meta name="twitter:card" content="summary_large_image">')
    parts.append(f'<meta name="twitter:title" content="{title}">')
    parts.append(f'<meta name="twitter:description" content="{description}">')
    parts.append(f'<meta name="twitter:image" content="{og_image}">')
    parts.append('<meta name="theme-color" content="#7209b7">')
    return "\n    " + "\n    ".join(parts)

HOME_META_TAGS = make_meta(
    "Vadrifts - Roblox Scripts & Tools",
    "Vadrift's all-in-one website! Roblox scripts, an image converter, and more.",
    "/",
    keywords="vadrifts, roblox scripts, roblox executor, image converter, pixel art, sprite generator"
)
SCRIPTS_META_TAGS = make_meta(
    "Vadrifts Scripts - Collection",
    "Check out our collection of all Vadrifts Scripts!",
    "/scripts",
    keywords="vadrifts scripts, roblox scripts, free roblox scripts, lua executor scripts"
)
PROJECTS_META_TAGS = make_meta(
    "Vadrifts Projects",
    "Everything Vadrifts has built \u2014 Roblox scripts, web apps, userscripts, and more.",
    "/projects",
    keywords="vadrifts projects, web apps, userscripts, roblox tools, vadrifts portfolio"
)

TEENYTUNING_META_TAGS = make_meta(
    "TeenyTuning — Free Online Audio Editor | Trim, Fade, Speed Curves, Pitch Shift",
    "Free in-browser audio editor. Trim with waveform, fade in/out, change speed with pitch lock, draw speed curves, shift pitch, extract or embed cover art. No signup, no upload limits.",
    "/teenytuning",
    override_title=True,
    image="https://i.imgur.com/J3TQ64u.png",
    keywords="audio editor, online audio editor, trim audio, fade audio, change audio speed, pitch shifter, speed curve, audio waveform, extract audio from video, cover art editor, mp3 editor, free audio tool"
)

CONVERTER_META_TAGS = make_meta(
    "Image Converter \u2014 Resize, Crop, Pixelate & Convert | Vadrifts",
    "Free online image converter. Resize, crop, pixelate, change format (PNG, JPG, WebP, GIF, BMP) or export pixel grid JSON. No upload limits, no signup.",
    "/converter",
    override_title=True,
    keywords="image converter, resize image, crop image, pixelate, png to jpg, webp converter, pixel grid json, sprite, free image tools"
)
CONVERT_META_TAGS = make_meta(
    "Convert Image Format \u2014 PNG, JPG, WebP, GIF, BMP | Vadrifts",
    "Convert any image between PNG, JPG, JPEG, WebP, GIF, BMP, and JSON. Free, fast, no signup.",
    "/convert",
    override_title=True,
    keywords="convert png to jpg, convert jpg to png, webp converter, gif converter, bmp converter, image format converter"
)
RESIZE_META_TAGS = make_meta(
    "Resize Image Online \u2014 Free Image Resizer up to 4096px | Vadrifts",
    "Resize images online up to 4096px on any side. Keep aspect ratio, fit, or stretch. Free and instant.",
    "/resize",
    override_title=True,
    keywords="resize image, image resizer, resize png, resize jpg, scale image, image dimensions"
)
CROP_META_TAGS = make_meta(
    "Crop Image Online \u2014 Free Image Cropper with Aspect Ratios | Vadrifts",
    "Crop images online for free. Free-form or aspect-locked crops with pixel-perfect control.",
    "/crop",
    override_title=True,
    keywords="crop image, image cropper, crop png, crop jpg, aspect ratio crop, free crop tool"
)
PIXELATE_META_TAGS = make_meta(
    "Pixelate Image \u2014 Pixel Art & Sprite Generator | Vadrifts",
    "Pixelate any image. Perfect for sprite art, pixel-art mockups, and retro-style assets.",
    "/pixelate",
    override_title=True,
    keywords="pixelate image, pixel art generator, sprite generator, image to pixel art, retro pixel converter"
)
PIXEL_GRID_META_TAGS = make_meta(
    "Pixel Grid JSON Exporter \u2014 RGB Array from Image | Vadrifts",
    "Export any image as a pixel grid JSON of RGB arrays. Built for sprite engines and API consumers.",
    "/pixel-grid",
    override_title=True,
    keywords="pixel grid json, rgb array, sprite json, image to json, pixel data exporter, sprite engine"
)
INVERT_META_TAGS = make_meta(
    "Invert Image Colors Online \u2014 Free Color Inverter | Vadrifts",
    "Invert the colors of any image online. Transparency stays intact. Free, fast, no signup.",
    "/invert",
    override_title=True,
    keywords="invert image, invert colors, negative image, color invert, photo negative, free invert tool"
)
MIRROR_META_TAGS = make_meta(
    "Mirror & Flip Image Online \u2014 Horizontal, Vertical, Both | Vadrifts",
    "Flip any image horizontally, vertically, or both. Free, fast, browser-based image mirror tool.",
    "/mirror",
    override_title=True,
    keywords="mirror image, flip image, flip horizontal, flip vertical, image mirror, reverse image"
)

CONVERTER_TOOL_META = {
    "/converter": CONVERTER_META_TAGS,
    "/convert": CONVERT_META_TAGS,
    "/resize": RESIZE_META_TAGS,
    "/crop": CROP_META_TAGS,
    "/pixelate": PIXELATE_META_TAGS,
    "/pixel-art": PIXELATE_META_TAGS,
    "/pixel-grid": PIXEL_GRID_META_TAGS,
    "/invert": INVERT_META_TAGS,
    "/invert-colors": INVERT_META_TAGS,
    "/mirror": MIRROR_META_TAGS,
    "/flip": MIRROR_META_TAGS,
}
