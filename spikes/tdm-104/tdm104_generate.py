"""TDM-104 proof: Claude writes a scene per task (rdf_common.llm; use RDF_LLM_BACKEND=claude_code locally),
each validated by rdf_common.scene_spec. Writes tmp/<slug>.json. Run from services/orchestrator:
    RDF_LLM_BACKEND=claude_code uv run python ../../spikes/tdm-104/tdm104_generate.py "task" ["task" ...]
"""
import json
import sys
import time
from pathlib import Path

from rdf_common import scene_spec as ss
from rdf_orchestrator.scene import generate_spec

out = Path(__file__).resolve().parent / "tmp"
out.mkdir(exist_ok=True)
for task in sys.argv[1:] or ["Pick up the cube and place it in the empty plate",
                             "stack the red block on the blue block"]:
    t0 = time.time()
    spec, info = generate_spec(task)
    p = out / f"{ss.slugify(task)}.json"
    p.write_text(json.dumps(spec, indent=1))
    print(f"{task!r}: {ss.describe_success(spec)} objects={[o['id'] for o in spec['objects']]} "
          f"attempts={info['attempts']} {time.time() - t0:.0f}s backend={info['usage'][-1].get('backend')} -> {p.name}")
    if spec.get("meta", {}).get("fixes"):
        print("   fixes:", spec["meta"]["fixes"])
