import sys
sys.path.insert(0, "/app")
from app.core.jfast import _HAS_ORJSON, dumps, loads

print("orjson active:", _HAS_ORJSON)
print("dumps:", dumps({"a": 1, "key": "value"}))
print("loads:", loads('{"b": 2}'))
