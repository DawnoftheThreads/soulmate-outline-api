from flask import Flask, request, jsonify
import urllib.request, base64, json, os, time, io

app = Flask(__name__)

# ---------------------------------------------------------------------------
# ControlNet Scribble — photo → edge map → fine ink line art portrait
# jagilley/controlnet-scribble on Replicate
# ---------------------------------------------------------------------------

CONTROLNET_VERSION = '435061a1b5a4c1e26740464bf786efdfa9cb3a3ac488595a2de23e143fdb0117'

LINE_ART_PROMPT = (
    'clean line art portrait illustration, fine single ink outlines, '
    'romantic couple portrait, beautiful detailed faces and hair, '
    'smooth flowing ink lines on cream paper, sepia brown ink, '
    'professional portrait sketch, no shading, no fill, no cross-hatching, '
    'pure clean outlines only, high detail line drawing'
)

LINE_ART_NEGATIVE = (
    'color, colorful, photograph, photorealistic, blurry, low quality, '
    'bad anatomy, distorted faces, watercolor, painting, digital art, '
    'extra fingers, deformed'
)


# ---------------------------------------------------------------------------
# Step 1 — edge extraction (pure PIL, no external API)
# ---------------------------------------------------------------------------

def extract_edges(image_bytes: bytes) -> bytes:
    """Convert photo to a high-contrast edge/scribble map for ControlNet."""
    from PIL import Image, ImageFilter, ImageOps, ImageEnhance

    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')

    # ControlNet scribble works best at 512 × 512
    img = img.resize((512, 512), Image.LANCZOS)

    # Grayscale → slight blur to reduce photo noise
    gray = img.convert('L')
    gray = gray.filter(ImageFilter.GaussianBlur(radius=1.5))

    # Edge detection
    edges = gray.filter(ImageFilter.FIND_EDGES)

    # Boost contrast so lines are clear
    edges = ImageEnhance.Contrast(edges).enhance(5.0)

    # Invert: white background, black lines (ControlNet scribble convention)
    edges = ImageOps.invert(edges)

    # Back to RGB for the model
    edges = edges.convert('RGB')

    buf = io.BytesIO()
    edges.save(buf, format='PNG')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Step 2 — ControlNet Replicate call
# ---------------------------------------------------------------------------

def generate_line_art(image_bytes: bytes, replicate_token: str) -> bytes:
    """Extract edges, then run ControlNet Scribble to produce ink line art."""

    edge_bytes = extract_edges(image_bytes)
    img_data_url = (
        'data:image/png;base64,'
        + base64.b64encode(edge_bytes).decode()
    )

    payload = {
        'version': CONTROLNET_VERSION,
        'input': {
            'image':             img_data_url,
            'prompt':            LINE_ART_PROMPT,
            'negative_prompt':   LINE_ART_NEGATIVE,
            'num_samples':       '1',
            'image_resolution':  '512',
            'ddim_steps':        40,
            'scale':             9.0,
            'eta':               0.0,
        },
    }

    # Create prediction
    req = urllib.request.Request(
        'https://api.replicate.com/v1/predictions',
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f'Token {replicate_token}',
            'Content-Type':  'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        prediction = json.loads(r.read())

    if prediction.get('error'):
        raise RuntimeError(f'Replicate create error: {prediction["error"]}')

    poll_url = f'https://api.replicate.com/v1/predictions/{prediction["id"]}'

    # Poll (up to 3 minutes)
    for _ in range(90):
        time.sleep(2)
        req = urllib.request.Request(
            poll_url,
            headers={'Authorization': f'Token {replicate_token}'},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            prediction = json.loads(r.read())

        status = prediction.get('status')
        if status == 'succeeded':
            output = prediction.get('output', [])
            output_url = output[0] if isinstance(output, list) and output else output
            if not output_url:
                raise RuntimeError('Replicate returned empty output')
            with urllib.request.urlopen(output_url, timeout=60) as r:
                return r.read()
        elif status in ('failed', 'canceled'):
            raise RuntimeError(
                f'Replicate prediction {status}: {prediction.get("error", "unknown")}'
            )

    raise RuntimeError('Replicate prediction timed out after 3 minutes')


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
@app.route('/api/process', methods=['GET'])
def health():
    return jsonify({
        'status':   'ok',
        'service':  'Soulmate Custom Gifts — Photo Outline API v7',
        'pipeline': 'PIL edge extraction → ControlNet Scribble → pen & ink portrait',
    })


@app.route('/api/process', methods=['POST'])
def process():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON body'}), 400

    photo_url = data.get('photo_url')
    if not photo_url:
        return jsonify({'error': 'Missing photo_url'}), 400

    replicate_token = data.get('replicate_token') or os.environ.get('REPLICATE_API_TOKEN', '')
    if not replicate_token:
        return jsonify({'error': 'Missing REPLICATE_API_TOKEN env var'}), 400

    # 1. Download photo
    try:
        req = urllib.request.Request(
            photo_url, headers={'User-Agent': 'Mozilla/5.0 SoulmateAPI/7'})
        with urllib.request.urlopen(req, timeout=60) as r:
            photo_bytes = r.read()
    except Exception as e:
        import traceback
        return jsonify({'error': f'[step1-download] {e}',
                        'trace': traceback.format_exc()[-600:]}), 500

    # 2. Edge extract + ControlNet line art
    try:
        lineart_bytes = generate_line_art(photo_bytes, replicate_token)
    except Exception as e:
        import traceback
        return jsonify({'error': f'[step2-controlnet] {e}',
                        'trace': traceback.format_exc()[-600:]}), 500

    return jsonify({
        'success':           True,
        'sketch_png_base64': base64.b64encode(lineart_bytes).decode(),
        'note':              'ControlNet Scribble — fine pen & ink line art portrait',
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
