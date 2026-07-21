import os
import glob
import json
import threading
import time
import subprocess
from importlib import import_module
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, render_template, jsonify, request, send_file, current_app

_job_manager_module = import_module('app.services.job_manager')
_workload_module = import_module('app.services.workload')
_sensor_module = import_module('app.services.sensor')

active_clients = _job_manager_module.active_clients
active_clients_lock = _job_manager_module.active_clients_lock
run_workload_background = _workload_module.run_workload_background
VECTOR_GROUP_SCRIPTS = _workload_module.VECTOR_GROUP_SCRIPTS
_save_log_file = _workload_module._save_log_file
_run_sysinfo = _sensor_module._run_sysinfo
collect_smc_pwr = _sensor_module.collect_smc_pwr
parse_smc_pwr = _sensor_module.parse_smc_pwr
_parse_thermal = _sensor_module._parse_thermal
_parse_power = _sensor_module._parse_power
_parse_util = _sensor_module._parse_util


bp = Blueprint('main', __name__)


def _tv_base(cfg):
    return cfg.TESTVECTOR_ROOT


def _cfg():
    return getattr(current_app, 'config_obj')


def _job_manager():
    return getattr(current_app, 'job_manager')


@bp.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')


@bp.route('/health')
def health():
    """헬스 체크"""
    return jsonify({'status': 'healthy'})


@bp.route('/api/workload-folders')
def get_workload_folders():
    """워크로드 폴더 목록 API"""
    try:
        cfg = _cfg()
        base_dir = _tv_base(cfg)

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
        default_rel = cfg.DEFAULT_WORKLOAD_DIR
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


@bp.route('/api/workloads')
def get_workloads():
    """워크로드 목록 API"""
    try:
        cfg = _cfg()
        base_dir = _tv_base(cfg)
        workload_dir_rel = request.args.get(
            'workload_dir',
            cfg.DEFAULT_WORKLOAD_DIR
        )
        workload_dir = os.path.join(base_dir, workload_dir_rel)

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
            files, err = _find_group_files(workload_dir, gname)
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


@bp.route('/api/run', methods=['POST'])
def run_workload():
    """워크로드 실행 API"""
    try:
        job_manager = _job_manager()
        cfg = _cfg()
        base_dir = _tv_base(cfg)
        data = request.json
        workload = data.get('workload')
        exec_time = data.get('exec_time', 10)
        workload_dir_rel = data.get(
            'workload_dir',
            cfg.DEFAULT_WORKLOAD_DIR
        )
        workload_dir = os.path.join(base_dir, workload_dir_rel)
        save_log = bool(data.get('save_log', False))
        log_path = str(data.get('log_path', '')).strip() or cfg.LOG_DIR

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
            args=(
                job_id,
                workload_path,
                exec_time,
                is_script,
                job_manager,
                cfg,
            ),
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


@bp.route('/api/run-group', methods=['POST'])
def run_group():
    try:
        job_manager = _job_manager()
        cfg = _cfg()
        base_dir = _tv_base(cfg)
        data = request.json
        groups = data.get('groups', [])
        exec_time = data.get('exec_time', 10)
        workload_dir_rel = data.get('workload_dir', cfg.DEFAULT_WORKLOAD_DIR)
        workload_dir = os.path.join(base_dir, workload_dir_rel)
        save_log = bool(data.get('save_log', False))
        log_path = str(data.get('log_path', '')).strip() or cfg.LOG_DIR

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
                    args=(job_id, fpath, exec_time, False, job_manager, cfg),
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


@bp.route('/api/run-queue', methods=['POST'])
def run_queue():
    try:
        job_manager = _job_manager()
        cfg = _cfg()
        base_dir = _tv_base(cfg)
        data = request.json
        queue = data.get('queue', [])
        exec_time = data.get('exec_time', 10)
        save_log = bool(data.get('save_log', False))
        log_path = str(data.get('log_path', '')).strip() or cfg.LOG_DIR

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
            item_dir_rel = item.get('workload_dir', cfg.DEFAULT_WORKLOAD_DIR)
            item_dir = os.path.join(base_dir, item_dir_rel)

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
                    run_workload_background(jid, plan_item['path'], exec_time, False, job_manager, cfg)
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
                            args=(jid, fpath, exec_time, False, job_manager, cfg),
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


@bp.route('/api/job/<job_id>')
def get_job_status(job_id):
    """작업 상태 조회 API"""
    try:
        job_manager = _job_manager()
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
                'workload_dir': job.get('workload_dir', _cfg().DEFAULT_WORKLOAD_DIR),
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


@bp.route('/api/job/<job_id>/logs')
def get_job_logs(job_id):
    """작업 로그 조회 API (offset 기반 incremental fetch)"""
    try:
        job_manager = _job_manager()
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


@bp.route('/api/job/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    """작업 취소 API"""
    try:
        job_manager = _job_manager()
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


@bp.route('/api/job/<job_id>/download')
def download_job_log(job_id):
    try:
        job_manager = _job_manager()
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


@bp.route('/api/system-info')
def get_system_info():
    """시스템 정보 API"""
    try:
        job_manager = _job_manager()
        cfg = _cfg()
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

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                'smi': executor.submit(subprocess.run, ['rbln-smi', '--json'], capture_output=True, text=True, timeout=5),
                'thermal': executor.submit(_run_sysinfo, 'thermal', cfg.SUDO_CMD, cfg.SUDO_INPUT),
                'power': executor.submit(_run_sysinfo, 'power', cfg.SUDO_CMD, cfg.SUDO_INPUT),
                'util': executor.submit(_run_sysinfo, 'util', cfg.SUDO_CMD, cfg.SUDO_INPUT),
                'smc_pwr': executor.submit(collect_smc_pwr, cfg.SUDO_CMD, cfg.SUDO_INPUT),
            }
            results = {k: v.result() for k, v in futures.items()}

        smi_result = results['smi']
        if smi_result.returncode != 0:
            return jsonify({'success': False, 'error': 'rbln-smi 실행 실패'}), 500

        data = json.loads(smi_result.stdout)
        thermal = _parse_thermal(results['thermal'])
        power = _parse_power(results['power'])
        util = _parse_util(results['util'])

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

        smc_pwr_raw = results['smc_pwr']
        smc_pwr_info = {'available': False, 'rails': []}
        if smc_pwr_raw:
            smc_pwr_info = {'available': True, 'rails': parse_smc_pwr(smc_pwr_raw)}

        return jsonify({
            'success': True,
            'timestamp': datetime.now().isoformat(),
            'devices': devices_info,
            'smc_pwr': smc_pwr_info,
            'active_clients': client_list,
            'running_job': running_job_info
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/system/npu-reset', methods=['POST'])
def system_npu_reset():
    cfg = _cfg()
    if not cfg.POWEROFF_SUDO_PASSWORD:
        return jsonify({'success': False, 'error': 'POWEROFF_SUDO_PASSWORD가 설정되지 않았습니다.'}), 503

    try:
        result = subprocess.run(
            ('sudo', '-S', '-p', '', 'tee', '/sys/class/rebellions/rsd0/hard_reset'),
            input=cfg.POWEROFF_SUDO_PASSWORD + '\n1\n',
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return jsonify({'success': False, 'error': result.stderr.strip()}), 500
        return jsonify({'success': True, 'message': 'NPU Hard Reset 완료'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/system/poweroff', methods=['POST'])
def system_poweroff():
    """PC 전원 종료"""
    cfg = _cfg()

    if not cfg.POWEROFF_SUDO_PASSWORD:
        return jsonify({'success': False, 'error': 'POWEROFF_SUDO_PASSWORD가 설정되지 않았습니다.'}), 503

    try:
        def _delayed_poweroff():
            time.sleep(2)
            subprocess.run(
                cfg.SUDO_CMD + ('shutdown', '-h', 'now'),
                input=cfg.SUDO_INPUT,
                capture_output=True,
                text=True,
                timeout=10
            )

        threading.Thread(target=_delayed_poweroff, daemon=True).start()
        return jsonify({'success': True, 'message': '시스템이 2초 후 종료됩니다.'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
