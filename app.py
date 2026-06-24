import os
import json
import time
import random
import string
import asyncio
import threading
import subprocess
import logging
from datetime import datetime, date
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, render_template, redirect, url_for
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'awssb-panel-secret-2024')
socketio = SocketIO(app, cors_allowed_origins="*")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ========== 配置 ==========
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        'username': os.environ.get('PANEL_USER', 'admin'),
        'password': os.environ.get('PANEL_PASS', 'admin123'),
        'instances': [],
        'check_interval': 60,
        'fail_threshold': 3,
        'check_port': 22,
        'bark_url': ''
    }

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history(h):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(h[-200:], f, indent=2, ensure_ascii=False)

config = load_config()
history = load_history()

# ========== 实例运行时状态 ==========
# { profileId: { ipv4_fails: 0, ipv6_fails: 0, ipv4_status: 'unknown', ipv6_status: 'unknown',
#                ipv4: '', ipv6: '', replacing: False, last_check: '' } }
runtime = {}

def get_runtime(profile_id):
    if profile_id not in runtime:
        runtime[profile_id] = {
            'ipv4_fails': 0, 'ipv6_fails': 0,
            'ipv4_status': 'unknown', 'ipv6_status': 'unknown',
            'ipv4': '', 'ipv6': '', 'replacing': False, 'last_check': ''
        }
    return runtime[profile_id]

# ========== 工具函数 ==========
def rand_r(n=11):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def add_history(profile_id, name, event, detail, status='info'):
    entry = {
        'id': rand_r(8),
        'profile_id': profile_id,
        'name': name,
        'event': event,
        'detail': detail,
        'status': status,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    history.insert(0, entry)
    save_history(history)
    socketio.emit('history_update', entry)
    return entry

def send_bark(msg):
    bark_url = config.get('bark_url', '').strip()
    if not bark_url:
        return
    try:
        url = bark_url.rstrip('/') + '/' + requests.utils.quote(msg)
        requests.get(url, timeout=5)
        log.info(f'Bark 通知已发送: {msg}')
    except Exception as e:
        log.warning(f'Bark 通知失败: {e}')

# ========== AWS.sb API ==========
AWSSB_BASE = 'https://aws.sb/api'
HEADERS = {
    'Content-Type': 'application/json',
    'Referer': 'https://aws.sb/',
    'Origin': 'https://aws.sb',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
}

def fetch_instance_profiles(sgt):
    r = rand_r()
    resp = requests.get(f'{AWSSB_BASE}/ec2-instance-profiles?r={r}',
                        headers={**HEADERS, 'X-Sgt': sgt}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def fetch_instance_detail(instance_id, sgt):
    r = rand_r()
    resp = requests.get(f'{AWSSB_BASE}/{instance_id}?r={r}',
                        headers={**HEADERS, 'X-Sgt': sgt}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def do_replace_ip(instance_id, profile_id, sgt):
    r = rand_r()
    resp = requests.post(
        f'{AWSSB_BASE}/filter-tasks?r={r}',
        headers=HEADERS,
        json={'tags': [instance_id, profile_id]},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()

# ========== IP 检测 ==========
def check_tcp(ip, port, timeout=5):
    """TCP 连通性检测，返回 (ok, latency_ms)"""
    import socket
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        latency = int((time.time() - start) * 1000)
        return result == 0, latency
    except Exception:
        return False, 0

def check_ping(ip, timeout=5):
    """ICMP ping 检测，返回 (ok, latency_ms)"""
    flag = '-6' if ':' in ip else '-4'
    try:
        start = time.time()
        result = subprocess.run(
            ['ping', flag, '-c', '1', '-W', str(timeout), ip],
            capture_output=True, timeout=timeout+2
        )
        latency = int((time.time() - start) * 1000)
        return result.returncode == 0, latency
    except Exception:
        return False, 0

def check_ip(ip, port=22):
    """优先 TCP，fallback ping"""
    if port:
        ok, ms = check_tcp(ip, port)
        if ok:
            return True, ms
    ok, ms = check_ping(ip)
    return ok, ms

# ========== 监控主循环 ==========
monitor_thread = None
stop_event = threading.Event()

def monitor_loop():
    log.info('监控线程启动')
    while not stop_event.is_set():
        for inst in config.get('instances', []):
            if stop_event.is_set():
                break
            profile_id = inst['profile_id']
            rt = get_runtime(profile_id)
            if rt['replacing']:
                continue

            # 拉取最新 IP
            try:
                detail = fetch_instance_detail(inst['instance_id'], inst['sgt'])
                rt['ipv4'] = detail.get('publicIpAddress', '')
                rt['ipv6'] = detail.get('ipv6Addresses', [None])[0] or ''
            except Exception as e:
                log.warning(f'拉取实例详情失败 {inst["name"]}: {e}')

            port = inst.get('check_port', config.get('check_port', 22))
            threshold = config.get('fail_threshold', 3)
            now = datetime.now().strftime('%H:%M:%S')
            rt['last_check'] = now

            # 检测 IPv4
            if rt['ipv4']:
                ok4, ms4 = check_ip(rt['ipv4'], port)
                if ok4:
                    rt['ipv4_fails'] = 0
                    rt['ipv4_status'] = f'通 {ms4}ms'
                else:
                    rt['ipv4_fails'] += 1
                    rt['ipv4_status'] = f'不通 {rt["ipv4_fails"]}/{threshold}'
            else:
                rt['ipv4_status'] = '无IP'
                ms4 = 0

            # 检测 IPv6
            if rt['ipv6']:
                ok6, ms6 = check_ip(rt['ipv6'], port)
                if ok6:
                    rt['ipv6_fails'] = 0
                    rt['ipv6_status'] = f'通 {ms6}ms'
                else:
                    rt['ipv6_fails'] += 1
                    rt['ipv6_status'] = f'不通 {rt["ipv6_fails"]}/{threshold}'
            else:
                rt['ipv6_status'] = '无IPv6'
                ms6 = 0

            # 推送状态到前端
            socketio.emit('status_update', {
                'profile_id': profile_id,
                'ipv4': rt['ipv4'],
                'ipv6': rt['ipv6'],
                'ipv4_status': rt['ipv4_status'],
                'ipv6_status': rt['ipv6_status'],
                'last_check': now
            })

            # 判断是否需要换IP（IPv4 或 IPv6 连续失败达到阈值）
            need_replace = (
                (rt['ipv4'] and rt['ipv4_fails'] >= threshold) or
                (rt['ipv6'] and rt['ipv6_fails'] >= threshold)
            )

            if need_replace and inst.get('auto_replace', True):
                log.info(f'{inst["name"]} 检测到IP被墙，触发换IP')
                rt['replacing'] = True
                rt['ipv4_fails'] = 0
                rt['ipv6_fails'] = 0
                socketio.emit('replacing', {'profile_id': profile_id})

                try:
                    result = do_replace_ip(inst['instance_id'], profile_id, inst['sgt'])
                    msg = f'IP被墙，已自动换IP'
                    add_history(profile_id, inst['name'], '自动换IP', msg, 'success')
                    send_bark(f'[{inst["name"]}] {msg}')
                    log.info(f'{inst["name"]} 换IP成功: {result}')
                    time.sleep(30)  # 等待新IP生效
                except Exception as e:
                    msg = f'换IP失败: {e}'
                    add_history(profile_id, inst['name'], '换IP失败', msg, 'error')
                    send_bark(f'[{inst["name"]}] {msg}')
                    log.error(f'{inst["name"]} 换IP失败: {e}')
                finally:
                    rt['replacing'] = False

        stop_event.wait(config.get('check_interval', 60))
    log.info('监控线程停止')

def start_monitor():
    global monitor_thread, stop_event
    stop_event.clear()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

def restart_monitor():
    global stop_event
    stop_event.set()
    time.sleep(1)
    start_monitor()

# ========== 登录验证 ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json:
                return jsonify({'error': 'unauthorized'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

# ========== 路由 ==========
@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login_page'))
    return render_template('index.html')

@app.route('/login', methods=['GET'])
def login_page():
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    if data.get('username') == config['username'] and data.get('password') == config['password']:
        session['logged_in'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '用户名或密码错误'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/instances', methods=['GET'])
@login_required
def get_instances():
    result = []
    for inst in config['instances']:
        pid = inst['profile_id']
        rt = get_runtime(pid)
        result.append({**inst, **rt})
    return jsonify(result)

@app.route('/api/instances', methods=['POST'])
@login_required
def add_instance():
    data = request.json
    required = ['name', 'sgt', 'instance_id', 'profile_id']
    for f in required:
        if not data.get(f):
            return jsonify({'error': f'{f} 必填'}), 400

    # 验证 sgt 有效性
    try:
        profiles = fetch_instance_profiles(data['sgt'])
        matched = next((p for p in profiles if p['id'] == data['profile_id']), None)
        if not matched:
            return jsonify({'error': 'Profile ID 不匹配，请检查 sgt 和 Profile ID'}), 400
    except Exception as e:
        return jsonify({'error': f'验证失败: {str(e)}'}), 400

    inst = {
        'name': data['name'],
        'sgt': data['sgt'],
        'instance_id': data['instance_id'],
        'profile_id': data['profile_id'],
        'check_port': int(data.get('check_port', 22)),
        'auto_replace': data.get('auto_replace', True),
        'region': data.get('region', '')
    }
    config['instances'].append(inst)
    save_config(config)
    add_history(inst['profile_id'], inst['name'], '添加实例', '实例已添加到面板', 'info')
    return jsonify({'ok': True, 'instance': inst})

@app.route('/api/instances/<profile_id>', methods=['DELETE'])
@login_required
def delete_instance(profile_id):
    config['instances'] = [i for i in config['instances'] if i['profile_id'] != profile_id]
    save_config(config)
    if profile_id in runtime:
        del runtime[profile_id]
    return jsonify({'ok': True})

@app.route('/api/instances/<profile_id>/replace', methods=['POST'])
@login_required
def manual_replace(profile_id):
    inst = next((i for i in config['instances'] if i['profile_id'] == profile_id), None)
    if not inst:
        return jsonify({'error': '实例不存在'}), 404
    try:
        result = do_replace_ip(inst['instance_id'], profile_id, inst['sgt'])
        add_history(profile_id, inst['name'], '手动换IP', '手动触发换IP成功', 'success')
        send_bark(f'[{inst["name"]}] 手动换IP成功')
        rt = get_runtime(profile_id)
        rt['ipv4_fails'] = 0
        rt['ipv6_fails'] = 0
        return jsonify({'ok': True, 'result': result})
    except Exception as e:
        add_history(profile_id, inst['name'], '手动换IP失败', str(e), 'error')
        return jsonify({'error': str(e)}), 500

@app.route('/api/instances/<profile_id>/toggle', methods=['POST'])
@login_required
def toggle_auto(profile_id):
    inst = next((i for i in config['instances'] if i['profile_id'] == profile_id), None)
    if not inst:
        return jsonify({'error': '实例不存在'}), 404
    inst['auto_replace'] = not inst.get('auto_replace', True)
    save_config(config)
    return jsonify({'ok': True, 'auto_replace': inst['auto_replace']})

@app.route('/api/history', methods=['GET'])
@login_required
def get_history():
    return jsonify(history[:100])

@app.route('/api/history', methods=['DELETE'])
@login_required
def clear_history():
    history.clear()
    save_history(history)
    return jsonify({'ok': True})

@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    return jsonify({
        'check_interval': config.get('check_interval', 60),
        'fail_threshold': config.get('fail_threshold', 3),
        'check_port': config.get('check_port', 22),
        'bark_url': config.get('bark_url', ''),
        'username': config.get('username', 'admin')
    })

@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.json
    if 'check_interval' in data:
        config['check_interval'] = max(30, int(data['check_interval']))
    if 'fail_threshold' in data:
        config['fail_threshold'] = max(1, int(data['fail_threshold']))
    if 'check_port' in data:
        config['check_port'] = int(data['check_port'])
    if 'bark_url' in data:
        config['bark_url'] = data['bark_url']
    if 'password' in data and data['password']:
        config['password'] = data['password']
    if 'username' in data and data['username']:
        config['username'] = data['username']
    save_config(config)
    restart_monitor()
    return jsonify({'ok': True})

@app.route('/api/import', methods=['POST'])
@login_required
def import_from_sgt():
    """通过 sgt 自动导入所有实例"""
    data = request.json
    sgt = data.get('sgt', '').strip()
    if not sgt:
        return jsonify({'error': 'sgt 不能为空'}), 400
    try:
        profiles = fetch_instance_profiles(sgt)
        added = 0
        for p in profiles:
            existing = any(i['profile_id'] == p['id'] for i in config['instances'])
            if not existing:
                inst = {
                    'name': p.get('instanceName') or p['instanceId'],
                    'sgt': sgt,
                    'instance_id': p['instanceId'],
                    'profile_id': p['id'],
                    'check_port': 22,
                    'auto_replace': True,
                    'region': p.get('regionName', '')
                }
                config['instances'].append(inst)
                added += 1
        save_config(config)
        return jsonify({'ok': True, 'added': added, 'total': len(profiles)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    start_monitor()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
