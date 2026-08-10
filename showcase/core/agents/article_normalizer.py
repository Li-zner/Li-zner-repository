"""
法条编号归一化工具
将口语化的"第X条"自动映射为"第一千零X条"格式
"""
import re

# 数字映射表
_DIGIT_MAP = {
    '0': '零', '1': '一', '2': '二', '3': '三', '4': '四',
    '5': '五', '6': '六', '7': '七', '8': '八', '9': '九',
}

# 中文数字→阿拉伯数字映射
_CN_DIGIT_MAP = {
    '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    '十': 10, '百': 100, '千': 1000,
}

# 法条编号前缀映射（民法典编目）
_BOOK_PREFIX = {
    1: '第一编 总则',
    2: '第二编 物权',
    3: '第三编 合同',
    4: '第四编 人格权',
    5: '第五编 婚姻家庭',
    6: '第六编 继承',
    7: '第七编 侵权责任',
}


def _arabic_to_chinese(num: int) -> str:
    """阿拉伯数字转中文数字（支持1-9999），正确处理零位"""
    if num < 10:
        return _DIGIT_MAP.get(str(num), str(num))
    
    digits = str(num)
    length = len(digits)
    
    if length == 4:
        # 千位
        result = _DIGIT_MAP[digits[0]] + '千'
        remaining = int(digits[1:])
        if remaining == 0:
            return result
        if remaining < 100:
            result += '零'
        result += _arabic_to_chinese(remaining)
        return result
    
    if length == 3:
        result = _DIGIT_MAP[digits[0]] + '百'
        remaining = int(digits[1:])
        if remaining == 0:
            return result
        if remaining < 10:
            result += '零'
        result += _arabic_to_chinese(remaining)
        return result
    
    if length == 2:
        if digits[0] == '1':
            result = '十'
        else:
            result = _DIGIT_MAP[digits[0]] + '十'
        remaining = int(digits[1])
        if remaining > 0:
            result += _DIGIT_MAP[digits[1]]
        return result
    
    return _DIGIT_MAP.get(digits[0], digits[0])


def _chinese_to_arabic(text: str) -> int:
    """中文数字转阿拉伯数字"""
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGIT_MAP:
            val = _CN_DIGIT_MAP[char]
            if val >= 10:
                if current == 0:
                    current = 1
                total += current * val
                current = 0
            else:
                current = val if current == 0 else current * 10 + val
        else:
            if current > 0:
                total += current
                current = 0
    total += current
    return total


# 正则：匹配"第X条"、"第X条之一"等格式
_ARTICLE_PATTERN = re.compile(r'第([一二三四五六七八九十百千0-9]+)条(?:之一|之二|之三|之四)?')


def normalize_article_ref(query: str) -> str:
    """
    将口语化的法条引用归一化。
    
    例如：
    "第1077条" → "民法典第一千零七十七条"
    "第1079条" → "民法典第一千零七十九条"
    "第366条"  → "民法典第三百六十六条"
    "第25条"   → "消费者权益保护法第二十五条"
    
    Args:
        query: 用户查询文本
    
    Returns:
        归一化后的文本（未匹配则返回原文）
    """
    def _replace(match):
        num_str = match.group(1)
        if num_str.isdigit():
            num = int(num_str)
        else:
            num = _chinese_to_arabic(num_str)
        
        if num <= 0:
            return match.group(0)
        
        # 判断原文是否已包含"民法典"前缀
        start = match.start()
        has_prefix = start >= 3 and query[start-3:start] == "民法典"
        has_law_prefix = start >= 2 and query[start-2:start] == "民法"
        
        # 民法典法条范围（1-1260条）
        if 1 <= num <= 1260:
            cn_num = _arabic_to_chinese(num)
            result = f"第{cn_num}条"
            if not has_prefix and not has_law_prefix:
                result = "民法典" + result
            return result
        # 消费者权益保护法（1-63条）
        elif num <= 63:
            return f"消费者权益保护法第{num}条"
        else:
            return match.group(0)
    
    return _ARTICLE_PATTERN.sub(_replace, query)


def get_article_number(query: str) -> int | None:
    """从查询中提取法条编号"""
    match = _ARTICLE_PATTERN.search(query)
    if not match:
        return None
    num_str = match.group(1)
    if num_str.isdigit():
        return int(num_str)
    return _chinese_to_arabic(num_str)
