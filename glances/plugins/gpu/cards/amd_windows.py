#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Aaron Fleshner
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""AMD (Windows) Extension unit for Glances' GPU plugin.

The AMD backend reads Linux sysfs, so on Windows AMD cards were not shown.
This one reads what Windows itself knows about the adapters, the same
sources Task Manager uses, so no AMD library is needed.

- Adapters (name, vendor, dedicated memory, LUID): DXGI `EnumAdapters1`.
- Load and memory used: the `GPU Engine` and `GPU Adapter Memory`
  performance counters (PDH), whose instance names carry the adapter LUID.
- Temperature: `D3DKMTQueryAdapterInfo(KMTQAITYPE_ADAPTERPERFDATA)`
  (Windows 10 2004 or later, and only when the driver reports it).

Only AMD adapters are reported; the others (NVIDIA, Intel, the Microsoft
Basic Render Driver) are left out.
"""

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass

from glances.globals import WINDOWS

AMD_VENDOR_ID: int = 0x1002
DXGI_ADAPTER_FLAG_SOFTWARE: int = 0x2
ENGINE_COUNTER: str = r'\GPU Engine(*)\Utilization Percentage'
MEMORY_COUNTER: str = r'\GPU Adapter Memory(*)\Dedicated Usage'
INSTANCE_PATTERN = re.compile(r'(luid_0x[0-9a-f]{8}_0x[0-9a-f]{8})_phys_(\d+)(?:_eng_(\d+))?', re.IGNORECASE)


@dataclass(frozen=True)
class Adapter:
    """One display adapter as DXGI reports it."""

    name: str
    vendor_id: int
    luid: str
    luid_low: int
    luid_high: int
    memory_total: int


class AmdWindowsGPU:
    """AMD GPU card class for Windows."""

    def __init__(self):
        """Init AMD Windows GPU card class. Raises when there is nothing to monitor."""
        if not WINDOWS:
            raise RuntimeError('Windows only')
        self.adapters = [a for a in list_adapters() if a.vendor_id == AMD_VENDOR_ID]
        if not self.adapters:
            raise RuntimeError('No AMD adapter')
        self.counters = PdhCounters([ENGINE_COUNTER, MEMORY_COUNTER])

    def exit(self):
        """Close AMD Windows GPU class."""
        self.counters.close()

    def get_device_stats(self):
        """Get AMD Windows GPU stats."""
        engine_samples, memory_samples = self.counters.collect()
        load = engine_load_by_luid(engine_samples)
        memory_used = memory_used_by_luid(memory_samples)
        # No engine sample at all is the first (rate-less) collection: unknown.
        # Otherwise an adapter that no process uses is idle.
        idle = 0 if engine_samples else None

        stats = []
        for index, adapter in enumerate(self.adapters):
            stats.append(
                {
                    # Dictionary key is the GPU_ID
                    'key': 'gpu_id',
                    # GPU id (for multiple GPU, start at 0)
                    'gpu_id': f'amd{index}',
                    # GPU name
                    'name': adapter.name,
                    # Memory consumption in %
                    'mem': memory_percent(memory_used.get(adapter.luid), adapter.memory_total),
                    # Processor consumption in %
                    'proc': load.get(adapter.luid, idle),
                    # Processor temperature in °C
                    'temperature': get_temperature(adapter),
                    # Fan speed in % (Windows only reports RPM)
                    'fan_speed': None,
                }
            )
        return stats


def engine_load_by_luid(samples: dict[str, float]) -> dict[str, int]:
    """Return the load in % per adapter LUID, the way Task Manager computes it.

    Each `GPU Engine` instance is one process on one engine. The processes are
    added up per engine, and the adapter load is its busiest engine.
    """
    per_engine: dict[tuple[str, str, str], float] = {}
    for instance, value in samples.items():
        match = INSTANCE_PATTERN.search(instance)
        if match and match.group(3) is not None:
            engine = (match.group(1).lower(), match.group(2), match.group(3))
            per_engine[engine] = per_engine.get(engine, 0.0) + value
    load: dict[str, int] = {}
    for (luid, _, _), value in per_engine.items():
        load[luid] = max(load.get(luid, 0), min(round(value), 100))
    return load


def memory_used_by_luid(samples: dict[str, float]) -> dict[str, int]:
    """Return the dedicated memory used in bytes per adapter LUID."""
    used: dict[str, int] = {}
    for instance, value in samples.items():
        match = INSTANCE_PATTERN.search(instance)
        if match:
            luid = match.group(1).lower()
            used[luid] = used.get(luid, 0) + int(value)
    return used


def memory_percent(used: int | None, total: int) -> int | None:
    """Return the memory consumption in %, or None when unknown."""
    if used is None or total <= 0:
        return None
    return round(used / total * 100)


# --- DXGI: the adapter list -------------------------------------------------


class _LUID(ctypes.Structure):
    _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ('Description', wintypes.WCHAR * 128),
        ('VendorId', wintypes.UINT),
        ('DeviceId', wintypes.UINT),
        ('SubSysId', wintypes.UINT),
        ('Revision', wintypes.UINT),
        ('DedicatedVideoMemory', ctypes.c_size_t),
        ('DedicatedSystemMemory', ctypes.c_size_t),
        ('SharedSystemMemory', ctypes.c_size_t),
        ('AdapterLuid', _LUID),
        ('Flags', wintypes.UINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', ctypes.c_ubyte * 8),
    ]


# 770aae78-f26f-4dba-a829-253c83d1b387
_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)
# COM vtable slots
_RELEASE = 2
_FACTORY_ENUM_ADAPTERS1 = 12
_ADAPTER_GET_DESC1 = 10


def _com_method(obj: ctypes.c_void_p, slot: int, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])


def list_adapters() -> list[Adapter]:
    """Return the hardware display adapters DXGI knows about."""
    factory = ctypes.c_void_p()
    if ctypes.WinDLL('dxgi').CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
        raise OSError('CreateDXGIFactory1 failed')
    adapters = []
    try:
        enum_adapters = _com_method(factory, _FACTORY_ENUM_ADAPTERS1, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        index = 0
        adapter = ctypes.c_void_p()
        while enum_adapters(factory, index, ctypes.byref(adapter)) == 0:
            desc = _DXGI_ADAPTER_DESC1()
            try:
                get_desc1 = _com_method(adapter, _ADAPTER_GET_DESC1, ctypes.POINTER(_DXGI_ADAPTER_DESC1))
                get_desc1(adapter, ctypes.byref(desc))
            finally:
                _com_method(adapter, _RELEASE)(adapter)
            if not desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE:
                low, high = desc.AdapterLuid.LowPart, desc.AdapterLuid.HighPart
                adapters.append(
                    Adapter(
                        name=desc.Description.strip(),
                        vendor_id=desc.VendorId,
                        luid=f'luid_0x{high & 0xFFFFFFFF:08x}_0x{low:08x}',
                        luid_low=low,
                        luid_high=high,
                        memory_total=desc.DedicatedVideoMemory,
                    )
                )
            index += 1
    finally:
        _com_method(factory, _RELEASE)(factory)
    return adapters


# --- PDH: load and memory used ----------------------------------------------


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [('CStatus', wintypes.DWORD), ('doubleValue', ctypes.c_double)]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [('szName', wintypes.LPWSTR), ('FmtValue', _PDH_FMT_COUNTERVALUE)]


_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2
_PDH_CSTATUS_VALID_DATA = 0x0
_PDH_CSTATUS_NEW_DATA = 0x1


class PdhCounters:
    """A PDH query over wildcard counters, read as {instance name: value}."""

    def __init__(self, paths: list[str]):
        self.pdh = ctypes.WinDLL('pdh')
        self.query = wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)) != 0:
            raise OSError('PdhOpenQueryW failed')
        self.counters = []
        for path in paths:
            counter = wintypes.HANDLE()
            if self.pdh.PdhAddEnglishCounterW(self.query, path, None, ctypes.byref(counter)) != 0:
                self.close()
                raise OSError(f'PdhAddEnglishCounterW failed for {path}')
            self.counters.append(counter)
        # Rate counters need a first sample to compare the next one against.
        self.pdh.PdhCollectQueryData(self.query)

    def collect(self) -> list[dict[str, float]]:
        """Take a sample and return one {instance: value} dict per counter."""
        self.pdh.PdhCollectQueryData(self.query)
        return [self._values(counter) for counter in self.counters]

    def _values(self, counter: wintypes.HANDLE) -> dict[str, float]:
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = self.pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
        )
        if status & 0xFFFFFFFF != _PDH_MORE_DATA:
            return {}
        buffer = (ctypes.c_byte * size.value)()
        status = self.pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buffer
        )
        if status != 0:
            return {}
        items = ctypes.cast(buffer, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W))
        return {
            items[i].szName: items[i].FmtValue.doubleValue
            for i in range(count.value)
            if items[i].FmtValue.CStatus in (_PDH_CSTATUS_VALID_DATA, _PDH_CSTATUS_NEW_DATA)
        }

    def close(self):
        """Close the PDH query."""
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = wintypes.HANDLE()


# --- D3DKMT: temperature ----------------------------------------------------


class _D3DKMT_OPENADAPTERFROMLUID(ctypes.Structure):
    _fields_ = [('AdapterLuid', _LUID), ('hAdapter', wintypes.UINT)]


class _D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [
        ('hAdapter', wintypes.UINT),
        ('Type', ctypes.c_int),
        ('pPrivateDriverData', ctypes.c_void_p),
        ('PrivateDriverDataSize', wintypes.UINT),
    ]


class _D3DKMT_CLOSEADAPTER(ctypes.Structure):
    _fields_ = [('hAdapter', wintypes.UINT)]


class _D3DKMT_ADAPTER_PERFDATA(ctypes.Structure):
    _fields_ = [
        ('PhysicalAdapterIndex', ctypes.c_uint32),
        ('MemoryFrequency', ctypes.c_uint64),
        ('MaxMemoryFrequency', ctypes.c_uint64),
        ('MaxMemoryFrequencyOC', ctypes.c_uint64),
        ('MemoryBandwidth', ctypes.c_uint64),
        ('PCIEBandwidth', ctypes.c_uint64),
        ('FanRPM', ctypes.c_ulong),
        ('Power', ctypes.c_ulong),
        ('Temperature', ctypes.c_ulong),  # deci-Celsius
        ('PowerStateOverride', ctypes.c_ubyte),
    ]


_KMTQAITYPE_ADAPTERPERFDATA = 62


def get_temperature(adapter: Adapter) -> int | None:
    """Return the adapter temperature in °C, or None when the driver does not report it."""
    gdi32 = ctypes.WinDLL('gdi32')
    opened = _D3DKMT_OPENADAPTERFROMLUID(_LUID(adapter.luid_low, adapter.luid_high))
    if gdi32.D3DKMTOpenAdapterFromLuid(ctypes.byref(opened)) != 0:
        return None
    try:
        data = _D3DKMT_ADAPTER_PERFDATA()
        query = _D3DKMT_QUERYADAPTERINFO(
            opened.hAdapter,
            _KMTQAITYPE_ADAPTERPERFDATA,
            ctypes.cast(ctypes.byref(data), ctypes.c_void_p),
            ctypes.sizeof(data),
        )
        if gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(query)) != 0:
            return None
    finally:
        gdi32.D3DKMTCloseAdapter(ctypes.byref(_D3DKMT_CLOSEADAPTER(opened.hAdapter)))
    return round(data.Temperature / 10) if data.Temperature else None
