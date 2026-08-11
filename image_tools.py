from PIL import Image, ImageOps, ImageFilter
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


def _parse_int(val, default, lo=None, hi=None):
    try:
        n = int(float(val))
    except (TypeError, ValueError):
        return default
    if lo is not None: n = max(lo, n)
    if hi is not None: n = min(hi, n)
    return n


def _parse_bool(val, default=False):
    if val is None: return default
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')


def _load_image(req):
    upload = req.files.get('file') if req.files else None
    if upload and upload.filename:
        return Image.open(BytesIO(upload.read())), upload.filename
    url = (req.args.get('url') or (req.form.get('url') if req.method == 'POST' and req.form else None))
    if not url:
        return None, None
    response = requests.get(url, timeout=15, stream=True,
                            headers={'User-Agent': 'PIXL/1.0 (+image-tools)'})
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


def _send_image(img, fmt, fname_base, start_time, original_size, quality=92):
    pil_format, mime = ALLOWED_FORMATS.get(fmt.lower(), ('PNG', 'image/png'))
    out_img = img
    if pil_format in ('JPEG', 'BMP') and out_img.mode != 'RGB':
        if out_img.mode == 'RGBA':
            bg = Image.new('RGB', out_img.size, (255, 255, 255))
            bg.paste(out_img, mask=out_img.split()[-1])
            out_img = bg
        else:
            out_img = out_img.convert('RGB')
    elif pil_format == 'GIF' and out_img.mode not in ('P', 'L'):
        out_img = out_img.convert('P', palette=Image.ADAPTIVE)
    elif out_img.mode not in ('RGB', 'RGBA', 'P', 'L'):
        out_img = out_img.convert('RGBA')

    out = BytesIO()
    kwargs = {}
    if pil_format == 'JPEG': kwargs.update(quality=max(1, min(100, quality)), optimize=True)
    elif pil_format == 'WEBP': kwargs.update(quality=max(1, min(100, quality)), method=4)
    elif pil_format == 'PNG': kwargs.update(optimize=True)
    out_img.save(out, format=pil_format, **kwargs)
    out.seek(0)
    ext = 'jpg' if pil_format == 'JPEG' else pil_format.lower()
    resp = send_file(out, mimetype=mime, as_attachment=False, download_name=f'{fname_base}.{ext}')
    resp.headers['X-Original-Size'] = f'{original_size[0]}x{original_size[1]}'
    resp.headers['X-Final-Size'] = f'{out_img.size[0]}x{out_img.size[1]}'
    resp.headers['X-Processing-Time'] = f'{time.time() - start_time:.3f}'
    resp.headers['Access-Control-Expose-Headers'] = 'X-Original-Size, X-Final-Size, X-Processing-Time'
    return resp


def _src(req):
    return req.form if (req.method == 'POST' and req.form) else req.args


def _open(req):
    try:
        img, name = _load_image(req)
    except Exception as exc:
        logger.error('Image load error: %s', exc)
        return None, None, (jsonify({'error': str(exc)}), 502)
    if img is None:
        return None, None, (jsonify({'error': 'No URL or file provided'}), 400)
    if img.mode == 'P':
        img = img.convert('RGBA')
    return img, name, None


def pixelate_endpoint(req=None):
    req = req or flask_request
    t0 = time.time()
    img, name, err = _open(req)
    if err: return err
    src = _src(req)
    block = _parse_int(src.get('block'), 10, 2, 128)
    mode = (src.get('mode') or 'simple').lower()
    out_fmt = (src.get('format') or 'png').lower()
    w, h = img.size
    tw = max(1, w // block); th = max(1, h // block)
    resample_down = Image.LANCZOS if mode == 'og' else Image.NEAREST
    small = img.resize((tw, th), resample_down)
    pix = small.resize((w, h), Image.NEAREST)
    return _send_image(pix, out_fmt, (name or 'pixl').rsplit('.', 1)[0] + '-pixelated', t0, img.size)


def invert_endpoint(req=None):
    req = req or flask_request
    t0 = time.time()
    img, name, err = _open(req)
    if err: return err
    if img.mode == 'RGBA':
        r, g, b, a = img.split()
        rgb = Image.merge('RGB', (r, g, b))
        rgb = ImageOps.invert(rgb)
        ir, ig, ib = rgb.split()
        out = Image.merge('RGBA', (ir, ig, ib, a))
    else:
        out = ImageOps.invert(img.convert('RGB'))
    return _send_image(out, (_src(req).get('format') or 'png'), (name or 'pixl').rsplit('.', 1)[0] + '-inverted', t0, img.size)


def mirror_endpoint(req=None):
    req = req or flask_request
    t0 = time.time()
    img, name, err = _open(req)
    if err: return err
    axis = (_src(req).get('axis') or 'h').lower()
    if axis == 'h': out = ImageOps.mirror(img)
    elif axis == 'v': out = ImageOps.flip(img)
    elif axis == 'both': out = ImageOps.flip(ImageOps.mirror(img))
    else: out = img
    return _send_image(out, (_src(req).get('format') or 'png'), (name or 'pixl').rsplit('.', 1)[0] + '-mirrored', t0, img.size)


def rotate_endpoint(req=None):
    req = req or flask_request
    t0 = time.time()
    img, name, err = _open(req)
    if err: return err
    src = _src(req)
    angle = _parse_int(src.get('angle'), 0, -360, 360)
    expand = _parse_bool(src.get('expand'), True)
    out = img.rotate(-angle, resample=Image.BICUBIC, expand=expand)
    return _send_image(out, (src.get('format') or 'png'), (name or 'pixl').rsplit('.', 1)[0] + f'-rot{angle}', t0, img.size)


def format_endpoint(req=None):
    req = req or flask_request
    t0 = time.time()
    img, name, err = _open(req)
    if err: return err
    src = _src(req)
    fmt = (src.get('format') or 'png').lower()
    if fmt not in ALLOWED_FORMATS:
        return jsonify({'error': 'Unsupported format'}), 400
    quality = _parse_int(src.get('quality'), 92, 1, 100)
    return _send_image(img, fmt, (name or 'pixl').rsplit('.', 1)[0], t0, img.size, quality=quality)


def remove_bg_endpoint(req=None):
    req = req or flask_request
    t0 = time.time()
    img, name, err = _open(req)
    if err: return err
    src = _src(req)
    tolerance = _parse_int(src.get('tolerance'), 32, 0, 120)
    feather = _parse_int(src.get('feather'), 4, 0, 20)
    rgba = img.convert('RGBA')
    pixels = rgba.load()
    w, h = rgba.size
    corners = [pixels[0, 0], pixels[w-1, 0], pixels[0, h-1], pixels[w-1, h-1]]
    cr = sum(p[0] for p in corners) / 4
    cg = sum(p[1] for p in corners) / 4
    cb = sum(p[2] for p in corners) / 4
    tol_sq = tolerance * tolerance * 3
    soft_sq = tol_sq * 1.6
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            dr = r - cr; dg = g - cg; db = b - cb
            d = dr*dr + dg*dg + db*db
            if d <= tol_sq:
                pixels[x, y] = (r, g, b, 0)
            elif d <= soft_sq and feather > 0:
                t = (d - tol_sq) / (soft_sq - tol_sq)
                pixels[x, y] = (r, g, b, max(0, min(255, int(a * t))))
    if feather > 0:
        alpha = rgba.split()[-1].filter(ImageFilter.GaussianBlur(radius=feather/3))
        rgba.putalpha(alpha)
    return _send_image(rgba, (src.get('format') or 'png'), (name or 'pixl').rsplit('.', 1)[0] + '-nobg', t0, img.size)
