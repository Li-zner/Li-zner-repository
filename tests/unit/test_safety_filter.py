"""DFA 敏感词过滤器单元测试（离线，无外部依赖）"""
import pytest

from app.core.safety_filter import SafetyFilter


@pytest.fixture
def sf():
    return SafetyFilter()


def test_detect_sensitive_word(sf):
    """明确敏感词必须被命中，且能指出触发词"""
    assert sf.contains_sensitive("我要学做炸弹") is True
    assert sf.triggered_word == "炸弹"


def test_safe_context_whitelist(sf):
    """法律/旅游安全上下文应跳过检查（避免误伤）"""
    assert sf.contains_sensitive("民法典关于离婚冷静期的规定") is False


def test_normal_travel_text_not_blocked(sf):
    """正常旅游内容不得误伤"""
    assert sf.contains_sensitive("我想去北京故宫和长城玩") is False


def test_normal_numbers_not_blocked(sf):
    """数字/价格/日期不得误伤"""
    assert sf.contains_sensitive("门票价格64元，行程8天") is False


def test_partial_word_not_matched(sf):
    """非敏感词的相似片段不得误伤（需完整词匹配）"""
    # "色彩情感" 不含连续子串 "色情"
    assert sf.contains_sensitive("色彩情感咨询") is False


def test_extra_words_supported(sf):
    """支持额外注入自定义敏感词"""
    custom = SafetyFilter(extra_words=["自定义违禁词"])
    assert custom.contains_sensitive("包含自定义违禁词") is True
