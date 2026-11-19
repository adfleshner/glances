#!/usr/bin/env python
#
# SPDX-FileCopyrightText: 2026 Aditya Raj Singh <aditya@bncw.in>
#
# SPDX-License-Identifier: LGPL-3.0-only

from collections import namedtuple
from unittest.mock import patch

from glances.cpu_percent import cpu_percent


def test_percpu_uses_guest_nice_value():
    cpu_times_type = namedtuple(
        'scputimes',
        'user nice system idle iowait irq softirq steal guest guest_nice',
    )
    cpu_times = cpu_times_type(
        user=1.0,
        nice=2.0,
        system=3.0,
        idle=4.0,
        iowait=5.0,
        irq=6.0,
        softirq=7.0,
        steal=8.0,
        guest=9.0,
        guest_nice=10.0,
    )

    with patch('glances.cpu_percent.psutil.cpu_times_percent', return_value=[cpu_times]):
        stats = cpu_percent._compute_percpu()

    assert stats[0]['steal'] == 8.0
    assert stats[0]['guest_nice'] == 10.0


def test_cpu_name_on_windows_reads_the_registry():
    from glances.cpu_percent import CpuPercent

    with (
        patch('glances.cpu_percent.WINDOWS', True),
        patch('glances.cpu_percent.windows_cpu_name', return_value='AMD Ryzen 5 5600X 6-Core Processor'),
    ):
        assert CpuPercent._CpuPercent__get_cpu_name() == 'AMD Ryzen 5 5600X 6-Core Processor'


def test_cpu_name_on_windows_falls_back_when_registry_is_unreadable():
    from glances.cpu_percent import CpuPercent

    with (
        patch('glances.cpu_percent.WINDOWS', True),
        patch('glances.cpu_percent.windows_cpu_name', return_value=None),
        patch('builtins.open', side_effect=FileNotFoundError),
        patch('platform.processor', return_value='AMD64 Family 25 Model 33'),
    ):
        assert CpuPercent._CpuPercent__get_cpu_name() == 'CPU AMD64 Family 25 Model 33'
