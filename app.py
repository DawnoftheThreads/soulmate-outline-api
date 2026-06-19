from flask import Flask, request, jsonify
import urllib.request, base64, json, os, time

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Replicate SDXL img2img — fine pen & ink line art portrait
# Model: stability-ai/sdxl (official, well-tested)
# ---------------------------------------------------------------------------

REPLICATE_VERSION = '39ed52f2a78e934b3ba6e2a89f5b1c712de7dfea535525255b1aa35c5565e08b'

LINE_ART_PROMPT = (
    'fine pen and ink line art illustration, detailed cross-hatching shading, '
    'engraving style portrait, clean precise ink lines on cream paper, '
    'romantic couple portrait drawing, beautiful detailed faces and hair, '
    'background scenery in line art style, sepia brown ink, '
    'professional artist illustration, fine line engraving, no colour fill'
)

LINE_ART_NEGATIVE = (
    'color, colorful, painting, watercolor, digital art, 3d render, photograph, '
    'photorealistic, blurry, low quality, bad anatomy, distorted faces, '
    'extra fingers, deformed hands, ugly, worst quality'
)


def generate_line_art(image_bytes: bytes, replicate_token: str) -> bytes:
    """Send photo to Replicate SDXL img2img, return line art PNG bytes."""
    # Detect JPEG vs PNG
    mime = 'image/jpeg' if image_bytes[:3] == b'\xff\xd8\xff' else 'image/png'
    img_data_url = f'data:{mime};base64,{base64.b64encode(image_bytes).decode()}'

    payload = {
        'version': REPLICATE_VERSION,
        'input': {
            'image':              img_data_url,
            'prompt':             LINE_ART_PROMPT,
            'negative_prompt':    LINE_ART_NEGATIVE,
            'prompt_strength':    0.65,   # 0 = keep photo, 1 = ignore photo
            'num_outputs':        1,
            'num_inference_steps': 50,
            'guidance_scale':     7.5,
            'width':              1024,
            'height':             1024,
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

    # Poll until complete (up to 3 minutes)
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
        'service':  'Soulmate Custom Gifts — Photo Outline API v6',
        'pipeline': 'Replicate SDXL img2img → fine pen & ink line art',
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
            photo_url, headers={'User-Agent': 'Mozilla/5.0 SoulmateAPI/6'})
        with urllib.request.urlopen(req, timeout=60) as r:
            photo_bytes = r.read()
    except Exception as e:
        import traceback
        return jsonify({'error': f'[step1-download] {e}',
                        'trace': traceback.format_exc()[-600:]}), 500

    # 2. Generate line art via Replicate
    try:
        lineart_bytes = generate_line_art(photo_bytes, replicate_token)
    except Exception as e:
        import traceback
        return jsonify({'error': f'[step2-replicate] {e}',
                        'trace': traceback.format_exc()[-600:]}), 500

    return jsonify({
        'success':           True,
        'sketch_png_base64': base64.b64encode(lineart_bytes).decode(),
        'note':              'Replicate SDXL img2img — pen & ink line art',
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
