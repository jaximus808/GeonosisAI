"""TDM-55 spike: stream server over a cloudflared quick tunnel with *textured* synthetic frames.

The stock synthetic cameras are flat colour (~6 KB JPEGs); real scenes at 424 px / q70 are ~15-30 KB.
This adds per-frame noise texture so the latency test pays realistic bandwidth. Also exercises
``start_stream(..., tunnel="cloudflared")`` (the API TDM-49 calls).

    cd services/robot-client && uv run python ../../spikes/tdm-55-stream/heavy_serve.py --noise 40
"""
from __future__ import annotations

import argparse
import json
import logging
import time

import numpy as np

from rdf_robot_client.cameras import CameraRig, SyntheticSource, synthetic_config, load_config
from rdf_robot_client.stream import start_stream


class TexturedSource(SyntheticSource):
    def __init__(self, cfg, noise: int):
        super().__init__(cfg)
        self.noise = noise
        h, w = cfg.height, cfg.width
        self._tex = np.random.default_rng(1).integers(0, noise, (h * 2, w * 2, 3), dtype=np.uint8)

    def read(self):
        img, t = super().read()
        if img is None:
            return img, t
        h, w = img.shape[:2]
        o = (self.k * 7) % h
        return np.clip(img.astype(np.int16) + self._tex[o:o + h, o:o + w] - self.noise // 2, 0, 255).astype(np.uint8), t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--noise", type=int, default=40)
    ap.add_argument("--port", type=int, default=8194)
    ap.add_argument("--tunnel", default="cloudflared")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--in-flight", type=int, default=2)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    rig = CameraRig(synthetic_config(load_config()), source_factory=lambda c: TexturedSource(c, a.noise)).start()
    rig.wait_ready(5)
    h = start_stream(rig, robot_id=1, port=a.port, tunnel=a.tunnel, width=a.width, max_in_flight=a.in_flight)
    print(json.dumps(h.info()), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        h.stop()
        rig.stop()


if __name__ == "__main__":
    main()
