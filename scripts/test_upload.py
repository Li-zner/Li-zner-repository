from jose import jwt
from datetime import datetime, timedelta
import requests, struct, zlib, sys, os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SECRET_KEY = '03c7f8909e4400e6e4b07944ddf3ddd83db6b07c536dc1d7d7a438a0251cd781'
token = jwt.encode({'sub': 'admin', 'exp': datetime.utcnow() + timedelta(days=1)}, SECRET_KEY, algorithm='HS256')

# Create a small test PNG
width, height = 100, 50
raw = b''
for y in range(height):
    raw += b'\x00'
    for x in range(width):
        raw += b'\xff\xff\xff'

def chunk(ctype, data):
    c = ctype + data
    return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
png_data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

for host in ['agent_gateway:10086', 'agent_gateway2:10086', 'nginx_lb']:
    try:
        r = requests.post('http://'+host+'/v2/upload',
            headers={'Authorization': 'Bearer '+token},
            files={'file': ('test.png', png_data, 'image/png')},
            timeout=30)
        print(host+': Status '+str(r.status_code))
        if r.status_code == 200:
            d = r.json()
            print('  OK: '+str(d.get('text_length','?'))+' chars, note: '+str(d.get('parse_note','')))
        else:
            print('  Body: '+r.text[:300])
    except Exception as e:
        print(host+': Error '+str(e))
