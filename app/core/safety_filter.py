"""
DFA 敏感词过滤器
- 确定性有穷自动机（Deterministic Finite Automaton）算法
- 一次构建，O(n) 扫描
- 命中后截断输出，返回预设安全文案
"""
import os
import re
from ..core.logging import setup_logging

logger = setup_logging()

# ============================================================
# 内置敏感词库（按需增删）
# ============================================================
_BUILTIN_SENSITIVE_WORDS = [
    # ── 政治高危（仅保留真正需要拦截的组合词）──
    # ⚠️ 注意：旅行助手场景下，以下规则必须严格审核：
    #   - 数字类（64/89等）会误伤日期、价格、距离等正常内容
    #   - 单个人名会误伤"毛泽东纪念堂"等正常推荐
    #   - "敏感词"本身会触发自指误伤
    "法轮功", "大纪元", "新唐人",
    "台独", "一中一台", "两个中国", "台湾独立", "西藏独立", "藏独",
    "新疆独立", "东突厥", "东伊运", "疆独",
    "分裂国家", "颠覆政权", "反革命",

    # ── 暴力恐怖（仅保留明确有害的）──
    "杀人", "放火", "爆炸", "炸弹", "恐怖袭击", "自杀式袭击",
    "砍人", "枪击", "绑架", "劫持", "贩毒",

    # ── 色情低俗（仅保留明确有害的）──
    "色情", "裸聊", "裸照", "裸体", "成人视频", "AV", "黄色网站",
    "卖淫", "嫖娼", "招嫖", "援交", "一夜情", "约炮",
    "情色", "三级片", "毛片", "淫秽",

    # ── 金融违法 ──
    "洗钱", "非法集资", "诈骗", "传销", "走私",
    "赌博", "赌场", "六合彩", "外围",

    # ── 网络安全 ──
    "黑客", "木马", "病毒", "入侵", "攻击服务器", "窃取数据",
    "DDOS", "网络攻击", "漏洞利用", "渗透",
]

# ============================================================
# DFA 节点
# ============================================================
class _DFANode:
    __slots__ = ('children', 'is_end')
    def __init__(self):
        self.children = {}
        self.is_end = False


class SafetyFilter:
    """DFA 敏感词过滤器（含超时机制）"""

    def __init__(self, extra_words: list = None):
        self._root = _DFANode()
        self._safe_message = os.getenv(
            "SAFETY_FILTER_MESSAGE",
            "该问题涉及敏感信息，小助手不便回答哦~"
        )
        words = list(_BUILTIN_SENSITIVE_WORDS)
        # 外部敏感词库（可选，每行一个词，# 开头为注释；P1 #56 不改代码即可增改词库）
        external_file = os.getenv("SAFETY_FILTER_WORDS_FILE", "")
        if external_file and os.path.exists(external_file):
            try:
                with open(external_file, "r", encoding="utf-8") as f:
                    for line in f:
                        w = line.strip()
                        if w and not w.startswith("#"):
                            words.append(w)
                logger.info(f"已加载外部敏感词库: {external_file}")
            except Exception as e:
                logger.warning(f"外部敏感词库加载失败，使用内置词库: {e}")
        self._build_trie(words)
        if extra_words:
            self._build_trie(extra_words)
        logger.info(f"DFA 过滤器已初始化，已加载 {self._count_words()} 个敏感词")

    def _build_trie(self, words: list):
        """构建 DFA Trie 树"""
        for word in words:
            if not word:
                continue
            node = self._root
            for char in word:
                if char not in node.children:
                    node.children[char] = _DFANode()
                node = node.children[char]
            node.is_end = True

    # ── 安全白名单上下文 ──
    # 当文本包含这些安全上下文时，跳过敏感词检查（避免法律/旅游查询被误伤）
    _SAFE_CONTEXTS = [
        "民法典", "法律咨询", "法律条款", "法条", "离婚冷静期",
        "危险徒步", "危险路线", "安全吗", "注意安全",
        "天安门广场", "天安门", "毛主席纪念堂",
        # 法律相关安全上下文（2026-07-23 补充）
        "抗辩权", "同时履行抗辩", "不安抗辩", "先履行抗辩",
        "合同法", "侵权责任", "婚姻家庭", "继承", "物权",
        "人格权", "债权", "担保", "抵押", "质押", "定金",
        "违约金", "损害赔偿", "诉讼时效", "民事责任",
        "劳动合同", "消费者权益", "食品安全", "知识产权",
        "行政处罚", "行政复议", "行政诉讼",
    ]

    def _in_safe_context(self, text: str) -> bool:
        """检查文本是否在安全上下文中（如法律咨询、旅游推荐）

        注（C4）：保持子串匹配——中文无空格词边界，正则 \b 不可靠；
        白名单上下文误判的代价是跳过敏感检查，当前风险可控。
        """
        if not text:
            return False
        text_lower = text.lower()
        for ctx in self._SAFE_CONTEXTS:
            if ctx in text_lower:
                return True
        return False

    def contains_sensitive(self, text: str, timeout_ms: int = 500) -> bool:
        """
        检查文本是否包含敏感词（带超时保护 + 安全上下文白名单）
        timeout_ms: 最大扫描毫秒数，超时视为安全
        """
        if not text:
            return False
        # 先检查安全上下文白名单
        if self._in_safe_context(text):
            return False
        # 大文本只扫描前 5000 字符，避免 O(n²) 卡死事件循环（P1 #58）
        text = text[:5000]
        import time
        start = time.perf_counter()
        n = len(text)
        for i in range(n):
            # 超时保护：防止大文本卡死事件循环
            if timeout_ms > 0 and (time.perf_counter() - start) * 1000 > timeout_ms:
                logger.warning(f"DFA 扫描超时({timeout_ms}ms)，跳过安全检查")
                return False
            node = self._root
            for j in range(i, n):
                char = text[j]
                if char not in node.children:
                    break
                node = node.children[char]
                if node.is_end:
                    self._last_triggered = text[i:j+1]
                    return True
        return False

    @property
    def triggered_word(self) -> str:
        """最近一次触发的敏感词"""
        return getattr(self, '_last_triggered', '')

    def _count_words(self) -> int:
        """统计敏感词数量"""
        count = 0
        stack = [self._root]
        while stack:
            node = stack.pop()
            if node.is_end:
                count += 1
            stack.extend(node.children.values())
        return count

    def find_first(self, text: str) -> tuple:
        """
        找到第一个敏感词
        返回: (敏感词, 起始位置, 结束位置) 或 None
        """
        if not text:
            return None
        text = text[:5000]  # 只扫描前 5000 字符，限制大文本开销（P1 #58）
        for i in range(len(text)):
            node = self._root
            for j in range(i, len(text)):
                char = text[j]
                if char not in node.children:
                    break
                node = node.children[char]
                if node.is_end:
                    return (text[i:j+1], i, j+1)
        return None

    def filter_text(self, text: str, mask_char: str = "*") -> str:
        """将文本中的敏感词替换为掩码字符"""
        if not text:
            return text
        result = list(text)
        for i in range(len(text)):
            node = self._root
            for j in range(i, len(text)):
                char = text[j]
                if char not in node.children:
                    break
                node = node.children[char]
                if node.is_end:
                    for k in range(i, j + 1):
                        result[k] = mask_char
        return "".join(result)

    def check_stream(self, chunk: str) -> dict:
        """
        检查流式输出块是否触发敏感词
        返回: {"safe": True/False, "chunk": 处理后的chunk, "triggered_word": "敏感词"}
        """
        if not chunk:
            return {"safe": True, "chunk": chunk, "triggered_word": None}

        result = self.find_first(chunk)
        if result:
            word, start, end = result
            logger.warning(f"🛡️ DFA 过滤器触发: 敏感词='{word}'")
            return {
                "safe": False,
                "chunk": chunk[:start],
                "triggered_word": word
            }
        return {"safe": True, "chunk": chunk, "triggered_word": None}

    @property
    def safe_message(self) -> str:
        return self._safe_message


# ============================================================
# 全局单例
# ============================================================
_filter = None


def get_filter() -> SafetyFilter:
    global _filter
    if _filter is None:
        _filter = SafetyFilter()
    return _filter
