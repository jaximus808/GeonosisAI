from rdf_common.settings import OrchestratorSettings, load_settings


def main() -> None:
    load_settings(OrchestratorSettings, "orchestrator")  # exits non-zero, naming any missing env var
    print("[orchestrator] placeholder: Planner and reviewer loops, dispatcher; writes decisions to the event log (not implemented yet)")


if __name__ == "__main__":
    main()
