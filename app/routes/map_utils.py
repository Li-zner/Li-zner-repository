"""地图路由的纯函数工具：IP 判定与城市名候选生成。"""
import ipaddress
import re


def is_reserved_ip(ip: str) -> bool:
    """判断 IP 是否为私有、回环或保留地址；非法 IP 也按无定位处理。"""
    if not ip:
        return True
    try:
        address = ipaddress.ip_address(ip)
        return address.is_private or address.is_loopback or address.is_reserved
    except ValueError:
        return True


def weather_candidates(name: str) -> list[str]:
    """生成高德天气可用的候选名称：原样、自治州核心名、去行政后缀。"""
    if not name:
        return []
    candidates = [name]
    match = re.search(
        r'^(.*?)(?:(?:维吾尔|壮|回|蒙古|藏|苗|彝|土家|侗|布依|瑶|白|哈尼|哈萨克|傣|'
        r'黎|傈僳|佤|畲|高山|拉祜|水|东乡|纳西|景颇|柯尔克孜|土|达斡尔|'
        r'仫佬|羌|布朗|撒拉|保安|仡佬|锡伯|阿昌|普米|朝鲜|满|鄂温克|鄂伦春|'
        r'赫哲|门巴|珞巴|基诺)族?)+自治州$',
        name,
    )
    if match and match.group(1):
        core = match.group(1)
        candidates.extend((core, core + '市'))
    if not match:
        cleaned = re.sub(r'(市|州|地区|特别行政区|省)$', '', name)
        if cleaned != name:
            candidates.append(cleaned)
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))
