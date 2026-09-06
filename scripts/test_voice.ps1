Write-Host "=== Container Status ==="
docker ps --format "table {{.Names}}`t{{.Status}}"

Write-Host "`n=== Testing tiktoken on gateway2 ==="
docker exec agent_gateway2 python -c "import tiktoken; print('tiktoken OK')" 2>&1

Write-Host "=== Testing voice endpoint ==="
python -c @'
import jwt, requests, io, wave
from datetime import datetime, timedelta

SECRET_KEY = '03c7f8909e4400e6e4b07944ddf3ddd83db6b07c536dc1d7d7a438a0251cd781'
token = jwt.encode({'sub': 'admin', 'exp': datetime.utcnow() + timedelta(days=1)}, SECRET_KEY, algorithm='HS256')
print('Token created')

buf = io.BytesIO()
with wave.open(buf, 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b'\x00\x00' * 16000)
buf.seek(0)

r = requests.post('http://localhost:10090/v2/audio/transcribe',
    headers={'Authorization': f'Bearer {token}'},
    files={'file': ('test.wav', buf, 'audio/wav')}, timeout=180)
print('Status:', r.status_code)
print('Response:', r.text[:500])
'@
