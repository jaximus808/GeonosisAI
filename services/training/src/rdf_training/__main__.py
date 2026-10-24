from rdf_common.settings import TrainingSettings, load_settings


def main() -> None:
    load_settings(TrainingSettings, "training")  # exits non-zero, naming any missing env var
    print("[training] placeholder: Episode conversion to LeRobot format, pi0.5 fine-tune, checkpoint export, policy serving (not implemented yet)")


if __name__ == "__main__":
    main()
