"""Validate deployment reference data and print its canonical approval checksum."""

import argparse
from pathlib import Path

from prahari.network_profile import load_network_profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    args = parser.parse_args()
    profile = load_network_profile(args.profile)
    print(f"profile_id={profile.profile_id}")
    print(f"deployment_scope={profile.deployment_scope}")
    print(f"network_profile_sha256={profile.fingerprint}")
    print("Device execution: blocked; no production device adapter is enabled.")


if __name__ == "__main__":
    main()
