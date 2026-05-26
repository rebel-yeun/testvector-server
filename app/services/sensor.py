import subprocess
import json
import re


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


def _run_sysinfo(type_flag, sudo_cmd, sudo_input):
    """rbln sysinfo -t <type> -d0 -j true 실행, JSON 파싱 반환"""
    cmd = sudo_cmd + ('rbln', 'sysinfo', '-t', type_flag, '-d0', '-j', 'true')
    try:
        result = subprocess.run(
            cmd, input=sudo_input, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return json.loads(result.stdout)

        if not sudo_cmd:
            fallback_cmd = ('sudo', '-n') + ('rbln', 'sysinfo', '-t', type_flag, '-d0', '-j', 'true')
            fb_result = subprocess.run(
                fallback_cmd, capture_output=True, text=True, timeout=5
            )
            if fb_result.returncode == 0:
                return json.loads(fb_result.stdout)
        return None
    except Exception:
        return None


def collect_smc_pwr(sudo_cmd, sudo_input):
    """SMC power rails 원시 텍스트 수집"""
    global SMC_PWR_LAST_ERROR
    cmd = sudo_cmd + ('rbln', 'sysinfo', '-t', 'smc_pwr', '-d0')
    try:
        result = subprocess.run(
            cmd, input=sudo_input, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout

        # sudo_cmd가 비어 있으면(비밀번호 미설정) sudo fallback 시도
        if not sudo_cmd:
            fallback_cmd = ('sudo', '-n') + ('rbln', 'sysinfo', '-t', 'smc_pwr', '-d0')
            fb_result = subprocess.run(
                fallback_cmd, capture_output=True, text=True, timeout=5
            )
            if fb_result.returncode == 0:
                return fb_result.stdout
            SMC_PWR_LAST_ERROR = fb_result.stderr.strip() if fb_result.stderr else 'permission denied'
            return None

        SMC_PWR_LAST_ERROR = result.stderr.strip() if result.stderr else 'exit code != 0'
        return None
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
