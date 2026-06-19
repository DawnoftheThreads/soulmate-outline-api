from flask import Flask, request, jsonify
import urllib.request, base64, io, os

app = Flask(__name__)

STABILITY_API = 'https://api.stability.ai'


# ---------------------------------------------------------------------------
# Multipart helper
# ---------------------------------------------------------------------------

def multipart_body(fields: dict, files: dict, boundary: str) -> bytes:
    b = boundary.encode()
    body = b''
    for name, value in fields.items():
        body += b'--' + b + b'\r\n'
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b'\r\n'
    for name, (filename, data, ct) in files.items():
        body += b'--' + b + b'\r\n'
        body += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        body += f'Content-Type: {ct}\r\n\r\n'.encode()
        body += data + b'\r\n'
    body += b'--' + b + b'--\r\n'
    return body


def stability_post(endpoint: str, api_key: str, fields: dict, files: dict) -> bytes:
    boundary = 'StabilityBoundary42'
    body = multipart_body(fields, files, boundary)
    req = urllib.request.Request(
        STABILITY_API + endpoint,
        data=body,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Accept':        'image/*',
            'Content-Type':  f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        status = r.status
        result = r.read()
    if status not in (200, 201):
        raise RuntimeError(f'Stability AI {endpoint} → {status}: {result[:400]}')
    return result


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def remove_background(image_bytes: bytes, api_key: str) -> bytes:
    return stability_post(
        '/v2beta/stable-image/edit/remove-background', api_key,
        fields={'output_format': 'png'},
        files={'image': ('photo.jpg', image_bytes, 'image/jpeg')},
    )


def generate_line_art(image_bytes: bytes, api_key: str) -> bytes:
    return stability_post(
        '/v2beta/stable-image/control/structure', api_key,
        fields={
            'prompt': (
                'fine pencil line art portrait, clean black ink outlines on white paper, '
                'minimal sketch illustration, no fill, no colour, no background, '
                'romantic couple portrait drawing'
            ),
            'control_strength': '0.85',
            'output_format':    'png',
        },
        files={'image': ('nobg.png', image_bytes, 'image/png')},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
@app.route('/api/process', methods=['GET'])
def health():
    return jsonify({
        'status':  'ok',
        'service': 'Soulmate Custom Gifts — Photo Outline API v4',
        'host':    'Railway (Stability AI pipeline)',
    })


@app.route('/api/process', methods=['POST'])
def process():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON body'}), 400

    photo_url = data.get('photo_url')
    if not photo_url:
        return jsonify({'error': 'Missing photo_url'}), 400

    api_key = data.get('stability_api_key') or os.environ.get('STABILITY_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Missing STABILITY_API_KEY env var'}), 400

    try:
        # 1. Download original photo
        req = urllib.request.Request(
            photo_url, headers={'User-Agent': 'Mozilla/5.0 SoulmateAPI/4'})
        with urllib.request.urlopen(req, timeout=60) as r:
            original_bytes = r.read()

        # 2. Remove background
        nobg_bytes = remove_background(original_bytes, api_key)

        # 3. Generate Firefly-quality line art
        lineart_bytes = generate_line_art(nobg_bytes, api_key)

        # 4. Return as base64 PNG
        return jsonify({
            'success':           True,
            'sketch_png_base64': base64.b64encode(lineart_bytes).decode(),
            'note':              'Stability AI: remove-background → structure-control line art',
        })

    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'trace': traceback.format_exc()[-800:],
        }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
