# -*- coding: utf-8 -*-
"""
Windows 打印后端: 提交PDF / 查询状态 / 取消 的统一抽象。

- WindowsBackend  SumatraPDF 命令行打印 + win32print/SNMP 状态

poll() 统一返回: {"status": idle|printing|paper_out|jam|offline,
                 "page_counter": int(-1表示不可用), "detail": str}
"""
import time
from pathlib import Path

from . import config


class SubmissionNotStarted(RuntimeError):
    """A preflight failure: no printing process was started."""


class BackendBase:
    name = "base"

    def submit(self, printer: dict, pdf_path: str) -> str:
        """下发合成PDF(永远只打1份,份数已拼进PDF)。返回本地作业号。"""
        raise NotImplementedError

    def poll(self, printer: dict) -> dict:
        raise NotImplementedError

    def cancel(self, printer: dict):
        pass


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


# ============================ Windows ============================

class WindowsBackend(BackendBase):
    name = "windows"

    def inspect_group_jobs(self, printer, pdf_path):
        """Read this class job and total queue size without changing any job."""
        import win32print
        from pathlib import PureWindowsPath
        expected = PureWindowsPath(pdf_path).name.casefold()
        handle = win32print.OpenPrinter(printer['queue_name'])
        try:
            jobs = win32print.EnumJobs(handle, 0, 9999, 1)
            matched = [job['JobId'] for job in jobs
                       if PureWindowsPath(job.get('pDocument') or '').name.casefold() == expected]
            return {'matching_job_ids': matched, 'queue_count': len(jobs)}
        finally:
            win32print.ClosePrinter(handle)

    def control_group(self, printer, pdf_path, action):
        """只操作本程序对应文件的作业，不清空其他应用的打印作业。"""
        import win32print
        from pathlib import PureWindowsPath
        expected = PureWindowsPath(pdf_path).name.casefold()
        handle = win32print.OpenPrinter(printer['queue_name'])
        try:
            jobs = win32print.EnumJobs(handle, 0, 9999, 1)
            matched = [job for job in jobs if PureWindowsPath(job.get('pDocument') or '').name.casefold() == expected]
            # A prefetched class may have left the queue since the last poll.
            # A missing match is a no-op, never a reason to control other jobs.
            command = {'pause': win32print.JOB_CONTROL_PAUSE,
                       'resume': win32print.JOB_CONTROL_RESUME,
                       'cancel': win32print.JOB_CONTROL_DELETE}[action]
            for job in matched:
                win32print.SetJob(handle, job['JobId'], 0, None, command)
            return len(matched)
        finally:
            win32print.ClosePrinter(handle)

    @staticmethod
    def installed_printers():
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [{"queue": row["pPrinterName"], "driver": row.get("pDriverName", ""),
                 "port": row.get("pPortName", "")}
                for row in win32print.EnumPrinters(flags, None, 2)]

    def submit(self, printer, pdf_path):
        import subprocess
        if not Path(config.SUMATRA_PATH).is_file():
            raise SubmissionNotStarted("缺少 PDF 打印组件 SumatraPDF.exe")
        if not Path(pdf_path).is_file():
            raise SubmissionNotStarted("班级 PDF 文件不存在")
        try:
            installed = {item["queue"] for item in self.installed_printers()}
        except Exception as exc:
            raise SubmissionNotStarted(f"无法读取 Windows 打印队列：{exc}") from exc
        if printer["queue_name"] not in installed:
            raise SubmissionNotStarted("Windows 打印队列不存在，请先在系统中安装驱动并添加打印机")
        args = [config.SUMATRA_PATH, "-print-to", printer["queue_name"],
                "-print-settings", "duplexlong,monochrome,1x", "-silent", str(pdf_path)]
        try:
            result = subprocess.run(args, timeout=300, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise SubmissionNotStarted(f"无法启动 PDF 打印组件：{exc}") from exc
        if result.returncode:
            raise RuntimeError(f"PDF 下发失败，打印组件返回代码 {result.returncode}")
        return f"win-{printer['id']}-{int(time.time())}"

    def poll_queue(self, printer):
        """Fast Windows-only snapshot for monitoring during PDF submission."""
        return self.poll(printer, use_snmp=False)

    def poll(self, printer, use_snmp=True):
        try:
            if printer["queue_name"] not in {item["queue"] for item in self.installed_printers()}:
                return {"status": "offline", "page_counter": -1, "detail": "Windows 队列不存在"}
        except Exception as e:
            return {"status": "offline", "page_counter": -1, "detail": str(e)}
        try:
            import win32print
            h = win32print.OpenPrinter(printer["queue_name"])
            try:
                info = win32print.GetPrinter(h, 2)
                jobs = win32print.EnumJobs(h, 0, 9999, 1)
            finally:
                win32print.ClosePrinter(h)
            status = info["Status"]
            if status & 0x00000020:
                st = "paper_out"
            elif status & 0x00000008:
                st = "jam"
            elif status & 0x00000400 or status & 0x00000002:
                st = "offline"
            elif jobs:
                st = "printing"
            else:
                st = "idle"
            snmp = _snmp_status(printer.get("ip", "")) if use_snmp else None
            result = snmp or {"status": st, "page_counter": -1, "detail": f"win32 status={status}"}
            if st in ('offline', 'paper_out', 'jam'):
                result['status'] = st
            result['detail'] = f"win32 status={status}; {result.get('detail', '')}"
            def flag(name, default):
                value = getattr(win32print, name, default)
                return value if isinstance(value, int) else default
            completed = flag('JOB_STATUS_PRINTED', 0x80) | flag('JOB_STATUS_COMPLETE', 0x1000)
            failed = (flag('JOB_STATUS_ERROR', 0x2) | flag('JOB_STATUS_DELETING', 0x4) |
                      flag('JOB_STATUS_DELETED', 0x100) | flag('JOB_STATUS_BLOCKED_DEVQ', 0x200))
            result['jobs'] = [{'id': job['JobId'], 'document': job.get('pDocument') or '',
                               'status_code': job.get('Status', 0),
                               'status_text': job.get('pStatus') or '',
                               'printing': bool(job.get('Status', 0) & flag('JOB_STATUS_PRINTING', 0x10)),
                               'failed': bool(job.get('Status', 0) & failed),
                               'finished': bool(job.get('Status', 0) & completed)} for job in jobs]
            return result
        except Exception as e:  # noqa: BLE001
            return {"status": "offline", "page_counter": -1, "detail": str(e)}
