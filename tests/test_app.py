import pytest
import json
from unittest.mock import patch, MagicMock

from app import create_app
from app.config import Config
from app.services.sensor import parse_smc_pwr, _parse_thermal, _parse_power, _parse_util


MOCK_SMI_OUTPUT = json.dumps({
    "devices": [{
        "npu": 0, "name": "ATOM", "device": "rebellions0",
        "status": "OK", "temperature": "30C", "card_power": "10W",
        "util": "0.0", "pstate": "P0",
        "memory": {"used": "0", "total": str(8 * 1024 ** 3)}
    }]
})

SAMPLE_SMC_TEXT = """
efuse_voltage (mV): 12000
efuse_current (mA): 500
rebel_core0_voltage (mV): 750
rebel_core0_current (mA): 2000
"""


@pytest.fixture
def app():
    a = create_app()
    a.config['TESTING'] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# --- HTTP route tests ---

def test_health_returns_200(client):
    r = client.get('/health')
    assert r.status_code == 200
    d = json.loads(r.data)
    assert d['status'] == 'healthy'


def test_get_job_not_found(client):
    r = client.get('/api/job/nonexistent')
    assert r.status_code == 404
    d = json.loads(r.data)
    assert d['success'] == False


def test_run_missing_workload(client):
    r = client.post('/api/run', json={})
    assert r.status_code == 400
    d = json.loads(r.data)
    assert d['success'] == False


def test_run_invalid_exec_time(client):
    r = client.post('/api/run', json={'workload': 'x.bin', 'exec_time': 'bad'})
    assert r.status_code == 400
    d = json.loads(r.data)
    assert d['success'] == False


def test_run_zero_exec_time(client):
    r = client.post('/api/run', json={'workload': 'x.bin', 'exec_time': 0})
    assert r.status_code == 400
    d = json.loads(r.data)
    assert d['success'] == False


def test_cancel_nonexistent_job(client):
    r = client.post('/api/job/nope/cancel')
    assert r.status_code == 200
    d = json.loads(r.data)
    assert d['success'] == False


def test_job_logs_not_found(client):
    r = client.get('/api/job/nope/logs')
    assert r.status_code == 404


def test_system_info_mocked(client):
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = MOCK_SMI_OUTPUT

    with patch('app.routes.subprocess.run', return_value=mock_proc), \
         patch('app.routes._run_sysinfo', return_value=None), \
         patch('app.routes.collect_smc_pwr', return_value=None):
        r = client.get('/api/system-info')

    assert r.status_code == 200
    d = json.loads(r.data)
    assert d['success'] == True
    assert 'devices' in d
    assert 'smc_pwr' in d
    assert 'active_clients' in d
    assert 'running_job' in d


# --- Config tests ---

def test_config_defaults():
    c = Config()
    assert c.PORT == 5000
    assert c.HOST == '0.0.0.0'
    assert c.DEBUG == False
    assert isinstance(c.POWEROFF_SUDO_PASSWORD, str)


def test_config_env_override(monkeypatch):
    monkeypatch.setenv('PORT', '9999')
    monkeypatch.setenv('DEBUG', 'true')
    c = Config()
    assert c.PORT == 9999
    assert c.DEBUG == True


# --- Sensor parsing tests ---

def test_parse_smc_pwr_valid():
    rails = parse_smc_pwr(SAMPLE_SMC_TEXT)
    assert len(rails) == 12
    efuse = next(r for r in rails if r['raw_key'] == 'efuse')
    assert efuse['voltage_mv'] == 12000
    assert efuse['current_ma'] == 500
    assert efuse['power_w'] == 6.0


def test_parse_thermal_none():
    result = _parse_thermal(None)
    assert result == {'temperature': None, 'throttling': False, 'dram_temp': [], 'dram_sid_temps': []}


def test_parse_power_none():
    result = _parse_power(None)
    assert result == {'power': None, 'voltage_mv': None, 'current_ma': None}


def test_parse_util_none():
    result = _parse_util(None)
    assert result == {'util': 0.0, 'chiplet_util': []}


# --- JobManager unit test ---

def test_job_lifecycle(app):
    jm = app.job_manager
    job_id = jm.create_job('test.bin', 30)
    job = jm.get_job(job_id)
    assert job is not None
    assert job['workload'] == 'test.bin'
    assert job['status'] == 'preparing'

    jm.update_job_status(job_id, 'running', progress={'stage': 'running', 'elapsed': 5, 'percentage': 10})
    job = jm.get_job(job_id)
    assert job['status'] == 'running'
    assert job['progress']['percentage'] == 10
