# -*- coding: utf-8 -*-
"""
打印后端(代理侧): 提交PDF / 查询状态 / 取消 的统一抽象。

- SimBackend      模拟器: 虚拟打印机群,速度可调,可注入卡纸/断网/缺纸
- CupsBackend     Linux 真机: pycups 提交 + SNMP 状态(需真机验证)
- WindowsBackend  Windows 真机: SumatraPDF 命令行打印 + win32print/SNMP 状态

poll() 统一返回: {"status": idle|printing|paper_out|jam|offline,
                 "page_counter": int(-1表示不可用), "detail": str}
"""
import time

from . import config


class BackendBase:
    name = "base"

    def submit(self, printer: dict, pdf_path: str) -> str:
        """下发合成PDF(永远只打1份,份数已拼进PDF)。返回本地作业号。"""
        raise NotImplementedError

    def poll(self, printer: dict) -> dict:
        raise NotImplementedError

    def cancel(self, printer: dict):
        pass


# ============================ 模拟器 ============================

class _SimPrinter:
    def __init__(self):
        self.remaining = 0          # 当前作业剩余张数
        self.page_counter = 0       # 累计已出纸张数(相当于真机页计数器)
        self.fault = None           # None | jam | offline | paper_out
        self.tray = config.SIM_TRAY_CAPACITY
        self.printing = False


class SimBackend(BackendBase):
    name = "sim"

    def __init__(self):
        self._vp: dict[int, _SimPrinter] = {}
        self._last_tick = time.time()

    def _get(self, pid: int) -> _SimPrinter:
        return self._vp.setdefault(pid, _SimPrinter())

    def _tick(self):
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now
        for vp in self._vp.values():
            if not (vp.printing and vp.fault is None and vp.remaining > 0):
                continue
            if vp.tray <= 0:
                vp.fault = "paper_out"
                continue
            done = min(vp.remaining, vp.tray,
                       max(1, int(dt * config.SIM_SHEETS_PER_SEC)))
            vp.remaining -= done
            vp.tray -= done
            vp.page_counter += done
            if vp.remaining == 0:
                vp.printing = False

    def submit(self, printer, pdf_path):
        vp = self._get(printer["id"])
        if vp.fault:
            raise RuntimeError(f"模拟器故障中: {vp.fault}")
        vp.remaining = int(printer.get("_sheets", 0))
        vp.printing = True
        return f"sim-{printer['id']}-{int(time.time())}"

    def poll(self, printer):
        self._tick()
        vp = self._get(printer["id"])
        if vp.fault == "offline":
            return {"status": "offline", "page_counter": vp.page_counter, "detail": "断网(注入)"}
        if vp.fault == "jam":
            return {"status": "jam", "page_counter": vp.page_counter, "detail": "卡纸(注入)"}
        if vp.fault == "paper_out" or (vp.printing and vp.tray <= 0):
            vp.fault = "paper_out"
            return {"status": "paper_out", "page_counter": vp.page_counter,
                    "detail": f"缺纸,纸盒余 {vp.tray}"}
        return {"status": "printing" if vp.printing else "idle",
                "page_counter": vp.page_counter, "detail": ""}

    def cancel(self, printer):
        vp = self._get(printer["id"])
        vp.printing = False
        vp.remaining = 0

    # ---- 模拟器专用(机队页调用) ----
    def inject(self, pid, fault):
        self._get(pid).fault = fault

    def refill(self, pid):
        vp = self._get(pid)
        vp.tray = config.SIM_TRAY_CAPACITY
        if vp.fault == "paper_out":
            vp.fault = None

    def tray_of(self, pid):
        return self._get(pid).tray


# ============================ SNMP 公共助手 ============================

def _snmp_status(ip: str):
    """
    读取打印机 SNMP 状态(标准 Printer-MIB + host-resources MIB)。
    返回统一状态字典; 取不到返回 None(调用方退化到队列状态)。

    ★ 真机接入验证清单(第一台 M1178 到货后必做):
      1. 页计数 prtMarkerLifeCount (1.3.6.1.2.1.43.10.2.1.4.1.1) 是否可读、单位是页还是张;
      2. hrPrinterDetectedErrorState 各位含义(缺纸/卡纸/门开/墨低)在本机的实际表现;
      3. 墨量/维护箱余量是否在 Epson 私有 MIB 暴露。
    """
    if not ip:
        return None
    try:
        from pysnmp.hlapi import (SnmpEngine, CommunityData, UdpTransportTarget,
                                  ContextData, ObjectType, ObjectIdentity, getCmd)
        oids = {
            "counter": "1.3.6.1.2.1.43.10.2.1.4.1.1",   # prtMarkerLifeCount
            "err": "1.3.6.1.2.1.25.3.5.1.2.1",          # hrPrinterDetectedErrorState
            "stat": "1.3.6.1.2.1.25.3.5.1.1.1",         # hrPrinterStatus
        }
        vals = {}
        for key, oid in oids.items():
            it = getCmd(SnmpEngine(), CommunityData("public"),
                        UdpTransportTarget((ip, 161), timeout=1, retries=1),
                        ContextData(), ObjectType(ObjectIdentity(oid)))
            err_ind, err_stat, _, binds = next(it)
            if err_ind or err_stat:
                return None
            vals[key] = binds[0][1].prettyPrint()
        counter = int(vals["counter"])
        st = int(vals["stat"])          # 3=idle 4=printing 5=printerStopped? 按RFC
        err_raw = vals["err"]
        # hrPrinterDetectedErrorState 是 OCTET STRING 位图: lowPaper/noPaper/jam 等
        detail = f"hrStatus={st} err={err_raw}"
        status = "printing" if st == 4 else "idle"
        if isinstance(err_raw, str) and err_raw.startswith("0x"):
            bits = int(err_raw[2:], 16)
            # 低位字节: bit3 noPaper, bit4 jam (常见实现,真机需校准)
            if bits & 0x08:
                status = "paper_out"
            elif bits & 0x10:
                status = "jam"
        return {"status": status, "page_counter": counter, "detail": detail}
    except Exception:  # noqa: BLE001 - pysnmp 未装/超时/不可达都视为不可用
        return None


# ============================ Linux CUPS ============================

class CupsBackend(BackendBase):
    name = "cups"

    def __init__(self):
        import cups
        self._cups = cups.Connection()

    def submit(self, printer, pdf_path):
        return str(self._cups.printFile(printer["queue_name"], pdf_path,
                                        "print-cluster", {}))

    def poll(self, printer):
        snmp = _snmp_status(printer.get("ip", ""))
        if snmp:
            return snmp
        printers = self._cups.getPrinters()
        info = printers.get(printer["queue_name"])
        if info is None:
            return {"status": "offline", "page_counter": -1, "detail": "队列不存在"}
        state = info.get("printer-state")           # 3=idle 4=printing 5=stopped
        reasons = ",".join(info.get("printer-state-reasons", []))
        if "media-empty" in reasons:
            st = "paper_out"
        elif state == 5 or "offline" in reasons:
            st = "offline"
        else:
            st = "printing" if state == 4 else "idle"
        return {"status": st, "page_counter": -1, "detail": reasons}

    def cancel(self, printer):
        try:
            self._cups.cancelJob(int(printer.get("_job_id", 0)), purge_job=True)
        except Exception:  # noqa: BLE001
            pass


# ============================ Windows ============================

class WindowsBackend(BackendBase):
    name = "windows"

    def submit(self, printer, pdf_path):
        import subprocess
        args = [config.SUMATRA_PATH, "-print-to", printer["queue_name"],
                "-print-settings", "duplexlong,1copy", "-silent", str(pdf_path)]
        subprocess.Popen(args)
        return f"win-{printer['id']}-{int(time.time())}"

    def poll(self, printer):
        snmp = _snmp_status(printer.get("ip", ""))
        if snmp:
            return snmp
        try:
            import win32print
            h = win32print.OpenPrinter(printer["queue_name"])
            info = win32print.GetPrinter(h, 2)
            win32print.ClosePrinter(h)
            status = info["Status"]
            if status & 0x00000020:
                st = "paper_out"
            elif status & 0x00000008:
                st = "jam"
            elif status & 0x00000400 or status & 0x00000002:
                st = "offline"
            else:
                st = "idle"
            return {"status": st, "page_counter": -1, "detail": f"win32 status={status}"}
        except Exception as e:  # noqa: BLE001
            return {"status": "offline", "page_counter": -1, "detail": str(e)}


def get_backend(name: str | None = None) -> BackendBase:
    name = (name or config.BACKEND).lower()
    if name == "cups":
        return CupsBackend()
    if name == "windows":
        return WindowsBackend()
    return SimBackend()
