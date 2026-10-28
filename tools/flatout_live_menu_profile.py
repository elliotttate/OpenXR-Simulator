"""Record native timing and hover telemetry while a human controls an owned game.

No input injection, screenshots, RenderDoc, navigation or setting changes.
"""
import argparse
import json
from pathlib import Path
import statistics
import struct
import time

from flatout_ui_atlas import bounded_pipe_read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--windows', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--wait-for-tracking-seconds', type=int, default=0, choices=range(0, 601))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    operations = []

    def call(method, params=None):
        assert method in {'perf.capture_start', 'perf.capture_read', 'vr.targets', 'vr.get_state', 'game.errors', 'menu.get_state'}
        started = time.time()
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).encode()
        with open(r'\\.\pipe\FlatoutMCP_' + str(args.pid), 'r+b', buffering=0) as pipe:
            pipe.write(struct.pack('<I', len(request)) + request)
            deadline = time.monotonic() + 15
            size = struct.unpack('<I', bounded_pipe_read(pipe, 4, deadline))[0]
            assert 0 < size < 32*1024*1024
            response = json.loads(bounded_pipe_read(pipe, size, deadline))
        operations.append({'method': method, 'started_unix': started, 'finished_unix': time.time()})
        assert 'error' not in response, response
        result = response['result']
        assert 'error' not in result, result
        return result

    until = time.monotonic() + 90
    while True:
        try:
            vr = call('vr.get_state')
            assert 'SteamVR' in vr.get('runtime', ''), vr.get('runtime')
            break
        except FileNotFoundError:
            if time.monotonic() >= until:
                raise
            time.sleep(.5)
    (args.output / 'runtime.json').write_text(json.dumps(vr, indent=2), encoding='utf-8')
    if args.wait_for_tracking_seconds:
        until = time.monotonic() + args.wait_for_tracking_seconds
        print('WAITING for a tracked controller before starting timed recording', flush=True)
        while not any(controller.get('valid') for controller in vr.get('controllers', [])):
            if time.monotonic() >= until:
                (args.output / 'not-recorded.json').write_text(json.dumps({'reason': 'No tracked controller before deadline'}), encoding='utf-8')
                return
            time.sleep(1)
            vr = call('vr.get_state')
        (args.output / 'runtime-tracked.json').write_text(json.dumps(vr, indent=2), encoding='utf-8')
    for window in range(args.windows):
        capture = call('perf.capture_start', {'scope': 'transition', 'duration_ms': 60000,
            'max_frames': 7500, 'hitch_detail_threshold_ms': 25, 'max_hitch_details': 32,
            'hitch_thread_mode': 'all_cpu'})
        print(f'RECORDING window {window+1}/{args.windows}, PID {args.pid}', flush=True)
        hover_samples = []
        while True:
            hover = call('vr.targets')
            hover_samples.append({'unix': time.time(), 'menu': hover.get('menu'),
                'hovered': hover.get('hovered'), 'clicks': hover.get('clicks')})
            (args.output / f'window-{window+1}-hover.json').write_text(json.dumps(hover_samples), encoding='utf-8')
            state = call('perf.capture_read', {'limit': 0})
            if not state['active']:
                break
            time.sleep(1)
        first = call('perf.capture_read', {'limit': 2000, 'hitch_details': True})
        offset = first['next_offset']
        while offset < first['count']:
            page = call('perf.capture_read', {'offset': offset, 'limit': 2000})
            first['rows'].extend(page['rows'])
            offset = page['next_offset']
        (args.output / f'window-{window+1}.json').write_text(json.dumps(first), encoding='utf-8')
        summary = {'window': window+1, 'frames': first['count'], 'metrics': {}}
        for metric in ('frame_ms', 'cpu_main_wall_ms', 'cpu_render_wall_ms', 'gpu_ms'):
            col = first['columns'].index(metric)
            values = sorted(row[col] for row in first['rows'] if row[col] is not None)
            if values:
                summary['metrics'][metric] = {'median': statistics.median(values),
                    'p95': values[int((len(values)-1)*.95)], 'max': max(values)}
        summary['hitches'] = [{k: hitch[k] for k in ('frame_id', 'elapsed_ms', 'frame_ms')}
                              for hitch in first.get('hitches', [])]
        (args.output / f'window-{window+1}-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        (args.output / 'observer-operations.json').write_text(json.dumps(operations), encoding='utf-8')
        print(json.dumps(summary), flush=True)
    (args.output / 'native-errors.json').write_text(json.dumps(call('game.errors'), indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
