from __future__ import annotations

import argparse
import json
import time
import urllib.request


VEHICLE_LABELS = {
    "bicycle": "bicycles",
    "car": "cars",
    "motorcycle": "motorcycles",
    "bus": "buses",
    "train": "trains",
    "truck": "trucks",
}
VEHICLE_CLASS_IDS = [1, 2, 3, 5, 6, 7]


def post_ai_update(api_url: str, payload: dict) -> None:
    request = urllib.request.Request(
        api_url.rstrip("/") + "/api/ai-traffic-update",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        response.read()


def signal_for_density(density: int, average_density: int) -> str:
    if density >= 65:
        return "go"
    if average_density >= 35:
        return "slow"
    return "ai"


def run_model(args: argparse.Namespace) -> None:
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("Install dependencies first: python -m pip install opencv-python ultralytics") from exc

    model = YOLO(args.weights)
    source = int(args.source) if str(args.source).isdigit() else args.source
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise SystemExit(f"Could not open video source: {args.source}")

    last_post = 0.0
    print(f"AI model reading {args.source}")
    print(f"Posting updates to {args.api_url}/api/ai-traffic-update as {args.lane}")

    while True:
        ok, frame = capture.read()
        if not ok:
            time.sleep(1)
            capture.release()
            capture = cv2.VideoCapture(source)
            continue

        if time.time() - last_post < args.interval:
            continue

        results = model.predict(frame, imgsz=args.image_size, conf=args.confidence, classes=VEHICLE_CLASS_IDS, verbose=False)
        counts = {value: 0 for value in VEHICLE_LABELS.values()}
        confidence_scores = []
        for result in results:
            names = result.names
            for box in result.boxes:
                label = names[int(box.cls[0])]
                if label not in VEHICLE_LABELS:
                    continue
                counts[VEHICLE_LABELS[label]] += 1
                confidence_scores.append(float(box.conf[0]))

        total = sum(counts.values())
        density = min(100, round(total / max(1, args.max_vehicles) * 100))
        avg_confidence = round(sum(confidence_scores) / len(confidence_scores) * 100) if confidence_scores else 70
        recommended_signal = signal_for_density(density, density)
        green_seconds = max(15, min(120, 20 + density))
        payload = {
            "token": args.token,
            "lane": args.lane,
            "target_label": args.lane,
            "target_location": args.location,
            "vehicle_count": total,
            "vehicle_counts": counts,
            "density": density,
            "confidence": avg_confidence,
            "recommended_signal": recommended_signal,
            "green_seconds": green_seconds,
            "source": "yolo-ai-model",
        }
        post_ai_update(args.api_url, payload)
        print(
            f"{time.strftime('%H:%M:%S')} total={total} cars={counts['cars']} buses={counts['buses']} "
            f"trucks={counts['trucks']} motorcycles={counts['motorcycles']} density={density}% signal={recommended_signal}"
        )
        last_post = time.time()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a separate vehicle-counting AI model and update TrafficControl OS.")
    parser.add_argument("--source", required=True, help="Camera/video source, such as 0, video.mp4, or an ESP32-CAM stream URL")
    parser.add_argument("--api-url", default="http://127.0.0.1:5000", help="TrafficControl OS base URL")
    parser.add_argument("--token", default="dev-traffic-node", help="AI model token. Match IOT_NODE_TOKEN on the web app.")
    parser.add_argument("--lane", default="Eastbound camera", help="Traffic lane or signal node name")
    parser.add_argument("--location", default="AI model traffic node", help="Location shown in admin signal details")
    parser.add_argument("--weights", default="yolov8n.pt", help="YOLO weights path or model name")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between admin updates")
    parser.add_argument("--confidence", type=float, default=0.35, help="Minimum detection confidence")
    parser.add_argument("--image-size", type=int, default=640, help="YOLO inference image size")
    parser.add_argument("--max-vehicles", type=int, default=20, help="Vehicle count treated as 100 percent density")
    return parser.parse_args()


if __name__ == "__main__":
    run_model(parse_args())
