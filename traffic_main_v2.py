"""Prototype: YOLOv8 detection + optional DeepSORT + per-lane ROI counting + webhook sender
"""
import argparse
import time
import yaml
from pathlib import Path
import os
import csv
import json

import cv2
import numpy as np
from ultralytics import YOLO

from traffic_tracker import CentroidTracker
from traffic_webhook_client import send_webhook
from traffic_controller import SignalController
from traffic_roi import load_lanes_from_config, assign_centroid_to_lane

VEHICLE_CLASSES = {1, 2, 3, 5, 6, 7}  # COCO: bicycle, car, motorcycle, bus, train, truck
VEHICLE_COUNT_KEYS = {
    1: 'bicycles',
    2: 'cars',
    3: 'motorcycles',
    5: 'buses',
    6: 'trains',
    7: 'trucks',
}


def load_config(path=None):
    default = {
        'webhook_url': None,
        'model': 'yolov8n.pt',
        'source': 0,
        'post_interval': 5,
        'detection_conf': 0.4,
        'count_window_s': 60,
        'ai_api_url': None,
        'ai_token': 'dev-traffic-node',
        'dashboard_lane': 'Eastbound camera',
        'dashboard_location': 'AI model traffic node',
        'max_vehicles_for_density': 20
    }
    if path and Path(path).exists():
        with open(path) as f:
            user = yaml.safe_load(f)
        default.update(user or {})
    return default


def parse_results(results):
    out = []
    try:
        r = results[0]
        if hasattr(r, 'boxes'):
            b = r.boxes
            xyxy = b.xyxy.cpu().numpy()
            cls = b.cls.cpu().numpy()
            conf = b.conf.cpu().numpy()
            for i in range(len(xyxy)):
                out.append({'xyxy': xyxy[i].tolist(), 'conf': float(conf[i]), 'cls': int(cls[i])})
            return out
    except Exception:
        pass
    try:
        for res in results:
            for box in getattr(res, 'boxes').data:
                x1, y1, x2, y2, conf, cls = box.tolist()
                out.append({'xyxy':[x1,y1,x2,y2], 'conf':float(conf), 'cls':int(cls)})
    except Exception:
        pass
    return out


def xyxy_to_centroid(xyxy):
    x1,y1,x2,y2 = xyxy
    return (int((x1+x2)/2), int((y1+y2)/2))


def signal_for_controller_state(state):
    phase = state.get('phase', 'GREEN')
    if phase == 'GREEN':
        return 'go'
    if phase == 'YELLOW':
        return 'slow'
    if phase == 'ALL_RED':
        return 'stop'
    return 'ai'


def vehicle_class_counts(vehicle_dets):
    counts = {'cars': 0, 'buses': 0, 'trucks': 0, 'motorcycles': 0, 'bicycles': 0, 'trains': 0}
    for det in vehicle_dets:
        key = VEHICLE_COUNT_KEYS.get(int(det['cls']))
        if key:
            counts[key] += 1
    return counts


def clipped_box_area(xyxy, rect):
    x1, y1, x2, y2 = xyxy
    rx1, ry1, rx2, ry2 = rect
    ix1 = max(float(x1), float(rx1))
    iy1 = max(float(y1), float(ry1))
    ix2 = min(float(x2), float(rx2))
    iy2 = min(float(y2), float(ry2))
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    return (ix2 - ix1) * (iy2 - iy1)


def lane_occupancy(vehicle_dets, lanes):
    occupancy = {}
    for lane in lanes:
        x1, y1, x2, y2 = lane.rect
        lane_area = max(1.0, float((x2 - x1) * (y2 - y1)))
        occupied_area = sum(clipped_box_area(det['xyxy'], lane.rect) for det in vehicle_dets)
        occupancy[lane.name] = min(100, round((occupied_area / lane_area) * 100))
    return occupancy


def combined_density(vehicle_count, occupancy_percent, max_vehicles):
    count_density = min(100, round(vehicle_count / max(1, max_vehicles) * 100))
    return min(100, round(occupancy_percent * 0.6 + count_density * 0.4))


def build_ai_payload(cfg, vehicle_dets, per_lane_counts, per_lane_occupancy, total_unique, state):
    max_vehicles = max(1, int(cfg.get('max_vehicles_for_density', 20)))
    green_lane = state.get('active_direction')
    if state.get('phase') != 'GREEN':
        green_lane = state.get('pending_direction') or green_lane
    priority_lane = green_lane or cfg.get('dashboard_lane', 'Eastbound camera')
    priority_count = per_lane_counts.get(priority_lane, total_unique)
    priority_occupancy = per_lane_occupancy.get(priority_lane, max(per_lane_occupancy.values(), default=0))
    density = combined_density(priority_count, priority_occupancy, max_vehicles)
    counts = vehicle_class_counts(vehicle_dets)
    if sum(counts.values()) < total_unique:
        counts['cars'] += total_unique - sum(counts.values())
    return {
        'token': cfg.get('ai_token', 'dev-traffic-node'),
        'lane': cfg.get('dashboard_lane', green_lane or 'Eastbound camera'),
        'target_label': green_lane or cfg.get('dashboard_lane', 'Eastbound camera'),
        'target_location': cfg.get('dashboard_location', 'AI model traffic node'),
        'vehicle_count': total_unique,
        'vehicle_counts': counts,
        'density': density,
        'confidence': round(max([det['conf'] for det in vehicle_dets], default=0.7) * 100),
        'recommended_signal': signal_for_controller_state(state),
        'green_seconds': int(state.get('green_duration', max(15, min(120, 20 + density)))),
        'signal_phase': state.get('phase', 'GREEN'),
        'green_remaining': int(state.get('green_remaining', 0)),
        'signal_scores': state.get('scores', {}),
        'per_lane_counts': per_lane_counts,
        'per_lane_occupancy': per_lane_occupancy,
        'source': 'traffic-main-v2-yolo',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='YAML config path', default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    webhook = cfg.get('webhook_url')
    ai_api_url = cfg.get('ai_api_url')
    source = cfg.get('source', 0)
    model_path = cfg.get('model', 'yolov8n.pt')
    post_interval = cfg.get('post_interval', 5)
    detect_conf = cfg.get('detection_conf', 0.4)
    count_window = cfg.get('count_window_s', 60)

    lanes = load_lanes_from_config(cfg)
    print(f'Loaded {len(lanes)} lanes')

    print('Loading model:', model_path)
    model = YOLO(model_path)

    # try to import DeepSORT (optional)
    deepsort = None
    try:
        from deep_sort_realtime.deepsort_tracker import DeepSort
        deepsort = DeepSort(max_age=30)
        print('DeepSort available: using for tracking')
    except Exception:
        print('DeepSort not available: falling back to CentroidTracker')

    cap = cv2.VideoCapture(source)
    centroid_tracker = CentroidTracker(max_disappeared=30, max_distance=60)
    controller = SignalController(
        min_green=int(cfg.get('min_green', 15)),
        max_green=int(cfg.get('max_green', 75)),
        yellow_seconds=int(cfg.get('yellow_seconds', 3)),
        all_red_seconds=int(cfg.get('all_red_seconds', 2)),
    )

    # outputs and logging
    outputs_dir = cfg.get('outputs_dir', os.path.join(os.getcwd(), 'outputs'))
    os.makedirs(outputs_dir, exist_ok=True)
    csv_path = cfg.get('csv_path', os.path.join(outputs_dir, 'traffic_log.csv'))
    video_path = cfg.get('video_path', os.path.join(outputs_dir, 'annotated_output.mp4'))
    video_writer = None
    csv_file = open(csv_path, 'w', newline='', encoding='utf-8')
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(['timestamp', 'frame_idx', 'per_lane_counts_json', 'total_unique', 'signal_json'])

    last_post = 0
    frame_idx = 0
    # sliding window store: id -> (lane_name, last_seen_ts)
    active_ids = {}

    while True:
        ret, frame = cap.read()
        if not ret:
            print('No frame, exiting')
            break

        h, w = frame.shape[:2]
        img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        try:
            results = model.predict(img, conf=detect_conf, classes=sorted(VEHICLE_CLASSES), verbose=False)
        except Exception:
            results = model(img, conf=detect_conf, classes=sorted(VEHICLE_CLASSES), verbose=False)

        detections = parse_results(results)
        vehicle_dets = [d for d in detections if int(d['cls']) in VEHICLE_CLASSES and d['conf'] >= detect_conf]

        tracked_items = []  # list of tuples (id, centroid)

        if deepsort is not None and len(vehicle_dets) > 0:
            # prepare detections: [x1,y1,x2,y2,score, class]
            ds_inputs = []
            for d in vehicle_dets:
                x1,y1,x2,y2 = d['xyxy']
                ds_inputs.append([int(x1), int(y1), int(x2), int(y2), float(d['conf']), int(d['cls'])])
            try:
                tracks = deepsort.update_tracks(ds_inputs, frame=frame)
                for t in tracks:
                    if not t.is_confirmed():
                        continue
                    tid = t.track_id
                    ltrb = t.to_ltrb()
                    cx = int((ltrb[0]+ltrb[2])/2)
                    cy = int((ltrb[1]+ltrb[3])/2)
                    tracked_items.append((tid, (cx,cy)))
            except Exception:
                # API mismatch or error — fallback
                tracked_items = []
        if len(tracked_items) == 0:
            # fallback to centroid tracker using bbox centroids
            rects = [d['xyxy'] for d in vehicle_dets]
            tracked = centroid_tracker.update(rects)
            # tracked: list of (id, centroid)
            tracked_items = tracked

        now = time.time()
        # update active ids with lane assignment
        for tid, centroid in tracked_items:
            lane = assign_centroid_to_lane(centroid, lanes)
            active_ids[tid] = (lane, now)

        # purge old ids
        expired = [tid for tid,(lane,ts) in active_ids.items() if now - ts > count_window]
        for tid in expired:
            del active_ids[tid]

        # compute per-lane unique counts
        per_lane_sets = {}
        for tid,(lane,ts) in active_ids.items():
            per_lane_sets.setdefault(lane, set()).add(tid)
        per_lane_counts = {lane: len(s) for lane,s in per_lane_sets.items()}
        total_unique = len(active_ids)
        per_lane_occupancy = lane_occupancy(vehicle_dets, lanes)

        # decide signal based on simple split between first two lanes (if available)
        densities = {}
        if len(lanes) >= 2:
            max_vehicles = max(1, int(cfg.get('max_vehicles_for_density', 20)))
            densities = {
                'north_south': combined_density(per_lane_counts.get(lanes[0].name, 0), per_lane_occupancy.get(lanes[0].name, 0), max_vehicles),
                'east_west': combined_density(per_lane_counts.get(lanes[1].name, 0), per_lane_occupancy.get(lanes[1].name, 0), max_vehicles)
            }
        else:
            densities = { 'north_south': total_unique, 'east_west': 0 }
        state = controller.decide(densities)

        # draw
        for lane in lanes:
            x1,y1,x2,y2 = lane.rect
            cv2.rectangle(frame, (x1,y1),(x2,y2),(255,0,0),2)
            cv2.putText(frame, f"{lane.name}: {per_lane_counts.get(lane.name,0)}", (x1+5,y1+20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,0,0),2)

        for tid, centroid in tracked_items:
            cv2.circle(frame, tuple(centroid), 4, (0,255,0), -1)
            cv2.putText(frame, f'ID {tid}', (centroid[0]-10,centroid[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0),1)

        # overlay densities explicitly
        ns = densities.get('north_south', 0)
        ew = densities.get('east_west', 0)
        cv2.putText(frame, f"Density NS: {ns}  EW: {ew}", (10,60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,165,255),2)
        cv2.putText(frame, f"Total (unique window): {total_unique}", (10,30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255),2)

        # initialize video writer if requested
        try:
            if video_writer is None:
                fps = cfg.get('output_fps') or cap.get(cv2.CAP_PROP_FPS) or 20
                try:
                    fps = float(fps)
                except Exception:
                    fps = 20.0
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                video_writer = cv2.VideoWriter(video_path, fourcc, fps, (w, h))
                print('Saving annotated video to', video_path)
        except Exception as e:
            print('Video writer init failed:', e)
            video_writer = None

        # write annotated frame to output video
        if video_writer is not None:
            try:
                video_writer.write(frame)
            except Exception:
                pass

        # log CSV per frame
        try:
            csv_writer.writerow([now, frame_idx, json.dumps(per_lane_counts), total_unique, json.dumps(state)])
            csv_file.flush()
        except Exception:
            pass

        frame_idx += 1
        cv2.imshow('traffic', frame)

        if (webhook or ai_api_url) and (now - last_post) > post_interval:
            payload = build_ai_payload(cfg, vehicle_dets, per_lane_counts, per_lane_occupancy, total_unique, state)
            if webhook:
                legacy_payload = {
                'timestamp': now,
                'per_lane_counts': per_lane_counts,
                'total_unique_in_window_s': total_unique,
                'signal': state
                }
                code, text = send_webhook(webhook, legacy_payload)
                print('Webhook ->', code, text)
            if ai_api_url:
                code, text = send_webhook(ai_api_url.rstrip('/') + '/api/ai-traffic-update', payload)
                print('Website AI update ->', code, text)
            last_post = now

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

    try:
        if video_writer is not None:
            video_writer.release()
            print('Released video writer')
    except Exception:
        pass
    try:
        if csv_file:
            csv_file.close()
            print('CSV log saved to', csv_path)
    except Exception:
        pass


if __name__ == '__main__':
    main()
