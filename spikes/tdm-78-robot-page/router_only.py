"""TDM-78: serve only the robot-commands router (local testing; no bus, no handlers).

    SPACETIME_HOST=... SPACETIME_DB_NAME=... SPACETIME_TOKEN=... RDF_OPERATOR_KEY=... \
      uv run python ../../spikes/tdm-78-robot-page/router_only.py --port 8078
"""
import argparse

import uvicorn
from fastapi import FastAPI

from rdf_orchestrator.robot_commands_api import robot_commands_router

p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=8078)
a = p.parse_args()
app = FastAPI(title="tdm78-router-only")
app.include_router(robot_commands_router)
uvicorn.run(app, host="127.0.0.1", port=a.port)
