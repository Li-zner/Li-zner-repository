#!/usr/bin/env python
"""Test the voice transcription endpoint from within the container."""
import jwt, requests, io, wave
from datetime import datetime, timedelta

SECRET_KEY = '03c7f8909e4400e6e4b07944ddf3ddd83db6b07c536dc1d7d7a438a0251cd781'
token = jwt.encode({'sub': 'admin', 'exp': datetime.utcnow() + timedelta(days=1)}, SECRET_KEY, algorithm='HS256')
print('Token:', token[:30])

# Create silent WAV
buf = io.BytesIO()
with wave.open(buf, 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b'\x00\x00' * 16000)
buf.seek(0)

# Test via nginx internal
r = requests.post('http://nginx_lb/v2/audio/transcribe',
    headers={'Authorization': f'Bearer {token}'},
    files={'file': ('test.wav', buf, 'audio/wav')}, timeout=180)
print('Via nginx:', r.status_code)
print('Response:', r.text[:500])
