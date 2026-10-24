"""Robot PC bridge: YAM arm control, cameras, recording, takeover/teleop, rollouts and eval trials"""
import sys

from rdf_common.settings import RobotClientSettings, load_settings


def main() -> None:
    if "--fake" in sys.argv[1:]:
        # TDM-28: fake robot for laptop integration tests (no arm, no Jetson env vars).
        from .fake import main as fake_main

        fake_main(sys.argv[1:])
        return
    load_settings(RobotClientSettings, "robot-client")  # exits non-zero, naming any missing env var
    print("[robot-client] placeholder: Robot PC bridge: YAM arm control, cameras, recording, takeover/teleop, rollouts and eval trials (not implemented yet)")


if __name__ == "__main__":
    main()
