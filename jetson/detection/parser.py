"""Parse Ultralytics YOLO OBB results into (display, assist) detection lists.

display — top-N per class at the high confidence threshold (drawn on screen)
assist  — all detections above the low (assist) threshold — full pool for
          AI acquisition / AI track assist, independent of display top_n.
"""


def parse_detections(results, display_conf, assist_conf, cfg):
    """
    Classify YOLO OBB detections into vehicle/person groups and filter.
    `cfg` is the full processor config — we read detection.vehicle_names,
    detection.person_names, and detection.top_n.
    Returns (display_dets, assist_dets).
    Each det is a dict: {"poly": np.ndarray, "conf": float, "label": str}.
    """
    vehicles = []
    persons  = []
    other    = []
    veh_set = set(n.lower() for n in cfg["detection"]["vehicle_names"])
    per_set = set(n.lower() for n in cfg["detection"]["person_names"])
    top_n   = int(cfg["detection"]["top_n"])

    for result in results:
        if result.obb is None or len(result.obb) == 0:
            continue
        polys   = result.obb.xyxyxyxy.cpu().numpy()
        confs   = result.obb.conf.cpu().numpy()
        classes = result.obb.cls.cpu().numpy().astype(int)
        names   = result.names
        for poly, conf, cls in zip(polys, confs, classes):
            label = str(names[cls]).lower()
            det = {"poly": poly, "conf": float(conf), "label": names[cls]}
            if any(v in label for v in veh_set):
                vehicles.append(det)
            elif any(p in label for p in per_set):
                persons.append(det)
            else:
                other.append(det)

    vehicles.sort(key=lambda d: d["conf"], reverse=True)
    persons.sort(key=lambda d: d["conf"], reverse=True)

    if vehicles == [] and persons == [] and other:
        seen = sorted({d["label"] for d in other})
        print(f"[PROC]  WARN: {len(other)} detections present but no class "
              f"matched vehicle/person lists. Seen labels: {seen}  "
              f"veh_set={veh_set}  per_set={per_set}")

    def top(src, thresh):
        return [d for d in src if d["conf"] >= thresh][:top_n]

    def all_above(src, thresh):
        return [d for d in src if d["conf"] >= thresh]

    display = top(vehicles, display_conf) + top(persons, display_conf)
    assist  = all_above(vehicles, assist_conf) + all_above(persons, assist_conf)
    return display, assist
