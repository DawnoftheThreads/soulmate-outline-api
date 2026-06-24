from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import urllib.request, base64, os, tempfile, io, hmac, hashlib, json
import fal_client
import requests as http_requests
from PIL import Image
import numpy as np

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://'
)

PROMPT = (
    'Convert to a clean fine line art portrait drawing. '
    'Single weight black outlines only on pure white background. '
    'No shading, no cross-hatching, no texture fills, no grey tones. '
    'Clean coloring book style outlines. 2K resolution.'
)

PRINTFUL_KEY       = os.environ.get('PRINTFUL_KEY')        # mockup token (read scopes)
PRINTFUL_ORDER_KEY = os.environ.get('PRINTFUL_ORDER_KEY')  # orders write token
SHOPIFY_WEBHOOK_SECRET = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')

# ── Per-product placement for Printful Mockup Generator API ───────────────
# GET /mockup-generator/printfiles/{product_id} lists valid placements per product.
# Apparel uses 'front'; flat/wrap products use 'default'.
PRODUCT_PLACEMENT = {
    1:   'front',    # Art Print (poster)
    3:   'default',  # Canvas Print (stretched canvas)
    19:  'default',  # White Mug (11 oz / 15 oz)
    71:  'front',    # Bella+Canvas 3001 T-Shirt
    145: 'front',    # Gildan 18000 Sweatshirt
    214: 'front',    # Throw Pillow
    234: 'front',    # Baby Bodysuit
    367: 'front',    # Tote Bag
    380: 'front',    # Gildan 18500 Hoodie
    594: 'front',    # Gym Bag
    678: 'default',  # Pet Bowl
    683: 'front',    # Phone Case
    711: 'front',    # Sherpa Blanket
    902: 'front',    # Pet Bandana Collar
}

# Cache: (product_id, placement) → position dict fetched from Printful printfiles API
_printfile_cache = {}

# ── Shopify variant ID → Printful variant + dark-product flag ──────────────
# dark=True  → invert image (white lines on black/dark substrate)
# dark=False → keep black lines on white/light substrate
VARIANT_MAP = {
    # ── T-Shirt (product 71) ──────────────────────────────────────────────
    58321490149760: {'variant_id': 9575,  'on_dark': True},   # Black Heather / XS
    58321490313600: {'variant_id': 8923,  'on_dark': True},   # Black Heather / S
    58321490346368: {'variant_id': 8924,  'on_dark': True},   # Black Heather / M
    58321490379136: {'variant_id': 8925,  'on_dark': True},   # Black Heather / L
    58321490411904: {'variant_id': 8926,  'on_dark': True},   # Black Heather / XL
    58321490444672: {'variant_id': 8927,  'on_dark': True},   # Black Heather / 2XL
    58321490477440: {'variant_id': 9526,  'on_dark': False},  # White / XS
    58321490510208: {'variant_id': 4011,  'on_dark': False},  # White / S
    58321490542976: {'variant_id': 4012,  'on_dark': False},  # White / M
    58321490575744: {'variant_id': 4013,  'on_dark': False},  # White / L
    58321490608512: {'variant_id': 4014,  'on_dark': False},  # White / XL
    58321490641280: {'variant_id': 4015,  'on_dark': False},  # White / 2XL

    # ── Hoodie (product 380) ──────────────────────────────────────────────
    58292420542848: {'variant_id': 10779, 'on_dark': True},   # Black / S
    58292420575616: {'variant_id': 10780, 'on_dark': True},   # Black / M
    58292420608384: {'variant_id': 10781, 'on_dark': True},   # Black / L
    58292420641152: {'variant_id': 10782, 'on_dark': True},   # Black / XL
    58292420673920: {'variant_id': 10783, 'on_dark': True},   # Black / 2XL
    58292420706688: {'variant_id': 13416, 'on_dark': True},   # Black / 3XL
    58292420739456: {'variant_id': 10774, 'on_dark': False},  # White / S
    58292420772224: {'variant_id': 10775, 'on_dark': False},  # White / M
    58292420804992: {'variant_id': 10776, 'on_dark': False},  # White / L
    58292420837760: {'variant_id': 10777, 'on_dark': False},  # White / XL
    58292420870528: {'variant_id': 10778, 'on_dark': False},  # White / 2XL
    58292420903296: {'variant_id': 13421, 'on_dark': False},  # White / 3XL

    # ── Sweatshirt (product 145) ──────────────────────────────────────────
    58292428538240: {'variant_id': 5434,  'on_dark': True},   # Black / S
    58292428571008: {'variant_id': 5435,  'on_dark': True},   # Black / M
    58292428603776: {'variant_id': 5436,  'on_dark': True},   # Black / L
    58292428636544: {'variant_id': 5437,  'on_dark': True},   # Black / XL
    58292428669312: {'variant_id': 5426,  'on_dark': False},  # White / S
    58292428702080: {'variant_id': 5427,  'on_dark': False},  # White / M
    58292428734848: {'variant_id': 5428,  'on_dark': False},  # White / L
    58292428767616: {'variant_id': 5429,  'on_dark': False},  # White / XL

    # ── Mug (product 19) ─────────────────────────────────────────────────
    58321490706816: {'variant_id': 1320,  'on_dark': False},  # 11 oz
    58321490772352: {'variant_id': 4830,  'on_dark': False},  # 15 oz

    # ── Tote Bag (product 367) ───────────────────────────────────────────
    58321530552704: {'variant_id': 10457, 'on_dark': True},   # Black

    # ── Canvas Print (product 3) ──────────────────────────────────────────
    58321490805120: {'variant_id': 823,   'on_dark': False},  # 12x12 in
    58321491362176: {'variant_id': 5,     'on_dark': False},  # 12x16 in
    58321491394944: {'variant_id': 6,     'on_dark': False},  # 16x20 in
    58321491427712: {'variant_id': 7,     'on_dark': False},  # 18x24 in
    58321491460480: {'variant_id': 825,   'on_dark': False},  # 24x36 in

    # ── Throw Pillow (product 214) ────────────────────────────────────────
    58321491526016: {'variant_id': 7907,  'on_dark': False},  # 20x12 in
    58321492738432: {'variant_id': 9515,  'on_dark': False},  # 18x18 in
    58321492771200: {'variant_id': 11077, 'on_dark': False},  # 22x22 in

    # ── Sherpa Blanket (product 711) ──────────────────────────────────────
    58321492869504: {'variant_id': 17483, 'on_dark': False},  # 37x57 in
    58321492902272: {'variant_id': 17482, 'on_dark': False},  # 50x60 in
    58321492935040: {'variant_id': 17449, 'on_dark': False},  # 60x80 in

    # ── Pet Bandana Collar (product 902) ──────────────────────────────────
    58321531175296: {'variant_id': 23142, 'on_dark': False},  # S
    58321540809088: {'variant_id': 23141, 'on_dark': False},  # M
    58321540841856: {'variant_id': 23140, 'on_dark': False},  # L
    58321540874624: {'variant_id': 23143, 'on_dark': False},  # XL

    # ── Pet Bowl (product 678) ────────────────────────────────────────────
    58321531535744: {'variant_id': 16785, 'on_dark': False},  # 18 oz
    58321541104000: {'variant_id': 16786, 'on_dark': False},  # 32 oz

    # ── Baby Bodysuit (product 234) ───────────────────────────────────────
    58321531863424: {'variant_id': 8177,  'on_dark': True},   # Black / 12M
    58321541267840: {'variant_id': 8178,  'on_dark': True},   # Black / 18M
    58321541300608: {'variant_id': 8179,  'on_dark': True},   # Black / 24M
    58321541333376: {'variant_id': 8182,  'on_dark': False},  # Heather / 12M
    58321541366144: {'variant_id': 8183,  'on_dark': False},  # Heather / 18M
    58321541398912: {'variant_id': 8184,  'on_dark': False},  # Heather / 24M
    58321541431680: {'variant_id': 8187,  'on_dark': False},  # Pink / 12M
    58321541464448: {'variant_id': 8188,  'on_dark': False},  # Pink / 18M
    58321541497216: {'variant_id': 8189,  'on_dark': False},  # Pink / 24M
    58321541529984: {'variant_id': 8172,  'on_dark': False},  # White / 12M
    58321541562752: {'variant_id': 8173,  'on_dark': False},  # White / 18M
    58321541595520: {'variant_id': 8174,  'on_dark': False},  # White / 24M

    # ── Gym Bag (product 594) ─────────────────────────────────────────────
    58321531928960: {'variant_id': 15155, 'on_dark': False},  # One Size

    # ── Art Print (product 1) ─────────────────────────────────────────────
    58321538449792: {'variant_id': 4463,  'on_dark': False},  # 8x10 in
    58321541726592: {'variant_id': 1349,  'on_dark': False},  # 12x16 in
    58321541759360: {'variant_id': 3877,  'on_dark': False},  # 16x20 in
    58321541792128: {'variant_id': 1,     'on_dark': False},  # 18x24 in
    58321541824896: {'variant_id': 2,     'on_dark': False},  # 24x36 in

    # ── Phone Case (product 683) ──────────────────────────────────────────
    58321538548096: {'variant_id': 16910, 'on_dark': False},  # iPhone 14 / Glossy
    58321542218112: {'variant_id': 16911, 'on_dark': False},  # iPhone 14 / Matte
    58321542250880: {'variant_id': 16912, 'on_dark': False},  # iPhone 14 Pro / Glossy
    58321542283648: {'variant_id': 16913, 'on_dark': False},  # iPhone 14 Pro / Matte
    58321542316416: {'variant_id': 16914, 'on_dark': False},  # iPhone 14 Plus / Glossy
    58321542349184: {'variant_id': 16915, 'on_dark': False},  # iPhone 14 Plus / Matte
    58321542381952: {'variant_id': 16916, 'on_dark': False},  # iPhone 14 Pro Max / Glossy
    58321542414720: {'variant_id': 16917, 'on_dark': False},  # iPhone 14 Pro Max / Matte
    58321542447488: {'variant_id': 17722, 'on_dark': False},  # iPhone 15 / Glossy
    58321542480256: {'variant_id': 17723, 'on_dark': False},  # iPhone 15 / Matte
    58321542513024: {'variant_id': 17726, 'on_dark': False},  # iPhone 15 Pro / Glossy
    58321542545792: {'variant_id': 17727, 'on_dark': False},  # iPhone 15 Pro / Matte
    58321542578560: {'variant_id': 17724, 'on_dark': False},  # iPhone 15 Plus / Glossy
    58321542611328: {'variant_id': 17725, 'on_dark': False},  # iPhone 15 Plus / Matte
    58321542644096: {'variant_id': 17728, 'on_dark': False},  # iPhone 15 Pro Max / Glossy
    58321542676864: {'variant_id': 17729, 'on_dark': False},  # iPhone 15 Pro Max / Matte
    58321542709632: {'variant_id': 20294, 'on_dark': False},  # iPhone 16 / Glossy
    58321542742400: {'variant_id': 20298, 'on_dark': False},  # iPhone 16 / Matte
    58321542775168: {'variant_id': 20296, 'on_dark': False},  # iPhone 16 Pro / Glossy
    58321542807936: {'variant_id': 20300, 'on_dark': False},  # iPhone 16 Pro / Matte
    58321542840704: {'variant_id': 20295, 'on_dark': False},  # iPhone 16 Plus / Glossy
    58321542873472: {'variant_id': 20299, 'on_dark': False},  # iPhone 16 Plus / Matte
    58321542906240: {'variant_id': 20297, 'on_dark': False},  # iPhone 16 Pro Max / Glossy
    58321542939008: {'variant_id': 20301, 'on_dark': False},  # iPhone 16 Pro Max / Matte
    58321542971776: {'variant_id': 34009, 'on_dark': False},  # iPhone 17 / Glossy
    58321543004544: {'variant_id': 34010, 'on_dark': False},  # iPhone 17 / Matte
    58321543037312: {'variant_id': 34011, 'on_dark': False},  # iPhone 17 Air / Glossy
    58321543070080: {'variant_id': 34012, 'on_dark': False},  # iPhone 17 Air / Matte
    58321543102848: {'variant_id': 34013, 'on_dark': False},  # iPhone 17 Pro / Glossy
    58321543135616: {'variant_id': 34014, 'on_dark': False},  # iPhone 17 Pro / Matte
    58321543168384: {'variant_id': 34015, 'on_dark': False},  # iPhone 17 Pro Max / Glossy
    58321543201152: {'variant_id': 34016, 'on_dark': False},  # iPhone 17 Pro Max / Matte
}


def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

app.after_request(add_cors)


def generate_line_art(photo_url: str):
    result = fal_client.run(
        'fal-ai/nano-banana-pro/edit',
        arguments={
            'image_urls': [photo_url],
            'prompt': PROMPT,
            'resolution': '2K',
        }
    )
    out_url = result['images'][0]['url']
    req = urllib.request.Request(out_url, headers={'User-Agent': 'SoulmateAPI/15'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return out_url, r.read()


def process_line_art(img_bytes: bytes, on_dark: bool) -> bytes:
    """Convert line art to transparent PNG for dark or light products.
    - on_dark=True:  invert (black->white lines), use brightness as alpha,
                     force RGB to pure white — crisp white lines, no colour tint
    - on_dark=False: keep black lines + make near-white transparent
    """
    img = Image.open(io.BytesIO(img_bytes)).convert('RGBA')
    data = np.array(img, dtype=np.uint8)

    if on_dark:
        # Invert: black lines become white, white background becomes black
        data[:, :, :3] = 255 - data[:, :, :3]
        r = data[:, :, 0].astype(np.float32)
        g = data[:, :, 1].astype(np.float32)
        b = data[:, :, 2].astype(np.float32)
        # Use per-channel minimum as brightness proxy — only truly white pixels
        # (from originally black lines) score near 255; everything else falls off.
        # gamma=2 pushes mid-grays toward transparent, sharpening the result.
        min_chan = np.minimum(np.minimum(r, g), b)
        # Threshold at 40: any pixel brighter than ~16% gets full opacity.
        # This ensures all line pixels (min_chan >> 40 after inversion) become
        # fully opaque white, giving crisp bright lines in the Printful mockup.
        alpha = np.clip(min_chan.astype(np.float32) * (255.0 / 40.0), 0, 255).astype(np.uint8)
        # Force RGB to pure white so no brownish/warm tint survives in Printful mockup
        data[:, :, :3] = 255
        data[:, :, 3] = alpha
    else:
        r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
        mask = (r > 200) & (g > 200) & (b > 200)
        data[mask, 3] = 0

    out = io.BytesIO()
    Image.fromarray(data).save(out, 'PNG')
    return out.getvalue()


def prepare_design_url(line_art_url: str, on_dark: bool) -> str:
    """Download line art, process it, upload to fal CDN, return public URL."""
    req = urllib.request.Request(line_art_url, headers={'User-Agent': 'SoulmateAPI/15'})
    with urllib.request.urlopen(req, timeout=60) as r:
        img_bytes = r.read()

    processed = process_line_art(img_bytes, on_dark)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
        tmp.write(processed)
        tmp_path = tmp.name

    try:
        return fal_client.upload_file(tmp_path)
    finally:
        os.unlink(tmp_path)


def get_position_for_product(product_id: int, placement: str) -> dict:
    """Fetch print area dimensions from Printful printfiles API (cached per product/placement)."""
    cache_key = (product_id, placement)
    if cache_key in _printfile_cache:
        return _printfile_cache[cache_key]

    pf = None
    try:
        resp = http_requests.get(
            f'https://api.printful.com/mockup-generator/printfiles/{product_id}',
            headers={'Authorization': f'Bearer {PRINTFUL_KEY}'},
            timeout=15
        )
        data = resp.json()
        printfiles = data.get('result', {}).get('printfiles', [])
        pf = next((p for p in printfiles if p.get('placement') == placement), None)
        if not pf and printfiles:
            pf = printfiles[0]
    except Exception:
        pf = None

    if pf and pf.get('width') and pf.get('height'):
        position = {
            'area_width':          pf['width'],
            'area_height':         pf['height'],
            'width':               pf['width'],
            'height':              pf['height'],
            'top':                 0,
            'left':                0,
            'limit_to_print_area': True,
        }
    else:
        # Fallback: generic apparel dimensions (1800×2400 @ 150 DPI = 12"×16")
        position = {
            'area_width':          1800,
            'area_height':         2400,
            'width':               1800,
            'height':              2400,
            'top':                 0,
            'left':                0,
            'limit_to_print_area': True,
        }

    _printfile_cache[cache_key] = position
    return position


# ============================================================================
# ROUTES
# ============================================================================

@app.route('/', methods=['GET'])
@app.route('/api/process', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'Soulmate Custom Gifts — Photo Outline API v17',
        'pipeline': 'fal.ai nano-banana-pro/edit -> fine line drawing + Printful mockups + order fulfillment'
    })


@app.route('/upload', methods=['OPTIONS', 'POST'])
def upload():
    if request.method == 'OPTIONS':
        return '', 200

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    if not os.environ.get('FAL_KEY'):
        return jsonify({'error': 'Missing FAL_KEY env var'}), 400

    ext = os.path.splitext(file.filename)[1] or '.jpg'
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        url = fal_client.upload_file(tmp_path)
        return jsonify({'success': True, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        os.unlink(tmp_path)


@app.route('/api/process', methods=['POST'])
@limiter.limit('5 per hour')
def process():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON body'}), 400
    photo_url = data.get('photo_url')
    if not photo_url:
        return jsonify({'error': 'Missing photo_url'}), 400
    if not os.environ.get('FAL_KEY'):
        return jsonify({'error': 'Missing FAL_KEY env var'}), 400
    try:
        line_art_url, lineart_bytes = generate_line_art(photo_url)
    except Exception as e:
        import traceback
        return jsonify({'error': f'[fal-nano-banana-pro] {e}',
                        'trace': traceback.format_exc()[-800:]}), 500
    return jsonify({
        'success': True,
        'line_art_url': line_art_url,
        'sketch_png_base64': base64.b64encode(lineart_bytes).decode(),
        'note': 'fal.ai nano-banana-pro/edit -- fine line drawing'
    })


@app.route('/mockup/start', methods=['OPTIONS', 'POST'])
def mockup_start():
    """Process line art + start Printful mockup task. Returns task_key for polling."""
    if request.method == 'OPTIONS':
        return '', 200

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    line_art_url     = data.get('line_art_url')
    product_id       = data.get('product_id')
    variant_ids      = data.get('variant_ids')
    on_dark          = data.get('on_dark', False)
    custom_position  = data.get('position')   # optional: customer-chosen position from canvas

    if not all([line_art_url, product_id, variant_ids]):
        return jsonify({'error': 'Missing required fields: line_art_url, product_id, variant_ids'}), 400

    if not PRINTFUL_KEY:
        return jsonify({'error': 'Missing PRINTFUL_KEY env var'}), 400

    if not os.environ.get('FAL_KEY'):
        return jsonify({'error': 'Missing FAL_KEY env var'}), 400

    try:
        design_url = prepare_design_url(line_art_url, on_dark)

        # Use product-specific placement — apparel needs 'front', flat/wrap products use 'default'
        placement = PRODUCT_PLACEMENT.get(product_id, 'front')

        # Use customer-chosen position if provided, otherwise fetch from Printful printfiles API
        if custom_position:
            position = custom_position
        else:
            position = get_position_for_product(product_id, placement)

        task_payload = {
            'variant_ids': variant_ids,
            'files': [{'placement': placement, 'image_url': design_url, 'position': position}],
            'format': 'jpg'
        }

        resp = http_requests.post(
            f'https://api.printful.com/mockup-generator/create-task/{product_id}',
            headers={'Authorization': f'Bearer {PRINTFUL_KEY}'},
            json=task_payload,
            timeout=30
        )

        resp_data = resp.json()
        if resp_data.get('code') != 200:
            return jsonify({'error': f'Printful error: {resp_data}'}), 500

        task_key = resp_data['result']['task_key']
        return jsonify({'success': True, 'task_key': task_key})

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()[-800:]}), 500


@app.route('/mockup/poll', methods=['GET'])
def mockup_poll():
    """Poll Printful mockup task. Returns status + mockup_urls when completed."""
    task_key = request.args.get('task_key')
    if not task_key:
        return jsonify({'error': 'Missing task_key'}), 400
    if not PRINTFUL_KEY:
        return jsonify({'error': 'Missing PRINTFUL_KEY env var'}), 400

    try:
        resp = http_requests.get(
            f'https://api.printful.com/mockup-generator/task?task_key={task_key}',
            headers={'Authorization': f'Bearer {PRINTFUL_KEY}'},
            timeout=15
        )
        result = resp.json().get('result', {})
        status = result.get('status', 'unknown')

        if status == 'completed':
            mockups = result.get('mockups', [])
            urls = [m['mockup_url'] for m in mockups[:3]]
            return jsonify({'status': 'completed', 'mockup_urls': urls})
        elif status == 'failed':
            return jsonify({'status': 'failed', 'error': 'Mockup generation failed'}), 500
        else:
            return jsonify({'status': status})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


_TRANSPARENT_PNG_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAABBoAAAdsCAYAAAABT+8eAAAeTUlEQVR42u3BMQEAAADCoPVPbQ0Po'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAeA3'
    'SYwABQXFW8gAAAAASUVORK5CYII='
)


@app.route('/transparent-png')
def transparent_png():
    """Serve 1050×1900 transparent PNG — used as blank design for Printful mockup generation."""
    from flask import send_file
    return send_file(
        io.BytesIO(base64.b64decode(_TRANSPARENT_PNG_B64)),
        mimetype='image/png',
        download_name='transparent.png'
    )


@app.route('/pf-blank-mockup/<int:pid>')
def pf_blank_mockup(pid):
    """DEBUG: Generate a blank (no-design) mockup for product <pid> using a transparent PNG.
    Query params: variant_id (required), placement (optional, default 'front')
    Returns: { mockup_url: ... } from Printful mockup generator.
    """
    import time
    variant_id = request.args.get('variant_id', type=int)
    placement  = request.args.get('placement', PRODUCT_PLACEMENT.get(pid, 'front'))

    if not variant_id:
        return jsonify({'error': 'Missing variant_id query param'}), 400
    if not PRINTFUL_KEY:
        return jsonify({'error': 'Missing PRINTFUL_KEY'}), 400

    # Use our own Railway endpoint as the transparent design URL
    base_url = request.host_url.rstrip('/')
    design_url = f'{base_url}/transparent-png'

    # Create the mockup task
    task_payload = {
        'variant_ids': [variant_id],
        'files': [{'placement': placement, 'image_url': design_url}],
        'format': 'jpg'
    }
    try:
        resp = http_requests.post(
            f'https://api.printful.com/mockup-generator/create-task/{pid}',
            headers={'Authorization': f'Bearer {PRINTFUL_KEY}'},
            json=task_payload,
            timeout=30
        )
        resp_data = resp.json()
        if resp_data.get('code') != 200:
            return jsonify({'error': 'Printful task error', 'data': resp_data}), 500
        task_key = resp_data['result']['task_key']
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Poll for result (max ~60s)
    for _ in range(20):
        time.sleep(3)
        try:
            poll = http_requests.get(
                f'https://api.printful.com/mockup-generator/task?task_key={task_key}',
                headers={'Authorization': f'Bearer {PRINTFUL_KEY}'},
                timeout=15
            )
            result = poll.json().get('result', {})
            status = result.get('status', 'unknown')
            if status == 'completed':
                mockups = result.get('mockups', [])
                urls = [m['mockup_url'] for m in mockups]
                return jsonify({'status': 'completed', 'task_key': task_key, 'mockup_urls': urls})
            elif status == 'failed':
                return jsonify({'status': 'failed', 'data': result}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'status': 'timeout', 'task_key': task_key}), 408


@app.route('/webhook/order', methods=['POST'])
def shopify_order_webhook():
    """
    Receives Shopify orders/create webhook.
    For each line item that has a 'Line Art Preview' property, processes
    the artwork and places a DRAFT Printful order for manual confirmation.

    Returns 200 immediately (Shopify retries on any other status code).
    """
    raw_body = request.data

    # 1. Verify Shopify HMAC signature
    if SHOPIFY_WEBHOOK_SECRET:
        hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
        digest = hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).digest()
        computed = base64.b64encode(digest).decode('utf-8')
        if not hmac.compare_digest(computed, hmac_header):
            print('[webhook/order] HMAC verification failed -- rejecting')
            return jsonify({'error': 'Invalid signature'}), 401
    else:
        print('[webhook/order] WARNING: SHOPIFY_WEBHOOK_SECRET not set -- skipping HMAC check')

    # 2. Parse order
    try:
        order = json.loads(raw_body)
    except Exception:
        return '', 200

    order_id   = order.get('id', 'unknown')
    order_name = order.get('name', f'#{order_id}')
    print(f'[webhook/order] Received order {order_name} (id={order_id})')

    # 3. Build Printful recipient
    addr = order.get('shipping_address') or order.get('billing_address', {})
    if not addr:
        print(f'[webhook/order] Order {order_name} has no shipping address -- skipping')
        return '', 200

    recipient = {
        'name':         f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip(),
        'address1':     addr.get('address1', ''),
        'address2':     addr.get('address2', '') or '',
        'city':         addr.get('city', ''),
        'state_code':   addr.get('province_code', '') or addr.get('province', ''),
        'country_code': addr.get('country_code', ''),
        'zip':          addr.get('zip', ''),
        'phone':        addr.get('phone', '') or '',
        'email':        order.get('email', ''),
    }

    # 4. Process each line item
    printful_items = []
    skipped        = []
    errors         = []

    for item in order.get('line_items', []):
        shopify_variant_id = item.get('variant_id')
        if not shopify_variant_id:
            skipped.append('item with no variant_id')
            continue

        props = {p['name']: p['value'] for p in item.get('properties', [])}
        line_art_url = props.get('_Line Art Preview', props.get('Line Art Preview', '')).strip()

        if not line_art_url:
            skipped.append(f"variant {shopify_variant_id} (no Line Art Preview property)")
            continue

        mapping = VARIANT_MAP.get(int(shopify_variant_id))
        if not mapping:
            errors.append(f"Unknown Shopify variant ID {shopify_variant_id}")
            continue

        printful_variant_id = mapping['variant_id']
        on_dark             = mapping['on_dark']

        try:
            design_url = prepare_design_url(line_art_url, on_dark)
        except Exception as e:
            errors.append(f"Design prep failed for variant {shopify_variant_id}: {e}")
            continue

        printful_items.append({
            'variant_id':   printful_variant_id,
            'quantity':     item.get('quantity', 1),
            'retail_price': str(item.get('price', '')),
            'name':         item.get('title', ''),
            'files': [{'type': 'default', 'url': design_url}],
        })

    print(f'[webhook/order] {len(printful_items)} items to print, '
          f'{len(skipped)} skipped, {len(errors)} errors')
    if skipped:
        print(f'[webhook/order] Skipped: {skipped}')
    if errors:
        print(f'[webhook/order] Errors: {errors}')

    # 5. Place draft Printful order
    if not printful_items:
        print(f'[webhook/order] Nothing to submit to Printful for order {order_name}')
        return '', 200

    order_key = PRINTFUL_ORDER_KEY or PRINTFUL_KEY
    if not order_key:
        print('[webhook/order] No Printful API key available -- cannot create order')
        return '', 200

    payload = {
        'external_id': str(order_id),
        'recipient':   recipient,
        'items':       printful_items,
        'confirm':     False,
        'retail_costs': {
            'currency': order.get('currency', 'GBP'),
            'total':    order.get('total_price'),
        },
    }

    try:
        resp = http_requests.post(
            'https://api.printful.com/orders',
            headers={'Authorization': f'Bearer {order_key}'},
            json=payload,
            timeout=60
        )
        result = resp.json()
        if result.get('code') == 200:
            pf_order_id = result['result']['id']
            print(f'[webhook/order] Printful draft order created: id={pf_order_id} '
                  f'for Shopify order {order_name}')
        else:
            print(f'[webhook/order] Printful error for order {order_name}: {result}')
    except Exception as e:
        print(f'[webhook/order] Printful API call failed for order {order_name}: {e}')

    return '', 200


@app.route('/webhook/order/test', methods=['GET'])
def webhook_test():
    """Simple health check to confirm the webhook endpoint is live."""
    return jsonify({
        'status': 'ok',
        'endpoint': '/webhook/order',
        'hmac_enabled': bool(SHOPIFY_WEBHOOK_SECRET),
        'printful_order_key_set': bool(PRINTFUL_ORDER_KEY),
        'variant_map_size': len(VARIANT_MAP),
    })


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        'error': 'rate_limited',
        'message': "You've used your 5 free previews for this hour. Please try again later or contact us for help."
    }), 429


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
