#!/usr/bin/env python3

"""
====================================================================
Plex Standalone Rclone Cacher (WebSocket Event-Driven)
====================================================================
Version: 0.9.1
Date: 2026-05-12

[주요 특징]
- [대역폭 제한 대응] ffmpeg readrate를 적용하여 rclone mount 선캐시가 회선 전체를 점유하지 않도록 제어
- [캐시 기록 안정화] ffmpeg -progress 출력 기반으로 DB 캐시 진행 구간 업데이트 안정성 개선
- [로깅 최적화] 로그 가독성 대폭 향상 (모든 로그에 Part ID와 파일명 표시)
- [완벽 호환성] MPV (비공식) 및 Web/App (공식) 클라이언트 양방향 완벽 대응
  1) MPV: Client-Identifier에 숨겨진 part_id 암호를 해독(Smuggling)하여 다이렉트 캐싱
  2) 공식 클라이언트: 암호가 없으면 Plex 세션의 decision/selected 속성을 통해 정확한 파트 추적
- 다중 해상도 파일 구분을 위해 DB 기록 기준을 파일 경로 해시값으로 유지
- 배속 기반 누적 탐색(Cumulative Speed-Seek) 감지 로직 적용
- API 폴링 루프 제거 및 Plex WebSocket 실시간 알림 시스템 도입

pip install plexapi psutil websocket-client

[ffmpeg 요구사항]
- 권장: ffmpeg 5.x 이상 또는 `ffmpeg -h full`에서 `-readrate`가 표시되는 빌드
- ffmpeg 4.4.x처럼 `-readrate` 미지원 빌드는 실행 가능하지만, 대역폭 제한 없이 기존 방식으로 선캐시합니다.

===================================================================
"""

import ctypes
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta

import psutil
import websocket
from plexapi.server import PlexServer

# ==========================================
# 사용자 설정
# ==========================================

PLEX_URL = os.getenv('PLEX_CACHER_PLEX_URL') or 'http://127.0.0.1:32400'


def get_plex_token():
    token = os.getenv('PLEX_CACHER_PLEX_TOKEN')
    if token:
        return token

    try:
        with open(
            '/config/Library/Application Support/Plex Media Server/Preferences.xml',
            encoding='utf-8',
        ) as preferences:
            match = re.search(r'PlexOnlineToken="([^"]*)"', preferences.read())
    except OSError:
        return ''

    return match.group(1) if match else ''


PLEX_TOKEN = get_plex_token()


def get_env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default

    value = value.strip().lower()
    if value in {'1', 'true', 'yes', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'off'}:
        return False
    return default


def get_env_number(name, default, converter):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    try:
        return converter(value)
    except ValueError:
        return default


def get_path_mapping():
    value = os.getenv('PLEX_CACHER_PATH_MAPPING')
    if value is None or not value.strip():
        return {}

    mapping = json.loads(value)
    if not isinstance(mapping, dict):
        raise ValueError('PLEX_CACHER_PATH_MAPPING must be a JSON object')
    return {str(source): str(target) for source, target in mapping.items()}


FFMPEG_PATH = os.getenv('PLEX_CACHER_FFMPEG_PATH', '')
CLEAN_STALE_FFMPEG_ON_START = get_env_bool('PLEX_CACHER_CLEAN_STALE_FFMPEG_ON_START', True)

PATH_MAPPING = get_path_mapping()

LOG_LEVEL = os.getenv('PLEX_CACHER_LOG_LEVEL', 'INFO')
PAUSE_CACHE_ON_PLEX_PAUSE = get_env_bool('PLEX_CACHER_PAUSE_CACHE_ON_PLEX_PAUSE', False)
WEBSOCKET_RECONNECT_DELAY = get_env_number('PLEX_CACHER_WEBSOCKET_RECONNECT_DELAY', 2.0, float)

# 탐색/세션 정리 기준
SEEK_DETECT_SECONDS = get_env_number('PLEX_CACHER_SEEK_DETECT_SECONDS', 25, int)
SEEK_DETECT_SPEED_MULTIPLIER = get_env_number('PLEX_CACHER_SEEK_DETECT_SPEED_MULTIPLIER', 2.1, float)
DEBOUNCE_SECONDS = get_env_number('PLEX_CACHER_DEBOUNCE_SECONDS', 1.5, float)
JANITOR_INTERVAL_SECONDS = get_env_number('PLEX_CACHER_JANITOR_INTERVAL_SECONDS', 30, int)
CACHE_VALID_HOURS = get_env_number('PLEX_CACHER_CACHE_VALID_HOURS', 24, int)
SESSION_TIMEOUT_SECONDS = get_env_number('PLEX_CACHER_SESSION_TIMEOUT_SECONDS', 120, int)
FFMPEG_TERMINATE_GRACE_SECONDS = get_env_number('PLEX_CACHER_FFMPEG_TERMINATE_GRACE_SECONDS', 3.0, float)

# 영상 끝부분 이내로 캐싱되어 있으면 파일 끝까지 캐싱된 것으로 간주합니다.
CACHE_END_TOLERANCE_SECONDS = get_env_number('PLEX_CACHER_CACHE_END_TOLERANCE_SECONDS', 30, int)

# 다음 화 프리캐시 기준
PRECACHE_TRIGGER_PERCENT = get_env_number('PLEX_CACHER_PRECACHE_TRIGGER_PERCENT', 0.90, float)
PRECACHE_DURATION_SECONDS = get_env_number('PLEX_CACHER_PRECACHE_DURATION_SECONDS', 300, int)

# rclone mount 선캐시 대역폭 제한
# - CACHE_MAX_BANDWIDTH_MBPS: 본편 선캐시 전체가 사용할 최대 대역폭입니다.
#   예) 50Mbps 제한 회선에서 재생 여유 20Mbps를 남기려면 30.0 정도로 둡니다.
#   본편 캐시가 여러 개 동시에 필요하면 이 값을 세션 수로 나눠 각 ffmpeg를 재시작합니다.
# - PRECACHE_MAX_BANDWIDTH_MBPS: 다음 화 프리캐시가 사용할 최대 대역폭입니다.
#   현재 재생을 방해하지 않도록 본편보다 낮게 잡는 것을 권장합니다.
# - FALLBACK_READRATE: Plex에서 영상 비트레이트를 못 받은 경우 사용할 재생 배율입니다.
#   1.25는 영상 시간 기준 1초 분량을 0.8초 정도에 읽는 속도입니다.
CACHE_MAX_BANDWIDTH_MBPS = get_env_number('PLEX_CACHER_CACHE_MAX_BANDWIDTH_MBPS', 25.0, float)
PRECACHE_MAX_BANDWIDTH_MBPS = get_env_number('PLEX_CACHER_PRECACHE_MAX_BANDWIDTH_MBPS', 8.0, float)
FALLBACK_READRATE = get_env_number('PLEX_CACHER_FALLBACK_READRATE', 1.25, float)

numeric_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
logging.basicConfig(level=numeric_level, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PlexStandaloneCache")
logger.setLevel(numeric_level)
logging.getLogger("websocket").setLevel(logging.CRITICAL)
websocket.enableTrace(False)

plex_server = None
active_sessions = {}  # session_key -> {'item_id', 'part_id', 'filename', 'offset', 'duration', 'pid', 'timer', 'path', 'precache_triggered', 'last_seen', 'is_suspended', 'history', 'bitrate_kbps'}
precache_pids = set()
managed_ffmpeg_pids = set()
process_stop_reasons = {}
cache_control_lock = threading.RLock()
shutdown_event = threading.Event()
ffmpeg_supports_readrate = None
current_ws = None
INSTANCE_ID = uuid.uuid4().hex[:8]
CACHE_PROCESS_TAG = f"plex_cacher_{INSTANCE_ID}"

db_path = os.path.join(
    os.getenv('PLEX_CACHER_DATA_DIR') or '/config/plex-cacher',
    'standalone_cache.db',
)
db_lock = threading.Lock()

# ==========================================
# 헬퍼 및 DB 함수
# ==========================================
def get_mapped_path(plex_path):
    if not plex_path: return None
    for plex_prefix, local_prefix in PATH_MAPPING.items():
        if plex_path.startswith(plex_prefix):
            mapped_path = plex_path.replace(plex_prefix, local_prefix, 1)
            logger.debug(f"🔄 경로 변환 적용: {plex_path} -> {mapped_path}")
            return mapped_path
    return plex_path

def ms_to_hms(ms):
    if not ms: return "0:00:00"
    return str(timedelta(seconds=int(ms / 1000)))

def get_path_hash(path):
    if not path: return ""
    return hashlib.md5(path.encode('utf-8')).hexdigest()

def get_media_bitrate_kbps(media):
    try:
        bitrate = getattr(media, 'bitrate', None)
        return int(bitrate) if bitrate else 0
    except (TypeError, ValueError):
        return 0

def clamp_readrate(value):
    return max(0.25, min(8.0, value))

def calculate_readrate(media_bitrate_kbps, is_precache=False, max_bandwidth_mbps=None):
    if not media_bitrate_kbps or media_bitrate_kbps <= 0:
        return clamp_readrate(FALLBACK_READRATE)

    max_bandwidth = max_bandwidth_mbps if max_bandwidth_mbps is not None else (PRECACHE_MAX_BANDWIDTH_MBPS if is_precache else CACHE_MAX_BANDWIDTH_MBPS)
    media_mbps = media_bitrate_kbps / 1000.0
    if media_mbps > 0:
        return clamp_readrate(max_bandwidth / media_mbps)
    return clamp_readrate(FALLBACK_READRATE)

def resolve_ffmpeg_path():
    if FFMPEG_PATH:
        return FFMPEG_PATH

    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_names = ['ffmpeg.exe', 'ffmpeg'] if os.name == 'nt' else ['ffmpeg', 'ffmpeg.exe']
    for name in local_names:
        candidate = os.path.join(script_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return 'ffmpeg'

def init_ffmpeg_capabilities():
    global ffmpeg_supports_readrate
    ffmpeg_path = resolve_ffmpeg_path()
    try:
        version_result = subprocess.run(
            [ffmpeg_path, '-version'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10
        )
        version_line = version_result.stdout.splitlines()[0] if version_result.stdout else "unknown version"

        help_result = subprocess.run(
            [ffmpeg_path, '-hide_banner', '-h', 'full'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10
        )
        ffmpeg_supports_readrate = '-readrate ' in help_result.stdout or '\n-readrate ' in help_result.stdout

        if ffmpeg_supports_readrate:
            logger.info(f"🎞️ ffmpeg 확인: {ffmpeg_path} | {version_line} | -readrate 지원")
        else:
            logger.warning(f"🎞️ ffmpeg 확인: {ffmpeg_path} | {version_line} | -readrate 미지원, 구버전 방식(무제한 선읽기)으로 동작")
        return True
    except Exception as e:
        ffmpeg_supports_readrate = False
        logger.error(f"❌ ffmpeg 확인 실패: {ffmpeg_path} | {e}")
        return False

def build_ffmpeg_base_command(readrate):
    command = [resolve_ffmpeg_path(), '-hide_banner', '-loglevel', 'error', '-nostats', '-progress', 'pipe:2']
    if ffmpeg_supports_readrate:
        command += ['-readrate', f'{readrate:.3f}']
    return command

def prepare_child_process():
    os.setsid()
    try:
        libc = ctypes.CDLL("libc.so.6")
        pr_set_pdeathsig = 1
        libc.prctl(pr_set_pdeathsig, signal.SIGTERM)
    except Exception:
        pass

def setup_database():
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cache_ranges_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path_hash TEXT,
                start_ms INTEGER,
                end_ms INTEGER,
                updated_at DATETIME
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_path_hash ON cache_ranges_v2 (path_hash)')
        conn.commit()
        conn.close()

def is_offset_cached(path_hash, offset_ms):
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1 FROM cache_ranges_v2
            WHERE path_hash = ? AND start_ms <= ? AND end_ms >= ?
            LIMIT 1
        ''', (path_hash, offset_ms, offset_ms))
        result = cursor.fetchone()
        conn.close()
        return result is not None

def get_cached_range_end(path_hash, offset_ms):
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT end_ms FROM cache_ranges_v2
            WHERE path_hash = ? AND start_ms <= ? AND end_ms >= ?
            ORDER BY end_ms DESC
            LIMIT 1
        ''', (path_hash, offset_ms, offset_ms))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_furthest_cached_end(path_hash, offset_ms):
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT MAX(end_ms) FROM cache_ranges_v2
            WHERE path_hash = ? AND end_ms > ?
        ''', (path_hash, offset_ms))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else None

def get_cache_resume_offset(path_hash, offset_ms, required_end_ms=None):
    cached_end = get_cached_range_end(path_hash, offset_ms)
    if cached_end is None:
        cached_end = get_furthest_cached_end(path_hash, offset_ms)
    if cached_end is None:
        return offset_ms, None

    if required_end_ms is not None and cached_end >= required_end_ms:
        return None, cached_end
    if required_end_ms is None:
        return max(offset_ms, cached_end), cached_end

    return max(offset_ms, cached_end), cached_end

def insert_new_range(path_hash, start_ms):
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            INSERT INTO cache_ranges_v2 (path_hash, start_ms, end_ms, updated_at)
            VALUES (?, ?, ?, ?)
        ''', (path_hash, start_ms, start_ms, now_str))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

def update_range_end(row_id, end_ms):
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('UPDATE cache_ranges_v2 SET end_ms = ?, updated_at = ? WHERE id = ?', (end_ms, now_str, row_id))
        conn.commit()
        conn.close()

def merge_overlapping_ranges(path_hash):
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('SELECT id, start_ms, end_ms FROM cache_ranges_v2 WHERE path_hash = ? ORDER BY start_ms ASC', (path_hash,))
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return

        merged_ranges = []
        current_id, current_start, current_end = rows[0]
        ids_to_delete = []

        for row in rows[1:]:
            r_id, r_start, r_end = row
            if r_start <= current_end + 10000:
                current_end = max(current_end, r_end)
                ids_to_delete.append(r_id)
            else:
                merged_ranges.append((current_id, current_start, current_end))
                current_id, current_start, current_end = r_id, r_start, r_end

        merged_ranges.append((current_id, current_start, current_end))

        for m_id, m_start, m_end in merged_ranges:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('UPDATE cache_ranges_v2 SET start_ms = ?, end_ms = ?, updated_at = ? WHERE id = ?', (m_start, m_end, now_str, m_id))

        for d_id in ids_to_delete:
            cursor.execute('DELETE FROM cache_ranges_v2 WHERE id = ?', (d_id,))

        conn.commit()
        conn.close()

def clean_old_cache_db():
    with db_lock:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=CACHE_VALID_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('DELETE FROM cache_ranges_v2 WHERE updated_at < ?', (cutoff,))
        deleted_count = cursor.rowcount
        try: cursor.execute('DELETE FROM cache_ranges WHERE updated_at < ?', (cutoff,))
        except sqlite3.OperationalError: pass
        conn.commit()
        conn.close()
        if deleted_count > 0:
            logger.info(f"🧹 DB 정리: 만료된 캐시 구간 기록 {deleted_count}개 삭제 완료")

# ==========================================
# 프로세스 관리
# ==========================================
def kill_process_tree(pid, reason=None):
    if not pid: return
    if reason:
        process_stop_reasons[pid] = reason
    try:
        parent = psutil.Process(pid)
        processes = parent.children(recursive=True) + [parent]
        for proc in processes:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass

        _, alive = psutil.wait_procs(processes, timeout=FFMPEG_TERMINATE_GRACE_SECONDS)
        for proc in alive:
            try:
                proc.kill()
                if reason:
                    process_stop_reasons[proc.pid] = reason
            except psutil.NoSuchProcess:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=1.0)
        logger.debug(f"🛑 프로세스 종료 완료 (PID: {pid} | Reason: {reason or 'unspecified'})")
    except psutil.NoSuchProcess:
        process_stop_reasons.pop(pid, None)
    except Exception as e: logger.error(f"프로세스 종료 중 오류 발생: {e}")

def cleanup_zombie_processes():
    managed_pids = set()
    for sess in active_sessions.values():
        if sess.get('pid'): managed_pids.add(sess['pid'])
    managed_pids.update(precache_pids)
    managed_pids.update(managed_ffmpeg_pids)

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.name().lower() in ('ffmpeg', 'ffmpeg.exe'):
                cmdline = proc.cmdline() if proc.cmdline() else []
                if CACHE_PROCESS_TAG in ' '.join(cmdline) and proc.pid not in managed_pids:
                    logger.warning(f"🧹 [Janitor] 관리되지 않는 ffmpeg 프로세스 강제 종료 (PID: {proc.pid})")
                    kill_process_tree(proc.pid, "Janitor orphan cleanup")
        except (psutil.NoSuchProcess, psutil.AccessDenied): continue

def cleanup_tagged_ffmpeg_processes():
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.name().lower() in ('ffmpeg', 'ffmpeg.exe'):
                cmdline = proc.cmdline() if proc.cmdline() else []
                if CACHE_PROCESS_TAG in ' '.join(cmdline):
                    logger.info(f"🧹 캐시 ffmpeg 정리 (PID: {proc.pid})")
                    kill_process_tree(proc.pid, "캐시 프로세스 정리")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

def cleanup_stale_cache_ffmpeg_processes():
    stale_tags = ('plex_cacher_', 'plex_standalone_cache_')
    current_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.pid == current_pid or proc.name().lower() not in ('ffmpeg', 'ffmpeg.exe'):
                continue
            cmdline = proc.cmdline() if proc.cmdline() else []
            cmdline_text = ' '.join(cmdline)
            if any(tag in cmdline_text for tag in stale_tags):
                logger.warning(f"🧹 이전 캐시 ffmpeg 정리 (PID: {proc.pid})")
                kill_process_tree(proc.pid, "이전 인스턴스 정리")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

def cleanup_session(sess_key):
    if sess_key in active_sessions:
        session_data = active_sessions[sess_key]
        item_id = session_data['item_id']
        filename = session_data.get('filename', 'Unknown')
        offset_ms = session_data.get('offset', 0)
        media_path = session_data.get('path')

        logger.info(f"⏹️ 재생 중지/타임아웃 감지 (Session: {sess_key} | Item: {item_id} | File: {filename} | 마지막 위치: {ms_to_hms(offset_ms)})")

        if session_data.get('timer'): session_data['timer'].cancel()
        if session_data.get('pid'): kill_process_tree(session_data['pid'], "세션 종료")
        if media_path: merge_overlapping_ranges(get_path_hash(media_path))

        del active_sessions[sess_key]
        if not shutdown_event.is_set():
            with cache_control_lock:
                rebalance_main_caches_locked("세션 종료")

def cleanup_all_sessions():
    for sess_key in list(active_sessions.keys()):
        cleanup_session(sess_key)
    for pid in list(precache_pids):
        kill_process_tree(pid, "전체 정리")
        precache_pids.discard(pid)
    for pid in list(managed_ffmpeg_pids):
        kill_process_tree(pid, "전체 정리")
        managed_ffmpeg_pids.discard(pid)
    cleanup_tagged_ffmpeg_processes()

def request_shutdown(signum=None, frame=None):
    if shutdown_event.is_set():
        return
    signal_name = signal.Signals(signum).name if signum else "manual"
    logger.info(f"🛑 종료 신호 수신: {signal_name}. 캐시 프로세스를 정리합니다.")
    shutdown_event.set()
    if current_ws:
        try:
            current_ws.keep_running = False
            current_ws.close()
        except Exception:
            pass

def check_orphan_sessions():
    now = time.time()
    for sess_key in list(active_sessions.keys()):
        if now - active_sessions[sess_key]['last_seen'] > SESSION_TIMEOUT_SECONDS:
            logger.warning(f"🧹 세션 타임아웃 방치 감지 (Session: {sess_key}). 세션을 정리합니다.")
            cleanup_session(sess_key)

def periodic_janitor():
    while not shutdown_event.wait(JANITOR_INTERVAL_SECONDS):
        check_orphan_sessions()
        cleanup_zombie_processes()
        clean_old_cache_db()

# ==========================================
# 캐싱 제어
# ==========================================
def ffmpeg_monitor_thread(session_key, item_id, part_id, filename, media_path, offset_ms, limit_seconds=0, media_bitrate_kbps=0, readrate_override=None):
    path_hash = get_path_hash(media_path)
    row_id = insert_new_range(path_hash, offset_ms)
    should_rebalance_on_completion = False

    comment_tag = f"{CACHE_PROCESS_TAG}_{item_id}"
    is_precache = session_key.startswith("precache_")
    readrate = readrate_override if readrate_override is not None else calculate_readrate(media_bitrate_kbps, is_precache)
    command = build_ffmpeg_base_command(readrate)

    offset_time = '0'
    if offset_ms > 0:
        offset_time = str(timedelta(seconds=offset_ms / 1000))
        command += ['-ss', offset_time]
    if limit_seconds > 0:
        command += ['-t', str(limit_seconds)]

    null_output = 'NUL' if os.name == 'nt' else '/dev/null'
    command += ['-i', media_path, '-metadata', f'comment={comment_tag}', '-c', 'copy', '-n', '-f', 'null', null_output]

    cache_type = "미리보기(Next Ep)" if is_precache else "본편"
    readrate_label = f"{readrate:.2f}x" if ffmpeg_supports_readrate else "unlimited (-readrate 미지원)"
    logger.info(f"✅ [{cache_type} 캐시 시작] Item: {item_id} | Part: {part_id} | Offset: {offset_time} | Readrate: {readrate_label} | Path: {media_path}")

    try:
        if os.name == 'nt':
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, preexec_fn=prepare_child_process)

        managed_ffmpeg_pids.add(process.pid)
        if is_precache:
            precache_pids.add(process.pid)
        elif session_key in active_sessions:
            active_sessions[session_key]['pid'] = process.pid
            active_sessions[session_key]['cache_readrate'] = readrate
            active_sessions[session_key]['cache_start_ms'] = offset_ms

        last_db_update_time = time.time()
        last_elapsed_ms = 0
        error_lines = []
        dts_warning_count = 0
        for line in process.stderr:
            line = line.strip()
            if line.startswith(('out_time_us=', 'out_time_ms=')):
                try:
                    elapsed_ms = int(line.split('=', 1)[1]) // 1000
                except ValueError:
                    continue
                last_elapsed_ms = max(last_elapsed_ms, elapsed_ms)
                if time.time() - last_db_update_time >= 2.0:
                    update_range_end(row_id, offset_ms + last_elapsed_ms)
                    last_db_update_time = time.time()
            elif 'non monotonically increasing dts' in line:
                dts_warning_count += 1
            elif line and not line.startswith(('frame=', 'fps=', 'stream_', 'bitrate=', 'total_size=', 'out_time=', 'dup_frames=', 'drop_frames=', 'speed=', 'progress=')):
                error_lines.append(line)
                if len(error_lines) > 20:
                    error_lines.pop(0)
        process.wait()
        if last_elapsed_ms:
            update_range_end(row_id, offset_ms + last_elapsed_ms)

        stop_reason = process_stop_reasons.pop(process.pid, None)
        if shutdown_event.is_set() or stop_reason:
            reason_label = stop_reason or "스크립트 종료"
            logger.info(f"🛑 {cache_type} 프로세스 의도된 종료 (returncode: {process.returncode}) | Reason: {reason_label} | Item: {item_id} | File: {filename}")
        elif process.returncode == 0:
            if dts_warning_count:
                logger.info(f"🎉 {cache_type} 스트림 정상 완료! Item: {item_id} | File: {filename} | DTS 경고 {dts_warning_count}건 무시")
            else:
                logger.info(f"🎉 {cache_type} 스트림 정상 완료! Item: {item_id} | File: {filename}")
            should_rebalance_on_completion = not is_precache and limit_seconds <= 0
        else:
            if error_lines:
                logger.error(f"⚠️ {cache_type} 프로세스 실패 (returncode: {process.returncode}) | Item: {item_id} | File: {filename}\n" + "\n".join(error_lines))
            elif dts_warning_count:
                logger.warning(f"⚠️ {cache_type} 프로세스 종료 (returncode: {process.returncode}) | DTS 경고 {dts_warning_count}건만 감지됨 | Item: {item_id} | File: {filename}")
            else:
                logger.warning(f"⚠️ {cache_type} 프로세스 중단 (returncode: {process.returncode}) | stderr 메시지 없음")
        merge_overlapping_ranges(path_hash)

    except Exception as e: logger.error(f"ffmpeg 모니터링 중 에러 발생: {e}")
    finally:
        if is_precache and 'process' in locals(): precache_pids.discard(process.pid)
        if 'process' in locals():
            managed_ffmpeg_pids.discard(process.pid)
            if not is_precache and session_key in active_sessions and active_sessions[session_key].get('pid') == process.pid:
                active_sessions[session_key]['pid'] = None
                active_sessions[session_key].pop('cache_readrate', None)
                active_sessions[session_key].pop('cache_start_ms', None)
        if should_rebalance_on_completion and not shutdown_event.is_set() and session_key in active_sessions:
            logger.info(f"✅ 본편 캐시 완료로 재분배 요청 | Session: {session_key} | Item: {item_id} | File: {filename}")
            with cache_control_lock:
                rebalance_main_caches_locked("본편 캐시 완료")

def start_cache_process(session_key, item_id, part_id, filename, media_path, offset_ms, limit_seconds=0, media_bitrate_kbps=0, readrate_override=None):
    threading.Thread(
        target=ffmpeg_monitor_thread,
        args=(session_key, item_id, part_id, filename, media_path, offset_ms, limit_seconds, media_bitrate_kbps, readrate_override),
        daemon=True
    ).start()

def get_cache_plan(path_hash, offset_ms, limit_seconds=0, duration_ms=0):
    if limit_seconds > 0:
        required_end_ms = offset_ms + (limit_seconds * 1000)
        skip_label = f"{limit_seconds}초 이상"
    elif duration_ms and duration_ms > 0:
        required_end_ms = max(0, duration_ms - (CACHE_END_TOLERANCE_SECONDS * 1000))
        skip_label = "파일 끝까지"
    else:
        required_end_ms = None
        skip_label = "기존 캐시 구간"
    resume_offset_ms, cached_end_ms = get_cache_resume_offset(path_hash, offset_ms, required_end_ms)
    return resume_offset_ms, cached_end_ms, skip_label

def rebalance_main_caches_locked(reason=""):
    plans = []
    for sess_key, sess in active_sessions.items():
        media_path = sess.get('path')
        if not media_path or not os.path.exists(media_path):
            continue
        item_id = sess.get('item_id')
        part_id = sess.get('part_id')
        filename = sess.get('filename', 'Unknown')
        offset_ms = sess.get('offset', 0)
        duration_ms = sess.get('duration', 0)
        bitrate_kbps = sess.get('bitrate_kbps', 0)
        path_hash = get_path_hash(media_path)
        resume_offset_ms, cached_end_ms, skip_label = get_cache_plan(path_hash, offset_ms, duration_ms=duration_ms)
        plans.append((sess_key, sess, item_id, part_id, filename, media_path, offset_ms, duration_ms, bitrate_kbps, resume_offset_ms, cached_end_ms, skip_label))

    if not plans:
        return

    runnable_plans = [plan for plan in plans if plan[9] is not None]
    if not runnable_plans:
        for _, _, _, part_id, filename, _, offset_ms, _, _, _, _, skip_label in plans:
            logger.info(f"✅ [스킵] 요청 위치({ms_to_hms(offset_ms)})부터 {skip_label} 기캐싱됨 (Part: {part_id} | File: {filename})")
        for _, sess, *_ in plans:
            old_pid = sess.get('pid')
            if old_pid:
                kill_process_tree(old_pid, "기캐싱 완료")
                sess['pid'] = None
        return

    active_count = len(runnable_plans)
    per_cache_bandwidth = CACHE_MAX_BANDWIDTH_MBPS / active_count
    logger.info(f"⚖️ 본편 캐시 재분배: {active_count}개 세션, 세션당 목표 {per_cache_bandwidth:.2f}Mbps" + (f" ({reason})" if reason else ""))

    runnable_keys = {plan[0] for plan in runnable_plans}
    for sess_key, sess, *_ in plans:
        if sess_key not in runnable_keys and sess.get('pid'):
            kill_process_tree(sess['pid'], "기캐싱 완료")
            sess['pid'] = None

    for sess_key, sess, item_id, part_id, filename, media_path, offset_ms, _, bitrate_kbps, resume_offset_ms, cached_end_ms, _ in runnable_plans:
        formatted_offset = ms_to_hms(offset_ms)

        readrate = calculate_readrate(bitrate_kbps, is_precache=False, max_bandwidth_mbps=per_cache_bandwidth)
        old_pid = sess.get('pid')
        old_readrate = sess.get('cache_readrate')
        old_start_ms = sess.get('cache_start_ms')
        can_keep_existing = (
            old_pid
            and psutil.pid_exists(old_pid)
            and old_readrate is not None
            and abs(old_readrate - readrate) < 0.01
            and old_start_ms is not None
            and old_start_ms <= offset_ms
            and cached_end_ms is not None
            and cached_end_ms >= offset_ms
        )
        if can_keep_existing:
            logger.debug(f"↔️ 기존 본편 캐시 유지 (Session: {sess_key} | PID: {old_pid} | Readrate: {readrate:.2f}x | File: {filename})")
            continue

        if old_pid:
            kill_process_tree(old_pid, reason or "본편 캐시 재분배")
            sess['pid'] = None

        if cached_end_ms is not None and cached_end_ms > offset_ms:
            logger.info(f"🔍 캐시 부족! 요청 위치({formatted_offset})는 일부 캐싱됨, 이어받기 위치({ms_to_hms(resume_offset_ms)})부터 캐싱을 시작합니다.")
        else:
            logger.info(f"🔍 공백 발견! 해당 위치({formatted_offset})부터 캐싱을 시작합니다.")
        start_cache_process(sess_key, item_id, part_id, filename, media_path, resume_offset_ms, media_bitrate_kbps=bitrate_kbps, readrate_override=readrate)

def start_ffmpeg_cache(session_key, item_id, part_id, filename, media_path, offset_ms, limit_seconds=0, media_bitrate_kbps=0, duration_ms=0):
    if not media_path or not os.path.exists(media_path):
        logger.error(f"❌ 파일을 찾을 수 없어 캐싱을 시작할 수 없습니다: {media_path}")
        return

    path_hash = get_path_hash(media_path)
    formatted_offset = ms_to_hms(offset_ms)
    resume_offset_ms, cached_end_ms, skip_label = get_cache_plan(path_hash, offset_ms, limit_seconds, duration_ms)

    if resume_offset_ms is None:
        logger.info(f"✅ [스킵] 요청 위치({formatted_offset})부터 {skip_label} 기캐싱됨 (Part: {part_id} | File: {filename})")
        return

    if limit_seconds <= 0 and session_key in active_sessions:
        with cache_control_lock:
            sess = active_sessions[session_key]
            sess.update({
                'item_id': item_id, 'part_id': part_id, 'filename': filename,
                'offset': offset_ms, 'duration': duration_ms, 'path': media_path,
                'bitrate_kbps': media_bitrate_kbps
            })
            rebalance_main_caches_locked("본편 캐시 시작")
        return

    if cached_end_ms is not None and cached_end_ms > offset_ms:
        logger.info(f"🔍 캐시 부족! 요청 위치({formatted_offset})는 일부 캐싱됨, 이어받기 위치({ms_to_hms(resume_offset_ms)})부터 캐싱을 시작합니다.")
    else:
        logger.info(f"🔍 공백 발견! 해당 위치({formatted_offset})부터 캐싱을 시작합니다.")

    start_cache_process(session_key, item_id, part_id, filename, media_path, resume_offset_ms, limit_seconds, media_bitrate_kbps)

def execute_cache_restart(session_key, item_id, part_id, filename, media_path, offset_ms, media_bitrate_kbps=0, duration_ms=0):
    path_hash = get_path_hash(media_path)
    formatted_offset = ms_to_hms(offset_ms)
    logger.debug(f"🚀 [캐시 재시작 확인] Item: {item_id} | Part: {part_id} | Offset: {formatted_offset}")

    if session_key in active_sessions:
        with cache_control_lock:
            session_data = active_sessions[session_key]
            session_data['timer'] = None
            session_data['is_suspended'] = False
            session_data.update({
                'item_id': item_id, 'part_id': part_id, 'filename': filename,
                'offset': offset_ms, 'duration': duration_ms, 'path': media_path,
                'bitrate_kbps': media_bitrate_kbps
            })
            rebalance_main_caches_locked(f"탐색 위치 {formatted_offset}")

# ==========================================
# [개편] Plex API 호출 연동 (공식/비공식 클라이언트 완벽 호환)
# ==========================================
def fetch_session_details(target_sess_key, target_item_id, client_identifier=""):
    """
    MPV(비공식)의 경우: Client-Identifier에 숨겨둔 _PART_###_ 암호를 해독합니다.
    Web/App(공식)의 경우: 암호가 없으므로 Plex 세션의 decision/selected 속성으로 정확히 추적합니다.
    """
    target_part_id = None

    # 1. MPV 트로이 목마 해석
    if client_identifier:
        match = re.search(r'_PART_(\d+)_', client_identifier)
        if match:
            target_part_id = int(match.group(1))
            logger.debug(f"🕵️ MPV 암호 해독 완료! 요청된 Part ID: {target_part_id}")

    try:
        full_item = plex_server.fetchItem(int(target_item_id))
        duration_ms = getattr(full_item, 'duration', 0)

        # 2. 암호를 해독했다면 즉시 해당 파일 리턴
        if target_part_id:
            for m in full_item.media:
                for p in m.parts:
                    if p.id == target_part_id:
                        raw_path = p.file
                        filename = os.path.basename(raw_path) if raw_path else "Unknown"
                        return get_mapped_path(raw_path), duration_ms, target_part_id, filename, get_media_bitrate_kbps(m)

        # 3. 공식 클라이언트 (웹 플레이어 등) 폴백 로직
        for s in plex_server.sessions():
            if str(s.sessionKey) == target_sess_key:
                raw_path, playing_part_id, playing_bitrate_kbps = None, None, 0
                if s.media:
                    for m in s.media:
                        for p in m.parts:
                            if getattr(p, 'decision', None) or getattr(p, 'selected', False):
                                playing_part_id = p.id
                                raw_path = p.file
                                playing_bitrate_kbps = get_media_bitrate_kbps(m)
                                break
                        if playing_part_id: break
                    if not playing_part_id and s.media[0].parts:
                        playing_part_id = s.media[0].parts[0].id
                        raw_path = s.media[0].parts[0].file
                        playing_bitrate_kbps = get_media_bitrate_kbps(s.media[0])

                if not raw_path and playing_part_id:
                    for m in full_item.media:
                        for p in m.parts:
                            if p.id == playing_part_id:
                                raw_path = p.file
                                playing_bitrate_kbps = get_media_bitrate_kbps(m)
                                break
                        if raw_path: break

                filename = os.path.basename(raw_path) if raw_path else "Unknown"
                return get_mapped_path(raw_path), duration_ms, playing_part_id, filename, playing_bitrate_kbps

    except Exception as e:
        logger.error(f"세션 세부 정보 조회 중 API 오류: {e}")

    return None, None, None, None, 0

def trigger_precache(sess_key, item_id, progress):
    def _task():
        try:
            full_item = plex_server.fetchItem(int(item_id))
            if full_item.type != 'episode': return

            all_eps = full_item.show().episodes()
            current_idx = next((i for i, ep in enumerate(all_eps) if str(ep.ratingKey) == item_id), -1)

            if current_idx != -1 and current_idx + 1 < len(all_eps):
                next_ep = all_eps[current_idx + 1]
                next_id = str(next_ep.ratingKey)

                if next_ep.media and next_ep.media[0].parts:
                    next_bitrate_kbps = get_media_bitrate_kbps(next_ep.media[0])
                    next_part_id = next_ep.media[0].parts[0].id
                    next_raw_path = next_ep.media[0].parts[0].file
                    next_filename = os.path.basename(next_raw_path) if next_raw_path else "Unknown"
                    next_media_path = get_mapped_path(next_raw_path)

                    if next_media_path:
                        next_path_hash = get_path_hash(next_media_path)
                        if not is_offset_cached(next_path_hash, 0):
                            logger.info(f"⏭️ 정주행 도달({progress*100:.1f}%)! 다음 화({next_id}) 초반부 백그라운드 캐싱을 시작합니다.")
                            start_ffmpeg_cache(
                                session_key=f"precache_{next_id}", item_id=next_id,
                                part_id=next_part_id, filename=next_filename,
                                media_path=next_media_path, offset_ms=0,
                                limit_seconds=PRECACHE_DURATION_SECONDS,
                                media_bitrate_kbps=next_bitrate_kbps
                            )
        except Exception as e: logger.error(f"⚠️ 프리캐싱 준비 중 오류 발생: {e}")
    threading.Thread(target=_task, daemon=True).start()

# ==========================================
# 웹소켓 리스너
# ==========================================
def start_websocket_listener():
    global current_ws
    ws_url = PLEX_URL.replace('http://', 'ws://').replace('https://', 'wss://') + f'/:/websockets/notifications?X-Plex-Token={PLEX_TOKEN}'

    def on_message(ws, message):
        if shutdown_event.is_set():
            return
        try:
            data = json.loads(message)
            container = data.get('NotificationContainer', {})

            if container.get('type') == 'playing':
                for session in container.get('PlaySessionStateNotification', []):
                    state = session.get('state')
                    sess_key = str(session.get('sessionKey'))
                    item_id = str(session.get('ratingKey'))
                    offset_ms = session.get('viewOffset', 0)
                    client_id = session.get('clientIdentifier', '')

                    if state in ['stopped', 'error']:
                        cleanup_session(sess_key)
                        continue

                    if state in ['playing', 'buffering', 'paused']:
                        current_time = time.time()

                        if sess_key in active_sessions:
                            session_data = active_sessions[sess_key]
                            session_data['offset'] = offset_ms
                            session_data['last_seen'] = current_time
                            s_part_id = session_data.get('part_id', 'Unknown')
                            s_filename = session_data.get('filename', 'Unknown')
                            s_bitrate = session_data.get('bitrate_kbps', 0)
                            duration_ms = session_data.get('duration', 0)

                            if PAUSE_CACHE_ON_PLEX_PAUSE:
                                pid = session_data.get('pid')
                                if state == 'paused' and pid and not session_data.get('is_suspended'):
                                    try:
                                        psutil.Process(pid).suspend()
                                        session_data['is_suspended'] = True
                                        logger.info(f"⏸️ 일시정지 감지: 프로세스(PID: {pid}) 일시정지 (Session: {sess_key})")
                                    except Exception: pass
                                elif state in ['playing', 'buffering'] and session_data.get('is_suspended'):
                                    if pid:
                                        try:
                                            psutil.Process(pid).resume()
                                            session_data['is_suspended'] = False
                                            logger.info(f"▶️ 재생 재개: 프로세스(PID: {pid}) 작동 재개 (Session: {sess_key})")
                                        except Exception: pass

                            history = session_data.setdefault('history', [])
                            history.append((current_time, offset_ms))
                            history = [entry for entry in history if current_time - entry[0] <= SEEK_DETECT_SECONDS]
                            session_data['history'] = history

                            seek_detected = False
                            detected_skip_duration, detected_time_diff = 0, 0

                            for old_time, old_offset in history:
                                time_diff = current_time - old_time
                                if time_diff < 0.1: continue
                                offset_diff = abs(offset_ms - old_offset) / 1000.0

                                if offset_diff >= SEEK_DETECT_SECONDS and (offset_diff / time_diff) >= SEEK_DETECT_SPEED_MULTIPLIER:
                                    seek_detected = True
                                    detected_skip_duration, detected_time_diff = offset_diff, time_diff
                                    break

                            if seek_detected:
                                skip_duration_str = str(timedelta(seconds=int(detected_skip_duration)))
                                logger.info(f"⏩ 탐색 감지! ({detected_time_diff:.1f}초 동안 {skip_duration_str} 이동) - Session: {sess_key} | File: {s_filename} | 위치: {ms_to_hms(offset_ms)}")

                                if session_data.get('timer'): session_data['timer'].cancel()
                                timer = threading.Timer(DEBOUNCE_SECONDS, execute_cache_restart, args=[sess_key, item_id, s_part_id, s_filename, session_data['path'], offset_ms, s_bitrate, duration_ms])
                                session_data['timer'] = timer
                                timer.start()
                                session_data['history'] = [(current_time, offset_ms)]

                            if state == 'playing' and duration_ms and duration_ms > 0:
                                if (offset_ms / duration_ms) >= PRECACHE_TRIGGER_PERCENT and not session_data.get('precache_triggered'):
                                    session_data['precache_triggered'] = True
                                    trigger_precache(sess_key, item_id, (offset_ms / duration_ms))
                        else:
                            # 신규 재생 감지 시, 웹소켓의 client_id를 함수로 넘겨서 분석
                            media_path, duration_ms, p_id, f_name, bitrate_kbps = fetch_session_details(sess_key, item_id, client_id)
                            if not media_path: return

                            logger.info(f"▶️ 재생 감지 (Session: {sess_key} | Item: {item_id} | Part: {p_id} | File: {f_name} | 위치: {ms_to_hms(offset_ms)})")
                            active_sessions[sess_key] = {
                                'item_id': item_id, 'part_id': p_id, 'filename': f_name,
                                'offset': offset_ms, 'duration': duration_ms,
                                'pid': None, 'timer': None, 'path': media_path, 'precache_triggered': False,
                                'last_seen': time.time(), 'is_suspended': False, 'history': [(time.time(), offset_ms)],
                                'bitrate_kbps': bitrate_kbps
                            }
                            start_ffmpeg_cache(sess_key, item_id, p_id, f_name, media_path, offset_ms, media_bitrate_kbps=bitrate_kbps, duration_ms=duration_ms)

        except json.JSONDecodeError: pass
        except Exception as e: logger.error(f"웹소켓 파싱 오류: {e}")

    def on_error(ws, error):
        if shutdown_event.is_set():
            return
        error_msg = str(error)
        ignore_patterns = ["opcode=8", "fin=1", "goodbye", "Connection to remote host was lost", "ping/pong timed out"]
        if any(p in error_msg for p in ignore_patterns): logger.debug(f"정상 종료/유휴 단절: {error_msg}")
        else: logger.error(f"⚠️ 웹소켓 에러: {error_msg}")

    def on_close(ws, close_status_code, close_msg):
        if shutdown_event.is_set():
            logger.debug("🔌 웹소켓 연결 종료됨.")
        else:
            logger.debug(f"🔌 웹소켓 연결 종료됨. 재연결 시도...")
            shutdown_event.wait(WEBSOCKET_RECONNECT_DELAY)

    def on_open(ws): logger.debug("📡 Plex 웹소켓 연결 완료.")

    while not shutdown_event.is_set():
        ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        current_ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=5)
    current_ws = None

def init_plex():
    global plex_server
    try:
        plex_server = PlexServer(PLEX_URL, PLEX_TOKEN)
        logger.info(f"✅ Plex API 서버 연결 성공: {PLEX_URL}")
        return True
    except Exception as e:
        logger.error(f"❌ Plex 서버 연결 실패: {e}")
        return False

if __name__ == "__main__":
    logger.info(f"🚀 Plex 단독 캐싱 스크립트 시작 (로그 레벨: {LOG_LEVEL})")
    logger.info(f"🔖 캐시 인스턴스 ID: {INSTANCE_ID}")
    setup_database()
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    if CLEAN_STALE_FFMPEG_ON_START:
        cleanup_stale_cache_ffmpeg_processes()
    if not init_ffmpeg_capabilities(): exit(1)
    if not init_plex(): exit(1)
    threading.Thread(target=periodic_janitor, daemon=True).start()
    try:
        start_websocket_listener()
    except KeyboardInterrupt:
        request_shutdown()
    finally:
        shutdown_event.set()
        cleanup_all_sessions()
        logger.info("👋 Plex 단독 캐싱 스크립트 종료 완료")
