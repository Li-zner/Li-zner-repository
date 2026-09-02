import asyncio, sys
sys.path.insert(0, "/app")
from app.core.db import init_pool, get_pool, close_pool

async def t():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE knowledge_chunks SET content = $1 WHERE source = 'project' AND heading = '个人基本信息'",
            "姓名：黎忠南\n年龄：23岁（2003年生）\n教育背景：广西科技大学 本科，土木工程（土木工程建造与管理），2021-2025\n在校职务：校组织运营部副部长、班级心理委员\n求职意向：AI应用开发\n期望城市：广州、深圳\n联系方式：电话 13357308241 | 微信 CXKSBLi | QQ 1270345165@qq.com\n期望薪资：10-12K\n公网项目：这个网站就是我做的公网项目\n百度网盘：https://pan.baidu.com/s/1ProMzaj1ANz_NdQloEKGTw?pwd=sj4b"
        )
        print("KB updated")
    await close_pool()

asyncio.run(t())
