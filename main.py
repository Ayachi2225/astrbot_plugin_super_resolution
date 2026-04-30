import asyncio
import time
import os
import tempfile
from io import BytesIO
from pathlib import Path
from httpx import AsyncClient
from typing import List, Dict, Tuple

import numpy as np
from PIL import Image as IMG

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    import ujson as json
except ModuleNotFoundError:
    import json

try:
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer
    enable = True
except ImportError:
    enable = False
    logger.warning("[超分插件] basicsr/realesrgan 未安装，超分功能不可用。")

upsampler = (
    RealESRGANer( # type: ignore
        scale=4,
        model_path=str(
            Path(__file__).parent / "RealESRGAN_x4plus_anime_6B.pth"
        ),
        model=RRDBNet( # type: ignore
            num_in_ch=3,
            num_out_ch=3,
            num_feat=64,
            num_block=6,
            num_grow_ch=32,
            scale=4,
        ),
        tile=100,
        tile_pad=10,
        pre_pad=0,
        half=False,
    )
    if enable
    else None
)
plugin_dir = Path(__file__).parent
config_path = plugin_dir / "_conf_schema.json"
with open(config_path, 'r', encoding='utf-8') as f:
    config_schema = json.load(f)

MAX_SIZE    = config_schema["max size"]["default"]
CD_TIME     = config_schema["cd"]["default"]
WAIT_TIMEOUT = config_schema["waitting"]["default"]
outscale     = config_schema["outscale"]["default"]

# ── 工具函数 ────────────────────────────────────────────────────
def get_image_urls(message_chain) -> List[str]:
    """从消息链中提取所有图片 URL。"""
    urls = []
    for seg in message_chain:
        if isinstance(seg, Comp.Image):
            url = getattr(seg, "url", None) or getattr(seg, "file", None)
            if url and url.startswith("http"):
                urls.append(url)
    return urls


def make_session_key(event: AstrMessageEvent) -> str:
    """用 group_id + sender_id 唯一标识一个用户会话。"""
    group_id  = event.message_obj.group_id or "private"
    sender_id = event.get_sender_id()
    return f"{group_id}:{sender_id}"


# ── 插件主体 ────────────────────────────────────────────────────
@register(
    "astrbot_plugin_super_resolution",
    "Ayachi2225",
    "本地超分插件，让你的 bot 能将提升图片分辨率",
    "1.0.0",
)
class SuperResolutionPlugin(Star):

    def __init__(self, context: Context):
        super().__init__(context)
        # { session_key: timestamp }  冷却记录
        self._cd_map: Dict[str, float] = {}
        # { session_key: asyncio.Future }  正在等待图片的会话
        self._waiting: Dict[str, asyncio.Future] = {}

    async def initialize(self):
        if not enable:
            logger.warning("[超分插件] 依赖缺失，插件已加载但超分功能不可用。")
        else:
            logger.info("[超分插件] 初始化完成，模型就绪。")

    # ── 指令入口：/超分 ────────────────────────────────────────
    @filter.command("超分")
    async def super_resolution(self, event: AstrMessageEvent):
        """收到指令后询问用户发图，等待图片后执行超分。"""

        if not enable:
            yield event.plain_result("超分功能不可用：服务器缺少 basicsr / realesrgan 依赖。")
            return

        key = make_session_key(event)

        now = time.time()
        last = self._cd_map.get(key, 0)
        remaining = CD_TIME - (now - last)
        if remaining > 0:
            yield event.plain_result(f"超分 CD 剩余时间：{remaining:.1f}s，请稍后再试。")
            return

        if key in self._waiting:
            yield event.plain_result("已有一个超分任务正在等待您发图，请直接发送图片。")
            return

        yield event.plain_result(f"请发送需要超分的图片（{WAIT_TIMEOUT}s 内有效）……")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiting[key] = fut

        try:
            img_url: str = await asyncio.wait_for(fut, timeout=WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            self._waiting.pop(key, None)
            yield event.plain_result("等待超时，超分已取消。")
            return
        finally:
            self._waiting.pop(key, None)

        yield event.plain_result("开始处理图片，请稍候……")

        try:
            async with AsyncClient(timeout=30) as client:
                resp = await client.get(img_url)
                if resp.status_code != 200:
                    yield event.plain_result(f"图片下载失败（HTTP {resp.status_code}），请稍后重试。")
                    return
                raw_bytes = resp.content
        except Exception as e:
            logger.error(f"[超分插件] 下载图片时出错：{e}")
            yield event.plain_result("图片下载失败，请检查网络后重试。")
            return

        image = IMG.open(BytesIO(raw_bytes))

        if getattr(image, "is_animated", False):
            yield event.plain_result("暂不支持对 GIF 动图进行超分，请发送静态图片。")
            return

        w, h = image.size
        pixel_count = w * h
        if pixel_count > MAX_SIZE:
            yield event.plain_result(
                f"图片尺寸过大（{w}×{h}={pixel_count} 像素），"
                f"请发送像素总数不超过 {MAX_SIZE:,} 的图片。"
            )
            return

        self._cd_map[key] = now  

        image_array: np.ndarray = np.array(image.convert("RGB"))
        start = time.time()
        try:
            output, _ = await loop.run_in_executor(
                None, lambda: upsampler.enhance(image_array, outscale=outscale) # type: ignore
            )
        except Exception as e:
            logger.error(f"[超分插件] 超分失败：{e}")
            yield event.plain_result("超分处理失败，可能是算力不足，请稍后重试。")
            return

        elapsed = round(time.time() - start, 2)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
                IMG.fromarray(output).save(tmp_path, format="PNG")

            yield event.chain_result([
                Comp.Plain(f"超分完成！处理用时：{elapsed}s"),
                Comp.Image.fromFileSystem(tmp_path),
            ])
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _image_listener(self, event: AstrMessageEvent):
        key = make_session_key(event)

        fut = self._waiting.get(key)
        if fut is None or fut.done():
            return

        urls = get_image_urls(event.get_messages())
        if not urls:
            yield event.plain_result("请发送图片，非图片消息已忽略。")
            return

        fut.set_result(urls[0])

    async def terminate(self):
        for fut in self._waiting.values():
            if not fut.done():
                fut.cancel()
        logger.info("[超分插件] 插件已卸载。")