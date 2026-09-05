"""일반 페이지 폴트와 구분되는 Windows 시스템 페이징 카운터를 계측합니다."""

import ctypes
from ctypes import wintypes
import time


class CounterValue(ctypes.Structure):
    """PDH_FMT_DOUBLE로 요청한 공식 카운터 결과 배치를 선언합니다."""
    _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]


class PagingMonitor:
    """시스템 전체의 디스크 페이지 입출력과 가용 메모리를 읽습니다."""

    PATHS = {
        "page_reads_per_second": r"\Memory\Page Reads/sec",
        "page_writes_per_second": r"\Memory\Page Writes/sec",
        "pages_input_per_second": r"\Memory\Pages Input/sec",
        "pages_output_per_second": r"\Memory\Pages Output/sec",
        "available_bytes": r"\Memory\Available Bytes",
        "committed_bytes": r"\Memory\Committed Bytes",
        "paging_file_usage_percent": r"\Paging File(_Total)\% Usage",
        "machine_cpu_percent": r"\Processor(_Total)\% Processor Time",
    }

    def __init__(self):
        """영문 API로 지역화와 무관한 카운터를 열고 첫 표본을 수집합니다."""
        self.api = ctypes.WinDLL("pdh")
        self.api.PdhOpenQueryW.argtypes = (wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE))
        self.api.PdhAddEnglishCounterW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE))
        self.api.PdhCollectQueryData.argtypes = (wintypes.HANDLE,)
        self.api.PdhGetFormattedCounterValue.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(CounterValue))
        self.api.PdhCloseQuery.argtypes = (wintypes.HANDLE,)
        self.query = wintypes.HANDLE()
        self.counters = {}
        self.samples = []
        self._check(self.api.PdhOpenQueryW(None, 0, ctypes.byref(self.query)))
        try:
            for name, path in self.PATHS.items():
                counter = wintypes.HANDLE()
                self._check(self.api.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(counter)))
                self.counters[name] = counter
            self._check(self.api.PdhCollectQueryData(self.query))
            self.started = self.last = time.monotonic()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(status):
        """카운터 접근 실패를 측정값 0으로 숨기지 않고 중단합니다."""
        if status:
            raise OSError(f"Windows PDH 측정 실패: 0x{status & 0xffffffff:08x}")

    def sample(self, phase):
        """직전 수집 이후 구간 값을 읽고 단계와 실제 간격을 기록합니다."""
        self._check(self.api.PdhCollectQueryData(self.query))
        now = time.monotonic()
        row = {"phase": phase, "elapsed_seconds": now - self.started, "interval_seconds": now - self.last}
        for name, counter in self.counters.items():
            value = CounterValue()
            self._check(self.api.PdhGetFormattedCounterValue(counter, 0x200, None, ctypes.byref(value)))
            if value.status not in (0, 1):
                raise OSError(f"Windows 카운터 값이 유효하지 않습니다: {name}, {value.status}")
            row[name] = value.value
        self.samples.append(row)
        self.last = now

    def report(self):
        """원시 표본과 단계별 최솟값·최댓값을 보존하고 전역 계측 한계를 표시합니다."""
        summary = {}
        for phase in ("baseline", "alignment"):
            rows = [row for row in self.samples if row["phase"] == phase]
            summary[phase] = {name: {"min": min(row[name] for row in rows), "max": max(row[name] for row in rows)}
                              for name in self.PATHS} if rows else {}
        return {"method": "Windows PDH PdhAddEnglishCounterW", "scope": "SYSTEM_WIDE",
                "counter_paths": self.PATHS, "samples": self.samples, "summary": summary,
                "limitation": "디스크 읽기에는 실행 파일·DLL·메모리 매핑 파일의 하드 폴트도 포함됩니다. 시스템 전체 값이므로 특정 프로세스의 스왑 읽기량으로 단정하지 않습니다.",
                "source": "https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/how-to-determine-the-appropriate-page-file-size-for-64-bit-versions-of-windows"}

    def close(self):
        """카운터 핸들을 한 번만 닫습니다."""
        if self.query:
            self.api.PdhCloseQuery(self.query)
            self.query = wintypes.HANDLE()


class MemoryCounters(ctypes.Structure):
    """공식 PROCESS_MEMORY_COUNTERS_EX 메모리 배치를 선언합니다."""
    _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
        ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage",
         "QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage","PrivateUsage")]


class ProcessEntry(ctypes.Structure):
    """공식 PROCESSENTRY32W 배치로 작업 프로세스의 자식만 식별합니다."""
    _fields_=[("dwSize",wintypes.DWORD),("cntUsage",wintypes.DWORD),("pid",wintypes.DWORD),
        ("heap",ctypes.c_size_t),("module",wintypes.DWORD),("threads",wintypes.DWORD),
        ("parent",wintypes.DWORD),("priority",wintypes.LONG),("flags",wintypes.DWORD),("exe",wintypes.WCHAR*260)]


class MemoryMonitor:
    """Windows 공식 API의 관측 메모리와 프로세스별 최고 작업 집합을 기록합니다."""
    def __init__(self):
        """메모리와 프로세스 조회용 읽기 전용 API를 연결합니다."""
        self.kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        self.kernel.K32GetProcessMemoryInfo.argtypes=(wintypes.HANDLE,ctypes.POINTER(MemoryCounters),wintypes.DWORD)
        self.kernel.CreateToolhelp32Snapshot.argtypes=(wintypes.DWORD,wintypes.DWORD)
        self.kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        for name in ("Process32FirstW","Process32NextW"):
            getattr(self.kernel,name).argtypes=(wintypes.HANDLE,ctypes.POINTER(ProcessEntry))
        self.processes={}
        self.observed_peak_sum=0
        self.samples=0
        self.errors={}

    def read(self,pid):
        """지정 PID의 실제 작업 집합과 최고 커밋 사용량을 조회합니다."""
        handle=self.kernel.OpenProcess(0x410,False,pid)
        if not handle:
            self.errors[str(pid)]=ctypes.get_last_error()
            return None
        try:
            counters=MemoryCounters();counters.cb=ctypes.sizeof(counters)
            if not self.kernel.K32GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb):
                self.errors[str(pid)]=ctypes.get_last_error()
                return None
            return {"working_set_bytes":int(counters.WorkingSetSize),"peak_working_set_bytes":int(counters.PeakWorkingSetSize),
                    "private_bytes":int(counters.PrivateUsage),"peak_commit_bytes":int(counters.PeakPagefileUsage)}
        finally:self.kernel.CloseHandle(handle)

    def sample(self,root_pid):
        """조정자와 관측된 자식 프로세스만 0.1초 간격으로 함께 측정합니다."""
        snapshot=self.kernel.CreateToolhelp32Snapshot(2,0)
        if snapshot==ctypes.c_void_p(-1).value:
            self.errors["snapshot"]=ctypes.get_last_error();return
        table={}
        try:
            entry=ProcessEntry();entry.dwSize=ctypes.sizeof(entry)
            valid=self.kernel.Process32FirstW(snapshot,ctypes.byref(entry))
            while valid:
                table[int(entry.pid)]=(int(entry.parent),str(entry.exe))
                valid=self.kernel.Process32NextW(snapshot,ctypes.byref(entry))
        finally:self.kernel.CloseHandle(snapshot)
        descendants={root_pid}
        while True:
            added={pid for pid,(parent,_) in table.items() if parent in descendants}
            if added.issubset(descendants):break
            descendants.update(added)
        current_sum=0
        for pid in descendants:
            counters=self.read(pid)
            if counters is None:continue
            current_sum+=counters["working_set_bytes"]
            previous=self.processes.get(str(pid),{})
            self.processes[str(pid)]={"name":table.get(pid,(0,"종료된 작업"))[1],**{name:max(previous.get(name,0),value) for name,value in counters.items()}}
        self.observed_peak_sum=max(self.observed_peak_sum,current_sum)
        self.samples+=1

    def report(self):
        """표본 최고값과 조회 실패를 구분하고 측정 범위를 명시합니다."""
        return {"method":"Windows GetProcessMemoryInfo(PROCESS_MEMORY_COUNTERS_EX)","sampling_interval_seconds":.1,
                "samples":self.samples,"observed_worker_tree_peak_working_set_bytes":self.observed_peak_sum,
                "processes":self.processes,"read_errors":self.errors,
                "limitation":"조정자 시작 후 0.1초 표본에서 관측한 작업 집합 합계입니다. 짧은 자식 프로세스를 놓칠 수 있으며 정확한 동시 최고 메모리로 추정하지 않습니다.",
                "source":"https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex"}
