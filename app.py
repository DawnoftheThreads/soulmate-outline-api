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

# ── Shopify variant ID → Printful variant + dark-product flag ──────────────
# dark=True  → invert image (white lines on black/dark substrate)
# dark=False → keep black lines on white/light substrate
VARIANT_MAP = {
    # ── T-Shirt (product 71) ──────────────────────────────────────────────
    58292438237568: {'variant_id': 9575,  'on_dark': True},   # Black Heather / XS
    58292438270336: {'variant_id': 8923,  'on_dark': True},   # Black Heather / S
    58292438303104: {'variant_id': 8924,  'on_dark': True},   # Black Heather / M
    58292438335872: {'variant_id': 8925,  'on_dark': True},   # Black Heather / L
    58292438368640: {'variant_id': 8926,  'on_dark': True},   # Black Heather / XL

    # ── Hoodie (product 380) ──────────────────────────────────────────────
    58292420542848: {'variant_id': 10779, 'on_dark': True},   # Black / S
    58292420575616: {'variant_id': 10780, 'on_dark': True},   # Black / M
    58292420608384: {'variant_id': 10781, 'on_dark': True},   # Black / L
    58292420641152: {'variant_id': 10782, 'on_dark': True},   # Black / XL
    58292420673920: {'variant_id': 10783, 'on_dark': True},   # Black / 2XL

    # ── Sweatshirt (product 145) ──────────────────────────────────────────
    58292428538240: {'variant_id': 5434,  'on_dark': True},   # Black / S
    58292428571008: {'variant_id': 5435,  'on_dark': True},   # Black / M
    58292428603776: {'variant_id': 5436,  'on_dark': True},   # Black / L
    58292428636544: {'variant_id': 5437,  'on_dark': True},   # Black / XL
    58292428669312: {'variant_id': 5426,  'on_dark': False},  # White / S

    # ── Mug (product 19) ─────────────────────────────────────────────────
    58292442759552: {'variant_id': 1320,  'on_dark': False},  # 11 oz
    58292442792320: {'variant_id': 4830,  'on_dark': False},  # 15 oz

    # ── Tote Bag (product 367) ───────────────────────────────────────────
    58292449313152: {'variant_id': 10457, 'on_dark': True},   # Black

    # ── Canvas Print (product 3) ──────────────────────────────────────────
    58303853429120: {'variant_id': 823,   'on_dark': False},  # 12×12 in
    58303853461888: {'variant_id': 5,     'on_dark': False},  # 12×16 in
    58303853494656: {'variant_id': 6,     'on_dark': False},  # 16×20 in
    58303853527424: {'variant_id': 7,     'on_dark': False},  # 18×24 in
    58303853560192: {'variant_id': 825,   'on_dark': False},  # 24×36 in

    # ── Throw Pillow (product 214) ────────────────────────────────────────
    58303853625728: {'variant_id': 7907,  'on_dark': False},  # 20×12 in
    58303853658496: {'variant_id': 9515,  'on_dark': False},  # 18×18 in
    58303853691264: {'variant_id': 11077, 'on_dark': False},  # 22×22 in

    # ── Sherpa Blanket (product 711) ──────────────────────────────────────
    58303853855104: {'variant_id': 17483, 'on_dark': False},  # 37×57 in
    58303853887872: {'variant_id': 17482, 'on_dark': False},  # 50×60 in
    58303853920640: {'variant_id': 17449, 'on_dark': False},  # 60×80 in

    # ── Pet Bandana Collar (product 902) ──────────────────────────────────
    58303853953408: {'variant_id': 23142, 'on_dark': False},  # S
    58303853986176: {'variant_id': 23141, 'on_dark': False},  # M
    58303854018944: {'variant_id': 23140, 'on_dark': False},  # L
    58303854051712: {'variant_id': 23143, 'on_dark': False},  # XL

    # ── Pet Bowl (product 678) ────────────────────────────────────────────
    58303854084480: {'variant_id': 16785, 'on_dark': False},  # 18 oz
    58303854117248: {'variant_id': 16786, 'on_dark': False},  # 32 oz
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
    req = urllib.request.Request(out_url, headers={'User-Agent': 'SoulmateAPI/14'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return out_url, r.read()


def process_line_art(img_bytes: bytes, on_dark: bool) -> bytes:
    """Convert line art to transparent PNG for dark or light products.
    - on_dark=True:  invert (black→white lines) + make near-black transparent
    - on_dark=False: keep black lines + make near-white transparent
    """
    img = Image.open(io.BytesIO(img_bytes)).convert('RGBA')
    data = np.array(img, dtype=np.uint8)

    if on_dark:
        data[:, :, :3] = 255 - data[:, :, :3]
        r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
        mask = (r < 60) & (g < 60) & (b < 60)
        data[mask, 3] = 0
    else:
        r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
        mask = (r > 200) & (g > 200) & (b > 200)
        data[mask, 3] = 0

    out = io.BytesIO()
    Image.fromarray(data).save(out, 'PNG')
    return out.getvalue()


def prepare_design_url(line_art_url: str, on_dark: bool) -> str:
    """Download line art, process it, upload to fal CDN, return public URL."""
    req = urllib.request.Request(line_art_url, headers={'User-Agent': 'SoulmateAPI/14'})
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


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/', methods=['GET'])
@app.route('/api/process', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'Soulmate Custom Gifts — Photo Outline API v14',
        'pipeline': 'fal.ai nano-banana-pro/edit → fine line drawing + Printful mockups + order fulfillment'
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
        'note': 'fal.ai nano-banana-pro/edit — fine line drawing'
    })


@app.route('/mockup/start', methods=['OPTIONS', 'POST'])
def mockup_start():
    """Process line art + start Printful mockup task. Returns task_key for polling."""
    if request.method == 'OPTIONS':
        return '', 200

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    line_art_url = data.get('line_art_url')
    product_id   = data.get('product_id')
    variant_ids  = data.get('variant_ids')
    on_dark      = data.get('on_dark', False)

    if not all([line_art_url, product_id, variant_ids]):
        return jsonify({'error': 'Missing required fields: line_art_url, product_id, variant_ids'}), 400

    if not PRINTFUL_KEY:
        return jsonify({'error': 'Missing PRINTFUL_KEY env var'}), 400

    if not os.environ.get('FAL_KEY'):
        return jsonify({'error': 'Missing FAL_KEY env var'}), 400

    try:
        design_url = prepare_design_url(line_art_url, on_dark)

        task_payload = {
            'variant_ids': variant_ids,
            'files': [{'placement': 'default', 'image_url': design_url}],
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


@app.route('/webhook/order', methods=['POST'])
def shopify_order_webhook():
    """
    Receives Shopify orders/create webhook.
    For each line item that has a 'Line Art Preview' property, processes
    the artwork and places a DRAFT Printful order for manual confirmation.

    Returns 200 immediately (Shopify retries on any other status code).
    """
    raw_body = request.data

    # ── 1. Verify Shopify HMAC signature ──────────────────────────────────
    if SHOPIFY_WEBHOOK_SECRET:
        hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
        digest = hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).digest()
        computed = base64.b64encode(digest).decode('utf-8')
        if not hmac.compare_digest(computed, hmac_header):
            print('[webhook/order] HMAC verification failed — rejecting')
            return jsonify({'error': 'Invalid signature'}), 401
    else:
        print('[webhook/order] WARNING: SHOPIFY_WEBHOOK_SECRET not set — skipping HMAC check')

    # ── 2. Parse order ─────────────────────────────────────────────────────
    try:
        order = json.loads(raw_body)
    except Exception:
        return '', 200

    order_id   = order.get('id', 'unknown')
    order_name = order.get('name', f'#{order_id}')
    print(f'[webhook/order] Received order {order_name} (id={order_id})')

    # ── 3. Build Printful recipient ────────────────────────────────────────
    addr = order.get('shipping_address') or order.get('billing_address', {})
    if not addr:
        print(f'[webhook/order] Order {order_name} has no shipping address — skipping')
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

    # ── 4. Process each line item ──────────────────────────────────────────
    printful_items = []
    skipped        = []
    errors         = []

    for item in order.get('line_items', []):
        shopify_variant_id = item.get('variant_id')
        if not shopify_variant_id:
            skipped.append('item with no variant_id')
            continue

        # Extract line art URL from line item properties
        props = {p['name']: p['value'] for p in item.get('properties', [])}
        line_art_url = props.get('Line Art Preview', '').strip()

        if not line_art_url:
            skipped.append(f"variant {shopify_variant_id} (no Line Art Preview property)")
            continue

        # Map Shopify variant → Printful variant
        mapping = VARIANT_MAP.get(int(shopify_variant_id))
        if not mapping:
            errors.append(f"Unknown Shopify variant ID {shopify_variant_id}")
            continue

        printful_variant_id = mapping['variant_id']
        on_dark             = mapping['on_dark']

        # Process line art and upload to CDN
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

    # ── 5. Place draft Printful order ──────────────────────────────────────
    if not printful_items:
        print(f'[webhook/order] Nothing to submit to Printful for order {order_name}')
        return '', 200

    order_key = PRINTFUL_ORDER_KEY or PRINTFUL_KEY
    if not order_key:
        print('[webhook/order] No Printful API key available — cannot create order')
        return '', 200

    payload = {
        'external_id': str(order_id),
        'recipient':   recipient,
        'items':       printful_items,
        'confirm':     False,   # Draft — review in Printful dashboard before confirming
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
            print(f'[webhook/order] ✅ Printful draft order created: id={pf_order_id} '
                  f'for Shopify order {order_name}')
        else:
            print(f'[webhook/order] ❌ Printful error for order {order_name}: {result}')
    except Exception as e:
        print(f'[webhook/order] ❌ Printful API call failed for order {order_name}: {e}')

    # Always return 200 — Shopify retries on any other code
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
