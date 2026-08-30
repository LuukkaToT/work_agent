r"""生成 20 组、六组件分层的 synthetic 基带诊断 benchmark。

输出属于测试数据，不代表华为或任何厂商的真实日志/错误码。场景语义参考
3GPP/O-RAN 的公开过程和开源 gNB 的分层日志习惯，组件名、字段和错误码均为
本仓库自定义。

本脚本是数据集的可复现来源。修改 SCENARIOS 后执行：

    .\.venv\Scripts\python.exe scripts\generate_mock_benchmark.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
KB_ROOT = ROOT / "work_agent" / "tools" / "mock" / "mock_5g_fault_kb"
LOG_ROOT = KB_ROOT / "logs" / "benchmark"
DATASET_PATH = KB_ROOT / "dataset" / "benchmark_cases.json"
EVAL_PATH = ROOT / "config" / "eval_cases.json"

COMPONENTS = ("comm", "rat", "bbh", "bbl", "marp", "compare")
BACKGROUND_LINES_PER_COMPONENT = 1100
PIPELINE_PLACEHOLDER = "{{PIPELINE_ID}}"
BASE_TIME = datetime(2026, 8, 9, 10, 0, 0)


def event(offset_ms: int, level: str, message: str) -> str:
    """构造紧凑事件定义；落日志时再补时间、组件和线程字段。"""
    return f"{offset_ms}|{level}|{message}"


def pipeline(case_name: str, *, version: str = "27B", host: int = 60) -> dict[str, Any]:
    return {
        "case_names": [case_name],
        "version": version,
        "physical_env": f"7.223.50.{host}",
    }


SCENARIOS: list[dict[str, Any]] = [
    {
        "scenario": "bench01_comm_connection_refused",
        "case_id": "b01_comm_connection_refused",
        "title": "COMM 端口未监听，RAT/BBL 级联失败",
        "fail_kind": "env",
        "root_component": "comm",
        "root_cause": "COMM 控制端点拒绝连接，服务未监听或端口配置错误",
        "expected_evidence_keys": ["E-COMM-1102", "Connection refused", "probable_component=comm"],
        "user_input": "小区 load fail，判断是 COMM、RAT 还是 BBL 根因。",
        "pipeline": pipeline("NR_BENCH_001_COMM_REFUSED", host=60),
        "events": {
            "comm": [
                event(112000, "DEBUG", "tcp_connect endpoint=10.10.7.21:19090 attempt=1 state=SYN_SENT"),
                event(112250, "ERROR", "COMM_LOAD_FAIL errorcode=E-COMM-1102 endpoint=10.10.7.21:19090 detail=Connection refused state=FAILED"),
            ],
            "rat": [event(113000, "ERROR", "RAT_LOAD_FAIL errorcode=E-RAT-1203 dependency=COMM dependency_state=FAILED cascade=true")],
            "bbh": [event(113500, "WARN", "radio_path_start skipped dependency=RAT_READY cascade=true")],
            "bbl": [event(114000, "ERROR", "CELL_SETUP_DEPENDENCY_FAILED cell=NR_CELL_0 errorcode=E-BBL-3104 probable_component=comm cascade=true")],
            "compare": [event(115000, "INFO", "verdict=NOT_RUN reason=CELL_NOT_READY")],
        },
    },
    {
        "scenario": "bench02_comm_connect_timeout",
        "case_id": "b02_comm_connect_timeout",
        "title": "COMM SYN 超时，疑似网络路径/防火墙异常",
        "fail_kind": "env",
        "root_component": "comm",
        "root_cause": "COMM 连接连续 SYN 超时，网络路径或防火墙丢包",
        "expected_evidence_keys": ["SYN timeout", "retry=3/3", "COMM_LOAD_FAIL"],
        "user_input": "COMM 一直 load fail 但不是 connection refused，帮我定位。",
        "pipeline": pipeline("NR_BENCH_002_COMM_TIMEOUT", host=61),
        "events": {
            "comm": [
                event(111500, "DEBUG", "tcp_connect endpoint=10.10.7.22:19090 retry=1/3 result=SYN timeout rtt_ms=3000"),
                event(113000, "DEBUG", "tcp_connect endpoint=10.10.7.22:19090 retry=2/3 result=SYN timeout rtt_ms=3000"),
                event(114500, "ERROR", "COMM_LOAD_FAIL errorcode=E-COMM-1102 endpoint=10.10.7.22:19090 retry=3/3 detail=connect timeout"),
            ],
            "rat": [event(115000, "WARN", "wait dependency=COMM_READY elapsed_ms=9000")],
            "bbl": [event(116000, "ERROR", "CELL_SETUP_DEPENDENCY_FAILED errorcode=E-BBL-3104 probable_component=comm cascade=true")],
        },
    },
    {
        "scenario": "bench03_comm_protocol_mismatch",
        "case_id": "b03_comm_protocol_mismatch",
        "title": "COMM TCP 正常但握手协议版本不兼容",
        "fail_kind": "version",
        "root_component": "comm",
        "root_cause": "27B 客户端与 26A 环境的 COMM 协议版本不兼容",
        "expected_evidence_keys": ["E-COMM-1107", "client_protocol=27B.4", "server_protocol=26A.9"],
        "user_input": "TCP 已经连上了但小区仍失败，是版本问题吗？",
        "pipeline": pipeline("NR_BENCH_003_PROTOCOL_MISMATCH", version="27B", host=62),
        "events": {
            "comm": [
                event(111500, "INFO", "tcp_connected endpoint=10.10.7.23:19090 rtt_ms=2"),
                event(112000, "DEBUG", "handshake_offer client_protocol=27B.4 capability_hash=8f2a"),
                event(112300, "ERROR", "COMM_PROTOCOL_INCOMPATIBLE errorcode=E-COMM-1107 client_protocol=27B.4 server_protocol=26A.9 handshake=rejected"),
            ],
            "rat": [event(113000, "ERROR", "RAT_LOAD_FAIL errorcode=E-RAT-1203 dependency=COMM_PROTOCOL cascade=true")],
            "bbl": [event(114000, "ERROR", "CELL_SETUP_DEPENDENCY_FAILED errorcode=E-BBL-3104 probable_component=software_version cascade=true")],
        },
    },
    {
        "scenario": "bench04_rat_invalid_bandwidth",
        "case_id": "b04_rat_invalid_bandwidth",
        "title": "RAT n78 带宽参数越界",
        "fail_kind": "case",
        "root_component": "rat",
        "root_cause": "用例给 n78 配置了不支持的 120MHz 带宽",
        "expected_evidence_keys": ["E-RAT-1211", "bandwidth_mhz=120", "allowed=[20,40,50,60,80,100]"],
        "user_input": "RAT load fail 后 BBL 报 cell setup fail，哪个是根因？",
        "pipeline": pipeline("NR_BENCH_004_INVALID_BANDWIDTH", host=63),
        "events": {
            "comm": [event(111000, "INFO", "state=READY protocol=27B.4")],
            "rat": [
                event(112000, "DEBUG", "validate_profile band=n78 scs_khz=30 bandwidth_mhz=120"),
                event(112200, "ERROR", "RAT_PROFILE_REJECTED errorcode=E-RAT-1211 field=bandwidth_mhz value=120 allowed=[20,40,50,60,80,100]"),
                event(112400, "ERROR", "RAT_LOAD_FAIL dependency=profile_validation cascade=true"),
            ],
            "bbl": [event(113000, "ERROR", "CELL_SETUP_DEPENDENCY_FAILED errorcode=E-BBL-3104 probable_component=rat cascade=true")],
        },
    },
    {
        "scenario": "bench05_rat_worker_not_ready",
        "case_id": "b05_rat_worker_not_ready",
        "title": "RAT 调度进程启动后退出",
        "fail_kind": "env",
        "root_component": "rat",
        "root_cause": "RAT scheduler worker 因 hugepage 资源不足退出",
        "expected_evidence_keys": ["E-RAT-1218", "hugepage_pool", "worker_exit_code=137"],
        "user_input": "配置校验都通过但 RAT 一直不 ready，请分析环境资源。",
        "pipeline": pipeline("NR_BENCH_005_RAT_WORKER_EXIT", host=64),
        "events": {
            "comm": [event(111000, "INFO", "state=READY protocol=27B.4")],
            "rat": [
                event(111500, "INFO", "profile_validation=PASSED state=STARTING"),
                event(112000, "DEBUG", "allocate hugepage_pool requested_mb=4096 available_mb=512"),
                event(112500, "ERROR", "RAT_PROCESS_NOT_READY errorcode=E-RAT-1218 process=scheduler worker_exit_code=137 resource=hugepage_pool"),
            ],
            "bbh": [event(113000, "WARN", "start deferred dependency=RAT_READY")],
            "bbl": [event(114000, "ERROR", "CELL_SETUP_DEPENDENCY_FAILED errorcode=E-BBL-3104 probable_component=rat cascade=true")],
        },
    },
    {
        "scenario": "bench06_bbh_clock_unlocked",
        "case_id": "b06_bbh_clock_unlocked",
        "title": "BBH PTP 时钟失锁导致小区激活超时",
        "fail_kind": "env",
        "root_component": "bbh",
        "root_cause": "BBH PTP reference 失锁且偏移超过 TDD 容限",
        "expected_evidence_keys": ["E-BBH-2101", "ptp_state=UNLOCKED", "offset_ns=4870"],
        "user_input": "BBL 报 cell activation timeout，看看是不是 BBH 时钟导致。",
        "pipeline": pipeline("NR_BENCH_006_PTP_UNLOCK", host=65),
        "events": {
            "bbh": [
                event(111500, "DEBUG", "clock_sample ptp_state=HOLDOVER offset_ns=1310 grandmaster=00:11:22:ff:fe:33:44:55"),
                event(112000, "ERROR", "CLOCK_REFERENCE_UNLOCKED errorcode=E-BBH-2101 ptp_state=UNLOCKED offset_ns=4870 holdover_ms=32000"),
            ],
            "bbl": [
                event(113000, "DEBUG", "cell_state cell=NR_CELL_0 CONFIGURED->ACTIVATING wait=slot_indication"),
                event(116000, "ERROR", "CELL_ACTIVATION_TIMEOUT errorcode=E-BBL-3112 cell=NR_CELL_0 probable_component=bbh last_state=ACTIVATING"),
            ],
        },
    },
    {
        "scenario": "bench07_bbh_fronthaul_sequence_gap",
        "case_id": "b07_bbh_fronthaul_sequence_gap",
        "title": "前传 eCPRI 序列断裂",
        "fail_kind": "env",
        "root_component": "bbh",
        "root_cause": "前传链路丢包/乱序造成 eCPRI 序列号不连续",
        "expected_evidence_keys": ["E-BBH-2407", "expected_seq=18421", "received_seq=18429"],
        "user_input": "小区起来后 KPI 很差，检查 BBH 前传链路。",
        "pipeline": pipeline("NR_BENCH_007_ECPRI_SEQ_GAP", host=66),
        "events": {
            "bbh": [
                event(112000, "DEBUG", "ecpri_rx flow=7 seq=18420 packet_status=ON_TIME"),
                event(112100, "ERROR", "FRONTHAUL_SEQUENCE_DISCONTINUITY errorcode=E-BBH-2407 flow=7 expected_seq=18421 received_seq=18429 gap=8"),
                event(112300, "WARN", "rx_symbol_drop flow=7 symbols=112 reorder_window_exceeded=true"),
            ],
            "bbl": [event(113000, "WARN", "slot_indication gap_detected=8 cell=NR_CELL_0 link_state=DEGRADED")],
            "compare": [event(115000, "ERROR", "KPI_OUT_OF_TOLERANCE errorcode=E-CMP-5102 metric=dl_bler actual=0.184 baseline=0.012 probable_component=bbh cascade=true")],
        },
    },
    {
        "scenario": "bench08_rx_subscription_debug_stall",
        "case_id": "b08_rx_subscription_debug_stall",
        "title": "BBH/BBL RX 订阅握手卡住，仅 DEBUG 重试",
        "fail_kind": "env",
        "root_component": "bbh",
        "root_cause": "BBH 到 BBL 的传输路径中断，RX 订阅请求无 ACK",
        "expected_evidence_keys": ["direction=RX", "attempt=12", "subscription_ack_missing"],
        "user_input": "没有明显 ERROR，但 cell load fail，检查 BBH/BBL 的 RX 订阅是否互通。",
        "pipeline": pipeline("NR_BENCH_008_RX_SUB_STALL", host=67),
        "events": {
            "bbh": [
                event(111000, "DEBUG", "request_subscribe direction=RX peer=bbl topic=cell0.rx attempt=9 link_state=CONNECTING"),
                event(112000, "DEBUG", "request_subscribe direction=RX peer=bbl topic=cell0.rx attempt=10 link_state=CONNECTING"),
                event(113000, "DEBUG", "request_subscribe direction=RX peer=bbl topic=cell0.rx attempt=11 link_state=CONNECTING"),
                event(114000, "DEBUG", "request_subscribe direction=RX peer=bbl topic=cell0.rx attempt=12 link_state=CONNECTING subscription_ack_missing=true"),
            ],
            "bbl": [
                event(111500, "DEBUG", "subscription_table direction=RX peer=bbh topic=cell0.rx state=EMPTY"),
                event(114500, "DEBUG", "cell_state cell=NR_CELL_0 state=CONFIGURING wait_for=rx_subscription elapsed_ms=12000"),
            ],
            "comm": [event(114800, "DEBUG", "peer_channel bbh<->bbl packets_rx=0 reconnects=12 socket_state=SYN_SENT")],
            "compare": [event(116000, "INFO", "verdict=NOT_RUN reason=CELL_LOAD_INCOMPLETE")],
        },
    },
    {
        "scenario": "bench09_tx_publication_debug_stall",
        "case_id": "b09_tx_publication_debug_stall",
        "title": "BBL TX 发布存在但 BBH 订阅 ACK 丢失，仅 DEBUG 异常",
        "fail_kind": "env",
        "root_component": "bbl",
        "root_cause": "BBL TX topic 路由未安装，双方订阅状态不对称",
        "expected_evidence_keys": ["direction=TX", "route_state=WAITING", "subscriber_count=0"],
        "user_input": "BBH/BBL 没有 errorcode，但 TX 方向一直连不上，分析 debug 行为。",
        "pipeline": pipeline("NR_BENCH_009_TX_PUB_STALL", host=68),
        "events": {
            "bbl": [
                event(111000, "DEBUG", "publish_announce direction=TX topic=cell0.tx generation=44 subscriber_count=0 route_state=WAITING"),
                event(112000, "DEBUG", "publish_announce direction=TX topic=cell0.tx generation=45 subscriber_count=0 route_state=WAITING"),
                event(113000, "DEBUG", "publish_announce direction=TX topic=cell0.tx generation=46 subscriber_count=0 route_state=WAITING"),
                event(115000, "DEBUG", "cell_state cell=NR_CELL_0 state=CONFIGURING wait_for=tx_subscriber elapsed_ms=15000"),
            ],
            "bbh": [
                event(111500, "DEBUG", "request_subscribe direction=TX peer=bbl topic=cell0.tx attempt=7 ack=false"),
                event(113500, "DEBUG", "request_subscribe direction=TX peer=bbl topic=cell0.tx attempt=9 ack=false"),
            ],
            "comm": [event(114000, "DEBUG", "route_lookup topic=cell0.tx result=MISS owner=bbl route_revision=43")],
        },
    },
    {
        "scenario": "bench10_bbl_duplicate_pci",
        "case_id": "b10_bbl_duplicate_pci",
        "title": "BBL 检测到重复 PCI",
        "fail_kind": "case",
        "root_component": "bbl",
        "root_cause": "多小区用例给两个相邻小区配置了同一 PCI",
        "expected_evidence_keys": ["E-BBL-3120", "pci=101", "cells=[NR_CELL_0,NR_CELL_1]"],
        "user_input": "两个小区都建立失败，看看是否是 PCI 配置冲突。",
        "pipeline": pipeline("NR_BENCH_010_DUPLICATE_PCI", host=69),
        "events": {
            "rat": [event(111000, "INFO", "state=READY configured_cells=2")],
            "bbl": [
                event(112000, "DEBUG", "validate_cell_identity cell=NR_CELL_0 pci=101 arfcn=633984"),
                event(112100, "DEBUG", "validate_cell_identity cell=NR_CELL_1 pci=101 arfcn=633984"),
                event(112300, "ERROR", "DUPLICATE_PCI errorcode=E-BBL-3120 pci=101 cells=[NR_CELL_0,NR_CELL_1] collision_domain=sector-7"),
            ],
            "compare": [event(114000, "INFO", "verdict=NOT_RUN reason=CELL_CONFIG_REJECTED")],
        },
    },
    {
        "scenario": "bench11_bbl_context_pool_exhausted",
        "case_id": "b11_bbl_context_pool_exhausted",
        "title": "BBL 小区上下文池耗尽",
        "fail_kind": "env",
        "root_component": "bbl",
        "root_cause": "前序任务未释放 cell context，运行时资源池耗尽",
        "expected_evidence_keys": ["E-BBL-3209", "used=256", "leaked_candidates=18"],
        "user_input": "同样用例换环境能过，本环境 cell context 分配失败。",
        "pipeline": pipeline("NR_BENCH_011_CONTEXT_POOL", host=70),
        "events": {
            "bbl": [
                event(111000, "DEBUG", "cell_context_pool capacity=256 used=256 free=0 high_watermark=256"),
                event(112000, "DEBUG", "context_gc scanned=256 releasable=0 leaked_candidates=18 oldest_age_s=8642"),
                event(112300, "ERROR", "CELL_CONTEXT_POOL_EXHAUSTED errorcode=E-BBL-3209 requested=1 used=256 capacity=256 leaked_candidates=18"),
            ],
            "compare": [event(114000, "INFO", "verdict=NOT_RUN reason=CELL_CONTEXT_ALLOC_FAILED")],
        },
    },
    {
        "scenario": "bench12_vue_antenna_map_missing",
        "case_id": "b12_vue_antenna_map_missing",
        "title": "VUE 小区因 MARP 天线映射缺失而失败",
        "fail_kind": "case",
        "root_component": "marp",
        "root_cause": "用例模板缺少 antenna_map，MARP 无法绑定 RF 端口",
        "expected_evidence_keys": ["E-MARP-4106", "antenna_map", "probable_component=marp"],
        "user_input": "VUE_CELL_SETUP_FAILED，结合 MARP 和 BBL 错误码找根因。",
        "pipeline": pipeline("NR_BENCH_012_VUE_ANTENNA_MAP", host=71),
        "events": {
            "marp": [
                event(111500, "DEBUG", "load_rf_profile profile=vue_4t4r required_field=antenna_map"),
                event(112000, "ERROR", "ANTENNA_MAP_MISSING errorcode=E-MARP-4106 profile=vue_4t4r field=antenna_map rf_ports=[0,1,2,3]"),
            ],
            "bbl": [event(113000, "ERROR", "VUE_CELL_SETUP_FAILED cell=VUE_CELL_0 errorcode=E-BBL-3108 probable_component=marp cause_chain=ANTENNA_MAP_MISSING cascade=true")],
            "compare": [event(115000, "INFO", "verdict=NOT_RUN reason=VUE_CELL_NOT_READY")],
        },
    },
    {
        "scenario": "bench13_vue_beam_calibration_mismatch",
        "case_id": "b13_vue_beam_calibration_mismatch",
        "title": "VUE 波束校准版本不匹配",
        "fail_kind": "version",
        "root_component": "marp",
        "root_cause": "活动 RF profile 与波束校准表 revision 不兼容",
        "expected_evidence_keys": ["E-MARP-4114", "profile_revision=27B-r12", "calibration_revision=26A-r8"],
        "user_input": "VUE 建立失败，判断是 BBL 现象还是 MARP 校准版本根因。",
        "pipeline": pipeline("NR_BENCH_013_VUE_CALIBRATION", version="27B", host=72),
        "events": {
            "marp": [
                event(111500, "DEBUG", "calibration_check profile_revision=27B-r12 calibration_revision=26A-r8 branch_count=64"),
                event(112000, "ERROR", "BEAM_CALIBRATION_MISMATCH errorcode=E-MARP-4114 profile_revision=27B-r12 calibration_revision=26A-r8 max_phase_delta_deg=31.4"),
            ],
            "bbl": [event(113000, "ERROR", "VUE_CELL_SETUP_FAILED cell=VUE_CELL_0 errorcode=E-BBL-3108 probable_component=marp cause_chain=BEAM_CALIBRATION_MISMATCH cascade=true")],
        },
    },
    {
        "scenario": "bench14_cell_load_cascade_from_rat",
        "case_id": "b14_cell_load_cascade_from_rat",
        "title": "RAT numerology 不合法引发 BBH/BBL 级联",
        "fail_kind": "case",
        "root_component": "rat",
        "root_cause": "用例的 SCS 与频段/带宽 profile 组合不受支持",
        "expected_evidence_keys": ["field=scs_khz", "value=15", "probable_component=rat"],
        "user_input": "BBH 和 BBL 都报 errorcode，沿 cause chain 找最早根因。",
        "pipeline": pipeline("NR_BENCH_014_RAT_SCS_CASCADE", host=73),
        "events": {
            "rat": [event(111500, "ERROR", "RAT_PROFILE_REJECTED errorcode=E-RAT-1211 field=scs_khz value=15 allowed=[30,60] band=n78 bandwidth_mhz=100")],
            "bbh": [event(112500, "ERROR", "UPSTREAM_COMPONENT_NOT_READY errorcode=E-BBH-2991 dependency=RAT probable_component=rat cascade=true")],
            "bbl": [event(113500, "ERROR", "CELL_SETUP_DEPENDENCY_FAILED errorcode=E-BBL-3104 dependency=BBH probable_component=rat cascade=true")],
        },
    },
    {
        "scenario": "bench15_cell_activation_timeout_clock",
        "case_id": "b15_cell_activation_timeout_clock",
        "title": "时钟进入长时间 holdover，BBL 激活超时",
        "fail_kind": "env",
        "root_component": "bbh",
        "root_cause": "SyncE/主时钟源丢失后 holdover 超限，slot indication 未产生",
        "expected_evidence_keys": ["holdover_ms=65000", "source=SyncE", "E-BBL-3112"],
        "user_input": "BBL 最后一条是 activation timeout，向前检查 BBH 时钟状态。",
        "pipeline": pipeline("NR_BENCH_015_CLOCK_HOLDOVER", host=74),
        "events": {
            "bbh": [
                event(111000, "DEBUG", "clock_source source=SyncE state=LOS quality=QL-FAILED"),
                event(112000, "ERROR", "CLOCK_REFERENCE_UNLOCKED errorcode=E-BBH-2101 source=SyncE ptp_state=HOLDOVER holdover_ms=65000 offset_ns=2900"),
            ],
            "bbl": [
                event(113000, "DEBUG", "cell_start_request cell=NR_CELL_0 request_id=884 accepted=true"),
                event(116000, "ERROR", "CELL_ACTIVATION_TIMEOUT errorcode=E-BBL-3112 request_id=884 wait_for=first_slot_indication probable_component=bbh"),
            ],
        },
    },
    {
        "scenario": "bench16_rach_timing_out_of_window",
        "case_id": "b16_rach_timing_out_of_window",
        "title": "PRACH 到达时间超窗",
        "fail_kind": "env",
        "root_component": "bbh",
        "root_cause": "BBH 上行时间对齐偏差导致 PRACH 检测窗口错位",
        "expected_evidence_keys": ["E-RAT-1231", "measured_ta_samples=418", "accepted=[-96,96]"],
        "user_input": "小区已经 UP，但 UE 随机接入反复失败，检查 PRACH timing。",
        "pipeline": pipeline("NR_BENCH_016_PRACH_TIMING", host=75),
        "events": {
            "bbl": [event(111000, "INFO", "cell_state cell=NR_CELL_0 state=ACTIVE first_slot=1023.9")],
            "bbh": [event(112000, "DEBUG", "ul_time_alignment flow=3 measured_ta_samples=418 calibration_offset=401")],
            "rat": [
                event(112500, "DEBUG", "prach_detect occasion=884 preamble=23 peak=0.82 measured_ta_samples=418 accepted=[-96,96]"),
                event(113000, "ERROR", "PRACH_TIMING_OUT_OF_WINDOW errorcode=E-RAT-1231 measured_ta_samples=418 accepted=[-96,96] probable_component=bbh"),
            ],
            "compare": [event(115000, "ERROR", "KPI_OUT_OF_TOLERANCE errorcode=E-CMP-5102 metric=rach_success_rate actual=0.00 baseline=0.99 cascade=true")],
        },
    },
    {
        "scenario": "bench17_rrc_setup_incomplete",
        "case_id": "b17_rrc_setup_incomplete",
        "title": "RACH 成功但 RRCSetupComplete 未返回",
        "fail_kind": "env",
        "root_component": "bbl",
        "root_cause": "UE/空口侧未回 RRCSetupComplete，网络侧只能确认 RRC 建链未闭环",
        "expected_evidence_keys": ["RACH_COMPLETE", "RRCSetup sent", "E-BBL-3306"],
        "user_input": "随机接入成功了但 UE 还是没接入，分析 RRC 阶段。",
        "pipeline": pipeline("NR_BENCH_017_RRC_INCOMPLETE", host=76),
        "events": {
            "rat": [event(111000, "INFO", "RACH_COMPLETE cell=NR_CELL_0 rnti=0x4601 preamble=11 msg3_crc=OK")],
            "bbl": [
                event(112000, "INFO", "RRCSetup sent cell=NR_CELL_0 rnti=0x4601 transaction_id=2 srb0_bytes=164"),
                event(114000, "DEBUG", "rrc_transaction transaction_id=2 state=WAIT_SETUP_COMPLETE elapsed_ms=2000 ul_ccch_rx=0"),
                event(116000, "ERROR", "RRC_SETUP_INCOMPLETE errorcode=E-BBL-3306 rnti=0x4601 transaction_id=2 timeout_ms=4000 probable_component=ue_or_radio_link"),
            ],
            "compare": [event(117000, "INFO", "verdict=NOT_RUN reason=UE_NOT_CONNECTED")],
        },
    },
    {
        "scenario": "bench18_compare_schema_mismatch",
        "case_id": "b18_compare_schema_mismatch",
        "title": "Compare 基线 schema 与结果版本不兼容",
        "fail_kind": "version",
        "root_component": "compare",
        "root_cause": "27B 输出 schema v5，仍使用 26A 的 baseline schema v4",
        "expected_evidence_keys": ["E-CMP-5110", "result_schema=v5", "baseline_schema=v4"],
        "user_input": "无线侧都通过，为什么 compare 阶段仍然失败？",
        "pipeline": pipeline("NR_BENCH_018_COMPARE_SCHEMA", version="27B", host=77),
        "events": {
            "bbl": [event(111000, "INFO", "cell_state cell=NR_CELL_0 state=ACTIVE")],
            "compare": [
                event(112000, "DEBUG", "load_result artifact=kpi.json result_schema=v5 producer_version=27B"),
                event(112100, "DEBUG", "load_baseline artifact=baseline_26A.json baseline_schema=v4 producer_version=26A"),
                event(112500, "ERROR", "BASELINE_SCHEMA_MISMATCH errorcode=E-CMP-5110 result_schema=v5 baseline_schema=v4 incompatible_fields=[bler_window,beam_id]"),
            ],
        },
    },
    {
        "scenario": "bench19_compare_kpi_from_fronthaul",
        "case_id": "b19_compare_kpi_from_fronthaul",
        "title": "Compare KPI 超差，根因在 BBH 前传丢包",
        "fail_kind": "env",
        "root_component": "bbh",
        "root_cause": "BBH 前传突发丢包导致下行 BLER 回归，Compare 只是最终判定者",
        "expected_evidence_keys": ["packet_loss_burst=47", "probable_component=bbh", "dl_bler"],
        "user_input": "compare 报 KPI fail，判断是阈值问题还是上游链路故障。",
        "pipeline": pipeline("NR_BENCH_019_KPI_FRONTHAUL", host=78),
        "events": {
            "bbh": [
                event(111500, "ERROR", "FRONTHAUL_SEQUENCE_DISCONTINUITY errorcode=E-BBH-2407 flow=9 packet_loss_burst=47 expected_seq=9012 received_seq=9059"),
                event(112000, "WARN", "dl_symbol_concealment flow=9 concealed_symbols=658"),
            ],
            "bbl": [event(113000, "DEBUG", "harq_summary cell=NR_CELL_0 ack=148 nack=39 dtx=8")],
            "compare": [event(115000, "ERROR", "KPI_OUT_OF_TOLERANCE errorcode=E-CMP-5102 metric=dl_bler actual=0.209 baseline=0.011 tolerance=0.020 probable_component=bbh cascade=true")],
        },
    },
    {
        "scenario": "bench20_transient_subscription_recovered",
        "case_id": "b20_transient_subscription_recovered",
        "title": "订阅短暂重试后恢复，健康对照组",
        "fail_kind": "none",
        "root_component": "none",
        "root_cause": "无故障；BBH/BBL 双向订阅在重试窗口内完成",
        "expected_evidence_keys": ["subscription_state=ESTABLISHED", "cell_state=ACTIVE", "verdict=pass"],
        "user_input": "日志有订阅重试，确认这是可恢复瞬态还是环境故障。",
        "pipeline": pipeline("NR_BENCH_020_TRANSIENT_RECOVERY", host=79),
        "events": {
            "bbh": [
                event(111000, "DEBUG", "request_subscribe direction=RX peer=bbl topic=cell0.rx attempt=1 ack=false"),
                event(111500, "WARN", "subscription retry direction=RX peer=bbl backoff_ms=500 transient=true"),
                event(112000, "DEBUG", "subscribe_ack direction=RX peer=bbl topic=cell0.rx attempt=2 subscription_state=ESTABLISHED"),
                event(112500, "DEBUG", "subscribe_ack direction=TX peer=bbl topic=cell0.tx attempt=1 subscription_state=ESTABLISHED"),
            ],
            "bbl": [
                event(113000, "INFO", "subscription_matrix RX=ESTABLISHED TX=ESTABLISHED peer=bbh"),
                event(114000, "INFO", "cell=NR_CELL_0 cell_state=ACTIVE first_slot=205.4"),
            ],
            "compare": [event(116000, "INFO", "case_finished verdict=pass kpi_status=WITHIN_TOLERANCE")],
        },
    },
]


BACKGROUND_MESSAGES: dict[str, tuple[str, ...]] = {
    "comm": (
        "control_heartbeat peer=orchestrator state=READY rtt_ms={metric}",
        "channel_poll endpoint=10.10.7.21:19090 state=ESTABLISHED rx_queue={small}",
        "route_cache lookup=cell-service result=HIT revision={revision}",
        "keepalive_ack peer=rat sequence={seq} age_ms={metric}",
    ),
    "rat": (
        "worker_heartbeat worker=scheduler-{small} state=RUNNING queue_depth={metric}",
        "slot_plan cell=NR_CELL_0 sfn={sfn} slot={slot} status=READY",
        "profile_cache band=n78 scs_khz=30 revision={revision} hit=true",
        "resource_sample dl_pool={metric} ul_pool={metric2} state=NORMAL",
    ),
    "bbh": (
        "ecpri_counter flow={small} rx_on_time={seq} rx_late=0 rx_drop=0",
        "clock_sample ptp_state=LOCKED offset_ns={metric} source=PTP",
        "subscription_health peer=bbl RX=ESTABLISHED TX=ESTABLISHED generation={revision}",
        "symbol_ring direction=RX used={metric} capacity=4096 overrun=0",
    ),
    "bbl": (
        "cell_tick cell=NR_CELL_0 state=ACTIVE sfn={sfn} slot={slot}",
        "subscription_health peer=bbh RX=ESTABLISHED TX=ESTABLISHED generation={revision}",
        "harq_window cell=NR_CELL_0 ack={seq} nack={small} dtx=0",
        "context_pool used={metric} capacity=256 gc_pending=0",
    ),
    "marp": (
        "beam_weight_refresh cell=NR_CELL_0 beam={small} revision={revision} status=OK",
        "rf_path_sample branch={small} gain_db=31.{metric} phase_delta_deg=0.{metric2}",
        "antenna_binding profile=default_4t4r mapped=4 missing=0",
        "calibration_cache revision=27B-r12 hit=true checksum=OK",
    ),
    "compare": (
        "kpi_sample metric=dl_bler value=0.0{small} window={revision}",
        "baseline_lookup case=active schema=v5 result=HIT revision={revision}",
        "sample_align source=du_metrics timestamp_delta_ms={metric} status=ALIGNED",
        "assertion_queue pending={small} completed={seq} state=RUNNING",
    ),
}


def timestamp(offset_ms: int) -> str:
    value = BASE_TIME + timedelta(milliseconds=offset_ms)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")


def parse_event(spec: str) -> tuple[int, str, str]:
    offset, level, message = spec.split("|", 2)
    return int(offset), level, message


def format_line(component: str, offset_ms: int, level: str, message: str, *, thread: str) -> str:
    return (
        f"{timestamp(offset_ms)} [{component.upper():<7}] [{level:<5}] "
        f"[thread={thread}] pipeline_id={PIPELINE_PLACEHOLDER} {message}"
    )


def render_component(scenario_index: int, scenario: dict[str, Any], component: str) -> str:
    lines = [
        format_line(component, 0, "INFO", f"process_boot component={component} build=mock-27B", thread="main"),
        format_line(component, 5, "INFO", f"scenario={scenario['scenario']} synthetic=true", thread="main"),
        format_line(component, 10, "DEBUG", "log_level=DEBUG structured_fields=enabled", thread="main"),
    ]
    patterns = BACKGROUND_MESSAGES[component]
    for i in range(BACKGROUND_LINES_PER_COMPONENT):
        offset_ms = 100 + i * 100
        message = patterns[(i + scenario_index) % len(patterns)].format(
            metric=(i * 7 + scenario_index * 11) % 97,
            metric2=(i * 13 + scenario_index * 3) % 89,
            small=(i + scenario_index) % 10,
            revision=100 + (i // 25),
            seq=10000 + i,
            sfn=(i // 10) % 1024,
            slot=i % 20,
        )
        level = "INFO" if i % 17 == 0 else "DEBUG"
        lines.append(format_line(component, offset_ms, level, message, thread=f"{component}-{i % 4}"))

    for spec in sorted(scenario.get("events", {}).get(component, []), key=lambda item: parse_event(item)[0]):
        offset_ms, level, message = parse_event(spec)
        lines.append(format_line(component, offset_ms, level, message, thread=f"{component}-fault"))

    lines.append(
        format_line(
            component,
            119000,
            "DEBUG",
            f"diagnostic_snapshot component={component} flush=complete",
            thread="main",
        )
    )
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def generate() -> dict[str, int]:
    if len(SCENARIOS) != 20:
        raise ValueError(f"benchmark 必须恰好 20 组，实际 {len(SCENARIOS)}")
    names = [str(item["scenario"]) for item in SCENARIOS]
    if len(names) != len(set(names)):
        raise ValueError("scenario 名称重复")

    total_lines = 0
    total_bytes = 0
    for index, scenario in enumerate(SCENARIOS, start=1):
        scenario_dir = LOG_ROOT / str(scenario["scenario"])
        scenario_dir.mkdir(parents=True, exist_ok=True)
        for component in COMPONENTS:
            content = render_component(index, scenario, component)
            target = scenario_dir / f"{component}.log"
            target.write_text(content, encoding="utf-8", newline="\n")
            total_lines += content.count("\n")
            total_bytes += len(content.encode("utf-8"))

    write_json(
        DATASET_PATH,
        {
            "suite": "baseband_layered_benchmark_v1",
            "notice": "Synthetic vendor-neutral benchmark. Error codes and log lines are invented for this repository.",
            "components": list(COMPONENTS),
            "background_lines_per_component": BACKGROUND_LINES_PER_COMPONENT,
            "cases": SCENARIOS,
        },
    )
    write_json(
        EVAL_PATH,
        {
            "suite": "baseband_layered_benchmark_v1",
            "note": "20 组分组件 synthetic 基带故障 golden set；expected_evidence_keys 必须存活到最终 working context。",
            "cases": [
                {
                    "case_id": item["case_id"],
                    "scenario": item["scenario"],
                    "user_input": item["user_input"],
                    "pipeline": item["pipeline"],
                    "expected_fail_kind": item["fail_kind"],
                    "expected_root_component": item["root_component"],
                    "expected_evidence_keys": item["expected_evidence_keys"],
                }
                for item in SCENARIOS
            ],
        },
    )
    return {
        "scenarios": len(SCENARIOS),
        "files": len(SCENARIOS) * len(COMPONENTS),
        "lines": total_lines,
        "bytes": total_bytes,
    }


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
