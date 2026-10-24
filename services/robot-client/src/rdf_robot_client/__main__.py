from rdf_common.settings import RobotClientSettings, load_settings


def main() -> None:
    load_settings(RobotClientSettings, "robot-client")  # exits non-zero, naming any missing env var
    print("[robot-client] placeholder: Robot PC bridge: YAM arm control, cameras, recording, takeover/teleop, rollouts and eval trials (not implemented yet)")


if __name__ == "__main__":
    main()
