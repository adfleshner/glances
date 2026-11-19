#
# Glances - An eye on your system
#
# SPDX-FileCopyrightText: 2026 Aaron Fleshner
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for the Windows GPU card (glances/plugins/gpu/cards/amd_windows.py)."""

from __future__ import annotations

import sys

import pytest

from glances.plugins.gpu.cards import amd_windows as windows

# Instance names as Windows 11 reports them for a Radeon RX 6700 XT (luid ..1f280)
# and a Radeon RX 6800 XT (luid ..1d143).
RX6700 = 'luid_0x00000000_0x0001f280'
RX6800 = 'luid_0x00000000_0x0001d143'


def test_engine_load_adds_processes_per_engine_and_keeps_the_busiest_engine():
    samples = {
        f'pid_10520_{RX6700}_phys_0_eng_0_engtype_3D': 30.0,
        f'pid_26040_{RX6700}_phys_0_eng_0_engtype_3D': 12.4,
        f'pid_4_{RX6700}_phys_0_eng_3_engtype_Compute': 20.0,
        f'pid_14968_{RX6800}_phys_0_eng_0_engtype_3D': 0.3,
    }
    assert windows.engine_load_by_luid(samples) == {RX6700: 42, RX6800: 0}


def test_engine_load_is_capped_at_100():
    samples = {
        f'pid_1_{RX6700}_phys_0_eng_0_engtype_3D': 70.0,
        f'pid_2_{RX6700}_phys_0_eng_0_engtype_3D': 45.0,
    }
    assert windows.engine_load_by_luid(samples) == {RX6700: 100}


def test_engine_load_ignores_names_without_an_engine():
    assert windows.engine_load_by_luid({f'{RX6700}_phys_0': 50.0, 'garbage': 10.0}) == {}


def test_memory_used_by_luid_reads_adapter_memory_instances():
    samples = {f'{RX6800}_phys_0': 4886364160.0, f'{RX6700}_phys_0': 5341937664.0}
    assert windows.memory_used_by_luid(samples) == {RX6800: 4886364160, RX6700: 5341937664}


@pytest.mark.parametrize(
    ('used', 'total', 'expected'),
    [
        (5341937664, 12836900864, 42),
        (None, 12836900864, None),
        (100, 0, None),
        # APU: 512 MiB carve-out, more than that counted as dedicated usage
        (805306368, 536870912, 100),
    ],
)
def test_memory_percent(used, total, expected):
    assert windows.memory_percent(used, total) == expected


def test_get_device_stats_builds_one_card_per_adapter(monkeypatch):
    class _Counters:
        def collect(self):
            return [
                {f'pid_1_{RX6700}_phys_0_eng_0_engtype_3D': 25.0},
                {f'{RX6700}_phys_0': 6418450432.0, f'{RX6800}_phys_0': 0.0},
            ]

    gpu = windows.AmdWindowsGPU.__new__(windows.AmdWindowsGPU)
    gpu.adapters = [
        windows.Adapter('AMD Radeon RX 6700 XT', 0x1002, RX6700, 0x1F280, 0, 12836900864),
        windows.Adapter('AMD Radeon RX 6800 XT', 0x1002, RX6800, 0x1D143, 0, 17130815488),
    ]
    gpu.counters = _Counters()
    monkeypatch.setattr(windows, 'get_temperature', lambda adapter: 48)

    assert gpu.get_device_stats() == [
        {
            'key': 'gpu_id',
            'gpu_id': 'amd0',
            'name': 'AMD Radeon RX 6700 XT',
            'mem': 50,
            'proc': 25,
            'temperature': 48,
            'fan_speed': None,
        },
        {
            'key': 'gpu_id',
            'gpu_id': 'amd1',
            'name': 'AMD Radeon RX 6800 XT',
            'mem': 0,
            'proc': 0,
            'temperature': 48,
            'fan_speed': None,
        },
    ]


def test_proc_is_unknown_before_the_first_rate_sample(monkeypatch):
    class _Counters:
        def collect(self):
            return [{}, {f'{RX6700}_phys_0': 0.0}]

    gpu = windows.AmdWindowsGPU.__new__(windows.AmdWindowsGPU)
    gpu.adapters = [windows.Adapter('AMD Radeon RX 6700 XT', 0x1002, RX6700, 0x1F280, 0, 12836900864)]
    gpu.counters = _Counters()
    monkeypatch.setattr(windows, 'get_temperature', lambda adapter: None)

    assert gpu.get_device_stats()[0]['proc'] is None


def test_init_keeps_only_amd_and_refuses_when_nothing_is_left(monkeypatch):
    monkeypatch.setattr(windows, 'WINDOWS', True)
    monkeypatch.setattr(
        windows,
        'list_adapters',
        lambda: [
            windows.Adapter('GeForce RTX 4090', 0x10DE, RX6700, 1, 0, 1),
            windows.Adapter('Intel UHD Graphics 770', 0x8086, RX6800, 2, 0, 1),
        ],
    )
    with pytest.raises(RuntimeError):
        windows.AmdWindowsGPU()


def test_init_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(windows, 'WINDOWS', False)
    with pytest.raises(RuntimeError):
        windows.AmdWindowsGPU()


@pytest.mark.skipif(not sys.platform.startswith('win'), reason='Windows APIs')
def test_real_windows_apis_answer_without_errors():
    """DXGI, PDH and D3DKMT calls work on this machine (values depend on its hardware)."""
    adapters = windows.list_adapters()
    assert all(a.luid.startswith('luid_0x') for a in adapters)
    counters = windows.PdhCounters([windows.ENGINE_COUNTER, windows.MEMORY_COUNTER])
    try:
        engines, memory = counters.collect()
    finally:
        counters.close()
    assert isinstance(engines, dict) and isinstance(memory, dict)
    for adapter in adapters:
        temperature = windows.get_temperature(adapter)
        assert temperature is None or 0 < temperature < 150
