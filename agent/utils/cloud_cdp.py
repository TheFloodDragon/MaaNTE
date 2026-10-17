"""云异环网页端 CDP 会话：白名单页面状态、1280x720 游戏帧、CSS 坐标点击/按键。

截图与输入都走 CDP（Page.captureScreenshot / Input.dispatch*），不依赖可交互桌面，
与三月七小助手云星铁一致。页面脚本只返回固定字段：不返回 UID、账号或 cookie，
弹窗文本先去掉 6 位以上数字再截断。
"""

import base64
import json
import math
import re
import time

from cloud_web_start import WebClientError, discover, page_targets, validate_profile
from utils.logger import logger

BASE_W, BASE_H = 1280, 720
# 云端按 screen.width*devicePixelRatio 决定 -ResX/-ResY：强制 1x 与 1280x720，
# 让游戏以 MaaNTE 基准分辨率渲染，模板/ROI 与 PC 端一致，同时降低码率与解码压力。
STREAM_OVERRIDE = {"width": 0, "height": 0, "deviceScaleFactor": 1, "mobile": False,
                   "screenWidth": BASE_W, "screenHeight": BASE_H}
_KEYS = {"Escape": ("Escape", 27), "Enter": ("Enter", 13), "Space": ("Space", 32)}

STATE_SCRIPT = r"""(() => {
 const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); if (r.width <= 1 || r.height <= 1) return false;
   const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0.05; };
 const rect = e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
 const clean = t => String(t || '').replace(/\d{6,}/g, '#').replace(/\s+/g, ' ').trim().slice(0, 120);
 const num = (root, sel) => { const e = root && root.querySelector(sel); const n = e ? parseInt(e.textContent, 10) : NaN; return Number.isFinite(n) ? n : null; };
 const minutes = sel => { const p = document.querySelector(sel); if (!vis(p)) return null;
   const h = num(p, '[data-name="hour"]'), m = num(p, '[data-name="minute"]'); return h === null && m === null ? null : (h || 0) * 60 + (m || 0); };
 const pops = Array.from(document.querySelectorAll('.cloud-popup-normal')).filter(vis).map(e => {
   const kind = (String(e.className).split(/\s+/).filter(c => c.endsWith('-popup') && c !== 'cloud-popup-normal').pop() || 'unknown-popup').slice(0, -6);
   const btns = Array.from(e.querySelectorAll('a,button')).filter(vis).map(b => ({x: clean(b.innerText).replace(/\s/g, '').slice(0, 12), c: String(b.className).slice(0, 30), r: rect(b)}));
   const rows = Array.from(e.querySelectorAll('.item-panel')).filter(vis).map(r => ({t: clean(r.innerText), on: r.classList.contains('on'), r: rect(r)}));
   return {kind, text: clean(e.innerText), btns, rows};
 });
 const v = document.querySelector('#gameVideo video, video#gameVideo');
 let content = null;
 if (v && vis(v) && v.videoWidth > 16) {
   const r = rect(v), fit = getComputedStyle(v).objectFit; let cw = r[2], ch = r[3];
   if (fit === 'contain' || fit === 'scale-down') { const s = Math.min(r[2] / v.videoWidth, r[3] / v.videoHeight); cw = v.videoWidth * s; ch = v.videoHeight * s; }
   content = [r[0] + (r[2] - cw) / 2, r[1] + (r[3] - ch) / 2, cw, ch];
 }
 const start = document.querySelector('.login-after-panel .start-game-btn'), login = document.querySelector('.login-before-panel .start-btn');
 const gift = document.querySelector('.first-login-panel');
 return {href_ok: location.href.startsWith('https://yh.wanmei.com/cloud'), ready: document.readyState,
   logged_in: vis(document.querySelector('.login-after-panel')), logged_out: vis(document.querySelector('.login-before-panel')),
   free_min: minutes('.duration-panel.free'), paid_min: minutes('.duration-panel.purchase'),
   start: vis(start) ? rect(start) : null, login_btn: vis(login) ? rect(login) : null,
   gift: vis(gift) ? {r: rect(gift), text: clean(gift.innerText)} : null, pops,
   home: vis(document.querySelector('.home-layer')), game: vis(document.querySelector('.game-container')),
   loading: vis(document.querySelector('.game-loading-layer')),
   video: v ? {w: v.videoWidth, h: v.videoHeight, rs: v.readyState, paused: v.paused, content} : null,
   inner: [innerWidth, innerHeight], dpr: devicePixelRatio, fs: !!document.fullscreenElement,
   events: (window.__maanteCloud && window.__maanteCloud.splice(0)) || []};
})()"""

# 只记录 SDK 回调名、连接状态码和固定提示，不记录 accountId 等附带数据。
HOOK_SCRIPT = r"""(() => {
 if (window.__maanteCloudHook) return true; window.__maanteCloudHook = true; window.__maanteCloud = window.__maanteCloud || [];
 const push = e => { if (window.__maanteCloud.length < 200) window.__maanteCloud.push(e); };
 const patch = () => { const G = window.WelinkCloudGame; if (!G || !G.prototype || G.prototype.__maante) return !!(G && G.prototype && G.prototype.__maante);
   const orig = G.prototype.setStreamListener; G.prototype.__maante = true;
   G.prototype.setStreamListener = function (l) { const wrapped = Object.assign({}, l);
     const tap = (name, fn) => { if (typeof l[name] === 'function') wrapped[name] = function (...a) { try { fn(a); } catch (e) {} return l[name].apply(this, a); }; };
     tap('onConnectState', a => push({k: 'connect', s: a[0], code: a[1] && a[1].code, re: !!(a[1] && a[1].isReconnect)}));
     tap('onFirstFrame', a => push({k: 'first_frame', s: a[0]}));
     tap('onStartGameFail', a => push({k: 'start_fail', code: a[0]}));
     tap('onRemoteExit', a => push({k: 'remote_exit', code: a[0]}));
     tap('onDecodeFail', a => push({k: 'decode_fail', s: a[0]}));
     tap('onStreamExtraData', a => { const d = a[1]; if (a[0] === 6 && d && typeof d.code === 'number') push({k: 'extra', code: d.code}); });
     return orig.call(this, wrapped); };
   return true; };
 if (!patch()) { const iv = setInterval(() => { if (patch()) clearInterval(iv); }, 50); setTimeout(() => clearInterval(iv), 30000); }
 return true;
})()"""

POINTER_LOCK_SCRIPT = r"""((block) => {
 const proto = Element.prototype;
 if (block) { if (!proto.__maanteLock) { proto.__maanteLock = proto.requestPointerLock;
     proto.requestPointerLock = function () { return Promise.reject(new DOMException('blocked during MaaNTE startup', 'NotAllowedError')); }; }
   if (document.pointerLockElement && document.exitPointerLock) document.exitPointerLock(); }
 else if (proto.__maanteLock) { proto.requestPointerLock = proto.__maanteLock; delete proto.__maanteLock; }
 return true;
})"""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class CloudSession:
    """单页面 CDP 会话；所有调用同步且带超时，便于调用方在间隙检查停止信号。"""

    def __init__(self, ws, endpoint, target_id):
        self._ws, self.endpoint, self.target_id, self._next = ws, endpoint, target_id, 0

    @classmethod
    def connect(cls, profile=None, timeout=3.0):
        profile = validate_profile(profile)
        endpoint, reason = discover(profile)
        if endpoint is None:
            logger.debug("CDP 端点不可用 | reason=%s", reason)
            raise WebClientError("cdp_unverified")
        targets = page_targets(endpoint)
        if len(targets) != 1:
            raise WebClientError("page_missing" if not targets else "ambiguous_window")
        from websockets.sync.client import connect

        options = dict(open_timeout=timeout, close_timeout=1, max_size=32 * 1024 * 1024, compression=None)
        try:
            try:
                ws = connect(targets[0]["ws"], proxy=None, **options)  # websockets>=15 会读取系统代理
            except TypeError:
                ws = connect(targets[0]["ws"], **options)
        except Exception as exc:
            logger.debug("CDP 连接失败 | error=%s", type(exc).__name__)
            raise WebClientError("cdp_failed") from None
        return cls(ws, endpoint, targets[0]["id"])

    def call(self, method, params=None, timeout=10.0):
        if self._ws is None:
            raise WebClientError("cdp_failed")
        self._next += 1
        ident = self._next
        try:
            self._ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebClientError("cdp_timeout")
                message = json.loads(self._ws.recv(timeout=remaining))
                if message.get("id") == ident:
                    break
        except WebClientError:
            raise
        except TimeoutError:
            raise WebClientError("cdp_timeout") from None
        except Exception as exc:
            logger.debug("CDP 调用失败 | method=%s | error=%s", method, type(exc).__name__)
            self.close()
            raise WebClientError("cdp_failed") from None
        if "error" in message:
            logger.debug("CDP 返回错误 | method=%s | code=%s", method, message["error"].get("code"))
            raise WebClientError("cdp_failed")
        return message.get("result", {})

    def evaluate(self, expression, timeout=8.0):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True}, timeout)
        if "exceptionDetails" in result:
            raise WebClientError("script_failed")
        return result.get("result", {}).get("value")

    def state(self):
        value = self.evaluate(STATE_SCRIPT)
        if not isinstance(value, dict) or "pops" not in value:
            raise WebClientError("script_failed")
        return value

    def install_hooks(self):
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_SCRIPT})
        return bool(self.evaluate(HOOK_SCRIPT))

    def set_stream_override(self):
        self.call("Emulation.setDeviceMetricsOverride", STREAM_OVERRIDE)

    def pointer_lock(self, block):
        self.evaluate("%s(%s)" % (POINTER_LOCK_SCRIPT, "true" if block else "false"))

    def click(self, x, y):
        if not (_finite(x) and _finite(y)) or x < 0 or y < 0:
            raise WebClientError("script_failed")
        for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
            self.call("Input.dispatchMouseEvent", {
                "type": kind, "x": float(x), "y": float(y), "buttons": buttons, "pointerType": "mouse",
                "button": "none" if kind == "mouseMoved" else "left", "clickCount": 0 if kind == "mouseMoved" else 1,
            })

    def click_rect(self, rect):
        self.click(rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)

    def key(self, name="Escape"):
        code, vk = _KEYS[name]
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {"type": kind, "key": name, "code": code,
                                                 "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})

    def capture(self, content, quality=90):
        """按视频内容区域截取并缩放为 1280x720 BGR 帧（PC 端识别资源的基准尺寸）。"""
        import cv2
        import numpy as np

        if not content or len(content) != 4 or not all(map(_finite, content)) or min(content[2:]) <= 16:
            raise WebClientError("frame_unavailable")
        x, y, width, height = content
        data = self.call("Page.captureScreenshot", {
            "format": "jpeg", "quality": quality, "fromSurface": True,
            "clip": {"x": x, "y": y, "width": width, "height": height, "scale": BASE_W / width},
        }, 15).get("data")
        frame = cv2.imdecode(np.frombuffer(base64.b64decode(data or ""), np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.ndim != 3:
            raise WebClientError("frame_unavailable")
        if frame.shape[:2] != (BASE_H, BASE_W):
            frame = cv2.resize(frame, (BASE_W, BASE_H), interpolation=cv2.INTER_AREA)
        return frame

    def frame_to_css(self, content, point):
        x, y, width, height = content
        return x + point[0] * width / BASE_W, y + point[1] * height / BASE_H

    def window_state(self, state):
        window = self.call("Browser.getWindowForTarget", {"targetId": self.target_id})
        self.call("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": state}})

    def bring_to_front(self):
        self.call("Page.bringToFront")

    def reload(self):
        self.call("Page.reload", {"ignoreCache": False})

    def close(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


QUEUE_PATTERN = re.compile(r"第\s*(\d+)\s*/\s*(\d+)\s*名")
ETA_PATTERN = re.compile(r"预计等待\s*([<>≤≥]?\s*\d+\s*(?:分钟|小时|秒)(?:以上|以内)?)")


def parse_queue(text):
    """从排队弹窗文本中只提取名次、总数与预计等待，其他文字一律丢弃。"""
    text = str(text or "")
    position = QUEUE_PATTERN.search(text)
    eta = ETA_PATTERN.search(text)
    return {
        "position": int(position.group(1)) if position else None,
        "total": int(position.group(2)) if position else None,
        "eta": re.sub(r"\s+", "", eta.group(1)) if eta else None,
        "vip": "VIP" in text,
    }


def format_minutes(value):
    if value is None:
        return "?"
    hours, minutes = divmod(int(value), 60)
    return "%d 小时 %d 分钟" % (hours, minutes) if hours else "%d 分钟" % minutes
