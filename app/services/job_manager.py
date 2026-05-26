import os
import threading
import time
import signal
from datetime import datetime


active_clients = {}
active_clients_lock = threading.Lock()


class JobManager:
    def __init__(self, cfg):
        self.jobs = {}
        self.lock = threading.Lock()
        self._cfg = cfg

    def create_job(self, workload, exec_time, workload_dir=None, save_log=False, log_path=None):
        """새 작업 생성"""
        if workload_dir is None:
            workload_dir = os.path.join(self._cfg.TESTVECTOR_ROOT, self._cfg.DEFAULT_WORKLOAD_DIR)

        job_id = f"job_{int(time.time() * 1000)}"
        job = {
            'job_id': job_id,
            'workload': workload,
            'workload_dir': workload_dir,
            'exec_time': exec_time,
            'save_log': save_log,
            'log_path': log_path or self._cfg.LOG_DIR,
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
            'output_file': os.path.join(self._cfg.JOBS_DIR, f"{job_id}.txt")
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
