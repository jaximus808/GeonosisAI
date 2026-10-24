from rdf_common.settings import FactorySettings, load_settings


def main() -> None:
    load_settings(FactorySettings, "factory")  # exits non-zero, naming any missing env var
    print("[factory] placeholder: Factory Droid session management (onboarding, data, training Droids) (not implemented yet)")


if __name__ == "__main__":
    main()
