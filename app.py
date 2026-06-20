from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import urllib.request, base64, os, tempfile, io
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

PRINTFUL_KEY = os.environ.get('PRINTFUL_KEY')


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
    req = urllib.request.Request(out_url, headers={'User-Agent': 'SoulmateAPI/13'})
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
        # Invert RGB so black lines become white
        data[:, :, :3] = 255 - data[:, :, :3]
        # Original white background is now black — make it transparent
        r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
        mask = (r < 60) & (g < 60) & (b < 60)
        data[mask, 3] = 0
    else:
        # Make near-white background transparent, keep dark lines
        r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
        mask = (r > 200) & (g > 200) & (b > 200)
        data[mask, 3] = 0

    out = io.BytesIO()
    Image.fromarray(data).save(out, 'PNG')
    return out.getvalue()


@app.route('/', methods=['GET'])
@app.route('/api/process', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'Soulmate Custom Gifts — Photo Outline API v13',
        'pipeline': 'fal.ai nano-banana-pro/edit → fine line drawing + Printful mockups'
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
    variant_ids  = data.get('variant_ids')   # list of ints
    on_dark      = data.get('on_dark', False)

    if not all([line_art_url, product_id, variant_ids]):
        return jsonify({'error': 'Missing required fields: line_art_url, product_id, variant_ids'}), 400

    if not PRINTFUL_KEY:
        return jsonify({'error': 'Missing PRINTFUL_KEY env var'}), 400

    if not os.environ.get('FAL_KEY'):
        return jsonify({'error': 'Missing FAL_KEY env var'}), 400

    try:
        # 1. Download line art
        req = urllib.request.Request(line_art_url, headers={'User-Agent': 'SoulmateAPI/13'})
        with urllib.request.urlopen(req, timeout=60) as r:
            img_bytes = r.read()

        # 2. Process to transparent PNG (white or inverted lines)
        processed = process_line_art(img_bytes, on_dark)

        # 3. Upload processed PNG to fal.ai CDN so Printful can fetch it
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(processed)
            tmp_path = tmp.name

        design_url = fal_client.upload_file(tmp_path)
        os.unlink(tmp_path)

        # 4. Create Printful mockup task
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


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        'error': 'rate_limited',
        'message': "You've used your 5 free previews for this hour. Please try again later or contact us for help."
    }), 429


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
