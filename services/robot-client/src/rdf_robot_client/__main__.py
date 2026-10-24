"""Robot PC bridge: YAM arm control, cameras, recording, takeover/teleop, rollouts and eval trials"""
import sys


def main() -> None:
    if "--fake" in sys.argv[1:]:
        # TDM-28: fake robot for laptop integration tests (no arm, no Jetson env vars).
        from .fake import main as fake_main

        fake_main(sys.argv[1:])
        return
    # TDM-49: the real command loop (--sim for the simulated arm); see main.py
    from .main import run

    run(sys.argv[1:])


if __name__ == "__main__":
    main()
