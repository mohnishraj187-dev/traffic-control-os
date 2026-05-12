from typing import List, Dict, Tuple

class Lane:
    def __init__(self, name: str, rect: Tuple[int,int,int,int]):
        # rect: x1,y1,x2,y2 in pixel coordinates
        self.name = name
        self.rect = rect

    def contains(self, point: Tuple[int,int]) -> bool:
        x, y = point
        x1, y1, x2, y2 = self.rect
        return x >= x1 and x <= x2 and y >= y1 and y <= y2


def load_lanes_from_config(cfg: Dict) -> List[Lane]:
    lanes = []
    for item in (cfg.get('lanes') or []):
        name = item.get('name') or item.get('id') or f"lane_{len(lanes)}"
        rect = item.get('rect') or item.get('bbox')
        if rect and len(rect) == 4:
            lanes.append(Lane(name, tuple(rect)))
    return lanes


def assign_centroid_to_lane(centroid: Tuple[int,int], lanes: List[Lane]) -> str:
    for lane in lanes:
        if lane.contains(centroid):
            return lane.name
    return 'unassigned'
