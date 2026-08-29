"""查清输入桌面为什么不可用：是锁屏、还是权限、还是会话断开。

三种原因的处置完全不同，不能混为一谈：

- **锁屏**：输入桌面切到 ``Winlogon``，``Default`` 桌面上的进程无法操作光标。
  解法是解锁会话。
- **权限**：``SetCursorPos`` 失败且 ``GetLastError=5``（拒绝访问），
  多为运行身份与交互会话的所有者不一致。
- **会话断开**：``WTSConnectState`` 不是 Active。

已观察到的现象：最初失败时 ``GetLastError=5``，后来失败时 ``GetLastError=0``
——错误码变了，说明前后不是同一个原因，必须分别判定。

用法：``python tools/probe_input_desktop.py``
"""

from __future__ import annotations

import ctypes
import io
import sys
import time
from ctypes import wintypes

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001
WTS_CONNECTSTATE = 8
WTS_SESSION_INFO_NAMES = {
    0: "Active（已连接并可交互）",
    1: "Connected",
    2: "ConnectQuery",
    3: "Shadow",
    4: "Disconnected（已断开）",
    5: "Idle",
    6: "Listen",
    7: "Reset",
    8: "Down",
    9: "Init",
}


def object_name(handle) -> str:
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    if user32.GetUserObjectInformationW(
        handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
    ):
        return buf.value
    return f"<读取失败 err={kernel32.GetLastError()}>"


def main() -> int:
    print("=" * 72)
    print("输入桌面诊断")
    print("=" * 72)

    # 1. 会话状态
    sid = ctypes.c_ulong()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    wtsapi = ctypes.windll.wtsapi32
    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    state = None
    if wtsapi.WTSQuerySessionInformationW(
        None, sid.value, WTS_CONNECTSTATE, ctypes.byref(buf), ctypes.byref(size)
    ):
        state = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
    print(f"会话 id={sid.value}")
    print(f"  ConnectState = {state} {WTS_SESSION_INFO_NAMES.get(state, '?')}")
    print(f"  远程会话 = {bool(user32.GetSystemMetrics(0x1000))}")

    # 2. 当前线程所在桌面 vs 输入桌面
    thread_desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    print(f"本线程桌面 = {object_name(thread_desktop)!r}")

    input_desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if input_desktop:
        name = object_name(input_desktop)
        print(f"输入桌面   = {name!r}")
        locked = name.lower() == "winlogon"
        print(f"  锁屏/安全桌面 = {locked}")
        user32.CloseDesktop(input_desktop)
    else:
        name = None
        locked = None
        print(
            f"输入桌面   = 打不开（err={kernel32.GetLastError()}），"
            "通常也意味着当前进程不在输入桌面上"
        )

    # 3. 实际能否操作光标
    kernel32.SetLastError(0)
    ok = bool(user32.SetCursorPos(700, 400))
    err = kernel32.GetLastError()
    time.sleep(0.2)
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    print(f"SetCursorPos = {ok} GetLastError = {err}")
    print(f"光标 = ({point.x}, {point.y})")
    print(f"前台窗口 = {user32.GetForegroundWindow()}")

    # 4. 结论
    print("-" * 72)
    if ok and (point.x, point.y) != (0, 0):
        print("结论：输入桌面可用，Seize 点击可以送达")
        return 0

    if locked:
        print("结论：**会话被锁**（输入桌面是 Winlogon）")
        print("      解法：解锁该会话（输入密码），然后保持不锁屏")
    elif err == 5:
        print("结论：**权限不足**（ERROR_ACCESS_DENIED）")
        print("      当前进程的运行身份与交互会话所有者不一致")
    elif state not in (0,):
        print(f"结论：**会话未处于 Active**（ConnectState={state}）")
        print("      解法：重新连接远程桌面")
    else:
        print("结论：会话 Active 且非锁屏，但仍无法操作光标")
        print(f"      本线程桌面={object_name(thread_desktop)!r} 输入桌面={name!r}")
        print("      若两者不同，说明当前进程不在输入桌面上")
    return 1


if __name__ == "__main__":
    sys.exit(main())
