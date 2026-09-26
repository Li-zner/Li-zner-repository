"""短信限流/验码原子脚本（2026-09-20 AUTH-1 自 phone.py 外移，行为等价）。

原"INCR→(超阈)DECR"分步版有竞态——键恰在两条命令之间过期时，DECR 会重建
无 TTL 的 -1 键，此后 count==1 永不成立、TTL 自愈失效，该手机号/IP 计数
永久累积直到永久 429 自锁。合并为单脚本：脚本内原子，中途不可能过期；
首次 INCR 必置 EXPIRE。

VERIFY_CODE_LUA 于 2026-09-22 同口径外移（脚本是纯 Redis 侧逻辑，phone.py 已
贴 600 行门禁），逐字未改。
"""

# 返回 1=放行 0=超小时/日上限 -1=分钟冷却未到
SMS_SEND_LUA = """
local n = redis.call('INCR', KEYS[2])
if n == 1 then redis.call('EXPIRE', KEYS[2], tonumber(ARGV[3])) end
if n > tonumber(ARGV[2]) then
    redis.call('DECR', KEYS[2])
    return 0
end
if redis.call('SET', KEYS[1], '1', 'NX', 'EX', tonumber(ARGV[1])) then
    return 1
end
redis.call('DECR', KEYS[2])
return -1
"""

# 返回 1=放行 0=超日上限
SMS_IP_DAY_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])) end
if n > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return 0
end
return 1
"""

# 验证码校验 Lua：锁检查、错误计数和成功消费必须在同一脚本内完成，
# 防止并发请求同时越过错误次数限制或重放同一验证码。
VERIFY_CODE_LUA = """
local function secure_equal(left, right)
    if string.len(left) ~= string.len(right) then return false end
    local diff = 0
    for i = 1, string.len(left) do
        diff = bit.bor(diff, bit.bxor(string.byte(left, i), string.byte(right, i)))
    end
    return diff == 0
end
if redis.call('GET', KEYS[1]) then return 'locked' end
local stored = redis.call('GET', KEYS[2])
if not stored then return 'expired' end
if not secure_equal(stored, ARGV[1]) then
    local count = redis.call('INCR', KEYS[3])
    if count == 1 then redis.call('EXPIRE', KEYS[3], tonumber(ARGV[3])) end
    if count >= tonumber(ARGV[2]) then
        redis.call('SET', KEYS[1], '1', 'EX', tonumber(ARGV[3]))
        redis.call('DEL', KEYS[3])
    end
    return 'invalid'
end
redis.call('DEL', KEYS[3])
redis.call('DEL', KEYS[2])
return 'ok'
"""
