import os
import subprocess
import re
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

from app.services.sensor import (
    _run_sysinfo, collect_smc_pwr, parse_smc_pwr, _parse_thermal
)


VECTOR_GROUP_SCRIPTS = [
    "run_bert_large.sh",
    "run_retinanet.sh",
    "run_resnet50_ss.sh",
    "run_resnet50_ms.sh"
]


def _read_output_file(path):
    """출력 파일을 안전하게 읽기"""
    if not os.path.exists(path):
        return ""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def run_workload_background(job_id, workload_path, exec_time, is_script, job_manager, cfg):
    """백그라운드에서 워크로드 실행"""
    job = job_manager.get_job(job_id)
    workload_dir = job['workload_dir']
    output_file = job['output_file']

    try:
        if is_script:
            cmd_parts = ['bash', workload_path, str(exec_time)]
        else:
            # .bin 워크로드는 기존 방식으로 rblntrace 직접 실행
            cmd_parts = [
                'bash', '-c',
                f'cd {workload_dir} && '
                f'rblntrace retrace --get_perf=2 --infer_idle_time_us=0 "{workload_path}" -e{exec_time}'
            ]

        with open(output_file, 'w') as outf:
            process = subprocess.Popen(
                cmd_parts,
                stdout=outf,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True
            )
            job_manager.jobs[job_id]['process'] = process

        # 프로세스 실행 모니터링
        monitor_workload_progress(job_id, output_file, exec_time, job_manager, cfg)
        return_code = process.wait()

        output = _read_output_file(output_file)
        latest_job = job_manager.get_job(job_id)

        if latest_job and latest_job.get('status') == 'cancelled':
            job_manager.update_job_status(job_id, 'cancelled', result={'output': output})
        elif return_code == 0:
            job_manager.update_job_status(job_id, 'completed', result={'output': output})
        else:
            job_manager.update_job_status(
                job_id,
                'error',
                result={
                    'error': f'프로세스 종료 코드: {return_code}',
                    'output': output
                }
            )

        # 로그 저장
        final_job = job_manager.get_job(job_id)
        if final_job and final_job.get('save_log'):
            try:
                saved_path = _save_log_file(final_job)
                with job_manager.lock:
                    if job_id in job_manager.jobs:
                        job_manager.jobs[job_id]['log_saved_path'] = saved_path
            except Exception as e:
                print(f"Log save failed: {e}")

    except Exception as e:
        job_manager.update_job_status(
            job_id,
            'error',
            result={'error': str(e)}
        )


def _collect_sample(cfg):
    """현재 시점의 power rails + thermal 샘플 수집"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    sample = {}
    sample['timestamp'] = timestamp

    # Power rails
    raw_pwr = collect_smc_pwr(cfg.SUDO_CMD, cfg.SUDO_INPUT)
    if raw_pwr:
        rails = parse_smc_pwr(raw_pwr)
        sample['rails'] = {r['name']: {'voltage_mv': r['voltage_mv'], 'current_ma': r['current_ma'], 'power_w': r['power_w']} for r in rails}

    # Thermal
    thermal_data = _run_sysinfo('thermal', cfg.SUDO_CMD, cfg.SUDO_INPUT)
    thermal = _parse_thermal(thermal_data)
    sample['npu_temp_c'] = thermal['temperature']
    sample['dram_temp_c'] = thermal['dram_temp']
    sample['throttling'] = thermal['throttling']

    return sample


def _save_log_file(job):
    """Job 완료 후 수집된 샘플을 JSON 로그로 저장"""
    log_dir = job['log_path']
    os.makedirs(log_dir, exist_ok=True)

    workload_name = os.path.splitext(job['workload'])[0] if '.' in job['workload'] else job['workload']
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{ts}_{job['workload']}_{job['exec_time']}s.json"
    filepath = os.path.join(log_dir, filename)

    samples = job.get('_samples', [])

    # power_rails / thermal 시계열 분리 (기존 로그 포맷 호환)
    power_rails = []
    thermal = []
    for s in samples:
        label = s.get('label', 'RUN')
        entry_pwr = {'label': label, 'timestamp': s['timestamp'], 'rails': s.get('rails', {})}
        entry_thm = {
            'label': label, 'timestamp': s['timestamp'],
            'npu_temp_c': s.get('npu_temp_c'),
            'dram_temp_c': s.get('dram_temp_c', []),
            'throttling': s.get('throttling', False)
        }
        power_rails.append(entry_pwr)
        thermal.append(entry_thm)

    # Summary 계산
    power_rails_summary = {}
    for entry in power_rails:
        for rail_name, vals in entry.get('rails', {}).items():
            if rail_name not in power_rails_summary:
                power_rails_summary[rail_name] = {'powers': [], 'voltages': [], 'currents': []}
            if vals.get('power_w') is not None:
                power_rails_summary[rail_name]['powers'].append(vals['power_w'])
            if vals.get('voltage_mv') is not None:
                power_rails_summary[rail_name]['voltages'].append(vals['voltage_mv'])
            if vals.get('current_ma') is not None:
                power_rails_summary[rail_name]['currents'].append(vals['current_ma'])

    summary = {}
    for rail_name, data in power_rails_summary.items():
        s = {}
        if data['powers']:
            s['avg_power_w'] = round(sum(data['powers']) / len(data['powers']), 2)
            s['peak_power_w'] = round(max(data['powers']), 2)
        if data['voltages']:
            s['peak_voltage_mv'] = max(data['voltages'])
        if data['currents']:
            s['peak_current_ma'] = max(data['currents'])
        summary[rail_name] = s

    thermal_summary = {}
    npu_temps = [t['npu_temp_c'] for t in thermal if t.get('npu_temp_c') is not None]
    if npu_temps:
        thermal_summary['avg_temp_c'] = round(sum(npu_temps) / len(npu_temps), 1)
        thermal_summary['peak_temp_c'] = max(npu_temps)
    dram_peaks = [max(t['dram_temp_c']) for t in thermal if t.get('dram_temp_c')]
    if dram_peaks:
        thermal_summary['dram_max_temp_c'] = max(dram_peaks)

    output = job.get('result', {}).get('output', '') if job.get('result') else ''

    log_data = {
        'workload': {
            'test_vector': job['workload'],
            'execution_time_s': job['exec_time'],
            'start_time': job['start_time'],
            'end_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
        'power_rails': power_rails,
        'thermal': thermal,
        'power_rails_summary': summary,
        'thermal_summary': thermal_summary,
        'workload_output': output
    }

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)

    return filepath


def monitor_workload_progress(job_id, output_file, exec_time, job_manager, cfg):
    """워크로드 진행 상황 모니터링 + 센서 데이터 수집"""
    start_time = time.time()
    running_started = False
    running_start_time = None
    perf_pattern = re.compile(r'perf\(us\)', re.IGNORECASE)
    samples = []
    last_sample_time = 0
    sample_executor = ThreadPoolExecutor(max_workers=1)
    pending_sample_future = None

    job = job_manager.get_job(job_id)
    pre_sample = _collect_sample(cfg)
    pre_sample['label'] = 'PRE'
    samples.append(pre_sample)

    while True:
        try:
            job = job_manager.get_job(job_id)
            if not job:
                break

            if job.get('status') == 'cancelled':
                break

            process = job.get('process')
            if process is None:
                break

            content = _read_output_file(output_file)
            now = time.time()

            if (not running_started) and perf_pattern.search(content):
                running_started = True
                running_start_time = now
                job_manager.update_job_status(
                    job_id,
                    'running',
                    progress={
                        'stage': 'running',
                        'elapsed': 0,
                        'percentage': 0
                    }
                )

            # 센서 수집을 별도 스레드에서 비동기로 처리
            if running_started and (now - last_sample_time >= 1.0):
                if pending_sample_future and pending_sample_future.done():
                    try:
                        result_sample = pending_sample_future.result()
                        result_sample['label'] = 'RUN'
                        samples.append(result_sample)
                    except Exception:
                        pass
                    pending_sample_future = None
                if not pending_sample_future:
                    pending_sample_future = sample_executor.submit(_collect_sample, cfg)
                    last_sample_time = now

            return_code = process.poll()

            if return_code is not None:
                current_status = job_manager.get_job(job_id)
                if current_status and current_status.get('status') != 'cancelled':
                    if running_started and running_start_time is not None:
                        elapsed = max(0, int(now - running_start_time))
                        job_manager.update_job_status(
                            job_id,
                            'running',
                            progress={
                                'stage': 'running',
                                'elapsed': elapsed,
                                'percentage': 100
                            }
                        )
                    else:
                        prep_elapsed = int(now - start_time)
                        job_manager.update_job_status(
                            job_id,
                            'preparing',
                            progress={
                                'stage': 'preparing',
                                'elapsed': prep_elapsed,
                                'percentage': 0
                            }
                        )
                break

            if running_started and running_start_time is not None:
                elapsed = int(now - running_start_time)
                percentage = min(99, int(elapsed * 100 / max(exec_time, 1)))
                job_manager.update_job_status(
                    job_id,
                    'running',
                    progress={
                        'stage': 'running',
                        'elapsed': elapsed,
                        'percentage': percentage
                    }
                )
            else:
                prep_elapsed = int(now - start_time)
                job_manager.update_job_status(
                    job_id,
                    'preparing',
                    progress={
                        'stage': 'preparing',
                        'elapsed': prep_elapsed,
                        'percentage': 0
                    }
                )

            time.sleep(0.5)

        except Exception as e:
            print(f"Error monitoring progress: {e}")
            time.sleep(1)

    # 남은 pending sample 수집
    if pending_sample_future:
        try:
            result_sample = pending_sample_future.result(timeout=3)
            result_sample['label'] = 'RUN'
            samples.append(result_sample)
        except Exception:
            pass
    sample_executor.shutdown(wait=False)

    if samples:
        with job_manager.lock:
            if job_id in job_manager.jobs:
                job_manager.jobs[job_id]['_samples'] = samples
