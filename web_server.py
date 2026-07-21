#!/usr/bin/env python3
"""
워크로드 실행 시스템 - Flask 웹 서버
"""

from flask import Flask, render_template, jsonify, request, send_file
import os
import glob
import subprocess
import threading
import json
import re
import time
import signal
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

_PROJECT_ROOT = Path(__file__).parent

def _load_env_file(path: Path):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_env_file(_PROJECT_ROOT / '.env')

app = Flask(__name__)

# 기본 워크로드 폴더 경로
DEFAULT_WORKLOAD_DIR = "/home/rebellions/yeun/testvector/cr13/v3.2.0"
JOBS_DIR = "/home/rebellions/yeun/jobs"
RUN_SCRIPT = "/home/rebellions/yeun/run_workloads.sh"
VECTOR_GROUP_SCRIPTS = [
    "run_bert_large.sh",
    "run_retinanet.sh",
    "run_resnet50_ss.sh",
    "run_resnet50_ms.sh"
]

VECTOR_GROUPS = {
    'bert_large': ['bert_large', 'bert_chiplet', 'rebel_bert_chiplet'],
    'retinanet': ['retinanet', 'retinanet_chiplet', 'rebel_retinanet_chiplet'],
    'resnet50_ss': ['resnet50_ss', 'resnet50_ss_chiplet', 'rebel_resnet50_ss_chiplet'],
    'resnet50_ms': ['resnet50_ms', 'resnet50_ms_chiplet', 'rebel_resnet50_ms_chiplet'],
}


def _find_group_files(workload_dir, group_name):
    patterns = VECTOR_GROUPS.get(group_name)
    if not patterns:
        return None, f'지원하지 않는 그룹: {group_name}'

    files = {}
    missing = []
    for idx in range(4):
        found = None
        for pattern in patterns:
            for candidate_name in [f'{pattern}_{idx}_0.bin', f'*_{pattern}_{idx}_0.bin']:
                matches = glob.glob(os.path.join(workload_dir, candidate_name))
                if matches:
                    matches.sort(key=os.path.getmtime, reverse=True)
                    found = matches[0]
                    break
            if found:
                break
        if found:
            files[idx] = found
        else:
            missing.append(idx)

    if missing:
        return None, f'{group_name} 그룹의 idx {missing} 파일이 없습니다'
    return files, None

POWEROFF_SUDO_PASSWORD = os.environ.get('POWEROFF_SUDO_PASSWORD', '')
SUDO_CMD = ('sudo', '-S', '-p', '')
SUDO_INPUT = POWEROFF_SUDO_PASSWORD + '\n' if POWEROFF_SUDO_PASSWORD else None

SMC_PWR_RAIL_MAP = {
    'efuse': 'INPUT_12V',
    'rebel_core0': 'CORE0_0P75V',
    'rebel_core1': 'CORE1_0P75V',
    'ucie_vddq': 'UCIE_0P75V',
    'hbm_vddq': 'HBM_1P1V',
    'rebel_hbm': 'HBM_0P85V',
    'rebel_hbm_vpp': 'SYS_1P8V',
    'hbm_vddql_top': 'HBM_0P4V_TOP',
    'hbm_vddql_btm': 'HBM_0P4V_BOT',
    'pcie_vp': 'PCIE_0P85V',
    'avdd_1p2v': 'AVDD_1P2V',
    'avdd_3p3v': 'SYS_3P3V',
}

SMC_PWR_LAST_ERROR = None

sysinfo_config = {
    'enabled': True,
    'interval_ms': 1000,
}
sysinfo_config_lock = threading.Lock()

active_clients = {}
active_clients_lock = threading.Lock()

Path(JOBS_DIR).mkdir(exist_ok=True)

_job_counter = 0
_job_counter_lock = threading.Lock()

class JobManager:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
    
    def create_job(self, workload, exec_time, workload_dir=None, save_log=False, log_path=None):
        global _job_counter
        if workload_dir is None:
            workload_dir = DEFAULT_WORKLOAD_DIR
        
        with _job_counter_lock:
            _job_counter += 1
            job_id = f"job_{int(time.time() * 1000)}_{_job_counter}"
        job = {
            'job_id': job_id,
            'workload': workload,
            'workload_dir': workload_dir,
            'exec_time': exec_time,
            'save_log': save_log,
            'log_path': log_path or '',
            'log_saved_path': None,
            'status': 'preparing',
            'process': None,
            'progress': {
                'stage': 'preparing',
                'elapsed': 0,
                'percentage': 0
            },
            'result': None,
            'start_time': datetime.now().isoformat(),
            'output_file': os.path.join(JOBS_DIR, f"{job_id}.txt")
        }
        
        with self.lock:
            self.jobs[job_id] = job
        
        return job_id
    
    def get_job(self, job_id):
        """작업 정보 조회"""
        with self.lock:
            return self.jobs.get(job_id)
    
    def update_job_status(self, job_id, status, progress=None, result=None):
        with self.lock:
            if job_id in self.jobs:
                if self.jobs[job_id]['status'] == 'cancelled' and status != 'cancelled':
                    return
                self.jobs[job_id]['status'] = status
                if progress:
                    self.jobs[job_id]['progress'] = progress
                if result:
                    self.jobs[job_id]['result'] = result
    
    def cancel_job(self, job_id):
        with self.lock:
            if job_id in self.jobs:
                process = self.jobs[job_id].get('process')
                if process and process.poll() is None:
                    try:
                        pgid = os.getpgid(process.pid)
                        os.killpg(pgid, signal.SIGTERM)
                        import threading as _th
                        def _force_kill():
                            import time as _t
                            _t.sleep(3)
                            if process.poll() is None:
                                try:
                                    os.killpg(pgid, signal.SIGKILL)
                                except (ProcessLookupError, PermissionError):
                                    pass
                        _th.Thread(target=_force_kill, daemon=True).start()
                        self.jobs[job_id]['status'] = 'cancelled'
                        return True
                    except (ProcessLookupError, PermissionError):
                        try:
                            process.kill()
                            self.jobs[job_id]['status'] = 'cancelled'
                            return True
                        except:
                            return False
        return False

job_manager = JobManager()


def _run_sysinfo(type_flag):
    """rbln sysinfo -t <type> -d0 -j true 실행, JSON 파싱 반환"""
    try:
        result = subprocess.run(
            SUDO_CMD + ('rbln', 'sysinfo', '-t', type_flag, '-d0', '-j', 'true'),
            input=SUDO_INPUT, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def collect_smc_pwr():
    """SMC power rails 원시 텍스트 수집"""
    global SMC_PWR_LAST_ERROR
    try:
        result = subprocess.run(
            SUDO_CMD + ('rbln', 'sysinfo', '-t', 'smc_pwr', '-d0'),
            input=SUDO_INPUT, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            SMC_PWR_LAST_ERROR = result.stderr.strip() if result.stderr else 'exit code != 0'
            return None
        return result.stdout
    except Exception as e:
        SMC_PWR_LAST_ERROR = str(e)
        return None


def parse_smc_pwr(raw_text):
    """SMC power raw 텍스트를 파싱하여 rail 리스트 반환"""
    rail_measurements = {}
    for line in raw_text.splitlines():
        match = re.match(r'^\s*([a-zA-Z0-9_]+)\s+\(([^)]+)\)\s*:\s*(-?\d+)\s*$', line)
        if not match:
            continue
        field_name, unit, value_text = match.groups()
        # skip pcie_vph_ fields
        if field_name.startswith('pcie_vph_'):
            continue
        if field_name.endswith('_voltage'):
            rail_key = field_name[:-8]
            metric = 'voltage_mv'
        elif field_name.endswith('_current'):
            rail_key = field_name[:-8]
            metric = 'current_ma'
        else:
            continue
        # mV→mV, mA→mA
        value = int(value_text)
        if unit == 'mV':
            rail_measurements.setdefault(rail_key, {})['voltage_mv'] = value
        elif unit == 'mA':
            rail_measurements.setdefault(rail_key, {})['current_ma'] = value

    rails = []
    for raw_key, display_name in SMC_PWR_RAIL_MAP.items():
        measurements = rail_measurements.get(raw_key, {})
        voltage_mv = measurements.get('voltage_mv')
        current_ma = measurements.get('current_ma')
        power_w = None
        if voltage_mv is not None and current_ma is not None:
            power_w = round(voltage_mv * current_ma / 1000000, 2)
        rails.append({
            'name': display_name,
            'raw_key': raw_key,
            'voltage_mv': voltage_mv,
            'current_ma': current_ma,
            'power_w': power_w
        })
    return rails


def _parse_thermal(data):
    """thermal JSON → temperature(int), dram_temp(list), dram_sid_temps(list), throttling(bool)"""
    if not data:
        return {'temperature': None, 'throttling': False, 'dram_temp': [], 'dram_sid_temps': []}
    info = data.get('Thermal Information', {})
    temp = info.get('Temperature_C')
    throttling = bool(info.get('Thermal Throttle Status'))
    dram_temp = []
    dram_sid_temps = []
    for chiplet in info.get('Chiplets', []):
        dram_temp.append(chiplet.get('DRAM Temperature_C'))
        dram_sid_temps.append({
            'sid0': chiplet.get('DRAM SID0 Temperature_C'),
            'sid1': chiplet.get('DRAM SID1 Temperature_C'),
            'sid2': chiplet.get('DRAM SID2 Temperature_C'),
        })
    return {'temperature': temp, 'throttling': throttling, 'dram_temp': dram_temp, 'dram_sid_temps': dram_sid_temps}


def _parse_power(data):
    """power JSON → power(float W), voltage_mv(int), current_ma(int)"""
    if not data:
        return {'power': None, 'voltage_mv': None, 'current_ma': None}
    info = data.get('Power Information', {})
    power_mw = info.get('power_mw')
    voltage_mv = info.get('voltage_mv')
    current_ma = info.get('current_ma')
    power_w = round(power_mw / 1000, 1) if power_mw else None
    return {'power': power_w, 'voltage_mv': voltage_mv, 'current_ma': current_ma}


def _parse_util(data):
    """util JSON → util(float), chiplet_util(list of float)"""
    if not data:
        return {'util': 0.0, 'chiplet_util': []}
    info = data.get('Utilization Information', {})
    total = info.get('total_util', '0.0')
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 0.0
    chiplet_util = []
    for val in info.get('chiplet_util', []):
        try:
            chiplet_util.append(float(val))
        except (TypeError, ValueError):
            chiplet_util.append(0.0)
    return {'util': total, 'chiplet_util': chiplet_util}


def _read_output_file(path):
    """출력 파일을 안전하게 읽기"""
    if not os.path.exists(path):
        return ""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()

def run_workload_background(job_id, workload_path, exec_time, is_script=False):
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
        monitor_workload_progress(job_id, output_file, exec_time)
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

def _collect_sample():
    """현재 시점의 power rails + thermal 샘플 수집"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    sample = {'timestamp': timestamp}

    # Power rails
    raw_pwr = collect_smc_pwr()
    if raw_pwr:
        rails = parse_smc_pwr(raw_pwr)
        sample['rails'] = {r['name']: {'voltage_mv': r['voltage_mv'], 'current_ma': r['current_ma'], 'power_w': r['power_w']} for r in rails}

    # Thermal
    thermal_data = _run_sysinfo('thermal')
    thermal = _parse_thermal(thermal_data)
    sample['npu_temp_c'] = thermal['temperature']
    sample['dram_temp_c'] = thermal['dram_temp']
    sample['throttling'] = thermal['throttling']

    return sample


def _save_log_file(job):
    """Job 완료 후 수집된 샘플을 JSON 로그로 저장"""
    log_dir = job.get('log_path', '/home/rebellions/yeun/testvector-server/logs')
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


def monitor_workload_progress(job_id, output_file, exec_time):
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
    pre_sample = _collect_sample()
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
                    pending_sample_future = sample_executor.submit(_collect_sample)
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


@app.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')


@app.route('/health')
def health():
    """헬스 체크"""
    return jsonify({'status': 'healthy'})


@app.route('/api/workload-folders')
def get_workload_folders():
    """워크로드 폴더 목록 API"""
    try:
        base_dir = "/home/rebellions/yeun/testvector"
        
        if not os.path.exists(base_dir):
            return jsonify({
                'success': False,
                'error': 'testvector 폴더가 존재하지 않습니다.'
            }), 404
        
        folders = []
        
        # 재귀적으로 폴더 탐색
        for root, dirs, files in os.walk(base_dir):
            # .bin 파일이 있는 폴더만 포함
            bin_files = [f for f in files if f.endswith('.bin')]
            if bin_files:
                # 상대 경로 계산
                rel_path = os.path.relpath(root, base_dir)
                folders.append(rel_path)
        
        # 기본 폴더를 맨 위로
        default_rel = os.path.relpath(DEFAULT_WORKLOAD_DIR, base_dir)
        if default_rel in folders:
            folders.remove(default_rel)
            folders.insert(0, default_rel)
        
        return jsonify({
            'success': True,
            'folders': folders,
            'default': default_rel
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/workloads')
def get_workloads():
    """워크로드 목록 API"""
    try:
        workload_dir_rel = request.args.get('workload_dir', 'cr13/v3.2.0')
        workload_dir = os.path.join("/home/rebellions/yeun/testvector", workload_dir_rel)
        
        if not os.path.exists(workload_dir):
            return jsonify({
                'success': False,
                'error': f'워크로드 폴더가 존재하지 않습니다: {workload_dir_rel}'
            }), 404
        
        # .bin 파일 모두 찾기
        pattern = os.path.join(workload_dir, "*.bin")
        files = sorted(glob.glob(pattern))
        
        # 파일명만 추출
        bin_workloads = [os.path.basename(f) for f in files]

        # 고정 그룹 스크립트(.sh) 추가
        script_workloads = [
            s for s in VECTOR_GROUP_SCRIPTS
            if os.path.exists(os.path.join(workload_dir, s))
        ]
        workloads = bin_workloads + script_workloads

        available_groups = []
        for gname in VECTOR_GROUPS:
            gfiles, err = _find_group_files(workload_dir, gname)
            if not err:
                available_groups.append(gname)

        return jsonify({
            'success': True,
            'workloads': workloads,
            'available_groups': available_groups,
            'count': len(workloads),
            'bin_count': len(bin_workloads),
            'script_count': len(script_workloads),
            'workload_dir': workload_dir_rel
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/run', methods=['POST'])
def run_workload():
    """워크로드 실행 API"""
    try:
        data = request.json
        workload = data.get('workload')
        exec_time = data.get('exec_time', 10)
        workload_dir_rel = data.get('workload_dir', 'cr13/v3.2.0')
        workload_dir = os.path.join("/home/rebellions/yeun/testvector", workload_dir_rel)
        save_log = bool(data.get('save_log', False))
        log_path = str(data.get('log_path', '')).strip() or '/home/rebellions/yeun/testvector-server/logs'
        
        if not workload:
            return jsonify({'success': False, 'error': '워크로드 선택 필요'}), 400
        
        try:
            exec_time = int(exec_time)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '실행 시간은 숫자여야 합니다'}), 400

        if exec_time <= 0:
            return jsonify({'success': False, 'error': '실행 시간은 0보다 커야 합니다'}), 400

        if not os.path.exists(workload_dir):
            return jsonify({'success': False, 'error': f'워크로드 폴더가 존재하지 않습니다: {workload_dir_rel}'}), 404
        
        workload_path = os.path.join(workload_dir, workload)
        
        if not os.path.exists(workload_path):
            return jsonify({'success': False, 'error': '워크로드 파일 없음'}), 404

        is_script = workload.endswith('.sh')
        if is_script and workload not in VECTOR_GROUP_SCRIPTS:
            return jsonify({'success': False, 'error': '허용되지 않은 스크립트입니다'}), 400
        
        # Job 생성
        job_id = job_manager.create_job(workload, exec_time, workload_dir, save_log, log_path)

        with job_manager.lock:
            if job_id in job_manager.jobs:
                job_manager.jobs[job_id]['client_ip'] = request.remote_addr or 'unknown'

        thread = threading.Thread(
            target=run_workload_background,
            args=(job_id, workload_path, exec_time, is_script),
            daemon=True
        )
        thread.start()
        
        return jsonify({
            'success': True,
            'job_id': job_id
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/run-group', methods=['POST'])
def run_group():
    try:
        data = request.json
        groups = data.get('groups', [])
        exec_time = data.get('exec_time', 10)
        workload_dir_rel = data.get('workload_dir', 'cr13/v3.2.0')
        workload_dir = os.path.join("/home/rebellions/yeun/testvector", workload_dir_rel)
        save_log = bool(data.get('save_log', False))
        log_path = str(data.get('log_path', '')).strip() or '/home/rebellions/yeun/testvector-server/logs'

        if not groups or not isinstance(groups, list):
            return jsonify({'success': False, 'error': '그룹을 선택하세요'}), 400

        invalid = [g for g in groups if g not in VECTOR_GROUPS]
        if invalid:
            return jsonify({'success': False, 'error': f'지원하지 않는 그룹: {invalid}'}), 400

        try:
            exec_time = int(exec_time)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '실행 시간은 숫자여야 합니다'}), 400

        if exec_time <= 0:
            return jsonify({'success': False, 'error': '실행 시간은 0보다 커야 합니다'}), 400

        if not os.path.exists(workload_dir):
            return jsonify({'success': False, 'error': f'워크로드 폴더가 존재하지 않습니다: {workload_dir_rel}'}), 404

        all_group_files = {}
        for g in groups:
            files, err = _find_group_files(workload_dir, g)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            all_group_files[g] = files

        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        job_ids = []

        for g, files in all_group_files.items():
            for idx in range(4):
                fpath = files[idx]
                fname = os.path.basename(fpath)
                job_id = job_manager.create_job(fname, exec_time, workload_dir, save_log, log_path)
                with job_manager.lock:
                    if job_id in job_manager.jobs:
                        job_manager.jobs[job_id]['batch_id'] = batch_id
                        job_manager.jobs[job_id]['group'] = g
                        job_manager.jobs[job_id]['client_ip'] = request.remote_addr or 'unknown'
                job_ids.append(job_id)

                thread = threading.Thread(
                    target=run_workload_background,
                    args=(job_id, fpath, exec_time, False),
                    daemon=True
                )
                thread.start()

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'job_ids': job_ids,
            'groups': groups,
            'total': len(job_ids)
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/run-queue', methods=['POST'])
def run_queue():
    try:
        data = request.json
        queue = data.get('queue', [])
        exec_time = data.get('exec_time', 10)
        save_log = bool(data.get('save_log', False))
        log_path = str(data.get('log_path', '')).strip() or '/home/rebellions/yeun/testvector-server/logs'

        if not queue or not isinstance(queue, list):
            return jsonify({'success': False, 'error': '실행 대기 목록이 비어있습니다'}), 400

        try:
            exec_time = int(exec_time)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '실행 시간은 숫자여야 합니다'}), 400

        if exec_time <= 0:
            return jsonify({'success': False, 'error': '실행 시간은 0보다 커야 합니다'}), 400

        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        all_job_ids = []
        queue_plan = []

        for item in queue:
            item_type = item.get('type')
            item_dir_rel = item.get('workload_dir', 'cr13/v3.2.0')
            item_dir = os.path.join("/home/rebellions/yeun/testvector", item_dir_rel)

            if not os.path.exists(item_dir):
                return jsonify({'success': False, 'error': f'워크로드 폴더가 존재하지 않습니다: {item_dir_rel}'}), 404

            if item_type == 'single':
                workload = item.get('workload')
                fpath = os.path.join(item_dir, workload)
                if not os.path.exists(fpath):
                    return jsonify({'success': False, 'error': f'파일 없음: {workload}'}), 404
                job_id = job_manager.create_job(workload, exec_time, item_dir, save_log, log_path)
                with job_manager.lock:
                    if job_id in job_manager.jobs:
                        job_manager.jobs[job_id]['batch_id'] = batch_id
                        job_manager.jobs[job_id]['client_ip'] = request.remote_addr or 'unknown'
                all_job_ids.append(job_id)
                queue_plan.append({'type': 'single', 'job_id': job_id, 'path': fpath})

            elif item_type == 'group':
                files = item.get('files', [])
                group_label = item.get('label', 'group')
                group_job_ids = []
                for fname in files:
                    fpath = os.path.join(item_dir, fname)
                    if not os.path.exists(fpath):
                        return jsonify({'success': False, 'error': f'파일 없음: {fname}'}), 404
                    job_id = job_manager.create_job(fname, exec_time, item_dir, save_log, log_path)
                    with job_manager.lock:
                        if job_id in job_manager.jobs:
                            job_manager.jobs[job_id]['batch_id'] = batch_id
                            job_manager.jobs[job_id]['group'] = group_label
                            job_manager.jobs[job_id]['client_ip'] = request.remote_addr or 'unknown'
                    group_job_ids.append(job_id)
                    all_job_ids.append(job_id)
                queue_plan.append({'type': 'group', 'job_ids': group_job_ids, 'paths': [os.path.join(item_dir, f) for f in files]})
            else:
                return jsonify({'success': False, 'error': f'알 수 없는 타입: {item_type}'}), 400

        def execute_queue():
            for plan_item in queue_plan:
                if plan_item['type'] == 'single':
                    jid = plan_item['job_id']
                    run_workload_background(jid, plan_item['path'], exec_time, False)
                    while True:
                        job = job_manager.get_job(jid)
                        if not job or job['status'] in ('completed', 'error', 'cancelled'):
                            break
                        time.sleep(0.5)

                elif plan_item['type'] == 'group':
                    threads = []
                    for jid, fpath in zip(plan_item['job_ids'], plan_item['paths']):
                        t = threading.Thread(
                            target=run_workload_background,
                            args=(jid, fpath, exec_time, False),
                            daemon=True
                        )
                        t.start()
                        threads.append(t)
                    for t in threads:
                        t.join()

        thread = threading.Thread(target=execute_queue, daemon=True)
        thread.start()

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'job_ids': all_job_ids,
            'total': len(all_job_ids)
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/job/<job_id>')
def get_job_status(job_id):
    """작업 상태 조회 API"""
    try:
        job = job_manager.get_job(job_id)
        
        if not job:
            return jsonify({
                'success': False,
                'error': '작업 없음'
            }), 404
        
        return jsonify({
            'success': True,
            'job': {
                'job_id': job['job_id'],
                'workload': job['workload'],
                'workload_dir': job.get('workload_dir', DEFAULT_WORKLOAD_DIR),
                'exec_time': job['exec_time'],
                'status': job['status'],
                'progress': job['progress'],
                'result': job['result'],
                'start_time': job['start_time'],
                'log_saved_path': job.get('log_saved_path')
            }
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/job/<job_id>/logs')
def get_job_logs(job_id):
    """작업 로그 조회 API (offset 기반 incremental fetch)"""
    try:
        job = job_manager.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': '작업 없음'}), 404

        output_file = job.get('output_file')
        if not output_file or not os.path.exists(output_file):
            return jsonify({
                'success': True,
                'logs': '',
                'next_offset': 0,
                'eof': True
            })

        try:
            offset = int(request.args.get('offset', 0))
        except (TypeError, ValueError):
            offset = 0

        offset = max(0, offset)
        max_chunk = 65536

        with open(output_file, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()

            if offset > file_size:
                offset = file_size

            f.seek(offset, os.SEEK_SET)
            logs = f.read(max_chunk)
            next_offset = f.tell()

        return jsonify({
            'success': True,
            'logs': logs,
            'next_offset': next_offset,
            'eof': next_offset >= file_size
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/job/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    """작업 취소 API"""
    try:
        success = job_manager.cancel_job(job_id)
        
        return jsonify({
            'success': success,
            'message': '작업 취소됨' if success else '작업 취소 실패'
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/job/<job_id>/download')
def download_job_log(job_id):
    try:
        job = job_manager.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': '작업 없음'}), 404
        log_path = job.get('log_saved_path')
        if log_path and os.path.exists(log_path):
            return send_file(log_path, as_attachment=True)
        if job.get('_samples'):
            saved = _save_log_file(job)
            with job_manager.lock:
                if job_id in job_manager.jobs:
                    job_manager.jobs[job_id]['log_saved_path'] = saved
            return send_file(saved, as_attachment=True)
        return jsonify({'success': False, 'error': '로그 데이터 없음'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sysinfo-config', methods=['GET', 'POST'])
def sysinfo_config_api():
    if request.method == 'GET':
        with sysinfo_config_lock:
            return jsonify(sysinfo_config)
    data = request.get_json(force=True)
    with sysinfo_config_lock:
        if 'enabled' in data:
            sysinfo_config['enabled'] = bool(data['enabled'])
        if 'interval_ms' in data:
            val = int(data['interval_ms'])
            sysinfo_config['interval_ms'] = max(1000, min(3000, val))
        return jsonify(sysinfo_config)


@app.route('/api/system-info')
def get_system_info():
    """시스템 정보 API"""
    try:
        client_ip = request.remote_addr or 'unknown'
        now = time.time()
        with active_clients_lock:
            active_clients[client_ip] = now
            stale = [ip for ip, ts in active_clients.items() if now - ts > 5]
            for ip in stale:
                del active_clients[ip]
            client_list = sorted(active_clients.keys())

        running_job_info = None
        with job_manager.lock:
            for jid, job in job_manager.jobs.items():
                if job['status'] in ('preparing', 'running'):
                    running_job_info = {
                        'job_id': jid,
                        'client_ip': job.get('client_ip', 'unknown'),
                        'workload': job['workload'],
                        'status': job['status'],
                        'progress': job.get('progress', {}),
                    }
                    break

        with sysinfo_config_lock:
            sysinfo_enabled = sysinfo_config['enabled']

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                'smi': executor.submit(subprocess.run, ['rbln-smi', '--json'], capture_output=True, text=True, timeout=5),
            }
            if sysinfo_enabled:
                futures['thermal'] = executor.submit(_run_sysinfo, 'thermal')
                futures['power'] = executor.submit(_run_sysinfo, 'power')
                futures['util'] = executor.submit(_run_sysinfo, 'util')
                futures['smc_pwr'] = executor.submit(collect_smc_pwr)
            results = {k: v.result() for k, v in futures.items()}

        smi_result = results['smi']
        if smi_result.returncode != 0:
            return jsonify({'success': False, 'error': 'rbln-smi 실행 실패'}), 500

        data = json.loads(smi_result.stdout)
        thermal = _parse_thermal(results.get('thermal'))
        power = _parse_power(results.get('power'))
        util = _parse_util(results.get('util'))

        devices_info = []
        for device in data.get('devices', []):
            memory = device.get('memory', {})
            try:
                mem_used_gb = int(memory.get('used', 0)) / (1024 ** 3)
                mem_total_gb = int(memory.get('total', 1)) / (1024 ** 3)
            except:
                mem_used_gb = 0
                mem_total_gb = 1

            temp = thermal.get('temperature')
            if temp is None:
                temp_str = device.get('temperature', '0C').replace('C', '')
                try:
                    temp = int(temp_str)
                except:
                    temp = 0

            power_w = power.get('power')
            if power_w is None:
                power_str = device.get('card_power', '0uW')
                try:
                    if 'uW' in power_str:
                        power_w = round(float(power_str.replace('uW', '')) / 1000000, 1)
                    else:
                        power_w = round(float(power_str.replace('W', '')), 1)
                except:
                    power_w = 0

            util_val = util.get('util', float(device.get('util', 0)))

            devices_info.append({
                'npu': device.get('npu', 0),
                'name': device.get('name', 'N/A'),
                'device': device.get('device', 'N/A'),
                'status': device.get('status', 'N/A'),
                'temperature': temp,
                'power': power_w,
                'memory': {'used': round(mem_used_gb, 2), 'total': round(mem_total_gb, 2)},
                'util': util_val,
                'voltage_mv': power.get('voltage_mv'),
                'current_ma': power.get('current_ma'),
                'pstate': device.get('pstate', 'N/A'),
                'throttling': thermal.get('throttling', False),
                'dram_temp': thermal.get('dram_temp', []),
                'dram_sid_temps': thermal.get('dram_sid_temps', []),
                'chiplet_util': util.get('chiplet_util', []),
            })

        smc_pwr_raw = results.get('smc_pwr')
        smc_pwr_info = {'available': False, 'rails': []}
        if not sysinfo_enabled:
            smc_pwr_info = {'disabled': True, 'rails': []}
        elif smc_pwr_raw:
            smc_pwr_info = {'available': True, 'rails': parse_smc_pwr(smc_pwr_raw)}

        return jsonify({
            'success': True,
            'timestamp': datetime.now().isoformat(),
            'devices': devices_info,
            'smc_pwr': smc_pwr_info,
            'sysinfo_enabled': sysinfo_enabled,
            'active_clients': client_list,
            'running_job': running_job_info
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/system/npu-reset', methods=['POST'])
def system_npu_reset():
    if not POWEROFF_SUDO_PASSWORD:
        return jsonify({'success': False, 'error': 'POWEROFF_SUDO_PASSWORD가 설정되지 않았습니다.'}), 503

    try:
        result = subprocess.run(
            ('sudo', '-S', '-p', '', 'bash', '-c', 'echo 1 > /sys/class/rebellions/rsd0/hard_reset'),
            input=POWEROFF_SUDO_PASSWORD + '\n',
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return jsonify({'success': False, 'error': result.stderr.strip()}), 500
        return jsonify({'success': True, 'message': 'NPU Hard Reset 완료'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/system/poweroff', methods=['POST'])
def system_poweroff():
    if not POWEROFF_SUDO_PASSWORD:
        return jsonify({'success': False, 'error': 'POWEROFF_SUDO_PASSWORD가 설정되지 않았습니다.'}), 503

    try:
        def _delayed_poweroff():
            time.sleep(2)
            subprocess.run(
                SUDO_CMD + ('shutdown', '-h', 'now'),
                input=SUDO_INPUT,
                capture_output=True,
                text=True,
                timeout=10
            )

        threading.Thread(target=_delayed_poweroff, daemon=True).start()
        return jsonify({'success': True, 'message': '시스템이 2초 후 종료됩니다.'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("=" * 50)
    print("🚀 워크로드 실행 시스템 시작")
    print("=" * 50)
    print(f"📂 기본 워크로드 폴더: {DEFAULT_WORKLOAD_DIR}")
    print(f"🌐 접속 주소: http://localhost:5000")
    print("=" * 50)
    print("")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
