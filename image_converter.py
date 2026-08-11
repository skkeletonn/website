from PIL import Image
from io import BytesIO
import requests
import time
import logging
from flask import jsonify, send_file, request as flask_request

logger = logging.getLogger(__name__)

ALLOWED_FORMATS = {
    'png':  ('PNG',  'image/png'),
    'jpg':  ('JPEG', 'image/jpeg'),
    'jpeg': ('JPEG', 'image/jpeg'),
    'webp': ('WEBP', 'image/webp'),
    'bmp':  ('BMP',  'image/bmp'),
    'gif':  ('GIF',  'image/gif'),
}

MAX_DIM = 4096
MAX_FETCH_BYTES = 25 * 1024 * 1024


def _resize_with_crop(image, target_size):
    ow, oh = image.size
    tw, th = target_size
    scale = max(tw / ow, th / oh)
    nw = max(1, int(round(ow * scale)))
    nh = max(1, int(round(oh * scale)))
    image = image.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return image.crop((left, top, left + tw, top + th))


def _resize_with_fit(image, target_size):
    ow, oh = image.size
    tw, th = target_size
    scale = min(tw / ow, th / oh)
    nw = max(1, int(round(ow * scale)))
    nh = max(1, int(round(oh * scale)))
    resized = image.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new('RGBA', target_size, (0, 0, 0, 0))
    if resized.mode != 'RGBA':
        resized = resized.convert('RGBA')
    canvas.paste(resized, ((tw - nw) // 2, (th - nh) // 2), resized)
    return canvas


def _parse_int(val, default, lo=1, hi=MAX_DIM):
    try:
        n = int(float(val))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _parse_bool(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')


def _load_image(req):
    upload = req.files.get('file') if req.files else None
    if upload and upload.filename:
        return Image.open(BytesIO(upload.read())), upload.filename

    url = req.args.get('url')
    if not url and req.method == 'POST':
        url = (req.form.get('url') if req.form else None)
    if not url:
        return None, None

    response = requests.get(url, timeout=15, stream=True,
                            headers={'User-Agent': 'PIXL/1.0 (+image-converter)'})
    if response.status_code != 200:
        raise RuntimeError(f'Failed to fetch image (HTTP {response.status_code})')

    buf = BytesIO()
    total = 0
    for chunk in response.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_FETCH_BYTES:
            raise RuntimeError('Remote image exceeds 25MB limit')
        buf.write(chunk)
    buf.seek(0)
    return Image.open(buf), url.rsplit('/', 1)[-1]


def _apply_pre_crop(image, src):
    cx = src.get('crop_x')
    cy = src.get('crop_y')
    cw = src.get('crop_w')
    ch = src.get('crop_h')
    if cx is None or cy is None or cw is None or ch is None:
        return image
    try:
        cx = max(0, int(float(cx)))
        cy = max(0, int(float(cy)))
        cw = max(1, int(float(cw)))
        ch = max(1, int(float(ch)))
    except (TypeError, ValueError):
        return image
    iw, ih = image.size
    right = min(iw, cx + cw)
    bottom = min(ih, cy + ch)
    if right <= cx or bottom <= cy:
        return image
    return image.crop((cx, cy, right, bottom))


def convert_image_endpoint(req=None):
    req = req or flask_request
    start_time = time.time()

    try:
        image, source_name = _load_image(req)
    except Exception as exc:
        logger.error('Image load error: %s', exc)
        return jsonify({'error': str(exc)}), 502

    if image is None:
        return jsonify({'error': 'No URL or file provided'}), 400

    src = req.form if (req.method == 'POST' and req.form) else req.args

    fmt_param = (src.get('format') or '').lower().strip()
    width = _parse_int(src.get('width'), 32)
    height = _parse_int(src.get('height'), 32)
    simple = _parse_bool(src.get('simple'))

    mode_param = (src.get('mode') or src.get('resize') or '').lower().strip()
    if not mode_param:
        legacy_crop = src.get('crop')
        if legacy_crop is not None:
            mode_param = 'crop' if _parse_bool(legacy_crop, True) else 'stretch'
        else:
            mode_param = 'crop'

    if mode_param not in ('crop', 'stretch', 'fit'):
        mode_param = 'crop'

    image = _apply_pre_crop(image, src)
    original_size = image.size

    if image.mode == 'P':
        image = image.convert('RGBA')

    resample = Image.NEAREST if simple else Image.LANCZOS

    if mode_param == 'crop':
        if simple:
            big_w = max(width * 2, width)
            big_h = max(height * 2, height)
            tmp = _resize_with_crop(image, (big_w, big_h))
            image = tmp.resize((width, height), Image.NEAREST)
            resize_method = 'simple'
        else:
            image = _resize_with_crop(image, (width, height))
            resize_method = 'crop'
    elif mode_param == 'fit':
        image = _resize_with_fit(image, (width, height))
        resize_method = 'fit'
    else:
        image = image.resize((width, height), resample)
        resize_method = 'stretch'

    if fmt_param in ALLOWED_FORMATS:
        pil_format, mime = ALLOWED_FORMATS[fmt_param]
        if pil_format in ('JPEG', 'BMP') and image.mode != 'RGB':
            if image.mode == 'RGBA':
                bg = Image.new('RGB', image.size, (255, 255, 255))
                bg.paste(image, mask=image.split()[-1])
                image = bg
            else:
                image = image.convert('RGB')
        elif pil_format == 'GIF' and image.mode not in ('P', 'L'):
            image = image.convert('P', palette=Image.ADAPTIVE)
        elif image.mode not in ('RGB', 'RGBA', 'P', 'L'):
            image = image.convert('RGBA')

        out = BytesIO()
        save_kwargs = {}
        if pil_format == 'JPEG':
            save_kwargs.update(quality=92, optimize=True)
        elif pil_format == 'WEBP':
            save_kwargs.update(quality=92, method=4)
        elif pil_format == 'PNG':
            save_kwargs.update(optimize=True)
        image.save(out, format=pil_format, **save_kwargs)
        out.seek(0)

        download_name = f'pixl-{width}x{height}.{fmt_param}'
        resp = send_file(out, mimetype=mime, as_attachment=False, download_name=download_name)
        resp.headers['X-Resize-Method'] = resize_method
        resp.headers['X-Original-Size'] = f'{original_size[0]}x{original_size[1]}'
        resp.headers['X-Final-Size'] = f'{width}x{height}'
        resp.headers['X-Processing-Time'] = f'{time.time() - start_time:.3f}'
        resp.headers['Access-Control-Expose-Headers'] = (
            'X-Resize-Method, X-Original-Size, X-Final-Size, X-Processing-Time'
        )
        return resp

    if image.mode != 'RGB':
        if image.mode == 'RGBA':
            bg = Image.new('RGB', image.size, (0, 0, 0))
            bg.paste(image, mask=image.split()[-1])
            image = bg
        else:
            image = image.convert('RGB')

    pixels = []
    px = image.load()
    for y in range(height):
        for x in range(width):
            r, g, b = px[x, y]
            pixels.append({'R': r, 'G': g, 'B': b})

    return jsonify({
        'pixels': pixels,
        'processing_time': time.time() - start_time,
        'original_size': list(original_size),
        'final_size': [width, height],
        'resize_method': resize_method,
        'simple_mode': simple,
        'source': source_name,
    })
